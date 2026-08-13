from datetime import UTC, datetime

import httpx
import pytest

from app import postgres_store
from app.main import ChatCompletion, app, chat_stream, split_text, stream_content


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
    monkeypatch.setattr(postgres_store, "ensure_kb", lambda kb_id: {"id": kb_id} if kb_id == "company" else None)
    monkeypatch.setattr(postgres_store, "ensure_project", lambda kb_id, project_id: {"id": project_id} if (kb_id, project_id) == ("company", "p-1") else None)
    monkeypatch.setattr(postgres_store, "list_kbs", lambda: [{"id": "company", "name": "公司知识库"}])
    monkeypatch.setattr(postgres_store, "list_projects", lambda kb_id: [{"id": "p-1", "kb_id": kb_id, "name": "研发项目"}])
    monkeypatch.setattr(
        postgres_store,
        "create_project",
        lambda kb_id, name, description: {"id": "p-2", "kb_id": kb_id, "name": name, "description": description},
    )
    monkeypatch.setattr(
        postgres_store,
        "list_documents",
        lambda kb_id: [
            {
                "id": "doc-1",
                "filename": "guide.md",
                "project_id": "p-1",
                "department": "engineering",
                "status": "READY",
                "chunk_count": 1,
                "source_type": "upload",
            }
        ],
    )
    monkeypatch.setattr(
        postgres_store,
        "insert_document",
        lambda **kw: {
            "id": kw["document_id"],
            "filename": kw["filename"],
            "project_id": kw["project_id"],
            "status": "READY",
            "chunk_count": len(kw["chunks"]),
            "chunking_strategy": kw.get("chunking_strategy", "recursive"),
            "parser": kw["parser"],
            "pdf_type": None,
            "pages_needing_ocr": [],
        },
    )
    monkeypatch.setattr(
        postgres_store,
        "search",
        lambda question, kb_id, project_id, department, top_k: [
            {"id": "chunk-1", "filename": "guide.md", "chunk_index": 0, "content": "重启服务前先保存配置。", "score": 0.9}
        ],
    )
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

    monkeypatch.setattr(
        postgres_store,
        "begin_generation",
        begin_generation,
    )
    monkeypatch.setattr(
        postgres_store,
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
        postgres_store,
        "clear_session",
        lambda session_id, kb_id=None, project_id=None, department=None, session_token=None: sum(
            m["session_id"] == session_id
            and (kb_id is None or (m["kb_id"], m["project_id"], m["department"]) == (kb_id, project_id, department))
            and (session_token is None or m["session_token"] == session_token)
            for m in messages
        ),
    )
    monkeypatch.setattr(
        postgres_store,
        "add_assistant_if_generation_active",
        lambda session_id, generation_id, content, kb_id, project_id, department: _complete_generation(
            messages, session_id, generation_id, content, kb_id, project_id, department
        ),
    )
    monkeypatch.setattr(
        postgres_store,
        "rollback_generation",
        lambda session_id, generation_id: messages.__setitem__(
            slice(None),
            [message for message in messages if not (message["session_id"] == session_id and message["generation_id"] == generation_id)],
        ),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_split_text_overlaps_chunks():
    chunks = split_text("a" * 1000)
    assert len(chunks) == 2
    assert chunks[0][-100:] == chunks[1][:100]


@pytest.mark.anyio
async def test_web_app_serves_build_assets_and_spa_fallback(client, monkeypatch, tmp_path):
    build_dir = tmp_path / "build"
    asset_dir = build_dir / "_app" / "immutable"
    asset_dir.mkdir(parents=True)
    (build_dir / "index.html").write_text("<!doctype html><title>Knowledge Base</title>", encoding="utf-8")
    (asset_dir / "app.js").write_text("console.log('ready')", encoding="utf-8")
    monkeypatch.setattr("app.main.WEB_BUILD_DIR", build_dir)

    async with client as http:
        index = await http.get("/")
        nested_route = await http.get("/chat/session-1")
        asset = await http.get("/_app/immutable/app.js")
        unknown_api = await http.get("/api/not-a-route")

    assert index.status_code == 200
    assert "Knowledge Base" in index.text
    assert nested_route.text == index.text
    assert asset.text == "console.log('ready')"
    assert asset.headers["content-type"].startswith("text/javascript")
    assert unknown_api.status_code == 404


@pytest.mark.anyio
async def test_web_app_reports_missing_production_build(client, monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.WEB_BUILD_DIR", tmp_path / "missing")

    async with client as http:
        response = await http.get("/")

    assert response.status_code == 503
    assert "make build" in response.json()["detail"]


@pytest.mark.anyio
async def test_scope_lists_and_upload(client, monkeypatch):
    monkeypatch.setattr("app.main.parse_document", lambda *_: ("部署步骤。" * 300, "plain-text", None, []))
    async with client as http:
        assert (await http.get("/api/knowledge-bases")).json()[0]["id"] == "company"
        assert (await http.get("/api/projects?kb_id=company")).json()[0]["id"] == "p-1"
        assert (await http.get("/api/documents?kb_id=company")).json()[0]["project_id"] == "p-1"
        response = await http.post(
            "/api/documents/upload",
            files={"file": ("guide.md", b"x", "text/markdown")},
            data={"kb_id": "company", "project_id": "p-1", "department": "engineering", "chunking_strategy": "fixed"},
        )
    assert response.status_code == 200
    assert response.json()["project_id"] == "p-1"
    assert response.json()["chunking_strategy"] == "fixed"


@pytest.mark.anyio
async def test_failed_upload_removes_stored_source(client, monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.UPLOAD_DIR", tmp_path)
    monkeypatch.setattr("app.main.parse_document", lambda *_: ("部署步骤。", "plain-text", None, []))
    monkeypatch.setattr(postgres_store, "insert_document", lambda **_: (_ for _ in ()).throw(RuntimeError("embedding unavailable")))

    async with client as http:
        with pytest.raises(RuntimeError, match="embedding unavailable"):
            _ = await http.post(
                "/api/documents/upload",
                files={"file": ("confidential.md", b"secret", "text/markdown")},
                data={"kb_id": "company", "project_id": "p-1"},
            )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.anyio
async def test_project_creation_is_validated(client):
    async with client as http:
        created = await http.post("/api/projects", json={"kb_id": "company", "name": "上线项目", "description": "生产发布资料"})
        invalid = await http.post("/api/projects", json={"kb_id": "company", "name": ""})

    assert created.status_code == 200
    assert created.json()["name"] == "上线项目"
    assert invalid.status_code == 422


@pytest.mark.anyio
async def test_project_scope_is_required(client):
    async with client as http:
        response = await http.post(
            "/api/documents/upload", files={"file": ("guide.md", b"x", "text/markdown")}, data={"kb_id": "company", "project_id": "other"}
        )
    assert response.status_code == 404


@pytest.mark.anyio
async def test_ask_keeps_scope_and_citations(client, monkeypatch):
    monkeypatch.setattr("app.main.llm_answer", lambda *_: None)

    async def answer(*_):
        return "请先保存配置。[1]", "test-model"

    monkeypatch.setattr("app.main.llm_answer", answer)
    async with client as http:
        response = await http.post("/api/ask", json={"question": "如何重启？", "kb_id": "company", "project_id": "p-1", "department": "engineering"})
    body = response.json()
    assert response.status_code == 200
    assert body["citations"][0]["filename"] == "guide.md"
    assert body["answer_mode"] == "test-model"


def test_search_adds_qwen_instruction_and_permission_filters(monkeypatch):
    embedded = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, query, params):
            assert "e.kb_id=%s AND e.project_id=%s" in query
            assert "e.department=%s OR e.department='general'" in query
            assert params[1:] == ("年假审批需要哪些材料？", "company", "p-1", "hr", 5)
            return self

        def fetchall(self):
            return []

    monkeypatch.setattr(postgres_store, "embed", lambda texts: embedded.extend(texts) or [[0.0] * 1024])
    monkeypatch.setattr(postgres_store, "connect", Connection)
    postgres_store.search("年假审批需要哪些材料？", "company", "p-1", "hr", 5)
    assert embedded == [f"Instruct: {postgres_store.QUERY_INSTRUCTION}\nQuery: 年假审批需要哪些材料？"]


def test_insert_document_persists_llm_summaries(monkeypatch):
    inserted = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, _query, params):
            inserted.append(params)
            return self

        def commit(self):
            pass

    monkeypatch.setattr(postgres_store, "embed", lambda chunks: [[0.0] * 1024 for _ in chunks])
    monkeypatch.setattr(postgres_store, "summarize_chunks", lambda chunks: ["部署前保存配置。" for _ in chunks])
    monkeypatch.setattr(postgres_store, "connect", Connection)

    postgres_store.insert_document(
        kb_id="company",
        project_id="p-1",
        document_id="doc-1",
        filename="guide.md",
        department="engineering",
        parser="plain-text",
        pdf_type=None,
        pages_needing_ocr=[],
        chunks=["部署服务前，需要先保存当前配置。"],
        stored_path="",
    )

    assert inserted[0][6] == "部署前保存配置。"


def test_summarize_chunks_uses_configured_llm(monkeypatch):
    real_client = httpx.Client

    def handler(request):
        assert request.url == "http://llm.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer secret"
        request_body = request.read()
        assert request_body.find(b'"model":"summary-model"') >= 0
        assert "摘要必须保持原文语言，不要翻译" in request_body.decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": "  部署前保存配置。  "}}]})

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_MODEL", "summary-model")
    monkeypatch.setattr(postgres_store.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))

    assert postgres_store.summarize_chunks(["部署服务前，需要先保存当前配置。"]) == ["部署前保存配置。"]


def test_summarize_chunks_degrades_per_failed_chunk(monkeypatch):
    real_client = httpx.Client
    responses = iter(
        [
            httpx.Response(200, json={"choices": [{"message": {"content": "摘要一"}}]}),
            httpx.Response(503),
            httpx.Response(200, json={"choices": [{"message": {"content": "摘要三"}}]}),
        ]
    )
    transport = httpx.MockTransport(lambda _request: next(responses))
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setattr(postgres_store.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))

    assert postgres_store.summarize_chunks(["片段一", "失败片段", "片段三"]) == ["摘要一", "", "摘要三"]


def test_insert_document_continues_when_summary_client_fails(monkeypatch):
    inserted = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, _query, params):
            inserted.append(params)
            return self

        def commit(self):
            pass

    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setattr(postgres_store.httpx, "Client", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("client unavailable")))
    monkeypatch.setattr(postgres_store, "embed", lambda chunks: [[0.0] * 1024 for _ in chunks])
    monkeypatch.setattr(postgres_store, "connect", Connection)

    postgres_store.insert_document(
        kb_id="company",
        project_id="p-1",
        document_id="doc-1",
        filename="guide.md",
        department="engineering",
        parser="plain-text",
        pdf_type=None,
        pages_needing_ocr=[],
        chunks=["索引必须继续。"],
        stored_path="",
    )

    assert inserted[0][6] == ""


def test_stream_content_accepts_both_openai_dialects():
    assert stream_content({"choices": [{"delta": {"content": "传统"}}]}) == "传统"
    assert stream_content({"type": "response.output_text.delta", "delta": "响应"}) == "响应"


class _FakeStreamResponse:
    def __init__(self, lines):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class _FakeAsyncClient:
    def __init__(self, lines, **_):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def stream(self, *_args, **_kwargs):
        return _FakeStreamResponse(self.lines)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (['data: {"choices":[{"delta":{"content":"partial"}}]}'], "LLM stream ended before its completion marker"),
        (["data: {bad-json", "data: [DONE]"], "LLM stream returned malformed JSON"),
        (["data: null", "data: [DONE]"], "LLM stream returned an invalid event shape"),
        (
            ['data: {"choices":[{"delta":{"content":"partial"}}]}', 'data: {"error":{"message":"quota exceeded"}}', "data: [DONE]"],
            "LLM stream error: quota exceeded",
        ),
    ],
)
async def test_chat_stream_rejects_truncated_or_malformed_upstream(monkeypatch, lines, message):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setattr("app.main.httpx.AsyncClient", lambda **kwargs: _FakeAsyncClient(lines, **kwargs))
    stored = []
    rolled_back = []
    monkeypatch.setattr(postgres_store, "add_assistant_if_generation_active", lambda *_: stored.append(True))
    monkeypatch.setattr(postgres_store, "rollback_generation", lambda session_id, generation_id: rolled_back.append((session_id, generation_id)))
    payload = ChatCompletion(session_id="s-1", session_token="t" * 32, question="继续", kb_id="company", project_id="p-1", department="engineering")

    events = [event async for event in chat_stream(payload, [], [], "generation-1")]

    assert f'"message": "{message}"' in events[-1]
    assert stored == []
    assert rolled_back == [("s-1", "generation-1")]


@pytest.mark.anyio
async def test_chat_stream_does_not_finish_when_session_was_cleared(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(postgres_store, "add_assistant_if_generation_active", lambda *_: False)
    rolled_back = []
    monkeypatch.setattr(postgres_store, "rollback_generation", lambda session_id, generation_id: rolled_back.append((session_id, generation_id)))
    payload = ChatCompletion(
        session_id="cleared-session",
        session_token="t" * 32,
        question="继续",
        kb_id="company",
        project_id="p-1",
        department="engineering",
    )

    events = [event async for event in chat_stream(payload, [], [], "generation-1")]

    assert '"type":"error"' in events[-1]
    assert not any('"type":"done"' in event for event in events)
    assert rolled_back == [("cleared-session", "generation-1")]


@pytest.mark.anyio
async def test_chat_stream_rolls_back_when_client_disconnects(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    rolled_back = []
    monkeypatch.setattr(postgres_store, "rollback_generation", lambda session_id, generation_id: rolled_back.append((session_id, generation_id)))
    payload = ChatCompletion(
        session_id="aborted-session",
        session_token="t" * 32,
        question="继续",
        kb_id="company",
        project_id="p-1",
        department="engineering",
    )
    stream = chat_stream(payload, [], [], "generation-1")

    _ = await anext(stream)
    _ = await anext(stream)
    await stream.aclose()

    assert rolled_back == [("aborted-session", "generation-1")]


@pytest.mark.anyio
async def test_failed_stream_is_excluded_from_history_and_next_prompt(client, monkeypatch):
    async def failed_stream(*_):
        yield 'data: {"type":"error","message":"upstream failed"}\n\n'

    monkeypatch.setattr("app.main.chat_stream", failed_stream)
    token = "t" * 32
    payload = {
        "session_id": "failed-stream",
        "session_token": token,
        "question": "失败的问题",
        "kb_id": "company",
        "project_id": "p-1",
        "department": "engineering",
    }
    scope = {"kb_id": "company", "project_id": "p-1", "department": "engineering"}
    captured_history = []

    async with client as http:
        failed = await http.post("/api/v1/chat/completions", json=payload)
        history = await http.get("/api/v1/chat/history/failed-stream", params=scope, headers={"x-session-token": token})

        async def capture_stream(_, __, prompt_history, ___):
            captured_history.extend(prompt_history)
            yield 'data: {"type":"done"}\n\n'

        monkeypatch.setattr("app.main.chat_stream", capture_stream)
        retried = await http.post("/api/v1/chat/completions", json={**payload, "question": "下一轮问题"})

    assert '"type":"error"' in failed.text
    assert history.json()["messages"] == []
    assert retried.status_code == 200
    assert captured_history == []


@pytest.mark.anyio
async def test_taskbook_import_history_stream_and_clear(client, monkeypatch):
    async def stream(*_):
        yield 'data: {"type":"delta","content":"请先保存配置。"}\n\n'
        yield 'data: {"type":"done"}\n\n'

    monkeypatch.setattr("app.main.chat_stream", stream)
    async with client as http:
        imported = await http.post(
            "/api/v1/document/import", json={"title": "guide.md", "content": "部署步骤。", "kb_id": "company", "project_id": "p-1", "department": "engineering"}
        )
        response = await http.post(
            "/api/v1/chat/completions",
            json={
                "session_id": "s-1",
                "session_token": "t" * 32,
                "question": "如何重启？",
                "kb_id": "company",
                "project_id": "p-1",
                "department": "engineering",
            },
        )
        scope = {"kb_id": "company", "project_id": "p-1", "department": "engineering"}
        headers = {"x-session-token": "t" * 32}
        history = await http.get("/api/v1/chat/history/s-1", params=scope, headers=headers)
        cleared = await http.delete("/api/v1/chat/session/s-1", params=scope, headers=headers)
    assert imported.status_code == 200
    assert imported.json()["status"] == "READY"
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type":"delta"' in response.text
    assert history.status_code == 200
    assert cleared.json()["deleted"] == 1


@pytest.mark.anyio
async def test_stream_sources_include_ids_and_excerpts(client, monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    async with client as http:
        response = await http.post(
            "/api/v1/chat/completions",
            json={
                "session_id": "source-session",
                "session_token": "t" * 32,
                "question": "如何重启？",
                "kb_id": "company",
                "project_id": "p-1",
                "department": "engineering",
            },
        )

    assert response.status_code == 200
    assert '"id": "chunk-1"' in response.text
    assert '"excerpt": "重启服务前先保存配置。"' in response.text
    assert '"type": "delta"' in response.text
    assert '"type":"done"' in response.text


@pytest.mark.anyio
async def test_chat_history_only_passes_model_message_fields(client, monkeypatch):
    captured_history = []
    monkeypatch.setattr(
        postgres_store,
        "begin_generation",
        lambda *_: [{"role": "assistant", "content": "上一轮回答", "created_at": datetime(2026, 1, 1, tzinfo=UTC)}],
    )

    async def capture_stream(_, __, history, ___):
        captured_history.extend(history)
        yield 'data: {"type":"done"}\n\n'

    monkeypatch.setattr("app.main.chat_stream", capture_stream)

    async with client as http:
        response = await http.post(
            "/api/v1/chat/completions",
            json={
                "session_id": "history-session",
                "session_token": "t" * 32,
                "question": "继续",
                "kb_id": "company",
                "project_id": "p-1",
                "department": "engineering",
            },
        )

    assert response.status_code == 200
    assert captured_history == [{"role": "assistant", "content": "上一轮回答"}]


@pytest.mark.anyio
async def test_chat_rejects_a_second_active_generation(client, monkeypatch):
    monkeypatch.setattr(postgres_store, "begin_generation", lambda *_: None)

    async with client as http:
        response = await http.post(
            "/api/v1/chat/completions",
            json={
                "session_id": "busy-session",
                "session_token": "t" * 32,
                "question": "并发问题",
                "kb_id": "company",
                "project_id": "p-1",
                "department": "engineering",
            },
        )

    assert response.status_code == 409
    assert "已有生成请求" in response.json()["detail"]


@pytest.mark.anyio
async def test_chat_history_and_clear_are_scope_bound(client):
    async with client as http:
        _ = await http.post(
            "/api/v1/chat/completions",
            json={
                "session_id": "scoped",
                "session_token": "t" * 32,
                "question": "如何重启？",
                "kb_id": "company",
                "project_id": "p-1",
                "department": "engineering",
            },
        )
        headers = {"x-session-token": "t" * 32}
        wrong = await http.get("/api/v1/chat/history/scoped", params={"kb_id": "company", "project_id": "p-1", "department": "hr"}, headers=headers)
        correct = await http.get("/api/v1/chat/history/scoped", params={"kb_id": "company", "project_id": "p-1", "department": "engineering"}, headers=headers)
        wrong_clear = await http.delete("/api/v1/chat/session/scoped", params={"kb_id": "company", "project_id": "p-1", "department": "hr"}, headers=headers)

    assert wrong.json()["messages"] == []
    assert correct.json()["messages"][0]["role"] == "user"
    assert wrong_clear.json()["deleted"] == 0


@pytest.mark.anyio
async def test_search_failure_rolls_back_pending_user_message(client, monkeypatch):
    monkeypatch.setattr(postgres_store, "search", lambda *_: (_ for _ in ()).throw(RuntimeError("embedding unavailable")))
    token = "t" * 32
    payload = {
        "session_id": "failed-search",
        "session_token": token,
        "question": "这条消息不应被保留",
        "kb_id": "company",
        "project_id": "p-1",
        "department": "engineering",
    }
    scope = {"kb_id": "company", "project_id": "p-1", "department": "engineering"}

    async with client as http:
        with pytest.raises(RuntimeError, match="embedding unavailable"):
            _ = await http.post("/api/v1/chat/completions", json=payload)
        history = await http.get("/api/v1/chat/history/failed-search", params=scope, headers={"x-session-token": token})

    assert history.json()["messages"] == []
