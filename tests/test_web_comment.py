"""web.routers.comment 评论监控接口测试（第4批 · web 段）。

覆盖对象：
- POST /collect 、POST /monitor 、GET /monitor/progress
- GET  /dashboard
- GET  /resident/status 、POST /resident/enable|pause|stop
- POST /monitor/account
- GET  /alerts 、PUT /alerts/{alert_id}/read 、POST /keywords

验证维度：
策略白名单 400 / 采集与监控成功与失败 / 进度快照 / 大屏无数据与有数据两分支 /
常驻监控 503 与控制指令透传 / 账号监控 / 预警列表与已读 404 / 自定义关键词。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- 采集器/监控器/常驻服务全部用契约级假对象；大屏走 tmp SQLite，绝不触碰仓库数据库。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import DatabaseManager, Comment, Video
from web.routers import comment as comment_module


# ---------------------------------------------------------------------------
# 契约级假对象
# ---------------------------------------------------------------------------


class _FakeCollector:
    """契约级假评论采集器。"""

    STRATEGY_FAST = "fast"
    STRATEGY_NORMAL = "normal"
    STRATEGY_FULL = "full"

    comments: list = [{"rpid": "1", "content": "你好"}]
    save_result: dict = {"warning": None, "saved": 1}
    progress: dict = {"phase": "done", "collected": 1, "limit": None, "finished": True}
    collect_error: Exception | None = None

    def __init__(self, api) -> None:
        self.api = api
        self.last_save_result = type(self).save_result

    async def collect_video_comments(self, bvid, strategy, max_count):
        """返回预置评论或抛出注入异常。"""
        if type(self).collect_error is not None:
            raise type(self).collect_error
        return type(self).comments

    @classmethod
    def get_progress(cls, bvid):
        """返回预置进度快照。"""
        return cls.progress


class _FakeDeduplicator:
    """契约级假去重器：返回 None 表示未触发去重。"""

    def deduplicate(self, comments):
        """返回 None 让调用方回退到原始评论。"""
        return None


class _FakeMonitor:
    """契约级假评论监控器。"""

    video_result: dict = {"bvid": "BV1", "total_comments": 3}
    account_result: dict = {"uid": "1", "monitored_videos": 2}
    alerts: list = [{"id": 1, "level": "high"}]
    read_result: bool = True
    video_error: Exception | None = None
    account_error: Exception | None = None
    alerts_error: Exception | None = None
    read_error: Exception | None = None
    added_keywords: list = []
    keyword_error: Exception | None = None

    def __init__(self) -> None:
        self.deduplicator = _FakeDeduplicator()
        self._viz = {"built": True}

    async def monitor_video(self, bvid, enable_dedup, enable_sentiment, strategy):
        """返回预置监控结果或抛出注入异常。"""
        if type(self).video_error is not None:
            raise type(self).video_error
        return type(self).video_result

    async def monitor_user_account(self, uid, video_limit, strategy):
        """返回预置账号监控结果或抛出注入异常。"""
        if type(self).account_error is not None:
            raise type(self).account_error
        return type(self).account_result

    async def get_alerts(self, bvid=None, level=None, is_read=None, limit=50):
        """返回预置预警列表或抛出注入异常。"""
        if type(self).alerts_error is not None:
            raise type(self).alerts_error
        return type(self).alerts

    async def mark_alert_read(self, alert_id):
        """返回预置已读结果或抛出注入异常。"""
        if type(self).read_error is not None:
            raise type(self).read_error
        return type(self).read_result

    def add_custom_keywords(self, keywords):
        """记录自定义关键词或抛出注入异常。"""
        if type(self).keyword_error is not None:
            raise type(self).keyword_error
        type(self).added_keywords.append(list(keywords))

    def _build_visualization_data(self, raw_comments, processed, dedup_result):
        """返回预置大屏数据。"""
        return self._viz


class _FakeMonitorService:
    """契约级假常驻监控服务。"""

    calls: list = []

    def snapshot(self) -> dict:
        """返回预置状态卡片数据。"""
        return {"enabled": True, "targets": ["BV1"]}

    async def enable(self, bvids):
        """记录启用目标。"""
        type(self).calls.append(("enable", bvids))
        return {"enabled": True, "targets": bvids}

    async def pause(self):
        """记录暂停。"""
        type(self).calls.append(("pause", None))
        return {"paused": True}

    async def stop(self):
        """记录停止。"""
        type(self).calls.append(("stop", None))
        return {"stopped": True}


@pytest.fixture()
def db(tmp_path):
    """tmp 目录内的真实 SQLite 管理器。"""
    return DatabaseManager(str(tmp_path / "comment.db"))


@pytest.fixture()
def client(monkeypatch, db):
    """挂载 comment router 的测试客户端，并注入全部假依赖。"""
    _FakeCollector.comments = [{"rpid": "1", "content": "你好"}]
    _FakeCollector.save_result = {"warning": None, "saved": 1}
    _FakeCollector.collect_error = None
    _FakeCollector.progress = {"phase": "done", "collected": 1, "limit": None, "finished": True}
    _FakeMonitor.video_result = {"bvid": "BV1", "total_comments": 3}
    _FakeMonitor.account_result = {"uid": "1", "monitored_videos": 2}
    _FakeMonitor.alerts = [{"id": 1, "level": "high"}]
    _FakeMonitor.read_result = True
    _FakeMonitor.video_error = None
    _FakeMonitor.account_error = None
    _FakeMonitor.alerts_error = None
    _FakeMonitor.read_error = None
    _FakeMonitor.added_keywords = []
    _FakeMonitor.keyword_error = None
    _FakeMonitorService.calls = []

    monitor = _FakeMonitor()
    # 显式取模块对象再 patch：字符串形式在命名空间包上解析不稳定。
    import importlib

    web_main = importlib.import_module("web.main")
    monkeypatch.setattr(comment_module, "CommentCollector", _FakeCollector)
    monkeypatch.setattr(comment_module, "get_api", lambda: object())
    monkeypatch.setattr(comment_module, "get_monitor", lambda: monitor)
    monkeypatch.setattr(comment_module, "get_session", db.get_session)
    monkeypatch.setattr(web_main, "monitor_service", _FakeMonitorService())

    app = FastAPI()
    app.include_router(comment_module.router, prefix="/api/comment")
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /collect
# ---------------------------------------------------------------------------


def test_collect_success(client):
    """采集成功应返回评论与持久化结果。"""
    response = client.post("/api/comment/collect", json={"bvid": "BV1", "strategy": "normal"})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["bvid"] == "BV1"
    assert body["count"] == 1
    assert body["comments"] == [{"rpid": "1", "content": "你好"}]
    assert body["persistence"]["saved"] == 1


def test_collect_invalid_strategy_returns_400(client):
    """策略校验的 HTTPException(400) 应原样透传，不得被 except Exception 兜底成 500。"""
    response = client.post("/api/comment/collect", json={"bvid": "BV1", "strategy": "turbo"})
    assert response.status_code == 400
    # detail 不得再套娃输出 "400: ..."
    assert response.json()["detail"] == "无效的采集策略: turbo"


def test_collect_failure_returns_500(client):
    """采集异常应转 500。"""
    _FakeCollector.collect_error = RuntimeError("接口限流")
    response = client.post("/api/comment/collect", json={"bvid": "BV1"})
    assert response.status_code == 500
    assert "采集评论失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /monitor
# ---------------------------------------------------------------------------


def test_monitor_success(client):
    """单视频监控成功应返回监控结果。"""
    body = client.post("/api/comment/monitor", json={"bvid": "BV1", "strategy": "full"}).json()
    assert body == {"success": True, "data": {"bvid": "BV1", "total_comments": 3}}


def test_monitor_fast_strategy_returns_400(client):
    """监控只允许 normal/full；fast 的 400 应原样透传，不得被兜底成 500。"""
    response = client.post("/api/comment/monitor", json={"bvid": "BV1", "strategy": "fast"})
    assert response.status_code == 400
    assert response.json()["detail"] == "无效的采集策略: fast"


def test_monitor_failure_returns_500(client):
    """监控异常应转 500。"""
    _FakeMonitor.video_error = RuntimeError("采集失败")
    response = client.post("/api/comment/monitor", json={"bvid": "BV1"})
    assert response.status_code == 500
    assert "监控失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /monitor/progress
# ---------------------------------------------------------------------------


def test_monitor_progress_returns_snapshot(client):
    """进度接口应透传采集器内存进度。"""
    body = client.get("/api/comment/monitor/progress", params={"bvid": "BV1"}).json()
    assert body == {"success": True, "data": _FakeCollector.progress}


def test_monitor_progress_requires_bvid(client):
    """缺少 bvid 应返回 422。"""
    assert client.get("/api/comment/monitor/progress").status_code == 422


# ---------------------------------------------------------------------------
# GET /dashboard
# ---------------------------------------------------------------------------


def test_dashboard_without_any_video(client):
    """无任何视频时应返回空大屏与提示文案。"""
    body = client.get("/api/comment/dashboard").json()
    assert body["success"] is True
    assert body["data"]["video"] is None
    assert body["data"]["message"] == "暂无已落库评论数据"
    assert "dedup_statistics" in body["data"]["visualization"]


def test_dashboard_with_seeded_video(client, db):
    """有评论数据时应返回视频信息与可视化结构。"""
    session = db.get_session()
    try:
        video = Video(bvid="BV1DASH", title="大屏视频")
        session.add(video)
        session.commit()
        session.add(
            Comment(
                rpid="d-1",
                video_id=video.id,
                uid=1,
                uname="观众",
                content="不错",
                ctime=datetime(2030, 1, 1, 12, 0, 0),
                like=3,
                sentiment="positive",
            )
        )
        session.commit()
    finally:
        session.close()

    body = client.get("/api/comment/dashboard", params={"bvid": "BV1DASH"}).json()
    assert body["data"]["video"] == {"bvid": "BV1DASH", "title": "大屏视频"}
    assert body["data"]["visualization"] == {"built": True}
    assert body["data"]["message"] == "已加载历史监控数据"


def test_dashboard_query_failure_returns_500(client, monkeypatch):
    """大屏查询失败（try 内）应转 500。"""

    class _BoomSession:
        """查询即抛错的契约级会话替身。"""

        def query(self, *args, **kwargs):
            """模拟 SQL 执行失败。"""
            raise RuntimeError("db down")

        def close(self) -> None:
            """无需清理。"""
            return None

    monkeypatch.setattr(comment_module, "get_session", lambda: _BoomSession())
    response = client.get("/api/comment/dashboard")
    assert response.status_code == 500
    assert "读取评论大屏失败" in response.json()["detail"]


def test_dashboard_session_open_failure_propagates(client, monkeypatch):
    """get_session 在 try 之外，获取失败会直接抛出（由 TestClient 暴露）。"""

    def boom():
        """模拟数据库连接不可用。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(comment_module, "get_session", boom)
    with pytest.raises(RuntimeError):
        client.get("/api/comment/dashboard")


# ---------------------------------------------------------------------------
# 常驻监控
# ---------------------------------------------------------------------------


def test_resident_status_ok(client):
    """常驻监控已初始化时应返回快照。"""
    body = client.get("/api/comment/resident/status").json()
    assert body == {"success": True, "data": {"enabled": True, "targets": ["BV1"]}}


def test_resident_status_unavailable_returns_503(client, monkeypatch):
    """常驻监控未初始化时应返回 503。"""
    import importlib

    monkeypatch.setattr(importlib.import_module("web.main"), "monitor_service", None)
    assert client.get("/api/comment/resident/status").status_code == 503


def test_resident_enable_passes_bvids(client):
    """启用应透传目标 BV 列表。"""
    body = client.post("/api/comment/resident/enable", json={"bvids": ["BV1", "BV2"]}).json()
    assert body["data"]["targets"] == ["BV1", "BV2"]
    assert _FakeMonitorService.calls[-1] == ("enable", ["BV1", "BV2"])


def test_resident_enable_without_bvids_defaults_to_none(client):
    """未传 bvids 时应沿用已保存目标（传 None）。"""
    client.post("/api/comment/resident/enable", json={})
    assert _FakeMonitorService.calls[-1] == ("enable", None)


def test_resident_pause_and_stop(client):
    """暂停与停止应分别调用服务方法。"""
    assert client.post("/api/comment/resident/pause").json()["data"] == {"paused": True}
    assert client.post("/api/comment/resident/stop").json()["data"] == {"stopped": True}
    assert [c[0] for c in _FakeMonitorService.calls] == ["pause", "stop"]


def test_resident_enable_unavailable_returns_503(client, monkeypatch):
    """未初始化时常驻控制接口应返回 503。"""
    import importlib

    monkeypatch.setattr(importlib.import_module("web.main"), "monitor_service", None)
    assert client.post("/api/comment/resident/enable", json={}).status_code == 503


# ---------------------------------------------------------------------------
# POST /monitor/account
# ---------------------------------------------------------------------------


def test_monitor_account_success(client):
    """账号监控成功应返回汇总结果。"""
    body = client.post("/api/comment/monitor/account", json={"uid": "1", "video_limit": 5}).json()
    assert body == {"success": True, "data": {"uid": "1", "monitored_videos": 2}}


def test_monitor_account_invalid_strategy_returns_400(client):
    """账号监控策略非法的 400 同样应原样透传，不得被兜底成 500。"""
    response = client.post("/api/comment/monitor/account", json={"uid": "1", "strategy": "fast"})
    assert response.status_code == 400
    assert response.json()["detail"] == "无效的采集策略: fast"


def test_monitor_account_failure_returns_500(client):
    """账号监控异常应转 500。"""
    _FakeMonitor.account_error = RuntimeError("账号抓取失败")
    response = client.post("/api/comment/monitor/account", json={"uid": "1"})
    assert response.status_code == 500
    assert "账号监控失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 预警
# ---------------------------------------------------------------------------


def test_get_alerts_returns_count(client):
    """预警列表应返回 alerts 与 count。"""
    body = client.get("/api/comment/alerts", params={"limit": 10}).json()
    assert body == {"success": True, "alerts": [{"id": 1, "level": "high"}], "count": 1}


def test_get_alerts_failure_returns_500(client):
    """预警查询异常应转 500。"""
    _FakeMonitor.alerts_error = RuntimeError("查询失败")
    response = client.get("/api/comment/alerts")
    assert response.status_code == 500


def test_mark_alert_read_success(client):
    """标记已读成功应返回成功文案。"""
    body = client.put("/api/comment/alerts/3/read").json()
    assert body == {"success": True, "message": "预警已标记为已读"}


def test_mark_alert_read_missing_returns_404(client):
    """预警不存在应返回 404。"""
    _FakeMonitor.read_result = False
    response = client.put("/api/comment/alerts/999/read")
    assert response.status_code == 404
    assert response.json()["detail"] == "预警不存在"


def test_mark_alert_read_failure_returns_500(client):
    """标记异常应转 500。"""
    _FakeMonitor.read_error = RuntimeError("写入失败")
    response = client.put("/api/comment/alerts/3/read")
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# 自定义关键词
# ---------------------------------------------------------------------------


def test_add_keywords_success(client):
    """添加关键词应回显数量与列表。"""
    response = client.post("/api/comment/keywords", json={"keywords": ["骗子", "取关"]})
    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "已添加 2 个关键词"
    assert body["keywords"] == ["骗子", "取关"]
    assert _FakeMonitor.added_keywords[-1] == ["骗子", "取关"]


def test_add_keywords_failure_returns_500(client):
    """添加异常应转 500。"""
    _FakeMonitor.keyword_error = RuntimeError("写入失败")
    response = client.post("/api/comment/keywords", json={"keywords": ["x"]})
    assert response.status_code == 500
    assert "添加关键词失败" in response.json()["detail"]
