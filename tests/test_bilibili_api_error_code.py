"""BilibiliAPIError 携带 B站数字业务码（bili_code）的契约测试。

AI 伪人巡检的举报客户端需要按 12008/12019/-352 等数字码分流；这里锁定
client.request() 各错误分支抛出的异常都带上正确的 bili_code，且消息文本
保持不变（旧代码 extract_api_code 仍按消息解析）。
"""
import asyncio

import pytest

from core.exceptions import (
    BilibiliAPIError,
    CookieExpiredError,
    RateLimitError,
    Status429Error,
)
from tests.test_bilibili_request import FakeLimiter, FakeResponse, build_core


def _run(core):
    return asyncio.run(core.request("POST", "https://api.bilibili.com/x/v2/reply/report", retry_times=1))


@pytest.mark.parametrize("code", [12008, 12019, 12002, -111, -400])
def test_generic_business_code_is_exposed(code):
    core = build_core(FakeResponse(payload={"code": code, "message": "msg"}))
    with pytest.raises(BilibiliAPIError) as info:
        _run(core)
    assert info.value.bili_code == code
    # 消息格式不变，兼容 modules/hotspot/discovery/sources.py:extract_api_code
    assert str(info.value) == f"API错误 [{code}]: msg"
    assert info.value.code == "BILIBILI_API_ERROR"


@pytest.mark.parametrize("code", [-352, -412])
def test_risk_business_code_is_exposed(code):
    core = build_core(FakeResponse(payload={"code": code, "message": "风控"}))
    with pytest.raises(BilibiliAPIError) as info:
        _run(core)
    assert info.value.bili_code == code
    assert str(info.value) == "请求被风控: 风控"


def test_http_412_maps_to_negative_412():
    core = build_core(FakeResponse(status=412, payload={}))
    with pytest.raises(BilibiliAPIError) as info:
        _run(core)
    assert info.value.bili_code == -412


def test_cookie_expired_has_minus_101():
    core = build_core(FakeResponse(payload={"code": -101, "message": "账号未登录"}))
    with pytest.raises(CookieExpiredError) as info:
        _run(core)
    assert info.value.bili_code == -101


def test_minus_509_without_limiter():
    core = build_core(FakeResponse(payload={"code": -509, "message": "频繁"}))
    with pytest.raises(RateLimitError) as info:
        _run(core)
    assert info.value.bili_code == -509


def test_minus_509_with_limiter_after_retries():
    core = build_core(FakeResponse(payload={"code": -509, "message": "频繁"}), limiter=FakeLimiter(retry_after=0))
    with pytest.raises(RateLimitError) as info:
        _run(core)
    assert info.value.bili_code == -509


def test_minus_799_on_last_attempt():
    core = build_core(FakeResponse(payload={"code": -799, "message": "受限"}))
    with pytest.raises(RateLimitError) as info:
        _run(core)
    assert info.value.bili_code == -799


def test_http_429_after_retries():
    core = build_core(FakeResponse(status=429, payload={}), limiter=FakeLimiter(retry_after=0))
    with pytest.raises(Status429Error) as info:
        _run(core)
    assert info.value.bili_code == 429


def test_default_bili_code_is_none():
    assert BilibiliAPIError("x").bili_code is None
    assert RateLimitError(3).bili_code is None
