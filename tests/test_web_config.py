"""web.routers.config 配置管理接口测试（第4批 · web 段）。

覆盖对象：
- GET  /            -> get_all_config
- GET  /llm         -> get_llm_config
- POST /llm/models  -> get_llm_models
- POST /llm         -> update_llm_config
- GET  /llm/usage   -> get_llm_usage
- PUT  /            -> update_config
- POST /reload      -> reload_config

验证维度：
脱敏规则 / LLM 读写与热生效 / 模型列表探测的成功与各类失败分支 /
用量按日聚合 / 单键更新 / 重载。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- ConfigManager 用内存替身，用量统计走 tmp SQLite，绝不触碰仓库配置与数据库。
- httpx 用契约级假 AsyncClient（含 TimeoutException/RequestError 契约），不发起真实请求。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import DatabaseManager, LLMUsage
from web.routers import config as config_module


# ---------------------------------------------------------------------------
# 契约级假配置
# ---------------------------------------------------------------------------


class _FakeConfig:
    """内存型配置管理器替身，支持点分路径读写与 secrets。"""

    plain: dict = {}
    secrets: dict = {}
    saved: int = 0
    reloaded: int = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    @classmethod
    def reset(cls) -> None:
        """重置类级状态，保证用例间隔离。"""
        cls.plain = {}
        cls.secrets = {}
        cls.saved = 0
        cls.reloaded = 0

    def get_secret(self, key, default=None):
        """读取密钥。"""
        return type(self).secrets.get(key, default)

    def save_secret(self, key, value):
        """写入密钥。"""
        type(self).secrets[key] = value

    def get(self, key, default=None):
        """按点分路径读取普通配置。"""
        node = type(self).plain
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key, value):
        """按点分路径写入普通配置，自动创建中间层级。"""
        parts = key.split(".")
        node = type(self).plain
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save_config(self):
        """记录落盘次数。"""
        type(self).saved += 1

    def reload(self):
        """记录重载次数。"""
        type(self).reloaded += 1

    @property
    def all(self):
        """返回普通配置副本（不含 secrets）。"""
        return {k: v for k, v in type(self).plain.items()}


# ---------------------------------------------------------------------------
# 契约级假 httpx
# ---------------------------------------------------------------------------


class _FakeTimeout(Exception):
    """模拟 httpx.TimeoutException。"""


class _FakeRequestError(Exception):
    """模拟 httpx.RequestError。"""


class _FakeResponse:
    """最小响应替身：status_code + json()。"""

    def __init__(self, status_code: int, payload=None, raise_json: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        """返回预置 payload，或抛出 ValueError 模拟非 JSON 响应。"""
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class _FakeAsyncClient:
    """契约级假 httpx.AsyncClient。"""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list = []

    async def __aenter__(self):
        """进入异步上下文。"""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """退出上下文不吞异常。"""
        return False

    async def get(self, url, headers=None):
        """调用注入的处理器并记录 URL。"""
        self.calls.append({"url": url, "headers": headers})
        return self._handler(url, headers)


def _install_httpx(monkeypatch, handler) -> None:
    """把 config 模块内的 httpx 换成契约级假命名空间。"""
    namespace = SimpleNamespace(
        AsyncClient=lambda **kwargs: _FakeAsyncClient(handler),
        TimeoutException=_FakeTimeout,
        RequestError=_FakeRequestError,
    )
    monkeypatch.setattr(config_module, "httpx", namespace)


@pytest.fixture()
def db(tmp_path):
    """tmp 目录内的真实 SQLite 管理器。"""
    return DatabaseManager(str(tmp_path / "config.db"))


@pytest.fixture()
def client(monkeypatch, db):
    """挂载 config router 的测试客户端，并注入假配置与隔离会话。"""
    # core/__init__.py 用 ``from .config import config`` 把 ``core`` 包的
    # ``config`` 属性覆盖成 ConfigManager 实例，因此 ``import core.config``
    # 的属性访问拿不到子模块，必须从 sys.modules 取真实模块对象。
    import sys

    core_config_module = sys.modules["core.config"]

    _FakeConfig.reset()
    monkeypatch.setattr(config_module, "ConfigManager", _FakeConfig)
    monkeypatch.setattr(config_module, "get_session", db.get_session)
    monkeypatch.setattr(core_config_module, "config", SimpleNamespace(reload=lambda: None))

    app = FastAPI()
    app.include_router(config_module.router, prefix="/api/config")
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


def test_get_all_config_without_key(client):
    """未配置密钥时返回普通配置且无脱敏字段。"""
    _FakeConfig.plain = {"app": {"name": "工具箱"}}
    body = client.get("/api/config/").json()
    assert body["success"] is True
    assert body["config"]["app"]["name"] == "工具箱"
    assert "api_key_masked" not in body["config"].get("llm", {})


def test_get_all_config_masks_api_key(client):
    """存在密钥时应返回前 8 后 4 脱敏串，并惰性创建 llm 节点。"""
    _FakeConfig.secrets["llm_api_key"] = "sk-1234567890abcdefGHIJ"
    body = client.get("/api/config/").json()
    masked = body["config"]["llm"]["api_key_masked"]
    assert masked == "sk-12345***" + "abcdefGHIJ"[-4:]
    assert "sk-1234567890abcdefGHIJ" not in str(body)


def test_get_all_config_failure_returns_500(client, monkeypatch):
    """配置读取异常应转 500。"""

    def boom(*args, **kwargs):
        """模拟配置构造失败。"""
        raise RuntimeError("broken")

    monkeypatch.setattr(config_module, "ConfigManager", boom)
    response = client.get("/api/config/")
    assert response.status_code == 500
    assert "获取配置失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /llm
# ---------------------------------------------------------------------------


def test_get_llm_config_returns_namespace(client):
    """应只返回 llm 命名空间。"""
    _FakeConfig.plain = {"llm": {"model": "gpt-x"}, "app": {"name": "x"}}
    body = client.get("/api/config/llm").json()
    assert body == {"success": True, "config": {"model": "gpt-x"}}


def test_get_llm_config_masks_key(client):
    """存在密钥时应附带脱敏字段。"""
    _FakeConfig.secrets["llm_api_key"] = "abcdefgh12345678ZZZZ"
    body = client.get("/api/config/llm").json()
    assert body["config"]["api_key_masked"].startswith("abcdefgh***")


def test_get_llm_config_failure_returns_500(client, monkeypatch):
    """读取失败应转 500。"""

    def boom(*args, **kwargs):
        """模拟配置构造失败。"""
        raise RuntimeError("broken")

    monkeypatch.setattr(config_module, "ConfigManager", boom)
    response = client.get("/api/config/llm")
    assert response.status_code == 500
    assert "获取LLM配置失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /llm/models
# ---------------------------------------------------------------------------


def test_llm_models_requires_base_url(client):
    """缺少 Base URL 时应返回可读失败。"""
    body = client.post("/api/config/llm/models", json={"api_key": "k"}).json()
    assert body == {"success": False, "models": [], "message": "请先填写 Base URL"}


def test_llm_models_requires_api_key(client):
    """缺少 API Key 时应返回可读失败。"""
    body = client.post(
        "/api/config/llm/models", json={"api_key": "", "base_url": "https://x/v1"}
    ).json()
    assert body["message"] == "请先填写 API Key"


def test_llm_models_success_dedupes_and_sorts(client, monkeypatch):
    """成功时应去重并排序模型名，且命中 /models 端点。"""
    captured = {}

    def handler(url, headers):
        """返回两个模型（含重复）。"""
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(200, {"data": [{"id": "b-model"}, {"id": "a-model"}, {"id": "b-model"}]})

    _install_httpx(monkeypatch, handler)

    body = client.post(
        "/api/config/llm/models",
        json={"api_key": "secret", "base_url": "https://api.example/v1"},
    ).json()

    assert body["success"] is True
    assert body["models"] == ["a-model", "b-model"]
    assert captured["url"] == "https://api.example/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer secret"


def test_llm_models_http_error_returns_message(client, monkeypatch):
    """HTTP 4xx/5xx 应返回带状态码的可读失败，不泄漏响应正文。"""
    _install_httpx(monkeypatch, lambda url, headers: _FakeResponse(401, {"error": "bad"}))

    body = client.post(
        "/api/config/llm/models",
        json={"api_key": "k", "base_url": "https://x/v1"},
    ).json()
    assert body["success"] is False
    assert "HTTP 401" in body["message"]


def test_llm_models_empty_payload_reports_incompatible(client, monkeypatch):
    """接口返回空模型列表时应提示格式不兼容。"""
    _install_httpx(monkeypatch, lambda url, headers: _FakeResponse(200, {"data": []}))

    body = client.post(
        "/api/config/llm/models",
        json={"api_key": "k", "base_url": "https://x/v1"},
    ).json()
    assert body["message"] == "接口返回为空或格式不兼容"


def test_llm_models_timeout_branch(client, monkeypatch):
    """超时应返回可读失败。"""

    def handler(url, headers):
        """模拟请求超时。"""
        raise _FakeTimeout("timeout")

    _install_httpx(monkeypatch, handler)
    body = client.post(
        "/api/config/llm/models",
        json={"api_key": "k", "base_url": "https://x/v1"},
    ).json()
    assert body["success"] is False
    assert "超时" in body["message"]


def test_llm_models_request_error_branch(client, monkeypatch):
    """连接错误应返回可读失败。"""

    def handler(url, headers):
        """模拟连接失败。"""
        raise _FakeRequestError("conn refused")

    _install_httpx(monkeypatch, handler)
    body = client.post(
        "/api/config/llm/models",
        json={"api_key": "k", "base_url": "https://x/v1"},
    ).json()
    assert body["success"] is False
    assert "无法连接模型服务" in body["message"]


def test_llm_models_invalid_json_branch(client, monkeypatch):
    """非 JSON 响应应返回可读失败。"""
    _install_httpx(monkeypatch, lambda url, headers: _FakeResponse(200, raise_json=True))

    body = client.post(
        "/api/config/llm/models",
        json={"api_key": "k", "base_url": "https://x/v1"},
    ).json()
    assert body["success"] is False
    assert "不是有效 JSON" in body["message"]


def test_llm_models_falls_back_to_saved_config(client, monkeypatch):
    """请求未提供字段时应回退读取已保存配置。"""
    _FakeConfig.secrets["llm_api_key"] = "saved-key"
    _FakeConfig.plain = {"llm": {"api_base": "https://saved/v1"}}
    _install_httpx(monkeypatch, lambda url, headers: _FakeResponse(200, {"data": [{"id": "m"}]}))

    body = client.post("/api/config/llm/models", json={}).json()
    assert body["success"] is True
    assert body["models"] == ["m"]


# ---------------------------------------------------------------------------
# POST /llm
# ---------------------------------------------------------------------------


def test_update_llm_config_persists_and_resets_client(client):
    """更新应加密落盘密钥、写入普通配置并触发重载与单例重置。"""
    response = client.post(
        "/api/config/llm",
        json={
            "api_key": "sk-new",
            "base_url": "https://api.new/v1",
            "model": "gpt-new",
            "daily_token_limit": 5000,
        },
    )
    assert response.status_code == 200
    assert response.json()["message"] == "LLM配置已更新"

    assert _FakeConfig.secrets["llm_api_key"] == "sk-new"
    assert _FakeConfig.plain["llm"]["api_base"] == "https://api.new/v1"
    assert _FakeConfig.plain["llm"]["model"] == "gpt-new"
    assert _FakeConfig.plain["llm"]["daily_token_limit"] == 5000
    assert _FakeConfig.saved == 1


def test_update_llm_config_skips_empty_daily_limit(client):
    """daily_token_limit 缺省时不应写入该键。"""
    client.post(
        "/api/config/llm",
        json={"api_key": "sk", "base_url": "https://x/v1", "model": "m"},
    )
    assert "daily_token_limit" not in _FakeConfig.plain["llm"]


def test_update_llm_config_failure_returns_500(client, monkeypatch):
    """写盘异常应转 500。"""

    def boom(*args, **kwargs):
        """模拟配置构造失败。"""
        raise RuntimeError("broken")

    monkeypatch.setattr(config_module, "ConfigManager", boom)
    response = client.post("/api/config/llm", json={"api_key": "k"})
    assert response.status_code == 500
    assert "更新配置失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /llm/usage
# ---------------------------------------------------------------------------


def test_llm_usage_aggregates_by_day(client, db):
    """用量应按自然日聚合 token 与请求次数。"""
    session = db.get_session()
    try:
        session.add_all(
            [
                LLMUsage(date="2031-01-01", model="m1", prompt_tokens=10, completion_tokens=5,
                         total_tokens=15, request_count=1, module="analysis"),
                LLMUsage(date="2031-01-01", model="m2", prompt_tokens=20, completion_tokens=10,
                         total_tokens=30, request_count=2, module="hotspot"),
            ]
        )
        session.commit()
    finally:
        session.close()

    body = client.get("/api/config/llm/usage", params={"days": 30}).json()
    assert body["success"] is True
    assert body["total_tokens"] == 45
    assert body["total_requests"] == 2
    assert len(body["daily_usage"]) == 1
    day = body["daily_usage"][0]
    assert day["prompt_tokens"] == 30
    assert day["completion_tokens"] == 15
    assert day["request_count"] == 2


def test_llm_usage_empty_returns_zero(client):
    """无记录时应返回零值而非 None。"""
    body = client.get("/api/config/llm/usage").json()
    assert body["total_tokens"] == 0
    assert body["total_requests"] == 0
    assert body["daily_usage"] == []


def test_llm_usage_failure_returns_500(client, monkeypatch):
    """用量查询异常应转 500。"""

    def boom():
        """模拟数据库不可用。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(config_module, "get_session", boom)
    response = client.get("/api/config/llm/usage")
    assert response.status_code == 500
    assert "获取用量统计失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# PUT /
# ---------------------------------------------------------------------------


def test_update_single_config_key(client):
    """单键更新应写入并落盘。"""
    response = client.put("/api/config/", json={"key": "llm.model", "value": "gpt-z"})
    assert response.status_code == 200
    assert response.json()["message"] == "配置 llm.model 已更新"
    assert _FakeConfig.plain["llm"]["model"] == "gpt-z"
    assert _FakeConfig.saved == 1


def test_update_single_config_failure_returns_500(client, monkeypatch):
    """写入异常应转 500。"""

    def boom(*args, **kwargs):
        """模拟配置构造失败。"""
        raise RuntimeError("broken")

    monkeypatch.setattr(config_module, "ConfigManager", boom)
    response = client.put("/api/config/", json={"key": "a", "value": 1})
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# POST /reload
# ---------------------------------------------------------------------------


def test_reload_config_calls_reload(client):
    """重载接口应触发 ConfigManager.reload 并返回成功文案。"""
    body = client.post("/api/config/reload").json()
    assert body == {"success": True, "message": "配置已重新加载"}
    assert _FakeConfig.reloaded == 1


def test_reload_config_failure_returns_500(client, monkeypatch):
    """重载异常应转 500。"""

    def boom(*args, **kwargs):
        """模拟配置构造失败。"""
        raise RuntimeError("broken")

    monkeypatch.setattr(config_module, "ConfigManager", boom)
    response = client.post("/api/config/reload")
    assert response.status_code == 500
    assert "重载配置失败" in response.json()["detail"]
