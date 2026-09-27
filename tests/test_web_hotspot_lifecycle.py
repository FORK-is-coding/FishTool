"""web.routers.hotspot.routes_lifecycle 生命周期/采集接口测试（第4批 · web 段）。

覆盖对象：
- GET  /lifecycle           -> get_lifecycle
- GET  /lifecycle/timeline  -> get_lifecycle_timeline
- GET  /lifecycle/accounts  -> get_lifecycle_accounts
- POST /collect             -> start_collect
- GET  /collect/progress    -> get_collect_progress

验证维度：
空/有数据生命周期分析 / 未知算法 400 / 快照读取失败 500 / 时间轴命中与 404 /
账号抽屉成功与充电降级 / 采集并发短路 / 进度透传。

测试策略：
- SQLite 全 tmp 隔离（DatabaseManager 写入 tmp_path），绝不触碰仓库 data/bili_ops.db。
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- 网络层用契约级假 api / 假采集器，避免真实请求。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import DatabaseManager, Video, VideoStats
from web.routers.hotspot import routes_lifecycle


class _FakeApi:
    """契约级假 B站 API：只暴露 get_charge_count。"""

    def __init__(self, charge_result: dict | None = None, charge_error: Exception | None = None) -> None:
        self._charge_result = charge_result if charge_result is not None else {"charge_count": 7, "source": "api"}
        self._charge_error = charge_error

    async def get_charge_count(self, mid: int) -> dict:
        """返回预置充电数据或抛出注入异常。"""
        if self._charge_error is not None:
            raise self._charge_error
        return self._charge_result


class _FakeCollector:
    """记录采集参数的假采集器。"""

    calls: list[dict] = []

    def __init__(self, api=None) -> None:
        self.api = api

    async def collect(self, tid, limit, min_view, sample_comments, sample_danmaku) -> None:
        """记录参数，不执行真实采集。"""
        type(self).calls.append(
            {"tid": tid, "limit": limit, "min_view": min_view}
        )


class _PendingTask:
    """永远未完成的假 asyncio 任务，用于验证并发采集短路。"""

    def done(self) -> bool:
        """恒返回 False 表示仍在运行。"""
        return False


@pytest.fixture()
def db(tmp_path):
    """tmp 目录内的真实 SQLite 管理器，完全隔离仓库数据库。"""
    return DatabaseManager(str(tmp_path / "lifecycle.db"))


@pytest.fixture()
def client(monkeypatch, db):
    """挂载 hotspot router 的测试客户端，并注入隔离依赖。"""
    from web.routers import hotspot

    _FakeCollector.calls = []
    monkeypatch.setattr(routes_lifecycle, "get_session", db.get_session)
    monkeypatch.setattr(routes_lifecycle, "get_api", lambda: _FakeApi())
    monkeypatch.setattr(routes_lifecycle, "get_progress", lambda: {"status": "idle", "progress": 0})
    monkeypatch.setattr(routes_lifecycle, "HotspotCollector", _FakeCollector)
    monkeypatch.setattr(routes_lifecycle, "_collect_task", None)

    app = FastAPI()
    app.include_router(hotspot.router, prefix="/api/hotspot")
    return TestClient(app)


def _seed_video_with_stats(db, bvid: str = "BV1LIFE00001", mid: int = 12345, points: int = 2) -> None:
    """向 tmp 库写入一个视频及其统计历史。"""
    session = db.get_session()
    try:
        video = Video(bvid=bvid, title="热点视频", tid=4, mid=mid, author="UP主")
        session.add(video)
        session.commit()
        now = datetime.now()
        for index in range(points):
            # 越晚的快照播放量越高；查询按 snapshot_time 升序，因此 view 升序。
            session.add(
                VideoStats(video_id=video.id, view=100 * (points - index), snapshot_time=now - timedelta(hours=index))
            )
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# GET /lifecycle
# ---------------------------------------------------------------------------


def test_lifecycle_empty_database_returns_empty_items(client):
    """无快照时应返回空 items 与 sample_count=0，并带算法版本。"""
    response = client.get("/api/hotspot/lifecycle")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["items"] == []
    assert data["sample_count"] == 0
    assert data["algorithm_version"]


def test_lifecycle_counts_samples_from_database(client, db, monkeypatch):
    """有历史快照时 sample_count 应等于读到的快照条数。"""
    _seed_video_with_stats(db, points=2)

    async def fake_metrics(api, mids):
        """UP 主轻量指标替身，直接返回空映射。"""
        return {}

    monkeypatch.setattr(routes_lifecycle, "get_up_light_metrics", fake_metrics)

    response = client.get("/api/hotspot/lifecycle", params={"tid": 4})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["sample_count"] == 2
    assert isinstance(data["items"], list)


def test_lifecycle_owner_metrics_failure_is_degraded(client, db, monkeypatch):
    """UP 主指标批量补充失败应降级为 None，不影响主体返回。"""
    _seed_video_with_stats(db, points=2)

    async def boom_metrics(api, mids):
        """模拟指标补采失败。"""
        raise RuntimeError("指标服务不可用")

    monkeypatch.setattr(routes_lifecycle, "get_up_light_metrics", boom_metrics)

    response = client.get("/api/hotspot/lifecycle", params={"tid": 4})
    assert response.status_code == 200
    assert response.json()["data"]["sample_count"] == 2


def test_lifecycle_unknown_algorithm_returns_400(client):
    """未知算法名应由 KeyError 转为 400。"""
    response = client.get("/api/hotspot/lifecycle", params={"algorithm": "no_such_algo"})
    assert response.status_code == 400


def test_lifecycle_session_open_failure_returns_500(client, monkeypatch):
    """会话获取失败（在快照读取 try 之外）应由外层统一转 500。"""

    def boom():
        """模拟数据库连接不可用。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(routes_lifecycle, "get_session", boom)
    response = client.get("/api/hotspot/lifecycle")
    assert response.status_code == 500
    assert "生命周期分析失败" in response.json()["detail"]


def test_lifecycle_query_failure_returns_500(client, monkeypatch):
    """快照查询失败应由 _load_snapshots 包装为 500。"""

    class _BoomSession:
        """查询即抛错的契约级会话替身。"""

        def query(self, *args, **kwargs):
            """模拟 SQL 执行失败。"""
            raise RuntimeError("query boom")

        def close(self) -> None:
            """无需清理。"""
            return None

    monkeypatch.setattr(routes_lifecycle, "get_session", lambda: _BoomSession())
    response = client.get("/api/hotspot/lifecycle")
    assert response.status_code == 500
    assert "读取热点快照失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /lifecycle/timeline
# ---------------------------------------------------------------------------


def test_timeline_returns_points(client, db):
    """命中视频时应返回按时间升序的统计点。"""
    _seed_video_with_stats(db, bvid="BV1LIFE00002", points=2)

    response = client.get("/api/hotspot/lifecycle/timeline", params={"bvid": "BV1LIFE00002"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["bvid"] == "BV1LIFE00002"
    assert data["title"] == "热点视频"
    assert len(data["points"]) == 2
    assert data["points"][0]["view"] == 100


def test_timeline_missing_video_returns_404(client):
    """视频不存在应返回 404。"""
    response = client.get("/api/hotspot/lifecycle/timeline", params={"bvid": "BV1MISSING"})
    assert response.status_code == 404
    assert "视频不存在" in response.json()["detail"]


def test_timeline_requires_bvid_query(client):
    """缺少 bvid 查询参数应返回 422。"""
    response = client.get("/api/hotspot/lifecycle/timeline")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /lifecycle/accounts
# ---------------------------------------------------------------------------


def test_accounts_success_includes_charge_count(client, db, monkeypatch):
    """命中视频时应返回关联数据并补上充电人数。"""
    _seed_video_with_stats(db, bvid="BV1LIFE00003", mid=777)

    async def fake_relation(api, mid):
        """UP 主关系数据替身。"""
        return {"mid": mid, "follower": 100}

    monkeypatch.setattr(routes_lifecycle, "get_up_relation", fake_relation)
    monkeypatch.setattr(routes_lifecycle, "get_api", lambda: _FakeApi({"charge_count": 12, "source": "api"}))

    response = client.get("/api/hotspot/lifecycle/accounts", params={"bvid": "BV1LIFE00003"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mid"] == 777
    assert data["charge_count"] == 12
    assert data["charge_source"] == "api"


def test_accounts_charge_failure_degrades(client, db, monkeypatch):
    """充电人数获取失败时应标记 unavailable 而非报错。"""
    _seed_video_with_stats(db, bvid="BV1LIFE00004", mid=888)

    async def fake_relation(api, mid):
        """UP 主关系数据替身。"""
        return {"mid": mid}

    monkeypatch.setattr(routes_lifecycle, "get_up_relation", fake_relation)
    monkeypatch.setattr(
        routes_lifecycle, "get_api", lambda: _FakeApi(charge_error=RuntimeError("充电接口失败"))
    )

    response = client.get("/api/hotspot/lifecycle/accounts", params={"bvid": "BV1LIFE00004"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["charge_count"] is None
    assert data["charge_source"] == "unavailable"


def test_accounts_missing_video_returns_404(client):
    """视频不存在应返回 404。"""
    response = client.get("/api/hotspot/lifecycle/accounts", params={"bvid": "BV1NONE"})
    assert response.status_code == 404


def test_accounts_video_without_mid_returns_404(client, db):
    """视频缺少 UP 主 mid 时也应返回 404。"""
    session = db.get_session()
    try:
        session.add(Video(bvid="BV1NOMID", title="无作者", tid=4))
        session.commit()
    finally:
        session.close()

    response = client.get("/api/hotspot/lifecycle/accounts", params={"bvid": "BV1NOMID"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /collect
# ---------------------------------------------------------------------------


def test_collect_starts_task(client):
    """无运行中任务时应启动采集并返回 started=True。"""
    response = client.post("/api/hotspot/collect", params={"tid": 4, "limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["started"] is True
    assert "分区 4" in body["data"]["message"]


def test_collect_short_circuits_when_task_running(client, monkeypatch):
    """已有运行中任务时应短路返回 started=False。"""
    monkeypatch.setattr(routes_lifecycle, "_collect_task", _PendingTask())

    response = client.post("/api/hotspot/collect")
    assert response.status_code == 200
    assert response.json()["data"] == {"started": False, "message": "已有采集任务进行中"}


def test_collect_rejects_out_of_range_limit(client):
    """limit 超出 1-200 范围应返回 422。"""
    response = client.post("/api/hotspot/collect", params={"limit": 500})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /collect/progress
# ---------------------------------------------------------------------------


def test_collect_progress_passthrough(client):
    """进度接口应原样透传 get_progress 数据。"""
    response = client.get("/api/hotspot/collect/progress")
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"status": "idle", "progress": 0}}
