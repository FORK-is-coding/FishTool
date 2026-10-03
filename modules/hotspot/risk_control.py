"""热点请求预算和统一风控策略。

预算时钟一律用 ``time.monotonic()``（进程内单调时钟）；它**不是** UTC epoch，因此本模块
里的任何时刻都不得当 epoch 持久化，也不得把 ``retry_at_mono`` 当成 UTC 时间写盘。计数只
在内存里，**进程重启即清零**——不存在「跨重启累计硬上限」，要作那种承诺必须在外部另行
持久化计数之后才行。
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BudgetDecision:
    """一次「非阻塞预算尝试」的结果（不可变）。

    Attributes:
        granted: 是否获准。True 表示已原子记入总额与该类别消费。
        retry_at_mono: 被拒时的最早可重试时刻（``time.monotonic()`` 口径）；获准时为 None。
        reason_code: 被拒原因码（``backoff`` 表示 429 退避中，``rate_limited`` 表示撞上某条
            窗口限速）；获准时为 None。
    """

    granted: bool
    retry_at_mono: Optional[float] = None
    reason_code: Optional[str] = None


class RequestBudget:
    """按分钟、小时、天限制请求数量，超限排到下一可用窗口。

    - 同步入口 :meth:`try_acquire`：**不许** ``await`` / ``sleep``，也**不得**在持锁状态下
      等待预算——只做一次「立刻给结论」的检查。
    - 异步入口 :meth:`acquire`：旧调用点的兼容包装，内部循环调 :meth:`try_acquire`，拿到
      ``retry_at_mono`` 后**在锁外** ``await asyncio.sleep``。
    """

    def __init__(self, per_minute: int = 20, per_hour: int = 300, per_day: int = 3000) -> None:
        """初始化请求预算。

        Args:
            per_minute: 每分钟上限。
            per_hour: 每小时上限。
            per_day: 每天上限。
        """
        self.limits = ((60.0, per_minute), (3600.0, per_hour), (86400.0, per_day))
        self._requests: deque[float] = deque()
        # 临界区里没有任何 await，用同步短锁即可；asyncio.Lock 会逼出「持锁 sleep」的老问题。
        self._lock = threading.Lock()
        self.backoff_until = 0.0
        # 各类别消费计数（仅内存，进程重启清零）。准许才 +1，拒绝不扣。
        self.category_consumption: dict[str, int] = {}

    # ------------------------------------------------------------------ 同步非阻塞检查

    def try_acquire(self, kind: str, now_mono: float) -> BudgetDecision:
        """同步、非阻塞地尝试领取一次预算（**不得** await / sleep / 持锁等待）。

        Args:
            kind: 消费类别标签（如 ``general`` / ``normal_watch`` / ``fast``）。
            now_mono: 当前单调时钟读数（``time.monotonic()``）。

        Returns:
            BudgetDecision: 准许时已原子记入总额与 ``kind`` 消费；拒绝时两者都不扣，
            并给出最早可重试的单调时刻 ``retry_at_mono`` 与 ``reason_code``。
        """
        with self._lock:
            retry_at_mono, reason_code = self._plan_locked(now_mono)
            if retry_at_mono <= now_mono:
                # 准许：总额 + 该类别消费一起在锁内原子记入。
                self._requests.append(now_mono)
                self.category_consumption[kind] = self.category_consumption.get(kind, 0) + 1
                return BudgetDecision(granted=True)
            # 拒绝：不改 _requests、不改类别消费。
            return BudgetDecision(
                granted=False, retry_at_mono=retry_at_mono, reason_code=reason_code
            )

    def _plan_locked(self, now_mono: float) -> tuple[float, str]:
        """在持锁状态下算出「最早可放行时刻 + 原因码」（调用方必须已持锁）。

        Args:
            now_mono: 当前单调时钟读数。

        Returns:
            ``(retry_at_mono, reason_code)``；``retry_at_mono <= now_mono`` 表示可放行。
        """
        # 先过期清理：只保留最近 24h 的请求时间戳（天窗）。
        while self._requests and now_mono - self._requests[0] >= 86400:
            self._requests.popleft()
        earliest = self.backoff_until
        reason = "backoff"
        for window, limit in self.limits:
            if len(self._requests) >= limit:
                # 该窗口下，第 limit 个（从尾部数）请求的时间 + 窗口宽度即最早可再发时刻。
                candidate = self._requests[-limit] + window
                if candidate > earliest:
                    earliest = candidate
                    reason = "rate_limited"
        return earliest, reason

    # ------------------------------------------------------------------ 异步兼容入口

    async def acquire(self) -> float:
        """等待可用预算并返回实际等待秒数（保留无参调用兼容）。

        内部按 ``kind='general'`` 循环调 :meth:`try_acquire`；被拒时拿着 ``retry_at_mono``
        **在锁外** ``await asyncio.sleep``，因此一个调用者在等，其他调用者的
        :meth:`try_acquire` **不会**被堵在锁外。

        Returns:
            float: 本调用实际累计等待的秒数。
        """
        waited = 0.0
        while True:
            decision = self.try_acquire("general", time.monotonic())
            if decision.granted:
                return waited
            now = time.monotonic()
            retry_at = decision.retry_at_mono if decision.retry_at_mono is not None else now
            delay = retry_at - now
            if delay <= 0:
                # 理论不可达（retry_at 已过就会被准许）；兜底让出一次事件循环，避免忙等。
                await asyncio.sleep(0)
                continue
            # 锁此刻已经放开：在锁外睡，别的调用者可以照常 try_acquire。
            await asyncio.sleep(delay)
            waited += delay

    def report_429(self, attempt: int = 0) -> float:
        """登记 429 并返回 30 秒到 10 分钟封顶的指数退避秒数。

        Args:
            attempt: 第几次重试（0 起）；用于指数增长。

        Returns:
            float: 本次退避秒数（30s 起、600s 封顶）。
        """
        delay = min(600.0, 30.0 * (2 ** max(0, attempt)))
        # backoff_until 是单调时钟口径；只推进不后退，避免并发下退避被抹平。
        self.backoff_until = max(self.backoff_until, time.monotonic() + delay)
        return delay
