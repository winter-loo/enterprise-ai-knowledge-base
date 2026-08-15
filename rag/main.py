from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal, cast

import anydoc
import httpx
import pdf_inspector
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from psycopg.rows import DictRow
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from rag import store
from rag.chunking import chunk_text
from rag.openai_responses import (
    build_response_input,
    build_response_request,
    response_answer_text,
    stream_completed,
    stream_error,
)
from rag.openai_responses import (
    stream_delta as stream_content,
)

JsonObject = dict[str, object]


BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
KNOWLEDGE_ASSISTANT_INSTRUCTIONS = "你是企业知识库助手。只能根据参考资料回答；资料不足时明确说不知道。必须保留引用编号，如[1]。不要编造政策、数字或来源。"


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init_db()
    yield


app = FastAPI(title="Enterprise AI Knowledge Base RAG", version="0.1.0", lifespan=lifespan)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class ProjectCreate(BaseModel):
    kb_id: str = Field(default="company", min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class RetrieveRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    kb_id: str = "company"
    project_id: str = "default"
    department: str = "general"
    top_k: int = Field(default=5, ge=1, le=10)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    kb_id: str = "company"
    project_id: str = "default"
    department: str = "general"
    top_k: int = Field(default=5, ge=1, le=10)
    history: list[dict[str, str]] = Field(default_factory=list)
    stream: bool = False


class ScopeResolve(BaseModel):
    kb_id: str = "company"
    project_id: str = "default"
    department: str = "general"


class DocumentImport(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    kb_id: str = "company"
    project_id: str = "default"
    department: str = "general"
    chunking_strategy: Literal["fixed", "recursive", "semantic", "paragraph"] = "recursive"


def ensure_kb(kb_id: str) -> DictRow:
    row = store.ensure_kb(kb_id)
    if not row:
        raise HTTPException(404, "知识库不存在")
    return row


def ensure_project(kb_id: str, project_id: str) -> DictRow:
    row = store.ensure_project(kb_id, project_id)
    if not row:
        raise HTTPException(404, "项目范围不存在")
    return row


def resolve_project_id(kb_id: str, project_id: str) -> str:
    """Validate a document scope in one synchronous worker-pool operation."""
    _ = ensure_kb(kb_id)
    return row_string(ensure_project(kb_id, project_id), "id")


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
        embedder=store.embed if strategy == "semantic" else None,
    )


def row_string(row: DictRow, key: str) -> str:
    return cast(str, row[key])


def fallback_answer(sources: list[DictRow]) -> str:
    if not sources:
        return "知识库中没有找到足够相关的资料，无法基于现有文档可靠回答。"
    excerpts = [re.sub(r"\s+", " ", row_string(source, "content")).strip()[:260] for source in sources[:3]]
    return "根据知识库中检索到的资料：\n" + "\n".join(f"- {text}" for text in excerpts)


def citations_for(sources: list[DictRow]) -> list[dict[str, object]]:
    return [
        {
            "id": source["id"],
            "filename": source["filename"],
            "chunk_index": source["chunk_index"],
            "score": source["score"],
            "excerpt": re.sub(r"\s+", " ", row_string(source, "content")).strip()[:300],
        }
        for source in sources
    ]


def resolve_and_search(question: str, kb_id: str, project_id: str, department: str, top_k: int) -> list[DictRow]:
    """Resolve a scope and run retrieval in one synchronous worker-pool call."""
    _ = ensure_kb(kb_id)
    resolved_project_id = row_string(ensure_project(kb_id, project_id), "id")
    return store.search(question, kb_id, resolved_project_id, department, top_k)


async def llm_answer(question: str, sources: list[DictRow], history: list[dict[str, str]]) -> tuple[str, str]:
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        return fallback_answer(sources), "local-fallback"
    context = "\n\n".join(f"[{i + 1}] {source['filename']}\n{source['content']}" for i, source in enumerate(sources))
    prompt = f"参考资料：\n{context or '无'}\n\n问题：{question}"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                f"{base_url}/responses",
                headers={"Authorization": f"Bearer {api_key}"},
                json=build_response_request(model, KNOWLEDGE_ASSISTANT_INSTRUCTIONS, build_response_input(history, prompt, 6)),
            )
            _ = response.raise_for_status()
            return response_answer_text(cast(object, response.json())), model
    except (httpx.HTTPError, ValueError):
        return fallback_answer(sources), "local-fallback-after-llm-error"


async def ask_stream(payload: AskRequest, sources: list[DictRow]) -> AsyncIterator[str]:
    # A stateless retrieval + grounded-answer stream: history is inline input,
    # and nothing about the conversation is persisted here.
    yield f"data: {json.dumps({'type': 'sources', 'sources': citations_for(sources)}, ensure_ascii=False)}\n\n"
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        answer = fallback_answer(sources)
        yield f"data: {json.dumps({'type': 'delta', 'content': answer}, ensure_ascii=False)}\n\n"
        yield 'data: {"type":"done"}\n\n'
        return
    context = "\n\n".join(f"[{i + 1}] {source['filename']}\n{source['content']}" for i, source in enumerate(sources))
    prompt = f"参考资料：\n{context or '无'}\n\n问题：{payload.question}"
    answer = ""
    completed = False
    try:
        async with (
            httpx.AsyncClient(timeout=90) as client,
            client.stream(
                "POST",
                f"{base_url}/responses",
                headers={"Authorization": f"Bearer {api_key}"},
                json=build_response_request(
                    model,
                    KNOWLEDGE_ASSISTANT_INSTRUCTIONS,
                    build_response_input(payload.history, prompt, 12),
                    stream=True,
                ),
            ) as response,
        ):
            _ = response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    raise RuntimeError("LLM Responses stream returned an unexpected [DONE] marker")
                try:
                    upstream_payload = cast(object, json.loads(data))
                except json.JSONDecodeError as exc:
                    raise RuntimeError("LLM stream returned malformed JSON") from exc
                if not isinstance(upstream_payload, dict):
                    raise RuntimeError("LLM stream returned an invalid event shape")
                error_message = stream_error(cast(object, upstream_payload))
                if error_message:
                    raise RuntimeError(f"LLM stream error: {error_message}")
                if stream_completed(cast(object, upstream_payload)):
                    completed = True
                    break
                delta = stream_content(cast(object, upstream_payload))
                if delta:
                    answer += delta
                    yield f"data: {json.dumps({'type': 'delta', 'content': delta}, ensure_ascii=False)}\n\n"
        if not completed:
            raise RuntimeError("LLM stream ended before its completion marker")
        if not answer:
            raise RuntimeError("LLM stream returned no content")
        yield 'data: {"type":"done"}\n\n'
    except (httpx.HTTPError, RuntimeError) as exc:
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "enterprise-ai-knowledge-base-rag"}


@app.get("/api/knowledge-bases")
def list_kbs() -> list[JsonObject]:
    return [dict(row) for row in store.list_kbs()]


@app.post("/api/knowledge-bases")
def create_kb(payload: KnowledgeBaseCreate) -> dict[str, str]:
    return store.create_kb(payload.name, payload.description)


@app.get("/api/projects")
def list_projects(kb_id: str = "company") -> list[JsonObject]:
    _ = ensure_kb(kb_id)
    return [dict(row) for row in store.list_projects(kb_id)]


@app.post("/api/projects")
def create_project(payload: ProjectCreate) -> dict[str, str]:
    _ = ensure_kb(payload.kb_id)
    return store.create_project(payload.kb_id, payload.name, payload.description)


@app.post("/api/documents/upload")
async def upload_document(
    file: Annotated[UploadFile, File()],
    kb_id: Annotated[str, Form()] = "company",
    project_id: Annotated[str, Form()] = "default",
    department: Annotated[str, Form()] = "general",
    chunking_strategy: Annotated[Literal["fixed", "recursive", "semantic", "paragraph"], Form()] = "recursive",
) -> JsonObject:
    # Scope validation performs synchronous PostgreSQL calls; keep it off the
    # event loop along with semantic chunking and document indexing.
    project_id = await run_in_threadpool(resolve_project_id, kb_id, project_id)
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件不能超过 10MB")
    filename = file.filename or "document.txt"
    text, parser, pdf_type, pages_needing_ocr = parse_document(filename, data)
    # Semantic chunking embeds candidate boundaries synchronously; keep that
    # work off the event loop just like the later indexing step.
    chunks = await run_in_threadpool(chunk_document, text, chunking_strategy)
    if not chunks:
        raise HTTPException(422, "文件中没有可索引的文本")
    document_id = uuid.uuid4().hex
    stored_path = UPLOAD_DIR / f"{document_id}-{Path(filename).name}"
    try:
        _ = stored_path.write_bytes(data)
        # Embedding, per-chunk summaries, and database writes are synchronous
        # network/IO work. Run them in Starlette's worker pool so a large upload
        # cannot block the FastAPI event loop and starve health/chat requests.
        return await run_in_threadpool(
            store.insert_document,
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
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise


@app.get("/api/documents")
def list_documents(kb_id: str = "company") -> list[JsonObject]:
    _ = ensure_kb(kb_id)
    return [dict(row) for row in store.list_documents(kb_id)]


@app.post("/api/retrieve")
def retrieve(payload: RetrieveRequest) -> JsonObject:
    sources = resolve_and_search(payload.question, payload.kb_id, payload.project_id, payload.department, payload.top_k)
    chunks = [
        {
            "id": source["id"],
            "filename": source["filename"],
            "chunk_index": source["chunk_index"],
            "score": source["score"],
            "content": source["content"],
            "summary": source["summary"],
        }
        for source in sources
    ]
    return {"chunks": chunks, "retrieved": len(chunks)}


@app.post("/api/scope/resolve")
def resolve_scope(payload: ScopeResolve) -> dict[str, str]:
    _ = ensure_kb(payload.kb_id)
    project_id = row_string(ensure_project(payload.kb_id, payload.project_id), "id")
    return {"kb_id": payload.kb_id, "project_id": project_id, "department": payload.department}


@app.post("/api/ask", response_model=None)
async def ask(payload: AskRequest) -> JsonObject | StreamingResponse:
    # Retrieval performs synchronous embedding and PostgreSQL calls; keep it off
    # the event loop so a slow search cannot starve concurrent answer streams.
    sources = await run_in_threadpool(resolve_and_search, payload.question, payload.kb_id, payload.project_id, payload.department, payload.top_k)
    if payload.stream:
        return StreamingResponse(
            ask_stream(payload, sources),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    answer, answer_mode = await llm_answer(payload.question, sources, payload.history)
    citations = citations_for(sources)
    return {"answer": answer, "answer_mode": answer_mode, "citations": citations, "retrieved": len(citations)}


@app.post("/api/v1/document/import")
async def import_document(payload: DocumentImport) -> JsonObject:
    # Scope validation performs synchronous PostgreSQL calls; keep it off the
    # event loop along with semantic chunking and document indexing.
    project_id = await run_in_threadpool(resolve_project_id, payload.kb_id, payload.project_id)
    # Semantic chunking can make synchronous embedding requests, so it must
    # also run in the worker pool before insert_document is scheduled there.
    chunks = await run_in_threadpool(chunk_document, payload.content, payload.chunking_strategy)
    if not chunks:
        raise HTTPException(422, "文档内容不能为空")
    # Keep the import endpoint non-blocking for the same reason as uploads:
    # indexing performs synchronous embedding, LLM, and PostgreSQL operations.
    return await run_in_threadpool(
        store.insert_document,
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
