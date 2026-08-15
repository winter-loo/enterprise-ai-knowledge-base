from session import store


class QueryResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []


class StaleGenerationConnection:
    def __init__(self):
        self.stale_pending = True
        self.queries: list[str] = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, query, params=None):
        normalized = " ".join(str(query).split())
        self.queries.append(normalized)
        if normalized.startswith("DELETE FROM chat_messages") and "make_interval" in normalized:
            assert params[-1] == store.GENERATION_LEASE_SECONDS
            self.stale_pending = False
        if normalized.startswith("SELECT 1 FROM chat_messages") and "generation_complete=FALSE" in normalized:
            return QueryResult({"exists": 1} if self.stale_pending else None)
        return QueryResult()

    def commit(self):
        self.committed = True


def test_stale_generation_lease_is_reclaimed_before_active_check(monkeypatch):
    connection = StaleGenerationConnection()
    monkeypatch.setattr(store, "connect", lambda: connection)

    history = store.begin_generation(
        "session-1",
        "t" * 32,
        "retry after crash",
        "generation-2",
        "company",
        "p-1",
        "engineering",
    )

    assert history == []
    assert connection.committed is True
    cleanup_index = next(index for index, query in enumerate(connection.queries) if query.startswith("DELETE FROM chat_messages"))
    active_index = next(index for index, query in enumerate(connection.queries) if query.startswith("SELECT 1 FROM chat_messages"))
    assert cleanup_index < active_index
