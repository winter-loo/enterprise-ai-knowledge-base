from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TypedDict, cast

import psycopg
from psycopg.rows import DictRow, dict_row

from shared.database_security import PRINCIPAL_SETTING, require_runtime_database_safety, schema_provisioning_enabled
from shared.tokens import count_tokens

GENERATION_LEASE_SECONDS = 900
MODEL_CONTEXT_TOKENS = int(os.getenv("RAG_CONTEXT_TOKENS", "128000"))
RESERVE_TOKENS = int(os.getenv("RAG_RESERVE_TOKENS", "16384"))
KEEP_RECENT_TOKENS = int(os.getenv("RAG_KEEP_RECENT_TOKENS", "20000"))
CONTEXT_BUDGET = MODEL_CONTEXT_TOKENS - RESERVE_TOKENS


class GenerationState(TypedDict):
    summary: str
    verbatim: list[DictRow]
    should_compact: bool
    project_id: str


def connect(principal_id: str = "") -> psycopg.Connection[DictRow]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    conn = psycopg.Connection[DictRow].connect(database_url, row_factory=dict_row)
    conn.execute(f"SELECT set_config('{PRINCIPAL_SETTING}', %s, true)", (principal_id,))
    return conn


def now() -> str:
    return datetime.now(UTC).isoformat()


def init_db() -> None:
    with connect() as conn:
        legacy = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name='chat_messages' AND column_name IN ('department', 'session_token_hash')"
        ).fetchone()
        if legacy is not None:
            raise RuntimeError("检测到旧会话数据结构；请先运行 make reset-dev-data")
        if schema_provisioning_enabled():
            _ = conn.execute(
                """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                owner_principal_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '新的研究',
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_sessions_owner_project_updated
                ON chat_sessions(owner_principal_id, project_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS chat_messages (
                id BIGSERIAL PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                generation_id TEXT,
                generation_complete BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, id);
            CREATE TABLE IF NOT EXISTS chat_session_summaries (
                session_id TEXT PRIMARY KEY REFERENCES chat_sessions(id) ON DELETE CASCADE,
                summary TEXT NOT NULL DEFAULT '',
                first_kept_id BIGINT,
                updated_at TIMESTAMPTZ NOT NULL
            );
                """
            )
            conn.commit()
        else:
            require_runtime_database_safety(conn, ("chat_sessions", "chat_messages", "chat_session_summaries"))
    from authz import store as authz_store

    authz_store.apply_rls_for_current_database()


def create_session(session_id: str, owner_principal_id: str, project_id: str) -> DictRow:
    with connect(owner_principal_id) as conn:
        created = now()
        _ = conn.execute(
            "INSERT INTO chat_sessions(id, owner_principal_id, project_id, created_at, updated_at) VALUES(%s,%s,%s,%s,%s)",
            (session_id, owner_principal_id, project_id, created, created),
        )
        row = conn.execute("SELECT * FROM chat_sessions WHERE id=%s", (session_id,)).fetchone()
        conn.commit()
    return cast(DictRow, row)


def list_sessions(principal_id: str, project_id: str) -> list[DictRow]:
    with connect(principal_id) as conn:
        return conn.execute(
            "SELECT id, project_id, title, created_at, updated_at FROM chat_sessions WHERE project_id=%s ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()


def get_session(session_id: str, principal_id: str) -> DictRow | None:
    with connect(principal_id) as conn:
        return conn.execute(
            "SELECT id, project_id, title, created_at, updated_at FROM chat_sessions WHERE id=%s",
            (session_id,),
        ).fetchone()


def _title_from_question(content: str) -> str:
    return " ".join(content.split())[:36] or "新的研究"


def begin_generation(session_id: str, principal_id: str, content: str, generation_id: str) -> GenerationState | None:
    """Reserve one owner-authorised turn before expensive retrieval and generation."""
    with connect(principal_id) as conn:
        _ = conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (session_id,))
        session = conn.execute("SELECT project_id, title FROM chat_sessions WHERE id=%s", (session_id,)).fetchone()
        if session is None:
            return None
        _ = conn.execute(
            "DELETE FROM chat_messages WHERE session_id=%s AND generation_complete=FALSE AND created_at < now() - make_interval(secs => %s)",
            (session_id, GENERATION_LEASE_SECONDS),
        )
        active = conn.execute(
            "SELECT 1 FROM chat_messages WHERE session_id=%s AND generation_complete=FALSE LIMIT 1",
            (session_id,),
        ).fetchone()
        if active is not None:
            return None
        summary_row = conn.execute(
            "SELECT summary, first_kept_id FROM chat_session_summaries WHERE session_id=%s",
            (session_id,),
        ).fetchone()
        summary = cast(str, summary_row["summary"]) if summary_row is not None else ""
        first_kept_id = cast(int | None, summary_row["first_kept_id"]) if summary_row is not None else None
        verbatim = conn.execute(
            "SELECT id, role, content, created_at FROM chat_messages WHERE session_id=%s AND generation_complete=TRUE AND id >= %s ORDER BY id",
            (session_id, first_kept_id or 1),
        ).fetchall()
        history_tokens = count_tokens(summary) + sum(count_tokens(cast(str, message["content"])) for message in verbatim)
        _ = conn.execute(
            "INSERT INTO chat_messages(session_id, role, content, created_at, generation_id) VALUES(%s,'user',%s,%s,%s)",
            (session_id, content, now(), generation_id),
        )
        if cast(str, session["title"]) == "新的研究":
            _ = conn.execute("UPDATE chat_sessions SET title=%s, updated_at=%s WHERE id=%s", (_title_from_question(content), now(), session_id))
        else:
            _ = conn.execute("UPDATE chat_sessions SET updated_at=%s WHERE id=%s", (now(), session_id))
        conn.commit()
        return {
            "summary": summary,
            "verbatim": verbatim,
            "should_compact": history_tokens > CONTEXT_BUDGET,
            "project_id": cast(str, session["project_id"]),
        }


def split_for_compaction(verbatim: list[DictRow]) -> tuple[list[DictRow], list[DictRow]]:
    cut = len(verbatim)
    kept_tokens = 0
    for index in range(len(verbatim) - 1, -1, -1):
        message_tokens = count_tokens(cast(str, verbatim[index]["content"]))
        if cut < len(verbatim) and kept_tokens + message_tokens > KEEP_RECENT_TOKENS:
            break
        cut = index
        kept_tokens += message_tokens
    while cut > 0 and verbatim[cut]["role"] == "assistant":
        cut -= 1
    return verbatim[:cut], verbatim[cut:]


def save_summary(session_id: str, principal_id: str, summary: str, first_kept_id: int | None) -> bool:
    with connect(principal_id) as conn:
        _ = conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (session_id,))
        exists = conn.execute("SELECT 1 FROM chat_sessions WHERE id=%s", (session_id,)).fetchone()
        if exists is None:
            return False
        _ = conn.execute(
            """
            INSERT INTO chat_session_summaries(session_id, summary, first_kept_id, updated_at)
            VALUES(%s,%s,%s,%s)
            ON CONFLICT(session_id) DO UPDATE SET summary=EXCLUDED.summary, first_kept_id=EXCLUDED.first_kept_id, updated_at=EXCLUDED.updated_at
            """,
            (session_id, summary, first_kept_id, now()),
        )
        conn.commit()
        return True


def list_messages(session_id: str, principal_id: str, limit: int | None = None) -> list[DictRow]:
    with connect(principal_id) as conn:
        if limit is not None:
            return conn.execute(
                "SELECT role, content, created_at FROM (SELECT * FROM chat_messages WHERE session_id=%s AND generation_complete=TRUE ORDER BY id DESC LIMIT %s) messages ORDER BY id",
                (session_id, limit),
            ).fetchall()
        return conn.execute(
            "SELECT role, content, created_at FROM chat_messages WHERE session_id=%s AND generation_complete=TRUE ORDER BY id",
            (session_id,),
        ).fetchall()


def clear_session(session_id: str, principal_id: str) -> bool:
    with connect(principal_id) as conn:
        _ = conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (session_id,))
        deleted = conn.execute("DELETE FROM chat_sessions WHERE id=%s", (session_id,)).rowcount
        conn.commit()
    return bool(deleted)


def add_assistant_if_generation_active(session_id: str, principal_id: str, generation_id: str, content: str) -> bool:
    with connect(principal_id) as conn:
        _ = conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (session_id,))
        inserted = conn.execute(
            """
            INSERT INTO chat_messages(session_id, role, content, created_at, generation_id, generation_complete)
            SELECT %s, 'assistant', %s, %s, %s, FALSE
            WHERE EXISTS(SELECT 1 FROM chat_messages WHERE session_id=%s AND generation_id=%s AND role='user' AND generation_complete=FALSE)
            """,
            (session_id, content, now(), generation_id, session_id, generation_id),
        ).rowcount
        if inserted == 1:
            _ = conn.execute(
                "UPDATE chat_messages SET generation_complete=TRUE WHERE session_id=%s AND generation_id=%s",
                (session_id, generation_id),
            )
            _ = conn.execute("UPDATE chat_sessions SET updated_at=%s WHERE id=%s", (now(), session_id))
        conn.commit()
        return inserted == 1


def rollback_generation(session_id: str, principal_id: str, generation_id: str) -> None:
    with connect(principal_id) as conn:
        _ = conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (session_id,))
        _ = conn.execute(
            "DELETE FROM chat_messages WHERE session_id=%s AND generation_id=%s AND generation_complete=FALSE",
            (session_id, generation_id),
        )
        conn.commit()
