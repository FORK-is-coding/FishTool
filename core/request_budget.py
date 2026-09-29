"""请求尝试预算的通用计数（规格 §8.1）。

本模块只做通用计数，不依赖任何业务模块（不 import client / service / DB / Web）。
设计要点：

- ``AttemptBudget`` 用「发送前钩子」而不是「发送后记账」来计数，
  因此每次真实 HTTP 尝试都会在真正发出去之前被计入；
- ``deadline_monotonic`` 使用 ``time.monotonic``，不受系统时间回拨影响；
- ``current_attempt_budget`` 是 ``ContextVar``，**默认值为 ``None``**，
  因此上下文之外的旧调用行为完全不变（这是硬约束）；
- 同一事件循环内的 async child task 会继承同一 budget 对象，
  计数是同步的；不要把该对象跨线程共享。

典型用法::

    from core.request_budget import AttemptBudget, current_attempt_budget

    token = current_attempt_budget.set(AttemptBudget(max_attempts=3000, deadline_monotonic=...))
    try:
        ...
    finally:
        current_attempt_budget.reset(token)
"""
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic


class RequestBudgetExceeded(RuntimeError):
    """请求预算耗尽（超过 HTTP 尝试上限或已过 deadline）。

    该异常**不是**可重试网络异常：上层捕获后必须原样抛出，
    绝不能被转换成普通 APIError 后再次进入重试循环。
    """


@dataclass
class AttemptBudget:
    """一次排名任务允许的 HTTP 尝试预算。

    Attributes:
        max_attempts: 允许的真实 HTTP 尝试次数上限（含重试与签名密钥请求）。
        deadline_monotonic: 绝对截止时刻（``time.monotonic`` 口径）。
        attempts: 已消费的尝试次数（初始为 0）。
    """

    max_attempts: int
    deadline_monotonic: float
    attempts: int = 0

    def before_send(self):
        """在真正发送 HTTP 之前记账并检查预算。

        Returns:
            无。

        Raises:
            RequestBudgetExceeded: 已过 deadline（deadline_exceeded）或已达尝试上限
                （http_attempt_limit）时抛出。
        """
        # 先判 deadline：即使次数还没用满，超时也应立即停止。
        if monotonic() >= self.deadline_monotonic:
            raise RequestBudgetExceeded('deadline_exceeded')
        if self.attempts >= self.max_attempts:
            raise RequestBudgetExceeded('http_attempt_limit')
        self.attempts += 1


#: 当前协程任务生效的 HTTP 尝试预算；默认 None -> 旧调用不做任何计数。
current_attempt_budget: ContextVar[AttemptBudget | None] = ContextVar(
    'current_attempt_budget', default=None
)


def before_http_attempt():
    """HTTP 发送前钩子：有预算上下文时记账，否则完全不干预。

    Returns:
        无。

    Raises:
        RequestBudgetExceeded: 预算耗尽时由 ``AttemptBudget.before_send`` 抛出。
    """
    budget = current_attempt_budget.get()
    if budget is not None:
        budget.before_send()
