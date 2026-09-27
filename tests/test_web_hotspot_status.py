"""web.routers.hotspot.routes_status LLM 状态接口测试（第4批 · web 段）。

覆盖对象：
- GET /llm-status -> check_llm_status

验证维度：
未配置 / 已配置且有效 / 已配置但无效 三种分支的 configured 与 message 契约。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- monkeypatch routes_status.get_llm_client 返回契约级假客户端，避免真实 LLM 初始化。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers.hotspot import routes_status


class _FakeLLMClient:
    """只暴露 is_configured() 的假 LLM 客户端。"""

    def __init__(self, configured: bool) -> None:
        self._configured = configured

    def is_configured(self) -> bool:
        """返回预置的配置有效性。"""
        return self._configured


@pytest.fixture()
def client():
    """挂载 hotspot router 的测试客户端。"""
    from web.routers import hotspot

    app = FastAPI()
    app.include_router(hotspot.router, prefix="/api/hotspot")
    return TestClient(app)


def test_llm_status_not_configured(client, monkeypatch):
    """客户端为 None 时返回未配置。"""
    monkeypatch.setattr(routes_status, "get_llm_client", lambda: None)

    response = client.get("/api/hotspot/llm-status")
    assert response.status_code == 200
    assert response.json() == {"configured": False, "message": "LLM未配置"}


def test_llm_status_configured_ok(client, monkeypatch):
    """客户端 is_configured 为真时返回已配置。"""
    monkeypatch.setattr(routes_status, "get_llm_client", lambda: _FakeLLMClient(True))

    body = client.get("/api/hotspot/llm-status").json()
    assert body == {"configured": True, "message": "LLM已配置"}


def test_llm_status_configured_invalid(client, monkeypatch):
    """客户端存在但配置无效时返回配置无效。"""
    monkeypatch.setattr(routes_status, "get_llm_client", lambda: _FakeLLMClient(False))

    body = client.get("/api/hotspot/llm-status").json()
    assert body == {"configured": False, "message": "LLM配置无效"}
