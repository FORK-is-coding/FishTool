"""全局跨站写请求拦截与 WebSocket Origin 校验测试。

背景：CORS 白名单只阻止跨站页面读取响应，不阻止请求执行。跨站网页可用
``fetch(url, {method: 'POST', mode: 'no-cors', body: new Blob([json])})`` 发出不带
Content-Type 的请求，FastAPI 仍按 JSON 解析请求体，从而跨站调用任意写接口。

覆盖：
- 跨站 Origin 的写请求被中间件 403 拦截，且业务处理函数不执行；
- 本机 Origin / 无 Origin / GET 请求不受影响；
- ``FISHTOOL_ALLOWED_HOSTS`` 覆盖与 local_guard 一致；
- ``/ws`` 拒绝跨站 Origin 握手，本机与无 Origin（桌宠客户端）可连接。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from web import main as web_main
from web.local_guard import is_cross_site_origin
from web.routers import config as config_module


@pytest.fixture()
def client(monkeypatch):
    """挂载真实 web.main.app，用 /api/config/reload 作探针，记录业务处理是否被执行。"""
    calls = []

    class _CountingConfig:
        """记录 reload 调用次数，用于证明被拦截的请求未进入业务处理。"""

        def reload(self):
            """记录一次重载。"""
            calls.append("reload")

    # reload 路由新建 ConfigManager 实例还是重载运行时单例属于路由内部实现，
    # 两处都换成计数替身，探针只关心「业务处理有没有执行」，不随该实现变化。
    monkeypatch.setattr(config_module, "ConfigManager", _CountingConfig)
    # core/__init__.py 以同名属性导出了单例实例，必须按模块路径取 core.config 子模块本身
    monkeypatch.setattr(importlib.import_module("core.config"), "config", _CountingConfig())
    monkeypatch.delenv("FISHTOOL_ALLOWED_HOSTS", raising=False)
    test_client = TestClient(web_main.app, base_url="http://127.0.0.1:8000")
    test_client.reload_calls = calls
    return test_client


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_cross_site_unsafe_methods_are_blocked(client, method):
    """跨站 Origin 的非安全方法一律 403，且不进入业务处理。"""
    response = client.request(method.upper(), "/api/config/reload", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json() == {"detail": "cross_site_request_blocked"}
    assert client.reload_calls == []


def test_cross_site_post_without_content_type_is_blocked(client):
    """模拟 no-cors fetch：无 Content-Type 的 JSON 请求体同样被拦截。"""
    response = client.post(
        "/api/config/llm",
        content=b'{"api_key": "attacker", "base_url": "https://evil.example/v1"}',
        headers={"Origin": "https://evil.example", "Content-Type": ""},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin",
    ["http://127.0.0.1:8000", "http://localhost:8000", "http://localhost:8080", "http://[::1]:8000"],
)
def test_local_origin_is_allowed(client, origin):
    """本机页面发起的写请求放行。"""
    response = client.post("/api/config/reload", headers={"Origin": origin})
    assert response.status_code == 200
    assert client.reload_calls == ["reload"]


def test_request_without_origin_is_allowed(client):
    """无 Origin（CLI / 桌面内嵌页同源请求 / 测试客户端）不受影响。"""
    response = client.post("/api/config/reload")
    assert response.status_code == 200
    assert client.reload_calls == ["reload"]


def test_cross_site_get_is_not_blocked_by_write_guard(client):
    """安全方法不经写拦截（跨站读取响应由 CORS 负责）。"""
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200


def test_allowed_hosts_override_is_respected(client, monkeypatch):
    """显式配置 FISHTOOL_ALLOWED_HOSTS 后，局域网来源可放行，与 local_guard 规则一致。"""
    monkeypatch.setenv("FISHTOOL_ALLOWED_HOSTS", "127.0.0.1,192.168.1.10")
    response = client.post("/api/config/reload", headers={"Origin": "http://192.168.1.10:8000"})
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (None, False),
        ("", False),
        ("http://127.0.0.1:8000", False),
        ("http://LOCALHOST:8000", False),
        ("https://evil.example", True),
        ("http://127.0.0.1.evil.example", True),
        ("null", True),
    ],
)
def test_is_cross_site_origin(monkeypatch, origin, expected):
    """跨站判定：仅允许集合内主机视为本机；``null``（沙箱 iframe / file://）视为跨站。"""
    monkeypatch.delenv("FISHTOOL_ALLOWED_HOSTS", raising=False)
    assert is_cross_site_origin(origin) is expected


def test_websocket_rejects_cross_site_origin(client):
    """跨站页面不能订阅实时预警。"""
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_text()
    assert excinfo.value.code == 1008
    assert web_main.manager.active_connections == []


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://localhost:8000"}])
def test_websocket_accepts_local_or_missing_origin(client, headers):
    """本机页面与无 Origin 的桌宠客户端可正常连接。"""
    with client.websocket_connect("/ws", headers=headers) as ws:
        assert len(web_main.manager.active_connections) == 1
        ws.send_text("ping")
    assert web_main.manager.active_connections == []
