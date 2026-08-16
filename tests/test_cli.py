from fastapi import HTTPException

from rag import store
from rag.cli import collect_files, main, normalize_extensions


def test_normalize_extensions_parses_and_normalizes():
    assert normalize_extensions("md,.TXT,pdf") == {".md", ".txt", ".pdf"}
    assert normalize_extensions("") is None
    assert normalize_extensions("  ") is None


def test_collect_files_filters_by_extension(tmp_path):
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "c.pdf").write_text("c")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "d.md").write_text("d")

    assert [path.name for path in collect_files(tmp_path, None)] == ["a.md", "b.txt", "c.pdf", "d.md"]
    assert [path.name for path in collect_files(tmp_path, {".md"})] == ["a.md", "d.md"]


def _mock_scope(monkeypatch):
    monkeypatch.setattr(store, "init_db", lambda: None)
    monkeypatch.setattr(store, "ensure_kb", lambda kb_id: {"id": kb_id})
    monkeypatch.setattr(store, "ensure_project", lambda kb_id, project_id: {"id": "p-1"})


def test_main_imports_all_files_in_directory(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.md").write_text("部署步骤。")
    (tmp_path / "b.txt").write_text("重启前保存配置。")

    imported = []
    _mock_scope(monkeypatch)
    monkeypatch.setattr("rag.main.parse_document", lambda filename, data: (data.decode(), "plain-text", None, []))
    monkeypatch.setattr("rag.main.chunk_document", lambda text, strategy: [text])
    monkeypatch.setattr(store, "insert_document", lambda **kw: imported.append(kw) or {"id": kw["document_id"]})

    exit_code = main([str(tmp_path), "--project-id", "p-1"])

    assert exit_code == 0
    assert [kw["filename"] for kw in imported] == ["a.md", "b.txt"]
    assert [kw["project_id"] for kw in imported] == ["p-1", "p-1"]
    assert "完成：导入 2 个，跳过 0 个，失败 0 个" in capsys.readouterr().out


def test_main_limits_to_requested_extensions(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.txt").write_text("b")

    imported = []
    _mock_scope(monkeypatch)
    monkeypatch.setattr("rag.main.parse_document", lambda filename, data: (data.decode(), "plain-text", None, []))
    monkeypatch.setattr("rag.main.chunk_document", lambda text, strategy: [text])
    monkeypatch.setattr(store, "insert_document", lambda **kw: imported.append(kw["filename"]) or {"id": "x"})

    assert main([str(tmp_path), "--ext", "md"]) == 0
    assert imported == ["a.md"]


def test_main_skips_unsupported_files(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.png").write_bytes(b"not a document")

    _mock_scope(monkeypatch)

    assert main([str(tmp_path)]) == 0
    assert "完成：导入 0 个，跳过 1 个，失败 0 个" in capsys.readouterr().out


def test_main_counts_parse_failure_as_failed(tmp_path, monkeypatch, capsys):
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-corrupt")

    _mock_scope(monkeypatch)
    monkeypatch.setattr("rag.main.parse_document", lambda *_: (_ for _ in ()).throw(HTTPException(415, "文档解析失败：corrupt")))

    assert main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "完成：导入 0 个，跳过 0 个，失败 1 个" in out
    assert "[failed] broken.pdf" in out


def test_main_continues_after_file_failure(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.md").write_text("内容一")
    (tmp_path / "b.md").write_text("内容二")

    calls = {"count": 0}
    _mock_scope(monkeypatch)
    monkeypatch.setattr("rag.main.parse_document", lambda filename, data: (data.decode(), "plain-text", None, []))
    monkeypatch.setattr("rag.main.chunk_document", lambda text, strategy: [text])

    def insert(**kw):
        calls["count"] += 1
        if kw["filename"] == "a.md":
            raise RuntimeError("embedding unavailable")
        return {"id": kw["document_id"]}

    monkeypatch.setattr(store, "insert_document", insert)

    assert main([str(tmp_path)]) == 1
    assert calls["count"] == 2
    out = capsys.readouterr().out
    assert "完成：导入 1 个，跳过 0 个，失败 1 个" in out
    assert "[failed] a.md" in out


def test_main_fails_when_kb_missing(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.md").write_text("内容")
    monkeypatch.setattr(store, "init_db", lambda: None)
    monkeypatch.setattr(store, "ensure_kb", lambda kb_id: None)

    assert main([str(tmp_path)]) == 1
    assert "知识库不存在" in capsys.readouterr().err


def test_main_rejects_missing_directory(tmp_path, monkeypatch, capsys):
    assert main([str(tmp_path / "missing")]) == 2
    assert "目录不存在或不是目录" in capsys.readouterr().err
