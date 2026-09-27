"""bilibili.auth 底座测试（第1批补齐）。

覆盖范围：
- QRLoginStatus 状态常量
- QRCodeLogin.__init__ / generate_qrcode / poll_login_status
- QRCodeLogin._extract_cookie_from_response / _extract_cookie_from_set_cookie
- QRCodeLogin.login_with_qrcode / get_qrcode_image_base64
- CookieLoginHelper.validate_cookie / get_user_info / parse_cookie_to_dict
  / extract_important_fields

测试策略：
- B站接口统一用支持 ``__aenter__/__aexit__`` 的假客户端替换，断言中校验
  ``entered/exited`` 与 ``poll_qrcode`` 调用次数，确保异步上下文分支真实执行。
- 轮询睡眠用 shim 记录而非 Mock；时钟用可控替身驱动超时分支。
- 二维码为真实 qrcode 库渲染，断言 PNG magic，不做图像内容 mock。
"""
from __future__ import annotations

import asyncio
import base64
import importlib

import pytest

from core.exceptions import AuthenticationError, BilibiliAPIError, CookieExpiredError

auth_module = importlib.import_module("bilibili.auth")
QRCodeLogin = auth_module.QRCodeLogin
QRLoginStatus = auth_module.QRLoginStatus
CookieLoginHelper = auth_module.CookieLoginHelper


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class _AsyncioShim:
    """代理真实 asyncio，仅拦截 sleep 以记录轮询间隔。"""

    def __init__(self, recorder: list) -> None:
        """保存等待时长记录列表。"""
        self._recorder = recorder
        self._real = asyncio

    def __getattr__(self, name: str):
        """未拦截属性转发真实 asyncio。"""
        return getattr(self._real, name)

    async def sleep(self, delay, *args, **kwargs):
        """记录轮询间隔并立即返回。"""
        self._recorder.append(delay)


class _ClockDatetime:
    """可控时钟替身：按序吐出预设时间，最后一项会重复返回。"""

    def __init__(self, values: list) -> None:
        """保存时间序列（至少一项）。"""
        self._values = list(values)

    def now(self):
        """返回下一个时间点；序列只剩一项时固定返回它。"""
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


class _FakeAPIClient:
    """支持异步上下文协议的 B站接口替身。"""

    def __init__(self, controller, cookie=None) -> None:
        """绑定控制器与 Cookie。"""
        self._controller = controller
        self.cookie = cookie
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        """进入上下文并计数。"""
        self.entered += 1
        return self

    async def __aexit__(self, *_exc):
        """退出上下文并计数。"""
        self.exited += 1
        return False

    async def get(self, url, retry_times=1, **kwargs):
        """返回预置 payload 或抛出预置异常。"""
        self._controller.get_calls.append(url)
        if self._controller.get_error is not None:
            raise self._controller.get_error
        return self._controller.get_payload

    async def poll_qrcode(self, qrcode_key):
        """按脚本返回 (raw, set_cookie_headers) 或抛异常。"""
        self._controller.poll_calls.append(qrcode_key)
        if not self._controller.poll_script:
            raise AssertionError("poll_qrcode 被调用的次数多于脚本长度")
        item = self._controller.poll_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _APIController:
    """控制假接口的返回内容并记录调用。"""

    def __init__(self) -> None:
        """初始化默认二维码 payload 与空脚本。"""
        self.get_payload = {
            "url": "https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=KEY",
            "qrcode_key": "KEY",
        }
        self.get_error = None
        self.get_calls = []
        self.poll_script = []
        self.poll_calls = []
        self.instances = []

    def factory(self, cookie=None):
        """作为 BilibiliAPI 的可调用替身。"""
        client = _FakeAPIClient(self, cookie)
        self.instances.append(client)
        return client


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_stub(monkeypatch):
    """替换 auth 内的 BilibiliAPI 为可控假客户端。"""
    controller = _APIController()
    monkeypatch.setattr(auth_module, "BilibiliAPI", controller.factory)
    return controller


@pytest.fixture()
def sleeps(monkeypatch):
    """把 auth 内的 asyncio 换成记录型 shim，返回轮询间隔列表。"""
    recorder: list = []
    monkeypatch.setattr(auth_module, "asyncio", _AsyncioShim(recorder))
    return recorder


# ---------------------------------------------------------------------------
# QRLoginStatus / QRCodeLogin.__init__
# ---------------------------------------------------------------------------


def test_qr_login_status_codes_are_stable():
    """状态码是回调契约，改动会破坏前端与调用方，必须锁定。"""
    assert (
        QRLoginStatus.NOT_SCANNED,
        QRLoginStatus.SCANNED,
        QRLoginStatus.CONFIRMED,
        QRLoginStatus.EXPIRED,
        QRLoginStatus.ERROR,
    ) == (0, 1, 2, 3, -1)


def test_qr_code_login_initial_state_is_empty():
    """新建实例的全部登录态字段应为空。"""
    login = QRCodeLogin()

    assert login.qrcode_key is None
    assert login.qrcode_url is None
    assert login.login_result is None
    assert login.cookie is None


# ---------------------------------------------------------------------------
# QRCodeLogin.generate_qrcode
# ---------------------------------------------------------------------------


def test_generate_qrcode_returns_url_and_png_bytes(api_stub):
    """生成二维码应返回 URL 与可识别 PNG 字节，并缓存 key。"""
    login = QRCodeLogin()

    url, image = asyncio.run(login.generate_qrcode())

    assert url == api_stub.get_payload["url"]
    assert login.qrcode_url == url
    assert login.qrcode_key == "KEY"
    assert isinstance(image, bytes) and len(image) > 100
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    # 确认走了异步上下文协议，且退出过
    assert api_stub.instances[0].entered == 1
    assert api_stub.instances[0].exited == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "https://x", "qrcode_key": None},
        {"url": None, "qrcode_key": "KEY"},
        {"url": "", "qrcode_key": ""},
        {},
    ],
)
def test_generate_qrcode_rejects_incomplete_payload(api_stub, payload):
    """缺失 url 或 qrcode_key 时必须抛 AuthenticationError。"""
    api_stub.get_payload = payload
    login = QRCodeLogin()

    with pytest.raises(AuthenticationError) as excinfo:
        asyncio.run(login.generate_qrcode())

    assert "二维码数据不完整" in str(excinfo.value)


# ---------------------------------------------------------------------------
# QRCodeLogin.poll_login_status —— 前置校验与超时
# ---------------------------------------------------------------------------


def test_poll_login_status_requires_generated_qrcode():
    """未生成二维码时轮询必须立即失败，且不发请求。"""
    login = QRCodeLogin()

    with pytest.raises(AuthenticationError) as excinfo:
        asyncio.run(login.poll_login_status())

    assert "请先生成二维码" in str(excinfo.value)


def test_poll_login_status_raises_on_timeout_and_notifies_callback(api_stub, monkeypatch):
    """超过 timeout 必须抛异常并回调 EXPIRED。"""
    login = QRCodeLogin()
    login.qrcode_key = "KEY"

    class _Time:
        pass

    import datetime as _dt

    start = _dt.datetime(2026, 1, 1, 0, 0, 0)
    clock = _ClockDatetime([start, start + _dt.timedelta(seconds=181)])
    monkeypatch.setattr(auth_module, "datetime", clock)

    events = []
    with pytest.raises(AuthenticationError) as excinfo:
        asyncio.run(login.poll_login_status(timeout=180, callback=lambda s, m: events.append((s, m))))

    assert "登录超时，二维码已过期" in str(excinfo.value)
    assert events == [(QRLoginStatus.EXPIRED, "二维码已过期")]
    assert api_stub.poll_calls == []


# ---------------------------------------------------------------------------
# QRCodeLogin.poll_login_status —— 状态机
# ---------------------------------------------------------------------------


def _poll_response(code, *, url=None, message=""):
    """构造一份 poll_qrcode 的 (raw, headers) 响应。"""
    data = {"code": code, "message": message}
    if url is not None:
        data["url"] = url
    return ({"data": data}, [])


def test_poll_login_status_polls_through_all_states_until_confirmed(api_stub, sleeps):
    """未扫码 -> 已扫码 -> 已确认 必须依次回调，并在成功时返回 Cookie。"""
    api_stub.poll_script = [
        _poll_response(86101),
        _poll_response(86090),
        _poll_response(0, url="https://passport?DedeUserID=9&SESSDATA=sess&bili_jct=jct"),
        ("should-not-be-used", []),
    ]
    login = QRCodeLogin()
    login.qrcode_key = "KEY"
    events = []

    result = asyncio.run(
        login.poll_login_status(callback=lambda s, m: events.append(s))
    )

    assert result["success"] is True
    assert result["cookie"] == "DedeUserID=9; SESSDATA=sess; bili_jct=jct"
    assert events == [QRLoginStatus.NOT_SCANNED, QRLoginStatus.SCANNED, QRLoginStatus.CONFIRMED]
    assert api_stub.poll_calls == ["KEY", "KEY", "KEY"]
    # 状态推进过程中确实等待过两次，成功后不再睡眠
    assert sleeps == [2, 2]
    assert login.cookie == "DedeUserID=9; SESSDATA=sess; bili_jct=jct"
    assert login.login_result["code"] == 0


def test_poll_login_status_falls_back_to_set_cookie_header(api_stub, sleeps):
    """成功但 url 无 Cookie 参数时必须回退 Set-Cookie 通道。"""
    api_stub.poll_script = [
        (
            {"data": {"code": 0}},
            ["SESSDATA=sess2; Path=/; HttpOnly", "bili_jct=jct2; Path=/", "buvid3=unused"],
        )
    ]
    login = QRCodeLogin()
    login.qrcode_key = "KEY"

    result = asyncio.run(login.poll_login_status())

    assert result["cookie"] == "SESSDATA=sess2; bili_jct=jct2"


def test_poll_login_status_raises_when_expired_code(api_stub, sleeps):
    """86038 必须抛 AuthenticationError 并回调 EXPIRED。"""
    api_stub.poll_script = [_poll_response(86038)]
    login = QRCodeLogin()
    login.qrcode_key = "KEY"
    events = []

    with pytest.raises(AuthenticationError) as excinfo:
        asyncio.run(login.poll_login_status(callback=lambda s, m: events.append(s)))

    assert "二维码已过期" in str(excinfo.value)
    assert events == [QRLoginStatus.EXPIRED]


def test_poll_login_status_reports_unknown_code_and_keeps_polling(api_stub, sleeps):
    """未知状态码只回调 ERROR 并继续轮询，不能中断登录流程。"""
    api_stub.poll_script = [
        _poll_response(99999, message="weird"),
        _poll_response(0, url="https://passport?SESSDATA=ok"),
    ]
    login = QRCodeLogin()
    login.qrcode_key = "KEY"
    events = []

    result = asyncio.run(login.poll_login_status(callback=lambda s, m: events.append((s, m))))

    assert events[0] == (QRLoginStatus.ERROR, "weird")
    assert events[-1][0] == QRLoginStatus.CONFIRMED
    assert result["cookie"] == "SESSDATA=ok"


def test_poll_login_status_survives_transient_request_error(api_stub, sleeps):
    """单次请求异常必须被吞掉并继续下一轮，否则网络抖动就会登录失败。"""
    api_stub.poll_script = [
        BilibiliAPIError("502"),
        _poll_response(0, url="https://passport?SESSDATA=final"),
    ]
    login = QRCodeLogin()
    login.qrcode_key = "KEY"
    events = []

    result = asyncio.run(login.poll_login_status(callback=lambda s, m: events.append(s)))

    assert result["cookie"] == "SESSDATA=final"
    assert events[0] == QRLoginStatus.ERROR
    assert events[-1] == QRLoginStatus.CONFIRMED
    assert len(api_stub.poll_calls) == 2


def test_poll_login_status_reads_outer_code_when_data_code_absent(api_stub, sleeps):
    """兼容旧结构：data 内无 code 时回退读外层 code。"""
    api_stub.poll_script = [
        ({"code": 0, "data": {"url": "https://passport?SESSDATA=outer"}}, []),
    ]
    login = QRCodeLogin()
    login.qrcode_key = "KEY"

    result = asyncio.run(login.poll_login_status())

    assert result["cookie"] == "SESSDATA=outer"


# ---------------------------------------------------------------------------
# QRCodeLogin._extract_cookie_from_response / _extract_cookie_from_set_cookie
# ---------------------------------------------------------------------------


def test_extract_cookie_from_response_returns_empty_without_url_and_headers():
    """既无 url 也无 Set-Cookie 时必须返回空串而不是 None。"""
    login = QRCodeLogin()

    assert login._extract_cookie_from_response({}) == ""
    assert login._extract_cookie_from_response({"url": ""}) == ""


def test_extract_cookie_from_response_keeps_partial_fields():
    """url 只带部分关键字段时，只拼出存在的字段。"""
    login = QRCodeLogin()

    cookie = login._extract_cookie_from_response(
        {"url": "https://passport/x?SESSDATA=only&other=1"}
    )

    assert cookie == "SESSDATA=only"


def test_extract_cookie_from_response_orders_three_fields():
    """三个关键字段必须按 DedeUserID/SESSDATA/bili_jct 固定顺序拼接。"""
    login = QRCodeLogin()

    cookie = login._extract_cookie_from_response(
        {"url": "https://p?bili_jct=j&DedeUserID=1&SESSDATA=s"}
    )

    assert cookie == "DedeUserID=1; SESSDATA=s; bili_jct=j"


def test_extract_cookie_from_response_falls_back_when_url_lacks_fields():
    """url 存在但无 Cookie 参数时回退 Set-Cookie。"""
    login = QRCodeLogin()

    cookie = login._extract_cookie_from_response(
        {"url": "https://passport/done?code=0"},
        ["SESSDATA=from-header; Path=/"],
    )

    assert cookie == "SESSDATA=from-header"


def test_extract_cookie_from_response_uses_headers_when_url_missing():
    """无 url 时直接用 Set-Cookie 通道。"""
    login = QRCodeLogin()

    cookie = login._extract_cookie_from_response({}, ["DedeUserID=5; Path=/"])

    assert cookie == "DedeUserID=5"


def test_extract_cookie_from_set_cookie_parses_attributes_and_order():
    """Set-Cookie 需剥离属性段，并按固定顺序只保留三个登录态字段。"""
    headers = [
        "buvid3=noise; Path=/; HttpOnly",
        "SESSDATA=sess3; Path=/; Domain=.bilibili.com; HttpOnly",
        "DedeUserID=77; Path=/",
        "bili_jct=jct3; Path=/; Secure",
    ]

    assert (
        QRCodeLogin._extract_cookie_from_set_cookie(headers)
        == "DedeUserID=77; SESSDATA=sess3; bili_jct=jct3"
    )


@pytest.mark.parametrize("headers", [None, []])
def test_extract_cookie_from_set_cookie_handles_empty_input(headers):
    """空输入必须返回空串。"""
    assert QRCodeLogin._extract_cookie_from_set_cookie(headers) == ""


def test_extract_cookie_from_set_cookie_skips_malformed_entries():
    """无等号的畸形头必须被跳过，且不影响其他头解析。"""
    headers = ["junk", "=empty-key", "SESSDATA=s"]

    assert QRCodeLogin._extract_cookie_from_set_cookie(headers) == "SESSDATA=s"


# ---------------------------------------------------------------------------
# QRCodeLogin.login_with_qrcode / get_qrcode_image_base64
# ---------------------------------------------------------------------------


def test_login_with_qrcode_chains_generation_and_polling(api_stub, monkeypatch):
    """完整流程必须先生成二维码、回调带图片字节，再返回轮询结果。"""
    login = QRCodeLogin()
    events = []

    async def _fake_poll(timeout=180, interval=2, callback=None):
        """真实协程替身：记录入参并返回固定结果。"""
        events.append(("poll", timeout, interval))
        return {"success": True, "cookie": "SESSDATA=chain"}

    monkeypatch.setattr(login, "poll_login_status", _fake_poll)

    result = asyncio.run(
        login.login_with_qrcode(callback=lambda s, m, img: events.append((s, m, bool(img))), timeout=99)
    )

    assert result == {"success": True, "cookie": "SESSDATA=chain"}
    assert events[0] == (QRLoginStatus.NOT_SCANNED, "二维码已生成", True)
    assert events[1] == ("poll", 99, 2)
    # 生成阶段真实调用了接口
    assert api_stub.get_calls


def test_get_qrcode_image_base64_roundtrips():
    """base64 编码必须可无失真还原原始字节。"""
    login = QRCodeLogin()
    raw = b"\x89PNG\r\n\x1a\nbinary-payload"

    encoded = login.get_qrcode_image_base64(raw)

    assert isinstance(encoded, str)
    assert base64.b64decode(encoded) == raw


# ---------------------------------------------------------------------------
# CookieLoginHelper
# ---------------------------------------------------------------------------


def test_validate_cookie_returns_true_when_logged_in(api_stub):
    """isLogin=True 必须返回 True，并真实走完异步上下文。"""
    api_stub.get_payload = {"isLogin": True}

    assert asyncio.run(CookieLoginHelper.validate_cookie("SESSDATA=a")) is True
    assert api_stub.instances[0].entered == 1
    assert api_stub.instances[0].exited == 1


def test_validate_cookie_returns_false_when_not_logged_in(api_stub):
    """isLogin=False 必须返回 False。"""
    api_stub.get_payload = {"isLogin": False}

    assert asyncio.run(CookieLoginHelper.validate_cookie("SESSDATA=a")) is False


def test_validate_cookie_swallows_cookie_expired(api_stub):
    """CookieExpiredError 必须收敛为 False，不向外抛。"""
    api_stub.get_error = CookieExpiredError()

    assert asyncio.run(CookieLoginHelper.validate_cookie("SESSDATA=a")) is False


def test_validate_cookie_swallows_unexpected_error(api_stub):
    """其他异常同样收敛为 False（调用方只看布尔值）。"""
    api_stub.get_error = BilibiliAPIError("500")

    assert asyncio.run(CookieLoginHelper.validate_cookie("SESSDATA=a")) is False


def test_get_user_info_maps_nav_fields(api_stub):
    """nav 响应必须映射为界面所需的用户信息结构。"""
    api_stub.get_payload = {
        "isLogin": True,
        "mid": 10086,
        "uname": "测试UP",
        "face": "https://face/x.jpg",
        "level_info": {"current_level": 6},
        "vip": {"type": 2},
    }

    info = asyncio.run(CookieLoginHelper.get_user_info("SESSDATA=a"))

    assert info == {
        "uid": 10086,
        "username": "测试UP",
        "face": "https://face/x.jpg",
        "level": 6,
        "vip_type": 2,
        "is_login": True,
    }


def test_get_user_info_raises_when_not_logged_in(api_stub):
    """未登录时必须抛 AuthenticationError，而不是返回空信息。"""
    api_stub.get_payload = {"isLogin": False}

    with pytest.raises(AuthenticationError):
        asyncio.run(CookieLoginHelper.get_user_info("SESSDATA=dead"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SESSDATA=a; bili_jct=b", {"SESSDATA": "a", "bili_jct": "b"}),
        ("  A=1 ;  B=2  ", {"A": "1", "B": "2"}),
        (";A=1;;B=2;", {"A": "1", "B": "2"}),
        ("", {}),
        ("noequals", {}),
        ("k=a=b", {"k": "a=b"}),
    ],
)
def test_parse_cookie_to_dict_boundaries(raw, expected):
    """Cookie 解析必须容忍空白、空项、畸形项与值内含等号。"""
    assert CookieLoginHelper.parse_cookie_to_dict(raw) == expected


def test_extract_important_fields_fills_missing_with_empty_string():
    """六个关键字段必须始终存在，缺失用空串补齐。"""
    fields = CookieLoginHelper.extract_important_fields("SESSDATA=s; buvid3=b")

    assert fields == {
        "sessdata": "s",
        "bili_jct": "",
        "buvid3": "b",
        "buvid4": "",
        "DedeUserID": "",
        "DedeUserID__ckMd5": "",
    }


def test_extract_important_fields_reads_all_keys():
    """完整 Cookie 应逐项提取六个字段。"""
    raw = (
        "SESSDATA=s1; bili_jct=j1; buvid3=b3; buvid4=b4; "
        "DedeUserID=123; DedeUserID__ckMd5=md5"
    )

    fields = CookieLoginHelper.extract_important_fields(raw)

    assert fields["sessdata"] == "s1"
    assert fields["bili_jct"] == "j1"
    assert fields["buvid3"] == "b3"
    assert fields["buvid4"] == "b4"
    assert fields["DedeUserID"] == "123"
    assert fields["DedeUserID__ckMd5"] == "md5"
