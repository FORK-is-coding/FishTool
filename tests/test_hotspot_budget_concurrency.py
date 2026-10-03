"""P2 · 请求预算：同步非阻塞 + 并发不互堵（FishTool 04 · R5 前置）。

被测：``modules/hotspot/risk_control.py::RequestBudget`` / ``BudgetDecision``。

覆盖点（逐条对齐派单 P2，本批唯一真正考验并发的一条）：
- ``try_acquire`` 是同步的：不加 await 直接调用即返回 ``BudgetDecision``；
- 准许时 ``_requests`` 长度 +1 且 ``category_consumption[kind]`` +1；拒绝时两者都不变；
- 被拒时 ``retry_at_mono > now_mono`` 且 ``reason_code ∈ {backoff, rate_limited}``；
  ``report_429`` 后 ``reason_code == "backoff"``；
- 并发核心：A 走 ``acquire()`` 被挂起等下一窗口时，
  * B 反复 ``try_acquire`` 必须立刻返回（响应延迟远小于 A 的等待时长），
  * A 睡眠期间锁未被持有（另一协程能进入临界区并跑完 ``try_acquire``，含准许路径）。

说明：仓库未安装 ``pytest-asyncio``，异步场景统一用 ``asyncio.run`` 驱动（与既有测试一致）。
"""
from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from modules.hotspot.risk_control import BudgetDecision, RequestBudget


def _run(coro):
    """用独立事件循环驱动协程（仓库未装 pytest-asyncio）。

    Args:
        coro: 待执行的协程对象。

    Returns:
        协程的返回值。
    """
    return asyncio.run(coro)


# --------------------------------------------------------------------------- 同步非阻塞


def test_try_acquire_is_synchronous_and_returns_decision() -> None:
    """``try_acquire`` 不是协程函数；直接调用返回 BudgetDecision（无需 await）。"""
    budget = RequestBudget(per_minute=5, per_hour=50, per_day=500)
    assert inspect.iscoroutinefunction(RequestBudget.try_acquire) is False

    decision = budget.try_acquire("general", 1_000.0)
    assert isinstance(decision, BudgetDecision)
    assert inspect.isawaitable(decision) is False
    assert decision.granted is True
    assert decision.retry_at_mono is None
    assert decision.reason_code is None


def test_grant_records_total_and_category() -> None:
    """准许：``_requests`` 长度 +1、``category_consumption[kind]`` +1。"""
    budget = RequestBudget(per_minute=2, per_hour=100, per_day=1000)
    assert budget.try_acquire("normal_watch", 1_000.0).granted is True
    assert budget.try_acquire("normal_watch", 1_001.0).granted is True

    assert len(budget._requests) == 2
    assert budget.category_consumption["normal_watch"] == 2


def test_deny_records_nothing_at_all() -> None:
    """拒绝：总额与该类别计数都原地不动（尤其不许凭空建类别 / 偷偷扣账）。"""
    budget = RequestBudget(per_minute=2, per_hour=100, per_day=1000)
    assert budget.try_acquire("normal_watch", 1_000.0).granted is True
    assert budget.try_acquire("normal_watch", 1_001.0).granted is True

    len_before = len(budget._requests)
    count_before = budget.category_consumption["normal_watch"]

    denied = budget.try_acquire("fast", 1_002.0)
    assert denied.granted is False
    assert len(budget._requests) == len_before
    assert budget.category_consumption["normal_watch"] == count_before
    # 拒绝不扣账，也不为未获准的类别新建计数条目。
    assert "fast" not in budget.category_consumption


def test_deny_reports_retry_and_rate_limited_reason() -> None:
    """被拒：``retry_at_mono > now_mono``，且无 429 时 reason_code == "rate_limited"。"""
    budget = RequestBudget(per_minute=2, per_hour=100, per_day=1000)
    assert budget.try_acquire("general", 1_000.0).granted is True
    assert budget.try_acquire("general", 1_001.0).granted is True

    denied = budget.try_acquire("general", 1_002.0)
    assert denied.granted is False
    assert denied.reason_code == "rate_limited"
    assert denied.reason_code in {"backoff", "rate_limited"}
    assert denied.retry_at_mono is not None
    assert denied.retry_at_mono > 1_002.0


def test_report_429_makes_reason_backoff() -> None:
    """``report_429`` 之后进入退避：被拒的 reason_code 必须是 "backoff"。"""
    budget = RequestBudget(per_minute=5, per_hour=100, per_day=1000)
    # 只占 1 个名额：避免撞上分钟窗口候选把 reason 覆盖成 rate_limited。
    assert budget.try_acquire("general", time.monotonic()).granted is True

    delay = budget.report_429(0)
    assert delay == 30.0  # 30s 起

    now = time.monotonic()
    denied = budget.try_acquire("general", now)
    assert denied.granted is False
    assert denied.reason_code == "backoff"
    assert denied.retry_at_mono is not None
    assert denied.retry_at_mono > now


# --------------------------------------------------------------------------- 并发核心


def test_concurrent_try_acquire_not_blocked_while_acquire_waits() -> None:
    """A 在 ``acquire()`` 里等下一窗口时，B 的 ``try_acquire`` 必须立刻返回。

    构造：把分钟预算打满，A 走 ``acquire()`` 被挂起等约 60s；
    此时 B 反复同步 ``try_acquire``，断言：
      - 单次响应延迟极小（远小于 A 的等待时长）；
      - 全部返回拒绝结论、未消费预算；
      - A 的睡眠期间锁未被持有（``_lock`` 可被非阻塞拿下）。
    """

    async def scenario() -> None:
        budget = RequestBudget(per_minute=1, per_hour=100, per_day=1000)

        # 1) 打满分钟预算：此后任何调用都只能等下一分钟窗口。
        t0 = time.monotonic()
        assert budget.try_acquire("general", t0).granted is True

        # 2) A 走 acquire()：会被挂起 await sleep(~60s)。
        waiter = asyncio.create_task(budget.acquire())
        await asyncio.sleep(0.01)  # 让 A 跑到它的 sleep
        assert waiter.done() is False, "A 应在等待下一窗口（不应立刻返回）"

        # 3) A 睡眠期间锁必须空闲：若 A 持锁 sleep，这里会拿不到。
        assert budget._lock.acquire(blocking=False) is True
        budget._lock.release()

        # 4) B：反复同步 try_acquire，必须立刻返回。
        latencies = []
        for _ in range(200):
            start = time.perf_counter()
            decision = budget.try_acquire("general", time.monotonic())
            latencies.append(time.perf_counter() - start)
            assert isinstance(decision, BudgetDecision)
            assert decision.granted is False
            assert decision.reason_code in {"backoff", "rate_limited"}

        max_latency = max(latencies)
        total_latency = sum(latencies)
        # B 单次响应应远小于 A 的 60s 等待（阈值取 50ms，实际微秒级）。
        assert max_latency < 0.05, f"B 单次 try_acquire 被拖慢: {max_latency:.6f}s"
        assert total_latency < 1.0, f"B 200 次总耗时异常: {total_latency:.6f}s"

        # B 全被拒，预算未被 B 消费。
        assert len(budget._requests) == 1
        # A 仍在等（远大于 B 的总耗时）。
        assert waiter.done() is False

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    _run(scenario())


def test_other_coroutine_can_enter_critical_section_while_acquire_waits() -> None:
    """A 睡眠期间，另一协程能跑完一次 ``try_acquire``，并走通「准许」路径。

    这里用显式 ``now_mono``（``try_acquire`` 的入参）模拟「下一窗口已到」，
    证明 A 的等待没有把临界区锁住：别的调用者仍可进入、记账并拿到 BudgetDecision。
    """

    async def scenario() -> None:
        budget = RequestBudget(per_minute=1, per_hour=100, per_day=1000)

        t0 = time.monotonic()
        assert budget.try_acquire("general", t0).granted is True

        waiter = asyncio.create_task(budget.acquire())
        await asyncio.sleep(0.01)
        assert waiter.done() is False

        # 另一协程进入临界区：窗口推进到 t0+61 后应获准（锁没被 A 占住）。
        start = time.perf_counter()
        decision = budget.try_acquire("general", t0 + 61.0)
        elapsed = time.perf_counter() - start

        assert decision.granted is True
        assert elapsed < 0.05
        assert len(budget._requests) == 2
        assert budget.category_consumption["general"] == 2

        # A 依旧在等（真实时钟还没到它的 retry 点），已被本次调用「跑完」所证明。
        assert waiter.done() is False

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    _run(scenario())
