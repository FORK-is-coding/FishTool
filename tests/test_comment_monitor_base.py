"""评论舆情监控器基础 Mixin 的契约级测试。

覆盖 modules/comment/_base.py 的 MonitorBaseMixin：
- 类常量与 __init__ 装配的子模块/历史容器
- _apply_sentiment_results：按 rpid 回写情感标签
- _empty_visualization_data：无数据大屏结构
- _build_visualization_data：日期聚合、去重统计、Top10 声量排序
- _sentiment_counts / _serialize_comment：标签兜底与清洗原因解释

纯内存计算，使用真实 CommentMonitor 实例（api 为占位对象）。
"""

from __future__ import annotations

from datetime import datetime

from modules.comment._base import MonitorBaseMixin
from modules.comment.collector import CommentCollector
from modules.comment.deduplicator import CommentDeduplicator
from modules.comment.monitor import CommentMonitor
from modules.comment.sentiment import SentimentAnalyzer


def _monitor(**kwargs) -> CommentMonitor:
    """构造以占位对象为 api 的真实监控器。"""
    return CommentMonitor(api=object(), **kwargs)


# --------------------------------------------------------------------- 常量与初始化

def test_class_thresholds() -> None:
    """预警阈值常量应与文档一致。"""
    assert MonitorBaseMixin.NEGATIVE_RATIO_THRESHOLD == 0.3
    assert MonitorBaseMixin.COMMENT_SURGE_THRESHOLD == 2.0
    assert MonitorBaseMixin.RISK_COUNT_THRESHOLD == 5


def test_init_assembles_dependencies() -> None:
    """初始化应装配采集/去重/情感三个子模块与历史容器。"""
    api = object()
    callback = object()
    monitor = CommentMonitor(api=api, llm_client=None, alert_callback=callback)

    assert monitor.api is api
    assert monitor.llm_client is None
    assert monitor.alert_callback is callback
    assert isinstance(monitor.collector, CommentCollector)
    assert isinstance(monitor.deduplicator, CommentDeduplicator)
    assert isinstance(monitor.analyzer, SentimentAnalyzer)
    assert monitor._monitoring_history == {}
    assert monitor.custom_keywords == set()


def test_init_injects_llm_client_into_analyzer() -> None:
    """注入的 LLM 客户端应传给情感分析器。"""
    llm = object()
    monitor = CommentMonitor(api=object(), llm_client=llm)

    assert monitor.analyzer.llm_client is llm
    assert monitor.analyzer.use_llm_summary is False


# --------------------------------------------------------------------- 情感回写

def test_apply_sentiment_results_writes_back_by_rpid() -> None:
    """分析结果应按 rpid 回写到原评论，含字符串/整数混合匹配。"""
    comments = [{"rpid": 1}, {"rpid": "2"}, {"rpid": 3}]
    result = {"analyzed_comments": [
        {"rpid": 1, "sentiment": "positive", "confidence": 0.8, "matched_keywords": {"好": 1}},
        {"rpid": "2", "sentiment": "negative", "confidence": 0.6},
    ]}

    MonitorBaseMixin._apply_sentiment_results(comments, result)

    assert comments[0] == {
        "rpid": 1,
        "sentiment": "positive",
        "sentiment_score": 0.8,
        "matched_keywords": {"好": 1},
    }
    assert comments[1]["sentiment"] == "negative"
    assert comments[1]["matched_keywords"] == {}
    # 没有分析结果的评论不应被改写。
    assert comments[2] == {"rpid": 3}


def test_apply_sentiment_results_defaults_none_label_and_confidence() -> None:
    """sentiment 为 None 时归 neutral，confidence 缺失按 0。"""
    comments = [{"rpid": 1}]
    result = {"analyzed_comments": [{"rpid": 1, "sentiment": None}]}

    MonitorBaseMixin._apply_sentiment_results(comments, result)

    assert comments[0]["sentiment"] == "neutral"
    assert comments[0]["sentiment_score"] == 0


def test_apply_sentiment_results_ignores_missing_rpid() -> None:
    """分析项缺少 rpid 时应跳过。"""
    comments = [{"rpid": 1}]
    result = {"analyzed_comments": [{"sentiment": "positive"}]}

    MonitorBaseMixin._apply_sentiment_results(comments, result)

    assert comments[0] == {"rpid": 1}


def test_apply_sentiment_results_empty_or_none() -> None:
    """None 与空结果都不应抛异常。"""
    comments = [{"rpid": 1}]

    MonitorBaseMixin._apply_sentiment_results(comments, None)
    MonitorBaseMixin._apply_sentiment_results(comments, {})

    assert comments == [{"rpid": 1}]


# --------------------------------------------------------------------- 空大屏

def test_empty_visualization_data_structure() -> None:
    """无数据大屏结构应字段完整且可安全渲染。"""
    data = MonitorBaseMixin._empty_visualization_data()

    assert data["sentiment_distribution"] == {}
    assert data["date_comment_counts"] == []
    assert data["top10_voice_comments"] == []
    assert data["dedup_statistics"]["reasons"] == {
        "same_user_repeat": 0,
        "cross_user_aggregation": 0,
        "fuzzy_similarity": 0,
        "time_window_hotspot": 0,
    }


# --------------------------------------------------------------------- 情感统计

def test_sentiment_counts_falls_back_to_neutral() -> None:
    """未知或缺失情感标签统一归入 neutral。"""
    comments = [
        {"sentiment": "positive"},
        {"sentiment": "positive"},
        {"sentiment": None},
        {},
        {"sentiment": "negative"},
    ]

    counts = MonitorBaseMixin._sentiment_counts(comments)

    assert counts == {"positive": 2, "neutral": 2, "negative": 1}


# --------------------------------------------------------------------- 单条序列化

def test_serialize_comment_cross_user_reason() -> None:
    """跨用户同内容聚合应生成声量合并说明。"""
    result = MonitorBaseMixin._serialize_comment({
        "rpid": 1,
        "uname": "u",
        "content": "c",
        "like": 3,
        "voice_type": "cross_user_same",
        "voice_weight": 4,
        "sentiment": "positive",
    })

    assert result["cleaning_reason"] == "跨用户同内容聚合，合并 4 条声量"
    assert result["voice_score"] == 3 + 3 * 10
    assert result["voice_weight"] == 4


def test_serialize_comment_user_repeat_reason() -> None:
    """同用户重复评论应折叠说明。"""
    result = MonitorBaseMixin._serialize_comment({"rpid": 1, "duplicate_type": "user_repeat"})

    assert result["cleaning_reason"] == "同用户重复评论折叠，保留 1 条代表"


def test_serialize_comment_fuzzy_reason() -> None:
    """相似内容去重应给出对应说明。"""
    result = MonitorBaseMixin._serialize_comment({"rpid": 1, "duplicate_type": "fuzzy_similar"})

    assert result["cleaning_reason"] == "相似内容去重，保留互动更高的代表评论"


def test_serialize_comment_legacy_duplicate_flag_reason() -> None:
    """历史去重标记应给出保留代表评论说明。"""
    result = MonitorBaseMixin._serialize_comment({"rpid": 1, "is_duplicate": True})

    assert result["cleaning_reason"] == "历史去重标记，保留代表评论"


def test_serialize_comment_unique_default_and_field_defaults() -> None:
    """唯一内容应给出默认说明，缺失字段回落默认值。"""
    result = MonitorBaseMixin._serialize_comment({"rpid": 9})

    assert result["uname"] == "匿名用户"
    assert result["content"] == ""
    assert result["like"] == 0
    assert result["voice_weight"] == 1
    assert result["voice_score"] == 0
    assert result["sentiment"] == "neutral"
    assert result["cleaning_reason"] == "唯一内容，清洗后保留"


def test_serialize_comment_weight_falls_back_to_duplicate_count() -> None:
    """没有 voice_weight 时应回退 duplicate_count 计权。"""
    result = MonitorBaseMixin._serialize_comment({"rpid": 1, "like": 5, "duplicate_count": 3})

    assert result["voice_weight"] == 3
    assert result["voice_score"] == 5 + 2 * 10


# --------------------------------------------------------------------- 大屏构造

def test_build_visualization_data_aggregates_dates_and_dedup() -> None:
    """应正确聚合日期、去重原因计数与前后条数。"""
    monitor = _monitor()
    raw = [
        {"rpid": 1, "ctime": datetime(2026, 1, 1, 10, 0), "sentiment": "positive"},
        {"rpid": 2, "ctime": datetime(2026, 1, 1, 11, 0), "sentiment": "negative"},
        {"rpid": 3, "ctime": "2026-01-02T08:00:00", "sentiment": "neutral"},
    ]
    processed = [{"rpid": 1, "ctime": datetime(2026, 1, 1, 10, 0)}]
    dedup = {
        "user_duplicates": [{"rpids": [1, 2]}],
        "cross_user_groups": [{"count": 2}],
        "fuzzy_groups": [],
        "time_hotspots": [{"type": "positive_burst"}],
    }

    data = monitor._build_visualization_data(raw, processed, dedup)

    assert data["date_comment_counts"] == [
        {"date": "2026-01-01", "count": 2},
        {"date": "2026-01-02", "count": 1},
    ]
    assert data["sentiment_distribution"] == {"positive": 1, "negative": 1, "neutral": 1}
    assert data["dedup_statistics"]["before_count"] == 3
    assert data["dedup_statistics"]["after_count"] == 1
    assert data["dedup_statistics"]["removed_count"] == 2
    assert data["dedup_statistics"]["reasons"] == {
        "same_user_repeat": 1,
        "cross_user_aggregation": 1,
        "fuzzy_similarity": 0,
        "time_window_hotspot": 1,
    }


def test_build_visualization_data_skips_entries_without_date() -> None:
    """ctime 缺失的评论不应产生日期计数。"""
    monitor = _monitor()

    data = monitor._build_visualization_data([{"rpid": 1}, {"rpid": 2}], [], None)

    assert data["date_comment_counts"] == []
    assert data["dedup_statistics"]["reasons"] == {
        "same_user_repeat": 0,
        "cross_user_aggregation": 0,
        "fuzzy_similarity": 0,
        "time_window_hotspot": 0,
    }
    assert data["dedup_statistics"]["removed_count"] == 2


def test_build_visualization_data_top10_sorted_by_voice_score() -> None:
    """Top10 声量榜应按点赞与复读权重降序排列。"""
    monitor = _monitor()
    processed = [
        {"rpid": 1, "like": 1, "voice_weight": 1},
        {"rpid": 2, "like": 5, "voice_weight": 2},
        {"rpid": 3, "like": 100, "voice_weight": 1},
    ]

    data = monitor._build_visualization_data(processed, processed, None)

    assert [item["rpid"] for item in data["top10_voice_comments"]] == [3, 2, 1]


def test_build_visualization_data_top10_capped_at_ten() -> None:
    """声量榜最多返回 10 条。"""
    monitor = _monitor()
    processed = [{"rpid": index, "like": index, "voice_weight": 1} for index in range(15)]

    data = monitor._build_visualization_data(processed, processed, None)

    assert len(data["top10_voice_comments"]) == 10


def test_build_visualization_data_removed_count_never_negative() -> None:
    """处理后条数多于原始条数时 removed_count 夹到 0。"""
    monitor = _monitor()

    data = monitor._build_visualization_data([{"rpid": 1}], [{"rpid": 1}, {"rpid": 2}], None)

    assert data["dedup_statistics"]["removed_count"] == 0
