from __future__ import annotations

import math
import re
from collections.abc import Callable

Embedder = Callable[[list[str]], list[list[float]]]
STRATEGIES = {"fixed", "recursive", "semantic", "paragraph"}


def _validate(size: int, overlap: int) -> None:
    if size < 1:
        raise ValueError("chunk size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("chunk overlap must be non-negative and smaller than size")


def _fixed(text: str, size: int, overlap: int) -> list[str]:
    step = size - overlap
    return [text[start : start + size].strip() for start in range(0, len(text), step) if text[start : start + size].strip()]


def _recursive(text: str, size: int, overlap: int) -> list[str]:
    """Split from strong structural boundaries down to individual characters."""
    separators = ("\n\n", "\n", "。", "！", "？", ". ", " ", "")

    def split(part: str, level: int = 0) -> list[str]:
        if len(part) <= size:
            return [part.strip()] if part.strip() else []
        separator = separators[level]
        if not separator:
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
    overlapped = [chunks[0]]
    for index, chunk in enumerate(chunks[1:], 1):
        carry = min(overlap, max(0, size - len(chunk)))
        overlapped.append((chunks[index - 1][-carry:] + chunk).strip() if carry else chunk)
    return overlapped


def _sentences(text: str) -> list[str]:
    matches = re.finditer(r"[^。！？.!?]+[。！？.!?]*", text, re.DOTALL)
    return [match.group().strip() for match in matches if match.group().strip()]


def _cosine_distance(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(value * value for value in left) * sum(value * value for value in right))
    return 1 - dot / norm if norm else 1.0


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _semantic(text: str, size: int, embedder: Embedder, percentile: float) -> list[str]:
    sentences = _sentences(text)
    if len(sentences) < 2:
        return _recursive(text, size, 0)
    vectors = embedder(sentences)
    if len(vectors) != len(sentences):
        raise ValueError("embedder must return one vector per sentence")
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
    return [chunk for group in groups for chunk in _recursive(group, size, 0)]


def _paragraph(text: str, size: int, overlap: int) -> list[str]:
    """Keep Markdown headings with their section; repeat them when a section is split."""
    matches = list(re.finditer(r"(?m)^#{1,6}\s+.+$", text))
    if not matches:
        return _recursive(text, size, overlap)
    chunks: list[str] = []
    if text[: matches[0].start()].strip():
        chunks.extend(_recursive(text[: matches[0].start()], size, overlap))
    for index, match in enumerate(matches):
        heading = match.group().strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        section = f"{heading}\n\n{body}" if body else heading
        if len(section) <= size:
            chunks.append(section)
            continue
        body_size = max(1, size - len(heading) - 2)
        chunks.extend(f"{heading}\n\n{part}" for part in _recursive(body, body_size, min(overlap, body_size - 1)))
    return chunks


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
