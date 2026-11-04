"""聊天指令的后台任务：去重占位、任务跟踪、异常兜底、取消。

长任务（舆情、自诊）不能在 handler 里同步等结果：先回复“已开始”，后台跑完再推送到发起的会话。
本模块不 import astrbot：发送函数和 logger 由 main.py 传入，便于单测。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .errors import CommandError

#: 推送文本的函数：``async (umo, text) -> bool``，返回是否发送成功。
SendText = Callable[[str, str], Awaitable[bool]]


def _close(coro) -> None:
    close = getattr(coro, "close", None)
    if close:
        close()


class JobRunner:
    """每个插件实例一个。``close()`` 之后不再接收新任务，也不再推送结果。"""

    def __init__(self, send_text: SendText, logger):
        self._send_text = send_text
        self.log = logger
        self._tasks: set[asyncio.Task] = set()
        self._running: set[str] = set()
        self._inner: dict[str, Awaitable] = {}
        self.closed = False

    # ------------------------------------------------------------------ claim
    def claim(self, key: str) -> bool:
        """同步占位：同一个 key 同时只允许一个任务。必须在 spawn 之前、同一事件循环轮次内调用。"""
        if self.closed or key in self._running:
            return False
        self._running.add(key)
        return True

    def release(self, key: str) -> None:
        self._running.discard(key)

    def is_running(self, key: str) -> bool:
        return key in self._running

    # ------------------------------------------------------------------ spawn
    def spawn(self, coro: Awaitable, *, name: str = "fishtool-job") -> asyncio.Task | None:
        """后台执行并跟踪任务；已关闭时直接关掉协程、返回 None。"""
        if self.closed:
            _close(coro)
            return None
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def start_text_job(self, key: str, umo: str, coro: Awaitable[str], *, name: str | None = None) -> asyncio.Task | None:
        """``spawn(run_text_job(...))`` 的便捷写法。

        内层协程登记在 ``_inner``：任务还没开始执行就被取消时，asyncio 不会运行
        ``run_text_job``，内层协程从未被 await；``close()`` 会把它们关掉，避免 "never awaited" 警告。
        """
        if self.closed:
            _close(coro)
            return None
        self._inner[key] = coro
        task = self.spawn(self.run_text_job(key, umo, coro), name=name or f"fishtool-{key}")
        if task is None:
            self._inner.pop(key, None)
        return task

    def _on_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # 正常情况下 run_text_job 已兜底；这里只是最后一道防线，避免 "Task exception was never retrieved"
            self.log.error(f"[fishtool] 后台任务 {task.get_name()} 异常退出: {exc!r}")

    async def run_text_job(self, key: str, umo: str, coro: Awaitable[str]) -> None:
        """执行一个返回文本的任务并推送结果。

        - CommandError：消息原样回复。
        - CancelledError：原样抛出（插件卸载时取消）。
        - 其他异常：记 error 日志（带 traceback），回复“内部错误”。
        - 无论成败都在 finally 里释放 key。
        """
        try:
            try:
                text = await coro
            except CommandError as exc:
                text = str(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务必须兜底，否则用户收不到任何回复
            self.log.error(f"[fishtool] {key} 执行失败: {exc}", exc_info=True)
            text = f"执行失败（内部错误），详情见 AstrBot 日志：{type(exc).__name__}"
        finally:
            self.release(key)
            self._inner.pop(key, None)
        if not self.closed:
            await self._send_text(umo, text)

    # ------------------------------------------------------------------ shutdown
    async def close(self) -> None:
        """取消所有后台任务并等待它们结束。terminate() 里必须在关闭 HTTP client 之前调用。"""
        self.closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    self.log.warning(f"[fishtool] 后台任务退出时出错: {result!r}")
        for coro in self._inner.values():
            _close(coro)  # 已经执行完或已在运行的协程 close() 是空操作
        self._inner.clear()
        self._tasks.clear()
        self._running.clear()

    @property
    def pending(self) -> int:
        return len(self._tasks)
