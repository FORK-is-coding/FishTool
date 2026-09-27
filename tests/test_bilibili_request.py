"""Bilibili 通用请求器核心分支测试。"""
import asyncio
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bilibili.api.client import BilibiliAPICore
from core.exceptions import BilibiliAPIError, CookieExpiredError, InvalidResponseError, RateLimitError


class FakeResponse:
    """实现 aiohttp 响应所需的异步上下文协议。"""

    def __init__(self, status=200, payload=None, text="invalid", headers=None):
        self.status = status
        self.payload = payload
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self):
        """进入异步响应上下文并返回自身。"""
        return self

    async def __aexit__(self, *_args):
        """退出异步响应上下文。"""
        return False

    async def json(self):
        """返回 JSON 数据，异常对象用于模拟解析失败。"""
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def text(self):
        """返回原始响应文本。"""
        return self._text


class FakeSession:
    """按顺序返回预设响应的会话替身。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.headers = {}
        self.closed = False
        self.calls = []

    def request(self, **kwargs):
        """记录请求参数并返回下一个响应。"""
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeLimiter:
    """记录限频器调用。"""

    def __init__(self, retry_after=0):
        self.retry_after = retry_after
        self.acquired = []
        self.successes = 0
        self.reports = []

    async def acquire(self, endpoint=None):
        """记录获取令牌的端点。"""
        self.acquired.append(endpoint)

    def report_429(self, endpoint):
        """记录限流并返回零等待。"""
        self.reports.append(endpoint)
        return self.retry_after

    def report_success(self):
        """记录成功请求。"""
        self.successes += 1


def build_core(*responses, limiter=None):
    """构造绕过真实网络初始化的请求器。"""
    core = BilibiliAPICore()
    core.session = FakeSession(*responses)
    core.headers = {"User-Agent": "pytest"}
    core.cookie = ""
    core.cookie_pool = None
    core.rate_limiter = limiter
    core.wbi_signer = SimpleNamespace(sign_params=AsyncMock(side_effect=lambda params, _session: {**params, "signed": 1}))
    core.init_session = AsyncMock()
    return core


def test_set_cookie_uses_request_header_snapshot_with_read_only_session_headers():
    """会话默认头只读时，Cookie 仍应通过每次请求的头快照发送。"""
    core = build_core(FakeResponse(payload={"code": 0, "data": {"ok": True}}))
    core.session.headers = MappingProxyType({"User-Agent": "session-default"})

    core.set_cookie("SESSDATA=test; buvid3=fingerprint")
    result = asyncio.run(core.request("GET", "https://example.test", retry_times=1))

    assert result == {"ok": True}
    assert core.cookie == "SESSDATA=test; buvid3=fingerprint"
    assert core.session.calls[0]["headers"]["Cookie"] == "SESSDATA=test; buvid3=fingerprint"
    assert core.session.headers == {"User-Agent": "session-default"}


def test_request_success_merges_headers_signs_and_reports_success():
    """成功请求应签名参数、合并请求头并剥离 data 外层。"""
    limiter = FakeLimiter()
    core = build_core(FakeResponse(payload={"code": 0, "data": {"ok": True}}), limiter=limiter)

    result = asyncio.run(core.request("GET", "https://example.test", params={"a": 1}, headers={"X-Test": "yes"}, need_sign=True))

    assert result == {"ok": True}
    assert core.session.calls[0]["params"] == {"a": 1, "signed": 1}
    assert core.session.calls[0]["headers"] == {"User-Agent": "pytest", "X-Test": "yes"}
    assert limiter.acquired == ["https://example.test"]
    assert limiter.successes == 1


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (FakeResponse(status=412), BilibiliAPIError),
        (FakeResponse(payload={"code": -101, "message": "expired"}), CookieExpiredError),
        (FakeResponse(payload={"code": -352, "message": "risk"}), BilibiliAPIError),
        (FakeResponse(payload=ValueError("bad json"), text="<html>bad</html>"), InvalidResponseError),
    ],
)
def test_request_non_retryable_error_branches(response, error_type):
    """反爬、Cookie 失效、风控和非法 JSON 应立即抛出明确异常。"""
    core = build_core(response)
    with pytest.raises(error_type):
        asyncio.run(core.request("GET", "https://example.test", retry_times=1))


def test_request_retries_http_429_then_succeeds(monkeypatch):
    """HTTP 429 应报告限频并在后续尝试成功。"""
    limiter = FakeLimiter()
    core = build_core(
        FakeResponse(status=429),
        FakeResponse(payload={"code": 0, "data": {"retried": True}}),
        limiter=limiter,
    )
    monkeypatch.setattr("bilibili.api.client.asyncio.sleep", AsyncMock())

    result = asyncio.run(core.request("GET", "https://example.test", retry_times=2))

    assert result == {"retried": True}
    assert limiter.reports == ["https://example.test"]
    assert len(core.session.calls) == 2


def test_minus_799_exhaustion_raises_rate_limit(monkeypatch):
    """业务码 -799 重试耗尽后必须转为 RateLimitError。"""
    core = build_core(
        FakeResponse(payload={"code": -799, "message": "busy"}),
        FakeResponse(payload={"code": -799, "message": "busy"}),
    )
    monkeypatch.setattr("bilibili.api.client.asyncio.sleep", AsyncMock())

    with pytest.raises(RateLimitError):
        asyncio.run(core.request("GET", "https://example.test", retry_times=2))
