import pytest

from app.chunking import chunk_text


def test_fixed_strategy_keeps_overlap():
    chunks = chunk_text("a" * 1000, strategy="fixed", size=700, overlap=100)
    assert len(chunks) == 2
    assert chunks[0][-100:] == chunks[1][:100]


def test_recursive_strategy_prefers_paragraph_boundaries():
    text = "第一段。\n\n第二段很长。\n\n第三段。"
    chunks = chunk_text(text, strategy="recursive", size=10, overlap=0)
    assert chunks == ["第一段。", "第二段很长。", "第三段。"]


def test_recursive_overlap_never_exceeds_chunk_size():
    chunks = chunk_text("第一段很长。\n\n第二段也很长。", strategy="recursive", size=8, overlap=3)
    assert all(len(chunk) <= 8 for chunk in chunks)


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


def test_unknown_strategy_fails_at_boundary():
    with pytest.raises(ValueError, match="unknown chunking strategy"):
        chunk_text("content", strategy="magic")
