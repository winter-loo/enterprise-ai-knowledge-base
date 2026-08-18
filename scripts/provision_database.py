"""Provision the schema and RLS with the database owner, not a runtime service role."""

from __future__ import annotations

import os


def main() -> int:
    owner_url = os.getenv("DATABASE_OWNER_URL")
    if not owner_url:
        raise RuntimeError("DATABASE_OWNER_URL is required")
    os.environ["DATABASE_URL"] = owner_url
    os.environ["DATABASE_SCHEMA_PROVISIONING"] = "yes"

    from authz import store as authz_store
    from rag import store as rag_store
    from session import store as session_store

    authz_store.init_db()
    rag_store.init_db()
    session_store.init_db()
    print("Database schema and RLS policies provisioned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
