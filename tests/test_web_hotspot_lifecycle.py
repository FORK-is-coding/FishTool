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

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sqlalchemy import null

from core.database import DatabaseManager, HotspotWatch, Video, VideoStats
from modules.hotspot.algorithm import registry as _algorithm_registry
from modules.hotspot.algorithm.lifecycle_v2 import LifecycleV2
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


def _seed_quality_rows(db, bvid: str = "BV1QLT00001", mid: int = 12345) -> None:
    """写入带质量列的三条快照：ok(100) / missing(NULL) / ok(300)，均带 epoch。

    坏点用 SQL ``null()`` 强制写入 NULL，绕过 ``Column(default=0)``（与
    ``snapshot_store.persist_snapshot`` 同一手法）。
    """
    session = db.get_session()
    try:
        video = Video(bvid=bvid, title="质量视频", tid=4, mid=mid, author="UP主")
        session.add(video)
        session.commit()
        now = datetime.now()
        session.add(VideoStats(
            video_id=video.id, view=100, view_status="ok", stat_status="ok",
            metric_status={"view": "ok", "like": "ok"},
            captured_epoch_s=1000, collection_tid=4, raw_tid=30,
            snapshot_time=now - timedelta(hours=2),
        ))
        session.add(VideoStats(
            video_id=video.id, view=null(), view_status="missing", stat_status="missing",
            metric_status={"view": "missing"},
            captured_epoch_s=2000, collection_tid=4, raw_tid=30,
            snapshot_time=now - timedelta(hours=1),
        ))
        session.add(VideoStats(
            video_id=video.id, view=300, view_status="ok", stat_status="ok",
            metric_status={"view": "ok"},
            captured_epoch_s=3000, collection_tid=4, raw_tid=30,
            snapshot_time=now,
        ))
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


def test_lifecycle_default_algorithm_is_lifecycle_v2(client):
    """不传 algorithm 时应走新默认 lifecycle_v2（接口 Query 默认基调已切换）。"""
    response = client.get("/api/hotspot/lifecycle")

    assert response.status_code == 200
    assert response.json()["data"]["algorithm_version"] == "lifecycle_v2"


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
    # 反例说明见 out_fishtool03/progress.md（红线）：该行只有旧 snapshot_time，
    # view_status/metric_status 均为 NULL，按 §4.3 两边都 NULL => unknown；
    # 旧预期把它当已验证播放量 100 是错的，原始值改放 raw 供审计。
    assert data["points"][0]["view"] is None
    assert data["points"][0]["raw"]["view"] == 100
    assert data["points"][0]["status"]["view"] == "unknown"


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


# ---------------------------------------------------------------------------
# 批 3：质量 marker / epoch 读端传递（规格 §4.3 / §4.4）
# ---------------------------------------------------------------------------


def test_load_snapshots_passes_quality_marker_and_epoch(db, monkeypatch):
    """读端应把质量 marker 与 epoch 传给 Snapshot，坏点 view 为 None（§4.4）。"""
    monkeypatch.setattr(routes_lifecycle, "get_session", db.get_session)
    _seed_quality_rows(db)

    snapshots = routes_lifecycle._load_snapshots(tid=4)

    assert [s.view for s in snapshots] == [100, None, 300]
    assert [s.view_quality for s in snapshots] == ["ok", "missing", "ok"]
    assert [s.captured_epoch_s for s in snapshots] == [1000, 2000, 3000]
    assert snapshots[0].collection_tid == 4
    assert snapshots[0].raw_tid == 30
    # 坏点保留 raw_view 供审计，但绝不兜底成 0
    assert snapshots[1].raw_view is None


def test_load_snapshots_flags_inconsistent_quality(db, monkeypatch):
    """view_status 与 metric_status.view 矛盾时标 inconsistent_quality（§4.3）。"""
    monkeypatch.setattr(routes_lifecycle, "get_session", db.get_session)
    session = db.get_session()
    try:
        video = Video(bvid="BV1INC00001", title="矛盾", tid=4)
        session.add(video)
        session.commit()
        session.add(VideoStats(
            video_id=video.id, view=100, view_status="ok", stat_status="ok",
            metric_status={"view": "missing"}, snapshot_time=datetime.now(),
        ))
        session.commit()
    finally:
        session.close()

    snapshots = routes_lifecycle._load_snapshots(bvid="BV1INC00001")
    assert snapshots[0].view is None
    assert snapshots[0].view_quality == "inconsistent_quality"


def test_load_snapshots_reads_watch_first_seen_per_bvid(db, monkeypatch):
    """B6b：_load_snapshots 一次查询按 bvid 读同一 watch 首次发现时间；无 watch 记 None。"""
    monkeypatch.setattr(routes_lifecycle, "get_session", db.get_session)
    session = db.get_session()
    try:
        watched = Video(bvid="BV1FS00011", title="有watch", tid=4)
        plain = Video(bvid="BV1FS00012", title="无watch", tid=4)
        session.add_all([watched, plain])
        session.commit()
        for video in (watched, plain):
            session.add(VideoStats(
                video_id=video.id, view=100, view_status="ok", stat_status="ok",
                metric_status={"view": "ok"}, snapshot_time=datetime.now(),
                captured_epoch_s=3000, collection_tid=4,
            ))
        session.add(HotspotWatch(
            bvid="BV1FS00011", first_seen_epoch_s=1750000000,
            ttl_end_epoch_s=1750100000, next_due_epoch_s=1750000000,
        ))
        session.commit()
    finally:
        session.close()

    by_bvid = {s.bvid: s.first_seen_epoch_s for s in routes_lifecycle._load_snapshots(tid=4)}
    assert by_bvid["BV1FS00011"] == 1750000000
    assert by_bvid["BV1FS00012"] is None


def test_lifecycle_excludes_quality_markers(client, db, monkeypatch):
    """生命周期回放只吃整数有效快照，坏点计入 rejected，不送进旧算式（§4.4）。"""
    monkeypatch.setattr(routes_lifecycle, "get_session", db.get_session)
    _seed_quality_rows(db)

    async def fake_metrics(api, mids):
        """UP 主轻量指标替身，避免测试触网。"""
        return {}

    monkeypatch.setattr(routes_lifecycle, "get_up_light_metrics", fake_metrics)

    response = client.get("/api/hotspot/lifecycle", params={"tid": 4})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["sample_count"] == 3
    assert data["valid_count"] == 2
    assert data["rejected_count"] == 1
    assert data["history_limited"] is True


def test_timeline_quality_aware_marks_missing_not_zero(client, db, monkeypatch):
    """时间轴：缺 view 的真实 NULL 返回 null 而非伪造 0，epoch 一并透出。"""
    monkeypatch.setattr(routes_lifecycle, "get_session", db.get_session)
    _seed_quality_rows(db, bvid="BV1QLT00002")

    response = client.get("/api/hotspot/lifecycle/timeline", params={"bvid": "BV1QLT00002"})
    assert response.status_code == 200
    points = response.json()["data"]["points"]
    assert [p["view"] for p in points] == [100, None, 300]
    assert [p["captured_epoch_s"] for p in points] == [1000, 2000, 3000]
    assert points[1]["raw"]["view"] is None
    assert points[1]["status"]["view"] == "missing"


# ---------------------------------------------------------------------------
# 追加：v2 断段 marker 不得在读端被 int-view 过滤洗掉（按算法分流）
# ---------------------------------------------------------------------------


def _register_lifecycle_v2(monkeypatch) -> None:
    """在测试内注册 lifecycle_v2 工厂；用例结束由 monkeypatch 自动还原。

    兜底保证本用例在「v2 默认切换」前后都能独立跑通：切换前 registry 只有
    heuristic_v1，此处临时注入；切换后 registry 自带该注册，重复覆盖也安全。
    """
    monkeypatch.setattr(
        _algorithm_registry,
        "_REGISTRY",
        {**_algorithm_registry._REGISTRY, "lifecycle_v2": LifecycleV2},
    )


def _seed_marker_rows(db, bvid: str = "BV1MRK00001", mid: int = 12345) -> None:
    """写入「ok(100) / missing(NULL) / ok(300)」三条快照，epoch 用固定 UTC 网格。

    时间取 2026-09-01T00:00:00Z 前后各一小时：首点落在前一日 23:00，保证 v2 日窗
    网格右边界落在观测区间内，使「两个 ok 点相连」时真正能产出速率（对照组）。
    坏点用 SQL ``null()`` 强制写入 NULL，绕过 ``Column(default=0)``。
    """
    base = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    session = db.get_session()
    try:
        video = Video(bvid=bvid, title="marker 视频", tid=4, mid=mid, author="UP主")
        session.add(video)
        session.commit()
        now = datetime.now()
        session.add(VideoStats(
            video_id=video.id, view=100, view_status="ok", stat_status="ok",
            metric_status={"view": "ok", "like": "ok"},
            captured_epoch_s=base - 3600, collection_tid=4, raw_tid=30,
            snapshot_time=now - timedelta(hours=2),
        ))
        session.add(VideoStats(
            video_id=video.id, view=null(), view_status="missing", stat_status="missing",
            metric_status={"view": "missing"},
            captured_epoch_s=base + 3600, collection_tid=4, raw_tid=30,
            snapshot_time=now - timedelta(hours=1),
        ))
        session.add(VideoStats(
            video_id=video.id, view=300, view_status="ok", stat_status="ok",
            metric_status={"view": "ok"},
            captured_epoch_s=base + 7200, collection_tid=4, raw_tid=30,
            snapshot_time=now,
        ))
        session.commit()
    finally:
        session.close()


def test_lifecycle_v2_keeps_quality_marker_as_segment_break(client, db, monkeypatch):
    """切 v2 时质量 marker 必须保留到算法输入层（断段），不被 int-view 过滤抹掉。

    同一份「ok(100) / missing(NULL) / ok(300)」快照下：
    - 对照组：沿用「只留 int view」的旧输入假设，v2 把两个 ok 点连成一段 -> 能出速率；
    - 实测组：/lifecycle?algorithm=lifecycle_v2 把含 marker 的全量行交给 v2，
      marker 断段、只剩尾段单点 -> observed_windows == 0、stage == 数据不足。
    """
    monkeypatch.setattr(routes_lifecycle, "get_session", db.get_session)
    _seed_marker_rows(db)

    async def fake_metrics(api, mids):
        """UP 主轻量指标替身，避免测试触网。"""
        return {}

    monkeypatch.setattr(routes_lifecycle, "get_up_light_metrics", fake_metrics)
    _register_lifecycle_v2(monkeypatch)

    # 对照组：旧的 int-view 过滤会把 marker 洗掉，两个 ok 点落在同一段、能出速率。
    snapshots = routes_lifecycle._load_snapshots(tid=4)
    control = LifecycleV2().detect([s for s in snapshots if type(s.view) is int])
    assert control and control[0].metrics["observed_windows"] > 0

    # 实测：路由把含 marker 的全量行交给 v2，断段 marker 被保留。
    response = client.get("/api/hotspot/lifecycle", params={"tid": 4, "algorithm": "lifecycle_v2"})
    assert response.status_code == 200
    item = response.json()["data"]["items"][0]
    assert item["metrics"]["observed_windows"] == 0
    assert item["stage"] == "数据不足"


# ---------------------------------------------------------------------------
# 08 案 §H2 / §H4 / §B4：请求级 as_of 与稿龄通道（B5 集成验收）
# ---------------------------------------------------------------------------


def _seed_pubdate_rows(db, bvid: str = "BV1AGE00001", mid: int = 12345) -> None:
    """写入三条带**有效发布时间**的快照（captured 与 pubdate 均为秒级 epoch）。

    发布时间统一取「三天前」，因此稿龄应稳定落在 3 天附近，便于断言小数天数。
    """
    session = db.get_session()
    try:
        video = Video(bvid=bvid, title="稿龄视频", tid=4, mid=mid, author="UP主")
        session.add(video)
        session.commit()
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        published = now_epoch - 3 * 86400
        for index in range(3):
            epoch_s = now_epoch - (2 - index) * 3600
            session.add(VideoStats(
                video_id=video.id,
                view=1000 + index * 100,
                snapshot_time=datetime.fromtimestamp(epoch_s),
                captured_epoch_s=epoch_s,
                collection_tid=4,
                raw_tid=4,
                view_status="ok",
                stat_status="ok",
                metric_status={"view": "ok"},
                pubdate_epoch_s=published,
                pubdate_status="ok",
            ))
        session.commit()
    finally:
        session.close()


def _stub_up_metrics(monkeypatch):
    """置空 UP 主指标补采，避免测试触网。"""

    async def fake_metrics(api, mids):
        """UP 主轻量指标替身。"""
        return {}

    monkeypatch.setattr(routes_lifecycle, "get_up_light_metrics", fake_metrics)


def test_lifecycle_exposes_single_request_level_as_of(client, db, monkeypatch):
    """整次读取只冻结一次评估截止，且下发给每个 item 的 metadata（H4 / B4）。"""
    monkeypatch.setattr(routes_lifecycle, "get_session", db.get_session)
    _stub_up_metrics(monkeypatch)
    _seed_pubdate_rows(db)

    response = client.get("/api/hotspot/lifecycle", params={"tid": 4})
    assert response.status_code == 200
    data = response.json()["data"]

    as_of = data["as_of_epoch_s"]
    assert isinstance(as_of, int) and as_of > 0
    items = data["items"]
    assert items
    for item in items:
        assert item["metadata"]["as_of_epoch_s"] == as_of
        assert item["metadata"]["as_of_source"] == "explicit"
        assert item["metadata"]["age_reference"] == "evaluation_as_of"


def test_lifecycle_item_carries_age_days_and_status(client, db, monkeypatch):
    """稿龄相对请求级 as_of 计算：天数进 metrics，状态 / 来源进 metadata。"""
    monkeypatch.setattr(routes_lifecycle, "get_session", db.get_session)
    _stub_up_metrics(monkeypatch)
    _seed_pubdate_rows(db, bvid="BV1AGE00002")

    response = client.get("/api/hotspot/lifecycle", params={"tid": 4})
    assert response.status_code == 200
    item = response.json()["data"]["items"][0]

    assert item["metadata"]["age_status"] == "ok"
    assert item["metadata"]["age_source"] == "pubdate_epoch_s"
    days = item["metrics"]["age_days"]
    assert isinstance(days, float)
    # 发布时间固定在三天前，分钟级读取偏差不会把它推出这个区间。
    assert 2.5 < days < 3.5


def test_lifecycle_age_status_is_explicit_when_pubdate_missing(client, db, monkeypatch):
    """旧行 / 无证据时如实报 missing，绝不返回 0 天冒充「刚发布」。"""
    monkeypatch.setattr(routes_lifecycle, "get_session", db.get_session)
    _stub_up_metrics(monkeypatch)
    _seed_quality_rows(db, bvid="BV1AGE00003")

    response = client.get("/api/hotspot/lifecycle", params={"tid": 4})
    assert response.status_code == 200
    item = response.json()["data"]["items"][0]

    assert item["metadata"]["age_status"] in {"missing", "unknown"}
    assert item["metrics"]["age_days"] is None


def test_lifecycle_v1_path_does_not_receive_as_of(client, db, monkeypatch):
    """v1 工厂不认 ``as_of_epoch_s``：路由只对 v2 透传，否则 registry 直接炸。"""
    monkeypatch.setattr(routes_lifecycle, "get_session", db.get_session)
    _stub_up_metrics(monkeypatch)
    _seed_pubdate_rows(db, bvid="BV1AGE00004")

    response = client.get(
        "/api/hotspot/lifecycle", params={"tid": 4, "algorithm": "heuristic_v1"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["algorithm_version"] == "heuristic_v1"
