"""authz 服务的数据层:权限模型的所有表、种子数据与授权决策函数。

权限模型是资源级 RBAC:users 通过 grants 绑定 (role, project, department?),角色决定
能做什么操作,project/department 决定能看哪些数据。本模块是授权的唯一所有者——
角色解析、部门树继承、`general` 公共部门语义都在这里,消费方(rag/session)只应用
visible_scope 返回的结果,不重新推导。

表归属:authz 拥有 departments/users/roles/grants;rag 拥有 knowledge_bases/
projects/knowledge_evidence。两个服务共享同一个 PostgreSQL(与 rag/session 的既有
约定一致),authz 对 rag 的 projects 表只有只读访问,用于把 `default` 别名解析为
规范项目 id,绝不写入。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import psycopg
from psycopg.rows import DictRow, dict_row

# 公共部门是内置的 well-known id:在「有权限访问的项目内」对所有 principal 可见,
# 不需要显式授权。它对应旧模型的 `general` 字符串,因此存量 evidence 无需迁移。
PUBLIC_DEPARTMENT = "general"

# 每个知识库预置的三档角色及其权限。permissions 是权限字符串集合,authorize 直接
# 按权限名匹配;以后要细粒度控制只需增加权限名,不必改授权查询。
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "viewer": ["retrieve", "ask", "evidence:read", "chat:start"],
    "editor": [
        "retrieve",
        "ask",
        "evidence:read",
        "chat:start",
        "document:upload",
        "document:import",
    ],
    "admin": [
        "retrieve",
        "ask",
        "evidence:read",
        "chat:start",
        "document:upload",
        "document:import",
        "project:create",
        "kb:create",
        "grant:manage",
        "department:manage",
        "user:manage",
    ],
}

DEFAULT_KB_ID = "company"

# 行级可见性的会话设置名,与 rag 服务的 connect() 约定一致:RAG 把 authz 计算好
# 的 scope_context 通过 set_config 写入该设置,RLS 策略据此过滤 knowledge_evidence。
SCOPE_SETTING = "app.visible_scope"
# '*' 表示对整个项目无行级收窄(全项目可见)。
SCOPE_ALL = "*"


def connect() -> psycopg.Connection[DictRow]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.Connection[DictRow].connect(database_url, row_factory=dict_row)


def now() -> str:
    return datetime.now(UTC).isoformat()


def _table_exists(conn: psycopg.Connection[DictRow], name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS r", (name,)).fetchone()
    return row is not None and row["r"] is not None


def _column_exists(conn: psycopg.Connection[DictRow], table: str, column: str) -> bool:
    row = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s", (table, column)).fetchone()
    return row is not None


def apply_rls(conn: psycopg.Connection[DictRow]) -> None:
    """在 knowledge_evidence 上启用 authz 拥有的行级安全策略(ADR 0004 D5)。

    读策略按当前事务的 SCOPE_SETTING 过滤可见行:'*' 表示全项目可见,否则只允许
    access_scope 命中逗号分隔的可见集合;未设置则该设置读取为 NULL,任意行都不可见
    (fail-closed)。写策略放行,写权限由应用层 authorize 门面把关,RLS 不做行级写过滤。

    knowledge_evidence 归 rag 服务所有,但它的可见性语义归 authz 数据面所有;
    authz 只读访问该表以应用 RLS,绝不写入其内容。表尚未创建时跳过(等待 rag 先启动)。
    """
    if not _table_exists(conn, "knowledge_evidence"):
        return
    if not _column_exists(conn, "knowledge_evidence", "access_scope"):
        # 表存在但还没有 access_scope 列(旧 schema), 跳过; 等 rag 用新 schema 重建。
        return
    _ = conn.execute("""
        ALTER TABLE knowledge_evidence ENABLE ROW LEVEL SECURITY;
        ALTER TABLE knowledge_evidence FORCE ROW LEVEL SECURITY;

        DROP POLICY IF EXISTS evidence_read ON knowledge_evidence;
        CREATE POLICY evidence_read ON knowledge_evidence
            FOR SELECT USING (
                current_setting('app.visible_scope', true) = '*'
                OR access_scope = ANY(string_to_array(current_setting('app.visible_scope', true), ','))
            );

        DROP POLICY IF EXISTS evidence_insert ON knowledge_evidence;
        CREATE POLICY evidence_insert ON knowledge_evidence FOR INSERT WITH CHECK (true);

        DROP POLICY IF EXISTS evidence_update ON knowledge_evidence;
        CREATE POLICY evidence_update ON knowledge_evidence FOR UPDATE USING (true) WITH CHECK (true);

        DROP POLICY IF EXISTS evidence_delete ON knowledge_evidence;
        CREATE POLICY evidence_delete ON knowledge_evidence FOR DELETE USING (true);
    """)


def init_db() -> None:
    with connect() as conn:
        _ = conn.execute("""
            CREATE TABLE IF NOT EXISTS departments (
                id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, parent_id TEXT REFERENCES departments(id),
                name TEXT NOT NULL, is_public BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(kb_id, name)
            );
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                is_superadmin BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS roles (
                id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', permissions TEXT[] NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL, UNIQUE(kb_id, name)
            );
            CREATE TABLE IF NOT EXISTS grants (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                role_id TEXT NOT NULL REFERENCES roles(id),
                project_id TEXT NOT NULL, department_id TEXT REFERENCES departments(id),
                created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(user_id, role_id, project_id, department_id)
            );
            CREATE INDEX IF NOT EXISTS idx_grants_user_project ON grants(user_id, project_id);
        """)
        # 引导超级管理员:SSO 接入前唯一的身份来源,见 ADR 0004 的 Open questions。
        _ = conn.execute(
            "INSERT INTO users(id, display_name, is_superadmin, created_at) VALUES(%s,%s,TRUE,%s) ON CONFLICT DO NOTHING",
            (os.getenv("AUTHZ_BOOTSTRAP_ADMIN", "admin"), "超级管理员", now()),
        )
        ensure_default_roles(conn, DEFAULT_KB_ID)
        apply_rls(conn)
        conn.commit()


def ensure_default_roles(conn: psycopg.Connection[DictRow], kb_id: str) -> None:
    for name, permissions in ROLE_PERMISSIONS.items():
        _ = conn.execute(
            "INSERT INTO roles(id, kb_id, name, description, permissions, created_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (f"{kb_id}:{name}", kb_id, name, f"{name} 角色", permissions, now()),
        )


def user_exists(user_id: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE id=%s", (user_id,)).fetchone()
    return row is not None


def is_superadmin(user_id: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT is_superadmin FROM users WHERE id=%s", (user_id,)).fetchone()
    return row is not None and bool(row["is_superadmin"])


def list_users() -> list[DictRow]:
    with connect() as conn:
        return conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()


def create_user(user_id: str, display_name: str = "") -> DictRow:
    with connect() as conn:
        _ = conn.execute(
            "INSERT INTO users(id, display_name, is_superadmin, created_at) VALUES(%s,%s,FALSE,%s) ON CONFLICT DO NOTHING",
            (user_id, display_name, now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()
        return cast(DictRow, row)


def list_roles(kb_id: str = DEFAULT_KB_ID) -> list[DictRow]:
    with connect() as conn:
        return conn.execute("SELECT * FROM roles WHERE kb_id=%s ORDER BY created_at", (kb_id,)).fetchall()


def create_role(kb_id: str, name: str, description: str, permissions: Sequence[str]) -> DictRow:
    role_id = f"{kb_id}:{name}"
    with connect() as conn:
        _ = conn.execute(
            "INSERT INTO roles(id, kb_id, name, description, permissions, created_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (role_id, kb_id, name, description, list(permissions), now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM roles WHERE id=%s", (role_id,)).fetchone()
        return cast(DictRow, row)


def list_grants(user_id: str | None = None) -> list[DictRow]:
    with connect() as conn:
        if user_id is not None:
            return conn.execute("SELECT * FROM grants WHERE user_id=%s ORDER BY created_at", (user_id,)).fetchall()
        return conn.execute("SELECT * FROM grants ORDER BY created_at").fetchall()


def create_grant(user_id: str, role_id: str, project_id: str, department_id: str | None = None) -> DictRow:
    with connect() as conn:
        if conn.execute("SELECT 1 FROM users WHERE id=%s", (user_id,)).fetchone() is None:
            raise ValueError(f"用户不存在：{user_id}")
        role = conn.execute("SELECT * FROM roles WHERE id=%s", (role_id,)).fetchone()
        if role is None:
            raise ValueError(f"角色不存在：{role_id}")
        if department_id is not None and department_id != PUBLIC_DEPARTMENT:
            department_row = conn.execute("SELECT 1 FROM departments WHERE id=%s", (department_id,)).fetchone()
            if department_row is None:
                raise ValueError(f"部门不存在：{department_id}")
        grant_id = uuid.uuid4().hex
        _ = conn.execute(
            "INSERT INTO grants(id, user_id, role_id, project_id, department_id, created_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (grant_id, user_id, role_id, project_id, department_id, now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM grants WHERE user_id=%s AND role_id=%s AND project_id=%s AND department_id IS NOT DISTINCT FROM %s",
            (user_id, role_id, project_id, department_id),
        ).fetchone()
        if row is None:
            raise ValueError("授权已存在或写入失败")
        return row


def list_departments(kb_id: str = DEFAULT_KB_ID) -> list[DictRow]:
    with connect() as conn:
        return conn.execute("SELECT * FROM departments WHERE kb_id=%s ORDER BY created_at", (kb_id,)).fetchall()


def create_department(kb_id: str, name: str, parent_id: str | None = None, is_public: bool = False) -> DictRow:
    department_id = uuid.uuid4().hex
    with connect() as conn:
        if parent_id is not None:
            parent = conn.execute("SELECT 1 FROM departments WHERE id=%s AND kb_id=%s", (parent_id, kb_id)).fetchone()
            if parent is None:
                raise ValueError(f"父部门不存在：{parent_id}")
        _ = conn.execute(
            "INSERT INTO departments(id, kb_id, parent_id, name, is_public, created_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (department_id, kb_id, parent_id, name, is_public, now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM departments WHERE id=%s", (department_id,)).fetchone()
        if row is None:
            raise ValueError("部门已存在或写入失败")
        return row


def department_exists(kb_id: str, department: str) -> bool:
    """部门有效性:`general` 是内置公共部门永远有效,其余必须存在于 departments 表。"""
    if department == PUBLIC_DEPARTMENT:
        return True
    with connect() as conn:
        if not _table_exists(conn, "departments"):
            return True
        row = conn.execute("SELECT 1 FROM departments WHERE id=%s AND kb_id=%s", (department, kb_id)).fetchone()
    return row is not None


def canonical_project_id(kb_id: str, project_id: str) -> str | None:
    """把项目 id 规范化为 rag 的 projects 表中的真实 id;`default` 解析为最早创建的项目。

    只读访问 rag 拥有的 projects 表(共享数据库);该表不存在时返回 None。
    """
    with connect() as conn:
        if not _table_exists(conn, "projects"):
            return None
        row = conn.execute("SELECT * FROM projects WHERE kb_id=%s AND id=%s", (kb_id, project_id)).fetchone()
        if not row and project_id == "default":
            row = conn.execute("SELECT * FROM projects WHERE kb_id=%s ORDER BY created_at LIMIT 1", (kb_id,)).fetchone()
    return None if row is None else str(row["id"])


def _department_descendants(conn: psycopg.Connection[DictRow], kb_id: str, department_id: str) -> list[str]:
    rows = conn.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT id FROM departments WHERE kb_id=%s AND id=%s
            UNION ALL
            SELECT d.id FROM departments d JOIN tree t ON d.parent_id=t.id WHERE d.kb_id=%s
        ) SELECT id FROM tree
        """,
        (kb_id, department_id, kb_id),
    ).fetchall()
    return [cast(str, row["id"]) for row in rows]


def visible_scope(user_id: str, kb_id: str, project_id: str) -> dict[str, object]:
    """返回 principal 对该项目的数据可见范围,以不透明 scope_context 表达(ADR 0004 D5)。

    allowed=False 表示无权访问;project_id 为规范项目 id(未知项目时为 None);
    scope_context 是不透明字符串,由调用方透传给 RAG,RAG 再 set_config 进数据库
    事务交给 RLS 行级过滤——RAG 不知道也不解释其含义。scope_context 取值:
    SCOPE_ALL('*') 表示全项目可见;否则为逗号分隔的可见部门 id 集合(含公共部门
    与授权部门的全部子树)。
    """
    canonical = canonical_project_id(kb_id, project_id)
    if canonical is None:
        return {"allowed": False, "project_id": None, "scope_context": SCOPE_ALL}
    if is_superadmin(user_id):
        return {"allowed": True, "project_id": canonical, "scope_context": SCOPE_ALL}
    with connect() as conn:
        grants = conn.execute("SELECT department_id FROM grants WHERE user_id=%s AND project_id=%s", (user_id, canonical)).fetchall()
    if not grants:
        return {"allowed": False, "project_id": canonical, "scope_context": SCOPE_ALL}
    if any(row["department_id"] is None for row in grants):
        return {"allowed": True, "project_id": canonical, "scope_context": SCOPE_ALL}
    departments = {PUBLIC_DEPARTMENT}
    with connect() as conn:
        for row in grants:
            departments.update(_department_descendants(conn, kb_id, cast(str, row["department_id"])))
    return {"allowed": True, "project_id": canonical, "scope_context": ",".join(sorted(departments))}


def has_permission(user_id: str, permission: str, kb_id: str | None = None, project_id: str | None = None) -> bool:
    """角色权限检查:项目级(project_id 给定)或知识库级(只有 kb_id)。

    超级管理员直接放行;其余按 grants 绑定的角色 permissions 数组是否包含
    permission 判定,`default` 项目别名在这里同样先规范化。
    """
    if is_superadmin(user_id):
        return True
    if project_id is not None:
        if kb_id is None:
            return False
        canonical = canonical_project_id(kb_id, project_id)
        if canonical is None:
            return False
        with connect() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM grants g JOIN roles r ON g.role_id=r.id WHERE g.user_id=%s AND g.project_id=%s AND %s = ANY(r.permissions)",
                (user_id, canonical, permission),
            ).fetchone()
    elif kb_id is not None:
        with connect() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM grants g JOIN roles r ON g.role_id=r.id WHERE g.user_id=%s AND r.kb_id=%s AND %s = ANY(r.permissions)",
                (user_id, kb_id, permission),
            ).fetchone()
    else:
        return False
    return row is not None and int(cast(int, row["n"])) > 0
