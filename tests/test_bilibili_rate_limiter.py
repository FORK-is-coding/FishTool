"""bilibili.rate_limiter 底座测试（第1批补齐）。

覆盖范围：
- RateLimiter.__init__ / _add_jitter / _refill_tokens / acquire
- RateLimiter.acquire_cookie_budget / report_429 / report_success
- RateLimiter._trigger_circuit_breaker / _reset_circuit / get_stats
- MultiEndpointRateLimiter.__init__ / get_limiter / acquire / report_429 / report_success
- 模块级 get_rate_limiter 单例与配置读取

测试策略：
- 全部为真实对象 + 真实算法，禁止用 AsyncMock 顶替异步方法。
- 只把 ``asyncio`` 替换为 shim（除 sleep 外全部代理真实模块），
  用于记录等待时长而不真正睡眠；shim 不是 MagicMock。
- 涉及风险日志（写文件）时做契约级替身，并在用例内注明"契约测试"。
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
import importlib
import time as _time

import pytest

import bilibili.rate_limiter as rate_limiter_module
from bilibili.rate_limiter import MultiEndpointRateLimiter, RateLimiter, get_rate_limiter
from core.exceptions import CircuitBreakerError


# 注意：core/__init__.py 里 `from .config import config` 会把包属性 core.config
# 覆盖成 ConfigManager 实例，因此必须用 importlib 显式取子模块对象。
CONFIG_MODULE = importlib.import_module("core.config")
LOGGER_MODULE = importlib.import_module("core.logger")


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class _AsyncioShim:
    """代理真实 asyncio，仅拦截 sleep 以记录等待时长。

    这是本轮明确要求的"支持异步协议的假对象"：除 sleep 外所有属性
    都转发给真实 asyncio 模块，避免 AsyncMock 破坏 ``await`` 语义。
    """

    def __init__(self, recorder: list) -> None:
        """记录等待时长的列表由调用方持有。"""
        self._recorder = recorder
        self._real = asyncio

    def __getattr__(self, name: str):
        """未拦截的属性全部转发真实 asyncio。"""
        return getattr(self._real, name)

    async def sleep(self, delay, *args, **kwargs):
        """记录延迟并立即返回，不真正睡眠。"""
        self._recorder.append(delay)


class _FakeRiskLogger:
    """契约测试替身：记录风控事件调用，不落盘。"""

    def __init__(self) -> None:
        """初始化三个事件通道的记录容器。"""
        self.events_429 = []
        self.cookie_expired = []
        self.circuit_breaks = []

    def log_429(self, endpoint, retry_after, count=1):
        """记录 429 事件参数。"""
        self.events_429.append({"endpoint": endpoint, "retry_after": retry_after, "count": count})

    def log_cookie_expired(self, cookie_name):
        """记录 Cookie 失效事件参数。"""
        self.cookie_expired.append(cookie_name)

    def log_circuit_break(self, reason):
        """记录熔断事件原因。"""
        self.circuit_breaks.append(reason)


class _FakeLoggerManager:
    """契约测试替身：仅暴露 risk_logger。"""

    def __init__(self, risk_logger) -> None:
        """注入风控日志替身。"""
        self.risk_logger = risk_logger


class _RecordingLimiter:
    """记录路由结果的限频器替身（用于多端点路由断言）。"""

    def __init__(self, retry_after: int = 77) -> None:
        """设置 report_429 的返回值。"""
        self.retry_after = retry_after
        self.acquired = []
        self.reports = []
        self.successes = 0

    async def acquire(self, endpoint):
        """记录端点。"""
        self.acquired.append(endpoint)

    def report_429(self, endpoint):
        """记录端点并返回预置等待时间。"""
        self.reports.append(endpoint)
        return self.retry_after

    def report_success(self):
        """累加成功计数。"""
        self.successes += 1


@pytest.fixture()
def sleeps(monkeypatch):
    """把 rate_limiter 内的 asyncio 换成记录型 shim，返回等待时长列表。"""
    recorder: list = []
    monkeypatch.setattr(rate_limiter_module, "asyncio", _AsyncioShim(recorder))
    return recorder


@pytest.fixture()
def risk_log(monkeypatch):
    """把全局 logger_manager 换成风控日志替身（源码在函数内惰性 import）。

    契约测试替身，不验真实落盘；返回记录器供断言事件载荷。
    """
    recorder = _FakeRiskLogger()
    monkeypatch.setattr(LOGGER_MODULE, "logger_manager", _FakeLoggerManager(recorder))
    return recorder


# ---------------------------------------------------------------------------
# RateLimiter.__init__ / _add_jitter / _refill_tokens
# ---------------------------------------------------------------------------


def test_rate_limiter_defaults_match_documented_policy():
    """默认参数、令牌桶初值与 Cookie 预算必须与文档一致。"""
    limiter = RateLimiter()

    assert limiter.rate == 2.0
    assert limiter.retry_delays == [30, 60, 120, 300, 600]
    assert limiter.max_429_count == 5
    assert limiter.jitter_factor == 0.2
    # 初始满令牌，保证启动后首个请求不被限
    assert limiter.tokens == 1.0
    assert limiter.max_tokens == 1.0
    assert limiter.status_429_count == 0
    assert limiter.last_429_time is None
    assert limiter.is_circuit_open is False
    assert limiter.circuit_open_time is None
    assert limiter.circuit_reset_timeout == timedelta(minutes=10)
    assert limiter.request_history.maxlen == 100
    assert limiter.cookie_budget_limit == 20
    assert limiter.cookie_budget_window == 60.0


def test_rate_limiter_honours_explicit_overrides():
    """显式传入的退避序列与阈值必须覆盖默认值。"""
    limiter = RateLimiter(rate=0.5, retry_delays=[1, 2], max_429_count=2, jitter_factor=0)

    assert limiter.rate == 0.5
    assert limiter.retry_delays == [1, 2]
    assert limiter.max_429_count == 2
    assert limiter.jitter_factor == 0


@pytest.mark.parametrize("factor", [0, -0.5])
def test_add_jitter_is_passthrough_when_factor_not_positive(factor):
    """抖动因子非正时必须原样返回基础延迟。"""
    limiter = RateLimiter(jitter_factor=factor)

    assert limiter._add_jitter(7.5) == 7.5


def test_add_jitter_stays_inside_configured_band_and_varies():
    """抖动延迟必须落在 ±factor 区间内，且不是恒定值。"""
    limiter = RateLimiter(jitter_factor=0.2)

    values = [limiter._add_jitter(10.0) for _ in range(200)]

    assert all(8.0 <= value <= 12.0 for value in values)
    assert len(set(values)) > 1


def test_refill_tokens_within_elapsed_time():
    """经过 rate 秒应恰好补充 1 个令牌。"""
    limiter = RateLimiter(rate=2.0)
    limiter.tokens = 0.0
    limiter.last_update = limiter.last_update - 2.0

    limiter._refill_tokens()

    assert limiter.tokens == pytest.approx(1.0, abs=1e-6)


def test_refill_tokens_caps_at_max_and_updates_timestamp():
    """长时间空闲后令牌不得溢出上限，并刷新 last_update。"""
    limiter = RateLimiter(rate=2.0)
    limiter.tokens = 0.0
    stale = limiter.last_update - 10_000.0
    limiter.last_update = stale

    limiter._refill_tokens()

    assert limiter.tokens == 1.0
    assert limiter.last_update > stale


# ---------------------------------------------------------------------------
# RateLimiter.acquire
# ---------------------------------------------------------------------------


def test_acquire_consumes_token_and_records_history():
    """令牌充足时应立即放行并写入请求历史。"""
    limiter = RateLimiter()

    asyncio.run(limiter.acquire("/api/normal"))

    assert limiter.tokens == pytest.approx(0.0, abs=1e-6)
    assert len(limiter.request_history) == 1
    entry = limiter.request_history[0]
    assert entry["endpoint"] == "/api/normal"
    assert isinstance(entry["timestamp"], datetime)


def test_acquire_waits_with_jitter_when_token_insufficient(sleeps):
    """令牌不足时应按 (1-tokens)*rate 计算基础等待并叠加抖动。"""
    limiter = RateLimiter(rate=4.0, jitter_factor=0.2)
    limiter.tokens = 0.0

    asyncio.run(limiter.acquire("/api/slow"))

    assert len(sleeps) == 1
    # 基础等待 (1-0)*4 = 4 秒，抖动区间 [3.2, 4.8]
    assert 3.2 <= sleeps[0] <= 4.8
    # 睡醒后令牌补满再消耗，最终归零
    assert limiter.tokens == pytest.approx(0.0, abs=1e-6)


def test_acquire_raises_while_circuit_open():
    """熔断窗口内 acquire 必须抛 CircuitBreakerError 且不记录请求。"""
    limiter = RateLimiter()
    limiter.is_circuit_open = True
    limiter.circuit_open_time = datetime.now()

    with pytest.raises(CircuitBreakerError) as excinfo:
        asyncio.run(limiter.acquire("/api/blocked"))

    assert "熔断保护中" in str(excinfo.value)
    assert len(limiter.request_history) == 0


def test_acquire_resets_circuit_after_timeout_and_proceeds():
    """熔断超时后应自动重置并恢复正常放行。"""
    limiter = RateLimiter()
    limiter.is_circuit_open = True
    limiter.circuit_open_time = datetime.now() - timedelta(minutes=11)
    limiter.status_429_count = 4

    asyncio.run(limiter.acquire("/api/recovered"))

    assert limiter.is_circuit_open is False
    assert limiter.circuit_open_time is None
    assert limiter.status_429_count == 0
    assert len(limiter.request_history) == 1


# ---------------------------------------------------------------------------
# RateLimiter.acquire_cookie_budget
# ---------------------------------------------------------------------------


def test_acquire_cookie_budget_appends_within_limit(sleeps):
    """预算未耗尽时直接入队，不产生等待。"""
    limiter = RateLimiter()

    asyncio.run(limiter.acquire_cookie_budget("hash-a", limit=3, window=60.0))

    history = limiter.cookie_request_history["charge:hash-a"]
    assert len(history) == 1
    assert sleeps == []


def test_acquire_cookie_budget_waits_when_window_full(sleeps):
    """同一 Cookie 达到窗口上限时应等待最早请求滑出窗口。"""
    limiter = RateLimiter()
    key = "charge:hash-b"

    now = _time.monotonic()
    limiter.cookie_request_history[key] = deque([now] * 3)

    asyncio.run(limiter.acquire_cookie_budget("hash-b", limit=3, window=60.0))

    # 触发滑动窗口等待，等待时长应为窗口剩余时间（0~60 秒之间）
    assert len(sleeps) == 1
    assert 0.0 <= sleeps[0] <= 60.0
    # 等待后仍会记录本次请求
    assert len(limiter.cookie_request_history[key]) == 4


def test_acquire_cookie_budget_isolates_cookies_and_prunes_expired(sleeps):
    """不同 Cookie 预算互相隔离，过期记录会被清理。"""
    limiter = RateLimiter()

    stale = _time.monotonic() - 600.0
    limiter.cookie_request_history["charge:old"] = deque([stale] * 30)

    asyncio.run(limiter.acquire_cookie_budget("old", limit=3, window=60.0))
    asyncio.run(limiter.acquire_cookie_budget("new", limit=3, window=60.0))

    # 过期记录被剔除，因此不会触发等待
    assert sleeps == []
    assert len(limiter.cookie_request_history["charge:old"]) == 1
    assert len(limiter.cookie_request_history["charge:new"]) == 1


def test_acquire_cookie_budget_clamps_limit_to_pool_budget(sleeps):
    """limit 超过 cookie_budget_limit 时以更小的内部上限为准。"""
    limiter = RateLimiter()
    limiter.cookie_budget_limit = 2

    now = _time.monotonic()
    limiter.cookie_request_history["charge:capped"] = deque([now, now])

    asyncio.run(limiter.acquire_cookie_budget("capped", limit=99, window=60.0))

    assert len(sleeps) == 1


# ---------------------------------------------------------------------------
# RateLimiter.report_429 / report_success
# ---------------------------------------------------------------------------


def test_report_429_follows_backoff_sequence_and_clamps(risk_log):
    """429 等待时间按序列递增，超出序列长度时取最后一位。"""
    limiter = RateLimiter(max_429_count=99)

    delays = [limiter.report_429("/api/x") for _ in range(7)]

    assert delays == [30, 60, 120, 300, 600, 600, 600]
    assert limiter.status_429_count == 7
    assert limiter.last_429_time is not None


def test_report_429_triggers_circuit_breaker_at_threshold(risk_log):
    """连续 429 达到阈值必须触发熔断并抛异常。"""
    limiter = RateLimiter(max_429_count=3)

    assert limiter.report_429("/api/a") == 30
    assert limiter.report_429("/api/b") == 60
    with pytest.raises(CircuitBreakerError) as excinfo:
        limiter.report_429("/api/c")

    assert limiter.is_circuit_open is True
    assert "连续3次遭遇429" in str(excinfo.value)
    assert risk_log.events_429[-1]["count"] == 3
    assert risk_log.circuit_breaks == ["连续3次遭遇429"]


def test_report_429_writes_risk_log_with_payload(risk_log):
    """契约测试，不验真实落盘：report_429 必须把端点/等待/次数交给风控日志。"""
    limiter = RateLimiter(max_429_count=99)

    limiter.report_429("/api/comment")

    assert risk_log.events_429 == [{"endpoint": "/api/comment", "retry_after": 30, "count": 1}]


def test_report_success_resets_counter_only_when_positive(risk_log):
    """有计数时清零并清空时间；计数为 0 时保持幂等。"""
    limiter = RateLimiter(max_429_count=99)
    limiter.report_429("/api/x")
    limiter.report_429("/api/y")

    limiter.report_success()

    assert limiter.status_429_count == 0
    assert limiter.last_429_time is None

    limiter.report_success()

    assert limiter.status_429_count == 0


# ---------------------------------------------------------------------------
# RateLimiter._trigger_circuit_breaker / _reset_circuit / get_stats
# ---------------------------------------------------------------------------


def test_trigger_circuit_breaker_sets_state_and_logs(risk_log):
    """契约测试，不验真实落盘：手动熔断必须置标志、记时间并写风控事件。"""
    limiter = RateLimiter()

    with pytest.raises(CircuitBreakerError) as excinfo:
        limiter._trigger_circuit_breaker("连续失败")

    assert limiter.is_circuit_open is True
    assert isinstance(limiter.circuit_open_time, datetime)
    assert "连续失败" in str(excinfo.value)
    assert risk_log.circuit_breaks == ["连续失败"]


def test_reset_circuit_clears_all_circuit_state(risk_log):
    """重置必须清空熔断标志、时间与 429 计数。"""
    limiter = RateLimiter(max_429_count=99)
    limiter.report_429("/api/x")
    limiter.is_circuit_open = True
    limiter.circuit_open_time = datetime.now()

    limiter._reset_circuit()

    assert limiter.is_circuit_open is False
    assert limiter.circuit_open_time is None
    assert limiter.status_429_count == 0


def test_get_stats_counts_only_last_minute_requests():
    """requests_last_minute 只统计 60 秒内的历史，total 统计全量。"""
    limiter = RateLimiter()
    limiter.tokens = 0.4
    limiter.request_history.append({"endpoint": "/recent", "timestamp": datetime.now()})
    limiter.request_history.append(
        {"endpoint": "/old", "timestamp": datetime.now() - timedelta(seconds=120)}
    )

    stats = limiter.get_stats()

    assert stats["rate"] == 2.0
    assert stats["tokens"] == 0.4
    assert stats["requests_last_minute"] == 1
    assert stats["total_requests"] == 2
    assert stats["is_circuit_open"] is False
    assert stats["status_429_count"] == 0


# ---------------------------------------------------------------------------
# MultiEndpointRateLimiter
# ---------------------------------------------------------------------------


def test_multi_endpoint_limiter_builds_three_default_limiters():
    """默认应为 normal/comment/dynamic 各建独立限频器并指定 normal 兜底。"""
    multi = MultiEndpointRateLimiter()

    assert set(multi.limiters) == {"normal", "comment", "dynamic"}
    assert multi.limiters["normal"].rate == 2.0
    assert multi.limiters["comment"].rate == 4.0
    assert multi.limiters["dynamic"].rate == 2.5
    assert multi.default_limiter is multi.limiters["normal"]
    # 每类必须是独立实例，不能共用同一把令牌桶
    assert len({id(item) for item in multi.limiters.values()}) == 3


def test_multi_endpoint_limiter_honours_custom_config():
    """自定义配置应覆盖默认速率与退避序列。"""
    multi = MultiEndpointRateLimiter(
        {"comment": {"rate": 9.0, "retry_delays": [1, 2], "max_429_count": 2}}
    )

    assert multi.limiters["comment"].rate == 9.0
    assert multi.limiters["comment"].retry_delays == [1, 2]
    assert multi.limiters["comment"].max_429_count == 2
    # 未配置 normal 时兜底为 None，get_limiter 返回 None 而非抛异常
    assert multi.default_limiter is None


def test_get_limiter_falls_back_to_normal_for_unknown_type():
    """未知端点类型回退到 normal 限频器。"""
    multi = MultiEndpointRateLimiter()

    assert multi.get_limiter("comment") is multi.limiters["comment"]
    assert multi.get_limiter("unknown") is multi.limiters["normal"]
    assert multi.get_limiter() is multi.limiters["normal"]


def test_multi_endpoint_acquire_routes_to_matching_limiter(sleeps):
    """acquire 必须按端点类型路由到对应限频器。"""
    multi = MultiEndpointRateLimiter()
    recorder = _RecordingLimiter()
    multi.limiters["comment"] = recorder

    asyncio.run(multi.acquire("/api/comment", "comment"))
    asyncio.run(multi.acquire("/api/other", "missing"))

    # 未识别类型回退 normal，不应打到 comment
    assert recorder.acquired == ["/api/comment"]
    assert multi.limiters["normal"].request_history[-1]["endpoint"] == "/api/other"


def test_multi_endpoint_report_429_and_success_route_to_type(risk_log):
    """report_429 / report_success 必须作用于目标端点类型。"""
    multi = MultiEndpointRateLimiter()
    recorder = _RecordingLimiter(retry_after=123)
    multi.limiters["comment"] = recorder

    retry_after = multi.report_429("/api/comment", "comment")
    multi.report_success("comment")
    multi.report_success("unknown")

    assert retry_after == 123
    assert recorder.reports == ["/api/comment"]
    assert recorder.successes == 1
    # unknown 落到 normal，不应污染 comment 计数
    assert multi.limiters["normal"].status_429_count == 0


def test_get_rate_limiter_is_lazy_singleton_reading_config(monkeypatch):
    """全局实例应惰性创建、读取配置并复用同一对象。"""
    monkeypatch.setattr(rate_limiter_module, "_global_rate_limiter", None)
    fake_values = {
        "bilibili.rate_limit.normal": 1.5,
        "bilibili.rate_limit.comment": 6.0,
        "bilibili.rate_limit.dynamic": 7.5,
        "bilibili.rate_limit.retry_429_delays": [5, 10],
        "bilibili.rate_limit.max_429_count": 2,
    }
    monkeypatch.setattr(
        CONFIG_MODULE.config,
        "get",
        lambda key, default=None: fake_values.get(key, default),
    )

    first = get_rate_limiter()
    second = get_rate_limiter()

    assert first is second
    assert first.limiters["normal"].rate == 1.5
    assert first.limiters["comment"].rate == 6.0
    assert first.limiters["dynamic"].rate == 7.5
    assert first.limiters["normal"].retry_delays == [5, 10]
    assert first.limiters["normal"].max_429_count == 2
