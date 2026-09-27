"""web.routers.hotspot.routes_activities 活动追踪接口测试（第4批 · web 段）。

覆盖对象：
- POST /activities -> fetch_activities

验证维度：
成功透传 / include_ugc 与 zone 透传 / 非法 zone 回退 all / 采集异常转 500。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- 用契约级假 ActivityTracker 与假 api 实例，避免真实网络请求。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers.hotspot import routes_activities


class _FakeTracker:
    """记录调用参数的假活动追踪器。"""

    SUPPORTED_ZONES = ["all", "game", "anime", "paint"]

    calls: list[dict] = []
    result: dict = {}
    error: Exception | None = None

    def __init__(self, api) -> None:
        self.api = api

    async def fetch_all_activities(self, include_ugc: bool = True, zone: str = "all") -> dict:
        """记录参数后返回预置结果或抛出注入异常。"""
        type(self).calls.append({"include_ugc": include_ugc, "zone": zone})
        if type(self).error is not None:
            raise type(self).error
        return type(self).result


@pytest.fixture()
def client(monkeypatch):
    """挂载 hotspot router 的测试客户端，并重置假追踪器状态。"""
    from web.routers import hotspot

    _FakeTracker.calls = []
    _FakeTracker.result = {"official": [{"title": "拜年祭"}], "ugc": [], "total_count": 1}
    _FakeTracker.error = None

    monkeypatch.setattr(routes_activities, "ActivityTracker", _FakeTracker)
    monkeypatch.setattr(routes_activities, "get_api", lambda: object())

    app = FastAPI()
    app.include_router(hotspot.router, prefix="/api/hotspot")
    return TestClient(app)


def test_activities_success_passthrough(client):
    """成功时应原样返回 tracker 结果。"""
    response = client.post("/api/hotspot/activities", json={"include_ugc": True, "zone": "game"})
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": _FakeTracker.result}


def test_activities_passes_include_ugc_flag(client):
    """include_ugc=False 应透传给 tracker。"""
    client.post("/api/hotspot/activities", json={"include_ugc": False, "zone": "game"})
    assert _FakeTracker.calls[-1]["include_ugc"] is False


def test_activities_invalid_zone_falls_back_to_all(client):
    """非法 zone 应回退为 all，不报错。"""
    client.post("/api/hotspot/activities", json={"include_ugc": True, "zone": "unknown"})
    assert _FakeTracker.calls[-1]["zone"] == "all"


def test_activities_uses_default_request_body(client):
    """空请求体时使用 ActivityRequest 默认值（include_ugc=True, zone=all）。"""
    response = client.post("/api/hotspot/activities", json={})
    assert response.status_code == 200
    assert _FakeTracker.calls[-1] == {"include_ugc": True, "zone": "all"}


def test_activities_failure_returns_500(client):
    """采集抛错应转为 HTTP 500 并带错误详情。"""
    _FakeTracker.error = RuntimeError("接口限流")
    response = client.post("/api/hotspot/activities", json={"include_ugc": True})
    assert response.status_code == 500
    assert "拉取活动失败" in response.json()["detail"]
