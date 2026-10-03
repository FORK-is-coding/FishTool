"""web.routers.hotspot.routes_watch 单视频跟踪管理接口测试（02 · 批 4）。

覆盖对象：
- GET  /watch                 -> list_watch
- GET  /watch/{bvid}          -> get_watch
- POST /watch                 -> create_watch
- POST /watch/{bvid}/release  -> release_watch

验证维度：
三态过滤（tracking/expired/released 各只捞到自己那条）/ 单目标详情 / 未知 state 400 /
分页 limit+offset（过滤后分页）/ 手动加 watch 成功 / 重复 bvid 幂等不炸且不重置调度 /
bvid 缺失 4xx / 非法 bvid 400 / 手动停追写 manual_stop 且转 released /
已释放行不被手动接口改写原因码 / 404 / 端点接线注册。

测试策略：
- SQLite 全 tmp 隔离（DatabaseManager 写入 tmp_path），绝不触碰仓库 data/bili_ops.db。
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- ``routes_watch`` 不依赖任何网络端口；本文件不 monkeypatch 任何 API 客户端，
  故全部测试路径天然不发网络请求（另有一条静态护栏用例锁定该性质）。
- 固定时钟：monkeypatch ``routes_watch._now_epoch_s``，不依赖墙钟。
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import DatabaseManager, HotspotWatch
from modules.hotspot.watch_store import DEFAULT_SAMPLE_INTERVAL_S, DEFAULT_TTL_S
from web.routers.hotspot import routes_watch

# 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int）。
E: int = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
HOUR: int = 3600


@pytest.fixture()
def db(tmp_path):
    """tmp 目录内的真实 SQLite 管理器，完全隔离仓库数据库。"""
    return DatabaseManager(str(tmp_path / "watch_api.db"))


@pytest.fixture()
def client(monkeypatch, db):
    """挂载 hotspot router 的测试客户端，并注入隔离库与固定时钟。"""
    from web.routers import hotspot

    monkeypatch.setattr(routes_watch, "get_session", db.get_session)
    monkeypatch.setattr(routes_watch, "_now_epoch_s", lambda: E)

    app = FastAPI()
    app.include_router(hotspot.router, prefix="/api/hotspot")
    return TestClient(app)


def _seed_watch(
    db,
    bvid: str,
    *,
    active: bool = True,
    ttl_epoch_s: int | None = None,
    stop_reason: str | None = None,
    released_epoch_s: int | None = None,
    collection_tid: int | None = None,
    sample_interval_s: int = DEFAULT_SAMPLE_INTERVAL_S,
    next_due_epoch_s: int | None = None,
    failure_count: int = 0,
    last_error_code: str | None = None,
    state_json: dict | None = None,
    state_revision: int = 0,
) -> int:
    """直接写一行 ``hotspot_watch``（绕过采集层，测试专用），返回自增主键。"""
    session = db.get_session()
    try:
        row = HotspotWatch(
            bvid=bvid,
            active=active,
            stop_reason=stop_reason,
            first_seen_epoch_s=E - HOUR,
            last_seen_epoch_s=E - HOUR,
            ttl_end_epoch_s=(E + HOUR) if ttl_epoch_s is None else ttl_epoch_s,
            released_epoch_s=released_epoch_s,
            next_due_epoch_s=(E - 60) if next_due_epoch_s is None else next_due_epoch_s,
            failure_count=failure_count,
            last_error_code=last_error_code,
            sample_interval_s=sample_interval_s,
            state_json=state_json,
            state_revision=state_revision,
            collection_tid=collection_tid,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id
    finally:
        session.close()


def _seed_three_states(db) -> None:
    """造三态各一条：判定时刻固定为 E，故 tracking / expired / released 各命中一条。"""
    _seed_watch(db, "BV1TRACK0001", active=True, ttl_epoch_s=E + HOUR)  # 跟踪中
    _seed_watch(db, "BV1EXPIRE001", active=True, ttl_epoch_s=E - HOUR)  # 已到期
    _seed_watch(
        db,
        "BV1RELEAS001",
        active=False,
        stop_reason="expired",
        released_epoch_s=E - 2 * HOUR,
    )  # 已释放


def _row_count(db, bvid: str) -> int:
    """返回指定 bvid 在库中的行数。"""
    session = db.get_session()
    try:
        return session.query(HotspotWatch).filter(HotspotWatch.bvid == bvid).count()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# GET /watch
# ---------------------------------------------------------------------------


def test_list_returns_computed_state_for_each_row(client, db):
    """无过滤时应返回全部行，且每项都带后端算好的 state 字段。"""
    _seed_three_states(db)

    response = client.get("/api/hotspot/watch")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 3
    assert len(data["items"]) == 3
    assert {item["bvid"]: item["state"] for item in data["items"]} == {
        "BV1TRACK0001": "tracking",
        "BV1EXPIRE001": "expired",
        "BV1RELEAS001": "released",
    }
    # 论断1-B：列表响应补来源字段（watch 链路恒 LifecycleV2）。
    assert data["algorithm_version"] == "lifecycle_v2"


def test_list_filters_tracking_only(client, db):
    """state=tracking 只应捞到跟踪中那条。"""
    _seed_three_states(db)

    data = client.get("/api/hotspot/watch", params={"state": "tracking"}).json()["data"]
    assert [item["bvid"] for item in data["items"]] == ["BV1TRACK0001"]
    assert data["total"] == 1


def test_list_filters_expired_only(client, db):
    """state=expired 只应捞到已到期那条。"""
    _seed_three_states(db)

    data = client.get("/api/hotspot/watch", params={"state": "expired"}).json()["data"]
    assert [item["bvid"] for item in data["items"]] == ["BV1EXPIRE001"]
    assert data["total"] == 1


def test_list_filters_released_only(client, db):
    """state=released 只应捞到已释放那条。"""
    _seed_three_states(db)

    data = client.get("/api/hotspot/watch", params={"state": "released"}).json()["data"]
    assert [item["bvid"] for item in data["items"]] == ["BV1RELEAS001"]
    assert data["total"] == 1


def test_list_unknown_state_returns_400(client):
    """未知 state 应返回 400，而不是悄悄返回空列表。"""
    response = client.get("/api/hotspot/watch", params={"state": "bogus"})
    assert response.status_code == 400
    assert "state" in response.json()["detail"]


def test_list_pagination_applies_after_filter(client, db):
    """分页作用在「过滤后的逻辑结果集」上：limit=1&offset=1 命中第二条。"""
    _seed_watch(db, "BV1TRACK0001", active=True, ttl_epoch_s=E + HOUR)
    _seed_watch(db, "BV1TRACK0002", active=True, ttl_epoch_s=E + HOUR)
    _seed_watch(db, "BV1TRACK0003", active=True, ttl_epoch_s=E + HOUR)

    data = client.get("/api/hotspot/watch", params={"limit": 1, "offset": 1}).json()["data"]
    assert data["total"] == 3  # total 反映过滤后总数，不是本页条数
    assert data["limit"] == 1
    assert data["offset"] == 1
    assert [item["bvid"] for item in data["items"]] == ["BV1TRACK0002"]


def test_list_pagination_offset_beyond_end(client, db):
    """offset 超过结果总数时应返回空切片，但 total 不变。"""
    _seed_watch(db, "BV1TRACK0001", active=True, ttl_epoch_s=E + HOUR)

    data = client.get("/api/hotspot/watch", params={"limit": 10, "offset": 5}).json()["data"]
    assert data["items"] == []
    assert data["total"] == 1


def test_list_rejects_limit_out_of_range(client):
    """limit 必须落在 1—200，越界返回 422。"""
    assert client.get("/api/hotspot/watch", params={"limit": 0}).status_code == 422
    assert client.get("/api/hotspot/watch", params={"limit": 500}).status_code == 422


# ---------------------------------------------------------------------------
# GET /watch/{bvid}
# ---------------------------------------------------------------------------


def test_get_watch_detail_includes_all_columns(client, db):
    """单目标详情应透出 state_json / failure_count / last_error_code / next_due_epoch_s 等。"""
    _seed_watch(
        db,
        "BV1DETAIL001",
        active=True,
        ttl_epoch_s=E + HOUR,
        next_due_epoch_s=E + 30,
        failure_count=2,
        last_error_code="http_412",
        sample_interval_s=600,
        state_json={"segment": "s1"},
        state_revision=3,
        collection_tid=4,
    )

    response = client.get("/api/hotspot/watch/BV1DETAIL001")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["bvid"] == "BV1DETAIL001"
    assert data["state"] == "tracking"
    assert data["collection_tid"] == 4
    assert data["failure_count"] == 2
    assert data["last_error_code"] == "http_412"
    assert data["next_due_epoch_s"] == E + 30
    assert data["state_json"] == {"segment": "s1"}
    assert data["state_revision"] == 3
    assert data["sample_interval_s"] == 600
    # 论断1-B：单目标详情响应补来源字段。
    assert data["algorithm_version"] == "lifecycle_v2"


def test_get_watch_detail_missing_returns_404(client):
    """目标不存在应返回 404。"""
    response = client.get("/api/hotspot/watch/BV1NOPE00001")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /watch
# ---------------------------------------------------------------------------


def test_create_watch_success_with_defaults(client, db):
    """手动加 watch 成功：默认间隔 3600、默认 TTL 14 天、状态 tracking。"""
    response = client.post("/api/hotspot/watch", json={"bvid": "BV1CREATE001", "collection_tid": 4})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["bvid"] == "BV1CREATE001"
    assert data["state"] == "tracking"
    assert data["active"] is True
    assert data["collection_tid"] == 4
    assert data["sample_interval_s"] == DEFAULT_SAMPLE_INTERVAL_S
    assert data["ttl_end_epoch_s"] == E + DEFAULT_TTL_S
    assert data["next_due_epoch_s"] == E + DEFAULT_SAMPLE_INTERVAL_S
    assert data["algorithm_version"] == "lifecycle_v2"
    assert _row_count(db, "BV1CREATE001") == 1


def test_create_watch_accepts_custom_interval(client, db):
    """显式传入 sample_interval_s 时应生效，next_due 按该间隔推算。"""
    data = client.post(
        "/api/hotspot/watch", json={"bvid": "BV1CREATE002", "sample_interval_s": 300}
    ).json()["data"]
    assert data["sample_interval_s"] == 300
    assert data["next_due_epoch_s"] == E + 300


def test_create_watch_duplicate_bvid_is_idempotent(client, db):
    """重复 bvid 不应报错，且库里仍只有一行。"""
    first = client.post("/api/hotspot/watch", json={"bvid": "BV1DUPX00001"})
    second = client.post("/api/hotspot/watch", json={"bvid": "BV1DUPX00001"})
    assert first.status_code == 200
    assert second.status_code == 200  # 关键：重复不炸
    assert second.json()["data"]["bvid"] == "BV1DUPX00001"
    assert _row_count(db, "BV1DUPX00001") == 1


def test_create_watch_duplicate_does_not_reset_schedule(client, db, monkeypatch):
    """重复发现只更新 last_seen，不重置 next_due / ttl / first_seen。"""
    assert client.post("/api/hotspot/watch", json={"bvid": "BV1DUPX00002"}).status_code == 200

    session = db.get_session()
    try:
        row = session.query(HotspotWatch).filter(HotspotWatch.bvid == "BV1DUPX00002").one()
        original_due, original_ttl, original_first = (
            row.next_due_epoch_s,
            row.ttl_end_epoch_s,
            row.first_seen_epoch_s,
        )
    finally:
        session.close()

    # 时钟推进 1 小时后再提交同一 bvid：调度列不应被重置。
    monkeypatch.setattr(routes_watch, "_now_epoch_s", lambda: E + HOUR)
    data = client.post("/api/hotspot/watch", json={"bvid": "BV1DUPX00002"}).json()["data"]

    assert data["next_due_epoch_s"] == original_due
    assert data["ttl_end_epoch_s"] == original_ttl
    assert data["first_seen_epoch_s"] == original_first
    assert data["last_seen_epoch_s"] == E + HOUR  # 只有 last_seen 前移
    assert _row_count(db, "BV1DUPX00002") == 1


def test_create_watch_missing_bvid_returns_4xx(client):
    """缺少 bvid 应返回 4xx（422 校验失败）。"""
    response = client.post("/api/hotspot/watch", json={})
    assert 400 <= response.status_code < 500


def test_create_watch_blank_bvid_returns_400(client):
    """空 / 纯空白 bvid 由 upsert 校验拦下，转 400。"""
    response = client.post("/api/hotspot/watch", json={"bvid": "   "})
    assert response.status_code == 400
    assert "invalid_bvid" in response.json()["detail"]


def test_create_watch_overlong_bvid_returns_400(client, db):
    """超过 20 字符的 bvid 应被拒绝（400），不落库。"""
    response = client.post("/api/hotspot/watch", json={"bvid": "BV" + "1" * 30})
    assert response.status_code == 400
    assert _row_count(db, "BV" + "1" * 30) == 0


# ---------------------------------------------------------------------------
# POST /watch/{bvid}/release
# ---------------------------------------------------------------------------


def test_release_tracking_writes_manual_stop_and_released(client, db):
    """手动停追跟踪中目标：stop_reason=manual_stop、active=False、状态转 released。"""
    _seed_watch(db, "BV1STOP00001", active=True, ttl_epoch_s=E + HOUR)

    response = client.post("/api/hotspot/watch/BV1STOP00001/release")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["stop_reason"] == "manual_stop"
    assert data["state"] == "released"
    assert data["active"] is False
    assert data["released_epoch_s"] == E
    assert data["state_revision"] == 1  # 释放使代际前进，fence 迟到写入
    assert data["algorithm_version"] == "lifecycle_v2"

    session = db.get_session()
    try:
        row = session.query(HotspotWatch).filter(HotspotWatch.bvid == "BV1STOP00001").one()
        assert (row.stop_reason, row.active, row.released_epoch_s) == ("manual_stop", False, E)
    finally:
        session.close()


def test_release_expired_state_row_writes_manual_stop(client, db):
    """仍 active 的「已到期」行被手动停追，同样写 manual_stop 并转 released。"""
    _seed_watch(db, "BV1STOP00002", active=True, ttl_epoch_s=E - HOUR)

    data = client.post("/api/hotspot/watch/BV1STOP00002/release").json()["data"]
    assert data["state"] == "released"
    assert data["stop_reason"] == "manual_stop"


def test_release_does_not_overwrite_auto_expired_reason(client, db):
    """自动到期已释放行（stop_reason='expired'）不被手动接口改写为 manual_stop。"""
    _seed_watch(
        db, "BV1AUTO00001", active=False, stop_reason="expired", released_epoch_s=E - 2 * HOUR
    )

    response = client.post("/api/hotspot/watch/BV1AUTO00001/release")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["stop_reason"] == "expired"
    assert data["state"] == "released"
    assert data["released_epoch_s"] == E - 2 * HOUR


def test_release_is_idempotent(client, db):
    """重复停追是幂等空操作：原因码与释放时刻保持首次结果。"""
    _seed_watch(db, "BV1STOP00003", active=True, ttl_epoch_s=E + HOUR)

    first = client.post("/api/hotspot/watch/BV1STOP00003/release").json()["data"]
    second = client.post("/api/hotspot/watch/BV1STOP00003/release").json()["data"]
    assert first["stop_reason"] == second["stop_reason"] == "manual_stop"
    assert first["released_epoch_s"] == second["released_epoch_s"] == E


def test_release_missing_returns_404(client):
    """目标不存在应返回 404。"""
    response = client.post("/api/hotspot/watch/BV1NOPE00099/release")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 接线 / 无网络护栏
# ---------------------------------------------------------------------------


def test_watch_endpoints_registered_in_hotspot_router():
    """四个 watch 端点已通过 __init__.py 接线注册进 hotspot router。"""
    from web.routers import hotspot

    methods_by_path: dict[str, set] = {}
    for route in hotspot.router.routes:
        methods_by_path.setdefault(route.path, set()).update(
            getattr(route, "methods", set()) or set()
        )
    assert "GET" in methods_by_path.get("/watch", set())
    assert "POST" in methods_by_path.get("/watch", set())
    assert "GET" in methods_by_path.get("/watch/{bvid}", set())
    assert "POST" in methods_by_path.get("/watch/{bvid}/release", set())


def test_routes_watch_has_no_network_dependency():
    """静态护栏：routes_watch 只读/写本地 watch 表，不引入任何网络客户端。"""
    source = inspect.getsource(routes_watch)
    for token in ("BilibiliAPI", "get_api", "httpx", "requests."):
        assert token not in source


# ---------------------------------------------------------------------------
# 论断1-B：/watch 响应补算法来源字段（写死取自 watch 链路，不走注册表）
# ---------------------------------------------------------------------------


def test_watch_algorithm_version_matches_lifecycle_v2():
    """写死版本号必须与 ``LifecycleV2.version`` 等值：防「写死」漂移。"""
    from modules.hotspot.algorithm.lifecycle_v2 import LifecycleV2

    assert routes_watch._WATCH_ALGORITHM_VERSION == LifecycleV2().version
