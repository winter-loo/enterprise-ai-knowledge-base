"""authz 服务:独立的权限控制部署单元(ADR 0004)。

授权决策(authorize / visible-scope)是数据访问的唯一裁决点;rag/session 通过
本服务的 HTTP 接口获取决策并把可见范围下推到检索 SQL。管理员 CRUD(users/roles/
grants/departments)也在这里,权限模型的所有状态由本服务独占。

运行:uv run uvicorn authz.main:app --host 127.0.0.1 --port 8012
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from authz import store

JsonObject = dict[str, object]


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init_db()
    yield


app = FastAPI(title="Enterprise AI Knowledge Base Authz", version="0.1.0", lifespan=lifespan)


class AuthorizeRequest(BaseModel):
    action: str = Field(min_length=1, max_length=100)
    resource: dict[str, str | None] = Field(default_factory=dict)


class VisibleScopeRequest(BaseModel):
    kb_id: str = "company"
    project_id: str = "default"


class DepartmentValidate(BaseModel):
    kb_id: str = "company"
    department: str = Field(min_length=1, max_length=200)


class UserCreate(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(default="", max_length=200)


class RoleCreate(BaseModel):
    kb_id: str = "company"
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    permissions: list[str] = Field(default_factory=list)


class GrantCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    role_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    department_id: str | None = None


class DepartmentCreate(BaseModel):
    kb_id: str = "company"
    name: str = Field(min_length=1, max_length=100)
    parent_id: str | None = None
    is_public: bool = False


@app.get("/api/v1/authz/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "enterprise-ai-knowledge-base-authz"}


@app.post("/api/v1/authz/authorize")
def authorize(payload: AuthorizeRequest, x_principal: str = Header(min_length=1, max_length=200)) -> JsonObject:
    kb_id = payload.resource.get("kb_id")
    project_id = payload.resource.get("project_id")
    allowed = store.has_permission(x_principal, payload.action, kb_id=kb_id, project_id=project_id)
    return {"allowed": allowed, "reason": None if allowed else "无权限执行该操作"}


@app.post("/api/v1/authz/visible-scope")
def visible_scope(payload: VisibleScopeRequest, x_principal: str = Header(min_length=1, max_length=200)) -> JsonObject:
    return store.visible_scope(x_principal, payload.kb_id, payload.project_id)


@app.post("/api/v1/authz/departments/validate")
def validate_department(payload: DepartmentValidate) -> JsonObject:
    return {"valid": store.department_exists(payload.kb_id, payload.department)}


@app.get("/api/v1/authz/users")
def list_users() -> list[JsonObject]:
    return [dict(row) for row in store.list_users()]


@app.post("/api/v1/authz/users")
def create_user(payload: UserCreate, x_principal: str = Header(min_length=1, max_length=200)) -> JsonObject:
    if not store.has_permission(x_principal, "user:manage"):
        raise HTTPException(403, "无权限管理用户")
    return dict(store.create_user(payload.id, payload.display_name))


@app.get("/api/v1/authz/roles")
def list_roles(kb_id: str = store.DEFAULT_KB_ID) -> list[JsonObject]:
    return [dict(row) for row in store.list_roles(kb_id)]


@app.post("/api/v1/authz/roles")
def create_role(payload: RoleCreate, x_principal: str = Header(min_length=1, max_length=200)) -> JsonObject:
    if not store.has_permission(x_principal, "grant:manage", kb_id=payload.kb_id):
        raise HTTPException(403, "无权限管理角色")
    try:
        return dict(store.create_role(payload.kb_id, payload.name, payload.description, payload.permissions))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/authz/grants")
def list_grants(user_id: str | None = None) -> list[JsonObject]:
    return [dict(row) for row in store.list_grants(user_id)]


@app.post("/api/v1/authz/grants")
def create_grant(payload: GrantCreate, x_principal: str = Header(min_length=1, max_length=200)) -> JsonObject:
    kb_id = store.project_kb_id(payload.project_id)
    if kb_id is None or not store.has_permission(x_principal, "grant:manage", kb_id=kb_id, project_id=payload.project_id):
        raise HTTPException(403, "无权限管理授权")
    try:
        return dict(store.create_grant(payload.user_id, payload.role_id, payload.project_id, payload.department_id))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/authz/departments")
def list_departments(kb_id: str = store.DEFAULT_KB_ID) -> list[JsonObject]:
    return [dict(row) for row in store.list_departments(kb_id)]


@app.post("/api/v1/authz/departments")
def create_department(payload: DepartmentCreate, x_principal: str = Header(min_length=1, max_length=200)) -> JsonObject:
    if not store.has_permission(x_principal, "department:manage", kb_id=payload.kb_id):
        raise HTTPException(403, "无权限管理部门")
    try:
        return dict(store.create_department(payload.kb_id, payload.name, payload.parent_id, payload.is_public))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
