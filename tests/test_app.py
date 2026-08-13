import httpx
import pytest

from app import postgres_store
from app.main import app, split_text, stream_content


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(postgres_store, "ensure_kb", lambda kb_id: {"id": kb_id} if kb_id == "company" else None)
    monkeypatch.setattr(postgres_store, "ensure_project", lambda kb_id, project_id: {"id": project_id} if (kb_id, project_id) == ("company", "p-1") else None)
    monkeypatch.setattr(postgres_store, "list_kbs", lambda: [{"id": "company", "name": "公司知识库"}])
    monkeypatch.setattr(postgres_store, "list_projects", lambda kb_id: [{"id": "p-1", "kb_id": kb_id, "name": "研发项目"}])
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
    monkeypatch.setattr(
        postgres_store, "add_message", lambda session_id, role, content: messages.append({"session_id": session_id, "role": role, "content": content})
    )
    monkeypatch.setattr(
        postgres_store, "list_messages", lambda session_id, limit=None: [m for m in messages if m["session_id"] == session_id][-limit if limit else None :]
    )
    monkeypatch.setattr(postgres_store, "clear_session", lambda session_id: sum(m["session_id"] == session_id for m in messages))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_split_text_overlaps_chunks():
    chunks = split_text("a" * 1000)
    assert len(chunks) == 2
    assert chunks[0][-100:] == chunks[1][:100]


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
            json={"session_id": "s-1", "question": "如何重启？", "kb_id": "company", "project_id": "p-1", "department": "engineering"},
        )
        history = await http.get("/api/v1/chat/history/s-1")
        cleared = await http.delete("/api/v1/chat/session/s-1")
    assert imported.status_code == 200
    assert imported.json()["status"] == "READY"
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type":"delta"' in response.text
    assert history.status_code == 200
    assert cleared.json()["deleted"] == 1
