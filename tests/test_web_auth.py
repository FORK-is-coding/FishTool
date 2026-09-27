"""web.routers.auth B站登录态管理接口测试（第4批 · web 段）。

覆盖对象：
- _mask_cookie（脱敏）
- GET  /status -> get_auth_status
- GET  /qrcode -> get_qrcode
- POST /poll   -> poll_login
- POST /logout -> logout

验证维度：
未登录/有效/失效三分支 / 脱敏规则 / 二维码生成 / 扫码状态码映射 /
登录成功三重校验与 Cookie 落盘 / 网络异常不抛 500 / 登出清空。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- ConfigManager / CookieLoginHelper / QRCodeLogin / BilibiliAPI 全部以契约级假对象注入。
- 仓库未安装 pytest-asyncio，异步用例统一用 asyncio.run 驱动。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers import auth as auth_module


# ---------------------------------------------------------------------------
# 契约级假对象
# ---------------------------------------------------------------------------


class _FakeConfig:
    """内存型配置替身，模拟加密 secrets 的读写。"""

    store: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get_secret(self, key, default=None):
        """从内存字典读密钥。"""
        return type(self).store.get(key, default)

    def save_secret(self, key, value):
        """写入内存字典。"""
        type(self).store[key] = value


class _FakeCookieLoginHelper:
    """假 Cookie 校验助手。"""

    valid: bool = True
    user: dict = {"uid": 42, "name": "测试UP"}

    @staticmethod
    async def validate_cookie(cookie: str) -> bool:
        """返回预置的有效性。"""
        return _FakeCookieLoginHelper.valid

    @staticmethod
    async def get_user_info(cookie: str) -> dict:
        """返回预置用户信息。"""
        return _FakeCookieLoginHelper.user


class _FakeQrCodeLogin:
    """假二维码登录器。"""

    cookie: str = "SESSDATA=abcdefgh1234; bili_jct=zzz"

    def __init__(self, *args, **kwargs) -> None:
        self.qrcode_key = "qr-key-1"

    async def generate_qrcode(self):
        """返回二维码链接与图片字节。"""
        return "https://qr.example/abc", b"img-bytes"

    def get_qrcode_image_base64(self, img_bytes: bytes) -> str:
        """返回预置 base64。"""
        return "base64-image"

    def _extract_cookie_from_response(self, data, set_cookie_headers):
        """返回预置 Cookie 字符串。"""
        return type(self).cookie


class _FakeBilibiliAPI:
    """假 B站客户端，支持 async with 协议。"""

    poll_result: tuple = ({"code": 0, "data": {"code": 86101}}, [])

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        """进入异步上下文返回自身。"""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """退出上下文不吞异常。"""
        return False

    async def poll_qrcode(self, qrcode_key: str):
        """返回预置轮询结果。"""
        return type(self).poll_result


class _FakeCookiePool:
    """记录 add_cookie 调用的假 Cookie 池。"""

    added: list = []

    def add_cookie(self, cookie, persist: bool = False) -> None:
        """记录写入的 Cookie。"""
        type(self).added.append({"cookie": cookie, "persist": persist})


@pytest.fixture()
def client(monkeypatch):
    """挂载 auth router 的测试客户端，并注入全部假依赖。"""
    _FakeConfig.store = {}
    _FakeCookieLoginHelper.valid = True
    _FakeCookieLoginHelper.user = {"uid": 42, "name": "测试UP"}
    _FakeQrCodeLogin.cookie = "SESSDATA=abcdefgh1234; bili_jct=zzz"
    _FakeBilibiliAPI.poll_result = ({"code": 0, "data": {"code": 86101}}, [])
    _FakeCookiePool.added = []

    monkeypatch.setattr(auth_module, "ConfigManager", _FakeConfig)
    monkeypatch.setattr(auth_module, "CookieLoginHelper", _FakeCookieLoginHelper)
    monkeypatch.setattr(auth_module, "QRCodeLogin", _FakeQrCodeLogin)
    monkeypatch.setattr(auth_module, "BilibiliAPI", _FakeBilibiliAPI)
    monkeypatch.setattr("bilibili.cookie_pool.get_cookie_pool", lambda: _FakeCookiePool())

    app = FastAPI()
    app.include_router(auth_module.router, prefix="/api/auth")
    return TestClient(app)


# ---------------------------------------------------------------------------
# _mask_cookie 脱敏
# ---------------------------------------------------------------------------


def test_mask_cookie_empty_returns_empty():
    """空 Cookie 返回空串。"""
    assert auth_module._mask_cookie("") == ""


def test_mask_cookie_masks_sessdata_middle():
    """SESSDATA 应保留前 4 后 4，中间打码。"""
    masked = auth_module._mask_cookie("buvid3=xyz; SESSDATA=abcdefgh1234")
    assert "SESSDATA=abcd***1234" in masked
    assert "buvid3" in masked


def test_mask_cookie_hides_short_sessdata():
    """过短的 SESSDATA 只保留键名，不泄漏值。"""
    masked = auth_module._mask_cookie("SESSDATA=short")
    assert masked == "SESSDATA"


def test_mask_cookie_falls_back_when_no_pairs():
    """无键值对时返回占位文案。"""
    assert auth_module._mask_cookie("garbage") == "已登录"


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


def test_status_without_cookie_is_logged_out(client):
    """本地无 Cookie 时返回未登录。"""
    body = client.get("/api/auth/status").json()
    assert body == {"success": True, "logged_in": False, "user": None, "cookie_masked": ""}


def test_status_with_valid_cookie(client):
    """有效 Cookie 时返回用户信息与脱敏串。"""
    _FakeConfig.store[auth_module.COOKIE_SECRET_KEY] = "SESSDATA=abcdefgh1234; bili_jct=zzz"

    body = client.get("/api/auth/status").json()
    assert body["logged_in"] is True
    assert body["user"] == {"uid": 42, "name": "测试UP"}
    assert body["cookie_masked"]


def test_status_with_invalid_cookie_marks_invalid(client):
    """Cookie 失效时返回 invalid=True 且不删除本地记录。"""
    _FakeConfig.store[auth_module.COOKIE_SECRET_KEY] = "SESSDATA=abcdefgh1234"
    _FakeCookieLoginHelper.valid = False

    body = client.get("/api/auth/status").json()
    assert body["logged_in"] is False
    assert body["invalid"] is True
    assert _FakeConfig.store[auth_module.COOKIE_SECRET_KEY]  # 未被清除


def test_status_internal_error_returns_500(client, monkeypatch):
    """内部异常应转 500。"""

    def boom(*args, **kwargs):
        """模拟配置构造失败。"""
        raise RuntimeError("config broken")

    monkeypatch.setattr(auth_module, "ConfigManager", boom)
    response = client.get("/api/auth/status")
    assert response.status_code == 500
    assert "查询登录态失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /qrcode
# ---------------------------------------------------------------------------


def test_qrcode_returns_key_url_and_image(client):
    """二维码接口应返回 qrcode_key、链接与 base64 图片。"""
    body = client.get("/api/auth/qrcode").json()
    assert body == {
        "success": True,
        "qrcode_key": "qr-key-1",
        "qrcode_url": "https://qr.example/abc",
        "qrcode_base64": "base64-image",
    }


def test_qrcode_failure_returns_500(client, monkeypatch):
    """生成失败应转 500。"""

    def boom(*args, **kwargs):
        """模拟二维码服务不可用。"""
        raise RuntimeError("passport down")

    monkeypatch.setattr(auth_module, "QRCodeLogin", boom)
    response = client.get("/api/auth/qrcode")
    assert response.status_code == 500
    assert "生成二维码失败" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /poll
# ---------------------------------------------------------------------------


def test_poll_rejects_empty_key(client):
    """缺少 qrcode_key 应返回 400。"""
    response = client.post("/api/auth/poll", json={"qrcode_key": "   "})
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("bili_code", "expected_status"),
    [(86101, "not_scanned"), (86090, "scanned"), (86038, "expired"), (99999, "error")],
)
def test_poll_maps_bilibili_status_codes(client, bili_code, expected_status):
    """B站状态码应映射为前端可消费的 status 文案。"""
    _FakeBilibiliAPI.poll_result = ({"code": 0, "data": {"code": bili_code}}, [])
    body = client.post("/api/auth/poll", json={"qrcode_key": "k"}).json()
    assert body["status"] == expected_status
    assert body["success"] is True
    assert body["user"] is None


def test_poll_success_saves_cookie_and_validates(client):
    """code==0 且三重校验通过时应报 confirmed 并加密落盘、同步 Cookie 池。"""
    _FakeBilibiliAPI.poll_result = ({"code": 0, "data": {"code": 0, "url": "x"}}, ["SESSDATA=x"])
    _FakeCookieLoginHelper.valid = True

    body = client.post("/api/auth/poll", json={"qrcode_key": "k"}).json()

    assert body["status"] == "confirmed"
    assert body["user"] == {"uid": 42, "name": "测试UP"}
    assert _FakeConfig.store[auth_module.COOKIE_SECRET_KEY].startswith("SESSDATA=")
    assert _FakeCookiePool.added and _FakeCookiePool.added[0]["persist"] is True


def test_poll_success_but_cookie_extraction_empty(client):
    """code==0 但 Cookie 提取为空应返回 error，不落盘。"""
    _FakeBilibiliAPI.poll_result = ({"code": 0, "data": {"code": 0}}, [])
    _FakeQrCodeLogin.cookie = ""

    body = client.post("/api/auth/poll", json={"qrcode_key": "k"}).json()
    assert body["status"] == "error"
    assert "Cookie提取失败" in body["message"]
    assert auth_module.COOKIE_SECRET_KEY not in _FakeConfig.store


def test_poll_success_but_cookie_without_sessdata(client):
    """提取到的 Cookie 不含 SESSDATA 时同样拒绝。"""
    _FakeBilibiliAPI.poll_result = ({"code": 0, "data": {"code": 0}}, [])
    _FakeQrCodeLogin.cookie = "bili_jct=only"

    body = client.post("/api/auth/poll", json={"qrcode_key": "k"}).json()
    assert body["status"] == "error"


def test_poll_success_but_nav_validation_fails(client):
    """Cookie 已落盘但 nav 校验失败时应返回 error。"""
    _FakeBilibiliAPI.poll_result = ({"code": 0, "data": {"code": 0}}, [])
    _FakeCookieLoginHelper.valid = False

    body = client.post("/api/auth/poll", json={"qrcode_key": "k"}).json()
    assert body["status"] == "error"
    assert "验证未通过" in body["message"]


def test_poll_cookie_pool_sync_failure_is_tolerated(client, monkeypatch):
    """Cookie 池同步失败不应影响登录成功结果。"""
    _FakeBilibiliAPI.poll_result = ({"code": 0, "data": {"code": 0}}, [])

    def boom_pool():
        """模拟 Cookie 池不可用。"""
        raise RuntimeError("pool down")

    monkeypatch.setattr("bilibili.cookie_pool.get_cookie_pool", boom_pool)

    body = client.post("/api/auth/poll", json={"qrcode_key": "k"}).json()
    assert body["status"] == "confirmed"


def test_poll_network_error_returns_error_status_not_500(client, monkeypatch):
    """轮询异常应返回 error 供前端重试，而不是 500。"""

    def boom(*args, **kwargs):
        """模拟网络层不可用。"""
        raise RuntimeError("network down")

    monkeypatch.setattr(auth_module, "BilibiliAPI", boom)
    response = client.post("/api/auth/poll", json={"qrcode_key": "k"})
    assert response.status_code == 200
    assert response.json()["status"] == "error"


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------


def test_logout_clears_secret(client):
    """登出应把本地 Cookie 置空。"""
    _FakeConfig.store[auth_module.COOKIE_SECRET_KEY] = "SESSDATA=abc"

    body = client.post("/api/auth/logout").json()
    assert body == {"success": True, "message": "已清除登录态"}
    assert _FakeConfig.store[auth_module.COOKIE_SECRET_KEY] == ""


def test_logout_failure_returns_500(client, monkeypatch):
    """登出写盘失败应转 500。"""

    def boom(*args, **kwargs):
        """模拟配置写盘失败。"""
        raise RuntimeError("disk full")

    monkeypatch.setattr(auth_module, "ConfigManager", boom)
    response = client.post("/api/auth/logout")
    assert response.status_code == 500
    assert "清除登录态失败" in response.json()["detail"]
