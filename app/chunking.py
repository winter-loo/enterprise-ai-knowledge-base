"""文本切片策略的大白话说明。

fixed 是拿一把固定长度的尺子从头量到尾。每次切一样长, 并和上一片重复一点内容。
它简单、稳定, 但可能从一句话中间切开。

recursive 是先找自然的停顿位置。它先尝试按段落切, 不行再按换行、句号和空格切,
实在找不到合适位置才按固定长度硬切。它尽量保住完整段落和句子, 所以是默认策略。

semantic 是先把文本拆成句子, 再比较相邻句子说的是不是同一件事。话题变化明显时就另起一片,
最后再把仍然过长的片段切小。它更关注内容含义, 但需要额外调用向量模型, 因此更慢、更贵。

paragraph 专门照顾 Markdown 文档。它把标题和标题下面的正文看成一个章节; 章节太长时,
每个切出来的片段都会重复标题, 让单独检索到的片段仍然知道自己属于哪个章节。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

from app.markdown_ast import MarkdownBlock, parse_markdown_blocks

Embedder = Callable[[list[str]], list[list[float]]]
STRATEGIES = {"fixed", "recursive", "semantic", "paragraph"}


def _validate(size: int, overlap: int) -> None:
    if size < 1:
        raise ValueError("chunk size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("chunk overlap must be non-negative and smaller than size")


def _fixed(text: str, size: int, overlap: int) -> list[str]:
    """按固定字符窗口滑动切分, 相邻窗口共享 overlap 字符以减少边界信息丢失。"""
    step = size - overlap
    return [text[start : start + size].strip() for start in range(0, len(text), step) if text[start : start + size].strip()]


def _recursive(text: str, size: int, overlap: int) -> list[str]:
    """优先沿文档结构切分; 结构边界不足时逐级退化, 最后才使用固定窗口。"""
    # 分隔符从强结构到弱结构排列, 使段落和完整句子尽量留在同一个 chunk 中。
    separators = ("\n\n", "\n", "。", "！", "？", ". ", " ", "")

    def split(part: str, level: int = 0) -> list[str]:
        if len(part) <= size:
            return [part.strip()] if part.strip() else []
        separator = separators[level]
        if not separator:
            # 连空格都无法安全切开时, 固定窗口保证任何超长文本最终都能满足 size 限制。
            return _fixed(part, size, overlap)
        pieces = part.split(separator)
        if len(pieces) == 1:
            return split(part, level + 1)
        units = [piece + (separator if index < len(pieces) - 1 else "") for index, piece in enumerate(pieces)]
        chunks: list[str] = []
        current = ""
        for unit in units:
            if len(unit) > size:
                if current.strip():
                    chunks.append(current.strip())
                    current = ""
                chunks.extend(split(unit, level + 1))
            elif current and len(current) + len(unit) > size:
                chunks.append(current.strip())
                current = unit
            else:
                current += unit
        if current.strip():
            chunks.append(current.strip())
        return chunks

    chunks = split(text)
    if not overlap or len(chunks) < 2:
        return chunks
    # 将前一片末尾带入后一片, 同时缩短 carry, 保证补重叠后仍不超过 size。
    overlapped = [chunks[0]]
    for index, chunk in enumerate(chunks[1:], 1):
        carry = min(overlap, max(0, size - len(chunk)))
        overlapped.append((chunks[index - 1][-carry:] + chunk).strip() if carry else chunk)
    return overlapped


def _sentences(text: str) -> list[str]:
    matches = re.finditer(r"[^。！？.!?]+[。！？.!?]*", text, re.DOTALL)
    return [match.group().strip() for match in matches if match.group().strip()]


def _cosine_distance(left: list[float], right: list[float]) -> float:
    """衡量两个句向量的语义差异; 距离越大, 越可能出现主题切换。"""
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(value * value for value in left) * sum(value * value for value in right))
    return 1 - dot / norm if norm else 1.0


def _percentile(values: list[float], percentile: float) -> float:
    """从当前文档的距离分布中选择动态阈值, 用来识别变化最显著的句子边界。"""
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _semantic(text: str, size: int, embedder: Embedder, percentile: float) -> list[str]:
    """按相邻句子的语义变化分组, 再对超长语义组执行无重叠的递归切分。"""
    sentences = _sentences(text)
    if len(sentences) < 2:
        return _recursive(text, size, 0)
    vectors = embedder(sentences)
    if len(vectors) != len(sentences):
        raise ValueError("embedder must return one vector per sentence")
    # 只比较相邻句: 明显高于本文档常态的距离被视为潜在主题边界。
    distances = [_cosine_distance(vectors[index], vectors[index + 1]) for index in range(len(vectors) - 1)]
    threshold = _percentile(distances, percentile)
    groups: list[str] = []
    current = sentences[0]
    for index, sentence in enumerate(sentences[1:]):
        if distances[index] >= threshold and current:
            groups.append(current)
            current = sentence
        else:
            current += sentence
    groups.append(current)
    # 语义边界决定分组, size 仍是硬限制; 这里不加 overlap, 避免重新跨越主题边界。
    return [chunk for group in groups for chunk in _recursive(group, size, 0)]


def _paragraph(text: str, size: int, overlap: int) -> list[str]:
    """用 Markdown AST 标题组织章节; 章节过长时重复标题, 让每个子片段保留所属上下文。"""
    blocks = parse_markdown_blocks(text)
    heading_indexes = [index for index, block in enumerate(blocks) if block.heading_level is not None]
    if not heading_indexes:
        # 普通文本没有标题层级可利用, 回退到通用递归策略。
        return _recursive(text, size, overlap)
    chunks: list[str] = []
    if heading_indexes[0] > 0:
        preamble = _join_markdown_blocks(blocks[: heading_indexes[0]])
        chunks.extend(_recursive(preamble, size, overlap))
    for index, heading_index in enumerate(heading_indexes):
        end_index = heading_indexes[index + 1] if index + 1 < len(heading_indexes) else len(blocks)
        heading = blocks[heading_index].text.rstrip("\r\n")
        body = _join_markdown_blocks(blocks[heading_index + 1 : end_index])
        section = f"{heading}\n\n{body}" if body else heading
        if len(section) <= size:
            chunks.append(section)
            continue
        # 先为标题预留空间, 再切正文, 确保每个子片段都能重新附带相同标题。
        body_size = max(1, size - len(heading) - 2)
        chunks.extend(f"{heading}\n\n{part}" for part in _recursive(body, body_size, min(overlap, body_size - 1)))
    return chunks


def _join_markdown_blocks(blocks: list[MarkdownBlock]) -> str:
    """按 source span 原样连接 AST 节点及节点间没有 AST 表示的原文。"""
    return "".join(block.text for block in blocks if block.text).strip("\r\n")


def chunk_text(
    text: str,
    *,
    strategy: str = "recursive",
    size: int = 700,
    overlap: int = 100,
    embedder: Embedder | None = None,
    semantic_percentile: float = 95,
) -> list[str]:
    """LightRAG-inspired fixed, recursive, semantic-vector and paragraph strategies."""
    _validate(size, overlap)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown chunking strategy: {strategy}")
    if strategy == "fixed":
        return _fixed(text, size, overlap)
    if strategy == "recursive":
        return _recursive(text, size, overlap)
    if strategy == "paragraph":
        return _paragraph(text, size, overlap)
    if embedder is None:
        raise ValueError("semantic chunking requires an embedder")
    if not 0 <= semantic_percentile <= 100:
        raise ValueError("semantic_percentile must be between 0 and 100")
    return _semantic(text, size, embedder, semantic_percentile)
