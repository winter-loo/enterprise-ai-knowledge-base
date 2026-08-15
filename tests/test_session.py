from datetime import UTC, datetime

import httpx
import pytest

from session import store
from session.main import ChatCompletion, app, chat_stream


def _complete_generation(messages, session_id, generation_id, content, kb_id, project_id, department):
    user = next(
        (message for message in messages if message["session_id"] == session_id and message["generation_id"] == generation_id and message["role"] == "user"),
        None,
    )
    if user is None:
        return False
    user["generation_complete"] = True
    messages.append(
        {
            "session_id": session_id,
            "session_token": user["session_token"],
            "role": "assistant",
            "content": content,
            "kb_id": kb_id,
            "project_id": project_id,
            "department": department,
            "generation_id": generation_id,
            "generation_complete": True,
        }
    )
    return True


@pytest.fixture
def client(monkeypatch):
    messages = []

    def begin_generation(session_id, session_token, content, generation_id, kb_id="company", project_id="default", department="general", history_limit=12):
        history = [
            {"role": m["role"], "content": m["content"], "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
            for m in messages
            if m["session_id"] == session_id
            and m["session_token"] == session_token
            and m.get("generation_complete", False)
            and (m["kb_id"], m["project_id"], m["department"]) == (kb_id, project_id, department)
        ][-history_limit:]
        messages.append(
            {
                "session_id": session_id,
                "session_token": session_token,
                "role": "user",
                "content": content,
                "kb_id": kb_id,
                "project_id": project_id,
                "department": department,
                "generation_id": generation_id,
                "generation_complete": False,
            }
        )
        return history

    monkeypatch.setattr(store, "begin_generation", begin_generation)
    monkeypatch.setattr(
        store,
        "list_messages",
        lambda session_id, limit=None, kb_id=None, project_id=None, department=None, session_token=None: [
            {"role": m["role"], "content": m["content"], "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
            for m in messages
            if m["session_id"] == session_id
            and m.get("generation_complete", False)
            and (session_token is None or m["session_token"] == session_token)
            and (kb_id is None or (m["kb_id"], m["project_id"], m["department"]) == (kb_id, project_id, department))
        ][-limit if limit else None :],
    )
    monkeypatch.setattr(
        store,
        "clear_session",
        lambda session_id, kb_id=None, project_id=None, department=None, session_token=None: sum(
            m["session_id"] == session_id
            and (kb_id is None or (m["kb_id"], m["project_id"], m["department"]) == (kb_id, project_id, department))
            and (session_token is None or m["session_token"] == session_token)
            for m in messages
        ),
    )
    monkeypatch.setattr(
        store,
        "add_assistant_if_generation_active",
        lambda session_id, generation_id, content, kb_id, project_id, department: _complete_generation(
            messages, session_id, generation_id, content, kb_id, project_id, department
        ),
    )
    monkeypatch.setattr(
        store,
        "rollback_generation",
        lambda session_id, generation_id: messages.__setitem__(
            slice(None),
            [message for message in messages if not (message["session_id"] == session_id and message["generation_id"] == generation_id)],
        ),
    )

    async def default_ask_stream(**kwargs):
        yield {"type": "delta", "content": "请先保存配置。"}
        yield {"type": "done"}

    monkeypatch.setattr("session.main.ask_stream", default_ask_stream)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _payload(**overrides):
    base = {
        "session_id": "s-1",
        "session_token": "t" * 32,
        "question": "继续",
        "kb_id": "company",
        "project_id": "p-1",
        "department": "engineering",
    }
    base.update(overrides)
    return base


@pytest.mark.anyio
async def test_chat_stream_persists_assistant_and_forwards_done(monkeypatch):
    async def fake_ask_stream(**kwargs):
        yield {"type": "sources", "sources": [{"id": "chunk-1", "filename": "guide.md", "chunk_index": 0, "score": 0.9, "excerpt": "重启服务前先保存配置。"}]}
        yield {"type": "delta", "content": "真实回答"}
        yield {"type": "done"}

    monkeypatch.setattr("session.main.ask_stream", fake_ask_stream)
    stored = []
    rolled_back = []
    monkeypatch.setattr(store, "add_assistant_if_generation_active", lambda *args: stored.append(args) or True)
    monkeypatch.setattr(store, "rollback_generation", lambda *args: rolled_back.append(args))
    payload = ChatCompletion(session_id="responses-api", session_token="t" * 32, question="继续", kb_id="company", project_id="p-1", department="engineering")

    events = [event async for event in chat_stream(payload, [], "generation-1")]

    assert any('"type": "done"' in event for event in events)
    assert not any('"type": "error"' in event for event in events)
    assert stored[0][2] == "真实回答"
    assert rolled_back == []


@pytest.mark.anyio
async def test_chat_stream_rolls_back_on_upstream_error(monkeypatch):
    async def fake_ask_stream(**kwargs):
        yield {"type": "error", "message": "upstream failed"}

    monkeypatch.setattr("session.main.ask_stream", fake_ask_stream)
    stored = []
    rolled_back = []
    monkeypatch.setattr(store, "add_assistant_if_generation_active", lambda *args: stored.append(args) or True)
    monkeypatch.setattr(store, "rollback_generation", lambda session_id, generation_id: rolled_back.append((session_id, generation_id)))
    payload = ChatCompletion(session_id="s-1", session_token="t" * 32, question="继续", kb_id="company", project_id="p-1", department="engineering")

    events = [event async for event in chat_stream(payload, [], "generation-1")]

    assert '"message": "upstream failed"' in events[-1]
    assert stored == []
    assert rolled_back == [("s-1", "generation-1")]


@pytest.mark.anyio
async def test_chat_stream_does_not_finish_when_session_cleared(monkeypatch):
    async def fake_ask_stream(**kwargs):
        yield {"type": "delta", "content": "回答"}
        yield {"type": "done"}

    monkeypatch.setattr("session.main.ask_stream", fake_ask_stream)
    monkeypatch.setattr(store, "add_assistant_if_generation_active", lambda *_: False)
    rolled_back = []
    monkeypatch.setattr(store, "rollback_generation", lambda session_id, generation_id: rolled_back.append((session_id, generation_id)))
    payload = ChatCompletion(session_id="cleared-session", session_token="t" * 32, question="继续", kb_id="company", project_id="p-1", department="engineering")

    events = [event async for event in chat_stream(payload, [], "generation-1")]

    assert '"type": "error"' in events[-1]
    assert not any('"type": "done"' in event for event in events)
    assert rolled_back == [("cleared-session", "generation-1")]


@pytest.mark.anyio
async def test_chat_stream_rolls_back_when_client_disconnects(monkeypatch):
    async def fake_ask_stream(**kwargs):
        yield {"type": "sources", "sources": []}
        yield {"type": "delta", "content": "部分回答"}
        yield {"type": "done"}

    monkeypatch.setattr("session.main.ask_stream", fake_ask_stream)
    rolled_back = []
    monkeypatch.setattr(store, "rollback_generation", lambda session_id, generation_id: rolled_back.append((session_id, generation_id)))
    payload = ChatCompletion(session_id="aborted-session", session_token="t" * 32, question="继续", kb_id="company", project_id="p-1", department="engineering")
    stream = chat_stream(payload, [], "generation-1")

    _ = await anext(stream)
    _ = await anext(stream)
    await stream.aclose()

    assert rolled_back == [("aborted-session", "generation-1")]


@pytest.mark.anyio
async def test_taskbook_chat_history_and_clear(client):
    async with client as http:
        response = await http.post("/api/v1/chat/completions", json=_payload(question="如何重启？"))
        scope = {"kb_id": "company", "project_id": "p-1", "department": "engineering"}
        headers = {"x-session-token": "t" * 32}
        history = await http.get("/api/v1/chat/history/s-1", params=scope, headers=headers)
        cleared = await http.delete("/api/v1/chat/session/s-1", params=scope, headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type": "delta"' in response.text
    assert '"type": "done"' in response.text
    assert history.status_code == 200
    assert [message["role"] for message in history.json()["messages"]] == ["user", "assistant"]
    assert cleared.json()["deleted"] == 2


@pytest.mark.anyio
async def test_failed_stream_is_excluded_from_history_and_next_prompt(client, monkeypatch):
    async def failed_stream(*_):
        yield 'data: {"type":"error","message":"upstream failed"}\n\n'

    monkeypatch.setattr("session.main.chat_stream", failed_stream)
    token = "t" * 32
    payload = _payload(session_id="failed-stream", question="失败的问题")
    scope = {"kb_id": "company", "project_id": "p-1", "department": "engineering"}
    captured_history = []

    async with client as http:
        failed = await http.post("/api/v1/chat/completions", json=payload)
        history = await http.get("/api/v1/chat/history/failed-stream", params=scope, headers={"x-session-token": token})

        async def capture_stream(_, prompt_history, __):
            captured_history.extend(prompt_history)
            yield 'data: {"type":"done"}\n\n'

        monkeypatch.setattr("session.main.chat_stream", capture_stream)
        retried = await http.post("/api/v1/chat/completions", json={**payload, "question": "下一轮问题"})

    assert '"type":"error"' in failed.text
    assert history.json()["messages"] == []
    assert retried.status_code == 200
    assert captured_history == []


@pytest.mark.anyio
async def test_chat_history_only_passes_model_message_fields(client, monkeypatch):
    captured_history = []
    monkeypatch.setattr(
        store,
        "begin_generation",
        lambda *_: [{"role": "assistant", "content": "上一轮回答", "created_at": datetime(2026, 1, 1, tzinfo=UTC)}],
    )

    async def capture_stream(_, prompt_history, __):
        captured_history.extend(prompt_history)
        yield 'data: {"type":"done"}\n\n'

    monkeypatch.setattr("session.main.chat_stream", capture_stream)

    async with client as http:
        response = await http.post("/api/v1/chat/completions", json=_payload(session_id="history-session"))

    assert response.status_code == 200
    assert captured_history == [{"role": "assistant", "content": "上一轮回答"}]


@pytest.mark.anyio
async def test_chat_rejects_a_second_active_generation(client, monkeypatch):
    monkeypatch.setattr(store, "begin_generation", lambda *_: None)

    async with client as http:
        response = await http.post("/api/v1/chat/completions", json=_payload(session_id="busy-session", question="并发问题"))

    assert response.status_code == 409
    assert "已有生成请求" in response.json()["detail"]


@pytest.mark.anyio
async def test_chat_history_and_clear_are_scope_bound(client):
    async with client as http:
        _ = await http.post("/api/v1/chat/completions", json=_payload(session_id="scoped", question="如何重启？"))
        headers = {"x-session-token": "t" * 32}
        wrong = await http.get("/api/v1/chat/history/scoped", params={"kb_id": "company", "project_id": "p-1", "department": "hr"}, headers=headers)
        correct = await http.get("/api/v1/chat/history/scoped", params={"kb_id": "company", "project_id": "p-1", "department": "engineering"}, headers=headers)
        wrong_clear = await http.delete("/api/v1/chat/session/scoped", params={"kb_id": "company", "project_id": "p-1", "department": "hr"}, headers=headers)

    assert wrong.json()["messages"] == []
    assert correct.json()["messages"][0]["role"] == "user"
    assert wrong_clear.json()["deleted"] == 0


@pytest.mark.anyio
async def test_rag_error_rolls_back_pending_user_message(client, monkeypatch):
    async def failing_ask_stream(**kwargs):
        raise httpx.ConnectError("RAG unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr("session.main.ask_stream", failing_ask_stream)
    token = "t" * 32
    payload = _payload(session_id="failed-rag", question="这条消息不应被保留")
    scope = {"kb_id": "company", "project_id": "p-1", "department": "engineering"}

    async with client as http:
        response = await http.post("/api/v1/chat/completions", json=payload)
        history = await http.get("/api/v1/chat/history/failed-rag", params=scope, headers={"x-session-token": token})

    assert '"type": "error"' in response.text
    assert history.json()["messages"] == []
