"""文本切片策略的大白话说明。

fixed 是拿一把固定长度的尺子从头量到尾。每次切一样长, 并和上一片重复一点内容。
它简单、稳定, 但可能从一句话中间切开。

recursive 是先找自然的停顿位置。它先尝试按段落切, 不行再按换行、句号和空格切,
实在找不到合适位置才按固定长度硬切。它尽量保住完整段落和句子, 所以是默认策略。
重叠在顶层统一处理: 正文先按 size - overlap 的目标切分, 再给后续片段前置上一片末尾,
拼接后重新计数; 如果超出 size, 就缩短重叠而不删减正文。

semantic 是先把文本拆成句子, 再比较相邻句子说的是不是同一件事。话题变化明显时就另起一片,
最后再把仍然过长的片段切小。它更关注内容含义, 但需要额外调用向量模型, 因此更慢、更贵。
为避免语义完全连贯的文档被强行切开, 只有距离既明显高于本文档常态、又超过一个绝对下限时,
才认定为真正的主题切换。embedding 会分批调用, 避免超长文档一次请求过大。

paragraph 专门照顾 Markdown 文档。它把标题和标题下面的正文看成一个章节; 章节太长时,
每个切出来的片段都会重复标题, 让单独检索到的片段仍然知道自己属于哪个章节。

size 与 overlap 的单位都是 token, 用项目选定的 o200k_base 词表统计。
它和 Qwen3 的原生 tokenizer 不是同一份词表(见 docs/qwen3-tokenizer-research.md),
只是同为 byte-level BPE; 这里只用它作稳定、跨语言一致的检索分块尺度, 而非模型原生 token 硬上限。
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from functools import lru_cache

import tiktoken

from rag.markdown_ast import MarkdownBlock, parse_markdown_blocks

Embedder = Callable[[list[str]], list[list[float]]]
STRATEGIES = {"fixed", "recursive", "semantic", "paragraph"}

# 项目选定的 Chunk Tokenizer: tiktoken 的 o200k_base。
# 注意它不是 Qwen3 的原生 tokenizer(Qwen3 用 Qwen2Tokenizer, 词表约 151k,
# 与 o200k_base 的 200k 词表不同, 见 docs/qwen3-tokenizer-research.md)。
# 这里只用它作稳定、跨语言一致的检索分块尺度; 若 size 需要是"模型实际接收的
# token 硬上限", 应改用随 embedding 模型配置的真实 tokenizer 做最终校验。
# 首次加载会下载约 4MB 词表到临时目录并缓存; 可用 TIKTOKEN_CACHE_DIR 指定持久位置。
_ENCODING_NAME = "o200k_base"


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(_ENCODING_NAME)


def _token_count(text: str) -> int:
    """返回 text 按项目选定 o200k_base 词表统计的 token 数。"""
    return len(_encoding().encode(text))


def _token_tail(text: str, token_budget: int) -> str:
    """返回 text 末尾至少 token_budget 个 token, 并避免切开 Unicode 字符。"""
    encoding = _encoding()
    tokens = encoding.encode(text)
    if len(tokens) <= token_budget:
        return text
    safe_boundaries = _safe_token_boundaries(encoding, tokens)
    start_limit = len(tokens) - token_budget
    start = safe_boundaries[bisect_right(safe_boundaries, start_limit) - 1]
    return encoding.decode(tokens[start:])


def _prepend_overlap_within_size(previous: str, current: str, overlap: int, size: int) -> str:
    """给 current 前置尽量接近 overlap 的安全后缀, 必要时缩短后缀以满足 size。"""
    encoding = _encoding()
    overlap_tokens = encoding.encode(_token_tail(previous, overlap))
    safe_boundaries = _safe_token_boundaries(encoding, overlap_tokens)
    for start in safe_boundaries:
        candidate = encoding.decode(overlap_tokens[start:]) + current
        # BPE 会跨字符串边界重新分词, 因此必须以拼接后的实际计数为准。
        if _token_count(candidate) <= size:
            return candidate
    raise ValueError("content chunk exceeds final chunk size")


def _safe_token_boundaries(encoding: tiktoken.Encoding, tokens: list[int]) -> list[int]:
    """返回不会从 UTF-8 code point 中间切开的 token 索引。"""
    token_bytes = [encoding.decode_single_token_bytes(token) for token in tokens]
    encoded = b"".join(token_bytes)
    boundaries = [0]
    offset = 0
    for index, value in enumerate(token_bytes, 1):
        offset += len(value)
        if offset == len(encoded) or encoded[offset] & 0b1100_0000 != 0b1000_0000:
            boundaries.append(index)
    return boundaries


# semantic 切分的两个参数:
# 分批调用 embedding, 避免长文档一次请求过大导致超时或超过服务端上限。
_EMBED_BATCH_SIZE = 64
# 相邻句余弦距离的绝对下限; 低于它的一律不视为主题切换, 防止同质文档被分布分位点强行切开。
_MIN_SEMANTIC_DISTANCE = 0.3

# 分句分成两步: 先找标点候选边界, 再判断英文句点是不是缩写的一部分。
# closers 是应归到前一句的闭合符号。例如 `"Stop."` 和带 CJK 闭合括号的句子都要把引号/括号
# 留在句尾, 而不是错误地放到下一句开头。
_SENTENCE_CLOSERS = r""""'”’\)\]\}）】》」』"""
_SENTENCE_BOUNDARY_PATTERN = re.compile(rf"[。！？]+[{_SENTENCE_CLOSERS}]*|[!?]+[{_SENTENCE_CLOSERS}]*|\.[{_SENTENCE_CLOSERS}]*(?=\s|$)")

# 强非句末缩写: 当前策略遇到这些句点时一律继续当前句。
# - Mr./Mrs./Ms.: 英文姓名前的先生、夫人/已婚女士、女士称谓; `Mistress` 只是 Mrs. 的历史词源,
#   Ms. 则是独立称谓, 不展开为另一个完整单词。
# - Dr./Prof.: Doctor/Professor, 姓名前的博士、医生或教授称谓。
# - Sr./Jr.: Senior/Junior, 用于区分同名家族成员或不同世代的姓名后缀。
# - vs.: versus, 表示“对、与……相比”, 如 `PostgreSQL vs. MySQL`。
# - e.g./i.e.: 拉丁语 exempli gratia/id est, 分别表示“例如”和“也就是说”。
# 第一条正则分支匹配单词缩写, 第二条匹配 e.g./i.e.; `\b` 防止从单词中间命中,
# `\.` 匹配字面句点, `$` 要求缩写恰好结束在当前候选句点, IGNORECASE 忽略大小写。
# 这是防止过度切分的保守启发式: Sr./Jr./vs. 偶尔也可能位于真正的句末。
_NON_TERMINAL_ABBREVIATION_PATTERN = re.compile(
    r"(?:\b(?:mr|mrs|ms|dr|prof|sr|jr|vs)\.|\b(?:e\.g|i\.e)\.)$",
    re.IGNORECASE,
)

# 上下文相关缩写: 仅凭左侧形状不能决定是否切句, 还要查看右侧文本。
# - `\betc\.` 单独匹配 etc.。
# - `(?:\b[a-z]{1,3}\.){2,}` 匹配至少两个“1~3 个字母 + 句点”的段,
#   因而覆盖 U.S.、Ph.D. 等形式 (e.g./i.e. 也符合, 但会先被上面的强规则处理)。
_CONTEXTUAL_ABBREVIATION_PATTERN = re.compile(
    r"(?:\betc\.|(?:\b[a-z]{1,3}\.){2,})$",
    re.IGNORECASE,
)


def _validate(size: int, overlap: int) -> None:
    if size < 1:
        raise ValueError("chunk size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("chunk overlap must be non-negative and smaller than size")


def _fixed(text: str, size: int, overlap: int) -> list[str]:
    """按固定 token 窗口滑动切分, 相邻窗口至少共享 overlap 个 token。"""
    if _token_count(text) <= size:
        return [text] if text.strip() else []
    encoding = _encoding()
    tokens = encoding.encode(text)
    safe_boundaries = _safe_token_boundaries(encoding, tokens)
    windows: list[tuple[int, int]] = []
    start = 0
    while start < len(tokens):
        end_limit = min(start + size, len(tokens))
        end = safe_boundaries[bisect_right(safe_boundaries, end_limit) - 1]
        if end <= start:
            raise ValueError("chunk size is too small to contain one Unicode character")
        windows.append((start, end))
        if end == len(tokens):
            break
        start_limit = end - overlap
        next_start = safe_boundaries[bisect_right(safe_boundaries, start_limit) - 1]
        if next_start <= start:
            next_start = safe_boundaries[bisect_right(safe_boundaries, start)]
        start = next_start
    # 尾片过短时重新平衡最后两个窗口, 避免无信息碎片, 同时保持 overlap 和 size 硬上限。
    min_chunk = max(1, size // 10)
    if len(windows) >= 2 and windows[-1][1] - windows[-2][1] < min_chunk:
        span_start = windows[-2][0]
        candidates: list[tuple[tuple[int, int, int], int, int]] = []
        for left_end in safe_boundaries:
            if left_end <= span_start or left_end - span_start > size:
                continue
            right_limit = max(span_start, left_end - overlap)
            right_start = safe_boundaries[bisect_left(safe_boundaries, right_limit)]
            left_size = left_end - span_start
            right_size = len(tokens) - right_start
            if 0 < right_size <= size:
                actual_overlap = left_end - right_start
                score = (min(left_size, right_size), actual_overlap, -abs(left_size - right_size))
                candidates.append((score, left_end, right_start))
        if candidates:
            _, left_end, right_start = max(candidates)
            windows[-2:] = [(span_start, left_end), (right_start, len(tokens))]
    chunks = [encoding.decode(tokens[start:end]) for start, end in windows]
    return chunks


def _recursive(text: str, size: int, overlap: int) -> list[str]:
    """优先沿文档结构切分; 结构边界不足时逐级退化, 最后才使用固定窗口。"""
    # 分隔符从强结构到弱结构排列, 使段落和完整句子尽量留在同一个 chunk 中。
    separators = ("\n\n", "\n", "。", "！", "？", "；", "：", ". ", "! ", "? ", " ", "")

    # 重叠在顶层统一处理: 正文先按 size - overlap 的目标切分, 后续片段再前置上一片末尾。
    # 拼接后重新计数; 如果超限就缩短重叠, size 是硬上限而 overlap 是尽力目标。
    budget = size - overlap if overlap else size

    def split(part: str, level: int = 0) -> list[str]:
        if _token_count(part) <= budget:
            return [part.strip()] if part.strip() else []
        separator = separators[level]
        if not separator:
            # 连空格都无法安全切开时, 固定窗口保证任何超长文本最终都能满足 budget 限制。
            try:
                return _fixed(part, budget, 0)
            except ValueError:
                return _fixed(part, size, 0)
        pieces = part.split(separator)
        if len(pieces) == 1:
            return split(part, level + 1)
        units = [piece + (separator if index < len(pieces) - 1 else "") for index, piece in enumerate(pieces)]
        chunks: list[str] = []
        current = ""
        for unit in units:
            if _token_count(unit) > budget:
                if current.strip():
                    chunks.append(current.strip())
                    current = ""
                chunks.extend(split(unit, level + 1))
            elif current and _token_count(current + unit) > budget:
                if current.strip():
                    chunks.append(current.strip())
                current = unit
            else:
                current += unit
        if current.strip():
            chunks.append(current.strip())
        return chunks

    def fit_content(chunk: str) -> list[str]:
        """优先按正文预算切分; 单个 Unicode 字符放不下时退回最终 size。"""
        if _token_count(chunk) <= budget:
            return [chunk]
        try:
            return _fixed(chunk, budget, 0)
        except ValueError:
            return _fixed(chunk, size, 0)

    # strip 或自然边界重组可能改变 BPE 结果; 对正文片段重新计数并收紧到 budget。
    content = [piece for chunk in split(text) for piece in fit_content(chunk)]
    if not overlap or len(content) < 2:
        return content
    overlapped = [content[0]]
    for index, chunk in enumerate(content[1:], 1):
        overlapped.append(_prepend_overlap_within_size(content[index - 1], chunk, overlap, size))
    return overlapped


def _sentences(text: str) -> list[str]:
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY_PATTERN.finditer(text):
        if match.group().startswith("."):
            period_end = match.start() + 1
            if _NON_TERMINAL_ABBREVIATION_PATTERN.search(text, 0, period_end):
                continue
            if _CONTEXTUAL_ABBREVIATION_PATTERN.search(text, 0, period_end):
                # 小写后继通常仍属于当前句, 例如 `Ph.D. candidate`。
                # 大写后继存在歧义: `The U.S. Army` 是句中, `He lives in the U.S. Next...` 是句末。
                # 当前句前缀恰好有两个空白分词 (如 `The U.S.`、`A Ph.D.`) 时继续当前句;
                # 一个或至少三个分词时允许切句。这只是轻量启发式, 不是完整 NLP 分句器。
                next_non_space = re.search(r"\S", text[match.end() :])
                prefix_word_count = len(text[start:period_end].split())
                if next_non_space is not None and (next_non_space.group().islower() or prefix_word_count == 2):
                    continue
        sentence = text[start : match.end()].strip()
        if sentence:
            sentences.append(sentence)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


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


def _embed_batches(embedder: Embedder, sentences: list[str]) -> list[list[float]]:
    """分批调用 embedder, 避免超长文档一次请求过大导致超时或超过服务端上限。"""
    vectors: list[list[float]] = []
    for start in range(0, len(sentences), _EMBED_BATCH_SIZE):
        vectors.extend(embedder(sentences[start : start + _EMBED_BATCH_SIZE]))
    return vectors


def _semantic(text: str, size: int, embedder: Embedder, percentile: float) -> list[str]:
    """按相邻句子的语义变化分组, 再对超长语义组执行无重叠的递归切分。"""
    sentences = _sentences(text)
    if len(sentences) < 2:
        return _recursive(text, size, 0)
    vectors = _embed_batches(embedder, sentences)
    if len(vectors) != len(sentences):
        raise ValueError("embedder must return one vector per sentence")
    # 只比较相邻句: 明显高于本文档常态的距离被视为潜在主题边界。
    distances = [_cosine_distance(vectors[index], vectors[index + 1]) for index in range(len(vectors) - 1)]
    # 阈值取「本文档距离分布分位点」与「绝对下限」的较大者:
    # 只有距离既明显高于本文档常态、又本身足够大, 才认定为真正的主题切换。
    threshold = max(_percentile(distances, percentile), _MIN_SEMANTIC_DISTANCE)
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
        if _token_count(section) <= size:
            chunks.append(section)
            continue
        # 标题自身已占满 size 时无法在每个子片段里重复标题, 直接回退到通用递归切分。
        overhead = _token_count(f"{heading}\n\n")
        if overhead >= size:
            chunks.extend(_recursive(section, size, overlap))
            continue
        # 先为标题预留 token 空间, 再切正文, 确保每个子片段都能重新附带相同标题。
        body_size = size - overhead
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
