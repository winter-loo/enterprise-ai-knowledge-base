import json
from datetime import UTC, datetime

import httpx
import pytest

from authz import store as authz_store
from session import store
from session.main import app
from session.rag_client import ask_stream as rag_ask_stream


@pytest.mark.anyio
async def test_session_rag_request_forwards_principal_not_scope_token(monkeypatch):
    captured = None

    async def handler(request):
        nonlocal captured
        captured = request
        return httpx.Response(200, text='data: {"type":"done"}\n\n')

    async_client = httpx.AsyncClient
    monkeypatch.setattr("session.rag_client.httpx.AsyncClient", lambda **_kwargs: async_client(transport=httpx.MockTransport(handler)))

    events = [
        event
        async for event in rag_ask_stream(
            question="问题",
            history=[],
            project_id="p-1",
            principal_id="alice",
            top_k=5,
        )
    ]

    assert events == [{"type": "done"}]
    assert captured is not None
    assert captured.headers["x-principal"] == "alice"
    assert "x-scope-context" not in captured.headers
    assert "department" not in json.loads(captured.content)


@pytest.fixture
def client(monkeypatch):
    rows = {
        "s-1": {
            "id": "s-1",
            "project_id": "p-1",
            "title": "新的研究",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    }

    monkeypatch.setattr(authz_store, "has_permission", lambda principal, action, project_id=None: principal == "alice" and project_id == "p-1")
    monkeypatch.setattr(authz_store, "project_role", lambda principal, project_id: "viewer" if principal == "alice" and project_id == "p-1" else None)
    monkeypatch.setattr(
        store,
        "create_session",
        lambda session_id, owner, project_id: {
            "id": session_id,
            "project_id": project_id,
            "title": "新的研究",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    )
    monkeypatch.setattr(store, "list_sessions", lambda principal, project_id: [rows["s-1"]] if principal == "alice" and project_id == "p-1" else [])
    monkeypatch.setattr(store, "get_session", lambda session_id, principal: rows.get(session_id) if principal == "alice" else None)
    monkeypatch.setattr(
        store,
        "list_messages",
        lambda session_id, principal, limit=None: (
            [{"role": "user", "content": "问题", "created_at": datetime(2026, 1, 1, tzinfo=UTC)}] if session_id == "s-1" and principal == "alice" else []
        ),
    )
    monkeypatch.setattr(store, "clear_session", lambda session_id, principal: session_id == "s-1" and principal == "alice")
    monkeypatch.setattr(
        store,
        "begin_generation",
        lambda session_id, principal, content, generation_id: (
            {
                "summary": "",
                "verbatim": [],
                "should_compact": False,
                "project_id": "p-1",
            }
            if session_id == "s-1" and principal == "alice"
            else None
        ),
    )
    monkeypatch.setattr(store, "add_assistant_if_generation_active", lambda *_args: True)
    monkeypatch.setattr(store, "rollback_generation", lambda *_args: None)

    async def fake_ask_stream(**_kwargs):
        yield {"type": "delta", "content": "回答"}
        yield {"type": "done"}

    monkeypatch.setattr("session.main.ask_stream", fake_ask_stream)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
async def test_session_history_is_private_even_for_platform_administrator(client):
    async with client as http:
        owner = await http.get("/api/v1/chat/history/s-1", headers={"x-principal": "alice"})
        administrator = await http.get("/api/v1/chat/history/s-1", headers={"x-principal": "admin"})

    assert owner.status_code == 200
    assert owner.json()["messages"][0]["content"] == "问题"
    assert administrator.status_code == 404


@pytest.mark.anyio
async def test_revoked_or_unknown_session_never_creates_a_replacement(client, monkeypatch):
    monkeypatch.setattr(store, "get_session", lambda *_args: None)
    async with client as http:
        response = await http.get("/api/v1/chat/history/s-1", headers={"x-principal": "alice"})

    assert response.status_code == 404
    assert response.json()["detail"] == "会话不存在或已无访问权限"


@pytest.mark.anyio
async def test_platform_administrator_needs_an_explicit_grant_for_own_session(client):
    async with client as http:
        response = await http.post(
            "/api/v1/chat/sessions",
            json={"project_id": "p-1"},
            headers={"x-principal": "admin"},
        )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_chat_completion_uses_session_project_and_principal(client, monkeypatch):
    captured = {}

    async def fake_ask_stream(**kwargs):
        captured.update(kwargs)
        yield {"type": "done"}

    monkeypatch.setattr("session.main.ask_stream", fake_ask_stream)
    async with client as http:
        response = await http.post(
            "/api/v1/chat/completions",
            json={"session_id": "s-1", "question": "继续", "top_k": 4},
            headers={"x-principal": "alice"},
        )

    assert response.status_code == 200
    assert '"type": "done"' in response.text
    assert captured["project_id"] == "p-1"
    assert captured["principal_id"] == "alice"
