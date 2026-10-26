"""进程内 Toast 通知总线。

设计目标：把「高优先级错误需要让用户看见」与「错误产生的位置」解耦：

- 业务层只调用 :func:`emit_toast`，不关心当前是 Web 还是桌面壳；
- Web 层注册一个 sink，把 toast 通过 ``/ws`` 广播给浏览器与桌宠；
- 桌面壳注册一个 sink，把 toast 显示为系统托盘气泡；
- 没有 UI 的运行环境（脚本 / 测试 / CLI）只保留日志，不因缺少 sink 报错。

约定（与 ``core.error_policy`` 一致）：

- sink 必须是线程安全的：回调可能在任意线程被调用；
- sink 内部异常必须自行吞掉并记录，**绝不允许**反向影响业务主流程；
- toast 是「用户可见提示」，不是错误处理本身；调用方仍须先写日志。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List

# 使用标准库 logger 而不是 core.logger，避免 Toast 自身失败时形成递归依赖。
_logger = logging.getLogger(__name__)

#: 允许的提示级别；未知级别统一降级为 ``error``。
VALID_LEVELS = frozenset({"info", "warning", "error"})

#: 默认去重窗口（秒）。同一标题+正文在窗口内只投递一次，避免轮询失败刷屏。
DEFAULT_DEDUPE_WINDOW_S = 2.0


@dataclass(frozen=True)
class ToastMessage:
    """一条待展示的 toast 消息。

    Attributes:
        title: 标题（如「操作失败」）。
        message: 正文；空消息不会投递。
        level: ``info`` / ``warning`` / ``error``。
        source: 产生位置（``web`` / ``desktop`` / ``monitor`` 等），供 sink 分流。
        error_code: 稳定错误码，便于日志与前端按码处理。
        created_at: 创建时间戳（秒），供去重与前端排序。
    """

    title: str
    message: str
    level: str = "error"
    source: str = ""
    error_code: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, str]:
        """转换为可 JSON 序列化的字典。

        Returns:
            Dict[str, str]: WebSocket / 前端使用的负载。
        """
        return {
            "title": self.title,
            "message": self.message,
            "level": self.level,
            "source": self.source,
            "error_code": self.error_code,
        }


#: 已注册的 sink：用 ``id(sink)`` 作键，避免同名函数互相覆盖。
_sinks: Dict[int, Callable[[ToastMessage], None]] = {}
_sinks_lock = threading.RLock()

#: 最近一次投递时间：键为 ``(title, message, level)``。
_last_emit_at: Dict[tuple[str, str, str], float] = {}
_dedupe_lock = threading.Lock()


def register_toast_sink(sink: Callable[[ToastMessage], None]) -> Callable[[], None]:
    """注册 toast 接收端。

    Args:
        sink: 接收 :class:`ToastMessage` 的可调用对象；必须线程安全且不得抛异常。

    Returns:
        Callable[[], None]: 幂等的注销函数，便于在 lifespan / 窗口关闭时清理。

    Raises:
        TypeError: ``sink`` 不可调用。
    """
    if not callable(sink):
        raise TypeError("toast sink 必须可调用")
    with _sinks_lock:
        _sinks[id(sink)] = sink

    def _unregister() -> None:
        """注销当前 sink；重复调用无副作用。"""
        unregister_toast_sink(sink)

    return _unregister


def unregister_toast_sink(sink: Callable[[ToastMessage], None]) -> None:
    """注销 toast 接收端（不存在时静默忽略）。

    Args:
        sink: 注册时使用的同一个可调用对象。
    """
    with _sinks_lock:
        _sinks.pop(id(sink), None)


def clear_toast_sinks() -> None:
    """清空全部 sink（仅测试使用）。"""
    with _sinks_lock:
        _sinks.clear()
    with _dedupe_lock:
        _last_emit_at.clear()


def active_toast_sink_count() -> int:
    """返回当前已注册的 sink 数量（测试 / 诊断用）。

    Returns:
        int: sink 数量。
    """
    with _sinks_lock:
        return len(_sinks)


def emit_toast(
    title: str,
    message: str,
    *,
    level: str = "error",
    source: str = "",
    error_code: str = "",
    dedupe_window_s: float = DEFAULT_DEDUPE_WINDOW_S,
) -> int:
    """向所有已注册 sink 投递一条 toast。

    该函数保证不抛异常：单个 sink 失败只记日志并继续投递其余 sink。

    Args:
        title: 标题；空标题降级为「提示」。
        message: 正文；空正文直接忽略。
        level: ``info`` / ``warning`` / ``error``；未知值降级为 ``error``。
        source: 产生位置，供 sink 分流（如 ``web`` / ``desktop`` / ``monitor``）。
        error_code: 稳定错误码。
        dedupe_window_s: 去重窗口（秒）；``<= 0`` 表示不去重。

    Returns:
        int: 实际成功投递的 sink 数量；被去重或没有 sink 时为 0。
    """
    text = str(message or "").strip()
    if not text:
        return 0

    normalized_level = level if level in VALID_LEVELS else "error"
    normalized_title = str(title or "").strip() or "提示"

    # 先取 sink 快照：没有接收端时直接返回，不占用去重窗口。
    with _sinks_lock:
        sinks: List[Callable[[ToastMessage], None]] = list(_sinks.values())
    if not sinks:
        return 0

    if not _allow_emit(normalized_title, text, normalized_level, dedupe_window_s):
        return 0

    payload = ToastMessage(
        title=normalized_title,
        message=text,
        level=normalized_level,
        source=source,
        error_code=error_code,
    )

    delivered = 0
    for sink in sinks:
        try:
            sink(payload)
            delivered += 1
        except Exception:  # noqa: BLE001 - sink 失败绝不能反向影响业务主流程
            _logger.warning("toast sink 投递失败，已忽略: %r", sink, exc_info=True)
    return delivered


def _allow_emit(title: str, message: str, level: str, window_s: float) -> bool:
    """按「标题+正文+级别」做短窗口去重。

    Args:
        title: 标题。
        message: 正文。
        level: 级别。
        window_s: 去重窗口（秒）。

    Returns:
        bool: True 表示允许本次投递。
    """
    if window_s <= 0:
        return True
    key = (title, message, level)
    now = time.monotonic()
    with _dedupe_lock:
        last = _last_emit_at.get(key)
        if last is not None and now - last < window_s:
            return False
        _last_emit_at[key] = now
        # 防止长期运行时字典无限增长：只保留最近 256 个键。
        if len(_last_emit_at) > 256:
            cutoff = now - max(window_s, 1.0)
            stale = [item for item, ts in _last_emit_at.items() if ts < cutoff]
            for item in stale:
                _last_emit_at.pop(item, None)
    return True
