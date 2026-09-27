"""web.routers.hotspot.routes_topic_library 选题库接口测试（第4批 · web 段）。

覆盖对象：
- GET /topics -> get_topic_library
- PUT /topics/{topic_id} -> update_topic_status

验证维度：
查询筛选透传与计数 / 更新成功文案 / 选题不存在 404 / 查询与更新异常 500。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- 用契约级假 TopicGenerator 记录筛选与更新参数，避免真实数据库访问。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers.hotspot import routes_topic_library


class _FakeTopicGenerator:
    """记录查询与更新调用的假选题生成器。"""

    library: list = []
    library_error: Exception | None = None
    update_result: bool = True
    update_error: Exception | None = None
    library_calls: list[dict] = []
    update_calls: list[dict] = []

    def __init__(self, api, llm_client) -> None:
        self.api = api
        self.llm_client = llm_client

    async def get_topic_library(self, zone_name=None, status=None, limit=50):
        """记录筛选参数后返回预置列表或抛错。"""
        type(self).library_calls.append({"zone_name": zone_name, "status": status, "limit": limit})
        if type(self).library_error is not None:
            raise type(self).library_error
        return type(self).library

    async def update_topic_status(self, topic_id, status):
        """记录更新参数后返回预置布尔或抛错。"""
        type(self).update_calls.append({"topic_id": topic_id, "status": status})
        if type(self).update_error is not None:
            raise type(self).update_error
        return type(self).update_result


@pytest.fixture()
def client(monkeypatch):
    """挂载 hotspot router 的测试客户端，并重置假生成器状态。"""
    from web.routers import hotspot

    _FakeTopicGenerator.library = [{"id": 1, "title": "选题一", "status": "pending"}]
    _FakeTopicGenerator.library_error = None
    _FakeTopicGenerator.update_result = True
    _FakeTopicGenerator.update_error = None
    _FakeTopicGenerator.library_calls = []
    _FakeTopicGenerator.update_calls = []

    monkeypatch.setattr(routes_topic_library, "TopicGenerator", _FakeTopicGenerator)
    monkeypatch.setattr(routes_topic_library, "get_api", lambda: object())
    monkeypatch.setattr(routes_topic_library, "get_llm_client", lambda: object())

    app = FastAPI()
    app.include_router(hotspot.router, prefix="/api/hotspot")
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /topics
# ---------------------------------------------------------------------------


def test_topic_library_returns_topics_and_count(client):
    """查询成功应返回 topics 与真实 count。"""
    response = client.get("/api/hotspot/topics")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["topics"] == _FakeTopicGenerator.library
    assert body["count"] == 1


def test_topic_library_forwards_filters(client):
    """zone_name/status/limit 查询参数应透传。"""
    client.get("/api/hotspot/topics", params={"zone_name": "游戏", "status": "pending", "limit": 20})
    assert _FakeTopicGenerator.library_calls[-1] == {
        "zone_name": "游戏",
        "status": "pending",
        "limit": 20,
    }


def test_topic_library_default_limit(client):
    """未传 limit 时默认 50。"""
    client.get("/api/hotspot/topics")
    assert _FakeTopicGenerator.library_calls[-1]["limit"] == 50


def test_topic_library_empty_result(client):
    """空结果应返回 count=0。"""
    _FakeTopicGenerator.library = []
    body = client.get("/api/hotspot/topics").json()
    assert body["topics"] == []
    assert body["count"] == 0


def test_topic_library_query_failure_returns_500(client):
    """查询抛错应转为 500。"""
    _FakeTopicGenerator.library_error = RuntimeError("数据库不可用")
    response = client.get("/api/hotspot/topics")
    assert response.status_code == 500
    assert "查询选题库失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# PUT /topics/{topic_id}
# ---------------------------------------------------------------------------


def test_update_topic_status_success(client):
    """更新成功应返回新状态文案。"""
    response = client.put("/api/hotspot/topics/7", json={"status": "adopted"})
    assert response.status_code == 200
    assert response.json() == {"success": True, "message": "选题状态已更新为 adopted"}
    assert _FakeTopicGenerator.update_calls[-1] == {"topic_id": 7, "status": "adopted"}


def test_update_topic_status_missing_returns_404(client):
    """生成器返回 False 时应返回 404。"""
    _FakeTopicGenerator.update_result = False
    response = client.put("/api/hotspot/topics/999", json={"status": "published"})
    assert response.status_code == 404
    assert "选题不存在" in response.json()["detail"]


def test_update_topic_status_failure_returns_500(client):
    """更新抛错应转为 500（非 404）。"""
    _FakeTopicGenerator.update_error = RuntimeError("写入失败")
    response = client.put("/api/hotspot/topics/7", json={"status": "adopted"})
    assert response.status_code == 500
    assert "更新选题失败" in response.json()["detail"]
