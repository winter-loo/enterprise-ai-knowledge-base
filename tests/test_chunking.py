from itertools import pairwise

import pytest

from app.chunking import _encoding, _sentences, _token_count, _token_tail, chunk_text


def _first_tokens(text: str, count: int) -> str:
    """返回 text 开头 count 个 token 组成的字符串。"""
    return _encoding().decode(_encoding().encode(text)[:count])


def test_fixed_strategy_keeps_overlap():
    text = "零一二三四五六七八九" * 30
    chunks = chunk_text(text, strategy="fixed", size=100, overlap=20)
    assert len(chunks) >= 2
    assert all(_token_count(chunk) <= 100 for chunk in chunks)
    assert all(_token_tail(left, 20) == _first_tokens(right, 20) for left, right in pairwise(chunks))


def test_recursive_strategy_prefers_paragraph_boundaries():
    text = "第一段。\n\n第二段很长。\n\n第三段。"
    chunks = chunk_text(text, strategy="recursive", size=6, overlap=0)
    assert chunks == ["第一段。", "第二段很长。", "第三段。"]


def test_recursive_overlap_never_exceeds_chunk_size():
    chunks = chunk_text("第一段很长。\n\n第二段也很长。", strategy="recursive", size=8, overlap=3)
    assert all(_token_count(chunk) <= 8 for chunk in chunks)


def test_semantic_strategy_breaks_at_topic_shift():
    vectors = {
        "猫喜欢晒太阳。": [1.0, 0.0],
        "小猫喜欢纸箱。": [0.9, 0.1],
        "数据库使用索引。": [0.0, 1.0],
        "查询需要执行计划。": [0.1, 0.9],
    }
    chunks = chunk_text(
        "猫喜欢晒太阳。小猫喜欢纸箱。数据库使用索引。查询需要执行计划。",
        strategy="semantic",
        size=100,
        overlap=0,
        embedder=lambda texts: [vectors[text] for text in texts],
        semantic_percentile=80,
    )
    assert chunks == ["猫喜欢晒太阳。小猫喜欢纸箱。", "数据库使用索引。查询需要执行计划。"]


def test_paragraph_strategy_repeats_heading_for_oversized_section():
    text = "# 安装\n\n" + "步骤。" * 12 + "\n\n# 运维\n\n检查日志。"
    chunks = chunk_text(text, strategy="paragraph", size=24, overlap=0)
    assert len(chunks) >= 3
    assert chunks[0].startswith("# 安装")
    assert chunks[1].startswith("# 安装")
    assert chunks[-1] == "# 运维\n\n检查日志。"


def test_paragraph_strategy_recognizes_setext_heading():
    text = "安装指南\n========\n\n" + "步骤。" * 12

    chunks = chunk_text(text, strategy="paragraph", size=24, overlap=0)

    assert len(chunks) >= 2
    assert all(chunk.startswith("安装指南\n========") for chunk in chunks)


def test_paragraph_strategy_ignores_heading_syntax_inside_code_fence():
    text = "# 示例\n\n```markdown\n# 这不是标题\n```\n\n结束。"

    chunks = chunk_text(text, strategy="paragraph", size=100, overlap=0)

    assert chunks == [text]


def test_paragraph_strategy_preserves_indented_code_block():
    text = "# 示例\n\n    print('保留缩进')"

    chunks = chunk_text(text, strategy="paragraph", size=100, overlap=0)

    assert chunks == [text]


def test_paragraph_strategy_does_not_treat_link_definition_as_heading():
    text = "# API\n\n[文档]: https://example.com/reference\n\n" + "正文。" * 8

    chunks = chunk_text(text, strategy="paragraph", size=48, overlap=0)

    assert all(_token_count(chunk) <= 48 for chunk in chunks)
    assert any("[文档]: https://example.com/reference" in chunk for chunk in chunks)


def test_paragraph_strategy_preserves_spacing_between_body_blocks():
    text = "# 示例\n正文\n- 条目"

    chunks = chunk_text(text, strategy="paragraph", size=100, overlap=0)

    assert chunks == ["# 示例\n\n正文\n- 条目"]


def test_fixed_rebalances_tiny_trailing_fragment():
    # "知识库" 每个 2 token; 51 个共 102 token, size=100、overlap=0 会产生 [100, 2] 尾片,
    # 2 < size//10, 应重新平衡最后两片, 避免小尾片且不突破 size 硬上限。
    text = "知识库" * 51
    chunks = chunk_text(text, strategy="fixed", size=100, overlap=0)
    assert len(chunks) == 2
    assert all(10 <= _token_count(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks) == text


def test_fixed_rebalances_tiny_unique_tail_when_overlap_is_present():
    chunks = chunk_text("知识库" * 91, strategy="fixed", size=100, overlap=20)

    assert all(30 <= _token_count(chunk) <= 100 for chunk in chunks)


def test_fixed_windows_do_not_split_unicode_characters():
    text = "🙂 知"

    chunks = chunk_text(text, strategy="fixed", size=2, overlap=0)

    assert "".join(chunks) == text
    assert all("�" not in chunk for chunk in chunks)
    assert all(_token_count(chunk) <= 2 for chunk in chunks)


def test_fixed_keeps_overlap_across_whitespace_windows():
    text = "。知。🙂\n知 🙂库🙂\n  a库ab\n🙂"

    chunks = chunk_text(text, strategy="fixed", size=2, overlap=1)

    assert all(any(left.endswith(right[:length]) for length in range(1, len(right) + 1)) for left, right in pairwise(chunks))
    assert all(_token_count(chunk) <= 2 for chunk in chunks)


def test_recursive_overlap_guaranteed_on_unbroken_text():
    text = "一二三四五六七八九十一二三四五六七八九十"
    chunks = chunk_text(text, strategy="recursive", size=10, overlap=4)
    assert all(_token_count(chunk) <= 10 for chunk in chunks)
    assert all(_token_tail(chunks[index], 4) == _first_tokens(chunks[index + 1], 4) for index in range(len(chunks) - 1))


def test_recursive_shrinks_overlap_when_concatenation_retokenizes_over_size():
    # 两个正文片段各 2 token, 但完整 overlap 拼接后会重新编码为 5 token。
    # 去掉最前面的 `a` 后仍保留 1 token overlap, 且候选正好为 4 token。
    assert _token_count("a！") == 2
    assert _token_count(".aé") == 2
    assert _token_count("a！.aé") == 5
    assert _token_count("！.aé") == 4

    chunks = chunk_text("a！.aé", strategy="recursive", size=4, overlap=2)

    assert chunks == ["a！", "！.aé"]
    assert all(_token_count(chunk) <= 4 for chunk in chunks)


def test_recursive_overlap_preserves_unicode_and_skips_empty_chunks():
    chunks = chunk_text("甲。 乙。 丙。", strategy="recursive", size=3, overlap=1)

    assert all(chunks)
    assert all("�" not in chunk for chunk in chunks)
    assert all(_token_count(chunk) <= 3 for chunk in chunks)


def test_recursive_reduces_overlap_when_content_budget_cannot_fit_character():
    chunks = chunk_text("👨 知", strategy="recursive", size=3, overlap=2)

    assert all("�" not in chunk for chunk in chunks)
    assert all(_token_count(chunk) <= 3 for chunk in chunks)


def test_semantic_does_not_split_homogeneous_document():
    text = "".join(f"第{index}句内容。" for index in range(10))

    def embedder(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.001 * index] for index in range(len(texts))]

    chunks = chunk_text(text, strategy="semantic", size=500, overlap=0, embedder=embedder, semantic_percentile=90)

    assert len(chunks) == 1


def test_semantic_batches_embedder_calls():
    text = "".join(f"第{index}句。" for index in range(200))
    batch_sizes: list[int] = []

    def embedder(batch: list[str]) -> list[list[float]]:
        batch_sizes.append(len(batch))
        return [[1.0, 0.0] for _ in batch]

    chunk_text(text, strategy="semantic", size=5000, overlap=0, embedder=embedder)

    assert all(size <= 64 for size in batch_sizes)
    assert sum(batch_sizes) == 200


def test_paragraph_falls_back_when_heading_exceeds_size():
    text = "# " + "很" * 30 + "\n\n正文内容。"
    chunks = chunk_text(text, strategy="paragraph", size=10, overlap=0)
    assert all(_token_count(chunk) <= 10 for chunk in chunks)


def test_recursive_splits_english_on_bang_and_question():
    chunks = chunk_text("AAAA! BBBB? CCCC", strategy="recursive", size=3, overlap=0)
    assert chunks == ["AAAA!", "BBBB?", "CCCC"]


def test_recursive_splits_on_cjk_semicolon_and_colon():
    chunks = chunk_text("甲；乙：丙", strategy="recursive", size=2, overlap=0)
    assert chunks == ["甲；", "乙：", "丙"]


def test_sentences_do_not_split_decimals():
    assert _sentences("版本 3.14 已发布。下一句。") == ["版本 3.14 已发布。", "下一句。"]


def test_sentences_split_english_on_period_plus_space():
    assert _sentences("Hello world. Next sentence.") == ["Hello world.", "Next sentence."]


def test_sentences_do_not_split_multi_period_abbreviations():
    assert _sentences("Use e.g. indexes. Next sentence.") == ["Use e.g. indexes.", "Next sentence."]


def test_sentences_do_not_split_title_abbreviations():
    assert _sentences("Dr. Smith arrived. Next sentence.") == ["Dr. Smith arrived.", "Next sentence."]


def test_sentences_do_not_split_richer_multi_period_abbreviations():
    assert _sentences("A Ph.D. candidate arrived. Next.") == ["A Ph.D. candidate arrived.", "Next."]


def test_sentences_allow_abbreviations_at_sentence_end():
    assert _sentences("We considered alternatives, etc. Next sentence.") == ["We considered alternatives, etc.", "Next sentence."]
    assert _sentences("He lives in the U.S. Next sentence.") == ["He lives in the U.S.", "Next sentence."]


def test_sentences_keep_capitalized_abbreviation_continuations():
    assert _sentences("The U.S. Army arrived. Next sentence.") == ["The U.S. Army arrived.", "Next sentence."]
    assert _sentences("A Ph.D. Candidate arrived. Next sentence.") == ["A Ph.D. Candidate arrived.", "Next sentence."]


def test_sentences_keep_closing_quote_with_previous_sentence():
    assert _sentences('He said "Stop." Next sentence.') == ['He said "Stop."', "Next sentence."]


def test_sentences_keep_cjk_closer_with_previous_sentence():
    assert _sentences("说明（结束。）下一句。") == ["说明（结束。）", "下一句。"]


def test_sentences_split_bang_and_question_without_spaces():
    assert _sentences("Hello!Next?") == ["Hello!", "Next?"]


def test_token_budget_is_language_independent():
    # 同样的 token 预算, 英文能容纳的字符数远多于中文, 证明切分按 token 而非字符。
    chinese = "知识库" * 100
    english = "knowledge " * 200
    zh_chunks = chunk_text(chinese, strategy="fixed", size=30, overlap=0)
    en_chunks = chunk_text(english, strategy="fixed", size=30, overlap=0)
    assert all(_token_count(chunk) <= 30 for chunk in zh_chunks + en_chunks)
    assert max(len(chunk) for chunk in en_chunks) > max(len(chunk) for chunk in zh_chunks)


def test_unknown_strategy_fails_at_boundary():
    with pytest.raises(ValueError, match="unknown chunking strategy"):
        chunk_text("content", strategy="magic")
