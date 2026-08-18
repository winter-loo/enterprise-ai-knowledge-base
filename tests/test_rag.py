from datetime import UTC, datetime

import httpx
import pytest

from authz import store as authz_store
from rag import store
from rag.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(store, "ensure_company_kb", lambda: {"id": "company"})
    monkeypatch.setattr(store, "ensure_project", lambda project_id: {"id": project_id} if project_id == "p-1" else None)
    monkeypatch.setattr(authz_store, "list_accessible_project_ids", lambda principal: None if principal == "admin" else ["p-1"])
    monkeypatch.setattr(
        store,
        "list_projects",
        lambda project_ids=None: [
            {
                "id": project_id,
                "name": f"Project {project_id}",
                "description": "",
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
            for project_id in (project_ids if project_ids is not None else ["p-1", "p-2"])
        ],
    )
    monkeypatch.setattr(authz_store, "has_permission", lambda principal, action, project_id=None: principal == "alice" or principal == "admin")
    monkeypatch.setattr(
        store,
        "list_documents",
        lambda project_id, _principal: [
            {
                "id": "doc-1",
                "filename": "guide.md",
                "project_id": project_id,
                "status": "READY",
                "chunk_count": 1,
                "source_type": "upload",
                "parser": "plain-text",
                "pdf_type": None,
                "chunking_strategy": "recursive",
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        ],
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
async def test_project_listing_returns_only_accessible_projects(client):
    async with client as http:
        response = await http.get("/api/projects", headers={"x-principal": "alice"})

    assert response.status_code == 200
    assert [project["id"] for project in response.json()] == ["p-1"]


@pytest.mark.anyio
async def test_project_creator_becomes_manager(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(store, "create_project", lambda name, _description: {"id": "p-new", "name": name})
    monkeypatch.setattr(authz_store, "upsert_grant", lambda principal, project, role: captured.update(principal=principal, project=project, role=role) or {})

    async with client as http:
        response = await http.post("/api/projects", json={"name": "发布计划", "description": ""}, headers={"x-principal": "alice"})

    assert response.status_code == 200
    assert captured == {"principal": "alice", "project": "p-new", "role": "manager"}


@pytest.mark.anyio
async def test_retrieve_passes_stable_principal_to_store(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        store,
        "search",
        lambda question, project_id, principal_id, top_k: (
            captured.update(question=question, project_id=project_id, principal_id=principal_id, top_k=top_k)
            or [{"id": "c-1", "filename": "guide.md", "chunk_index": 0, "score": 0.8, "content": "内容", "summary": "摘要"}]
        ),
    )

    async with client as http:
        response = await http.post("/api/retrieve", json={"question": "怎么做", "project_id": "p-1"}, headers={"x-principal": "alice"})

    assert response.status_code == 200
    assert captured == {"question": "怎么做", "project_id": "p-1", "principal_id": "alice", "top_k": 5}
    assert "access_scope" not in response.text


@pytest.mark.anyio
async def test_retrieve_rejects_the_retired_scope_payload_field(client):
    async with client as http:
        response = await http.post(
            "/api/retrieve",
            json={"question": "怎么做", "project_id": "p-1", "access_scope": "old"},
            headers={"x-principal": "alice"},
        )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_document_listing_requires_project_read_permission(client, monkeypatch):
    monkeypatch.setattr(authz_store, "has_permission", lambda *_args: False)
    async with client as http:
        response = await http.get("/api/documents?project_id=p-1", headers={"x-principal": "alice"})

    assert response.status_code == 403


@pytest.mark.anyio
async def test_evidence_response_is_bounded_by_project(client, monkeypatch):
    monkeypatch.setattr(
        store,
        "get_evidence",
        lambda *_args: {
            "id": "c-1",
            "filename": "guide.md",
            "chunk_index": 0,
            "content": "内容",
            "summary": "摘要",
            "project_id": "p-1",
            "document_id": "doc-1",
            "source_type": "upload",
            "source_uri": "",
            "page": None,
            "metadata": {},
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    )
    async with client as http:
        response = await http.get("/api/evidence/c-1?project_id=p-1", headers={"x-principal": "alice"})

    assert response.status_code == 200
    assert response.json()["project_id"] == "p-1"
    assert "access_scope" not in response.json()


def test_rag_connection_sets_principal_for_rls(monkeypatch):
    executed = []
    monkeypatch.setenv("DATABASE_URL", "postgresql:///fake")

    class FakeConn:
        def execute(self, query, params):
            executed.append((query, params))

    class FakeConnection:
        def __class_getitem__(cls, _item):
            return cls

        @classmethod
        def connect(cls, _url, row_factory):
            del row_factory
            return FakeConn()

    monkeypatch.setattr(store.psycopg, "Connection", FakeConnection)
    _ = store.connect("alice")

    assert executed == [(f"SELECT set_config('{store.PRINCIPAL_SETTING}', %s, true)", ("alice",))]
