"""跨站写请求防护与 WebSocket Origin 校验测试。

背景：CORS 白名单只阻止跨站页面读取响应，不阻止请求执行。跨站网页可用
``fetch(url, {method: 'POST', mode: 'no-cors', body: new Blob([json])})`` 发出不带
Content-Type 的请求，FastAPI 仍按 JSON 解析请求体，从而跨站调用任意写接口。

覆盖：
- 跨站 Origin 的写请求被中间件 403 拦截，且业务处理函数不执行；
- 同源判定必须比较 scheme / host / port：任意本机端口的页面（5173 / 3000）算跨站（#57）；
- 服务跑在自定义端口（如 ``start_web.bat 9000``）时，与请求同端口的页面算同源，HTTP 与 ``/ws`` 一致；
- 无 Origin 的写请求（CLI）同样必须携带 ``X-Local-Token``，否则 403；
- Host 不在白名单时写请求 403（防 DNS rebinding）；
- ``FISHTOOL_ALLOWED_HOSTS`` / ``FISHTOOL_ALLOWED_PORTS`` 覆盖与 local_guard 一致。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from web import local_guard as web_local_guard
from web import main as web_main
from web.local_guard import is_cross_site_origin
from web.routers import config as config_module

#: 测试用固定 token：每个用例都重置 LocalGuard 单例，保证跨用例不串。
TEST_TOKEN = "test-local-token"
AUTH = {"X-Local-Token": TEST_TOKEN}


@pytest.fixture(autouse=True)
def _fixed_local_token(monkeypatch):
    """把进程内 LocalGuard 单例固定成已知 token，避免用例之间互相污染。"""
    monkeypatch.setattr(web_local_guard, "_guard", web_local_guard.LocalGuard(token=TEST_TOKEN))


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
    monkeypatch.delenv("FISHTOOL_ALLOWED_PORTS", raising=False)
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
    """本机同源页面发起的写请求（携带 token）放行。"""
    response = client.post("/api/config/reload", headers={"Origin": origin, **AUTH})
    assert response.status_code == 200
    assert client.reload_calls == ["reload"]


@pytest.mark.parametrize("origin", ["http://127.0.0.1:5173", "http://localhost:3000", "http://127.0.0.1:9000"])
def test_other_local_port_is_cross_site(client, origin):
    """#57 回归：本机其它端口的页面不是同源。"""
    response = client.post("/api/config/reload", headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json() == {"detail": "cross_site_request_blocked"}
    assert client.reload_calls == []


def test_allowed_ports_override_is_respected(client, monkeypatch):
    """显式配置 FISHTOOL_ALLOWED_PORTS 后，该端口的本机页面被视为同源。"""
    monkeypatch.setenv("FISHTOOL_ALLOWED_PORTS", "5173")
    response = client.post("/api/config/reload", headers={"Origin": "http://127.0.0.1:5173", **AUTH})
    assert response.status_code == 200
    assert client.reload_calls == ["reload"]


def test_custom_server_port_same_origin_write_is_allowed(monkeypatch):
    """服务跑在 9000（README：start_web.bat 9000）时，9000 页面的写请求是同源。"""
    monkeypatch.delenv("FISHTOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("FISHTOOL_ALLOWED_PORTS", raising=False)
    custom = TestClient(web_main.app, base_url="http://localhost:9000")
    response = custom.post("/api/config/reload", headers={"Origin": "http://localhost:9000", **AUTH})
    assert response.status_code == 200


def test_request_without_origin_and_token_is_rejected(client):
    """无 Origin 的写请求（CLI）也必须带 token，否则 403 且不进入业务处理。"""
    response = client.post("/api/config/reload")
    assert response.status_code == 403
    assert response.json() == {"detail": "local_token_required"}
    assert client.reload_calls == []


def test_request_without_origin_with_token_is_allowed(client):
    """CLI / 桌面客户端带上 token 后，无 Origin 的写请求照常放行。"""
    response = client.post("/api/config/reload", headers=AUTH)
    assert response.status_code == 200
    assert client.reload_calls == ["reload"]


def test_same_origin_write_without_token_is_rejected(client):
    """同源页面也不能绕过 token：Origin 合法但缺 token 仍然 403。"""
    response = client.post("/api/config/reload", headers={"Origin": "http://127.0.0.1:8000"})
    assert response.status_code == 403
    assert response.json() == {"detail": "local_token_required"}
    assert client.reload_calls == []


def test_write_with_non_local_host_is_rejected(client):
    """Host 不在白名单（DNS rebinding 场景）时，即使带 token 也拒绝。"""
    response = client.post(
        "/api/config/reload",
        headers={**AUTH, "Host": "evil.example"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "local_host_required"}
    assert client.reload_calls == []


def test_local_token_endpoint_issues_token(client):
    """同源页面可通过 /api/local-token 获取写请求 token。"""
    response = client.get("/api/local-token")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["token"] == TEST_TOKEN
    assert body["data"]["header"] == "X-Local-Token"


def test_cross_site_get_is_not_blocked_by_write_guard(client):
    """安全方法不经写拦截（跨站读取响应由 CORS 负责）。"""
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200


def test_allowed_hosts_override_is_respected(client, monkeypatch):
    """显式配置 FISHTOOL_ALLOWED_HOSTS 后，局域网来源可放行，与 local_guard 规则一致。"""
    monkeypatch.setenv("FISHTOOL_ALLOWED_HOSTS", "127.0.0.1,192.168.1.10")
    response = client.post("/api/config/reload", headers={"Origin": "http://192.168.1.10:8000", **AUTH})
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
        # #57：端口是 Origin 的一部分，任意本机端口不再算同源
        ("http://127.0.0.1:5173", True),
        ("http://localhost:3000", True),
        ("http://127.0.0.1:9000", True),
        # 缺 scheme 的伪 Origin 无法构成同源
        ("127.0.0.1:8000", True),
    ],
)
def test_is_cross_site_origin(monkeypatch, origin, expected):
    """跨站判定：scheme + host + port 同时匹配才算同源；``null`` 视为跨站。"""
    monkeypatch.delenv("FISHTOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("FISHTOOL_ALLOWED_PORTS", raising=False)
    assert is_cross_site_origin(origin) is expected


def test_is_cross_site_origin_allows_custom_request_port(monkeypatch):
    """服务跑在自定义端口时，与请求同端口的本机页面视为同源。"""
    monkeypatch.delenv("FISHTOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("FISHTOOL_ALLOWED_PORTS", raising=False)
    assert is_cross_site_origin("http://127.0.0.1:8123", request_port=8123) is False
    assert is_cross_site_origin("http://127.0.0.1:8124", request_port=8123) is True


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


def test_websocket_accepts_same_origin_on_custom_port(monkeypatch):
    """回归：服务跑在 9000 时，同源页面的 /ws 握手不能被当成跨站（实时预警依赖它）。"""
    monkeypatch.delenv("FISHTOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("FISHTOOL_ALLOWED_PORTS", raising=False)
    custom = TestClient(web_main.app, base_url="http://localhost:9000")
    # TestClient 的 websocket_connect 不使用 base_url，Host 固定为 testserver；
    # 浏览器真实握手会带 Host: localhost:9000，这里显式还原。
    headers = {"Host": "localhost:9000", "Origin": "http://localhost:9000"}
    with custom.websocket_connect("/ws", headers=headers) as ws:
        assert len(web_main.manager.active_connections) == 1
        ws.send_text("ping")
    assert web_main.manager.active_connections == []


def test_websocket_rejects_other_local_port_on_custom_port(monkeypatch):
    """服务跑在 9000 时，5173 等其它本机端口的页面仍被拒。"""
    monkeypatch.delenv("FISHTOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("FISHTOOL_ALLOWED_PORTS", raising=False)
    custom = TestClient(web_main.app, base_url="http://localhost:9000")
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with custom.websocket_connect(
            "/ws", headers={"Host": "localhost:9000", "Origin": "http://localhost:5173"}
        ) as ws:
            ws.receive_text()
    assert excinfo.value.code == 1008
