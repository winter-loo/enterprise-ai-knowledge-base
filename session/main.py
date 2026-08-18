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
from pydantic import BaseModel, ConfigDict, Field

from authz import store as authz_store
from session import store
from session.rag_client import ask_stream
from session.summarizer import update_summary

JsonObject = dict[str, object]


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init_db()
    yield


app = FastAPI(title="Enterprise AI Knowledge Base Session", version="0.1.0", lifespan=lifespan)


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)


class ChatCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)


def row_string(row: DictRow, key: str) -> str:
    return cast(str, row[key])


def sse(event: JsonObject) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def require_session(session_id: str, principal_id: str) -> DictRow:
    session = store.get_session(session_id, principal_id)
    if session is None:
        raise HTTPException(404, "会话不存在或已无访问权限")
    return session


def require_session_project_grant(principal_id: str, project_id: str) -> None:
    """Keep API admission identical to the non-bypass Session RLS policy."""
    if authz_store.project_role(principal_id, project_id) is None:
        raise HTTPException(403, "无权在该 Project 创建或查看自己的会话")


async def chat_stream(
    payload: ChatCompletion,
    principal_id: str,
    project_id: str,
    summary: str,
    history: list[dict[str, str]],
    generation_id: str,
) -> AsyncGenerator[str, None]:
    persisted = False
    answer = ""
    try:
        async for event in ask_stream(
            question=payload.question,
            history=history,
            project_id=project_id,
            principal_id=principal_id,
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
                persisted = store.add_assistant_if_generation_active(payload.session_id, principal_id, generation_id, answer)
                if not persisted:
                    yield sse({"type": "error", "message": "会话已被删除或权限已收回，回答未保存"})
                    return
                yield sse(event)
            else:
                yield sse(event)
    except httpx.HTTPError as exc:
        yield sse({"type": "error", "message": f"RAG 服务调用失败：{exc}"})
    finally:
        if not persisted:
            store.rollback_generation(payload.session_id, principal_id, generation_id)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "enterprise-ai-knowledge-base-session"}


@app.get("/api/v1/chat/health")
def chat_health() -> dict[str, str]:
    return {"status": "ok", "service": "enterprise-ai-knowledge-base-session"}


@app.post("/api/v1/chat/sessions")
def create_session(payload: SessionCreate, x_principal: Annotated[str, Header(min_length=1, max_length=300)]) -> JsonObject:
    require_session_project_grant(x_principal, payload.project_id)
    row = store.create_session(uuid.uuid4().hex, x_principal, payload.project_id)
    return dict(row)


@app.get("/api/v1/chat/sessions")
def sessions(project_id: str, x_principal: Annotated[str, Header(min_length=1, max_length=300)]) -> list[JsonObject]:
    require_session_project_grant(x_principal, project_id)
    return [dict(row) for row in store.list_sessions(x_principal, project_id)]


@app.post("/api/v1/chat/completions")
def chat_completions(payload: ChatCompletion, x_principal: Annotated[str, Header(min_length=1, max_length=300)]) -> StreamingResponse:
    _ = require_session(payload.session_id, x_principal)
    generation_id = uuid.uuid4().hex
    state = store.begin_generation(payload.session_id, x_principal, payload.question, generation_id)
    if state is None:
        raise HTTPException(409, "会话正在生成，或已无访问权限")
    try:
        summary = state["summary"]
        verbatim = state["verbatim"]
        if state["should_compact"]:
            to_summarize, kept = store.split_for_compaction(verbatim)
            new_summary = update_summary(
                summary,
                [{"role": row_string(message, "role"), "content": row_string(message, "content")} for message in to_summarize],
            )
            if new_summary is None:
                history = [{"role": row_string(message, "role"), "content": row_string(message, "content")} for message in verbatim]
            else:
                summary = new_summary
                _ = store.save_summary(
                    payload.session_id,
                    x_principal,
                    summary,
                    cast(int, kept[0]["id"]) if kept else None,
                )
                history = [{"role": row_string(message, "role"), "content": row_string(message, "content")} for message in kept]
        else:
            history = [{"role": row_string(message, "role"), "content": row_string(message, "content")} for message in verbatim]
    except Exception:
        store.rollback_generation(payload.session_id, x_principal, generation_id)
        raise
    return StreamingResponse(
        chat_stream(payload, x_principal, state["project_id"], summary, history, generation_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/v1/chat/history/{session_id}")
def chat_history(session_id: str, x_principal: Annotated[str, Header(min_length=1, max_length=300)]) -> JsonObject:
    _ = require_session(session_id, x_principal)
    messages = store.list_messages(session_id, x_principal)
    return {"session_id": session_id, "messages": [dict(row) for row in messages]}


@app.delete("/api/v1/chat/session/{session_id}")
def clear_chat_session(session_id: str, x_principal: Annotated[str, Header(min_length=1, max_length=300)]) -> JsonObject:
    if not store.clear_session(session_id, x_principal):
        raise HTTPException(404, "会话不存在或已无访问权限")
    return {"session_id": session_id, "deleted": True}
