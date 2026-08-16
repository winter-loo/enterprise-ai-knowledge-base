import httpx
import pytest

from authz import store
from authz.main import app


class _Rows(list):
    """把 dict 列表伪装成 psycopg 结果集,提供 fetchone/fetchall。"""

    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class _FakeConnection:
    """按查询特征分发的假连接:projects 规范化、grants 查询、部门树递归。"""

    def __init__(self, projects=None, grants=None, descendants=None, count=None):
        self.projects = projects or []
        self.grants = grants or []
        self.descendants = descendants or []
        self.count = count

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute(self, query, params):
        if "to_regclass" in query:
            return _Rows([{"r": "projects"}])
        if "count(*) AS n" in query:
            return _Rows([{"n": self.count}])
        if "FROM projects" in query:
            if "ORDER BY created_at LIMIT 1" in query:
                return _Rows(self.projects[:1])
            return _Rows([p for p in self.projects if p["kb_id"] == params[0] and p["id"] == params[1]])
        if "FROM grants" in query:
            return _Rows([g for g in self.grants if g.get("user_id") == params[0] and g.get("project_id") == params[1]])
        if "RECURSIVE tree" in query:
            return _Rows([{"id": item} for item in self.descendants])
        return _Rows([])


def _connect(monkeypatch, connection):
    monkeypatch.setattr(store, "connect", lambda: connection)


def test_visible_scope_whole_project_grant_returns_full_scope(monkeypatch):
    connection = _FakeConnection(
        projects=[{"id": "p-1", "kb_id": "company"}],
        grants=[{"user_id": "alice", "project_id": "p-1", "department_id": None}],
    )
    _connect(monkeypatch, connection)

    scope = store.visible_scope("alice", "company", "p-1")

    assert scope == {"allowed": True, "project_id": "p-1", "scope_context": store.SCOPE_ALL}


def test_visible_scope_superadmin_returns_full_scope(monkeypatch):
    connection = _FakeConnection(projects=[{"id": "p-1", "kb_id": "company"}])
    _connect(monkeypatch, connection)
    monkeypatch.setattr(store, "is_superadmin", lambda user_id: user_id == "admin")

    scope = store.visible_scope("admin", "company", "p-1")

    assert scope == {"allowed": True, "project_id": "p-1", "scope_context": store.SCOPE_ALL}


def test_visible_scope_department_grant_returns_opaque_scope_context(monkeypatch):
    connection = _FakeConnection(
        projects=[{"id": "p-1", "kb_id": "company"}],
        grants=[{"user_id": "bob", "project_id": "p-1", "department_id": "eng"}],
        descendants=["eng", "eng-mobile"],
    )
    _connect(monkeypatch, connection)

    scope = store.visible_scope("bob", "company", "p-1")

    assert scope["allowed"] is True
    assert scope["project_id"] == "p-1"
    assert scope["scope_context"] == "eng,eng-mobile,general"


def test_visible_scope_denies_without_grant(monkeypatch):
    connection = _FakeConnection(projects=[{"id": "p-1", "kb_id": "company"}], grants=[])
    _connect(monkeypatch, connection)

    scope = store.visible_scope("carol", "company", "p-1")

    assert scope["allowed"] is False
    assert scope["project_id"] == "p-1"


def test_visible_scope_unknown_project_has_no_canonical_id(monkeypatch):
    connection = _FakeConnection(projects=[])
    _connect(monkeypatch, connection)

    scope = store.visible_scope("alice", "company", "other")

    assert scope == {"allowed": False, "project_id": None, "scope_context": store.SCOPE_ALL}


def test_visible_scope_default_alias_resolves_to_first_project(monkeypatch):
    connection = _FakeConnection(
        projects=[{"id": "p-1", "kb_id": "company"}],
        grants=[{"user_id": "alice", "project_id": "p-1", "department_id": None}],
    )
    _connect(monkeypatch, connection)

    scope = store.visible_scope("alice", "company", "default")

    assert scope == {"allowed": True, "project_id": "p-1", "scope_context": store.SCOPE_ALL}


def test_has_permission_checks_role_permissions_on_project(monkeypatch):
    _connect(monkeypatch, _FakeConnection(projects=[{"id": "p-1", "kb_id": "company"}], count=1))
    assert store.has_permission("alice", "document:upload", kb_id="company", project_id="p-1") is True


def test_has_permission_denies_when_role_lacks_permission(monkeypatch):
    _connect(monkeypatch, _FakeConnection(projects=[{"id": "p-1", "kb_id": "company"}], count=0))
    assert store.has_permission("alice", "kb:create", kb_id="company", project_id="p-1") is False


def test_has_permission_kb_scoped_query_uses_role_kb(monkeypatch):
    _connect(monkeypatch, _FakeConnection(count=1))
    assert store.has_permission("alice", "project:create", kb_id="company") is True


def test_has_permission_superadmin_bypasses_grants(monkeypatch):
    monkeypatch.setattr(store, "is_superadmin", lambda user_id: True)
    assert store.has_permission("root", "kb:create") is True


def test_has_permission_requires_kb_for_project_scope(monkeypatch):
    _connect(monkeypatch, _FakeConnection(count=1))
    assert store.has_permission("alice", "retrieve", project_id="p-1") is False


def test_department_exists_public_is_always_valid(monkeypatch):
    _connect(monkeypatch, _FakeConnection())
    assert store.department_exists("company", store.PUBLIC_DEPARTMENT) is True


def test_department_exists_checks_departments_table(monkeypatch):
    _connect(monkeypatch, _FakeConnection())
    monkeypatch.setattr(store, "_table_exists", lambda _conn, _name: True)
    assert store.department_exists("company", "eng") is False


@pytest.fixture
def client(monkeypatch):
    def visible_scope(user_id, kb_id, project_id):
        if project_id == "unknown":
            return {"allowed": False, "project_id": None, "scope_context": store.SCOPE_ALL}
        if user_id != "alice":
            return {"allowed": False, "project_id": "p-1", "scope_context": store.SCOPE_ALL}
        return {"allowed": True, "project_id": "p-1", "scope_context": "general,engineering"}

    monkeypatch.setattr(
        store,
        "has_permission",
        lambda user_id, permission, kb_id=None, project_id=None: user_id == "root" or permission == "retrieve",
    )
    monkeypatch.setattr(store, "visible_scope", visible_scope)
    monkeypatch.setattr(store, "department_exists", lambda kb_id, department: department == "general")
    monkeypatch.setattr(store, "list_users", lambda: [{"id": "alice", "display_name": "Alice", "is_superadmin": False}])
    monkeypatch.setattr(store, "create_user", lambda user_id, display_name="": {"id": user_id, "display_name": display_name, "is_superadmin": False})
    monkeypatch.setattr(
        store,
        "list_roles",
        lambda kb_id="company": [{"id": "company:viewer", "kb_id": kb_id, "name": "viewer", "permissions": ["retrieve"]}],
    )
    monkeypatch.setattr(
        store,
        "create_role",
        lambda kb_id, name, description, permissions: {"id": f"{kb_id}:{name}", "kb_id": kb_id, "name": name, "permissions": list(permissions)},
    )
    monkeypatch.setattr(store, "list_grants", lambda user_id=None: [{"user_id": "alice", "project_id": "p-1"}])
    monkeypatch.setattr(
        store,
        "create_grant",
        lambda user_id, role_id, project_id, department_id=None: {
            "user_id": user_id,
            "role_id": role_id,
            "project_id": project_id,
            "department_id": department_id,
        },
    )
    monkeypatch.setattr(store, "list_departments", lambda kb_id="company": [{"id": "eng", "kb_id": kb_id, "name": "研发", "is_public": False}])
    monkeypatch.setattr(
        store,
        "create_department",
        lambda kb_id, name, parent_id=None, is_public=False: {"id": "eng", "kb_id": kb_id, "name": name, "parent_id": parent_id, "is_public": is_public},
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
async def test_health(client):
    async with client as http:
        response = await http.get("/api/v1/authz/health")
    assert response.status_code == 200
    assert response.json()["service"].endswith("authz")


@pytest.mark.anyio
async def test_authorize_returns_decision(client):
    async with client as http:
        allowed = await http.post(
            "/api/v1/authz/authorize", json={"principal": "alice", "action": "retrieve", "resource": {"kb_id": "company", "project_id": "p-1"}}
        )
        denied = await http.post("/api/v1/authz/authorize", json={"principal": "alice", "action": "kb:create", "resource": {}})
    assert allowed.json()["allowed"] is True
    assert denied.json()["allowed"] is False
    assert denied.json()["reason"] is not None


@pytest.mark.anyio
async def test_visible_scope_returns_scope(client):
    async with client as http:
        granted = await http.post("/api/v1/authz/visible-scope", json={"principal": "alice", "kb_id": "company", "project_id": "p-1"})
        denied = await http.post("/api/v1/authz/visible-scope", json={"principal": "mallory", "kb_id": "company", "project_id": "p-1"})
        unknown = await http.post("/api/v1/authz/visible-scope", json={"principal": "alice", "kb_id": "company", "project_id": "unknown"})
    assert granted.json() == {"allowed": True, "project_id": "p-1", "scope_context": "general,engineering"}
    assert denied.json()["allowed"] is False
    assert unknown.json()["project_id"] is None


@pytest.mark.anyio
async def test_departments_validate(client):
    async with client as http:
        response = await http.post("/api/v1/authz/departments/validate", json={"kb_id": "company", "department": "general"})
    assert response.json() == {"valid": True}


@pytest.mark.anyio
async def test_user_role_grant_department_crud(client):
    async with client as http:
        users = await http.get("/api/v1/authz/users")
        created_user = await http.post("/api/v1/authz/users", json={"id": "bob", "display_name": "Bob"})
        roles = await http.get("/api/v1/authz/roles")
        created_role = await http.post("/api/v1/authz/roles", json={"kb_id": "company", "name": "editor", "permissions": ["document:upload"]})
        grants = await http.get("/api/v1/authz/grants")
        created_grant = await http.post("/api/v1/authz/grants", json={"user_id": "alice", "role_id": "company:viewer", "project_id": "p-1"})
        departments = await http.get("/api/v1/authz/departments")
        created_department = await http.post("/api/v1/authz/departments", json={"kb_id": "company", "name": "研发"})

    assert users.json()[0]["id"] == "alice"
    assert created_user.json()["id"] == "bob"
    assert roles.json()[0]["name"] == "viewer"
    assert created_role.json()["permissions"] == ["document:upload"]
    assert grants.json()[0]["project_id"] == "p-1"
    assert created_grant.json()["user_id"] == "alice"
    assert departments.json()[0]["name"] == "研发"
    assert created_department.json()["is_public"] is False
