"""桌面内嵌服务监听地址与配置接口本机校验（issue #5 第 1 点 B）。

覆盖对象：
- desktop/main_window.py：源码模式 ``app.py web`` 子进程参数、冻结模式进程内
  ``uvicorn.Config(host=...)``
- web/main.py 挂载的 ``/api/config`` 路由：Host / Origin 不在本机白名单时 403

``app.py web`` 的 ``--host`` 默认值由 tests/test_app_entry.py 覆盖。

测试策略：
- 桌面启动逻辑在 Qt 窗口方法 / 后台线程里，不便直接执行，读源码做 AST 静态断言。
- 路由校验用真实 ``web.main.app``，不进入 lifespan；ConfigManager 换成内存替身，
  不读写仓库配置。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
LOOPBACK = "127.0.0.1"


def _desktop_tree() -> ast.Module:
    """读取并解析 desktop/main_window.py。"""
    return ast.parse((ROOT / "desktop" / "main_window.py").read_text(encoding="utf-8"))


def _calls(tree: ast.Module, name: str) -> list:
    """收集末段名称为 ``name`` 的调用表达式，如 ``uvicorn.Config`` -> ``Config``。"""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if attr == name:
            found.append(node)
    return found


# ---------------------------------------------------------------------------
# 桌面内嵌服务监听地址
# ---------------------------------------------------------------------------


def test_desktop_subprocess_passes_loopback_host():
    """源码模式的 app.py web 子进程显式只监听回环地址。"""
    popens = _calls(_desktop_tree(), "Popen")
    assert len(popens) == 1
    argv = popens[0].args[0]
    assert isinstance(argv, ast.List)
    values = [item.value if isinstance(item, ast.Constant) else None for item in argv.elts]
    assert "web" in values
    assert "--host" in values
    assert values[values.index("--host") + 1] == LOOPBACK


def test_desktop_frozen_inprocess_server_listens_on_loopback():
    """桌面打包模式的进程内 uvicorn 只监听回环地址。"""
    hosts = [
        keyword.value.value
        for call in _calls(_desktop_tree(), "Config")
        for keyword in call.keywords
        if keyword.arg == "host" and isinstance(keyword.value, ast.Constant)
    ]
    assert hosts == [LOOPBACK]


def test_desktop_source_has_no_all_interfaces_host():
    """桌面窗口源码里不再出现 0.0.0.0 监听地址（含启动日志）。"""
    text = (ROOT / "desktop" / "main_window.py").read_text(encoding="utf-8")
    assert "0.0.0.0" not in text


# ---------------------------------------------------------------------------
# /api/config 本机 Host / Origin 校验
# ---------------------------------------------------------------------------


class _FakeConfig:
    """最小配置替身：llm 命名空间为空、无密钥。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get(self, key, default=None):
        """普通配置一律返回默认值。"""
        return default

    def get_secret(self, key, default=None):
        """未配置任何密钥。"""
        return default


@pytest.fixture()
def config_app(monkeypatch):
    """真实 web.main.app，配置管理器换成内存替身。"""
    from web.main import app
    from web.routers import config as config_module

    monkeypatch.delenv("FISHTOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(config_module, "ConfigManager", _FakeConfig)
    return app


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8000", "http://localhost:8000"])
def test_config_route_allows_loopback_host(config_app, base_url):
    """本机 Host 访问配置接口正常返回。"""
    response = TestClient(config_app, base_url=base_url).get("/api/config/llm")
    assert response.status_code == 200
    assert response.json()["success"] is True


@pytest.mark.parametrize("base_url", ["http://evil.example", "http://192.168.1.10:8000"])
def test_config_route_rejects_foreign_host(config_app, base_url):
    """DNS rebinding / 局域网 Host 未放行时，配置接口一律 403。"""
    client = TestClient(config_app, base_url=base_url)
    responses = [
        client.get("/api/config/"),
        client.get("/api/config/llm"),
        client.post("/api/config/llm/models", json={}),
    ]
    for response in responses:
        assert response.status_code == 403, response.request.url
        assert response.json()["detail"] == "local_host_required"


def test_config_route_rejects_cross_site_origin(config_app):
    """本机 Host 但 Origin 为外站时 403。"""
    client = TestClient(config_app, base_url="http://127.0.0.1:8000")
    response = client.get("/api/config/llm", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert response.json()["detail"] == "cross_site_request_blocked"


def test_config_route_lan_host_allowed_via_env(config_app, monkeypatch):
    """显式配置 FISHTOOL_ALLOWED_HOSTS 后，局域网 Host 可以访问。"""
    monkeypatch.setenv("FISHTOOL_ALLOWED_HOSTS", "localhost,127.0.0.1,192.168.1.10")
    response = TestClient(config_app, base_url="http://192.168.1.10:8000").get("/api/config/llm")
    assert response.status_code == 200
