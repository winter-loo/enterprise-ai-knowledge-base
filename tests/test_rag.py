import json

import httpx
import pytest
from psycopg._queries import _split_query

from rag import store
from rag.chunking import _token_count
from rag.main import AskRequest, app, ask_stream, llm_answer, split_text, stream_content
from rag.openai_responses import response_answer_text

SOURCES = [{"id": "chunk-1", "filename": "guide.md", "chunk_index": 0, "score": 0.9, "content": "重启服务前先保存配置。", "summary": "重启前保存配置"}]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(store, "ensure_kb", lambda kb_id: {"id": kb_id} if kb_id == "company" else None)
    monkeypatch.setattr(store, "ensure_project", lambda kb_id, project_id: {"id": project_id} if (kb_id, project_id) == ("company", "p-1") else None)
    monkeypatch.setattr(store, "list_kbs", lambda: [{"id": "company", "name": "公司知识库"}])
    monkeypatch.setattr(store, "list_projects", lambda kb_id: [{"id": "p-1", "kb_id": kb_id, "name": "研发项目"}])
    monkeypatch.setattr(
        store,
        "create_project",
        lambda kb_id, name, description: {"id": "p-2", "kb_id": kb_id, "name": name, "description": description},
    )
    monkeypatch.setattr(
        store,
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
        store,
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
    monkeypatch.setattr(store, "search", lambda question, kb_id, project_id, department, top_k: SOURCES)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_split_text_overlaps_chunks():
    chunks = split_text("知识库" * 400)  # 800 token > 默认 size 700, 会切成两片
    assert len(chunks) == 2
    assert all(_token_count(chunk) <= 700 for chunk in chunks)


@pytest.mark.anyio
async def test_scope_lists_and_upload(client, monkeypatch):
    monkeypatch.setattr("rag.main.parse_document", lambda *_: ("部署步骤。" * 300, "plain-text", None, []))
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
    monkeypatch.setattr("rag.main.UPLOAD_DIR", tmp_path)
    monkeypatch.setattr("rag.main.parse_document", lambda *_: ("部署步骤。", "plain-text", None, []))
    monkeypatch.setattr(store, "insert_document", lambda **_: (_ for _ in ()).throw(RuntimeError("embedding unavailable")))

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
async def test_retrieve_returns_full_chunks(client):
    async with client as http:
        response = await http.post("/api/retrieve", json={"question": "如何重启？", "kb_id": "company", "project_id": "p-1", "department": "engineering"})
    body = response.json()
    assert response.status_code == 200
    assert body["retrieved"] == 1
    assert body["chunks"][0]["content"] == "重启服务前先保存配置。"
    assert body["chunks"][0]["summary"] == "重启前保存配置"


@pytest.mark.anyio
async def test_document_import(client):
    async with client as http:
        imported = await http.post(
            "/api/v1/document/import", json={"title": "guide.md", "content": "部署步骤。", "kb_id": "company", "project_id": "p-1", "department": "engineering"}
        )
    assert imported.status_code == 200
    assert imported.json()["status"] == "READY"


@pytest.mark.anyio
async def test_scope_resolve_returns_canonical_project(client):
    async with client as http:
        response = await http.post("/api/scope/resolve", json={"kb_id": "company", "project_id": "p-1", "department": "engineering"})
    assert response.status_code == 200
    assert response.json() == {"kb_id": "company", "project_id": "p-1", "department": "engineering"}


@pytest.mark.anyio
async def test_scope_resolve_rejects_unknown_project(client):
    async with client as http:
        response = await http.post("/api/scope/resolve", json={"kb_id": "company", "project_id": "other", "department": "engineering"})
    assert response.status_code == 404


@pytest.mark.anyio
async def test_ask_keeps_scope_and_citations(client, monkeypatch):
    async def answer(*_):
        return "请先保存配置。[1]", "test-model"

    monkeypatch.setattr("rag.main.llm_answer", answer)
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
            _ = _split_query(query.encode("utf-8"))
            assert "e.kb_id=%s AND e.project_id=%s" in query
            assert "e.department=%s OR e.department='general'" in query
            assert params[1:] == ("年假审批需要哪些材料？", "company", "p-1", "hr", 5)
            return self

        def fetchall(self):
            return []

    monkeypatch.setattr(store, "embed", lambda texts: embedded.extend(texts) or [[0.0] * 1024])
    monkeypatch.setattr(store, "connect", Connection)
    store.search("年假审批需要哪些材料？", "company", "p-1", "hr", 5)
    assert embedded == [f"Instruct: {store.QUERY_INSTRUCTION}\nQuery: 年假审批需要哪些材料？"]


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

    monkeypatch.setattr(store, "embed", lambda chunks: [[0.0] * 1024 for _ in chunks])
    monkeypatch.setattr(store, "summarize_chunks", lambda chunks: ["部署前保存配置。" for _ in chunks])
    monkeypatch.setattr(store, "connect", Connection)

    store.insert_document(
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
        assert request.url == "http://llm.test/v1/responses"
        assert request.headers["authorization"] == "Bearer secret"
        request_body = json.loads(request.read())
        assert request_body["model"] == "summary-model"
        assert "摘要必须保持原文语言，不要翻译" in request_body["instructions"]
        assert request_body["input"] == [{"role": "user", "content": "部署服务前，需要先保存当前配置。"}]
        assert request_body["max_output_tokens"] == 160
        assert request_body["store"] is False
        assert "messages" not in request_body
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "  部署前保存配置。  "}]}],
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_MODEL", "summary-model")
    monkeypatch.setattr(store.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))

    assert store.summarize_chunks(["部署服务前，需要先保存当前配置。"]) == ["部署前保存配置。"]


def test_summarize_chunks_degrades_per_failed_chunk(monkeypatch):
    real_client = httpx.Client
    responses = iter(
        [
            httpx.Response(200, json={"status": "completed", "output": [{"content": [{"type": "output_text", "text": "摘要一"}]}]}),
            httpx.Response(503),
            httpx.Response(200, json={"status": "completed", "output": [{"content": [{"type": "output_text", "text": "摘要三"}]}]}),
        ]
    )
    transport = httpx.MockTransport(lambda _request: next(responses))
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setattr(store.httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs))

    assert store.summarize_chunks(["片段一", "失败片段", "片段三"]) == ["摘要一", "", "摘要三"]


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
    monkeypatch.setattr(store.httpx, "Client", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("client unavailable")))
    monkeypatch.setattr(store, "embed", lambda chunks: [[0.0] * 1024 for _ in chunks])
    monkeypatch.setattr(store, "connect", Connection)

    store.insert_document(
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


def test_stream_content_accepts_responses_api_text_and_refusal_deltas():
    assert stream_content({"type": "response.output_text.delta", "delta": "响应"}) == "响应"
    assert stream_content({"type": "response.refusal.delta", "delta": "无法回答"}) == "无法回答"


def test_response_answer_text_preserves_refusals():
    payload = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "无法安全回答。"}]}],
    }

    assert response_answer_text(payload) == "无法安全回答。"


@pytest.mark.anyio
async def test_llm_answer_uses_responses_api(monkeypatch):
    real_client = httpx.AsyncClient

    def handler(request):
        assert request.url == "http://llm.test/v1/responses"
        request_body = json.loads(request.read())
        assert request_body["instructions"].startswith("你是企业知识库助手")
        assert request_body["input"][-1] == {"role": "user", "content": "参考资料：\n无\n\n问题：当前有哪些资料？"}
        assert request_body["store"] is False
        assert "messages" not in request_body
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "当前没有资料。"}]}],
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_MODEL", "responses-model")
    monkeypatch.setattr("rag.main.httpx.AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))

    answer, mode = await llm_answer("当前有哪些资料？", [], [])

    assert answer == "当前没有资料。"
    assert mode == "responses-model"


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
async def test_ask_stream_accepts_responses_api_completion_event(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    lines = [
        'data: {"type":"response.output_text.delta","delta":"真实回答"}',
        'data: {"type":"response.completed"}',
    ]

    class CaptureClient(_FakeAsyncClient):
        def stream(self, method, url, **kwargs):
            assert method == "POST"
            assert url == "http://llm.test/v1/responses"
            request_body = kwargs["json"]
            assert request_body["input"][-1]["role"] == "user"
            assert request_body["store"] is False
            assert "messages" not in request_body
            return super().stream(method, url, **kwargs)

    monkeypatch.setattr("rag.main.httpx.AsyncClient", lambda **kwargs: CaptureClient(lines, **kwargs))
    payload = AskRequest(question="继续", kb_id="company", project_id="p-1", department="engineering")

    events = [event async for event in ask_stream(payload, SOURCES)]

    assert any('"type":"done"' in event for event in events)
    assert not any('"type": "error"' in event for event in events)
    assert any('"真实回答"' in event for event in events)


@pytest.mark.anyio
async def test_ask_stream_emits_sources_with_ids_and_excerpts(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    payload = AskRequest(question="如何重启？", kb_id="company", project_id="p-1", department="engineering")

    events = [event async for event in ask_stream(payload, SOURCES)]

    first = json.loads(events[0][6:])
    assert first["type"] == "sources"
    assert first["sources"][0]["id"] == "chunk-1"
    assert first["sources"][0]["excerpt"] == "重启服务前先保存配置。"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (['data: {"type":"response.output_text.delta","delta":"partial"}'], "LLM stream ended before its completion marker"),
        (
            ['data: {"type":"response.output_text.delta","delta":"partial"}', "data: [DONE]"],
            "LLM Responses stream returned an unexpected [DONE] marker",
        ),
        (["data: {bad-json", 'data: {"type":"response.completed"}'], "LLM stream returned malformed JSON"),
        (["data: null", 'data: {"type":"response.completed"}'], "LLM stream returned an invalid event shape"),
        (
            [
                'data: {"type":"response.output_text.delta","delta":"partial"}',
                'data: {"type":"error","message":"quota exceeded"}',
                'data: {"type":"response.completed"}',
            ],
            "LLM stream error: quota exceeded",
        ),
    ],
)
async def test_ask_stream_rejects_truncated_or_malformed_upstream(monkeypatch, lines, message):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setattr("rag.main.httpx.AsyncClient", lambda **kwargs: _FakeAsyncClient(lines, **kwargs))
    payload = AskRequest(question="继续", kb_id="company", project_id="p-1", department="engineering")

    events = [event async for event in ask_stream(payload, SOURCES)]

    assert f'"message": "{message}"' in events[-1]
