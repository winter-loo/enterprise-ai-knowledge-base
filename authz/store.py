"""Data layer for stable Principal-to-Project grants and their RLS policies."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Literal, cast

import psycopg
from psycopg.rows import DictRow, dict_row

from shared.database_security import PRINCIPAL_SETTING, require_runtime_database_safety, schema_provisioning_enabled

ProjectRole = Literal["viewer", "editor", "manager"]

ROLE_RANK: dict[ProjectRole, int] = {"viewer": 1, "editor": 2, "manager": 3}
ACTION_MINIMUM_ROLE: dict[str, ProjectRole] = {
    "project:read": "viewer",
    "document:read": "viewer",
    "retrieve": "viewer",
    "ask": "viewer",
    "evidence:read": "viewer",
    "chat:start": "viewer",
    "document:write": "editor",
    "document:upload": "editor",
    "document:import": "editor",
    "grant:manage": "manager",
}


def connect() -> psycopg.Connection[DictRow]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.Connection[DictRow].connect(database_url, row_factory=dict_row)


def now() -> str:
    return datetime.now(UTC).isoformat()


def role_allows(role: str, action: str) -> bool:
    """Return whether one fixed Project role permits an action."""
    minimum = ACTION_MINIMUM_ROLE.get(action)
    return minimum is not None and role in ROLE_RANK and ROLE_RANK[role] >= ROLE_RANK[minimum]


def is_valid_role(role: str) -> bool:
    return role in ROLE_RANK


def _table_exists(conn: psycopg.Connection[DictRow], name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS relation", (name,)).fetchone()
    return row is not None and row["relation"] is not None


def configured_platform_administrators() -> set[str]:
    raw = os.getenv("AUTHZ_DEV_PLATFORM_ADMINS", "admin") if os.getenv("APP_ENV", "development") == "development" else os.getenv("AUTHZ_PLATFORM_ADMINS", "")
    return {value.strip() for value in raw.split(",") if value.strip()}


def init_db() -> None:
    """Initialise only authorization-owned tables; never reset existing data."""
    with connect() as conn:
        if schema_provisioning_enabled():
            _ = conn.execute(
                """
            CREATE TABLE IF NOT EXISTS platform_administrators (
                principal_id TEXT PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_grants (
                principal_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('viewer', 'editor', 'manager')),
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(principal_id, project_id)
            );
            CREATE INDEX IF NOT EXISTS idx_project_grants_project_principal
                ON project_grants(project_id, principal_id);
                """
            )
            for principal_id in configured_platform_administrators():
                _ = conn.execute(
                    "INSERT INTO platform_administrators(principal_id, created_at) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (principal_id, now()),
                )
            conn.commit()
            apply_rls(conn)
        else:
            require_runtime_database_safety(conn, ("platform_administrators", "project_grants"))


def is_platform_administrator(principal_id: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM platform_administrators WHERE principal_id=%s", (principal_id,)).fetchone()
    return row is not None


def project_role(principal_id: str, project_id: str) -> ProjectRole | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT role FROM project_grants WHERE principal_id=%s AND project_id=%s",
            (principal_id, project_id),
        ).fetchone()
    return cast(ProjectRole, row["role"]) if row is not None else None


def has_permission(principal_id: str, action: str, project_id: str | None = None) -> bool:
    if action != "project:create" and action not in ACTION_MINIMUM_ROLE:
        return False
    if is_platform_administrator(principal_id):
        return True
    if action == "project:create":
        with connect() as conn:
            row = conn.execute("SELECT 1 FROM project_grants WHERE principal_id=%s AND role='manager' LIMIT 1", (principal_id,)).fetchone()
        return row is not None
    if project_id is None:
        return False
    role = project_role(principal_id, project_id)
    return role is not None and role_allows(role, action)


def list_accessible_project_ids(principal_id: str) -> list[str] | None:
    """Return explicit Project ids, or ``None`` for a platform administrator."""
    if is_platform_administrator(principal_id):
        return None
    with connect() as conn:
        rows = conn.execute(
            "SELECT project_id FROM project_grants WHERE principal_id=%s ORDER BY project_id",
            (principal_id,),
        ).fetchall()
    return [cast(str, row["project_id"]) for row in rows]


def list_grants(project_id: str) -> list[DictRow]:
    with connect() as conn:
        return conn.execute(
            "SELECT principal_id, project_id, role, created_at, updated_at FROM project_grants WHERE project_id=%s ORDER BY principal_id",
            (project_id,),
        ).fetchall()


def upsert_grant(principal_id: str, project_id: str, role: str) -> DictRow:
    if not is_valid_role(role):
        raise ValueError("角色必须是 viewer、editor 或 manager")
    with connect() as conn:
        timestamp = now()
        _ = conn.execute(
            """
            INSERT INTO project_grants(principal_id, project_id, role, created_at, updated_at)
            VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(principal_id, project_id)
            DO UPDATE SET role=EXCLUDED.role, updated_at=EXCLUDED.updated_at
            """,
            (principal_id, project_id, role, timestamp, timestamp),
        )
        conn.commit()
        row = conn.execute(
            "SELECT principal_id, project_id, role, created_at, updated_at FROM project_grants WHERE principal_id=%s AND project_id=%s",
            (principal_id, project_id),
        ).fetchone()
    return cast(DictRow, row)


def revoke_grant(principal_id: str, project_id: str) -> bool:
    with connect() as conn:
        deleted = conn.execute(
            "DELETE FROM project_grants WHERE principal_id=%s AND project_id=%s",
            (principal_id, project_id),
        ).rowcount
        conn.commit()
    return bool(deleted)


def _current_principal_sql() -> str:
    return f"NULLIF(current_setting('{PRINCIPAL_SETTING}', true), '')"


def _project_read_sql(project_column: str) -> str:
    principal = _current_principal_sql()
    return f"""
        EXISTS(SELECT 1 FROM platform_administrators pa WHERE pa.principal_id={principal})
        OR EXISTS(
            SELECT 1 FROM project_grants project_grant
            WHERE project_grant.principal_id={principal} AND project_grant.project_id={project_column}
              AND project_grant.role IN ('viewer', 'editor', 'manager')
        )
    """


def _project_write_sql(project_column: str) -> str:
    principal = _current_principal_sql()
    return f"""
        EXISTS(SELECT 1 FROM platform_administrators pa WHERE pa.principal_id={principal})
        OR EXISTS(
            SELECT 1 FROM project_grants project_grant
            WHERE project_grant.principal_id={principal} AND project_grant.project_id={project_column}
              AND project_grant.role IN ('editor', 'manager')
        )
    """


def _session_owner_sql(session_alias: str) -> str:
    principal = _current_principal_sql()
    return f"""
        {session_alias}.owner_principal_id={principal}
        AND EXISTS(
            SELECT 1 FROM project_grants project_grant
            WHERE project_grant.principal_id={principal} AND project_grant.project_id={session_alias}.project_id
              AND project_grant.role IN ('viewer', 'editor', 'manager')
        )
    """


def _session_child_owner_sql(child_table: str, session_id_column: str) -> str:
    principal = _current_principal_sql()
    return f"""
        EXISTS(
            SELECT 1 FROM chat_sessions session
            WHERE session.id={child_table}.{session_id_column}
              AND session.owner_principal_id={principal}
              AND EXISTS(
                  SELECT 1 FROM project_grants project_grant
                  WHERE project_grant.principal_id={principal}
                    AND project_grant.project_id=session.project_id
                    AND project_grant.role IN ('viewer', 'editor', 'manager')
              )
        )
    """


def apply_rls(conn: psycopg.Connection[DictRow]) -> None:
    """Install fail-closed RLS on tables that have already been created."""
    if _table_exists(conn, "knowledge_evidence"):
        _ = conn.execute(
            cast(
                Any,
                f"""
            ALTER TABLE knowledge_evidence ENABLE ROW LEVEL SECURITY;
            ALTER TABLE knowledge_evidence FORCE ROW LEVEL SECURITY;
            DROP POLICY IF EXISTS evidence_read ON knowledge_evidence;
            DROP POLICY IF EXISTS evidence_write ON knowledge_evidence;
            CREATE POLICY evidence_read ON knowledge_evidence FOR SELECT USING ({_project_read_sql("project_id")});
            CREATE POLICY evidence_write ON knowledge_evidence FOR ALL
                USING ({_project_write_sql("project_id")})
                WITH CHECK ({_project_write_sql("project_id")});
            """,
            )
        )

    if _table_exists(conn, "chat_sessions"):
        _ = conn.execute(
            cast(
                Any,
                f"""
            ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY;
            ALTER TABLE chat_sessions FORCE ROW LEVEL SECURITY;
            DROP POLICY IF EXISTS session_owner ON chat_sessions;
            CREATE POLICY session_owner ON chat_sessions FOR ALL
                USING ({_session_owner_sql("chat_sessions")})
                WITH CHECK ({_session_owner_sql("chat_sessions")});
            """,
            )
        )
    if _table_exists(conn, "chat_messages"):
        owner_sql = _session_child_owner_sql("chat_messages", "session_id")
        _ = conn.execute(
            cast(
                Any,
                f"""
            ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;
            ALTER TABLE chat_messages FORCE ROW LEVEL SECURITY;
            DROP POLICY IF EXISTS message_owner ON chat_messages;
            CREATE POLICY message_owner ON chat_messages FOR ALL
                USING ({owner_sql})
                WITH CHECK ({owner_sql});
            """,
            )
        )
    if _table_exists(conn, "chat_session_summaries"):
        owner_sql = _session_child_owner_sql("chat_session_summaries", "session_id")
        _ = conn.execute(
            cast(
                Any,
                f"""
            ALTER TABLE chat_session_summaries ENABLE ROW LEVEL SECURITY;
            ALTER TABLE chat_session_summaries FORCE ROW LEVEL SECURITY;
            DROP POLICY IF EXISTS summary_owner ON chat_session_summaries;
            CREATE POLICY summary_owner ON chat_session_summaries FOR ALL
                USING ({owner_sql})
                WITH CHECK ({owner_sql});
            """,
            )
        )


def apply_rls_for_current_database() -> None:
    if not schema_provisioning_enabled():
        return
    with connect() as conn:
        apply_rls(conn)
        conn.commit()
