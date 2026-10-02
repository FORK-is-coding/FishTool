"""06 采集广度 · service 调度 / 共享缓存 / 去重落库 / 快照四态 单测（不联网）。

仓库未安装 pytest-asyncio，async 用例统一用 ``asyncio.run(...)`` 驱动。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from core.database import DatabaseManager, HotKeywordSignal, HotspotSignal
from core.exceptions import BilibiliAPIError
from core.request_budget import RequestBudgetExceeded
from modules.hotspot.discovery.service import (
    DiscoverRunConfig,
    DiscoveryPollCache,
    DiscoveryService,
)
from modules.hotspot.discovery.snapshot import DiscoverySnapshotStore
from modules.hotspot.discovery.store import KeywordSignalStore, VideoSignalStore

CAPTURED = int(time.time()) - 60  # 贴近当前时间，避免 30 天滚动裁剪误删
_FIXTURE_PATH = Path(__file__).resolve().parent / "data" / "discovery" / "sources_fixtures.json"
FIXTURES = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))

POPULAR_PAGE2 = {
    "code": 0,
    "message": "0",
    "data": {"list": [{"bvid": "BV1PoP1001", "tid": 4, "owner": {"mid": 1}, "stat": {"view": 10}}]},
}


def _strip(envelope: dict) -> dict:
    """模拟 real client：``api.get()`` / ``get_ranking()`` 返回已拆外层 data。"""
    return envelope["data"]


class _StubAPI:
    """离线 API 替身：记录调用，返回已拆 data 或抛业务异常。"""

    def __init__(self) -> None:
        self.keyword = FIXTURES["search_square"]["ok"]
        self.keyword_error = None
        self.popular = {1: FIXTURES["popular"]["ok"], 2: POPULAR_PAGE2}
        self.popular_error = {}
        self.ranking = FIXTURES["ranking"]["ok"]
        self.ranking_error = None
        self.get_calls = []

    async def get(self, url, params=None, **kwargs):
        params = dict(params or {})
        self.get_calls.append((url, params))
        if url.endswith("search/square"):
            if self.keyword_error is not None:
                raise self.keyword_error
            return _strip(self.keyword)
        if url.endswith("popular"):
            page = int(params.get("pn", 1))
            error = self.popular_error.get(page)
            if error is not None:
                raise error
            return _strip(self.popular[page])
        if url.endswith("ranking/v2"):
            if self.ranking_error is not None:
                raise self.ranking_error
            return _strip(self.ranking)
        raise AssertionError(f"unexpected url: {url}")


class _BudgetSpy:
    """记录记账钩子的 (domain, category)。"""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, *, domain=None, category=None, **kwargs) -> None:
        self.calls.append((domain, category))


def _build_service(tmp_path, manager, api, budget_hook):
    """在临时库 + 临时快照上构造服务。"""
    return DiscoveryService(
        api,
        cache=DiscoveryPollCache(),
        snapshot_store=DiscoverySnapshotStore(tmp_path / "snapshot.json"),
        keyword_store=KeywordSignalStore(manager.get_session),
        video_store=VideoSignalStore(manager.get_session),
        config=DiscoverRunConfig(),
        clock=lambda: CAPTURED,
        budget_hook=budget_hook,
    )


@pytest.fixture()
def env(tmp_path):
    """构造隔离临时库 + 服务 + 桩 API + 记账探针。"""
    manager = DatabaseManager(str(tmp_path / "discovery.db"))
    api = _StubAPI()
    spy = _BudgetSpy()
    service = _build_service(tmp_path, manager, api, spy)
    return service, manager, api, spy, tmp_path


def _keywords(manager: DatabaseManager):
    """读取全部关键词行。"""
    session = manager.get_session()
    try:
        return session.query(HotKeywordSignal).all()
    finally:
        session.close()


def _signals(manager: DatabaseManager):
    """读取全部视频信号行。"""
    session = manager.get_session()
    try:
        return session.query(HotspotSignal).all()
    finally:
        session.close()


# --------------------------------------------------------------------------
# 落库与冻结
# --------------------------------------------------------------------------

def test_poll_once_writes_and_freezes_snapshot(env) -> None:
    """一轮 poll：三源 ok、关键词与视频信号落库、others 单列、快照冻结。"""
    service, manager, api, spy, _ = env
    snapshot = asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))

    assert snapshot["sources"]["search_square"]["state"] == "ok"
    assert snapshot["sources"]["popular"]["state"] == "ok"
    assert snapshot["sources"]["ranking_all"]["state"] == "ok"
    assert snapshot["sources"]["ranking_all"]["others_count"] == 3
    assert snapshot["keyword_count"] == 2
    # popular(2 + 1 页) + ranking(2 主 + 3 others) = 8 个不同 bvid。
    assert snapshot["video_count"] == 8
    assert snapshot["written_keywords"] == 2
    assert snapshot["written_videos"] == 8

    assert {row.keyword for row in _keywords(manager)} == {"中低端显卡价格冲击历史新高", "新番开播"}
    signals = _signals(manager)
    assert len(signals) == 8
    others_rows = [row for row in signals if "ranking_all_others" in row.value["payload"]["sources"]]
    assert len(others_rows) == 3

    latest = service.snapshot_store.latest()
    assert latest["snapshot_id"] == snapshot["snapshot_id"]
    assert service.latest_sources_state()["popular"]["from_cache"] is False


def test_budget_hook_uses_declared_domains(env) -> None:
    """search/popular 走 no_cookie+discovery；ranking 走 no_cookie+ranking（06 全体免 Cookie 域）。"""
    service, manager, api, spy, _ = env
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))
    assert spy.calls.count(("no_cookie", "discovery")) == 3  # search 1 + popular 2 页
    assert spy.calls.count(("no_cookie", "ranking")) == 1


def test_replay_same_snapshot_is_idempotent(env) -> None:
    """同一 (参数, 采样时刻) 重放：短路返回既有快照，库内条数不变。"""
    service, manager, api, spy, _ = env
    first = asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))
    calls_after_first = len(api.get_calls)

    second = asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))
    assert second.get("replayed") is True
    assert second["snapshot_id"] == first["snapshot_id"]
    assert len(api.get_calls) == calls_after_first
    assert len(_keywords(manager)) == 2
    assert len(_signals(manager)) == 8


# --------------------------------------------------------------------------
# 失败状态四态区分
# --------------------------------------------------------------------------

def test_source_error_recorded_not_as_empty(env) -> None:
    """code != 0 -> 快照记 error，绝不落成「零条信号」。"""
    service, manager, api, spy, _ = env
    api.keyword_error = BilibiliAPIError("API错误 [-352]: 风控")
    snapshot = asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))
    source = snapshot["sources"]["search_square"]
    assert source["state"] == "error"
    assert source["error_code"] == -352
    assert source["returned_count"] == 0
    assert snapshot["keyword_count"] == 0
    assert _keywords(manager) == []


def test_popular_partial_keeps_first_page(env) -> None:
    """popular 第二页失败：保留第一页数据并显式标 partial。"""
    service, manager, api, spy, _ = env
    api.popular_error = {2: BilibiliAPIError("API错误 [-509]: 请求过于频繁")}
    api.ranking = FIXTURES["ranking"]["empty"]
    snapshot = asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))
    source = snapshot["sources"]["popular"]
    assert source["state"] == "partial"
    assert source["returned_count"] == 2
    assert [page["state"] for page in source["pages"]] == ["ok", "error"]
    assert snapshot["video_count"] == 2


def test_empty_ranking_is_ok_zero_distinct_from_error(env) -> None:
    """真空榜 = ok + 0 条；与 error 的 0 条可区分。"""
    service, manager, api, spy, _ = env
    api.ranking = FIXTURES["ranking"]["empty"]
    source = asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))["sources"]["ranking_all"]
    assert source["state"] == "ok"
    assert source["returned_count"] == 0
    assert source["error_code"] == 0


def test_quota_exhausted_records_error_without_signals(tmp_path) -> None:
    """配额耗尽：该源记 error，不写任何信号，也不伪装成空榜。"""
    manager = DatabaseManager(str(tmp_path / "quota.db"))
    api = _StubAPI()

    def _exhausted(**kwargs):
        raise RequestBudgetExceeded("category:discovery")

    service = _build_service(tmp_path, manager, api, _exhausted)
    snapshot = asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))
    assert snapshot["sources"]["search_square"]["state"] == "error"
    assert snapshot["sources"]["search_square"]["reason"] == "quota_exceeded"
    assert snapshot["keyword_count"] == 0
    assert snapshot["video_count"] == 0
    assert _keywords(manager) == []
    assert _signals(manager) == []


# --------------------------------------------------------------------------
# 共享轮询缓存（不许每个事件各发一遍）
# --------------------------------------------------------------------------

def test_shared_cache_avoids_second_http(env) -> None:
    """同一轮 TTL 内第二次 poll 命中共享缓存，不再发 HTTP，并标 from_cache。"""
    service, manager, api, spy, _ = env
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))
    get_calls = len(api.get_calls)

    snapshot = asyncio.run(service.poll_once(captured_epoch_s=CAPTURED + 5))
    assert len(api.get_calls) == get_calls  # 三源（含 ranking）都没再发 HTTP
    assert snapshot["served_from_cache"] is True
    assert snapshot["sources"]["search_square"]["from_cache"] is True
    assert snapshot["sources"]["search_square"]["cache_age_s"] == 5
    # 读端可识别「当前展示的是上轮缓存」。
    assert service.latest_sources_state()["ranking_all"]["from_cache"] is True


# --------------------------------------------------------------------------
# heat_score 缺失 / 非法口径
# --------------------------------------------------------------------------

def test_invalid_heat_never_written_as_zero(env) -> None:
    """非法 heat_score 不落库；真实 0 合法保留。"""
    service, manager, api, spy, _ = env
    api.keyword = FIXTURES["search_square"]["bad_heat"]
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))
    rows = {row.keyword: row.heat_score for row in _keywords(manager)}
    assert rows == {"合法零分": 0, "合法正常": 12345}


def test_missing_heat_kept_with_null(env) -> None:
    """heat_score 缺失保留候选，score 写 NULL + status missing。"""
    service, manager, api, spy, _ = env
    api.keyword = FIXTURES["search_square"]["missing_fields"]
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))
    rows = {row.keyword: row for row in _keywords(manager)}
    assert rows["缺失分数的词"].heat_score is None
    assert rows["缺失分数的词"].heat_status == "missing"
    assert rows["缺展示名但有分"].heat_score == 100
