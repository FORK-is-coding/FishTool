"""评论舆情监控器监控动作 Mixin 的契约级测试。

覆盖 modules/comment/_monitor.py 的 MonitorActionsMixin：
- add_custom_keywords：关键词集合累积
- monitor_video：采集->情感->去重->预警->持久化全链路，以及禁用情感/去重、空评论短路、落库 warning 透传
- monitor_multiple_videos：逐视频监控与预警聚合
- monitor_user_account：昵称获取（成功/失败）、无视频短路、批量监控适配

所有协程外层使用 asyncio.wait_for 超时兜底，配合契约级假依赖，绝不触网、不落库。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from modules.comment.monitor import CommentMonitor

WAIT_SECONDS = 5.0


def _run(coro):
    """带外层超时兜底的协程运行器。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=WAIT_SECONDS))


def _monitor() -> CommentMonitor:
    """构造以占位对象为 api 的真实监控器。"""
    return CommentMonitor(api=object())


def _comments(count: int = 3) -> list[dict]:
    """构造一批带 ctime 的评论字典。"""
    return [
        {
            "rpid": index,
            "uid": index,
            "uname": f"u{index}",
            "content": f"内容{index}",
            "like": index,
            "ctime": datetime(2026, 1, 1),
            "sentiment": "neutral",
        }
        for index in range(count)
    ]


def _stub_pipeline(monitor: CommentMonitor, *, comments=None, analyzed=None, deduped=None, alerts=None) -> dict:
    """把监控器的五个外部依赖替换为可记录的契约级替身。"""
    comments = comments if comments is not None else _comments(3)
    captured: dict = {}

    async def _collect(bvid, strategy=None):
        """记录采集参数并返回预置评论。"""
        captured["collect"] = (bvid, strategy)
        return comments

    def _analyze(rows):
        """返回预置情感分析结果。"""
        captured["analyze"] = rows
        return analyzed if analyzed is not None else {
            "analyzed_comments": [{"rpid": 0, "sentiment": "positive", "confidence": 0.9}],
            "negative_ratio": 0.0,
            "sentiment_distribution": {},
        }

    def _dedup(rows):
        """返回预置去重结果。"""
        captured["dedup"] = rows
        return deduped if deduped is not None else {
            "deduplicated_comments": rows[:2],
            "user_duplicates": [],
            "cross_user_groups": [],
            "fuzzy_groups": [],
            "time_hotspots": [],
        }

    async def _detect(bvid, raw, processed, sentiment):
        """记录预警检测入参并返回预置预警。"""
        captured["detect"] = (bvid, len(raw), len(processed), sentiment)
        return alerts if alerts is not None else []

    async def _save(bvid, rows, detected):
        """记录持久化调用，不真正落库。"""
        captured["save"] = (bvid, len(rows), detected)

    monitor.collector.collect_video_comments = _collect
    monitor.analyzer.analyze_batch = _analyze
    monitor.deduplicator.deduplicate = _dedup
    monitor._detect_alerts = _detect
    monitor._save_monitoring_record = _save
    return captured


# --------------------------------------------------------------------- 自定义关键词

def test_add_custom_keywords_accumulates() -> None:
    """多次添加应累加进同一集合。"""
    monitor = _monitor()

    monitor.add_custom_keywords(["a", "b"])
    monitor.add_custom_keywords(["b", "c"])

    assert monitor.custom_keywords == {"a", "b", "c"}


def test_add_custom_keywords_empty_list_is_noop() -> None:
    """空列表不改变关键词集合。"""
    monitor = _monitor()

    monitor.add_custom_keywords([])

    assert monitor.custom_keywords == set()


# --------------------------------------------------------------------- 单视频监控

def test_monitor_video_full_pipeline() -> None:
    """完整链路应返回采集/处理计数、可视化与预警。"""
    monitor = _monitor()
    captured = _stub_pipeline(monitor, alerts=[{"type": "x", "level": "low", "message": "m"}])

    result = _run(monitor.monitor_video("BV1"))

    assert result["success"] is True
    assert result["bvid"] == "BV1"
    assert result["collected_count"] == 3
    assert result["processed_count"] == 2
    assert result["alerts"] == [{"type": "x", "level": "low", "message": "m"}]
    assert result["warning"] is None
    assert result["monitored_at"]
    assert captured["collect"] == ("BV1", "normal")
    assert captured["detect"][1] == 3 and captured["detect"][2] == 2
    assert captured["save"] == ("BV1", 3, [{"type": "x", "level": "low", "message": "m"}])


def test_monitor_video_writes_back_sentiment_before_visualization() -> None:
    """情感结果应回写原评论，使大屏情感分布读取真实标签。"""
    monitor = _monitor()
    _stub_pipeline(monitor)

    result = _run(monitor.monitor_video("BV1"))

    assert result["visualization"]["sentiment_distribution"] == {"positive": 1, "neutral": 2}


def test_monitor_video_returns_failure_without_comments() -> None:
    """采集不到评论时应返回失败结构与引导文案。"""
    monitor = _monitor()
    _stub_pipeline(monitor, comments=[])

    result = _run(monitor.monitor_video("BV1"))

    assert result["success"] is False
    assert result["collected_count"] == 0
    assert result["processed_count"] == 0
    assert "未采集到评论数据" in result["error"]
    assert result["visualization"] == monitor._empty_visualization_data()


def test_monitor_video_propagates_save_warning() -> None:
    """落库失败时 warning 应透传到监控结果。"""
    monitor = _monitor()
    monitor.collector.last_save_result = {"success": False, "saved_count": 0, "warning": "评论已采集但落库失败"}
    _stub_pipeline(monitor)

    result = _run(monitor.monitor_video("BV1"))

    assert result["warning"] == "评论已采集但落库失败"


def test_monitor_video_can_disable_sentiment() -> None:
    """enable_sentiment=False 时不做情感分析，sentiment_result 为 None。"""
    monitor = _monitor()
    _stub_pipeline(monitor)
    monitor.analyzer.analyze_batch = lambda rows: pytest.fail("不应调用情感分析")

    result = _run(monitor.monitor_video("BV1", enable_sentiment=False))

    assert result["sentiment_result"] is None
    # 未回写标签，情感分布应全为 neutral（未知归 neutral）。
    assert result["visualization"]["sentiment_distribution"] == {"neutral": 3}


def test_monitor_video_can_disable_dedup() -> None:
    """enable_dedup=False 时跳过去重，处理结果等于原始评论。"""
    monitor = _monitor()
    _stub_pipeline(monitor)
    monitor.deduplicator.deduplicate = lambda rows: pytest.fail("不应调用去重")

    result = _run(monitor.monitor_video("BV1", enable_dedup=False))

    assert result["dedup_result"] is None
    assert result["processed_count"] == 3


def test_monitor_video_passes_custom_strategy() -> None:
    """自定义采集策略应透传给采集器。"""
    monitor = _monitor()
    captured = _stub_pipeline(monitor)

    _run(monitor.monitor_video("BV1", strategy="full"))

    assert captured["collect"] == ("BV1", "full")


# --------------------------------------------------------------------- 批量监控

def test_monitor_multiple_videos_aggregates() -> None:
    """批量监控应聚合成功数与预警总数。"""
    monitor = _monitor()
    captured: dict = {}
    results_map = {
        "BV1": {"success": True, "alerts": [{"type": "a"}]},
        "BV2": {"success": False, "alerts": []},
        "BV3": {"success": True, "alerts": [{"type": "b"}, {"type": "c"}]},
    }

    async def _monitor_video(bvid, strategy=None):
        """记录策略并返回预置结果。"""
        captured["strategy"] = strategy
        return results_map[bvid]

    monitor.monitor_video = _monitor_video

    summary = _run(monitor.monitor_multiple_videos(["BV1", "BV2", "BV3"], strategy="fast"))

    assert summary["total_videos"] == 3
    assert summary["successful_count"] == 2
    assert summary["total_alerts"] == 3
    assert len(summary["results"]) == 3
    assert summary["monitored_at"]
    assert captured["strategy"] == "fast"


def test_monitor_multiple_videos_empty_list() -> None:
    """空列表应返回全零汇总。"""
    monitor = _monitor()

    summary = _run(monitor.monitor_multiple_videos([]))

    assert summary["total_videos"] == 0
    assert summary["successful_count"] == 0
    assert summary["total_alerts"] == 0
    assert summary["results"] == []


# --------------------------------------------------------------------- 账号监控

def _account_monitor(*, videos, user_info_result=None, user_info_error=None) -> tuple[CommentMonitor, dict]:
    """构造可注入用户信息与视频列表的监控器。"""
    monitor = _monitor()
    captured: dict = {}

    async def _get_user_info(uid):
        """返回预置用户资料或抛异常。"""
        captured["user_info_uid"] = uid
        if user_info_error is not None:
            raise user_info_error
        return user_info_result

    monitor.api = SimpleNamespace(get_user_info=_get_user_info)

    async def _get_user_videos(uid, limit):
        """记录并返回预置视频列表。"""
        captured["videos_args"] = (uid, limit)
        return videos

    monitor.collector._get_user_videos = _get_user_videos
    return monitor, captured


def test_monitor_user_account_happy_path() -> None:
    """账号监控应附上昵称、视频列表与批量结果。"""
    monitor, captured = _account_monitor(
        videos=[{"bvid": "BV1"}, {"bvid": "BV2"}],
        user_info_result={"data": {"name": "  UP名  "}},
    )
    batch_calls: dict = {}

    async def _batch(bvids, strategy=None):
        """记录批量监控入参。"""
        batch_calls["bvids"] = bvids
        batch_calls["strategy"] = strategy
        return {"total_videos": 2, "successful_count": 2, "total_alerts": 0, "results": [], "monitored_at": "t"}

    monitor.monitor_multiple_videos = _batch

    result = _run(monitor.monitor_user_account("42", video_limit=5, strategy="full"))

    assert captured["user_info_uid"] == 42
    assert captured["videos_args"] == ("42", 5)
    assert result["uid"] == "42"
    assert result["nickname"] == "UP名"
    assert result["username"] == "UP名"
    assert result["videos"] == [{"bvid": "BV1"}, {"bvid": "BV2"}]
    assert batch_calls["bvids"] == ["BV1", "BV2"]
    assert batch_calls["strategy"] == "full"


def test_monitor_user_account_returns_failure_without_videos() -> None:
    """没有视频时应返回失败结构。"""
    monitor, _ = _account_monitor(videos=[], user_info_result={"data": {"name": "UP"}})

    result = _run(monitor.monitor_user_account("42"))

    assert result["success"] is False
    assert result["error"] == "未找到视频"
    assert result["uid"] == "42"
    assert result["nickname"] == "UP"


def test_monitor_user_account_survives_nickname_failure() -> None:
    """昵称获取失败应继续采集，昵称回落为空串。"""
    monitor, _ = _account_monitor(
        videos=[{"bvid": "BV1"}],
        user_info_error=RuntimeError("profile down"),
    )

    async def _batch(bvids, strategy=None):
        """返回最小批量结果。"""
        return {"total_videos": 1, "successful_count": 1, "total_alerts": 0, "results": [], "monitored_at": "t"}

    monitor.monitor_multiple_videos = _batch

    result = _run(monitor.monitor_user_account("42"))

    assert result["nickname"] == ""
    assert result["username"] == ""
    assert result["videos"] == [{"bvid": "BV1"}]


def test_monitor_user_account_handles_empty_user_data() -> None:
    """用户资料结构缺失时昵称回落为空串。"""
    monitor, _ = _account_monitor(videos=[], user_info_result={})

    result = _run(monitor.monitor_user_account("42"))

    assert result["nickname"] == ""


def test_monitor_user_account_default_api_missing_method() -> None:
    """api 缺少 get_user_info 时应容错，不冒泡。"""
    monitor = _monitor()

    async def _get_user_videos(uid, limit):
        """返回空视频列表。"""
        return []

    monitor.collector._get_user_videos = _get_user_videos

    result = _run(monitor.monitor_user_account("42"))

    assert result["nickname"] == ""
    assert result["success"] is False
