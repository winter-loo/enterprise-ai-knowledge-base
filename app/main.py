from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anydoc
import httpx
import pdf_inspector
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import postgres_store

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


def ensure_kb(kb_id: str) -> dict[str, Any]:
    row = postgres_store.ensure_kb(kb_id)
    if not row:
        raise HTTPException(404, "知识库不存在")
    return row


def ensure_project(kb_id: str, project_id: str) -> dict[str, Any]:
    row = postgres_store.ensure_project(kb_id, project_id)
    if not row:
        raise HTTPException(404, "项目范围不存在")
    return row


def extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".doc", ".docx", ".docm", ".ppt", ".pptx", ".pptm", ".xls", ".xlsx", ".xlsm", ".xlsb", ".odt", ".ods", ".odp", ".rtf", ".epub", ".csv"}:
        try:
            return anydoc.to_markdown_bytes(data, suffix.removeprefix("."))
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
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end), text.rfind(".", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return [chunk for chunk in chunks if chunk]


def fallback_answer(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return "知识库中没有找到足够相关的资料，无法基于现有文档可靠回答。"
    excerpts = [re.sub(r"\s+", " ", source["content"]).strip()[:260] for source in sources[:3]]
    return "根据知识库中检索到的资料：\n" + "\n".join(f"- {text}" for text in excerpts)


async def llm_answer(question: str, sources: list[dict[str, Any]], history: list[dict[str, str]]) -> tuple[str, str]:
    base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if not base_url or not api_key:
        return fallback_answer(sources), "local-fallback"
    context = "\n\n".join(f"[{i + 1}] {source['filename']}\n{source['content']}" for i, source in enumerate(sources))
    messages = [{"role": "system", "content": "你是企业知识库助手。只能根据参考资料回答；资料不足时明确说不知道。必须保留引用编号，如[1]。不要编造政策、数字或来源。"}]
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": f"参考资料：\n{context or '无'}\n\n问题：{question}"})
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(f"{base_url}/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json={"model": model, "messages": messages, "temperature": 0.1})
            response.raise_for_status()
            payload = response.json()
            return payload["choices"][0]["message"]["content"], model
    except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError):
        return fallback_answer(sources), "local-fallback-after-llm-error"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "enterprise-ai-knowledge-base"}


@app.get("/api/knowledge-bases")
def list_kbs() -> list[dict[str, Any]]:
    return [dict(row) for row in postgres_store.list_kbs()]


@app.post("/api/knowledge-bases")
def create_kb(payload: KnowledgeBaseCreate) -> dict[str, Any]:
    return postgres_store.create_kb(payload.name, payload.description)


@app.get("/api/projects")
def list_projects(kb_id: str = "company") -> list[dict[str, Any]]:
    ensure_kb(kb_id)
    return [dict(row) for row in postgres_store.list_projects(kb_id)]


@app.post("/api/projects")
def create_project(payload: dict[str, str]) -> dict[str, str]:
    kb_id = payload.get("kb_id", "company")
    ensure_kb(kb_id)
    return postgres_store.create_project(kb_id, payload["name"], payload.get("description", ""))


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...), kb_id: str = Form("company"), project_id: str = Form("default"), department: str = Form("general")) -> dict[str, Any]:
    ensure_kb(kb_id)
    project_id = ensure_project(kb_id, project_id)["id"]
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件不能超过 10MB")
    filename = file.filename or "document.txt"
    text, parser, pdf_type, pages_needing_ocr = parse_document(filename, data)
    chunks = split_text(text)
    if not chunks:
        raise HTTPException(422, "文件中没有可索引的文本")
    document_id = uuid.uuid4().hex
    stored_path = UPLOAD_DIR / f"{document_id}-{Path(filename).name}"
    stored_path.write_bytes(data)
    return postgres_store.insert_document(kb_id=kb_id, project_id=project_id, document_id=document_id, filename=filename, department=department, parser=parser, pdf_type=pdf_type, pages_needing_ocr=pages_needing_ocr, chunks=chunks, stored_path=str(stored_path))


@app.get("/api/documents")
def list_documents(kb_id: str = "company") -> list[dict[str, Any]]:
    ensure_kb(kb_id)
    return [dict(row) for row in postgres_store.list_documents(kb_id)]


@app.post("/api/ask")
async def ask(payload: AskRequest) -> dict[str, Any]:
    ensure_kb(payload.kb_id)
    project_id = ensure_project(payload.kb_id, payload.project_id)["id"]
    sources = postgres_store.search(payload.question, payload.kb_id, project_id, payload.department, payload.top_k)
    answer, answer_mode = await llm_answer(payload.question, sources, payload.history)
    citations = [{"id": source["id"], "filename": source["filename"], "chunk_index": source["chunk_index"], "score": source["score"], "excerpt": re.sub(r"\s+", " ", source["content"])[:300]} for source in sources]
    return {"answer": answer, "answer_mode": answer_mode, "citations": citations, "retrieved": len(citations)}
