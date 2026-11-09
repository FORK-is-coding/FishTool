"""代理层：响应信封、方法白名单、状态码透传、token 占位、API Key 写保护。"""
from __future__ import annotations

import asyncio
import logging

import pytest

from fishtool_adapter.client import EngineUnavailable
from fishtool_adapter.constants import engine_cfg
from fishtool_adapter.proxy import EngineProxy, api_key_blocked, envelope, is_long_request, is_safe_path

log = logging.getLogger("test")


class FakeSup:
    def __init__(self, state="running"):
        self.state = state
        self.last_error = ""


class FakeClient:
    def __init__(self, result=(200, {"success": True, "data": {"x": 1}}), exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    async def call(self, method, path, *, query=None, json=None, timeout=120):
        self.calls.append({"method": method, "path": path, "query": query, "json": json, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.result


def run(coro):
    return asyncio.run(coro)


def make(state="running", **client_kw):
    client = FakeClient(**client_kw)
    proxy = EngineProxy(FakeSup(state), client, engine_cfg({}), log)
    return proxy, client


def test_envelope_shape_survives_bridge_strip():
    env = envelope(200, {"success": True, "status": "error", "data": {"a": 1}})
    # bridge: 只看顶层 status；成功时取 response.data.data
    assert env["status"] == "ok"
    stripped = env["data"]
    assert stripped == {"http_status": 200, "body": {"success": True, "status": "error", "data": {"a": 1}}}


@pytest.mark.parametrize("status", [200, 202, 404, 409, 422, 500])
def test_status_passthrough(status):
    proxy, client = make(result=(status, {"detail": "x"}))
    out = run(proxy.handle("comment/alerts", method="GET", query={"limit": "10"}, body_json=None, username="astrbot"))
    assert out == envelope(status, {"detail": "x"})
    assert client.calls[0]["path"] == "/api/comment/alerts"
    assert client.calls[0]["query"] == {"limit": "10"}


def test_tunnel_methods_and_query_forwarded():
    proxy, client = make()
    run(proxy.handle("comment/semantic/preferences", method="PUT", query={}, body_json={"a": 1}, username="astrbot"))
    run(proxy.handle("hotspot/collect", method="POST", query={"tid": 4, "limit": None}, body_json=None, username="astrbot"))
    assert client.calls[0]["method"] == "PUT" and client.calls[0]["json"] == {"a": 1}
    assert client.calls[1]["query"] == {"tid": 4}


def test_method_not_allowed():
    proxy, _ = make()
    out = run(proxy.handle("x", method="TRACE", query={}, body_json=None, username=None))
    assert out["data"]["http_status"] == 405


@pytest.mark.parametrize("bad", ["", "../etc", "a/../b", "a//b", "a b", "a?x=1", "a\\b", "x" * 400])
def test_invalid_paths_rejected(bad):
    assert not is_safe_path(bad)
    proxy, client = make()
    out = run(proxy.handle(bad, method="GET", query={}, body_json=None, username=None))
    assert out["data"]["http_status"] == 400
    assert not client.calls


def test_engine_not_running_returns_503_without_call():
    proxy, client = make(state="preparing")
    out = run(proxy.handle("auth/status", method="GET", query={}, body_json=None, username=None))
    assert out["data"]["http_status"] == 503
    assert out["data"]["body"]["error_code"] == "engine_not_running"
    assert not client.calls


def test_closed_proxy():
    proxy, client = make()
    proxy.closed = True
    out = run(proxy.handle("auth/status", method="GET", query={}, body_json=None, username=None))
    assert out["data"]["http_status"] == 503
    assert not client.calls


def test_local_token_never_reaches_iframe():
    proxy, client = make()
    for path in ("analysis/benchmark/local-token", "comment/semantic/local-token"):
        out = run(proxy.handle(path, method="GET", query={}, body_json=None, username="astrbot"))
        assert out["data"]["body"]["data"]["token"] == "proxy-managed"
    assert not client.calls


def test_long_timeout_selection():
    proxy, client = make()
    run(proxy.handle("analysis/self-diagnosis", method="POST", query={}, body_json={"uid": 1}, username=None))
    run(proxy.handle("auth/status", method="GET", query={}, body_json=None, username=None))
    assert client.calls[0]["timeout"] == 1800
    assert client.calls[1]["timeout"] == 120
    assert is_long_request("hotspot/tag-cloud/tasks")


def test_engine_unavailable_maps_to_502_504():
    proxy, _ = make(exc=EngineUnavailable("engine request timeout after 120s"))
    out = run(proxy.handle("auth/status", method="GET", query={}, body_json=None, username=None))
    assert out["data"]["http_status"] == 504
    proxy, _ = make(exc=EngineUnavailable("engine unreachable"))
    out = run(proxy.handle("auth/status", method="GET", query={}, body_json=None, username=None))
    assert out["data"]["http_status"] == 502


def test_api_key_write_block_is_opt_in():
    proxy, client = make()
    out = run(proxy.handle("config/llm", method="POST", query={}, body_json={}, username="api_key:1"))
    assert out["data"]["http_status"] == 200  # 默认放行（执行案 7.1 已定）
    proxy.cfg["block_api_key_write"] = True
    out = run(proxy.handle("config/llm", method="POST", query={}, body_json={}, username="api_key:1"))
    assert out["data"]["http_status"] == 403
    out = run(proxy.handle("comment/alerts", method="GET", query={}, body_json=None, username="api_key:1"))
    assert out["data"]["http_status"] == 200
    out = run(proxy.handle("config/llm", method="POST", query={}, body_json={}, username="astrbot"))
    assert out["data"]["http_status"] == 200


def test_api_key_blocked_rules():
    assert api_key_blocked("api_key:1", "POST", "comment/monitor")
    assert api_key_blocked("api_key:1", "GET", "auth/status")
    assert api_key_blocked("api_key:1", "GET", "analysis/benchmark/local-token")
    assert not api_key_blocked("api_key:1", "GET", "comment/alerts")
    assert not api_key_blocked("astrbot", "POST", "config/llm")
