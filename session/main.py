from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, cast

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from psycopg.rows import DictRow
from pydantic import BaseModel, Field

from session import store
from session.rag_client import ask_stream, resolve_scope
from session.summarizer import update_summary

JsonObject = dict[str, object]


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init_db()
    yield


app = FastAPI(title="Enterprise AI Knowledge Base Session", version="0.1.0", lifespan=lifespan)


class ChatCompletion(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    session_token: str = Field(min_length=32, max_length=200)
    question: str = Field(min_length=1, max_length=2000)
    kb_id: str = "company"
    project_id: str = "default"
    department: str = "general"
    top_k: int = Field(default=5, ge=1, le=10)


def row_string(row: DictRow, key: str) -> str:
    return cast(str, row[key])


def sse(event: JsonObject) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def canonical_project_id(kb_id: str, project_id: str, department: str) -> str:
    try:
        scope = resolve_scope(kb_id, project_id, department)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    return cast(str, scope["project_id"])


async def chat_stream(payload: ChatCompletion, summary: str, history: list[dict[str, str]], generation_id: str) -> AsyncGenerator[str, None]:
    # A generation is one persisted user turn plus its eventual assistant turn.
    # Retrieval and grounded generation are delegated to the RAG service; the
    # generation_id lets this completion step verify that the turn still exists
    # (and was not cleared or superseded) before saving the assistant response.
    persisted = False
    answer = ""
    try:
        async for event in ask_stream(
            question=payload.question,
            history=history,
            kb_id=payload.kb_id,
            project_id=payload.project_id,
            department=payload.department,
            top_k=payload.top_k,
            summary=summary,
        ):
            event_type = event.get("type")
            if event_type == "delta":
                content = event.get("content")
                if isinstance(content, str):
                    answer += content
                yield sse(event)
            elif event_type == "done":
                persisted = store.add_assistant_if_generation_active(
                    payload.session_id, generation_id, answer, payload.kb_id, payload.project_id, payload.department
                )
                if not persisted:
                    yield sse({"type": "error", "message": "会话已被清除，回答未保存"})
                    return
                yield sse(event)
            else:
                # sources, error, and any future event kinds pass through verbatim.
                yield sse(event)
    except httpx.HTTPError as exc:
        yield sse({"type": "error", "message": f"RAG 服务调用失败：{exc}"})
    finally:
        if not persisted:
            store.rollback_generation(payload.session_id, generation_id)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "enterprise-ai-knowledge-base-session"}


@app.get("/api/v1/chat/health")
def chat_health() -> dict[str, str]:
    return {"status": "ok", "service": "enterprise-ai-knowledge-base-session"}


@app.post("/api/v1/chat/completions")
def chat_completions(payload: ChatCompletion) -> StreamingResponse:
    # Resolve the scope through RAG before persisting, so the session records
    # the same canonical project id that retrieval uses ("default" is an alias).
    resolved_project_id = canonical_project_id(payload.kb_id, payload.project_id, payload.department)
    generation_id = uuid.uuid4().hex
    state = store.begin_generation(
        payload.session_id,
        payload.session_token,
        payload.question,
        generation_id,
        payload.kb_id,
        resolved_project_id,
        payload.department,
    )
    if state is None:
        raise HTTPException(409, "会话已清除、凭证不匹配或已有生成请求正在进行")
    try:
        summary = state["summary"]
        verbatim = state["verbatim"]
        if state["should_compact"]:
            # 上下文接近模型上限: 保留最近 KEEP_RECENT_TOKENS 的原文窗口, 把窗口之前的部分并入摘要。
            to_summarize, kept = store.split_for_compaction(verbatim)
            new_summary = update_summary(
                summary,
                [{"role": row_string(message, "role"), "content": row_string(message, "content")} for message in to_summarize],
            )
            if new_summary is None:
                # 摘要失败/无 LLM: 不推进 first_kept_id, 保留全部原文, 下一轮重试。
                history = [{"role": row_string(message, "role"), "content": row_string(message, "content")} for message in verbatim]
            else:
                summary = new_summary
                store.save_summary(
                    payload.session_id,
                    payload.kb_id,
                    resolved_project_id,
                    payload.department,
                    payload.session_token,
                    summary,
                    cast(int, kept[0]["id"]) if kept else None,
                )
                history = [{"role": row_string(message, "role"), "content": row_string(message, "content")} for message in kept]
        else:
            history = [{"role": row_string(message, "role"), "content": row_string(message, "content")} for message in verbatim]
    except Exception:
        # 预流式压缩失败时回滚已提交的 user 行, 避免后续重试一直 409。
        store.rollback_generation(payload.session_id, generation_id)
        raise
    resolved_payload = payload.model_copy(update={"project_id": resolved_project_id})
    return StreamingResponse(
        chat_stream(resolved_payload, summary, history, generation_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/v1/chat/history/{session_id}")
def chat_history(
    session_id: str,
    x_session_token: Annotated[str, Header(min_length=32, max_length=200)],
    kb_id: str = "company",
    project_id: str = "default",
    department: str = "general",
) -> JsonObject:
    resolved_project_id = canonical_project_id(kb_id, project_id, department)
    messages = store.list_messages(session_id, None, kb_id, resolved_project_id, department, x_session_token)
    return {"session_id": session_id, "messages": [dict(row) for row in messages]}


@app.delete("/api/v1/chat/session/{session_id}")
def clear_chat_session(
    session_id: str,
    x_session_token: Annotated[str, Header(min_length=32, max_length=200)],
    kb_id: str = "company",
    project_id: str = "default",
    department: str = "general",
) -> JsonObject:
    resolved_project_id = canonical_project_id(kb_id, project_id, department)
    deleted = store.clear_session(session_id, kb_id, resolved_project_id, department, x_session_token)
    return {"session_id": session_id, "deleted": deleted}
