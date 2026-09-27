"""web.routers.hotspot.routes_tag_cloud 词云任务接口测试（第4批 · web 段）。

覆盖对象：
- POST /tag-cloud/tasks -> start_tag_cloud_task
- GET  /tag-cloud/tasks/{task_id} -> get_tag_cloud_task
- POST /tag-cloud -> generate_tag_cloud

验证维度：
任务创建与非法分区 400 / 任务查询命中与未命中 404 / 同步生成结果字段归一 /
参数错误 400 / 内部异常 500。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- 用契约级假 TagCloudGenerator，并把后台协程替换为无副作用替身，避免真实采集。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.exceptions import ValidationError
from web.routers.hotspot import routes_tag_cloud


class _FakeGenerator:
    """契约级词云生成器替身：同时提供分区校验与异步生成。"""

    collectable: bool = True
    result: dict = {}
    error: Exception | None = None

    def __init__(self, api) -> None:
        self.api = api

    @staticmethod
    def is_collectable_zone(zone_name: str) -> bool:
        """返回预置的可采集判定。"""
        return _FakeGenerator.collectable

    async def generate_cloud_data(self, zone_name, limit, top_n, progress_callback=None):
        """返回预置结果或抛出注入异常。"""
        if _FakeGenerator.error is not None:
            raise _FakeGenerator.error
        return _FakeGenerator.result


async def _noop_run_task(task_id, request) -> None:
    """后台任务替身：不执行真实采集。"""
    return None


@pytest.fixture()
def client(monkeypatch):
    """挂载 hotspot router 的测试客户端，并注入假生成器与独立任务表。"""
    from web.routers import hotspot

    _FakeGenerator.collectable = True
    _FakeGenerator.result = {"word_frequency": {"原神": 5}, "total_videos": 20}
    _FakeGenerator.error = None

    monkeypatch.setattr(routes_tag_cloud, "TagCloudGenerator", _FakeGenerator)
    monkeypatch.setattr(routes_tag_cloud, "get_api", lambda: object())
    monkeypatch.setattr(routes_tag_cloud, "_run_tag_cloud_task", _noop_run_task)
    monkeypatch.setattr(routes_tag_cloud, "_tag_cloud_tasks", {})

    app = FastAPI()
    app.include_router(hotspot.router, prefix="/api/hotspot")
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /tag-cloud/tasks
# ---------------------------------------------------------------------------


def test_start_task_creates_running_task(client):
    """合法分区应创建 running 任务并返回 task_id。"""
    response = client.post("/api/hotspot/tag-cloud/tasks", json={"zone_name": "游戏"})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["task_id"]

    task = routes_tag_cloud._tag_cloud_tasks[body["task_id"]]
    assert task["status"] == "running"
    assert task["stage"] == "queued"
    assert task["progress"] == 2
    assert task["result"] is None


def test_start_task_rejects_unknown_zone(client):
    """不可采集分区应返回 400。"""
    _FakeGenerator.collectable = False
    response = client.post("/api/hotspot/tag-cloud/tasks", json={"zone_name": "不存在的区"})
    assert response.status_code == 400
    assert "未知的分区名称" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /tag-cloud/tasks/{task_id}
# ---------------------------------------------------------------------------


def test_get_task_hides_internal_timing_field(client):
    """任务查询应返回状态但隐藏 started_monotonic。"""
    routes_tag_cloud._tag_cloud_tasks["abc"] = {
        "task_id": "abc",
        "status": "running",
        "stage": "collecting",
        "progress": 40,
        "message": "采集中",
        "estimated_seconds": 12,
        "started_monotonic": 123.45,
        "result": None,
    }

    response = client.get("/api/hotspot/tag-cloud/tasks/abc")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["progress"] == 40
    assert "started_monotonic" not in data


def test_get_task_missing_returns_404(client):
    """任务不存在应返回 404。"""
    response = client.get("/api/hotspot/tag-cloud/tasks/not-exist")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /tag-cloud
# ---------------------------------------------------------------------------


def test_generate_tag_cloud_normalises_fields(client):
    """同步生成应补齐 video_count/tag_count/progress/stage 契约字段。"""
    response = client.post("/api/hotspot/tag-cloud", json={"zone_name": "游戏"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["word_frequency"] == {"原神": 5}
    assert data["video_count"] == 20
    assert data["tag_count"] == 1
    assert data["progress"] == 100
    assert data["stage"] == "completed"


def test_generate_tag_cloud_param_error_returns_400(client):
    """生成器抛 ValueError 应转为 400。"""
    _FakeGenerator.error = ValueError("分区名称无效")
    response = client.post("/api/hotspot/tag-cloud", json={"zone_name": "游戏"})
    assert response.status_code == 400


def test_generate_tag_cloud_validation_error_returns_400(client):
    """生成器抛 ValidationError 也应转为 400。"""
    _FakeGenerator.error = ValidationError("zone_name", "非法分区")
    response = client.post("/api/hotspot/tag-cloud", json={"zone_name": "游戏"})
    assert response.status_code == 400


def test_generate_tag_cloud_internal_error_returns_500(client):
    """生成器抛其他异常应转为 500。"""
    _FakeGenerator.error = RuntimeError("网络失败")
    response = client.post("/api/hotspot/tag-cloud", json={"zone_name": "游戏"})
    assert response.status_code == 500
    assert "生成词云失败" in response.json()["detail"]


def test_generate_tag_cloud_http_exception_is_not_wrapped(client):
    """try 内抛出的 HTTPException 必须原样透传，不得被 except Exception 兜底成 500。"""
    _FakeGenerator.error = HTTPException(status_code=400, detail="非法分区")
    response = client.post("/api/hotspot/tag-cloud", json={"zone_name": "游戏"})
    assert response.status_code == 400
    assert response.json()["detail"] == "非法分区"
