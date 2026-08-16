"""命令行批量导入文档。

扫描指定目录下的文件, 解析后按所选策略切片并写入知识库。默认递归读取目录下
所有可解析的文件, 也可以用 --ext 只处理指定后缀的文件。批量导入是尽力而为的:
单个文件解析或写入失败会跳过并继续, 结束后打印「导入 / 跳过 / 失败」统计。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from fastapi import HTTPException

from rag import main as rag_main
from rag import store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import-documents",
        description="批量导入目录下的文档到企业知识库。",
    )
    parser.add_argument("directory", help="要扫描的目录，递归读取其中所有文件")
    parser.add_argument("--ext", default="", help="只处理指定后缀，逗号分隔（如 md,txt,pdf）；默认处理所有可解析文件")
    parser.add_argument("--kb-id", default="company", help="知识库 id（默认 company）")
    parser.add_argument("--project-id", default="default", help="项目 id（默认 default）")
    parser.add_argument("--department", default="general", help="部门范围（默认 general）")
    parser.add_argument(
        "--chunking-strategy",
        choices=["fixed", "recursive", "semantic", "paragraph"],
        default="recursive",
        help="切片策略（默认 recursive）",
    )
    return parser


def normalize_extensions(raw: str) -> set[str] | None:
    """把逗号分隔的后缀规范化为带点的小写集合; 空输入返回 None, 表示不过滤。"""
    values = [value.strip().lower() for value in raw.split(",") if value.strip()]
    if not values:
        return None
    return {"." + value.removeprefix(".") for value in values}


def collect_files(directory: Path, extensions: set[str] | None) -> list[Path]:
    """递归收集目录下的普通文件并排序; extensions 非空时只保留匹配后缀的文件。"""
    files = [path for path in directory.rglob("*") if path.is_file()]
    if extensions is not None:
        files = [path for path in files if path.suffix.lower() in extensions]
    return sorted(files)


def import_one(
    path: Path,
    *,
    kb_id: str,
    project_id: str,
    department: str,
    chunking_strategy: str,
) -> tuple[str, str]:
    """解析并写入单个文件, 返回 (状态, 说明); 状态为 imported、skipped 或 failed。"""
    if path.suffix.lower() not in rag_main.SUPPORTED_SUFFIXES:
        return "skipped", "不支持的文档格式"
    try:
        data = path.read_bytes()
        text, parser, pdf_type, pages_needing_ocr = rag_main.parse_document(path.name, data)
        chunks = rag_main.chunk_document(text, chunking_strategy)
        if not chunks:
            return "skipped", "没有可索引的文本"
        store.insert_document(
            kb_id=kb_id,
            project_id=project_id,
            document_id=uuid.uuid4().hex,
            filename=path.name,
            department=department,
            parser=parser,
            pdf_type=pdf_type,
            pages_needing_ocr=pages_needing_ocr,
            chunks=chunks,
            stored_path=str(path.resolve()),
            chunking_strategy=chunking_strategy,
        )
    except HTTPException as exc:
        # 后缀受支持但解析失败(损坏的 PDF/Office 文档), 计为失败而非跳过。
        return "failed", str(exc.detail)
    except Exception as exc:
        return "failed", f"{type(exc).__name__}: {exc}"
    return "imported", f"{len(chunks)} 个片段"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"目录不存在或不是目录：{directory}", file=sys.stderr)
        return 2

    try:
        store.init_db()
    except Exception as exc:
        print(f"初始化数据库失败：{exc}", file=sys.stderr)
        return 1
    if store.ensure_kb(args.kb_id) is None:
        print(f"知识库不存在：{args.kb_id}", file=sys.stderr)
        return 1
    project = store.ensure_project(args.kb_id, args.project_id)
    if project is None:
        print(f"项目范围不存在：{args.project_id}", file=sys.stderr)
        return 1
    resolved_project_id = str(project["id"])

    files = collect_files(directory, normalize_extensions(args.ext))
    if not files:
        print("目录下没有找到可导入的文件。")
        return 0

    imported = skipped = failed = 0
    for path in files:
        status, detail = import_one(
            path,
            kb_id=args.kb_id,
            project_id=resolved_project_id,
            department=args.department,
            chunking_strategy=args.chunking_strategy,
        )
        if status == "imported":
            imported += 1
        elif status == "skipped":
            skipped += 1
        else:
            failed += 1
        print(f"[{status}] {path.name} -> {detail}")

    print(f"完成：导入 {imported} 个，跳过 {skipped} 个，失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
