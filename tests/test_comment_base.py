"""评论监控基础 Mixin 契约测试（modules/comment/_base.py）。

覆盖 MonitorBaseMixin 的全部方法：
- __init__：子模块装配、可选依赖注入
- _apply_sentiment_results：情感分析结果按 rpid 回写
- _empty_visualization_data / _build_visualization_data：大屏数据组装
- _sentiment_counts / _serialize_comment：情感统计与声量评论序列化

测试策略：
- 使用真实 MonitorBaseMixin（内部真实去重器与情感分析器），仅用契约级假 API 满足构造依赖；
- 不触发网络、数据库读写，纯内存断言。
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from modules.comment._base import MonitorBaseMixin
from modules.comment.collector import CommentCollector
from modules.comment.deduplicator import CommentDeduplicator
from modules.comment.sentiment import SentimentAnalyzer


def make_monitor(llm_client=None, alert_callback=None) -> MonitorBaseMixin:
    """构造只依赖契约级假 API 的基础监控器。

    Args:
        llm_client: 可选的 LLM 客户端替身。
        alert_callback: 可选的预警回调替身。

    Returns:
        真实的 MonitorBaseMixin 实例（依赖均为真实子模块）。
    """
    return MonitorBaseMixin(api=SimpleNamespace(), llm_client=llm_client, alert_callback=alert_callback)


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


def test_init_assembles_real_submodules_without_optional_deps():
    """默认初始化应装配真实采集器/去重器/情感分析器，且无 LLM 与回调。"""
    monitor = make_monitor()

    assert isinstance(monitor.collector, CommentCollector)
    assert isinstance(monitor.deduplicator, CommentDeduplicator)
    assert isinstance(monitor.analyzer, SentimentAnalyzer)
    assert monitor.llm_client is None
    assert monitor.alert_callback is None
    # LLM 未注入时不允许启用 LLM 聚合总结，避免无谓成本。
    assert monitor.analyzer.use_llm_summary is False
    assert monitor._monitoring_history == {}
    assert monitor.custom_keywords == set()


def test_init_keeps_injected_llm_client_and_callback():
    """注入 LLM 客户端与回调后应原样保存（use_llm_summary 仍默认关闭）。"""
    llm = SimpleNamespace()
    callback = lambda payload: None  # noqa: E731 - 仅作引用透传校验

    monitor = make_monitor(llm_client=llm, alert_callback=callback)

    assert monitor.llm_client is llm
    assert monitor.alert_callback is callback
    assert monitor.analyzer.llm_client is llm
    assert monitor.analyzer.use_llm_summary is False


def test_threshold_constants_follow_contract():
    """类常量是预警规则的契约基准，不能被悄然改动。"""
    assert MonitorBaseMixin.NEGATIVE_RATIO_THRESHOLD == 0.3
    assert MonitorBaseMixin.COMMENT_SURGE_THRESHOLD == 2.0
    assert MonitorBaseMixin.RISK_COUNT_THRESHOLD == 5


# ---------------------------------------------------------------------------
# _apply_sentiment_results
# ---------------------------------------------------------------------------


def test_apply_sentiment_results_writes_back_matching_rpid():
    """命中的评论应原地补齐 sentiment / sentiment_score / matched_keywords。"""
    comments = [
        {"rpid": 1, "content": "好"},
        {"rpid": 2, "content": "差"},
    ]
    result = {
        "analyzed_comments": [
            {"rpid": 1, "sentiment": "positive", "confidence": 0.8, "matched_keywords": {"positive": ["好"]}},
            {"rpid": 2, "sentiment": "negative", "confidence": 0.7, "matched_keywords": {"negative": ["差"]}},
        ]
    }

    MonitorBaseMixin._apply_sentiment_results(comments, result)

    assert comments[0]["sentiment"] == "positive"
    assert comments[0]["sentiment_score"] == 0.8
    assert comments[0]["matched_keywords"] == {"positive": ["好"]}
    assert comments[1]["sentiment"] == "negative"


def test_apply_sentiment_results_skips_unmatched_and_none_rpid():
    """无对应分析结果或 rpid 为 None 的评论必须保持原样，不写入脏标签。"""
    comments = [{"rpid": 9, "content": "未分析"}, {"rpid": None, "content": "无ID"}]
    result = {"analyzed_comments": [{"rpid": None, "sentiment": "risk", "confidence": 0.9}]}

    MonitorBaseMixin._apply_sentiment_results(comments, result)

    assert "sentiment" not in comments[0]
    # rpid=None 的评论不会被 None 键误伤。
    assert "sentiment" not in comments[1]


def test_apply_sentiment_results_falls_back_to_neutral_for_missing_label():
    """分析结果缺少 sentiment 时应回落为 neutral，置信度缺失补 0。"""
    comments = [{"rpid": 5}]
    result = {"analyzed_comments": [{"rpid": 5, "sentiment": None}]}

    MonitorBaseMixin._apply_sentiment_results(comments, result)

    assert comments[0]["sentiment"] == "neutral"
    assert comments[0]["sentiment_score"] == 0
    assert comments[0]["matched_keywords"] == {}


def test_apply_sentiment_results_tolerates_none_result():
    """情感结果为 None（未启用分析）时不得抛异常，也不修改评论。"""
    comments = [{"rpid": 1, "content": "x"}]

    MonitorBaseMixin._apply_sentiment_results(comments, None)

    assert comments == [{"rpid": 1, "content": "x"}]


# ---------------------------------------------------------------------------
# _empty_visualization_data
# ---------------------------------------------------------------------------


def test_empty_visualization_data_shape_is_renderable():
    """空大屏数据必须字段完整，前端可直接消费。"""
    data = MonitorBaseMixin._empty_visualization_data()

    assert data["sentiment_distribution"] == {}
    assert data["date_comment_counts"] == []
    assert data["top10_voice_comments"] == []
    assert data["dedup_statistics"]["before_count"] == 0
    assert data["dedup_statistics"]["after_count"] == 0
    assert data["dedup_statistics"]["removed_count"] == 0
    assert data["dedup_statistics"]["reasons"] == {
        "same_user_repeat": 0,
        "cross_user_aggregation": 0,
        "fuzzy_similarity": 0,
        "time_window_hotspot": 0,
    }


# ---------------------------------------------------------------------------
# _build_visualization_data
# ---------------------------------------------------------------------------


def test_build_visualization_data_aggregates_dates_sentiment_and_dedup():
    """日期桶、情感分布与去重原因统计应与输入一致。"""
    monitor = make_monitor()
    base = datetime(2026, 8, 22, 10, 0)
    raw = [
        {"rpid": 1, "uname": "a", "content": "好评", "ctime": base, "like": 3, "sentiment": "positive"},
        {"rpid": 2, "uname": "b", "content": "一般", "ctime": "2026-08-23T09:00:00", "like": 0, "sentiment": "neutral"},
        {"rpid": 3, "uname": "c", "content": "差评", "ctime": None, "like": 1, "sentiment": "negative"},
    ]
    processed = raw[:2]
    dedup_result = {
        "user_duplicates": [{"rpids": [1, 2]}],
        "cross_user_groups": [{}, {}],
        "fuzzy_groups": [],
        "time_hotspots": [{}],
    }

    data = monitor._build_visualization_data(raw, processed, dedup_result)

    # 无 ctime 的评论不产生日期桶。
    assert data["date_comment_counts"] == [
        {"date": "2026-08-22", "count": 1},
        {"date": "2026-08-23", "count": 1},
    ]
    assert data["sentiment_distribution"] == {"positive": 1, "neutral": 1, "negative": 1}
    assert data["dedup_statistics"] == {
        "before_count": 3,
        "after_count": 2,
        "removed_count": 1,
        "reasons": {
            "same_user_repeat": 1,
            "cross_user_aggregation": 2,
            "fuzzy_similarity": 0,
            "time_window_hotspot": 1,
        },
    }


def test_build_visualization_data_handles_missing_dedup_result():
    """去重结果为 None 时原因统计全 0，且移除数不会为负。"""
    monitor = make_monitor()
    processed = [{"rpid": 1, "uname": "a", "content": "x", "ctime": datetime(2026, 8, 22), "like": 5}]

    data = monitor._build_visualization_data(processed, processed, None)

    assert data["dedup_statistics"]["reasons"] == {
        "same_user_repeat": 0,
        "cross_user_aggregation": 0,
        "fuzzy_similarity": 0,
        "time_window_hotspot": 0,
    }
    assert data["dedup_statistics"]["removed_count"] == 0


def test_build_visualization_data_ranks_by_voice_score_and_caps_ten():
    """Top 评论按声量分降序、最多 10 条，且点赞不敌跨用户复读权重。"""
    monitor = make_monitor()
    processed = [
        {"rpid": index, "uname": f"u{index}", "content": "c", "like": index, "sentiment": "neutral"}
        for index in range(12)
    ]
    # 0 赞但被 3 人复读的评论，声量分 20，应压过 15 赞的单条。
    processed.append({"rpid": 99, "uname": "hot", "content": "复读", "like": 0, "voice_weight": 3})

    data = monitor._build_visualization_data(processed, processed, None)
    top = data["top10_voice_comments"]

    assert len(top) == 10
    assert top[0]["rpid"] == 99
    assert top[0]["voice_score"] == 20
    scores = [item["voice_score"] for item in top]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# _sentiment_counts
# ---------------------------------------------------------------------------


def test_sentiment_counts_defaults_unknown_label_to_neutral():
    """缺失或空情感标签一律计入 neutral。"""
    comments = [{"sentiment": "positive"}, {}, {"sentiment": ""}, {"sentiment": "risk"}]

    counts = MonitorBaseMixin._sentiment_counts(comments)

    assert counts == {"positive": 1, "neutral": 2, "risk": 1}


def test_sentiment_counts_on_empty_input_returns_empty_dict():
    """空列表不应凭空造出 neutral 计数。"""
    assert MonitorBaseMixin._sentiment_counts([]) == {}


# ---------------------------------------------------------------------------
# _serialize_comment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("comment", "expected_reason"),
    [
        ({"voice_type": "cross_user_same", "voice_weight": 3}, "跨用户同内容聚合，合并 3 条声量"),
        ({"duplicate_type": "user_repeat"}, "同用户重复评论折叠，保留 1 条代表"),
        ({"duplicate_type": "fuzzy_similar"}, "相似内容去重，保留互动更高的代表评论"),
        ({"is_duplicate": True}, "历史去重标记，保留代表评论"),
        ({}, "唯一内容，清洗后保留"),
    ],
)
def test_serialize_comment_cleaning_reason_priority(comment, expected_reason):
    """清洗结论按“跨用户聚合 > 同用户复读 > 模糊相似 > 历史标记 > 唯一”判定。"""
    result = MonitorBaseMixin._serialize_comment(comment)

    assert result["cleaning_reason"] == expected_reason


def test_serialize_comment_fills_defaults_for_missing_fields():
    """字段缺失时给出安全默认值，避免前端渲染出 None。"""
    result = MonitorBaseMixin._serialize_comment({})

    assert result["rpid"] is None
    assert result["uname"] == "匿名用户"
    assert result["content"] == ""
    assert result["like"] == 0
    assert result["ctime"] is None
    assert result["voice_weight"] == 1
    assert result["voice_score"] == 0
    assert result["sentiment"] == "neutral"


def test_serialize_comment_supports_duplicate_count_alias_and_weight_floor():
    """duplicate_count 作为 voice_weight 的别名，且权重下限为 1。"""
    aliased = MonitorBaseMixin._serialize_comment({"duplicate_count": 4, "like": 2})
    floored = MonitorBaseMixin._serialize_comment({"voice_weight": 0, "like": 7})

    assert aliased["voice_weight"] == 4
    assert aliased["voice_score"] == 2 + 3 * 10
    assert floored["voice_weight"] == 1
    assert floored["voice_score"] == 7


def test_serialize_comment_coerces_string_numbers():
    """字符串点赞数应被转成整数参与声量计算。"""
    result = MonitorBaseMixin._serialize_comment({"like": "12", "voice_weight": "2"})

    assert result["like"] == 12
    assert result["voice_score"] == 12 + 10
