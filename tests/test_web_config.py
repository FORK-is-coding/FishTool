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
        self.kwargs: dict = {}

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


def _install_httpx(monkeypatch, handler) -> list:
    """把 config 模块内的 httpx 换成契约级假命名空间。

    返回按创建顺序记录的假 client 列表，供断言构造参数（例如
    ``follow_redirects is False``）——旧版本把它吞进 ``**kwargs`` 无法验证。
    """
    created: list = []

    def _make_client(**kwargs):
        """记录构造参数后返回假 client。"""
        fake = _FakeAsyncClient(handler)
        fake.kwargs = dict(kwargs)
        created.append(fake)
        return fake

    namespace = SimpleNamespace(
        AsyncClient=_make_client,
        TimeoutException=_FakeTimeout,
        RequestError=_FakeRequestError,
    )
    monkeypatch.setattr(config_module, "httpx", namespace)
    # 域名解析检查在测试里不发真实 DNS：替身域名（https://x/v1 等）解析不到即放行，
    # 与生产“解析失败交给真实请求按无法连接处理”的语义一致。
    async def _no_dns(_base_url):
        return None

    monkeypatch.setattr(config_module, "_reject_blocked_resolution", _no_dns)
    return created


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
    # 运行时单例替身：PUT / reload 现在都会重载它，用计数断言重载确实发生在单例上
    runtime = SimpleNamespace(reloaded=0)
    runtime.reload = lambda: setattr(runtime, "reloaded", runtime.reloaded + 1)
    monkeypatch.setattr(core_config_module, "config", runtime)

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


def test_get_all_config_hides_legacy_plaintext_key(client):
    """历史遗留的明文 llm.api_key 不得回显，只能以脱敏串出现（#88）。"""
    _FakeConfig.plain = {"llm": {"api_key": "sk-legacy-plaintext", "model": "gpt-x"}}
    body = client.get("/api/config/").json()
    llm_node = body["config"]["llm"]
    assert "api_key" not in llm_node
    assert llm_node["api_key_masked"].startswith("sk-legac***")
    assert "sk-legacy-plaintext" not in str(body)


def test_get_all_config_does_not_mutate_live_config(client):
    """脱敏只能作用于响应副本：不得污染活配置，更不得把脱敏串写回落盘（#88）。"""
    _FakeConfig.plain = {"llm": {"model": "gpt-x"}}
    _FakeConfig.secrets["llm_api_key"] = "sk-1234567890abcdefGHIJ"

    client.get("/api/config/")

    assert "api_key_masked" not in _FakeConfig.plain.get("llm", {})
    assert _FakeConfig.plain == {"llm": {"model": "gpt-x"}}
    # 读取路径不应触发任何写盘
    assert _FakeConfig.saved == 0


def test_get_llm_config_hides_plaintext_key_and_keeps_config_intact(client):
    """GET /llm 同样只返回脱敏串，且不修改底层配置对象（#88）。"""
    _FakeConfig.plain = {"llm": {"api_key": "sk-legacy-plaintext", "model": "gpt-x"}}

    body = client.get("/api/config/llm").json()

    assert "api_key" not in body["config"]
    assert body["config"]["api_key_masked"].startswith("sk-legac***")
    assert "sk-legacy-plaintext" not in str(body)
    assert _FakeConfig.plain["llm"] == {"api_key": "sk-legacy-plaintext", "model": "gpt-x"}


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


@pytest.mark.parametrize(
    "payload",
    [
        {"base_url": "https://evil.example/v1"},
        {"base_url": "https://evil.example/v1", "api_key": None},
        {"base_url": "https://saved:8443/v1"},
        {"base_url": "https://saved@evil.example/v1"},
    ],
)
def test_llm_models_rejects_saved_key_to_other_base_url(client, monkeypatch, payload):
    """未提供密钥时，已保存密钥不得被发往与已保存地址不同的 Base URL（#5-1）。"""
    _FakeConfig.secrets["llm_api_key"] = "saved-key"
    _FakeConfig.plain = {"llm": {"api_base": "https://saved/v1"}}
    calls = []

    def handler(url, headers):
        """记录外发请求；本用例要求一次都不发生。"""
        calls.append((url, headers))
        return _FakeResponse(200, {"data": [{"id": "m"}]})

    _install_httpx(monkeypatch, handler)

    response = client.post("/api/config/llm/models", json=payload)
    assert response.status_code == 400
    assert "已保存的 Base URL" in response.json()["detail"]
    assert calls == []


def test_llm_models_saved_key_allows_equivalent_saved_base_url(client, monkeypatch):
    """仅大小写与末尾斜杠不同的已保存地址仍可使用已保存密钥。"""
    _FakeConfig.secrets["llm_api_key"] = "saved-key"
    _FakeConfig.plain = {"llm": {"api_base": "https://Saved.Example/v1"}}
    captured = {}

    def handler(url, headers):
        """记录外发请求地址与鉴权头。"""
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(200, {"data": [{"id": "m"}]})

    _install_httpx(monkeypatch, handler)

    body = client.post("/api/config/llm/models", json={"base_url": "https://saved.example/v1/"}).json()
    assert body["success"] is True
    assert captured["headers"]["Authorization"] == "Bearer saved-key"


def test_llm_models_saved_key_rejected_when_no_saved_base_url(client, monkeypatch):
    """已保存密钥但未保存地址时，不得把密钥发往请求方指定地址。"""
    _FakeConfig.secrets["llm_api_key"] = "saved-key"
    calls = []
    _install_httpx(monkeypatch, lambda url, headers: calls.append(url))

    response = client.post("/api/config/llm/models", json={"base_url": "https://any/v1"})
    assert response.status_code == 400
    assert calls == []


def test_llm_models_explicit_key_allows_any_base_url(client, monkeypatch):
    """请求自带密钥时可探测任意地址，且不会混入已保存密钥。"""
    _FakeConfig.secrets["llm_api_key"] = "saved-key"
    _FakeConfig.plain = {"llm": {"api_base": "https://saved/v1"}}
    captured = {}

    def handler(url, headers):
        """记录外发请求地址与鉴权头。"""
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(200, {"data": [{"id": "m"}]})

    _install_httpx(monkeypatch, handler)

    body = client.post(
        "/api/config/llm/models",
        json={"api_key": "new-key", "base_url": "https://other/v1"},
    ).json()
    assert body["success"] is True
    assert captured["url"] == "https://other/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer new-key"


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///etc/passwd",
        "ftp://example.com/v1",
        "gopher://example.com/v1",
        "api.example.com/v1",
        "//api.example.com/v1",
    ],
)
def test_llm_models_rejects_non_http_schemes(client, monkeypatch, base_url):
    """只允许 http/https：其他协议（含无 scheme 的相对地址）一律 400 且不外发（#81）。"""
    calls = []
    _install_httpx(monkeypatch, lambda url, headers: calls.append(url))

    response = client.post(
        "/api/config/llm/models", json={"api_key": "k", "base_url": base_url}
    )
    assert response.status_code == 400
    assert "http" in response.json()["detail"]
    assert calls == []


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:pass@api.example/v1",
        "https://user@api.example/v1",
        # 内嵌凭据即使指向本地地址也要拒绝：@ 之前的内容会掩盖真实主机
        "http://user:pass@127.0.0.1:8080/v1",
    ],
)
def test_llm_models_rejects_embedded_credentials(client, monkeypatch, base_url):
    """拒绝 URL 内嵌凭据，避免把凭据拼进请求地址或掩盖真实主机（#81）。"""
    calls = []
    _install_httpx(monkeypatch, lambda url, headers: calls.append(url))

    response = client.post(
        "/api/config/llm/models", json={"api_key": "k", "base_url": base_url}
    )
    assert response.status_code == 400
    assert "用户名或密码" in response.json()["detail"]
    assert calls == []


@pytest.mark.parametrize(
    "base_url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://169.254.169.254",
        "http://metadata.google.internal/computeMetadata/v1",
        "http://metadata.goog/v1",
        "http://100.100.100.200/latest/meta-data",
        "http://[fe80::1]/v1",
        "http://0.0.0.0:8080/v1",
        # 非标准写法与 IPv6 内嵌 IPv4：都能被系统解析成元数据地址
        "http://[::ffff:169.254.169.254]/",
        "http://[::ffff:a9fe:a9fe]/",
        "http://[::169.254.169.254]/",
        "http://[64:ff9b::a9fe:a9fe]/",
        "http://[2002:a9fe:a9fe::1]/",
        "http://[::ffff:0:a9fe:a9fe]/",
        "http://[64:ff9b:1::a9fe:a9fe]/",
        "http://[fd00:ec2::254]/",
        "http://2852039166/",
        "http://0xa9fea9fe/",
        "http://0251.0376.0251.0376/",
        "http://169.254.43518/",
        "http://169.254.169.254./",
    ],
)
def test_llm_models_rejects_metadata_and_link_local(client, monkeypatch, base_url):
    """拒绝云元数据地址与 link-local/未指定地址（#81）。"""
    calls = []
    _install_httpx(monkeypatch, lambda url, headers: calls.append(url))

    response = client.post(
        "/api/config/llm/models", json={"api_key": "k", "base_url": base_url}
    )
    assert response.status_code == 400
    assert calls == []


@pytest.mark.parametrize(
    ("resolved", "blocked"),
    [
        ("169.254.169.254", True),   # 169.254.169.254.nip.io 这类域名
        ("169.254.170.2", True),     # IPv4 link-local 仍拦截（如 ECS 任务凭据端点）
        ("fd00:ec2::254", True),
        ("::ffff:169.254.169.254", True),
        ("127.0.0.1", False),        # 解析到本机的域名照常放行（本地模型服务）
        ("203.0.113.7", False),
        ("fe80::287f:77ac:2f2:7826", False),  # Windows 解析机器名时附带的网卡地址
    ],
)
def test_resolution_check_rejects_domains_resolving_to_metadata(monkeypatch, resolved, blocked):
    """域名的解析结果同样要过地址检查：拦元数据 / link-local，放行环回与公网。"""
    import asyncio
    import socket

    family = socket.AF_INET6 if ":" in resolved else socket.AF_INET

    async def _fake_resolve(host):
        assert host == "meta.example"
        return [(family, socket.SOCK_STREAM, 6, "", (resolved, 0))]

    monkeypatch.setattr(config_module, "_resolve_host", _fake_resolve)
    run = lambda: asyncio.run(config_module._reject_blocked_resolution("http://meta.example/v1"))  # noqa: E731

    if blocked:
        with pytest.raises(config_module.HTTPException) as excinfo:
            run()
        assert excinfo.value.status_code == 400
    else:
        run()


@pytest.mark.parametrize(
    ("addresses", "blocked"),
    [
        # 局域网机器名：IPv4 + 网卡 fe80:: 地址，必须放行
        (["10.7.5.39", "fe80::287f:77ac:2f2:7826"], False),
        (["192.168.1.20", "fe80::1"], False),
        # 只要有一个结果是元数据地址就拦截，跟同时解析出的其他地址无关
        (["203.0.113.7", "169.254.169.254"], True),
        (["fe80::1", "fd00:ec2::254"], True),
    ],
)
def test_resolution_check_with_multiple_results(monkeypatch, addresses, blocked):
    """多个解析结果逐个检查：fe80:: 不再连累同名的局域网 IPv4，元数据地址仍一票否决。"""
    import asyncio
    import socket

    async def _fake_resolve(host):
        return [
            (socket.AF_INET6 if ":" in item else socket.AF_INET, socket.SOCK_STREAM, 6, "", (item, 0))
            for item in addresses
        ]

    monkeypatch.setattr(config_module, "_resolve_host", _fake_resolve)
    run = lambda: asyncio.run(config_module._reject_blocked_resolution("http://lan-box:1234/v1"))  # noqa: E731

    if blocked:
        with pytest.raises(config_module.HTTPException):
            run()
    else:
        run()


def test_resolution_check_blocks_mapped_metadata_on_newer_python(monkeypatch):
    """较新的 CPython 对 ::ffff:a.b.c.d 按内嵌 IPv4 计算 is_link_local。

    放行 fe80:: 的判断不能依赖这个属性，否则 ::ffff:169.254.169.254 会被当成
    link-local 跳过、元数据地址漏过去（CI 的 Linux Python 上实际出现过）。
    这里强制模拟新语义，保证任何 Python 版本上都覆盖到这条路径。
    """
    import asyncio
    import ipaddress
    import socket

    real = ipaddress.IPv6Address.is_link_local

    def _newer_semantics(self):
        mapped = self.ipv4_mapped
        return mapped.is_link_local if mapped is not None else real.fget(self)

    monkeypatch.setattr(ipaddress.IPv6Address, "is_link_local", property(_newer_semantics))
    assert ipaddress.ip_address("::ffff:169.254.169.254").is_link_local  # 前提：确实模拟成了新语义

    async def _fake_resolve(host):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:169.254.169.254", 0))]

    monkeypatch.setattr(config_module, "_resolve_host", _fake_resolve)
    with pytest.raises(config_module.HTTPException) as excinfo:
        asyncio.run(config_module._reject_blocked_resolution("http://meta.example/v1"))
    assert excinfo.value.status_code == 400


def test_resolution_check_ignores_resolver_failure(monkeypatch):
    """解析失败不在这里报错，交给随后的真实请求按“无法连接”处理。"""
    import asyncio

    async def _fail(_host):
        raise OSError("no such host")

    monkeypatch.setattr(config_module, "_resolve_host", _fail)
    asyncio.run(config_module._reject_blocked_resolution("http://nowhere.invalid/v1"))


@pytest.mark.parametrize(
    "base_url",
    ["http://ollama.lan:11434/v1", "http://1.2.3.4.example/v1", "http://[::1]:8080/v1"],
)
def test_llm_models_does_not_block_normal_hostnames(client, monkeypatch, base_url):
    """局域网主机名、以数字开头的普通域名、IPv6 环回都不是非标准 IP 写法，照常放行。"""
    _install_httpx(monkeypatch, lambda url, headers: _FakeResponse(200, {"data": [{"id": "m"}]}))

    body = client.post("/api/config/llm/models", json={"api_key": "k", "base_url": base_url}).json()

    assert body["success"] is True


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:11434/v1",
        "http://localhost:1234/v1",
        "http://192.168.1.10:8000/v1",
        "http://10.0.0.5/v1",
    ],
)
def test_llm_models_allows_unsaved_local_endpoints(client, monkeypatch, base_url):
    """本地 Ollama / LM Studio / 局域网模型服务是合法用法，未保存的新地址也可刷新模型。

    前端在 Base URL 失焦时就会带当前输入框内容来刷新模型列表，不能要求先保存。
    """
    _FakeConfig.plain = {"llm": {"api_base": "https://api.deepseek.com/v1"}}
    captured = {}

    def handler(url, headers):
        """记录外发地址。"""
        captured["url"] = url
        return _FakeResponse(200, {"data": [{"id": "llama3"}]})

    _install_httpx(monkeypatch, handler)

    body = client.post(
        "/api/config/llm/models", json={"api_key": "k", "base_url": base_url}
    ).json()
    assert body["success"] is True
    assert body["models"] == ["llama3"]
    assert captured["url"] == base_url.rstrip("/") + "/models"


def test_llm_models_does_not_follow_redirects(client, monkeypatch):
    """必须禁用自动重定向：否则公开地址可 302 到内网绕过地址校验（#81）。"""
    created = _install_httpx(
        monkeypatch, lambda url, headers: _FakeResponse(301, {"data": []})
    )

    body = client.post(
        "/api/config/llm/models",
        json={"api_key": "k", "base_url": "https://x/v1"},
    ).json()
    assert created, "应至少构造一次 client"
    assert created[0].kwargs["follow_redirects"] is False
    # 3xx 也属于“没拿到模型列表”，按状态码提示
    assert body["success"] is False
    assert body["message"] == "模型列表请求失败（HTTP 301）"
    # 且只请求了一次原始地址，未追到 Location
    assert [call["url"] for call in created[0].calls] == ["https://x/v1/models"]


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


def test_update_llm_config_closes_old_global_client(client, monkeypatch):
    """保存后应经 reset_llm_client() 清空全局单例并关闭旧客户端连接。"""
    import llm.client as llm_module

    class _OldClient:
        """持有旧配置的客户端替身。"""

        closed = 0

        async def close(self):
            """记录关闭次数。"""
            type(self).closed += 1

    old_client = _OldClient()
    monkeypatch.setattr(llm_module, "_global_llm_client", old_client)

    response = client.post(
        "/api/config/llm",
        json={"api_key": "sk-new", "base_url": "https://api.new/v1", "model": "gpt-new"},
    )
    assert response.status_code == 200
    assert llm_module._global_llm_client is None
    assert _OldClient.closed == 1


def test_update_llm_config_skips_empty_daily_limit(client):
    """daily_token_limit 缺省时不应写入该键。"""
    client.post(
        "/api/config/llm",
        json={"api_key": "sk", "base_url": "https://x/v1", "model": "m"},
    )
    assert "daily_token_limit" not in _FakeConfig.plain["llm"]


def test_update_llm_config_accepts_zero_daily_limit(client):
    """0 表示不限制，必须能保存（原实现用真值判断，0 被静默忽略）。"""
    response = client.post(
        "/api/config/llm",
        json={"api_key": "sk", "base_url": "https://x/v1", "model": "m", "daily_token_limit": 0},
    )
    assert response.status_code == 200
    assert _FakeConfig.plain["llm"]["daily_token_limit"] == 0


@pytest.mark.parametrize("value", [-1, 1_000_000_001, 1.5, "100", True])
def test_update_llm_config_rejects_invalid_daily_limit(client, value):
    """非法上限返回 422，密钥与地址也不写入（请求整体被拒）。"""
    response = client.post(
        "/api/config/llm",
        json={"api_key": "sk", "base_url": "https://x/v1", "model": "m", "daily_token_limit": value},
    )
    assert response.status_code == 422
    assert _FakeConfig.secrets == {}
    assert _FakeConfig.saved == 0


def test_update_llm_config_key_only_does_not_reset_endpoint(client):
    """只提交 api_key 时不得把已保存的 base_url / model 重置为默认值（#91）。"""
    _FakeConfig.plain = {"llm": {"api_base": "http://127.0.0.1:11434/v1", "model": "qwen2"}}

    response = client.post("/api/config/llm", json={"api_key": "sk-only-key"})

    assert response.status_code == 200
    assert _FakeConfig.secrets["llm_api_key"] == "sk-only-key"
    assert _FakeConfig.plain["llm"]["api_base"] == "http://127.0.0.1:11434/v1"
    assert _FakeConfig.plain["llm"]["model"] == "qwen2"


@pytest.mark.parametrize("api_key", ["", "   ", "\t\n"])
def test_update_llm_config_rejects_blank_key(client, api_key):
    """空/全空白 api_key 返回 422，绝不能被当成“清空密钥”（#91）。"""
    _FakeConfig.secrets["llm_api_key"] = "sk-existing"

    response = client.post("/api/config/llm", json={"api_key": api_key})

    assert response.status_code == 422
    assert _FakeConfig.secrets["llm_api_key"] == "sk-existing"
    assert _FakeConfig.saved == 0


def test_update_llm_config_rejects_empty_body(client):
    """缺少必填的 api_key 返回 422（#91）。"""
    response = client.post("/api/config/llm", json={})

    assert response.status_code == 422
    assert _FakeConfig.saved == 0


def test_update_llm_config_strips_text_fields(client):
    """提交值两侧空白应被 strip 后再落盘。"""
    client.post(
        "/api/config/llm",
        json={"api_key": "  sk-padded  ", "base_url": " https://x/v1 ", "model": " gpt-x "},
    )
    assert _FakeConfig.secrets["llm_api_key"] == "sk-padded"
    assert _FakeConfig.plain["llm"]["api_base"] == "https://x/v1"
    assert _FakeConfig.plain["llm"]["model"] == "gpt-x"


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
    assert body["today"]["total_tokens"] == 0


def test_llm_usage_reports_today_against_configured_limit(client, db):
    """today 只统计今天（按 LLMUsage.date，与限额检查同口径），并带当前上限与剩余。"""
    from datetime import date, timedelta

    today = date.today()
    session = db.get_session()
    try:
        session.add_all([
            LLMUsage(date=today.isoformat(), model="m", prompt_tokens=100, completion_tokens=50,
                     total_tokens=150, request_count=1, module="hotspot"),
            LLMUsage(date=(today - timedelta(days=1)).isoformat(), model="m", prompt_tokens=900,
                     completion_tokens=0, total_tokens=900, request_count=1, module="hotspot"),
        ])
        session.commit()
    finally:
        session.close()
    _FakeConfig.plain = {"llm": {"daily_token_limit": 1000}}

    today_usage = client.get("/api/config/llm/usage").json()["today"]

    assert today_usage == {"total_tokens": 150, "limit": 1000, "remaining": 850}


def test_llm_usage_today_unlimited_has_no_remaining(client):
    """上限为 0（不限制）时 remaining 为 None。"""
    _FakeConfig.plain = {"llm": {"daily_token_limit": 0}}
    today_usage = client.get("/api/config/llm/usage").json()["today"]
    assert today_usage["limit"] == 0
    assert today_usage["remaining"] is None


def test_llm_usage_today_uses_default_when_limit_missing(client):
    """未配置上限时按新默认值（200 万）显示。"""
    from core.config import LLM_DAILY_TOKEN_LIMIT_DEFAULT

    today_usage = client.get("/api/config/llm/usage").json()["today"]
    assert today_usage["limit"] == LLM_DAILY_TOKEN_LIMIT_DEFAULT


def test_llm_usage_failure_returns_500(client, monkeypatch):
    """用量查询异常应转 500。"""

    def boom():
        """模拟数据库不可用。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(config_module, "get_session", boom)
    response = client.get("/api/config/llm/usage")
    assert response.status_code == 500
    assert "获取用量统计失败" in response.json()["detail"]


@pytest.mark.parametrize("days", [0, -1, -30, 366, 10 ** 9])
def test_llm_usage_rejects_out_of_range_days(client, days):
    """#94 回归：days 必须有上下界，避免 timedelta 溢出 500 或静默空窗口。"""
    response = client.get("/api/config/llm/usage", params={"days": days})
    assert response.status_code == 422


@pytest.mark.parametrize("days", [1, 7, 365])
def test_llm_usage_accepts_bounded_days(client, days):
    """边界内取值正常返回，并把实际天数回显给前端。"""
    body = client.get("/api/config/llm/usage", params={"days": days}).json()
    assert body["days"] == days


def _fake_usage_session(closed: list, fail: bool = False):
    """构造会话替身：记录 close 调用，可按需让查询抛错。"""

    class _Chain:
        """可链式调用的查询替身，只覆盖路由用到的接口。"""

        def filter(self, *args, **kwargs):
            """返回自身以保持链式调用。"""
            return self

        def group_by(self, *args, **kwargs):
            """返回自身以保持链式调用。"""
            return self

        def all(self):
            """无聚合结果。"""
            return []

        def first(self):
            """无总计结果。"""
            return None

        def one(self):
            """今日用量聚合在空库返回一行 NULL。"""
            return None, None, None

    class _Session:
        """会话替身。"""

        def query(self, *args, **kwargs):
            """返回查询链。"""
            if fail:
                raise RuntimeError("query down")
            return _Chain()

        def close(self):
            """记录关闭。"""
            closed.append(True)

    return _Session()


def test_llm_usage_closes_session_on_success(client, monkeypatch):
    """#94 回归：正常路径必须关闭会话。"""
    closed: list = []
    monkeypatch.setattr(config_module, "get_session", lambda: _fake_usage_session(closed))
    response = client.get("/api/config/llm/usage")
    assert response.status_code == 200
    assert closed == [True]


def test_llm_usage_closes_session_when_query_fails(client, monkeypatch):
    """#94 回归：查询抛错转 500 时也必须关闭会话（原先泄漏连接）。"""
    closed: list = []
    monkeypatch.setattr(
        config_module, "get_session", lambda: _fake_usage_session(closed, fail=True)
    )
    response = client.get("/api/config/llm/usage")
    assert response.status_code == 500
    assert "获取用量统计失败" in response.json()["detail"]
    assert closed == [True]


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


@pytest.mark.parametrize("value", [0, 1, 2_000_000, 1_000_000_000])
def test_put_daily_limit_accepts_valid_values(client, value):
    """配置页“保存上限”走 PUT /：0（不限制）到 10 亿的整数都能保存。"""
    response = client.put("/api/config/", json={"key": "llm.daily_token_limit", "value": value})
    assert response.status_code == 200
    assert _FakeConfig.plain["llm"]["daily_token_limit"] == value
    assert _FakeConfig.saved == 1


def test_put_daily_limit_writes_migration_marker(client):
    """经配置页保存的上限（包括恰好 100000）要带迁移标记，下次启动不能被当作旧默认值改掉。"""
    response = client.put("/api/config/", json={"key": "llm.daily_token_limit", "value": 100000})
    assert response.status_code == 200
    assert _FakeConfig.plain["llm"] == {"daily_token_limit": 100000, "daily_token_limit_migrated": 1}


@pytest.mark.parametrize("value", [1, 2000, 8192, 1_000_000])
def test_put_max_tokens_accepts_valid_values(client, value):
    """配置页“保存输出上限”走 PUT /：1 到 100 万的整数都能保存，并写到 llm.max_tokens。"""
    response = client.put("/api/config/", json={"key": "llm.max_tokens", "value": value})
    assert response.status_code == 200
    assert _FakeConfig.plain["llm"]["max_tokens"] == value
    assert _FakeConfig.saved == 1


@pytest.mark.parametrize("value", [0, -1, 1_000_001, 1.5, "2000", True, None])
def test_put_max_tokens_rejects_invalid_values(client, value):
    """0、负数、超上限、小数、字符串、布尔都返回 422，不写配置。"""
    response = client.put("/api/config/", json={"key": "llm.max_tokens", "value": value})
    assert response.status_code == 422
    assert _FakeConfig.saved == 0


def test_put_max_tokens_with_padded_key_writes_clean_path(client):
    """带空格的键也按校验后的 llm.max_tokens 写入，不写出脏路径。"""
    response = client.put("/api/config/", json={"key": " llm.max_tokens ", "value": 4096})
    assert response.status_code == 200
    assert _FakeConfig.plain["llm"] == {"max_tokens": 4096}


def test_put_key_normalizes_every_path_segment(client):
    """点分路径的每一段都要 strip，不得写出带空格的中间层级（#98）。"""
    response = client.put("/api/config/", json={"key": " llm . model ", "value": "gpt-clean"})

    assert response.status_code == 200
    assert response.json()["message"] == "配置 llm.model 已更新"
    assert _FakeConfig.plain == {"llm": {"model": "gpt-clean"}}
    # 任何带空格的顶层键都属于脏数据
    assert not [k for k in _FakeConfig.plain if k != k.strip()]


@pytest.mark.parametrize("key", ["", "   ", "llm.", ".llm", "llm..model", " llm . "])
def test_put_key_rejects_malformed_path(client, key):
    """空键、空段、只有空白的段一律 422，不写入也不落盘（#98）。"""
    response = client.put("/api/config/", json={"key": key, "value": 1})

    assert response.status_code == 422
    assert _FakeConfig.plain == {}
    assert _FakeConfig.saved == 0


def test_post_llm_with_daily_limit_writes_migration_marker(client):
    client.post("/api/config/llm", json={"api_key": "sk", "base_url": "https://x/v1", "model": "m",
                                         "daily_token_limit": 100000})
    assert _FakeConfig.plain["llm"]["daily_token_limit_migrated"] == 1


def test_post_llm_without_daily_limit_does_not_write_marker(client):
    client.post("/api/config/llm", json={"api_key": "sk", "base_url": "https://x/v1", "model": "m"})
    assert "daily_token_limit_migrated" not in _FakeConfig.plain["llm"]


def test_saved_legacy_value_survives_restart(tmp_path, monkeypatch):
    """端到端：真实 ConfigManager 上经 PUT 保存 100000，重启后仍是 100000。"""
    import sys

    from core.config import ConfigManager as RealConfigManager

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("llm:\n  daily_token_limit: 300000\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "ConfigManager",
                        lambda *a, **k: RealConfigManager(config_dir=str(cfg_dir)))
    runtime = SimpleNamespace(reload=lambda: None)
    monkeypatch.setattr(sys.modules["core.config"], "config", runtime)

    import llm.client as llm_module

    async def _noop():
        return None

    monkeypatch.setattr(llm_module, "reset_llm_client", _noop)
    app = FastAPI()
    app.include_router(config_module.router, prefix="/api/config")
    response = TestClient(app).put("/api/config/", json={"key": "llm.daily_token_limit", "value": 100000})
    assert response.status_code == 200

    assert RealConfigManager(config_dir=str(cfg_dir)).get("llm.daily_token_limit") == 100000


@pytest.mark.parametrize("value", [-1, 1_000_000_001, 1.5, "100", True, None, [1]])
def test_put_daily_limit_rejects_invalid_values(client, value):
    """非法上限返回 422，不写入、不落盘。"""
    response = client.put("/api/config/", json={"key": "llm.daily_token_limit", "value": value})
    assert response.status_code == 422
    assert "每日 token 上限" in response.json()["detail"]
    assert _FakeConfig.plain == {}
    assert _FakeConfig.saved == 0


def test_put_daily_limit_validates_padded_key(client):
    """键名带空格时同样校验（与受保护键的 strip 口径一致）。"""
    response = client.put("/api/config/", json={"key": " llm.daily_token_limit ", "value": -5})
    assert response.status_code == 422
    assert _FakeConfig.saved == 0


def test_put_daily_limit_padded_key_writes_clean_path(client):
    """带空格的上限键：写入用 strip 后的键，不写出 ' llm' 这类脏路径。"""
    response = client.put("/api/config/", json={"key": " llm.daily_token_limit ", "value": 5})
    assert response.status_code == 200
    assert _FakeConfig.plain == {"llm": {"daily_token_limit": 5, "daily_token_limit_migrated": 1}}


@pytest.mark.parametrize("key", ["llm.api_base", " llm.api_base ", "llm", "llm.api_key", "secrets.llm_api_key"])
def test_update_single_config_rejects_protected_keys(client, key):
    """LLM 地址与密钥不得经通用接口写入，防止绕过地址-密钥绑定（#5-1）。"""
    response = client.put("/api/config/", json={"key": key, "value": "https://evil.example/v1"})
    assert response.status_code == 400
    assert _FakeConfig.plain == {}
    assert _FakeConfig.saved == 0


# ---------------------------------------------------------------------------
# PUT / 与 POST /reload 对运行中进程生效（真实 ConfigManager + 临时配置目录）
# ---------------------------------------------------------------------------


@pytest.fixture()
def runtime_env(tmp_path, monkeypatch):
    """真实 ConfigManager 落在临时目录：路由新建实例与运行时单例读写同一目录。

    返回 dict：runtime 为替换后的运行时单例，cfg_dir 为临时配置目录，
    resets 记录 reset_llm_client 被 await 的次数。绝不触碰仓库 config/。
    """
    import sys

    import llm.client as llm_module

    real_config_manager = sys.modules["core.config"].ConfigManager
    cfg_dir = str(tmp_path / "runtime_config")
    runtime = real_config_manager(config_dir=cfg_dir)
    monkeypatch.setattr(sys.modules["core.config"], "config", runtime)
    monkeypatch.setattr(
        config_module, "ConfigManager", lambda *a, **k: real_config_manager(config_dir=cfg_dir)
    )

    env = {"runtime": runtime, "cfg_dir": cfg_dir, "resets": 0}

    async def _counting_reset():
        """记录 reset_llm_client 调用次数，不触碰真实全局客户端。"""
        env["resets"] += 1

    monkeypatch.setattr(llm_module, "reset_llm_client", _counting_reset)
    app = FastAPI()
    app.include_router(config_module.router, prefix="/api/config")
    env["client"] = TestClient(app)
    return env


def test_put_config_visible_to_runtime_singleton(runtime_env):
    """PUT 后运行中的 core.config.config 应立即读到新值。"""
    import sys

    response = runtime_env["client"].put(
        "/api/config/", json={"key": "monitor.check_interval", "value": 42}
    )
    assert response.status_code == 200
    assert sys.modules["core.config"].config.get("monitor.check_interval") == 42
    # 非 llm.* 键不必重建 LLM 客户端
    assert runtime_env["resets"] == 0


def test_put_llm_key_resets_llm_client(runtime_env):
    """PUT llm.* 键后运行时单例读到新值，并重建全局 LLM 客户端。"""
    response = runtime_env["client"].put(
        "/api/config/", json={"key": "llm.model", "value": "gpt-runtime"}
    )
    assert response.status_code == 200
    assert runtime_env["runtime"].get("llm.model") == "gpt-runtime"
    assert runtime_env["resets"] == 1


def test_put_padded_llm_key_still_resets_llm_client(runtime_env):
    """带空格的 llm.* 键规范化后必须仍然触发 LLM 客户端重建（#98）。"""
    response = runtime_env["client"].put(
        "/api/config/", json={"key": " llm.model ", "value": "gpt-spaced"}
    )

    assert response.status_code == 200
    assert runtime_env["runtime"].get("llm.model") == "gpt-spaced"
    assert runtime_env["resets"] == 1
    assert not [k for k in runtime_env["runtime"].all if k != k.strip()]


def test_reload_picks_up_disk_changes_and_resets_llm_client(runtime_env):
    """外部改写磁盘后 POST /reload，运行时单例应读到磁盘新值并重建 LLM 客户端。"""
    import sys

    external = sys.modules["core.config"].ConfigManager(config_dir=runtime_env["cfg_dir"])
    external.set("llm.model", "gpt-from-disk")
    external.save_config()
    assert runtime_env["runtime"].get("llm.model") != "gpt-from-disk"

    response = runtime_env["client"].post("/api/config/reload")
    assert response.status_code == 200
    assert runtime_env["runtime"].get("llm.model") == "gpt-from-disk"
    assert runtime_env["resets"] == 1


def test_web_main_config_manager_follows_put(runtime_env, monkeypatch):
    """web.main lifespan 的 config_manager（注入常驻监控）应是运行时单例，PUT 后同步可见。"""
    import importlib

    web_main = importlib.import_module("web.main")
    runtime = runtime_env["runtime"]
    # 真实 lifespan；conftest 已隔离 DB 与常驻监控，这里让运行时单例指向临时目录实例
    monkeypatch.setattr(web_main, "runtime_config", runtime, raising=False)
    # 走真实 app：显式用本机 Host，与浏览器 / 桌面端访问一致（/api/config 可能挂本机 Host 校验）
    with TestClient(web_main.app, base_url="http://127.0.0.1:8000") as client:
        response = client.put("/api/config/", json={"key": "monitor.check_interval", "value": 77})
        assert response.status_code == 200
        assert web_main.config_manager.get("monitor.check_interval") == 77
        monitor = web_main.monitor_service
        assert monitor.args[1] is web_main.config_manager


# ---------------------------------------------------------------------------
# POST /reload
# ---------------------------------------------------------------------------


def test_reload_config_calls_reload(client):
    """重载接口应重载运行时单例（而非新建实例）并返回成功文案。"""
    import sys

    body = client.post("/api/config/reload").json()
    assert body == {"success": True, "message": "配置已重新加载"}
    assert sys.modules["core.config"].config.reloaded == 1
    assert _FakeConfig.reloaded == 0


def test_reload_config_failure_returns_500(client, monkeypatch):
    """重载异常应转 500。"""
    import sys

    def boom(*args, **kwargs):
        """模拟运行时单例重载失败（如配置文件损坏）。"""
        raise RuntimeError("broken")

    monkeypatch.setattr(sys.modules["core.config"].config, "reload", boom)
    response = client.post("/api/config/reload")
    assert response.status_code == 500
    assert "重载配置失败" in response.json()["detail"]
