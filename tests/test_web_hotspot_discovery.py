"""web.routers.hotspot.routes_discovery 首屏只读端点测试（FishTool 04 · R5 第一批）。

覆盖对象：
- GET  /api/hotspot/discovery/latest      -> get_discovery_latest
- POST /api/hotspot/discovery/research_draft -> create_research_draft

验证维度（对齐 R5 §6 验收与硬口径）：
- 端点接线注册；
- 无快照时返回 ``snapshot: null``（不伪造空对象），前端有占位「尚未跑过发现轮」；
- 空 watch 池下首屏仍有内容；
- 端点走缓存、**零 HTTP**（多次调用不改动桩 API 调用次数）；
- ``state=error``（异常）与 ``item_count=0``（真实的空）两个独立用例；
- 单源失败不清空其他源展示；
- ``served_from_cache`` 透出；
- ``sources`` 逐源透出、**不合并成单一 state**；
- ``conflict=True`` 多来源冲突真的出现，且展示值取优先级最高来源（非按播放量挑）；
- 未知字段为 ``null``（不是 0）；
- 低成本研究草案复用降级模板路径且零新增采集。

测试策略：
- SQLite 全 tmp 隔离（DatabaseManager 写入 tmp_path），绝不触碰仓库 data/。
- 快照文件亦落在 tmp_path，绝不覆盖仓库 data/hotspot/discovery_snapshot.json。
- 用真实 ``DiscoveryService`` + 离线桩 API 驱动一轮 ``poll_once``，再经 TestClient 调真实 router。
- 仓库未安装 pytest-asyncio，async 用例统一 ``asyncio.run(...)`` 驱动。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import DatabaseManager, HotspotWatch
from core.exceptions import BilibiliAPIError
from modules.hotspot.discovery.service import (
    DiscoverRunConfig,
    DiscoveryPollCache,
    DiscoveryService,
)
from modules.hotspot.discovery.snapshot import DiscoverySnapshotStore
from modules.hotspot.discovery.store import KeywordSignalStore, VideoSignalStore
from web.routers import hotspot
from web.routers.hotspot import routes_discovery

CAPTURED = int(time.time()) - 60  # 贴近当前时间，避免 30 天滚动裁剪误删
_FIXTURE_PATH = Path(__file__).resolve().parent / "data" / "discovery" / "sources_fixtures.json"
FIXTURES = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))

# 同 bvid 从 popular + ranking 两来源被看到、但 view 不一致 -> conflict=True。
_CONFLICT_POPULAR = {
    "code": 0,
    "message": "0",
    "data": {
        "list": [
            {"bvid": "BV1CONFLICT1", "tid": 4, "owner": {"mid": 1}, "stat": {"view": 100},
             "tidv2": 1004, "pid_v2": 1000}
        ],
        "no_more": True,
    },
}
_CONFLICT_RANKING = {
    "code": 0,
    "message": "0",
    "data": {
        "list": [
            {"bvid": "BV1CONFLICT1", "tid": 4, "owner": {"mid": 1}, "stat": {"view": 999},
             "tidv2": 1004, "pid_v2": 1000, "others": []}
        ]
    },
}


def _strip(envelope: dict) -> dict:
    """模拟 real client：``api.get()`` 返回已拆外层 data。"""
    return envelope["data"]


class _StubAPI:
    """离线 API 替身：记录调用，返回已拆 data 或抛业务异常（照 test_discovery_service）。"""

    def __init__(self) -> None:
        """初始化三源可替换 envelope 与逐源错误注入位。"""
        self.keyword = FIXTURES["search_square"]["ok"]
        self.keyword_error = None
        self.popular = {1: FIXTURES["popular"]["ok"], 2: {"code": 0, "message": "0", "data": {"list": [], "no_more": True}}}
        self.popular_error = {}
        self.ranking = FIXTURES["ranking"]["ok"]
        self.ranking_error = None
        self.get_calls = []

    async def get(self, url, params=None, **kwargs):
        """按 URL 路由到对应 envelope；命中错误注入位则抛业务异常。"""
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


def _no_budget(**kwargs) -> None:
    """空记账钩子：测试不关心配额域。"""


def _build_service(tmp_path, manager, api) -> DiscoveryService:
    """在临时库 + 临时快照上构造真实发现服务（不联网）。"""
    return DiscoveryService(
        api,
        cache=DiscoveryPollCache(),
        snapshot_store=DiscoverySnapshotStore(tmp_path / "snapshot.json"),
        keyword_store=KeywordSignalStore(manager.get_session),
        video_store=VideoSignalStore(manager.get_session),
        config=DiscoverRunConfig(),
        clock=lambda: CAPTURED,
        budget_hook=_no_budget,
    )


@pytest.fixture()
def make_client(monkeypatch):
    """返回一个把给定服务注入 routes_discovery 的 TestClient 工厂。"""

    def _make(service) -> TestClient:
        monkeypatch.setattr(routes_discovery, "_resolve_discovery_service", lambda: service)
        app = FastAPI()
        app.include_router(hotspot.router, prefix="/api/hotspot")
        return TestClient(app)

    return _make


def _snapshot(client: TestClient):
    """取 GET /discovery/latest 的 data.snapshot。"""
    response = client.get("/api/hotspot/discovery/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    return body["data"]["snapshot"]


# ---------------------------------------------------------------------------
# 接线注册
# ---------------------------------------------------------------------------


def test_discovery_endpoints_registered_in_hotspot_router() -> None:
    """GET /discovery/latest 与 POST /discovery/research_draft 已接线注册。"""
    methods_by_path: dict[str, set] = {}
    for route in hotspot.router.routes:
        methods_by_path.setdefault(route.path, set()).update(
            getattr(route, "methods", set()) or set()
        )
    assert "GET" in methods_by_path.get("/discovery/latest", set())
    assert "POST" in methods_by_path.get("/discovery/research_draft", set())


def test_routes_discovery_has_no_network_dependency() -> None:
    """静态护栏：routes_discovery 不引入任何网络客户端 / 采集入口。"""
    source = inspect.getsource(routes_discovery)
    for token in ("BilibiliAPI", "get_api", "httpx", "requests.", "aiohttp", "urlopen"):
        assert token not in source


# ---------------------------------------------------------------------------
# 无快照 -> null（不伪造空对象）
# ---------------------------------------------------------------------------


def test_no_snapshot_returns_null_snapshot(tmp_path, make_client) -> None:
    """从未跑过发现轮：data.snapshot 必须为 null，且不夹带伪造字段。"""
    manager = DatabaseManager(str(tmp_path / "empty.db"))
    service = _build_service(tmp_path, manager, _StubAPI())
    client = make_client(service)

    body = client.get("/api/hotspot/discovery/latest").json()
    assert body["success"] is True
    assert body["data"]["snapshot"] is None
    # 不返回 {} / 0 之类的伪造结构：data 只应该有一个 snapshot 键。
    assert set(body["data"].keys()) == {"snapshot"}


# ---------------------------------------------------------------------------
# 空 watch 池首屏有内容
# ---------------------------------------------------------------------------


def test_first_screen_has_content_with_empty_watch_pool(tmp_path, make_client) -> None:
    """空 watch 池（库内 hotspot_watch 为 0 行）下，首屏仍返回视频与热搜词。"""
    manager = DatabaseManager(str(tmp_path / "first.db"))
    service = _build_service(tmp_path, manager, _StubAPI())
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))

    client = make_client(service)
    snapshot = _snapshot(client)

    assert snapshot is not None
    assert snapshot["video_count"] == len(snapshot["videos"]) > 0
    assert snapshot["keyword_count"] == len(snapshot["keywords"]) > 0

    session = manager.get_session()
    try:
        assert session.query(HotspotWatch).count() == 0  # watch 池确实是空的
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 端点走缓存、零 HTTP
# ---------------------------------------------------------------------------


def test_endpoint_uses_cache_and_makes_zero_http(tmp_path, make_client) -> None:
    """端点只读：多次调用不改动桩 API 调用次数；上轮缓存时 served_from_cache=True。"""
    manager = DatabaseManager(str(tmp_path / "cache.db"))
    api = _StubAPI()
    service = _build_service(tmp_path, manager, api)
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))
    # 第二轮同 TTL 内命中共享轮询缓存：整轮 served_from_cache=True。
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED + 5))

    calls_before = len(api.get_calls)
    client = make_client(service)
    for _ in range(3):
        snapshot = _snapshot(client)
        assert snapshot["served_from_cache"] is True
    assert len(api.get_calls) == calls_before  # 端点零 HTTP


# ---------------------------------------------------------------------------
# state=error 与 item_count=0 是两个独立用例
# ---------------------------------------------------------------------------


def test_source_error_surfaced_as_anomaly(tmp_path, make_client) -> None:
    """接口失败（state=error）如实透出 error_code，且不被伪装成空榜。"""
    manager = DatabaseManager(str(tmp_path / "err.db"))
    api = _StubAPI()
    api.keyword_error = BilibiliAPIError("API错误 [-352]: 风控")
    service = _build_service(tmp_path, manager, api)
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))

    snapshot = _snapshot(make_client(service))
    source = snapshot["sources"]["search_square"]
    assert source["state"] == "error"
    assert source["error_code"] == -352
    assert snapshot["keyword_count"] == 0


def test_empty_source_is_ok_with_zero_item_count(tmp_path, make_client) -> None:
    """真实的空榜：state=ok 且 item_count=0（与 error 区分）。"""
    manager = DatabaseManager(str(tmp_path / "empty2.db"))
    api = _StubAPI()
    api.ranking = FIXTURES["ranking"]["empty"]
    service = _build_service(tmp_path, manager, api)
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))

    snapshot = _snapshot(make_client(service))
    source = snapshot["sources"]["ranking_all"]
    assert source["state"] == "ok"       # 不是 error
    assert source["item_count"] == 0
    assert source["returned_count"] == 0


# ---------------------------------------------------------------------------
# 单源失败不清空其他源展示
# ---------------------------------------------------------------------------


def test_single_source_failure_does_not_clear_others(tmp_path, make_client) -> None:
    """popular 全页失败时，ranking / search 的展示不被清空。"""
    manager = DatabaseManager(str(tmp_path / "partial.db"))
    api = _StubAPI()
    api.popular_error = {1: BilibiliAPIError("boom"), 2: BilibiliAPIError("boom")}
    service = _build_service(tmp_path, manager, api)
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))

    snapshot = _snapshot(make_client(service))
    assert snapshot["sources"]["popular"]["state"] == "error"
    assert snapshot["sources"]["ranking_all"]["state"] == "ok"
    assert snapshot["sources"]["search_square"]["state"] == "ok"
    assert snapshot["video_count"] > 0     # ranking 视频仍在
    assert snapshot["keyword_count"] > 0   # 热搜词仍在


# ---------------------------------------------------------------------------
# sources 不合并成单一 state
# ---------------------------------------------------------------------------


def test_sources_are_per_source_not_merged(tmp_path, make_client) -> None:
    """sources 逐源透出，每个来源各自带 state/item_count；顶层无合并 state。"""
    manager = DatabaseManager(str(tmp_path / "sources.db"))
    service = _build_service(tmp_path, manager, _StubAPI())
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))

    snapshot = _snapshot(make_client(service))
    assert isinstance(snapshot["sources"], dict)
    assert set(snapshot["sources"]) == {"search_square", "popular", "ranking_all"}
    for source in snapshot["sources"].values():
        assert {"state", "item_count", "returned_count"} <= set(source)
    assert "state" not in snapshot  # 没有前端/后端合并出来的单一 state


# ---------------------------------------------------------------------------
# conflict=True 真的出现，展示值取优先级最高来源
# ---------------------------------------------------------------------------


def test_conflict_flag_and_priority_display_source(tmp_path, make_client) -> None:
    """同一 bvid 跨来源 view 不一致 -> conflict=True；展示值取 ranking_all（非按播放量）。"""
    manager = DatabaseManager(str(tmp_path / "conflict.db"))
    api = _StubAPI()
    api.popular = {1: _CONFLICT_POPULAR, 2: {"code": 0, "message": "0", "data": {"list": [], "no_more": True}}}
    api.ranking = _CONFLICT_RANKING
    service = _build_service(tmp_path, manager, api)
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))

    snapshot = _snapshot(make_client(service))
    video = [item for item in snapshot["videos"] if item["bvid"] == "BV1CONFLICT1"][0]
    assert video["conflict"] is True
    assert set(video["sources"]) == {"popular", "ranking_all"}
    assert video["display_source"] == "ranking_all"  # 固定优先级最高
    assert video["view"] == 999                       # 取 ranking_all 的展示值，不是按播放量挑


# ---------------------------------------------------------------------------
# 未知字段为 null（不是 0）
# ---------------------------------------------------------------------------


def test_missing_heat_score_is_null_not_zero(tmp_path, make_client) -> None:
    """heat_score 缺失 -> null + status missing，绝不补 0；合法值保留。"""
    manager = DatabaseManager(str(tmp_path / "heat.db"))
    api = _StubAPI()
    api.keyword = FIXTURES["search_square"]["missing_fields"]
    service = _build_service(tmp_path, manager, api)
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))

    snapshot = _snapshot(make_client(service))
    by_keyword = {item["keyword"]: item for item in snapshot["keywords"]}
    assert by_keyword["缺失分数的词"]["heat_score"] is None
    assert by_keyword["缺失分数的词"]["heat_status"] == "missing"
    assert by_keyword["缺展示名但有分"]["heat_score"] == 100


def test_missing_view_is_null_not_zero(tmp_path, make_client) -> None:
    """视频 view 缺失 -> null + status missing，绝不补 0。"""
    manager = DatabaseManager(str(tmp_path / "view.db"))
    api = _StubAPI()
    api.popular = {1: FIXTURES["popular"]["missing_fields"], 2: FIXTURES["popular"]["empty"]}
    api.ranking = FIXTURES["ranking"]["empty"]
    service = _build_service(tmp_path, manager, api)
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))

    snapshot = _snapshot(make_client(service))
    video = [item for item in snapshot["videos"] if item["bvid"] == "BV1PoP0100"][0]
    assert video["view"] is None
    assert video["view_status"] == "missing"


# ---------------------------------------------------------------------------
# 低成本研究草案：复用降级模板路径、零新增采集
# ---------------------------------------------------------------------------


def test_research_draft_reuses_fallback_and_zero_http(tmp_path, make_client) -> None:
    """草案复用 topic_generator 降级模板路径，素材取热搜词，全程零采集。"""
    manager = DatabaseManager(str(tmp_path / "draft.db"))
    api = _StubAPI()
    service = _build_service(tmp_path, manager, api)
    asyncio.run(service.poll_once(captured_epoch_s=CAPTURED))
    calls_before = len(api.get_calls)

    client = make_client(service)
    response = client.post(
        "/api/hotspot/discovery/research_draft",
        json={"direction": "数码", "zone_name": "数码", "count": 3},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["used_llm"] is False
    assert data["mode"] == "fallback_template"
    assert data["count"] == len(data["topics"]) > 0
    assert data["hot_tags"]  # 素材来自最近一轮热搜词
    assert all(topic["title"] for topic in data["topics"])
    assert len(api.get_calls) == calls_before  # 零新增采集


def test_research_draft_without_keywords_returns_empty(tmp_path, make_client) -> None:
    """无热搜词素材时不伪造草案，如实返回空列表 + reason。"""
    manager = DatabaseManager(str(tmp_path / "nodraft.db"))
    service = _build_service(tmp_path, manager, _StubAPI())
    client = make_client(service)

    data = client.post("/api/hotspot/discovery/research_draft", json={}).json()["data"]
    assert data["topics"] == []
    assert data["count"] == 0
    assert data["reason"] == "no_keyword_candidates"


# ---------------------------------------------------------------------------
# 前端接线静态护栏（只加区块，不重构；未知值显示空值不显示 0）
# ---------------------------------------------------------------------------


def test_index_html_has_discovery_block_and_placeholder() -> None:
    """index.html 已加 discovery 区块与内联 JS：占位、加入关注、空值不显示 0。"""
    html = (
        Path(__file__).resolve().parent.parent
        / "web" / "frontend" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="hotspot-discovery-meta"' in html
    assert "尚未跑过发现轮" in html
    assert "hotspotDiscoveryValue" in html
    assert "加入关注" in html
    assert "/hotspot/discovery/latest" in html
    assert "/hotspot/discovery/research_draft" in html
    assert "/hotspot/watch" in html
    # 未知值（null/undefined）分支返回空字符串，而不是 0。
    assert "value === null || value === undefined" in html
