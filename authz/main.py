from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from authz import store


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init_db()
    yield


app = FastAPI(title="Enterprise Knowledge Base Authz", lifespan=lifespan)


class AuthorizeResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = Field(default=None, min_length=1, max_length=200)


class AuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=100)
    resource: AuthorizeResource = Field(default_factory=AuthorizeResource)


class GrantUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_id: str = Field(min_length=1, max_length=300)
    role: Literal["viewer", "editor", "manager"]


def require_manager_or_platform(principal_id: str, project_id: str) -> None:
    if not store.has_permission(principal_id, "grant:manage", project_id):
        raise HTTPException(403, "无权限管理该 Project 的授权")


@app.get("/api/v1/authz/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "enterprise-ai-knowledge-base-authz"}


@app.post("/api/v1/authz/authorize")
def authorize(payload: AuthorizeRequest, x_principal: str = Header(min_length=1, max_length=300)) -> dict[str, object]:
    allowed = store.has_permission(x_principal, payload.action, payload.resource.project_id)
    return {"allowed": allowed, "reason": None if allowed else "无权限执行该操作"}


@app.get("/api/v1/authz/projects/{project_id}/grants")
def grants(project_id: str, x_principal: str = Header(min_length=1, max_length=300)) -> list[dict[str, object]]:
    require_manager_or_platform(x_principal, project_id)
    return [dict(row) for row in store.list_grants(project_id)]


@app.put("/api/v1/authz/projects/{project_id}/grants")
def put_grant(project_id: str, payload: GrantUpsert, x_principal: str = Header(min_length=1, max_length=300)) -> dict[str, object]:
    require_manager_or_platform(x_principal, project_id)
    return dict(store.upsert_grant(payload.principal_id, project_id, payload.role))


@app.delete("/api/v1/authz/projects/{project_id}/grants/{principal_id}")
def delete_grant(project_id: str, principal_id: str, x_principal: str = Header(min_length=1, max_length=300)) -> dict[str, object]:
    require_manager_or_platform(x_principal, project_id)
    return {"principal_id": principal_id, "project_id": project_id, "deleted": store.revoke_grant(principal_id, project_id)}
