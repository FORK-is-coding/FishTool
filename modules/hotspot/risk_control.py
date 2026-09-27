"""热点请求预算和统一风控策略。"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timedelta


class RequestBudget:
    """按分钟、小时、天限制请求数量，超限排到下一可用窗口。"""

    def __init__(self, per_minute: int = 20, per_hour: int = 300, per_day: int = 3000) -> None:
        """初始化请求预算。"""
        self.limits = ((60.0, per_minute), (3600.0, per_hour), (86400.0, per_day))
        self._requests: deque[float] = deque()
        self._lock = asyncio.Lock()
        self.backoff_until = 0.0

    async def acquire(self) -> float:
        """等待可用预算并返回实际等待秒数。"""
        waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._requests and now - self._requests[0] >= 86400:
                    self._requests.popleft()
                delays = [window - (now - self._requests[-limit]) for window, limit in self.limits if len(self._requests) >= limit]
                delay = max([0.0, self.backoff_until - now, *delays])
                if delay <= 0:
                    self._requests.append(now)
                    return waited
                await asyncio.sleep(delay)
                waited += delay

    def report_429(self, attempt: int = 0) -> float:
        """登记 429 并返回 30 秒到 10 分钟封顶的指数退避秒数。"""
        delay = min(600.0, 30.0 * (2 ** max(0, attempt)))
        self.backoff_until = max(self.backoff_until, time.monotonic() + delay)
        return delay
