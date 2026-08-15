from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

JsonObject = dict[str, object]


def base_url() -> str:
    return os.getenv("RAG_BASE_URL", "http://127.0.0.1:8010").rstrip("/")


def _fastapi_detail(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    detail = cast(dict[str, object], body).get("detail")
    return detail if isinstance(detail, str) else None


def resolve_scope(kb_id: str, project_id: str, department: str) -> JsonObject:
    """Canonicalize a scope through the RAG service; raises RuntimeError on a bad scope."""
    payload = {"kb_id": kb_id, "project_id": project_id, "department": department}
    with httpx.Client(timeout=10) as client:
        response = client.post(f"{base_url()}/api/scope/resolve", json=payload)
    if response.is_error:
        raise RuntimeError(_fastapi_detail(response) or "RAG 范围解析失败")
    return cast(JsonObject, response.json())


async def ask_stream(
    *,
    question: str,
    history: list[dict[str, str]],
    kb_id: str,
    project_id: str,
    department: str,
    top_k: int,
) -> AsyncIterator[JsonObject]:
    """Stream the stateless RAG ask endpoint and yield parsed SSE events."""
    payload = {
        "question": question,
        "history": history,
        "kb_id": kb_id,
        "project_id": project_id,
        "department": department,
        "top_k": top_k,
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=120) as client, client.stream("POST", f"{base_url()}/api/ask", json=payload) as response:
        _ = response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                event: Any = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event
