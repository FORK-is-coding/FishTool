"""llm.client 底座测试（第1批补齐 · llm 段）。

覆盖范围：
- LLMClient.__init__ / __aenter__ / __aexit__ / close
- LLMClient._check_token_limit / _record_token_usage
- LLMClient.chat_completion / chat_completion_stream / simple_chat / batch_process
- LLMClient.get_daily_usage / is_configured
- 模块级 get_llm_client / is_llm_available

测试策略：
- HTTP 层用实现 ``post`` / ``stream`` / ``aclose`` 的假客户端替换（含 async context
  manager），断言中校验真实调用路径，不用 AsyncMock 顶替协程语义。
- 数据库会话用记录型替身：验证 add/commit/close 与失败降级分支，不落盘。
- 失败分支断言一律从外部用 ``pytest.raises`` 打，不吞异常。
- 冒烟：token 限额跨天重置用可控 date 替身驱动。
"""
from __future__ import annotations

import asyncio
import importlib
from datetime import date, datetime

import httpx
import pytest

from core.exceptions import (
    LLMAPIError,
    LLMError,
    LLMNotConfiguredError,
    TokenLimitExceededError,
)


llm_module = importlib.import_module("llm.client")


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _RecordingLogger:
    """记录日志调用的替身，避免触发真实文件日志初始化。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls = []

    def _record(self, level, *args, **kwargs):
        """保存日志级别与参数。"""
        self.calls.append((level, args, kwargs))

    def debug(self, *a, **k):
        """记录 debug。"""
        self._record("debug", *a, **k)

    def info(self, *a, **k):
        """记录 info。"""
        self._record("info", *a, **k)

    def warning(self, *a, **k):
        """记录 warning。"""
        self._record("warning", *a, **k)

    def error(self, *a, **k):
        """记录 error。"""
        self._record("error", *a, **k)

    def exception(self, *a, **k):
        """记录 exception。"""
        self._record("exception", *a, **k)


class _FakeConfig:
    """只暴露 get / get_secret 的配置替身。"""

    def __init__(self, values: dict | None = None, secrets: dict | None = None) -> None:
        """保存预置普通配置与敏感配置。"""
        self._values = values or {}
        self._secrets = secrets or {}

    def get(self, key, default=None):
        """返回普通配置项。"""
        return self._values.get(key, default)

    def get_secret(self, key, default=None):
        """返回敏感配置项。"""
        return self._secrets.get(key, default)


class _FakeResponse:
    """chat_completion 用的响应替身。"""

    def __init__(self, payload=None, status_error=None) -> None:
        """保存预置 payload 与可选状态异常。"""
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self):
        """按预置异常触发 raise_for_status。"""
        if self._status_error is not None:
            raise self._status_error

    def json(self):
        """返回预置 JSON 数据。"""
        return self._payload


class _AsyncLineResponse:
    """流式响应替身：可迭代 SSE 行。"""

    def __init__(self, lines, status_error=None) -> None:
        """保存预置行与可选状态异常。"""
        self._lines = list(lines)
        self._status_error = status_error

    def raise_for_status(self):
        """按预置异常触发 raise_for_status。"""
        if self._status_error is not None:
            raise self._status_error

    async def aiter_lines(self):
        """逐行产出 SSE 文本。"""
        for line in self._lines:
            yield line


class _FakeStreamContext:
    """``client.stream(...)`` 返回的异步上下文管理器替身。"""

    def __init__(self, response) -> None:
        """保存要产出的响应对象。"""
        self._response = response

    async def __aenter__(self):
        """进入上下文返回响应。"""
        return self._response

    async def __aexit__(self, *exc):
        """退出上下文，不吞异常。"""
        return False


class _FakeHTTPClient:
    """httpx.AsyncClient 替身：覆盖 post / stream / aclose。"""

    def __init__(self) -> None:
        """初始化可配置的响应与调用记录。"""
        self.post_response = None
        self.post_error = None
        self.stream_response = None
        self.stream_error = None
        self.posts = []
        self.streams = []
        self.closed = 0

    async def post(self, url, json=None, **kwargs):
        """记录请求并返回预置响应或抛预置异常。"""
        self.posts.append({"url": url, "json": json, "kwargs": kwargs})
        if self.post_error is not None:
            raise self.post_error
        return self.post_response

    def stream(self, method, url, json=None, **kwargs):
        """记录请求并返回异步上下文管理器。"""
        self.streams.append({"method": method, "url": url, "json": json})
        if self.stream_error is not None:
            raise self.stream_error
        return _FakeStreamContext(self.stream_response)

    async def aclose(self):
        """累加关闭次数。"""
        self.closed += 1


class _FakeSession:
    """记录型数据库会话替身，模拟 query 链与增删写。"""

    def __init__(self, records=None, fail_on_add: bool = False) -> None:
        """保存预置查询结果与写入失败开关。"""
        self.records = records or []
        self.fail_on_add = fail_on_add
        self.added = []
        self.committed = 0
        self.closed = 0

    def add(self, obj):
        """记录新增对象，可模拟失败。"""
        if self.fail_on_add:
            raise RuntimeError("add boom")
        self.added.append(obj)

    def commit(self):
        """累加提交次数。"""
        self.committed += 1

    def close(self):
        """累加关闭次数。"""
        self.closed += 1

    def query(self, model):
        """返回自身以支持链式调用。"""
        return self

    def filter(self, *args, **kwargs):
        """返回自身以支持链式调用。"""
        return self

    def all(self):
        """返回预置记录列表。"""
        return self.records


class _Rec:
    """LLMUsage 查询结果替身，只暴露聚合所需字段。"""

    def __init__(self, total_tokens: int, request_count: int) -> None:
        """保存单条统计值。"""
        self.total_tokens = total_tokens
        self.request_count = request_count


def _http_status_error(status_code: int, text: str) -> httpx.HTTPStatusError:
    """构造真实 httpx.HTTPStatusError，供 API 异常分支断言。"""
    request = httpx.Request("POST", "http://test.local/v1/chat/completions")
    response = httpx.Response(status_code, text=text, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


# ---------------------------------------------------------------------------
# 环境隔离 fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def llm_env(monkeypatch):
    """隔离 llm.client：假 httpx 工厂 + 受控配置 + 记录 logger + 重置单例。"""
    config = _FakeConfig(
        values={
            "llm.api_base": "http://test.local/v1",
            "llm.model": "test-model",
            "llm.temperature": 0.7,
            "llm.max_tokens": 2000,
            "llm.batch_size": 2,
            "llm.daily_token_limit": 1000,
        },
        secrets={"llm_api_key": "sk-test"},
    )
    monkeypatch.setattr(llm_module, "config", config)
    monkeypatch.setattr(llm_module, "logger", _RecordingLogger())

    created: list = []

    def factory(*args, **kwargs):
        """替身 httpx.AsyncClient 工厂，记录构造出的假客户端。"""
        client = _FakeHTTPClient()
        client.init_kwargs = kwargs
        created.append(client)
        return client

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", factory)
    monkeypatch.setattr(llm_module, "_global_llm_client", None)
    return {"config": config, "created": created}


def _make_client(**overrides) -> "llm_module.LLMClient":
    """构造使用显式参数的 LLMClient（httpx 已由 fixture 替换）。"""
    kwargs = {"api_key": "sk-test", "api_base": "http://test.local/v1", "model": "test-model"}
    kwargs.update(overrides)
    return llm_module.LLMClient(**kwargs)


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------


def test_init_uses_explicit_params(llm_env):
    """显式参数优先，daily_token_limit 来自配置。"""
    client = _make_client(timeout=5)
    assert client.api_key == "sk-test"
    assert client.api_base == "http://test.local/v1"
    assert client.model == "test-model"
    assert client.timeout == 5
    assert client.daily_token_limit == 1000
    assert isinstance(client.client, _FakeHTTPClient)


def test_init_falls_back_to_config(llm_env):
    """不传参数时全部走配置（含加密 secrets 里的密钥）。"""
    client = llm_module.LLMClient()
    assert client.api_key == "sk-test"
    assert client.api_base == "http://test.local/v1"
    assert client.model == "test-model"


def test_init_without_api_key_raises(monkeypatch):
    """缺密钥直接抛 LLMNotConfiguredError。"""
    monkeypatch.setattr(
        llm_module, "config", _FakeConfig(values={"llm.api_base": "http://x/v1"})
    )
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", lambda *a, **k: _FakeHTTPClient())
    with pytest.raises(LLMNotConfiguredError) as excinfo:
        llm_module.LLMClient()
    assert "密钥" in str(excinfo.value)


def test_init_without_api_base_raises(monkeypatch):
    """缺 API 地址也抛 LLMNotConfiguredError。"""
    monkeypatch.setattr(
        llm_module,
        "config",
        _FakeConfig(secrets={"llm_api_key": "sk"}, values={"llm.api_base": ""}),
    )
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", lambda *a, **k: _FakeHTTPClient())
    with pytest.raises(LLMNotConfiguredError) as excinfo:
        llm_module.LLMClient()
    assert "URL" in str(excinfo.value)


def test_async_context_manager_closes_client(llm_env):
    """async with 退出时关闭底层 HTTP 客户端。"""
    client = _make_client()
    fake = client.client

    async def _scenario():
        """进入并退出异步上下文，返回关闭次数。"""
        async with client as entered:
            assert entered is client
            assert fake.closed == 0
        return fake.closed

    assert asyncio.run(_scenario()) == 1


# ---------------------------------------------------------------------------
# Token 限额
# ---------------------------------------------------------------------------


def test_check_token_limit_allows_within_budget(llm_env):
    """未越界时正常通过。"""
    client = _make_client()
    client.daily_tokens_used = 999
    client._check_token_limit(0)
    client._check_token_limit(1)  # 1000 <= 1000 边界内


def test_check_token_limit_exceeded_raises(llm_env):
    """预检越界抛 TokenLimitExceededError 并携带 used/limit。"""
    client = _make_client()
    client.daily_tokens_used = 999
    with pytest.raises(TokenLimitExceededError) as excinfo:
        client._check_token_limit(2)
    assert excinfo.value.used == 1001
    assert excinfo.value.limit == 1000


def test_check_token_limit_resets_on_new_day(llm_env, monkeypatch):
    """跨天时自动清零累计用量。"""
    client = _make_client()
    client.daily_tokens_used = 500
    client.current_date = datetime(2020, 1, 1).date()

    class _Day:
        """可控日期替身。"""

        @staticmethod
        def today():
            """返回固定的"今天"。"""
            return datetime(2030, 1, 1).date()

    monkeypatch.setattr(llm_module, "date", _Day)
    client._check_token_limit(0)
    assert client.daily_tokens_used == 0
    assert client.current_date == datetime(2030, 1, 1).date()


# ---------------------------------------------------------------------------
# 用量记录
# ---------------------------------------------------------------------------


def test_record_token_usage_persists(llm_env, monkeypatch):
    """记录用量时更新内存累计并写入 LLMUsage。"""
    client = _make_client()
    session = _FakeSession()
    monkeypatch.setattr(llm_module, "get_session", lambda: session)

    client._record_token_usage(10, 5, module="unit")

    assert client.daily_tokens_used == 15
    assert session.committed == 1
    assert session.closed == 1
    assert len(session.added) == 1
    usage = session.added[0]
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 5
    assert usage.total_tokens == 15
    assert usage.module == "unit"
    assert usage.model == "test-model"
    assert usage.request_count == 1
    assert usage.date == date.today().isoformat()


def test_record_token_usage_tolerates_write_failure(llm_env, monkeypatch):
    """写库失败仅记录日志，内存计数照常更新，会话仍被关闭。"""
    client = _make_client()
    session = _FakeSession(fail_on_add=True)
    monkeypatch.setattr(llm_module, "get_session", lambda: session)

    client._record_token_usage(1, 2)  # 不应抛出
    assert client.daily_tokens_used == 3
    assert session.closed == 1


def test_record_token_usage_tolerates_session_failure(llm_env, monkeypatch):
    """获取会话失败也不影响调用方。"""
    client = _make_client()

    def _boom():
        """模拟取会话失败。"""
        raise RuntimeError("no db")

    monkeypatch.setattr(llm_module, "get_session", _boom)
    client._record_token_usage(4, 6)  # 不应抛出
    assert client.daily_tokens_used == 10


# ---------------------------------------------------------------------------
# chat_completion
# ---------------------------------------------------------------------------


def test_chat_completion_success_records_usage(llm_env):
    """成功返回响应体，并按 usage 字段记录用量。"""
    client = _make_client()
    payload = {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }
    fake = client.client
    fake.post_response = _FakeResponse(payload=payload)
    recorded: dict = {}
    client._record_token_usage = lambda **kw: recorded.update(kw)

    result = asyncio.run(client.chat_completion([{"role": "user", "content": "hello"}]))

    assert result is payload
    assert recorded == {"prompt_tokens": 3, "completion_tokens": 4, "module": "unknown"}
    assert fake.posts[0]["url"] == "/chat/completions"
    assert fake.posts[0]["json"]["model"] == "test-model"
    assert fake.posts[0]["json"]["stream"] is False


def test_chat_completion_without_usage_skips_recording(llm_env):
    """响应无 usage 字段时不记录用量。"""
    client = _make_client()
    payload = {"choices": [{"message": {"content": "hi"}}]}
    client.client.post_response = _FakeResponse(payload=payload)
    recorded: dict = {}
    client._record_token_usage = lambda **kw: recorded.update(kw)

    asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
    assert recorded == {}


def test_chat_completion_passes_module_kwarg(llm_env):
    """module 透传到用量记录。"""
    client = _make_client()
    payload = {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    client.client.post_response = _FakeResponse(payload=payload)
    recorded: dict = {}
    client._record_token_usage = lambda **kw: recorded.update(kw)

    asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}], module="sentiment"))
    assert recorded["module"] == "sentiment"


def test_chat_completion_http_error_becomes_llm_api_error(llm_env):
    """HTTP 状态异常统一转 LLMAPIError。"""
    client = _make_client()
    client.client.post_error = _http_status_error(500, "internal error")
    with pytest.raises(LLMAPIError) as excinfo:
        asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
    assert "API请求失败" in str(excinfo.value)


def test_chat_completion_model_not_found_gives_hint(llm_env):
    """model_not_found 给出可操作中文提示。"""
    client = _make_client()
    client.client.post_error = _http_status_error(404, "model_not_found")
    with pytest.raises(LLMAPIError) as excinfo:
        asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
    assert "模型不存在" in str(excinfo.value)


def test_chat_completion_unexpected_error_wrapped(llm_env):
    """非 HTTP 异常统一包成 LLMError。"""
    client = _make_client()
    client.client.post_error = ValueError("boom")
    with pytest.raises(LLMError) as excinfo:
        asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
    assert type(excinfo.value) is LLMError
    assert "请求失败" in str(excinfo.value)


def test_chat_completion_blocks_when_token_limit_exceeded(llm_env):
    """限额预检失败时不发起 HTTP 请求。"""
    client = _make_client()
    client.daily_tokens_used = 1000
    with pytest.raises(TokenLimitExceededError):
        asyncio.run(client.chat_completion([{"role": "user", "content": "aaaa"}]))
    assert client.client.posts == []


# ---------------------------------------------------------------------------
# chat_completion_stream
# ---------------------------------------------------------------------------


def test_chat_completion_stream_yields_delta_content(llm_env):
    """只 yield delta.content，跳过空 delta / 坏 JSON / DONE 之后的内容。"""
    client = _make_client()
    lines = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{}}]}',
        "data: not-json",
        "data: [DONE]",
        'data: {"choices":[{"delta":{"content":"after-done"}}]}',
    ]
    client.client.stream_response = _AsyncLineResponse(lines)

    async def _collect():
        """收集所有流式片段。"""
        chunks = []
        async for chunk in client.chat_completion_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)
        return chunks

    assert asyncio.run(_collect()) == ["Hel"]
    assert client.client.streams[0]["method"] == "POST"
    assert client.client.streams[0]["url"] == "/chat/completions"
    assert client.client.streams[0]["json"]["stream"] is True


def test_chat_completion_stream_http_error(llm_env):
    """建立流时的 HTTP 异常转 LLMAPIError。"""
    client = _make_client()
    client.client.stream_error = _http_status_error(500, "boom")

    async def _collect():
        """消费流（预期抛出）。"""
        async for _ in client.chat_completion_stream([{"role": "user", "content": "hi"}]):
            pass

    with pytest.raises(LLMAPIError):
        asyncio.run(_collect())


def test_chat_completion_stream_status_error_on_raise_for_status(llm_env):
    """响应 raise_for_status 失败同样转 LLMAPIError。"""
    client = _make_client()
    client.client.stream_response = _AsyncLineResponse(
        [], status_error=_http_status_error(429, "slow down")
    )

    async def _collect():
        """消费流（预期抛出）。"""
        async for _ in client.chat_completion_stream([{"role": "user", "content": "hi"}]):
            pass

    with pytest.raises(LLMAPIError) as excinfo:
        asyncio.run(_collect())
    assert "流式请求失败" in str(excinfo.value)


# ---------------------------------------------------------------------------
# simple_chat / batch_process
# ---------------------------------------------------------------------------


def test_simple_chat_builds_messages_with_and_without_system(llm_env):
    """system 存在时前置系统消息，不存在时只有用户消息。"""
    client = _make_client()
    captured = []

    async def _fake_completion(messages, **kwargs):
        """记录消息并返回固定回复。"""
        captured.append(messages)
        return {"choices": [{"message": {"content": "reply"}}]}

    client.chat_completion = _fake_completion

    assert asyncio.run(client.simple_chat("hello", system="sys")) == "reply"
    assert captured[0] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]

    asyncio.run(client.simple_chat("only-user"))
    assert captured[1] == [{"role": "user", "content": "only-user"}]


def test_simple_chat_invalid_response_raises(llm_env):
    """响应缺少 choices 时抛 LLMAPIError。"""
    client = _make_client()

    async def _bad(messages, **kwargs):
        """返回无 choices 的畸形响应。"""
        return {"choices": []}

    client.chat_completion = _bad
    with pytest.raises(LLMAPIError) as excinfo:
        asyncio.run(client.simple_chat("hi"))
    assert "格式错误" in str(excinfo.value)


def test_batch_process_isolates_single_failure(llm_env):
    """批内单条失败返回空串占位，顺序与总数保持不变。"""
    client = _make_client()
    seen = []

    async def _fake_chat(prompt, system=None, **kwargs):
        """记录提示词，bad 抛错其余返回大写。"""
        seen.append(prompt)
        if prompt == "bad":
            raise RuntimeError("boom")
        return f"R:{prompt}"

    client.simple_chat = _fake_chat
    results = asyncio.run(client.batch_process(["a", "bad", "c"], batch_size=2))

    assert results == ["R:a", "", "R:c"]
    assert len(seen) == 3


def test_batch_process_default_batch_size_from_config(llm_env):
    """未指定 batch_size 时使用配置值（此处为 2）。"""
    client = _make_client()

    async def _fake_chat(prompt, system=None, **kwargs):
        """将提示词转大写返回。"""
        return prompt.upper()

    client.simple_chat = _fake_chat
    assert asyncio.run(client.batch_process(["x", "y", "z"])) == ["X", "Y", "Z"]


# ---------------------------------------------------------------------------
# 用量查询与配置探测
# ---------------------------------------------------------------------------


def test_get_daily_usage_aggregates_db_records(llm_env, monkeypatch):
    """优先从数据库聚合当日用量并计算剩余与百分比。"""
    client = _make_client()
    session = _FakeSession(records=[_Rec(100, 1), _Rec(50, 2)])
    monkeypatch.setattr(llm_module, "get_session", lambda: session)

    usage = client.get_daily_usage()

    assert usage["total_tokens"] == 150
    assert usage["total_requests"] == 3
    assert usage["limit"] == 1000
    assert usage["remaining"] == 850
    assert usage["usage_percent"] == pytest.approx(15.0)
    assert usage["date"] == date.today().isoformat()
    assert session.closed == 1


def test_get_daily_usage_falls_back_to_memory(llm_env, monkeypatch):
    """数据库不可用时回退到内存计数。"""
    client = _make_client()
    client.daily_tokens_used = 42

    def _boom():
        """模拟取会话失败。"""
        raise RuntimeError("no db")

    monkeypatch.setattr(llm_module, "get_session", _boom)
    usage = client.get_daily_usage()

    assert usage["total_tokens"] == 42
    assert usage["remaining"] == 958
    assert usage["limit"] == 1000
    assert "total_requests" not in usage


def test_get_daily_usage_zero_limit_avoids_division_by_zero(llm_env, monkeypatch):
    """限额为 0 时百分比按 0 处理。"""
    client = _make_client()
    client.daily_token_limit = 0
    monkeypatch.setattr(llm_module, "get_session", lambda: _FakeSession(records=[]))

    usage = client.get_daily_usage()
    assert usage["usage_percent"] == 0
    assert usage["remaining"] == 0


def test_is_configured_reflects_credentials(llm_env):
    """密钥与地址齐全才算已配置。"""
    client = _make_client()
    assert client.is_configured() is True
    client.api_key = ""
    assert client.is_configured() is False


# ---------------------------------------------------------------------------
# 全局入口
# ---------------------------------------------------------------------------


def test_get_llm_client_is_lazy_singleton(llm_env):
    """首次调用创建，之后复用同一实例。"""
    first = llm_module.get_llm_client()
    second = llm_module.get_llm_client()
    assert first is second
    assert isinstance(first, llm_module.LLMClient)
    assert len(llm_env["created"]) == 1


def test_is_llm_available_true_when_configured(llm_env):
    """配置齐全时可用。"""
    assert llm_module.is_llm_available() is True


def test_is_llm_available_false_when_not_configured(monkeypatch):
    """未配置时返回 False 而非抛出。"""

    def _boom():
        """模拟未配置。"""
        raise LLMNotConfiguredError()

    monkeypatch.setattr(llm_module, "get_llm_client", _boom)
    assert llm_module.is_llm_available() is False
