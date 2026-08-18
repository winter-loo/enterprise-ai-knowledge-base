"""Explicitly reset development-only authorization, knowledge, and session data."""

from __future__ import annotations

import os

import psycopg


def require_development_confirmation() -> None:
    if os.getenv("APP_ENV") != "development":
        raise RuntimeError("APP_ENV must be development")
    if os.getenv("CONFIRM_RESET_DEV_DATA") != "yes":
        raise RuntimeError("CONFIRM_RESET_DEV_DATA must be yes")
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is required")


def main() -> int:
    require_development_confirmation()
    database_url = os.environ["DATABASE_URL"]
    with psycopg.connect(database_url) as conn:
        _ = conn.execute(
            """
            DROP TABLE IF EXISTS chat_session_summaries CASCADE;
            DROP TABLE IF EXISTS chat_messages CASCADE;
            DROP TABLE IF EXISTS chat_session_tombstones CASCADE;
            DROP TABLE IF EXISTS chat_sessions CASCADE;
            DROP TABLE IF EXISTS knowledge_evidence CASCADE;
            DROP TABLE IF EXISTS projects CASCADE;
            DROP TABLE IF EXISTS knowledge_bases CASCADE;
            DROP TABLE IF EXISTS project_grants CASCADE;
            DROP TABLE IF EXISTS platform_administrators CASCADE;
            DROP TABLE IF EXISTS grants CASCADE;
            DROP TABLE IF EXISTS roles CASCADE;
            DROP TABLE IF EXISTS users CASCADE;
            DROP TABLE IF EXISTS departments CASCADE;
            """
        )
        conn.commit()
    print("Development knowledge, authorization, and session data has been reset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
