import json
from datetime import UTC, datetime

import httpx
import pytest

from session import store, summarizer
from session.main import ChatCompletion, app, chat_stream
from session.rag_client import ask_stream as rag_ask_stream
from session.summarizer import update_summary


@pytest.mark.anyio
async def test_rag_stream_forwards_opaque_scope_as_header(monkeypatch):
    request = None

    async def handler(next_request):
        nonlocal request
        request = next_request
        return httpx.Response(200, text='data: {"type":"done"}\n\n')

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient
    monkeypatch.setattr("session.rag_client.httpx.AsyncClient", lambda **kwargs: async_client(transport=transport))

    events = [
        event
        async for event in rag_ask_stream(
            question="问题",
            history=[],
            kb_id="company",
            project_id="p-1",
            department="engineering,general",
            top_k=5,
        )
    ]

    assert events == [{"type": "done"}]
    assert request is not None
    assert request.headers["x-scope-context"] == "engineering,general"
    assert "department" not in json.loads(request.content)


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

    def begin_generation(session_id, session_token, content, generation_id, kb_id="company", project_id="default", department="general"):
        history = [
            {"role": m["role"], "content": m["content"], "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
            for m in messages
            if m["session_id"] == session_id
            and m["session_token"] == session_token
            and m.get("generation_complete", False)
            and (m["kb_id"], m["project_id"], m["department"]) == (kb_id, project_id, department)
        ]
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
        return {"summary": "", "verbatim": history, "should_compact": False}

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
    monkeypatch.setattr(
        "session.main.resolve_scope",
        lambda kb_id, project_id, department: {"kb_id": kb_id, "project_id": project_id, "department": department},
    )
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

    events = [event async for event in chat_stream(payload, "", [], "generation-1")]

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

    events = [event async for event in chat_stream(payload, "", [], "generation-1")]

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

    events = [event async for event in chat_stream(payload, "", [], "generation-1")]

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
    stream = chat_stream(payload, "", [], "generation-1")

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

        async def capture_stream(_, __, prompt_history, ___):
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
        lambda *_: {
            "summary": "",
            "verbatim": [{"role": "assistant", "content": "上一轮回答", "created_at": datetime(2026, 1, 1, tzinfo=UTC)}],
            "should_compact": False,
        },
    )

    async def capture_stream(_, __, prompt_history, ___):
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


@pytest.mark.anyio
async def test_chat_completions_persists_canonical_project_scope(client, monkeypatch):
    def resolve(kb_id, project_id, department):
        return {"kb_id": kb_id, "project_id": "p-1" if project_id == "default" else project_id, "department": department}

    monkeypatch.setattr("session.main.resolve_scope", resolve)

    async with client as http:
        response = await http.post("/api/v1/chat/completions", json=_payload(session_id="canonical", project_id="default"))
        history = await http.get(
            "/api/v1/chat/history/canonical",
            params={"kb_id": "company", "project_id": "p-1", "department": "engineering"},
            headers={"x-session-token": "t" * 32},
        )

    assert response.status_code == 200
    assert [message["role"] for message in history.json()["messages"]] == ["user", "assistant"]


@pytest.mark.anyio
async def test_session_chat_health(client):
    async with client as http:
        response = await http.get("/api/v1/chat/health")

    assert response.status_code == 200
    assert response.json()["service"] == "enterprise-ai-knowledge-base-session"


@pytest.mark.anyio
async def test_chat_completions_compacts_keeping_recent_window(client, monkeypatch):
    monkeypatch.setattr(
        store,
        "begin_generation",
        lambda *_: {
            "summary": "旧摘要",
            "verbatim": [
                {"id": 10, "role": "user", "content": "更早问题"},
                {"id": 11, "role": "assistant", "content": "更早回答"},
                {"id": 12, "role": "user", "content": "最近问题"},
            ],
            "should_compact": True,
        },
    )
    monkeypatch.setattr(
        store,
        "split_for_compaction",
        lambda verbatim: (verbatim[:2], verbatim[2:]),
    )
    captured = {}
    monkeypatch.setattr(
        "session.main.update_summary",
        lambda prev, msgs: captured.update(prev=prev, msgs=msgs) or "新摘要",
    )
    saved = []
    monkeypatch.setattr(store, "save_summary", lambda *args: saved.append(args))
    forwarded = {}

    async def capture_ask_stream(**kwargs):
        forwarded.update(summary=kwargs["summary"], history=kwargs["history"])
        yield {"type": "done"}

    monkeypatch.setattr("session.main.ask_stream", capture_ask_stream)

    async with client as http:
        response = await http.post("/api/v1/chat/completions", json=_payload(session_id="long-conv"))

    assert response.status_code == 200
    assert captured["prev"] == "旧摘要"
    assert [message["content"] for message in captured["msgs"]] == ["更早问题", "更早回答"]
    assert saved[0][5] == "新摘要"
    assert saved[0][6] == 12
    assert forwarded["summary"] == "新摘要"
    assert [message["content"] for message in forwarded["history"]] == ["最近问题"]


def test_split_for_compaction_keeps_recent_and_snaps_to_turn(monkeypatch):
    monkeypatch.setattr("session.store.count_tokens", lambda text: 1)
    monkeypatch.setattr("session.store.KEEP_RECENT_TOKENS", 2)
    verbatim = [
        {"id": 1, "role": "user", "content": "u1"},
        {"id": 2, "role": "assistant", "content": "a1"},
        {"id": 3, "role": "user", "content": "u2"},
        {"id": 4, "role": "assistant", "content": "a2"},
        {"id": 5, "role": "user", "content": "u3"},
    ]

    to_summarize, kept = store.split_for_compaction(verbatim)

    assert [message["id"] for message in to_summarize] == [1, 2]
    assert [message["id"] for message in kept] == [3, 4, 5]


@pytest.mark.anyio
async def test_chat_completions_passes_verbatim_without_compression(client, monkeypatch):
    monkeypatch.setattr(
        store,
        "begin_generation",
        lambda *_: {
            "summary": "旧摘要",
            "verbatim": [{"id": 10, "role": "user", "content": "最近问题"}],
            "should_compact": False,
        },
    )
    forwarded = {}

    async def capture_ask_stream(**kwargs):
        forwarded.update(summary=kwargs["summary"], history=kwargs["history"])
        yield {"type": "done"}

    monkeypatch.setattr("session.main.ask_stream", capture_ask_stream)
    compressed = []
    monkeypatch.setattr("session.main.update_summary", lambda *args: compressed.append(args))

    async with client as http:
        response = await http.post("/api/v1/chat/completions", json=_payload(session_id="short-conv"))

    assert response.status_code == 200
    assert forwarded["summary"] == "旧摘要"
    assert [message["content"] for message in forwarded["history"]] == ["最近问题"]
    assert compressed == []


@pytest.mark.anyio
async def test_chat_completions_keeps_verbatim_when_summary_fails(client, monkeypatch):
    monkeypatch.setattr(
        store,
        "begin_generation",
        lambda *_: {
            "summary": "旧摘要",
            "verbatim": [
                {"id": 10, "role": "user", "content": "更早问题"},
                {"id": 11, "role": "assistant", "content": "更早回答"},
            ],
            "should_compact": True,
        },
    )
    monkeypatch.setattr("session.main.update_summary", lambda prev, msgs: None)
    saved = []
    monkeypatch.setattr(store, "save_summary", lambda *args: saved.append(args))
    forwarded = {}

    async def capture_ask_stream(**kwargs):
        forwarded.update(summary=kwargs["summary"], history=kwargs["history"])
        yield {"type": "done"}

    monkeypatch.setattr("session.main.ask_stream", capture_ask_stream)

    async with client as http:
        response = await http.post("/api/v1/chat/completions", json=_payload(session_id="fallback-conv"))

    assert response.status_code == 200
    assert saved == []
    assert forwarded["summary"] == "旧摘要"
    assert [message["content"] for message in forwarded["history"]] == ["更早问题", "更早回答"]


@pytest.mark.anyio
async def test_chat_completions_rolls_back_when_compaction_fails(client, monkeypatch):
    monkeypatch.setattr(
        store,
        "begin_generation",
        lambda *_: {
            "summary": "旧摘要",
            "verbatim": [{"id": 10, "role": "user", "content": "更早问题"}],
            "should_compact": True,
        },
    )
    monkeypatch.setattr(
        "session.main.update_summary",
        lambda prev, msgs: (_ for _ in ()).throw(RuntimeError("summarization unavailable")),
    )
    rolled_back = []
    monkeypatch.setattr(
        store,
        "rollback_generation",
        lambda session_id, generation_id: rolled_back.append((session_id, generation_id)),
    )

    async with client as http:
        with pytest.raises(RuntimeError, match="summarization unavailable"):
            await http.post("/api/v1/chat/completions", json=_payload(session_id="fail-conv"))

    assert len(rolled_back) == 1
    assert rolled_back[0][0] == "fail-conv"


def test_update_summary_returns_none_when_no_llm(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    assert update_summary("旧摘要", [{"role": "user", "content": "新问题"}]) is None


def test_update_summary_uses_configured_llm(monkeypatch):
    real_client = httpx.Client

    def handler(request):
        assert request.url == "http://llm.test/v1/responses"
        body = json.loads(request.read())
        assert "现有摘要" in body["input"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "更新后的摘要"}]}],
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setattr(summarizer.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))

    assert update_summary("旧摘要", [{"role": "user", "content": "新问题"}]) == "更新后的摘要"
