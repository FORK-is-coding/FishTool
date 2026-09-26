"""评论去重核心逻辑测试。"""
from datetime import datetime, timedelta

import pytest

from modules.comment.deduplicator import CommentDeduplicator


def make_comment(rpid: int, uid: int, content: str, ctime=None, sentiment="neutral"):
    """构造评论字典。

    Args:
        rpid: 评论 ID。
        uid: 用户 ID。
        content: 评论文本。
        ctime: 评论时间。
        sentiment: 情感标签。

    Returns:
        可供去重器处理的评论字典。
    """
    return {
        "rpid": rpid,
        "uid": uid,
        "uname": f"user-{uid}",
        "content": content,
        "ctime": ctime,
        "sentiment": sentiment,
    }


def test_deduplicate_runs_all_four_layers(monkeypatch):
    """完整管线应折叠复读、聚合跨用户内容并执行模糊去重。"""
    deduplicator = CommentDeduplicator(short_text_threshold=5, fuzzy_match_threshold=0.8)
    monkeypatch.setattr(
        deduplicator,
        "_calculate_similarity",
        lambda left, right: 0.9 if left.startswith("这个视频") and right.startswith("这个视频") else 0.0,
    )
    now = datetime(2026, 8, 22, 10, 0)
    comments = [
        make_comment(1, 10, "好", now),
        make_comment(2, 10, "好", now),
        make_comment(3, 11, "好", now),
        make_comment(4, 12, "这个视频非常精彩", now, "positive"),
        make_comment(5, 13, "这个视频真的精彩", now, "positive"),
    ]

    result = deduplicator.deduplicate(comments)

    assert result["original_count"] == 5
    assert len(result["user_duplicates"]) == 1
    assert result["user_duplicates"][0]["rpids"] == [1, 2]
    assert len(result["cross_user_groups"]) == 1
    assert result["cross_user_groups"][0]["count"] == 2
    assert len(result["fuzzy_groups"]) == 1
    assert result["deduplicated_count"] == 2


def test_fuzzy_dedup_keeps_short_and_folds_similar_long_comments(monkeypatch):
    """短文本必须保留，相似长文本必须折叠并记录变体。"""
    deduplicator = CommentDeduplicator(short_text_threshold=5, fuzzy_match_threshold=0.85)
    monkeypatch.setattr(deduplicator, "_calculate_similarity", lambda *_: 0.9)
    comments = [
        make_comment(1, 1, "短评"),
        make_comment(2, 2, "第一条足够长的评论"),
        make_comment(3, 3, "第二条足够长的评论"),
    ]

    result, groups = deduplicator._deduplicate_by_fuzzy(comments)

    assert [item["rpid"] for item in result] == [1, 2]
    assert result[1]["fuzzy_duplicate_count"] == 2
    assert result[1]["fuzzy_variants"] == [{"rpid": 3, "content": "第二条足够长的评论"}]
    assert groups[0]["rpids"] == [2, 3]


def test_time_hotspots_classifies_positive_and_negative_bursts():
    """高于双倍均值的小时窗口应按情感占比分类。"""
    deduplicator = CommentDeduplicator()
    base = datetime(2026, 8, 22, 8, 0)
    comments = [make_comment(1, 1, "x", base, "neutral")]
    comments.extend(make_comment(10 + i, 10 + i, "x", base + timedelta(hours=1), "positive") for i in range(7))
    comments.extend(make_comment(20 + i, 20 + i, "x", base + timedelta(hours=2), "neutral") for i in range(2))

    hotspots = deduplicator._detect_time_hotspots(comments)

    assert len(hotspots) == 1
    assert hotspots[0]["type"] == "positive_burst"
    assert hotspots[0]["comment_count"] == 7
    assert deduplicator._detect_time_hotspots([make_comment(99, 1, "x")]) == []


@pytest.mark.parametrize(
    ("left", "right", "distance"),
    [("", "abc", 3), ("abc", "", 3), ("kitten", "sitting", 3), ("same", "same", 0)],
)
def test_levenshtein_distance_boundaries(left, right, distance):
    """编辑距离应覆盖空串、相同串和普通替换场景。"""
    assert CommentDeduplicator()._levenshtein_distance(left, right) == distance


def test_normalize_and_similarity_boundaries(monkeypatch):
    """标准化及相似度应正确处理空文本和 SimHash 粗筛。"""
    deduplicator = CommentDeduplicator()
    assert deduplicator._normalize_content(" A B\nC ") == "abc"
    assert deduplicator._normalize_content("") == ""
    assert deduplicator._calculate_similarity("", "abc") == 0.0
    monkeypatch.setattr(deduplicator, "_simhash", lambda text: 0 if text == "a" else 15)
    assert deduplicator._calculate_similarity("a", "b") == 0.0
