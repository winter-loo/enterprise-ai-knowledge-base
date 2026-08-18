import pytest

from shared.database_security import require_runtime_database_safety


class Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, *, bypass=False, missing=None, owned=None):
        self.bypass = bypass
        self.missing = missing or []
        self.owned = owned or []

    def execute(self, query, _params=None):
        text = str(query)
        if "rolbypassrls" in text:
            return Result({"rolbypassrls": self.bypass})
        if "EXCEPT SELECT relname" in text:
            return Result(rows=[{"name": name} for name in self.missing])
        return Result(rows=[{"relname": name} for name in self.owned])


def test_production_runtime_role_rejects_bypassrls(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(RuntimeError, match="BYPASSRLS"):
        require_runtime_database_safety(Connection(bypass=True), ("knowledge_evidence",))


def test_production_runtime_role_rejects_table_ownership(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(RuntimeError, match="must not own"):
        require_runtime_database_safety(Connection(owned=["knowledge_evidence"]), ("knowledge_evidence",))
