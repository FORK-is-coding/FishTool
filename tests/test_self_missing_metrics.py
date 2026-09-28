"""自诊缺失指标口径测试（FishTool 03 · 批 4 · 规格 §5.2 / §5.3 / §5.6）。

覆盖：
- 有效样本均值 / 配对分母（不是全列表分母）
- 收藏率的同稿配对（不是各自有效集合均值相除）
- follower=None / =0 的伪 0 防线（unavailable / zero_denominator）
- metric_status 为 None / 空 dict / 缺 key / 非 dict 都不自动 ok
- 显式 ok 但 value=None / 非法 也不能参与均值或配对
"""
from __future__ import annotations

from typing import List

from modules.self_diagnosis.self_analyzer import (
    SelfAnalyzer,
    current_metric,
    paired_rate,
    summarize_metric,
)


class _FakeLimiter:
    """限频替身，直接放行。"""

    async def acquire(self, _level: str) -> None:
        """不等待直接返回。"""
        return None


class _FakeAPI:
    """纯计算测试用的最小 API 替身。"""

    BASE_URL = "https://api.bilibili.com"

    async def get_user_videos(self, _uid: int, page: int = 1, page_size: int = 50) -> dict:
        """返回空投稿占位，不使用网络。"""
        return {"data": {"list": {"vlist": []}, "page": {"count": 0}}}


def _analyzer() -> SelfAnalyzer:
    """构造仅用于纯计算的分析器，不触发网络。"""
    return SelfAnalyzer(_FakeAPI(), _FakeLimiter())


def test_avg_play_uses_valid_samples_not_full_list() -> None:
    """play=[100,None] -> avg_play=100、有效1/缺失1，不是 50。"""
    stats = _analyzer()._analyze_video_stats([
        {"play": 100, "metric_status": {"play": "ok"}},
        {"play": None, "metric_status": {"play": "missing"}},
    ])
    assert stats["avg_play"] == 100
    assert stats["coverage"]["play"] == {"valid_count": 1, "missing_count": 1}


def test_avg_favorite_excludes_missing_but_keeps_real_zero() -> None:
    """favorite=[10,None,0] -> 均值 5、有效 2，不是 10/3。"""
    stats = _analyzer()._analyze_video_stats([
        {"favorite": 10, "metric_status": {"favorite": "ok"}},
        {"favorite": None, "metric_status": {"favorite": "missing"}},
        {"favorite": 0, "metric_status": {"favorite": "ok"}},
    ])
    assert stats["avg_favorite"] == 5
    assert stats["coverage"]["favorite"] == {"valid_count": 2, "missing_count": 1}
    # 真实 0 仍算有效样本
    assert summarize_metric(
        [{"favorite": 0, "metric_status": {"favorite": "ok"}}], "favorite"
    )["valid_count"] == 1


def test_paired_favorite_rate_only_uses_same_video_pairs() -> None:
    """[(play100,fav10),(play900,favNone)] -> 收藏率 10%、配对 1，不是 1%。"""
    rate, status, pairs = paired_rate([
        {"play": 100, "favorite": 10, "metric_status": {"play": "ok", "favorite": "ok"}},
        {"play": 900, "favorite": None, "metric_status": {"play": "ok", "favorite": "missing"}},
    ], "favorite", "play")
    assert status == "ok"
    assert pairs == 1
    assert round(rate, 4) == 10.0


def test_failed_fetch_follower_ratio_unavailable() -> None:
    """follower=None（采集失败）-> 均播/粉丝比 None + unavailable。"""
    metrics = _analyzer()._calculate_engagement(
        [{"play": 100, "comment": 1, "favorite": 2,
          "metric_status": {"play": "ok", "comment": "ok", "favorite": "ok"}}],
        None,
    )
    assert metrics["play_to_fans_ratio"] is None
    assert metrics["play_to_fans_status"] == "unavailable"


def test_zero_follower_is_zero_denominator_not_zero_ratio() -> None:
    """follower=0 -> 均播/粉丝比 None + zero_denominator，不输出伪 0。"""
    metrics = _analyzer()._calculate_engagement(
        [{"play": 100, "metric_status": {"play": "ok"}}],
        0,
    )
    assert metrics["play_to_fans_ratio"] is None
    assert metrics["play_to_fans_status"] == "zero_denominator"


def test_comment_rate_zero_denominator_when_all_play_zero() -> None:
    """分母合计为 0 -> zero_denominator，不输出除零结果。"""
    rate, status, pairs = paired_rate([
        {"play": 0, "comment": 5, "metric_status": {"play": "ok", "comment": "ok"}},
    ], "comment", "play")
    assert rate is None
    assert status == "zero_denominator"
    assert pairs == 1


def test_metric_status_absent_is_not_ok() -> None:
    """None / 空 dict / 缺 key / 非 dict 都不自动 ok（验收 19）。"""
    assert current_metric({"play": 100}, "play") == (None, "unknown")
    assert current_metric({"play": 100, "metric_status": {}}, "play") == (None, "unknown")
    assert current_metric({"play": 100, "metric_status": {"comment": "ok"}}, "play") == (None, "unknown")
    assert current_metric({"play": 100, "metric_status": "ok"}, "play") == (None, "unknown")


def test_explicit_ok_with_invalid_value_is_not_effective() -> None:
    """显式 ok 但 value=None / 非法 也不能参与均值或配对（验收 19）。"""
    assert current_metric({"play": None, "metric_status": {"play": "ok"}}, "play") == (None, "missing")
    assert current_metric({"play": -3, "metric_status": {"play": "ok"}}, "play") == (None, "invalid")
    assert current_metric({"play": True, "metric_status": {"play": "ok"}}, "play") == (None, "invalid")
