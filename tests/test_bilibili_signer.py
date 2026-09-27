"""bilibili.api.signer WBISigner 签名器测试（第4批 · 其他段）。

覆盖对象：
- WBISigner：混淆表、mixin_key 生成、参数签名（enc_wbi）、密钥刷新（update_wbi_keys）、
  过期判断（need_update）、自动刷新的签名入口（sign_params）。

测试策略：
- 网络层使用契约级假 aiohttp 会话（支持 ``async with session.get(url) as resp`` 协议），
  不使用 AsyncMock，也不发起真实请求。
- 仓库未安装 pytest-asyncio，async 用例统一用 ``asyncio.run(...)`` 驱动（沿用既有测试约定）。
- 时间源通过 monkeypatch ``signer.time.time`` 固定，保证 w_rid 可复算。
- 失败分支（HTTP 非 200 / wbi_img 缺失 / 网络异常）一律从外部以 pytest.raises 断言。
"""
from __future__ import annotations

import asyncio
import hashlib
import urllib.parse
from datetime import datetime, timedelta

import pytest

from core.exceptions import WBISignError
from bilibili.api import signer as signer_module
from bilibili.api.signer import WBISigner


NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
# 真实 img_key/sub_key 各 32 位；get_mixin_key 依赖拼接长度 >= 64。
IMG_KEY = "i" * 32
SUB_KEY = "s" * 32


# ---------------------------------------------------------------------------
# 契约级假 aiohttp 会话
# ---------------------------------------------------------------------------


class _FakeResponse:
    """最小 aiohttp 响应替身：只暴露 status 与 awaitable json()。"""

    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload

    async def json(self) -> dict:
        """返回预置 JSON 负载。"""
        return self._payload


class _FakeGetContext:
    """``session.get(url)`` 的异步上下文管理器替身。"""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        """进入上下文返回响应对象。"""
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """退出上下文不吞异常。"""
        return False


class _FakeSession:
    """记录请求地址的假会话；可选注入异常以模拟网络故障。"""

    def __init__(self, response: _FakeResponse | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[str] = []

    def get(self, url: str) -> _FakeGetContext:
        """记录 URL 并返回上下文；注入异常时直接抛出。"""
        self.calls.append(url)
        if self._error is not None:
            raise self._error
        assert self._response is not None, "假会话缺少响应"
        return _FakeGetContext(self._response)


def _nav_payload(img_name: str = "A" * 32, sub_name: str = "B" * 32, code: int = 0) -> dict:
    """构造 nav 接口形态的响应体。"""
    return {
        "code": code,
        "message": "0",
        "data": {
            "wbi_img": {
                "img_url": f"https://i0.hdslb.com/bfs/wbi/{img_name}.png",
                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{sub_name}.png",
            }
        },
    }


@pytest.fixture()
def fixed_time(monkeypatch):
    """把 signer 模块内 time.time 固定为常量，保证签名可复算。"""
    monkeypatch.setattr(signer_module.time, "time", lambda: 1_700_000_000)
    return 1_700_000_000


# ---------------------------------------------------------------------------
# 构造与混淆表
# ---------------------------------------------------------------------------


def test_init_defaults():
    """初始密钥为空，刷新间隔为 1 小时。"""
    signer = WBISigner()
    assert signer.img_key is None
    assert signer.sub_key is None
    assert signer.mixin_key is None
    assert signer.last_update is None
    assert signer.update_interval == timedelta(hours=1)


def test_mixin_key_table_is_permutation_of_0_to_63():
    """混淆表应为 0..63 的一个完整排列。"""
    table = WBISigner.MIXIN_KEY_ENC_TAB
    assert len(table) == 64
    assert set(table) == set(range(64))


def test_get_mixin_key_is_deterministic_and_32_chars():
    """mixin_key 取混淆后前 32 位，且同输入稳定。"""
    signer = WBISigner()
    orig = "".join(chr(0x61 + (i % 26)) for i in range(64))
    first = signer.get_mixin_key(orig)
    second = signer.get_mixin_key(orig)
    assert len(first) == 32
    assert first == second
    # 手工按表复算，验证实现与契约一致
    expected = "".join(orig[index] for index in WBISigner.MIXIN_KEY_ENC_TAB)[:32]
    assert first == expected


# ---------------------------------------------------------------------------
# enc_wbi 签名
# ---------------------------------------------------------------------------


def test_enc_wbi_adds_timestamp_and_signature(fixed_time):
    """签名结果应补充 wts 时间戳与 32 位十六进制 w_rid。"""
    signer = WBISigner()
    params = {"mid": 1, "keyword": "原神"}
    signed = signer.enc_wbi(params, IMG_KEY, SUB_KEY)

    # 签名前会对所有值做 str() 归一，因此 wts 以字符串形式返回。
    assert signed["wts"] == str(fixed_time)
    assert len(signed["w_rid"]) == 32
    int(signed["w_rid"], 16)  # 必须是合法十六进制


def test_enc_wbi_signature_matches_manual_digest(fixed_time):
    """w_rid 应等于 md5(urlencode(排序参数) + mixin_key) 的手工复算值。"""
    signer = WBISigner()
    img_key, sub_key = IMG_KEY, SUB_KEY
    signed = signer.enc_wbi({"b": 2, "a": 1}, img_key, sub_key)

    params = {"a": 1, "b": 2, "wts": fixed_time}
    mixin_key = signer.get_mixin_key(img_key + sub_key)
    expected = hashlib.md5((urllib.parse.urlencode(params) + mixin_key).encode()).hexdigest()
    assert signed["w_rid"] == expected


def test_enc_wbi_filters_special_characters(fixed_time):
    """参数值中的 !'()* 特殊字符应在签名前被剔除。"""
    signer = WBISigner()
    img_key, sub_key = IMG_KEY, SUB_KEY
    signed = signer.enc_wbi({"q": "a!b'c(d)e*f"}, img_key, sub_key)

    mixin_key = signer.get_mixin_key(img_key + sub_key)
    cleaned = urllib.parse.urlencode({"q": "abcdef", "wts": fixed_time})
    expected = hashlib.md5((cleaned + mixin_key).encode()).hexdigest()
    assert signed["w_rid"] == expected


def test_enc_wbi_sorts_keys_before_signing(fixed_time):
    """参数按字典序排序后再签名，键顺序不影响结果。"""
    signer = WBISigner()
    first = signer.enc_wbi({"z": 1, "a": 2, "m": 3}, IMG_KEY, SUB_KEY)
    second = signer.enc_wbi({"m": 3, "z": 1, "a": 2}, IMG_KEY, SUB_KEY)
    assert first["w_rid"] == second["w_rid"]


# ---------------------------------------------------------------------------
# need_update 过期判断
# ---------------------------------------------------------------------------


def test_need_update_true_when_never_refreshed():
    """从未刷新时应需要更新。"""
    assert WBISigner().need_update() is True


def test_need_update_false_within_interval():
    """刷新时间在 1 小时内时不需要更新。"""
    signer = WBISigner()
    signer.last_update = datetime.now()
    assert signer.need_update() is False


def test_need_update_true_after_interval():
    """超过 1 小时未刷新时需要更新。"""
    signer = WBISigner()
    signer.last_update = datetime.now() - timedelta(hours=2)
    assert signer.need_update() is True


# ---------------------------------------------------------------------------
# update_wbi_keys
# ---------------------------------------------------------------------------


def test_update_wbi_keys_success_extracts_keys():
    """nav 正常返回时应提取 img/sub 密钥并生成 mixin_key。"""
    signer = WBISigner()
    session = _FakeSession(_FakeResponse(200, _nav_payload("img" + "A" * 29, "sub" + "B" * 29)))

    asyncio.run(signer.update_wbi_keys(session))

    assert session.calls == [NAV_URL]
    assert signer.img_key == "img" + "A" * 29
    assert signer.sub_key == "sub" + "B" * 29
    assert signer.mixin_key == signer.get_mixin_key("img" + "A" * 29 + "sub" + "B" * 29)
    assert isinstance(signer.last_update, datetime)


def test_update_wbi_keys_accepts_not_logged_in_response():
    """未登录 code=-101 但 wbi_img 正常时不应中断签名流程。"""
    signer = WBISigner()
    session = _FakeSession(_FakeResponse(200, _nav_payload(code=-101)))

    asyncio.run(signer.update_wbi_keys(session))
    assert signer.img_key is not None


def test_update_wbi_keys_raises_on_http_error():
    """HTTP 非 200 应转为 WBISignError。"""
    signer = WBISigner()
    session = _FakeSession(_FakeResponse(503, {}))

    with pytest.raises(WBISignError):
        asyncio.run(signer.update_wbi_keys(session))


def test_update_wbi_keys_raises_when_wbi_img_missing():
    """缺少 wbi_img 时应转为 WBISignError。"""
    signer = WBISigner()
    session = _FakeSession(_FakeResponse(200, {"code": 0, "data": {}}))

    with pytest.raises(WBISignError):
        asyncio.run(signer.update_wbi_keys(session))


def test_update_wbi_keys_wraps_network_exception():
    """底层网络异常应被包装为 WBISignError。"""
    signer = WBISigner()
    session = _FakeSession(error=ConnectionError("boom"))

    with pytest.raises(WBISignError):
        asyncio.run(signer.update_wbi_keys(session))


# ---------------------------------------------------------------------------
# sign_params 自动刷新
# ---------------------------------------------------------------------------


def test_sign_params_refreshes_when_needed(fixed_time):
    """首次签名应先刷新密钥再完成签名。"""
    signer = WBISigner()
    session = _FakeSession(_FakeResponse(200, _nav_payload()))

    signed = asyncio.run(signer.sign_params({"mid": 1}, session))

    assert session.calls == [NAV_URL]
    assert signed["w_rid"]
    assert signed["wts"] == str(fixed_time)


def test_sign_params_skips_refresh_when_fresh(fixed_time):
    """密钥新鲜时不应重复请求 nav。"""
    signer = WBISigner()
    signer.img_key = IMG_KEY
    signer.sub_key = SUB_KEY
    signer.last_update = datetime.now()
    session = _FakeSession(_FakeResponse(200, _nav_payload()))

    signed = asyncio.run(signer.sign_params({"mid": 1}, session))

    assert session.calls == []
    assert signed["w_rid"]
