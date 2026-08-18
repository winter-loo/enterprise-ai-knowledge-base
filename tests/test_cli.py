from pathlib import Path

from rag import cli, store


def test_cli_requires_an_explicit_project_and_principal():
    parser = cli.build_parser()
    args = parser.parse_args(["/tmp/docs", "--project-id", "project-1"])

    assert args.project_id == "project-1"
    assert args.principal_id == "admin"
    assert not hasattr(args, "access_scope")


def test_import_one_records_the_calling_principal(monkeypatch, tmp_path):
    path = Path(tmp_path) / "guide.md"
    path.write_text("内容", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(cli.rag_main, "parse_document", lambda *_args: ("内容", "plain-text", None, []))
    monkeypatch.setattr(cli.rag_main, "chunk_document", lambda *_args: ["内容"])
    monkeypatch.setattr(store, "insert_document", lambda **kwargs: captured.update(kwargs) or {})

    status, _detail = cli.import_one(
        path,
        project_id="project-1",
        principal_id="alice",
        chunking_strategy="recursive",
    )

    assert status == "imported"
    assert captured["principal_id"] == "alice"
    assert "access_scope" not in captured
