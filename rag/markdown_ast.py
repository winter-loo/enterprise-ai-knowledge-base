"""把 Multimark AST 转换成项目内部稳定的 Markdown 块。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, cast

from multimark import parse as _untyped_parse  # pyright: ignore[reportMissingTypeStubs]

GFM_EXTENSIONS = ("table", "strikethrough", "autolink", "tasklist", "tagfilter")


class _MultimarkNode(Protocol):
    """描述本项目实际使用的 Multimark 节点接口, 隔离未提供 py.typed 的第三方库。"""

    @property
    def type_string(self) -> str: ...

    @property
    def start_line(self) -> int: ...

    @property
    def end_line(self) -> int: ...

    @property
    def heading_level(self) -> int: ...

    @property
    def children(self) -> Iterable[_MultimarkNode]: ...


_parse_markdown = cast(Callable[..., _MultimarkNode], _untyped_parse)


@dataclass(frozen=True)
class MarkdownBlock:
    """一个可映射回原文的顶层 Markdown 语法块。"""

    kind: str
    text: str
    start_line: int
    end_line: int
    heading_level: int | None = None


def _line_start_offsets(text: str) -> list[int]:
    """记录每一行在原文中的起点, 用 AST 行号安全地切回 Python 字符串。"""
    offsets = [0]
    offsets.extend(index + 1 for index, character in enumerate(text) if character == "\n")
    return offsets


def parse_markdown_blocks(text: str) -> list[MarkdownBlock]:
    """解析顶层 Markdown 块, 保留原文文本、行范围和标题层级。"""
    if not text:
        return []

    root = _parse_markdown(text, extensions=GFM_EXTENSIONS)
    nodes = [node for node in root.children if node.start_line > 0 and node.end_line > 0]
    if not nodes:
        return []

    line_offsets = _line_start_offsets(text)
    blocks: list[MarkdownBlock] = []
    cursor_offset = 0
    cursor_line = 1
    for node in nodes:
        start_line = node.start_line
        start_offset = line_offsets[start_line - 1]
        end_line = max(start_line, min(node.end_line, len(line_offsets)))
        end_offset = line_offsets[end_line] if end_line < len(line_offsets) else len(text)

        # 链接引用定义等语法不会生成 AST 节点, 但仍必须作为原文间隙保留下来。
        if start_offset > cursor_offset:
            blocks.append(
                MarkdownBlock(
                    kind="source",
                    text=text[cursor_offset:start_offset],
                    start_line=cursor_line,
                    end_line=start_line - 1,
                )
            )

        blocks.append(
            MarkdownBlock(
                kind=node.type_string,
                text=text[start_offset:end_offset],
                start_line=start_line,
                end_line=end_line,
                heading_level=node.heading_level if node.type_string == "heading" else None,
            )
        )
        cursor_offset = end_offset
        cursor_line = end_line + 1

    if cursor_offset < len(text):
        blocks.append(
            MarkdownBlock(
                kind="source",
                text=text[cursor_offset:],
                start_line=cursor_line,
                end_line=len(line_offsets),
            )
        )
    return blocks
