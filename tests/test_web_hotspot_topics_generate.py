"""web.routers.hotspot.routes_topics_generate AI 选题生成接口测试（第4批 · web 段）。

覆盖对象：
- POST /topics/generate -> generate_topics

验证维度：
成功透传 / 请求参数透传到生成器 / 生成异常转 500 / LLM 客户端透传。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- 用契约级假 TopicGenerator 与假 api/llm 对象，避免真实 LLM 与网络调用。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers.hotspot import routes_topics_generate


class _FakeTopicGenerator:
    """记录构造与调用参数的假选题生成器。"""

    calls: list[dict] = []
    result: dict = {}
    error: Exception | None = None
    init_args: tuple = ()

    def __init__(self, api, llm_client) -> None:
        type(self).init_args = (api, llm_client)

    async def generate_topics(self, direction, zone_name, count, use_llm):
        """记录参数后返回预置结果或抛出注入异常。"""
        type(self).calls.append(
            {"direction": direction, "zone_name": zone_name, "count": count, "use_llm": use_llm}
        )
        if type(self).error is not None:
            raise type(self).error
        return type(self).result


@pytest.fixture()
def client(monkeypatch):
    """挂载 hotspot router 的测试客户端，并重置假生成器状态。"""
    from web.routers import hotspot

    _FakeTopicGenerator.calls = []
    _FakeTopicGenerator.result = {"topics": [{"title": "选题一"}], "count": 1, "llm_used": True}
    _FakeTopicGenerator.error = None
    _FakeTopicGenerator.init_args = ()

    api_sentinel = object()
    llm_sentinel = object()
    monkeypatch.setattr(routes_topics_generate, "TopicGenerator", _FakeTopicGenerator)
    monkeypatch.setattr(routes_topics_generate, "get_api", lambda: api_sentinel)
    monkeypatch.setattr(routes_topics_generate, "get_llm_client", lambda: llm_sentinel)

    app = FastAPI()
    app.include_router(hotspot.router, prefix="/api/hotspot")
    return TestClient(app)


def test_generate_topics_success_passthrough(client):
    """成功时应原样返回生成结果。"""
    response = client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "游戏攻略", "zone_name": "游戏", "count": 3, "use_llm": True},
    )
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": _FakeTopicGenerator.result}


def test_generate_topics_passes_request_fields(client):
    """direction/zone_name/count/use_llm 应完整透传。"""
    client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "搞怪", "zone_name": "生活", "count": 7, "use_llm": False},
    )
    assert _FakeTopicGenerator.calls[-1] == {
        "direction": "搞怪",
        "zone_name": "生活",
        "count": 7,
        "use_llm": False,
    }


def test_generate_topics_injects_api_and_llm_client(client):
    """生成器应以 get_api/get_llm_client 的返回值构造。"""
    client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "d", "zone_name": "z"},
    )
    api, llm = _FakeTopicGenerator.init_args
    assert api is not None
    assert llm is not None
    assert api is not llm


def test_generate_topics_applies_defaults(client):
    """缺省 count/use_llm 时使用 schema 默认值 10/True。"""
    client.post("/api/hotspot/topics/generate", json={"direction": "d", "zone_name": "z"})
    assert _FakeTopicGenerator.calls[-1]["count"] == 10
    assert _FakeTopicGenerator.calls[-1]["use_llm"] is True


def test_generate_topics_failure_returns_500(client):
    """生成抛错应转为 HTTP 500 并带错误详情。"""
    _FakeTopicGenerator.error = RuntimeError("LLM 超时")
    response = client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "d", "zone_name": "z"},
    )
    assert response.status_code == 500
    assert "生成选题失败" in response.json()["detail"]
