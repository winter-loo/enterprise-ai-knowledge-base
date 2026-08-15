from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime
from typing import cast

import psycopg
from psycopg.rows import DictRow, dict_row

GENERATION_LEASE_SECONDS = 900


def connect() -> psycopg.Connection[DictRow]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.Connection[DictRow].connect(database_url, row_factory=dict_row)


def now() -> str:
    return datetime.now(UTC).isoformat()


def init_db() -> None:
    with connect() as conn:
        _ = conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
                kb_id TEXT NOT NULL DEFAULT 'company', project_id TEXT NOT NULL DEFAULT 'default',
                department TEXT NOT NULL DEFAULT 'general', generation_id TEXT,
                session_token_hash TEXT NOT NULL DEFAULT '', generation_complete BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS chat_session_tombstones (
                session_id TEXT NOT NULL, kb_id TEXT NOT NULL, project_id TEXT NOT NULL,
                department TEXT NOT NULL, session_token_hash TEXT NOT NULL DEFAULT '', cleared_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(session_id,kb_id,project_id,department,session_token_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, id);
        """)
        _ = conn.execute(
            """
            DELETE FROM chat_messages
            WHERE generation_complete=FALSE
              AND created_at < now() - make_interval(secs => %s)
            """,
            (GENERATION_LEASE_SECONDS,),
        )
        conn.commit()


def session_token_hash(session_token: str) -> str:
    return hashlib.sha256(session_token.encode()).hexdigest()


def begin_generation(
    session_id: str,
    session_token: str,
    content: str,
    generation_id: str,
    kb_id: str = "company",
    project_id: str = "default",
    department: str = "general",
    history_limit: int = 12,
) -> list[DictRow] | None:
    """Reserve one chat turn and return only history from completed turns.

    The reservation is deliberately committed before retrieval/LLM work: those
    operations can take a long time and must not hold a database transaction.
    The pending user row, generation_id, session token, and advisory lock give
    later completion/rollback code an atomic way to decide whether the stream
    may still write an assistant message.
    """
    with connect() as conn:
        _ = conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (session_id,))
        token_hash = session_token_hash(session_token)
        tombstone = conn.execute(
            """
            SELECT 1 FROM chat_session_tombstones
            WHERE session_id=%s AND kb_id=%s AND project_id=%s AND department=%s
              AND session_token_hash=%s
            """,
            (session_id, kb_id, project_id, department, token_hash),
        ).fetchone()
        if tombstone is not None:
            return None
        _ = conn.execute(
            """
            DELETE FROM chat_messages
            WHERE session_id=%s AND kb_id=%s AND project_id=%s AND department=%s
              AND generation_complete=FALSE
              AND created_at < now() - make_interval(secs => %s)
            """,
            (session_id, kb_id, project_id, department, GENERATION_LEASE_SECONDS),
        )
        owner = conn.execute(
            "SELECT session_token_hash FROM chat_messages WHERE session_id=%s AND kb_id=%s AND project_id=%s AND department=%s LIMIT 1",
            (session_id, kb_id, project_id, department),
        ).fetchone()
        if owner is not None and not hmac.compare_digest(cast(str, owner["session_token_hash"]), token_hash):
            return None
        active_generation = conn.execute(
            """
            SELECT 1 FROM chat_messages
            WHERE session_id=%s AND kb_id=%s AND project_id=%s AND department=%s
              AND session_token_hash=%s AND generation_complete=FALSE
            LIMIT 1
            """,
            (session_id, kb_id, project_id, department, token_hash),
        ).fetchone()
        if active_generation is not None:
            return None
        history = conn.execute(
            """
            SELECT role,content,created_at FROM (
                SELECT * FROM chat_messages
                WHERE session_id=%s AND kb_id=%s AND project_id=%s AND department=%s
                  AND session_token_hash=%s AND generation_complete=TRUE
                ORDER BY id DESC LIMIT %s
            ) m ORDER BY id
            """,
            (session_id, kb_id, project_id, department, token_hash, history_limit),
        ).fetchall()
        _ = conn.execute(
            """
            INSERT INTO chat_messages
            (session_id,role,content,created_at,kb_id,project_id,department,generation_id,session_token_hash)
            VALUES(%s,'user',%s,%s,%s,%s,%s,%s,%s)
            """,
            (session_id, content, now(), kb_id, project_id, department, generation_id, token_hash),
        )
        conn.commit()
        return history


def list_messages(
    session_id: str,
    limit: int | None = None,
    kb_id: str | None = None,
    project_id: str | None = None,
    department: str | None = None,
    session_token: str | None = None,
) -> list[DictRow]:
    with connect() as conn:
        scope_sql = " AND generation_complete=TRUE"
        params: list[object] = [session_id]
        if kb_id is not None and project_id is not None and department is not None:
            scope_sql += " AND kb_id=%s AND project_id=%s AND department=%s"
            params.extend([kb_id, project_id, department])
        if session_token is not None:
            scope_sql += " AND session_token_hash=%s"
            params.append(session_token_hash(session_token))
        if limit:
            params.append(limit)
            return conn.execute(
                f"SELECT role,content,created_at FROM (SELECT * FROM chat_messages WHERE session_id=%s{scope_sql} ORDER BY id DESC LIMIT %s) m ORDER BY id",
                params,
            ).fetchall()
        return conn.execute(
            f"SELECT role,content,created_at FROM chat_messages WHERE session_id=%s{scope_sql} ORDER BY id",
            params,
        ).fetchall()


def clear_session(
    session_id: str,
    kb_id: str | None = None,
    project_id: str | None = None,
    department: str | None = None,
    session_token: str | None = None,
) -> int:
    with connect() as conn:
        _ = conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (session_id,))
        token_hash: str | None = None
        if kb_id is not None and project_id is not None and department is not None and session_token is not None:
            token_hash = session_token_hash(session_token)
            owner = conn.execute(
                "SELECT session_token_hash FROM chat_messages WHERE session_id=%s AND kb_id=%s AND project_id=%s AND department=%s LIMIT 1",
                (session_id, kb_id, project_id, department),
            ).fetchone()
            if owner is not None and not hmac.compare_digest(cast(str, owner["session_token_hash"]), token_hash):
                conn.commit()
                return 0
            deleted = conn.execute(
                "DELETE FROM chat_messages WHERE session_id=%s AND kb_id=%s AND project_id=%s AND department=%s AND session_token_hash=%s",
                (session_id, kb_id, project_id, department, token_hash),
            ).rowcount
        else:
            deleted = conn.execute("DELETE FROM chat_messages WHERE session_id=%s", (session_id,)).rowcount
        if kb_id is not None and project_id is not None and department is not None and token_hash is not None:
            _ = conn.execute(
                """
                INSERT INTO chat_session_tombstones(session_id,kb_id,project_id,department,session_token_hash,cleared_at)
                VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT(session_id,kb_id,project_id,department,session_token_hash)
                DO UPDATE SET cleared_at=EXCLUDED.cleared_at
                """,
                (session_id, kb_id, project_id, department, token_hash, now()),
            )
        conn.commit()
        return deleted


def add_assistant_if_generation_active(
    session_id: str,
    generation_id: str,
    content: str,
    kb_id: str,
    project_id: str,
    department: str,
) -> bool:
    """Complete a reserved turn only if its user row is still active.

    This conditional insert closes the clear-vs-stream race: if another request
    deleted the pending generation while the LLM was streaming, no assistant
    row is inserted and the caller can report that the answer was not saved.
    """
    with connect() as conn:
        _ = conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (session_id,))
        inserted = conn.execute(
            """
            INSERT INTO chat_messages(
                session_id,role,content,created_at,kb_id,project_id,department,
                generation_id,session_token_hash,generation_complete
            )
            SELECT %s,'assistant',%s,%s,%s,%s,%s,%s,session_token_hash,FALSE
            FROM chat_messages
            WHERE session_id=%s AND generation_id=%s AND role='user'
              AND kb_id=%s AND project_id=%s AND department=%s
            LIMIT 1
            """,
            (
                session_id,
                content,
                now(),
                kb_id,
                project_id,
                department,
                generation_id,
                session_id,
                generation_id,
                kb_id,
                project_id,
                department,
            ),
        ).rowcount
        if inserted == 1:
            _ = conn.execute(
                """
                UPDATE chat_messages SET generation_complete=TRUE
                WHERE session_id=%s AND generation_id=%s
                  AND kb_id=%s AND project_id=%s AND department=%s
                """,
                (session_id, generation_id, kb_id, project_id, department),
            )
        conn.commit()
        return inserted == 1


def rollback_generation(session_id: str, generation_id: str) -> None:
    """Remove an unfinished turn after search, streaming, or client failure.

    Without this rollback, a user question whose assistant response never
    completed would remain in persistent history and be sent to the model as if
    it were a valid prior turn on the next request.
    """
    with connect() as conn:
        _ = conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (session_id,))
        _ = conn.execute(
            "DELETE FROM chat_messages WHERE session_id=%s AND generation_id=%s AND role='user'",
            (session_id, generation_id),
        )
        conn.commit()
