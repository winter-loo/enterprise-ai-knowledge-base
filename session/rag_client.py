from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

JsonObject = dict[str, object]


def base_url() -> str:
    return os.getenv("RAG_BASE_URL", "http://127.0.0.1:8010").rstrip("/")


async def ask_stream(
    *,
    question: str,
    history: list[dict[str, str]],
    project_id: str,
    principal_id: str,
    top_k: int,
    summary: str = "",
) -> AsyncIterator[JsonObject]:
    """Stream stateless RAG under the same trusted Principal as the Session request."""
    payload = {
        "question": question,
        "history": history,
        "project_id": project_id,
        "top_k": top_k,
        "summary": summary,
        "stream": True,
    }
    headers = {"x-principal": principal_id}
    async with httpx.AsyncClient(timeout=120) as client, client.stream("POST", f"{base_url()}/api/ask", json=payload, headers=headers) as response:
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
                yield cast(JsonObject, event)
