from __future__ import annotations

import asyncio
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
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from psycopg.rows import DictRow
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from authz import store as authz_store
from rag import store
from rag.chunking import chunk_text
from shared.openai_responses import (
    build_response_input,
    build_response_request,
    response_answer_text,
    stream_completed,
    stream_error,
)
from shared.openai_responses import (
    stream_delta as stream_content,
)

JsonObject = dict[str, object]


BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
KNOWLEDGE_ASSISTANT_INSTRUCTIONS = "你是企业知识库助手。只能根据参考资料回答；资料不足时明确说不知道。必须保留引用编号，如[1]。不要编造政策、数字或来源。"

# 支持入库的文档后缀, 供上传接口与命令行批量导入复用。anydoc 与纯文本分别
# 对应 extract_text 的两条解析路径, pdf 由 parse_document 用 pdf-inspector 处理。
ANYDOC_SUFFIXES = frozenset(
    {
        ".doc",
        ".docx",
        ".docm",
        ".ppt",
        ".pptx",
        ".pptm",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".xlsb",
        ".odt",
        ".ods",
        ".odp",
        ".rtf",
        ".epub",
        ".csv",
    }
)
PLAIN_TEXT_SUFFIXES = frozenset({".txt", ".md", ".json", ".log", ".html", ".xml"})
SUPPORTED_SUFFIXES = ANYDOC_SUFFIXES | PLAIN_TEXT_SUFFIXES | frozenset({".pdf"})


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init_db()
    yield


app = FastAPI(title="Enterprise AI Knowledge Base RAG", version="0.1.0", lifespan=lifespan)


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class RetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    project_id: str = Field(min_length=1, max_length=200)
    top_k: int = Field(default=5, ge=1, le=10)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    project_id: str = Field(min_length=1, max_length=200)
    top_k: int = Field(default=5, ge=1, le=10)
    history: list[dict[str, str]] = Field(default_factory=list)
    summary: str = ""
    stream: bool = False


class DocumentImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    project_id: str = Field(min_length=1, max_length=200)
    chunking_strategy: Literal["fixed", "recursive", "semantic", "paragraph"] = "recursive"


def ensure_company_kb() -> DictRow:
    row = store.ensure_company_kb()
    if not row:
        raise HTTPException(404, "公司知识库不存在")
    return row


def ensure_project(project_id: str) -> DictRow:
    row = store.ensure_project(project_id)
    if not row:
        raise HTTPException(404, "Project 不存在")
    return row


def resolve_project_id(project_id: str) -> str:
    _ = ensure_company_kb()
    return row_string(ensure_project(project_id), "id")


def require_project_permission(principal_id: str, action: str, project_id: str) -> None:
    if not authz_store.has_permission(principal_id, action, project_id):
        raise HTTPException(403, "无权访问该 Project")


def resolve_and_search(question: str, project_id: str, principal_id: str, top_k: int) -> list[DictRow]:
    """Validate the Project and run retrieval under the stable Principal."""
    resolved_project_id = resolve_project_id(project_id)
    require_project_permission(principal_id, "retrieve", resolved_project_id)
    return store.search(question, resolved_project_id, principal_id, top_k)


def extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in ANYDOC_SUFFIXES:
        try:
            document_format = cast(anydoc.Format, suffix.removeprefix("."))
            return anydoc.to_markdown_bytes(data, document_format)
        except Exception as exc:
            raise HTTPException(415, f"文档解析失败：{exc}") from exc
    if suffix in PLAIN_TEXT_SUFFIXES:
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
    parser = "anydoc" if Path(filename).suffix.lower() not in PLAIN_TEXT_SUFFIXES else "plain-text"
    return text, parser, None, []


def _index_uploaded_data(
    *,
    data: bytes,
    filename: str,
    project_id: str,
    principal_id: str,
    chunking_strategy: str,
    on_progress: store.ProgressCallback | None = None,
) -> JsonObject:
    def report(stage: str, message: str, completed: int, total: int, percent: int) -> None:
        if on_progress is not None:
            on_progress({"stage": stage, "message": message, "completed": completed, "total": total, "percent": percent})

    report("parsing", "解析文档", 0, 1, 10)
    text, parser, pdf_type, pages_needing_ocr = parse_document(filename, data)
    chunks = chunk_document(text, chunking_strategy)
    if not chunks:
        raise HTTPException(422, "文件中没有可索引的文本")
    report("chunking", "切分文档", len(chunks), len(chunks), 30)
    document_id = uuid.uuid4().hex
    stored_path = UPLOAD_DIR / f"{document_id}-{Path(filename).name}"
    try:
        _ = stored_path.write_bytes(data)
        return store.insert_document(
            project_id=project_id,
            principal_id=principal_id,
            document_id=document_id,
            filename=filename,
            parser=parser,
            pdf_type=pdf_type,
            pages_needing_ocr=pages_needing_ocr,
            chunks=chunks,
            stored_path=str(stored_path),
            chunking_strategy=chunking_strategy,
            on_progress=on_progress,
        )
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise


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
    return "根据知识库中检索到的资料：\n" + "\n".join(f"[{index + 1}] {text}" for index, text in enumerate(excerpts))


def citations_with_indexes(sources: list[DictRow]) -> list[dict[str, object]]:
    return [
        {
            "id": source["id"],
            "filename": source["filename"],
            "chunk_index": source["chunk_index"],
            "score": source["score"],
            "excerpt": re.sub(r"\s+", " ", row_string(source, "content")).strip()[:300],
            "citation_index": index + 1,
        }
        for index, source in enumerate(sources)
    ]


def cited_sources(answer: str, sources: list[DictRow]) -> list[dict[str, object]]:
    """Return only the citations the answer actually references via [N] markers."""
    citations = citations_with_indexes(sources)
    cited_indexes = [int(match.group(1)) - 1 for match in re.finditer(r"\[(\d+)\]", answer)]
    valid = sorted({index for index in cited_indexes if 0 <= index < len(sources)})
    return [citations[index] for index in valid]


def build_prompt(question: str, sources: list[DictRow], summary: str = "") -> str:
    context = "\n\n".join(f"[{i + 1}] {source['filename']}\n{source['content']}" for i, source in enumerate(sources))
    prefix = f"对话历史摘要：\n{summary}\n\n" if summary else ""
    return f"{prefix}参考资料：\n{context or '无'}\n\n问题：{question}"


async def llm_answer(question: str, sources: list[DictRow], history: list[dict[str, str]], summary: str = "") -> tuple[str, str]:
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        return fallback_answer(sources), "local-fallback"
    prompt = build_prompt(question, sources, summary)
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
    # and nothing about the conversation is persisted here. Citations are sent
    # after the answer so they can be narrowed to the chunks the answer cites.
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        answer = fallback_answer(sources)
        yield f"data: {json.dumps({'type': 'delta', 'content': answer}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'sources', 'sources': cited_sources(answer, sources)}, ensure_ascii=False)}\n\n"
        yield 'data: {"type":"done"}\n\n'
        return
    prompt = build_prompt(payload.question, sources, payload.summary)
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
                    # The session service already sizes history to its token budget,
                    # so pass it through without re-truncating to a fixed count.
                    build_response_input(payload.history, prompt, len(payload.history) or 1),
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
        yield f"data: {json.dumps({'type': 'sources', 'sources': cited_sources(answer, sources)}, ensure_ascii=False)}\n\n"
        yield 'data: {"type":"done"}\n\n'
    except (httpx.HTTPError, RuntimeError) as exc:
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "enterprise-ai-knowledge-base-rag"}


@app.get("/api/projects")
def list_projects(x_principal: Annotated[str, Header(min_length=1, max_length=300)]) -> list[JsonObject]:
    _ = ensure_company_kb()
    project_ids = authz_store.list_accessible_project_ids(x_principal)
    return [dict(row) for row in store.list_projects(project_ids)]


@app.post("/api/projects")
def create_project(payload: ProjectCreate, x_principal: Annotated[str, Header(min_length=1, max_length=300)]) -> dict[str, str]:
    if not authz_store.has_permission(x_principal, "project:create"):
        raise HTTPException(403, "只有 Project Manager 或平台管理员可以创建 Project")
    created = store.create_project(payload.name, payload.description)
    try:
        _ = authz_store.upsert_grant(x_principal, created["id"], "manager")
    except Exception:
        store.delete_project(created["id"])
        raise
    return {**created, "description": payload.description}


@app.post("/api/documents/upload")
async def upload_document(
    file: Annotated[UploadFile, File()],
    x_principal: Annotated[str, Header(min_length=1, max_length=300)],
    project_id: Annotated[str, Form()] = "",
    chunking_strategy: Annotated[Literal["fixed", "recursive", "semantic", "paragraph"], Form()] = "recursive",
) -> StreamingResponse:
    project_id = await run_in_threadpool(resolve_project_id, project_id)
    require_project_permission(x_principal, "document:write", project_id)
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件不能超过 10MB")
    filename = file.filename or "document.txt"

    async def events() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[JsonObject | None] = asyncio.Queue()

        def progress(event: store.IndexProgress) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, dict(event))

        async def index() -> None:
            try:
                result = await run_in_threadpool(
                    _index_uploaded_data,
                    data=data,
                    filename=filename,
                    project_id=project_id,
                    principal_id=x_principal,
                    chunking_strategy=chunking_strategy,
                    on_progress=progress,
                )
                chunk_count = cast(int, result["chunk_count"])
                await queue.put(
                    {
                        "stage": "complete",
                        "message": "索引完成",
                        "completed": chunk_count,
                        "total": chunk_count,
                        "percent": 100,
                        "result": result,
                    }
                )
            except store.EmbeddingUnavailableError as exc:
                await queue.put({"stage": "error", "message": str(exc), "status": 503, "percent": 0})
            except HTTPException as exc:
                await queue.put({"stage": "error", "message": str(exc.detail), "status": exc.status_code, "percent": 0})
            except Exception:
                await queue.put({"stage": "error", "message": "索引失败，请稍后重试", "status": 500, "percent": 0})
            finally:
                await queue.put(None)

        task = asyncio.create_task(index())
        while (event := await queue.get()) is not None:
            yield json.dumps(event, ensure_ascii=False) + "\n"
        await task

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/api/documents")
def list_documents(
    project_id: str,
    x_principal: Annotated[str, Header(min_length=1, max_length=300)],
) -> list[JsonObject]:
    resolved_project_id = resolve_project_id(project_id)
    require_project_permission(x_principal, "document:read", resolved_project_id)
    return [dict(row) for row in store.list_documents(resolved_project_id, x_principal)]


@app.get("/api/evidence/{chunk_id}")
def get_evidence(
    chunk_id: str,
    project_id: str,
    x_principal: Annotated[str, Header(min_length=1, max_length=300)],
) -> JsonObject:
    resolved_project_id = resolve_project_id(project_id)
    require_project_permission(x_principal, "evidence:read", resolved_project_id)
    row = store.get_evidence(chunk_id, resolved_project_id, x_principal)
    if not row:
        raise HTTPException(404, "参考资料片段不存在")
    return dict(row)


@app.post("/api/retrieve")
def retrieve(payload: RetrieveRequest, x_principal: Annotated[str, Header(min_length=1, max_length=300)]) -> JsonObject:
    sources = resolve_and_search(payload.question, payload.project_id, x_principal, payload.top_k)
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


@app.post("/api/ask", response_model=None)
async def ask(payload: AskRequest, x_principal: Annotated[str, Header(min_length=1, max_length=300)]) -> JsonObject | StreamingResponse:
    sources = await run_in_threadpool(resolve_and_search, payload.question, payload.project_id, x_principal, payload.top_k)
    if payload.stream:
        return StreamingResponse(
            ask_stream(payload, sources),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    answer, answer_mode = await llm_answer(payload.question, sources, payload.history, payload.summary)
    citations = cited_sources(answer, sources)
    return {"answer": answer, "answer_mode": answer_mode, "citations": citations, "retrieved": len(sources)}


@app.post("/api/v1/document/import")
async def import_document(payload: DocumentImport, x_principal: Annotated[str, Header(min_length=1, max_length=300)]) -> JsonObject:
    project_id = await run_in_threadpool(resolve_project_id, payload.project_id)
    require_project_permission(x_principal, "document:write", project_id)
    chunks = await run_in_threadpool(chunk_document, payload.content, payload.chunking_strategy)
    if not chunks:
        raise HTTPException(422, "文档内容不能为空")
    return await run_in_threadpool(
        store.insert_document,
        project_id=project_id,
        principal_id=x_principal,
        document_id=uuid.uuid4().hex,
        filename=payload.title,
        parser="plain-text",
        pdf_type=None,
        pages_needing_ocr=[],
        chunks=chunks,
        stored_path="",
        chunking_strategy=payload.chunking_strategy,
    )
