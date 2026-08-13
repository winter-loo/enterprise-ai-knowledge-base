from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal, TypedDict, cast

import anydoc
import httpx
import pdf_inspector
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import DictRow
from pydantic import BaseModel, Field

from app import postgres_store
from app.chunking import chunk_text

JsonObject = dict[str, object]


class ChatMessage(TypedDict):
    role: str
    content: str


class ChatChoiceDelta(TypedDict, total=False):
    content: str


class ChatChoice(TypedDict):
    delta: ChatChoiceDelta


class StreamPayload(TypedDict, total=False):
    type: str
    delta: str
    choices: list[ChatChoice]


class CompletionMessage(TypedDict):
    content: str


class CompletionChoice(TypedDict):
    message: CompletionMessage


class CompletionPayload(TypedDict):
    choices: list[CompletionChoice]


BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    postgres_store.init_db()
    yield


app = FastAPI(title="Enterprise AI Knowledge Base", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    kb_id: str = "company"
    project_id: str = "default"
    department: str = "general"
    top_k: int = Field(default=5, ge=1, le=10)
    history: list[dict[str, str]] = Field(default_factory=list)


class DocumentImport(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    kb_id: str = "company"
    project_id: str = "default"
    department: str = "general"
    chunking_strategy: Literal["fixed", "recursive", "semantic", "paragraph"] = "recursive"


class ChatCompletion(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2000)
    kb_id: str = "company"
    project_id: str = "default"
    department: str = "general"
    top_k: int = Field(default=5, ge=1, le=10)


def ensure_kb(kb_id: str) -> DictRow:
    row = postgres_store.ensure_kb(kb_id)
    if not row:
        raise HTTPException(404, "知识库不存在")
    return row


def ensure_project(kb_id: str, project_id: str) -> DictRow:
    row = postgres_store.ensure_project(kb_id, project_id)
    if not row:
        raise HTTPException(404, "项目范围不存在")
    return row


def extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".doc", ".docx", ".docm", ".ppt", ".pptx", ".pptm", ".xls", ".xlsx", ".xlsm", ".xlsb", ".odt", ".ods", ".odp", ".rtf", ".epub", ".csv"}:
        try:
            document_format = cast(anydoc.Format, suffix.removeprefix("."))
            return anydoc.to_markdown_bytes(data, document_format)
        except Exception as exc:
            raise HTTPException(415, f"文档解析失败：{exc}") from exc
    if suffix in {".txt", ".md", ".json", ".log", ".html", ".xml"}:
        return data.decode("utf-8", errors="replace")
    raise HTTPException(415, "不支持的文档格式")


def parse_document(filename: str, data: bytes) -> tuple[str, str, str | None, list[int]]:
    if Path(filename).suffix.lower() == ".pdf":
        try:
            result = pdf_inspector.process_pdf_bytes(data)
            return result.markdown or "", "pdf-inspector", result.pdf_type, result.pages_needing_ocr
        except Exception as exc:
            raise HTTPException(415, f"文档解析失败：{exc}") from exc
    text = extract_text(filename, data)
    parser = "anydoc" if Path(filename).suffix.lower() not in {".txt", ".md", ".json", ".log", ".html", ".xml"} else "plain-text"
    return text, parser, None, []


def split_text(text: str, size: int = 700, overlap: int = 100) -> list[str]:
    """Compatibility wrapper for the previous public helper; recursive is the new default."""
    return chunk_text(text, strategy="recursive", size=size, overlap=overlap)


def chunk_document(text: str, strategy: str) -> list[str]:
    return chunk_text(
        text,
        strategy=strategy,
        embedder=postgres_store.embed if strategy == "semantic" else None,
    )


def row_string(row: DictRow, key: str) -> str:
    return cast(str, row[key])


def fallback_answer(sources: list[DictRow]) -> str:
    if not sources:
        return "知识库中没有找到足够相关的资料，无法基于现有文档可靠回答。"
    excerpts = [re.sub(r"\s+", " ", row_string(source, "content")).strip()[:260] for source in sources[:3]]
    return "根据知识库中检索到的资料：\n" + "\n".join(f"- {text}" for text in excerpts)


async def llm_answer(question: str, sources: list[DictRow], history: list[dict[str, str]]) -> tuple[str, str]:
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        return fallback_answer(sources), "local-fallback"
    context = "\n\n".join(f"[{i + 1}] {source['filename']}\n{source['content']}" for i, source in enumerate(sources))
    messages = [
        {"role": "system", "content": "你是企业知识库助手。只能根据参考资料回答；资料不足时明确说不知道。必须保留引用编号，如[1]。不要编造政策、数字或来源。"}
    ]
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": f"参考资料：\n{context or '无'}\n\n问题：{question}"})
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                f"{base_url}/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json={"model": model, "messages": messages, "temperature": 0.1}
            )
            _ = response.raise_for_status()
            payload = cast(CompletionPayload, response.json())
            return payload["choices"][0]["message"]["content"], model
    except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError):
        return fallback_answer(sources), "local-fallback-after-llm-error"


def stream_content(payload: StreamPayload) -> str:
    if payload.get("type") == "response.output_text.delta":
        return payload.get("delta", "")
    choices = payload.get("choices", [])
    return choices[0].get("delta", {}).get("content", "") if choices else ""


async def chat_stream(payload: ChatCompletion, sources: list[DictRow], history: list[dict[str, str]]) -> AsyncIterator[str]:
    yield f"data: {json.dumps({'type': 'sources', 'sources': [{'filename': s['filename'], 'chunk_index': s['chunk_index'], 'score': s['score']} for s in sources]}, ensure_ascii=False)}\n\n"
    base_url, api_key, model = os.getenv("LLM_BASE_URL", "").rstrip("/"), os.getenv("LLM_API_KEY", ""), os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        yield f"data: {json.dumps({'type': 'error', 'message': 'LLM configuration is required'}, ensure_ascii=False)}\n\n"
        return
    context = "\n\n".join(f"[{i + 1}] {source['filename']}\n{source['content']}" for i, source in enumerate(sources))
    messages = [
        {"role": "system", "content": "你是企业知识库助手。只能根据参考资料回答；资料不足时明确说不知道。必须保留引用编号。"},
        *history[-12:],
        {"role": "user", "content": f"参考资料：\n{context or '无'}\n\n问题：{payload.question}"},
    ]
    answer = ""
    try:
        async with (
            httpx.AsyncClient(timeout=90) as client,
            client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": messages, "temperature": 0.1, "stream": True},
            ) as response,
        ):
            _ = response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                try:
                    delta = stream_content(cast(StreamPayload, json.loads(line[5:].strip())))
                except json.JSONDecodeError:
                    continue
                if delta:
                    answer += delta
                    yield f"data: {json.dumps({'type': 'delta', 'content': delta}, ensure_ascii=False)}\n\n"
        if not answer:
            raise RuntimeError("LLM stream returned no content")
        postgres_store.add_message(payload.session_id, "assistant", answer)
        yield 'data: {"type":"done"}\n\n'
    except (httpx.HTTPError, RuntimeError) as exc:
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "enterprise-ai-knowledge-base"}


@app.get("/api/knowledge-bases")
def list_kbs() -> list[JsonObject]:
    return [dict(row) for row in postgres_store.list_kbs()]


@app.post("/api/knowledge-bases")
def create_kb(payload: KnowledgeBaseCreate) -> dict[str, str]:
    return postgres_store.create_kb(payload.name, payload.description)


@app.get("/api/projects")
def list_projects(kb_id: str = "company") -> list[JsonObject]:
    _ = ensure_kb(kb_id)
    return [dict(row) for row in postgres_store.list_projects(kb_id)]


@app.post("/api/projects")
def create_project(payload: dict[str, str]) -> dict[str, str]:
    kb_id = payload.get("kb_id", "company")
    _ = ensure_kb(kb_id)
    return postgres_store.create_project(kb_id, payload["name"], payload.get("description", ""))


@app.post("/api/documents/upload")
async def upload_document(
    file: Annotated[UploadFile, File()],
    kb_id: Annotated[str, Form()] = "company",
    project_id: Annotated[str, Form()] = "default",
    department: Annotated[str, Form()] = "general",
    chunking_strategy: Annotated[Literal["fixed", "recursive", "semantic", "paragraph"], Form()] = "recursive",
) -> JsonObject:
    _ = ensure_kb(kb_id)
    project_id = row_string(ensure_project(kb_id, project_id), "id")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件不能超过 10MB")
    filename = file.filename or "document.txt"
    text, parser, pdf_type, pages_needing_ocr = parse_document(filename, data)
    chunks = chunk_document(text, chunking_strategy)
    if not chunks:
        raise HTTPException(422, "文件中没有可索引的文本")
    document_id = uuid.uuid4().hex
    stored_path = UPLOAD_DIR / f"{document_id}-{Path(filename).name}"
    _ = stored_path.write_bytes(data)
    return postgres_store.insert_document(
        kb_id=kb_id,
        project_id=project_id,
        document_id=document_id,
        filename=filename,
        department=department,
        parser=parser,
        pdf_type=pdf_type,
        pages_needing_ocr=pages_needing_ocr,
        chunks=chunks,
        stored_path=str(stored_path),
        chunking_strategy=chunking_strategy,
    )


@app.get("/api/documents")
def list_documents(kb_id: str = "company") -> list[JsonObject]:
    _ = ensure_kb(kb_id)
    return [dict(row) for row in postgres_store.list_documents(kb_id)]


@app.post("/api/ask")
async def ask(payload: AskRequest) -> JsonObject:
    _ = ensure_kb(payload.kb_id)
    project_id = row_string(ensure_project(payload.kb_id, payload.project_id), "id")
    sources = postgres_store.search(payload.question, payload.kb_id, project_id, payload.department, payload.top_k)
    answer, answer_mode = await llm_answer(payload.question, sources, payload.history)
    citations = [
        {
            "id": source["id"],
            "filename": source["filename"],
            "chunk_index": source["chunk_index"],
            "score": source["score"],
            "excerpt": re.sub(r"\s+", " ", row_string(source, "content"))[:300],
        }
        for source in sources
    ]
    return {"answer": answer, "answer_mode": answer_mode, "citations": citations, "retrieved": len(citations)}


@app.post("/api/v1/document/import")
def import_document(payload: DocumentImport) -> JsonObject:
    _ = ensure_kb(payload.kb_id)
    project_id = row_string(ensure_project(payload.kb_id, payload.project_id), "id")
    chunks = chunk_document(payload.content, payload.chunking_strategy)
    if not chunks:
        raise HTTPException(422, "文档内容不能为空")
    return postgres_store.insert_document(
        kb_id=payload.kb_id,
        project_id=project_id,
        document_id=uuid.uuid4().hex,
        filename=payload.title,
        department=payload.department,
        parser="plain-text",
        pdf_type=None,
        pages_needing_ocr=[],
        chunks=chunks,
        stored_path="",
        chunking_strategy=payload.chunking_strategy,
    )


@app.post("/api/v1/chat/completions")
def chat_completions(payload: ChatCompletion) -> StreamingResponse:
    _ = ensure_kb(payload.kb_id)
    project_id = row_string(ensure_project(payload.kb_id, payload.project_id), "id")
    history = cast(list[dict[str, str]], postgres_store.list_messages(payload.session_id, 12))
    sources = postgres_store.search(payload.question, payload.kb_id, project_id, payload.department, payload.top_k)
    postgres_store.add_message(payload.session_id, "user", payload.question)
    return StreamingResponse(chat_stream(payload, sources, history), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/v1/chat/history/{session_id}")
def chat_history(session_id: str) -> JsonObject:
    return {"session_id": session_id, "messages": [dict(row) for row in postgres_store.list_messages(session_id)]}


@app.delete("/api/v1/chat/session/{session_id}")
def clear_chat_session(session_id: str) -> JsonObject:
    return {"session_id": session_id, "deleted": postgres_store.clear_session(session_id)}
