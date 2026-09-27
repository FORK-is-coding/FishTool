"""数据质量守卫测试。

覆盖本批改动引入的质量标记，防止两类问题回归：

- 接口字段缺失被静默当成真实 0（播放量与六项互动量同源同待遇）
- 投稿分页残缺却把数据维度标记为已获取

覆盖范围：
- hotspot.collector._read_stat_int 的取值边界
- hotspot.collector._classify_stat_quality 的三档完整度判定
- self_diagnosis._analyze_video_stats 的口径标识
- self_diagnosis._resolve_video_list_complete 的判定语义
- self_diagnosis._fetch_all_videos 的分页完整性与 count 缺失不早停
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from core.exceptions import BilibiliAPIError
from modules.hotspot.collector import HotspotCollector
from modules.self_diagnosis.self_analyzer import SelfAnalyzer


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------


class _FakeLimiter:
    """限频替身，直接放行。"""

    async def acquire(self, _level: str) -> None:
        """不等待，直接返回。"""
        return None


class _FakeAPI:
    """按页返回预置响应的 B站接口替身。"""

    def __init__(self, pages: List[Dict[str, Any]], fail_on_call: int | None = None) -> None:
        """记录预置页数据与需要抛错的第几次调用。

        Args:
            pages: 按页顺序排列的响应字典。
            fail_on_call: 第几次调用时抛 BilibiliAPIError；None 表示不抛。
        """
        self.pages = pages
        self.fail_on_call = fail_on_call
        self.calls = 0

    async def get_user_videos(
        self, _uid: int, page: int = 1, page_size: int = 50
    ) -> Dict[str, Any]:
        """返回预置页；命中失败次数时抛 BilibiliAPIError。"""
        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise BilibiliAPIError("page blocked")
        if page <= len(self.pages):
            return self.pages[page - 1]
        return {"data": {"list": {"vlist": []}, "page": {"count": 0}}}


def _page(vlist: List[Dict[str, Any]], count: int | None) -> Dict[str, Any]:
    """构造一页投稿响应，count 为 None 时模拟接口不返回总数。"""
    page_info = {} if count is None else {"count": count}
    return {"data": {"list": {"vlist": vlist}, "page": page_info}}


def _videos(start: int, amount: int) -> List[Dict[str, Any]]:
    """生成连续编号的投稿占位数据。"""
    return [{"bvid": f"BV{start + i}", "aid": start + i} for i in range(amount)]


def _analyzer() -> SelfAnalyzer:
    """构造仅用于纯计算的分析器，不触发网络请求。"""
    return SelfAnalyzer(_FakeAPI([]), _FakeLimiter())


# --------------------------------------------------------------------------
# collector._read_stat_int
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 0),
        (1234, 1234),
        (None, None),
        (-5, None),
        (True, None),
        ("", None),
        ("abc", None),
        ("123", 123),
        (" 123 ", 123),
        (1.5, None),
    ],
)
def test_read_stat_int_boundaries(raw: Any, expected: int | None) -> None:
    """接口统计值只认非负整数，其余一律视为未拿到。"""
    assert HotspotCollector._read_stat_int({"view": raw}, "view") == expected


def test_read_stat_int_missing_key_returns_none() -> None:
    """键不存在时不得当成 0。"""
    assert HotspotCollector._read_stat_int({}, "view") is None


# --------------------------------------------------------------------------
# collector._classify_stat_quality
# --------------------------------------------------------------------------

_ALL_STAT: Dict[str, Any] = {
    "view": 100,
    "danmaku": 1,
    "reply": 2,
    "favorite": 3,
    "coin": 4,
    "share": 5,
    "like": 6,
}


def test_classify_stat_quality_all_present() -> None:
    """七项齐全时播放量与整条统计都算完整。"""
    assert HotspotCollector._classify_stat_quality(dict(_ALL_STAT)) == ("ok", "ok")


def test_classify_stat_quality_partial_when_interaction_missing() -> None:
    """互动量缺失时播放量仍有效，但整条统计降为部分缺失。"""
    stat = dict(_ALL_STAT)
    stat.pop("like")
    assert HotspotCollector._classify_stat_quality(stat) == ("ok", "partial")


def test_classify_stat_quality_all_missing() -> None:
    """全部缺失时整条统计不可用。"""
    assert HotspotCollector._classify_stat_quality({}) == ("missing", "missing")


def test_classify_stat_quality_view_missing_alone() -> None:
    """播放量单独缺失必须被标记，不能让 0 冒充真实值。"""
    stat = dict(_ALL_STAT)
    stat["view"] = None
    assert HotspotCollector._classify_stat_quality(stat) == ("missing", "partial")


def test_classify_stat_quality_negative_view_is_missing() -> None:
    """非法负值不得当作有效播放量。"""
    stat = dict(_ALL_STAT)
    stat["view"] = -1
    assert HotspotCollector._classify_stat_quality(stat)[0] == "missing"


# --------------------------------------------------------------------------
# self_analyzer._analyze_video_stats
# --------------------------------------------------------------------------


def test_analyze_video_stats_carries_scope_label() -> None:
    """均值必须带全历史口径标识，避免被误读为近期数据。"""
    stats = _analyzer()._analyze_video_stats(
        [
            {"play": 100, "comment": 1, "favorite": 2},
            {"play": 300, "comment": 3, "favorite": 4},
        ]
    )
    assert stats["stats_scope"] == "all_history"
    assert stats["stats_scope_label"]
    assert stats["avg_play"] == 200


def test_analyze_video_stats_empty_result_keeps_scope_and_avg_favorite() -> None:
    """空投稿也要返回口径标识，并补全原先遗漏的 avg_favorite。"""
    stats = _analyzer()._analyze_video_stats([])
    assert stats["stats_scope"] == "all_history"
    assert "avg_favorite" in stats
    assert stats["avg_favorite"] == 0


# --------------------------------------------------------------------------
# self_analyzer._resolve_video_list_complete
# --------------------------------------------------------------------------


def test_resolve_video_list_complete_without_evidence_does_not_fail() -> None:
    """无完整性依据时不得判定失败。"""
    assert SelfAnalyzer._resolve_video_list_complete(None) is True


def test_resolve_video_list_complete_follows_meta() -> None:
    """有依据时严格按 complete 字段判定。"""
    assert SelfAnalyzer._resolve_video_list_complete({"complete": True}) is True
    assert SelfAnalyzer._resolve_video_list_complete({"complete": False}) is False
    assert SelfAnalyzer._resolve_video_list_complete({}) is False


# --------------------------------------------------------------------------
# self_analyzer._fetch_all_videos
# --------------------------------------------------------------------------


def test_fetch_all_videos_marks_complete_when_count_reached() -> None:
    """采满接口声明总数时判定为完整。"""
    api = _FakeAPI([_page(_videos(0, 50), 50)])
    analyzer = SelfAnalyzer(api, _FakeLimiter())

    videos = asyncio.run(analyzer._fetch_all_videos(42))

    meta = analyzer._last_video_fetch_meta
    assert len(videos) == 50
    assert meta["expected_total"] == 50
    assert meta["complete"] is True


def test_fetch_all_videos_does_not_stop_early_when_count_missing() -> None:
    """接口不给 count 时不得在第一页后就停手。"""
    api = _FakeAPI(
        [
            _page(_videos(0, 2), None),
            _page(_videos(2, 2), None),
            _page([], None),
        ]
    )
    analyzer = SelfAnalyzer(api, _FakeLimiter())

    videos = asyncio.run(analyzer._fetch_all_videos(42))

    assert len(videos) == 4
    assert api.calls == 3


def test_fetch_all_videos_marks_incomplete_on_api_error() -> None:
    """中途接口异常时必须标记为不完整，同时保留已采部分。"""
    api = _FakeAPI(
        [_page(_videos(0, 3), 10), _page(_videos(3, 3), 10)],
        fail_on_call=2,
    )
    analyzer = SelfAnalyzer(api, _FakeLimiter())

    videos = asyncio.run(analyzer._fetch_all_videos(42))

    meta = analyzer._last_video_fetch_meta
    assert len(videos) == 3
    assert meta["truncated_by_error"] is True
    assert meta["complete"] is False
    assert SelfAnalyzer._resolve_video_list_complete(meta) is False


def test_fetch_all_videos_marks_incomplete_when_count_not_reached() -> None:
    """实际条数少于接口声明总数时必须标记为不完整。"""
    api = _FakeAPI([_page(_videos(0, 3), 10), _page([], 10)])
    analyzer = SelfAnalyzer(api, _FakeLimiter())

    asyncio.run(analyzer._fetch_all_videos(42))

    meta = analyzer._last_video_fetch_meta
    assert meta["expected_total"] == 10
    assert meta["fetched_count"] == 3
    assert meta["complete"] is False
