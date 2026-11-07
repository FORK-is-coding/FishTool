"""全局写请求防护（同源 + 本机 token）与 WebSocket Origin 校验测试。

背景：CORS 白名单只阻止跨站页面读取响应，不阻止请求执行。跨站网页可用
``fetch(url, {method: 'POST', mode: 'no-cors', body: new Blob([json])})`` 发出不带
Content-Type 的请求，FastAPI 仍按 JSON 解析请求体，从而跨站调用任意写接口。

覆盖：
- 跨站 Origin 的写请求被中间件 403 拦截，且业务处理函数不执行；
- 同源判定必须比较 scheme / host / port：任意本机端口的页面（5173 / 3000）算跨站；
- 无 Origin 的写请求必须带 ``X-Local-Token``（CLI 同样），GET 只读接口不受影响；
- ``/api/local-token`` 下发 token，``FISHTOOL_ALLOWED_HOSTS`` / ``FISHTOOL_ALLOWED_PORTS``
  覆盖与 local_guard 一致；
- ``/ws`` 拒绝跨站 Origin 握手，本机与无 Origin（桌宠客户端）可连接。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from web import main as web_main
from web.local_guard import LOCAL_TOKEN_HEADER, get_local_guard, is_cross_site_origin
from web.routers import config as config_module


def _token_headers(**extra: str) -> dict[str, str]:
    """构造带本机 token 的请求头。

    Args:
        **extra: 额外请求头。

    Returns:
        Dict[str, str]: 含 ``X-Local-Token`` 的请求头。
    """
    headers = {LOCAL_TOKEN_HEADER: get_local_guard().issue_token()}
    headers.update(extra)
    return headers


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
    response = client.request(
        method.upper(), "/api/config/reload", headers=_token_headers(Origin="https://evil.example")
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "cross_site_request_blocked"}
    assert client.reload_calls == []


def test_cross_site_post_without_content_type_is_blocked(client):
    """模拟 no-cors fetch：无 Content-Type 的 JSON 请求体同样被拦截。"""
    headers = _token_headers(Origin="https://evil.example", **{"Content-Type": ""})
    response = client.post(
        "/api/config/llm",
        content=b'{"api_key": "attacker", "base_url": "https://evil.example/v1"}',
        headers=headers,
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin",
    ["http://127.0.0.1:8000", "http://localhost:8000", "http://localhost:8080", "http://[::1]:8000"],
)
def test_local_origin_is_allowed(client, origin):
    """本机同源页面发起的写请求放行。"""
    response = client.post("/api/config/reload", headers=_token_headers(Origin=origin))
    assert response.status_code == 200
    assert client.reload_calls == ["reload"]


@pytest.mark.parametrize("origin", ["http://127.0.0.1:5173", "http://localhost:3000", "http://127.0.0.1:9000"])
def test_other_local_port_is_cross_site(client, origin):
    """#57 回归：本机其它端口的页面不是同源，带 token 也要拦。"""
    response = client.post("/api/config/reload", headers=_token_headers(Origin=origin))
    assert response.status_code == 403
    assert response.json() == {"detail": "cross_site_request_blocked"}
    assert client.reload_calls == []


def test_allowed_ports_override_is_respected(client, monkeypatch):
    """显式配置 FISHTOOL_ALLOWED_PORTS 后，该端口的本机页面被视为同源。"""
    monkeypatch.setenv("FISHTOOL_ALLOWED_PORTS", "5173")
    response = client.post("/api/config/reload", headers=_token_headers(Origin="http://127.0.0.1:5173"))
    assert response.status_code == 200
    assert client.reload_calls == ["reload"]


def test_request_without_origin_requires_token(client):
    """#79 回归：无 Origin（CLI / 脚本）的写请求必须带本机 token。"""
    response = client.post("/api/config/reload")
    assert response.status_code == 403
    assert response.json() == {"detail": "local_token_required"}
    assert client.reload_calls == []


def test_request_without_origin_and_with_token_is_allowed(client):
    """带 token 的无 Origin 写请求（CLI / 桌面内嵌页）放行。"""
    response = client.post("/api/config/reload", headers=_token_headers())
    assert response.status_code == 200
    assert client.reload_calls == ["reload"]


def test_wrong_token_is_rejected(client):
    """错误 token 与缺失 token 一样被拒。"""
    response = client.post("/api/config/reload", headers={LOCAL_TOKEN_HEADER: "not-the-token"})
    assert response.status_code == 403
    assert response.json() == {"detail": "local_token_required"}


def test_foreign_host_write_is_rejected(client):
    """#79：非本机 Host 的写请求即使带正确 token 也 403（DNS rebinding 防线）。"""
    foreign = TestClient(web_main.app, base_url="http://evil.example")
    token = get_local_guard().issue_token()
    response = foreign.post("/api/config/reload", headers={LOCAL_TOKEN_HEADER: token})
    assert response.status_code == 403
    assert response.json() == {"detail": "local_host_required"}
    assert client.reload_calls == []


def test_foreign_host_cannot_fetch_write_token(client):
    """外站 Host 连 token 都取不到：/api/local-token 由 require_local_read 守 Host。"""
    foreign = TestClient(web_main.app, base_url="http://evil.example")
    response = foreign.get("/api/local-token")
    assert response.status_code == 403
    assert response.json() == {"detail": "local_host_required"}


def test_local_token_endpoint_issues_usable_token(client):
    """同源页面可从 /api/local-token 取到写接口所需的 token。"""
    response = client.get("/api/local-token")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["header"] == LOCAL_TOKEN_HEADER
    token = payload["data"]["token"]
    assert get_local_guard().verify_token(token)
    write = client.post("/api/config/reload", headers={LOCAL_TOKEN_HEADER: token})
    assert write.status_code == 200


def test_local_token_endpoint_stays_read_only_and_origin_guarded(client):
    """token 端点自身不放宽：跨站 Origin 的 GET 仍被拒（Host/Origin 校验）。"""
    response = client.get("/api/local-token", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json() == {"detail": "cross_site_request_blocked"}


def test_cross_site_get_is_not_blocked_by_write_guard(client):
    """安全方法不经写拦截（跨站读取响应由 CORS 负责）。"""
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200


def test_allowed_hosts_override_is_respected(client, monkeypatch):
    """显式配置 FISHTOOL_ALLOWED_HOSTS 后，局域网来源可放行，与 local_guard 规则一致。"""
    monkeypatch.setenv("FISHTOOL_ALLOWED_HOSTS", "127.0.0.1,192.168.1.10")
    response = client.post(
        "/api/config/reload", headers=_token_headers(Origin="http://192.168.1.10:8000")
    )
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
