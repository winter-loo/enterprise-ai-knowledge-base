import httpx
import pytest

from app import postgres_store
from app.main import app, split_text


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(postgres_store, "ensure_kb", lambda kb_id: {"id": kb_id} if kb_id == "company" else None)
    monkeypatch.setattr(postgres_store, "ensure_project", lambda kb_id, project_id: {"id": project_id} if (kb_id, project_id) == ("company", "p-1") else None)
    monkeypatch.setattr(postgres_store, "list_kbs", lambda: [{"id": "company", "name": "公司知识库"}])
    monkeypatch.setattr(postgres_store, "list_projects", lambda kb_id: [{"id": "p-1", "kb_id": kb_id, "name": "研发项目"}])
    monkeypatch.setattr(postgres_store, "list_documents", lambda kb_id: [{"id": "doc-1", "filename": "guide.md", "project_id": "p-1", "department": "engineering", "status": "READY", "chunk_count": 1, "source_type": "upload"}])
    monkeypatch.setattr(postgres_store, "insert_document", lambda **kw: {"id": kw["document_id"], "filename": kw["filename"], "project_id": kw["project_id"], "status": "READY", "chunk_count": len(kw["chunks"]), "parser": kw["parser"], "pdf_type": None, "pages_needing_ocr": []})
    monkeypatch.setattr(postgres_store, "search", lambda question, kb_id, project_id, department, top_k: [{"id": "chunk-1", "filename": "guide.md", "chunk_index": 0, "content": "重启服务前先保存配置。", "score": 0.9}])
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
        response = await http.post("/api/documents/upload", files={"file": ("guide.md", b"x", "text/markdown")}, data={"kb_id": "company", "project_id": "p-1", "department": "engineering"})
    assert response.status_code == 200
    assert response.json()["project_id"] == "p-1"


@pytest.mark.anyio
async def test_project_scope_is_required(client):
    async with client as http:
        response = await http.post("/api/documents/upload", files={"file": ("guide.md", b"x", "text/markdown")}, data={"kb_id": "company", "project_id": "other"})
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
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, query, params):
            assert "e.kb_id=%s AND e.project_id=%s" in query
            assert "e.department=%s OR e.department='general'" in query
            assert params[1:] == ("年假审批需要哪些材料？", "company", "p-1", "hr", 5)
            return self
        def fetchall(self): return []

    monkeypatch.setattr(postgres_store, "embed", lambda texts: embedded.extend(texts) or [[0.0] * 1024])
    monkeypatch.setattr(postgres_store, "connect", Connection)
    postgres_store.search("年假审批需要哪些材料？", "company", "p-1", "hr", 5)
    assert embedded == [f"Instruct: {postgres_store.QUERY_INSTRUCTION}\nQuery: 年假审批需要哪些材料？"]
