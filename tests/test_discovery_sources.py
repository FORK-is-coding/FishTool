"""06 采集广度 · 三源读取与容错解析 fixture 单测（不联网）。

覆盖每个来源的：正常 / ``code != 0`` / ``list`` 为空 / 字段缺失 / 非法数值。
视频源无 ``heat_score`` 字段，等价地覆盖 ``view`` 非法（同为质量三态口径）。

仓库未安装 pytest-asyncio，async 用例统一用 ``asyncio.run(...)`` 驱动。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.exceptions import BilibiliAPIError
from core.request_budget import RequestBudgetExceeded
from modules.hotspot.discovery import contracts, sources

CAPTURED = 1_700_000_000

_FIXTURE_PATH = Path(__file__).resolve().parent / "data" / "discovery" / "sources_fixtures.json"
FIXTURES = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _fx(source: str, case: str) -> dict:
    """取某来源某场景的 fixture。"""
    return FIXTURES[source][case]


# ==========================================================================
# search/square
# ==========================================================================

class TestSearchSquare:
    def test_ok(self) -> None:
        """正常返回：两条均 ok，rank 从 1 开始，带采样身份。"""
        outcome = sources.parse_hot_keywords(_fx("search_square", "ok"), CAPTURED)
        assert outcome.state == contracts.STATE_OK
        assert outcome.returned_count == 2
        assert [item.keyword for item in outcome.items] == ["中低端显卡价格冲击历史新高", "新番开播"]
        assert all(item.heat_status == "ok" for item in outcome.items)
        assert outcome.items[0].rank == 1
        assert outcome.items[0].source == contracts.SOURCE_SEARCH_SQUARE
        assert outcome.items[0].captured_epoch_s == CAPTURED

    def test_code_nonzero_is_error_not_empty(self) -> None:
        """code != 0 记 error，不得当作「今天没有话题」。"""
        outcome = sources.parse_hot_keywords(_fx("search_square", "code_nonzero"), CAPTURED)
        assert outcome.state == contracts.STATE_ERROR
        assert outcome.items == []
        assert outcome.error_code == -352
        assert outcome.reason == "api_code_-352"

    def test_empty_list_is_ok_zero(self) -> None:
        """list 为空是真空榜：ok 且 0 条。"""
        outcome = sources.parse_hot_keywords(_fx("search_square", "empty"), CAPTURED)
        assert outcome.state == contracts.STATE_OK
        assert outcome.items == []
        assert outcome.returned_count == 0

    def test_missing_fields(self) -> None:
        """heat_score 缺失保留候选；空关键词 / 非 dict 跳过。"""
        outcome = sources.parse_hot_keywords(_fx("search_square", "missing_fields"), CAPTURED)
        assert outcome.state == contracts.STATE_OK
        assert [item.keyword for item in outcome.items] == ["缺失分数的词", "缺展示名但有分"]
        assert outcome.items[0].heat_score is None
        assert outcome.items[0].heat_status == "missing"
        assert outcome.items[1].heat_score == 100

    def test_bad_heat_skipped_and_no_zero(self) -> None:
        """非法 heat_score（负数/bool/文本/浮点）跳过，不写 0；真实 0 合法。"""
        outcome = sources.parse_hot_keywords(_fx("search_square", "bad_heat"), CAPTURED)
        assert [item.keyword for item in outcome.items] == ["合法零分", "合法正常"]
        assert outcome.items[0].heat_score == 0
        assert outcome.items[0].heat_status == "ok"
        # 被跳过的 4 条不得以任何形式出现（尤其不得写成 0）。
        skipped = {"负分", "布尔分", "文本分", "浮点分"}
        assert skipped.isdisjoint(item.keyword for item in outcome.items)

    def test_missing_code_is_contract_error(self) -> None:
        """envelope 缺 code 视为契约破坏，记 error。"""
        outcome = sources.parse_hot_keywords({"data": {"trending": {"list": []}}}, CAPTURED)
        assert outcome.state == contracts.STATE_ERROR
        assert outcome.reason == "missing_code"


# ==========================================================================
# popular
# ==========================================================================

class TestPopular:
    def test_ok(self) -> None:
        """正常返回：字段齐全，三套分类字段各自就位。"""
        outcome = sources.parse_popular(_fx("popular", "ok"), CAPTURED)
        assert outcome.state == contracts.STATE_OK
        assert outcome.returned_count == 2
        first = outcome.items[0]
        assert first.bvid == "BV1PoP0001"
        assert first.source == contracts.SOURCE_POPULAR
        assert first.position == 1
        assert first.legacy_tid == 4 and first.legacy_tid_status == "ok"
        assert first.tidv2 == 1004
        assert first.pid_v2 == 1000
        assert first.owner_mid == 101 and first.owner_status == "ok"
        assert first.view == 120000 and first.view_status == "ok"
        assert first.rcmd_reason == "热门推荐"

    def test_code_nonzero_is_error(self) -> None:
        outcome = sources.parse_popular(_fx("popular", "code_nonzero"), CAPTURED)
        assert outcome.state == contracts.STATE_ERROR
        assert outcome.items == []
        assert outcome.error_code == -509

    def test_empty_list_is_ok_zero(self) -> None:
        outcome = sources.parse_popular(_fx("popular", "empty"), CAPTURED)
        assert outcome.state == contracts.STATE_OK
        assert outcome.items == [] and outcome.returned_count == 0

    def test_missing_fields_kept_with_missing_status(self) -> None:
        """缺 owner/tid/view 保留候选并记 missing；缺 bvid 跳过。"""
        outcome = sources.parse_popular(_fx("popular", "missing_fields"), CAPTURED)
        assert [item.bvid for item in outcome.items] == ["BV1PoP0100", "BV1PoP0101"]
        zero = outcome.items[0]
        assert zero.legacy_tid is None and zero.legacy_tid_status == "missing"
        assert zero.owner_mid is None and zero.owner_status == "missing"
        assert zero.view is None and zero.view_status == "missing"
        # 缺 bvid 的那条不能被补成空串写库。
        assert all(item.bvid for item in outcome.items)

    def test_invalid_view_kept_but_never_zero(self) -> None:
        """view 非法保留候选、value=None/status=invalid；真实 0 保留为 0。"""
        outcome = sources.parse_popular(_fx("popular", "bad_view"), CAPTURED)
        assert len(outcome.items) == 6
        by_bvid = {item.bvid: item for item in outcome.items}
        for bvid in ("BV1PoP0200", "BV1PoP0201", "BV1PoP0202", "BV1PoP0203"):
            assert by_bvid[bvid].view is None
            assert by_bvid[bvid].view_status == "invalid"
        assert by_bvid["BV1PoP0204"].view == 0
        assert by_bvid["BV1PoP0205"].view == 999


# ==========================================================================
# ranking/v2?rid=0
# ==========================================================================

class TestRanking:
    def test_ok_keeps_others(self) -> None:
        """others 单独标来源落候选，不得丢掉。"""
        outcome = sources.parse_ranking(_fx("ranking", "ok"), CAPTURED)
        assert outcome.state == contracts.STATE_OK
        assert outcome.returned_count == 2
        assert outcome.others_count == 3
        assert len(outcome.items) == 5
        others = [item for item in outcome.items if item.source == contracts.SOURCE_RANKING_ALL_OTHERS]
        assert [item.bvid for item in others] == ["BV1Rank001o1", "BV1Rank001o2", "BV1Rank002o1"]
        assert [item.bvid for item in outcome.items if item.source == contracts.SOURCE_RANKING_ALL] == ["BV1Rank001", "BV1Rank002"]

    def test_code_nonzero_is_error(self) -> None:
        outcome = sources.parse_ranking(_fx("ranking", "code_nonzero"), CAPTURED)
        assert outcome.state == contracts.STATE_ERROR
        assert outcome.error_code == -352
        assert outcome.items == []

    def test_empty_list_is_ok_zero(self) -> None:
        outcome = sources.parse_ranking(_fx("ranking", "empty"), CAPTURED)
        assert outcome.state == contracts.STATE_OK
        assert outcome.items == [] and outcome.others_count == 0

    def test_missing_fields(self) -> None:
        """主条目/others 字段不全时保留候选并记 missing；others 非 list 忽略。"""
        outcome = sources.parse_ranking(_fx("ranking", "missing_fields"), CAPTURED)
        assert outcome.state == contracts.STATE_OK
        assert [item.bvid for item in outcome.items] == [
            "BV1Rank100", "BV1Rank101", "BV1Rank102", "BV1Rank103"
        ]
        assert outcome.others_count == 1  # BV1Rank103 的 others 原始 1 条（缺 bvid 被跳过）
        assert all(item.source == contracts.SOURCE_RANKING_ALL for item in outcome.items)


# ==========================================================================
# fetch 层：envelope 标准化 + 预算钩子
# ==========================================================================

class _BudgetSpy:
    """记录记账钩子调用（域 / 类别）。"""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, *, domain=None, category=None, **kwargs) -> None:
        self.calls.append({"domain": domain, "category": category})


class _StubAPI:
    """离线 API 替身：记录调用，返回预设已拆数据或抛预设异常。"""

    def __init__(self, *, get_result=None, get_error=None) -> None:
        self._get_result = get_result
        self._get_error = get_error
        self.calls = []

    async def get(self, url, params=None, **kwargs):
        """模拟 ``api.get``：返回已拆外层 data，并记录额外 kwargs（如 headers）。"""
        self.calls.append(("get", url, dict(params or {}), dict(kwargs)))
        if self._get_error is not None:
            raise self._get_error
        return self._get_result


def test_fetch_hot_keywords_wraps_data_and_uses_discovery_budget() -> None:
    """api.get 返回已拆 data，fetch 标准化成 envelope；记账用 no_cookie/discovery。"""
    spy = _BudgetSpy()
    api = _StubAPI(get_result={"trending": {"list": []}})
    envelope = asyncio.run(sources.fetch_hot_keywords(api, budget_hook=spy))
    assert envelope == {"code": 0, "message": "", "data": {"trending": {"list": []}}}
    assert spy.calls == [{"domain": "no_cookie", "category": "discovery"}]
    assert api.calls[0][1] == sources.SEARCH_SQUARE_URL
    assert api.calls[0][2] == {"limit": 10}


def test_fetch_hot_keywords_maps_business_error_to_code() -> None:
    """client 业务异常 -> 提取业务码写入 envelope，不当作空榜。"""
    spy = _BudgetSpy()
    api = _StubAPI(get_error=BilibiliAPIError("API错误 [-352]: 风控"))
    envelope = asyncio.run(sources.fetch_hot_keywords(api, budget_hook=spy))
    assert envelope["code"] == -352
    assert envelope["data"] is None


def test_fetch_popular_page_params() -> None:
    spy = _BudgetSpy()
    api = _StubAPI(get_result={"list": []})
    asyncio.run(sources.fetch_popular_page(api, page=2, ps=20, budget_hook=spy))
    assert api.calls[0][1] == sources.POPULAR_URL
    assert api.calls[0][2] == {"ps": 20, "pn": 2}


def test_fetch_ranking_uses_bare_spec_url() -> None:
    """ranking 复用同一 client 的 api.get，参数严格 §3.3（不带 day/pn）；记账 no_cookie/ranking。"""
    spy = _BudgetSpy()
    api = _StubAPI(get_result={"list": [{"bvid": "BV1"}]})
    envelope = asyncio.run(sources.fetch_ranking(api, rid=0, day=7, budget_hook=spy))
    assert envelope["code"] == 0
    assert envelope["data"] == {"list": [{"bvid": "BV1"}]}
    assert api.calls[0] == (
        "get",
        sources.RANKING_URL,
        {"rid": 0, "type": "all"},
        {"headers": {"Referer": sources.RANKING_REFERER}},
    )
    assert spy.calls == [{"domain": "no_cookie", "category": "ranking"}]


def test_fetch_ranking_maps_business_error() -> None:
    """风控异常（不带方括号的码）也能提取业务码。"""
    spy = _BudgetSpy()
    api = _StubAPI(get_error=BilibiliAPIError("请求被风控: -352"))
    envelope = asyncio.run(sources.fetch_ranking(api, budget_hook=spy))
    assert envelope["code"] == -352
    assert envelope["data"] is None


def test_budget_exhaustion_propagates() -> None:
    """配额耗尽必须原样抛出，不能被吞成「请求失败」。"""

    def _exhausted(**kwargs):
        raise RequestBudgetExceeded("category:discovery")

    api = _StubAPI(get_result={})
    with pytest.raises(RequestBudgetExceeded):
        asyncio.run(sources.fetch_hot_keywords(api, budget_hook=_exhausted))
