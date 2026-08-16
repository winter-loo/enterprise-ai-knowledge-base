from __future__ import annotations

from functools import lru_cache

import tiktoken

# 与 rag/chunking.py 的 Chunk Tokenizer 保持一致: 项目选定的稳定、跨语言一致的
# 计数尺度是 o200k_base, 而不是某个具体模型的 tokenizer。这里用它来估算会话历史
# 占用的上下文 token, 从而决定何时压缩。
_ENCODING_NAME = "o200k_base"


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(_ENCODING_NAME)


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text))
