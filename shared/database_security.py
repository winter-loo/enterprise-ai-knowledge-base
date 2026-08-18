"""Shared PostgreSQL Principal context and production schema safeguards."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any, cast

PRINCIPAL_SETTING = "app.principal_id"


def schema_provisioning_enabled() -> bool:
    """Allow DDL only for local development or an explicit owner-run command."""
    return os.getenv("APP_ENV", "development") == "development" or os.getenv("DATABASE_SCHEMA_PROVISIONING") == "yes"


def require_runtime_database_safety(conn: Any, tables: Iterable[str]) -> None:
    """Fail production startup if the service connection could bypass RLS."""
    if schema_provisioning_enabled():
        return
    bypass = cast(dict[str, object] | None, conn.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone())
    if bypass is None or bool(bypass["rolbypassrls"]):
        raise RuntimeError("DATABASE_URL role must not have BYPASSRLS")
    table_names = list(tables)
    missing = cast(
        list[dict[str, object]],
        conn.execute("SELECT unnest(%s::text[]) AS name EXCEPT SELECT relname FROM pg_class WHERE relkind='r'", (table_names,)).fetchall(),
    )
    if missing:
        raise RuntimeError("database schema is missing; run scripts/provision_database.py with DATABASE_OWNER_URL")
    owned = cast(
        list[dict[str, object]],
        conn.execute(
            "SELECT relname FROM pg_class WHERE relkind='r' AND relname = ANY(%s) AND relowner = (SELECT oid FROM pg_roles WHERE rolname=current_user)",
            (table_names,),
        ).fetchall(),
    )
    if owned:
        names = ", ".join(str(row["relname"]) for row in owned)
        raise RuntimeError(f"DATABASE_URL role must not own business tables: {names}")
