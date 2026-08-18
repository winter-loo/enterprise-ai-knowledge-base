import httpx
import pytest

from authz import store
from authz.main import app


def test_project_permission_uses_fixed_role_hierarchy(monkeypatch):
    monkeypatch.setattr(store, "is_platform_administrator", lambda _principal: False)
    monkeypatch.setattr(store, "project_role", lambda _principal, _project: "viewer")

    assert store.has_permission("alice", "retrieve", "project-1") is True
    assert store.has_permission("alice", "document:write", "project-1") is False


def test_platform_administrator_bypasses_project_role_lookup(monkeypatch):
    monkeypatch.setattr(store, "is_platform_administrator", lambda principal: principal == "admin")
    monkeypatch.setattr(store, "project_role", lambda *_: pytest.fail("platform administrator must not need a Grant"))

    assert store.has_permission("admin", "document:write", "project-1") is True


def test_platform_administrator_does_not_approve_undefined_actions(monkeypatch):
    monkeypatch.setattr(store, "is_platform_administrator", lambda principal: principal == "admin")

    assert store.has_permission("admin", "session:read", "project-1") is False


def test_rls_policies_use_principal_and_never_scope_context(monkeypatch):
    executed: list[str] = []

    class Connection:
        def execute(self, query, _params=None):
            executed.append(str(query))

    monkeypatch.setattr(store, "_table_exists", lambda _conn, name: name in {"knowledge_evidence", "chat_sessions", "chat_messages", "chat_session_summaries"})
    store.apply_rls(Connection())  # type: ignore[arg-type]

    policy_sql = "\n".join(executed)
    assert "app.principal_id" in policy_sql
    assert "project_grants" in policy_sql
    assert "project_grant" in policy_sql
    assert "project_grants grant" not in policy_sql
    assert "visible_scope" not in policy_sql
    assert "access_scope" not in policy_sql


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        store,
        "has_permission",
        lambda principal, action, project_id=None: principal == "admin" or (principal == "manager" and action == "grant:manage" and project_id == "p-1"),
    )
    monkeypatch.setattr(
        store,
        "list_grants",
        lambda project_id: [{"principal_id": "alice", "project_id": project_id, "role": "viewer"}],
    )
    monkeypatch.setattr(
        store,
        "upsert_grant",
        lambda principal_id, project_id, role: {"principal_id": principal_id, "project_id": project_id, "role": role},
    )
    monkeypatch.setattr(store, "revoke_grant", lambda _principal_id, _project_id: True)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
async def test_manager_can_manage_only_its_project_grants(client):
    async with client as http:
        allowed = await http.put(
            "/api/v1/authz/projects/p-1/grants",
            json={"principal_id": "alice", "role": "editor"},
            headers={"x-principal": "manager"},
        )
        denied = await http.put(
            "/api/v1/authz/projects/p-2/grants",
            json={"principal_id": "alice", "role": "editor"},
            headers={"x-principal": "manager"},
        )

    assert allowed.status_code == 200
    assert allowed.json()["role"] == "editor"
    assert denied.status_code == 403


@pytest.mark.anyio
async def test_authorize_has_no_visible_scope_response(client):
    async with client as http:
        response = await http.post(
            "/api/v1/authz/authorize",
            json={"action": "retrieve", "resource": {"project_id": "p-1"}},
            headers={"x-principal": "manager"},
        )

    assert response.json() == {"allowed": False, "reason": "无权限执行该操作"}
