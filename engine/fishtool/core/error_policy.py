"""统一异常处理策略：低优先级记日志，高优先级记日志并弹 toast。

项目内所有 ``except`` 分支按「错误是否会让用户当前操作失败」分两级：

- :attr:`ErrorPriority.LOW`：后台采集、单条解析、降级回退、清理失败等。
  只写日志（带上下文与可选堆栈），不打扰用户。
- :attr:`ErrorPriority.HIGH`：配置/数据库/认证/权限/任务失败等。
  写 ERROR 日志后，再通过 :func:`core.toast.emit_toast` 弹一次 toast。

典型用法::

    from core.error_policy import report_exception, ErrorPriority

    try:
        await save_config()
    except Exception as exc:
        report_exception(
            exc,
            logger=logger,
            action="保存配置",
            priority=ErrorPriority.HIGH,
            context={"path": config_path},
            source="web",
        )
        raise

约定：
1. 先决定「用户是否必须知道」；不确定时按 LOW 处理并写 warning/error。
2. 不要只写 ``str(exc)`` 而丢掉堆栈：非预期异常请传 ``include_traceback=True``。
3. 该模块只负责「记录 + 通知」，不负责吞异常；调用方决定是否 ``raise``。
4. toast 失败绝不影响主流程（由 :mod:`core.toast` 保证）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from core.exceptions import (
    AccountRiskError,
    AuthenticationError,
    BiliOpsException,
    CaptchaRequiredError,
    ConfigError,
    DatabaseError,
    IPBannedError,
    LLMNotConfiguredError,
    PermissionDeniedError,
    RecordNotFoundError,
    TaskError,
    TokenLimitExceededError,
)
from core.toast import emit_toast

#: 默认判定为「高优先级」的异常类型。
#:
#: 这些异常通常意味着用户当前操作无法完成，或需要用户介入（重新登录 / 改配置 /
#: 检查数据库），因此除日志外还需要 toast。限频、网络、解析等可重试/可降级错误
#: 不在此列，按低优先级处理。
HIGH_PRIORITY_ERROR_TYPES: tuple[type[BaseException], ...] = (
    AuthenticationError,
    ConfigError,
    DatabaseError,
    PermissionDeniedError,
    TokenLimitExceededError,
    LLMNotConfiguredError,
    TaskError,
    IPBannedError,
    AccountRiskError,
    CaptchaRequiredError,
)

#: toast 正文的默认长度上限，避免将整段堆栈或超长响应塞进界面。
_MAX_TOAST_MESSAGE_LEN = 240

#: 日志上下文值的长度上限，避免把整段 HTML / JSON 写进一行日志。
_MAX_CONTEXT_VALUE_LEN = 120


class ErrorPriority(str, Enum):
    """错误优先级。"""

    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True)
class ExceptionReport:
    """一次异常处理的标准化结果。

    Attributes:
        action: 业务动作描述（如「保存配置」）。
        message: 已拼好上下文的日志文案。
        priority: 本次采用的优先级。
        error_code: 稳定错误码（自定义异常取 ``code``，否则取异常类名）。
        error_type: 异常类名。
        notified: 是否已投递 toast。
    """

    action: str
    message: str
    priority: ErrorPriority
    error_code: str
    error_type: str
    notified: bool = False


def infer_priority(exc: BaseException) -> ErrorPriority:
    """根据异常类型推断优先级。

    Args:
        exc: 捕获到的异常。

    Returns:
        ErrorPriority: 命中 :data:`HIGH_PRIORITY_ERROR_TYPES` 时为 HIGH，否则 LOW。
    """
    # 「记录不存在」是常见的 404 业务分支，不应因为继承 DatabaseError 就弹窗。
    if isinstance(exc, RecordNotFoundError):
        return ErrorPriority.LOW
    return ErrorPriority.HIGH if isinstance(exc, HIGH_PRIORITY_ERROR_TYPES) else ErrorPriority.LOW


def error_code_for(exc: BaseException) -> str:
    """提取稳定错误码。

    Args:
        exc: 捕获到的异常。

    Returns:
        str: 自定义异常的 ``code``；标准异常使用类名，便于日志检索。
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()
    return type(exc).__name__


def _format_context(context: Optional[Mapping[str, Any]]) -> str:
    """把上下文字典格式化为单行 ``k=v, k=v``。

    Args:
        context: 调用方显式提供的排查信息；不传则不追加。

    Returns:
        str: 空字符串或 ``（k=v, k=v）`` 形式的后缀。
    """
    if not context:
        return ""
    parts = []
    for key, value in context.items():
        text = str(value).replace("\n", " ").replace("\r", " ").strip()
        if len(text) > _MAX_CONTEXT_VALUE_LEN:
            text = text[:_MAX_CONTEXT_VALUE_LEN] + "…"
        parts.append(f"{key}={text}")
    return "（" + ", ".join(parts) + "）" if parts else ""


def _build_log_message(action: str, exc: BaseException, context: Optional[Mapping[str, Any]]) -> str:
    """构造统一日志文案。

    Args:
        action: 业务动作描述。
        exc: 捕获到的异常。
        context: 额外上下文。

    Returns:
        str: ``动作失败: 异常信息（上下文）``。
    """
    prefix = f"{action}失败" if action else "操作失败"
    return f"{prefix}: {exc}{_format_context(context)}"


def _build_toast_message(
    action: str,
    exc: BaseException,
    toast_message: Optional[str],
) -> str:
    """构造用户可见的 toast 正文。

    Args:
        action: 业务动作描述。
        exc: 捕获到的异常。
        toast_message: 调用方显式指定的正文；优先使用。

    Returns:
        str: 截断后的正文。
    """
    if toast_message:
        text = str(toast_message)
    elif isinstance(exc, BiliOpsException) and str(exc.message or "").strip():
        # 自定义异常的消息面向用户，直接展示。
        text = str(exc.message)
    else:
        prefix = f"{action}失败" if action else "操作失败"
        text = f"{prefix}，详细原因已写入日志。"
    text = text.strip()
    if len(text) > _MAX_TOAST_MESSAGE_LEN:
        text = text[:_MAX_TOAST_MESSAGE_LEN] + "…"
    return text


def report_exception(
    exc: BaseException,
    *,
    logger: logging.Logger,
    action: str = "",
    priority: Optional[ErrorPriority] = None,
    context: Optional[Mapping[str, Any]] = None,
    toast_title: Optional[str] = None,
    toast_message: Optional[str] = None,
    source: str = "",
    include_traceback: bool = True,
    notify: bool = True,
    level: Optional[int] = None,
) -> ExceptionReport:
    """统一记录异常，并按优先级决定是否弹 toast。

    Args:
        exc: 捕获到的异常。
        logger: 当前模块的 logger（``get_logger(__name__)``）。
        action: 业务动作描述，用于日志与 toast 文案。
        priority: 显式优先级；不传则按异常类型推断。
        context: 额外排查上下文（如 bvid / path / uid），避免把敏感信息放进来。
        toast_title: toast 标题；默认「操作失败」。
        toast_message: toast 正文；默认使用自定义异常消息或通用提示。
        source: 产生位置，供 sink 分流（``web`` / ``desktop`` / ``monitor`` 等）。
        include_traceback: 是否记录堆栈。预期内的高频错误可传 False 降噪。
        notify: HIGH 优先级下是否真的投递 toast；测试或纯后台场景可关。
        level: 覆盖日志级别；默认 HIGH=ERROR、LOW=WARNING。

    Returns:
        ExceptionReport: 标准化处理结果，便于上层继续返回错误码。
    """
    resolved_priority = priority if isinstance(priority, ErrorPriority) else infer_priority(exc)
    message = _build_log_message(action, exc, context)
    code = error_code_for(exc)

    log_level = level
    if log_level is None:
        log_level = logging.ERROR if resolved_priority is ErrorPriority.HIGH else logging.WARNING

    if exc_info_available(exc) and include_traceback:
        # 直接传异常对象，确保记录的是传入的 exc，而不是调用栈里恰好存在的另一个异常。
        logger.log(log_level, "%s", message, exc_info=exc)
    else:
        logger.log(log_level, "%s", message)

    notified = False
    if resolved_priority is ErrorPriority.HIGH and notify:
        title = (toast_title or "操作失败").strip() or "操作失败"
        text = _build_toast_message(action, exc, toast_message)
        # emit_toast 自身保证不抛异常；这里仍显式判断投递数量。
        notified = emit_toast(
            title,
            text,
            level="error",
            source=source,
            error_code=code,
        ) > 0

    return ExceptionReport(
        action=action,
        message=message,
        priority=resolved_priority,
        error_code=code,
        error_type=type(exc).__name__,
        notified=notified,
    )


def log_low_priority(
    exc: BaseException,
    *,
    logger: logging.Logger,
    action: str = "",
    context: Optional[Mapping[str, Any]] = None,
    include_traceback: bool = True,
) -> ExceptionReport:
    """低优先级错误的便捷入口：只记日志，不弹 toast。

    Args:
        exc: 捕获到的异常。
        logger: 当前模块 logger。
        action: 业务动作描述。
        context: 额外排查上下文。
        include_traceback: 是否记录堆栈。

    Returns:
        ExceptionReport: 标准化处理结果。
    """
    return report_exception(
        exc,
        logger=logger,
        action=action,
        priority=ErrorPriority.LOW,
        context=context,
        include_traceback=include_traceback,
        notify=False,
    )


def report_high_priority(
    exc: BaseException,
    *,
    logger: logging.Logger,
    action: str = "",
    context: Optional[Mapping[str, Any]] = None,
    toast_title: Optional[str] = None,
    toast_message: Optional[str] = None,
    source: str = "",
    include_traceback: bool = True,
    notify: bool = True,
) -> ExceptionReport:
    """高优先级错误的便捷入口：记 ERROR 日志并弹 toast。

    Args:
        exc: 捕获到的异常。
        logger: 当前模块 logger。
        action: 业务动作描述。
        context: 额外排查上下文。
        toast_title: toast 标题。
        toast_message: toast 正文。
        source: 产生位置。
        include_traceback: 是否记录堆栈。
        notify: 是否真的投递 toast。

    Returns:
        ExceptionReport: 标准化处理结果。
    """
    return report_exception(
        exc,
        logger=logger,
        action=action,
        priority=ErrorPriority.HIGH,
        context=context,
        toast_title=toast_title,
        toast_message=toast_message,
        source=source,
        include_traceback=include_traceback,
        notify=notify,
    )


def exc_info_available(exc: BaseException) -> bool:
    """判断当前是否仍有可用的异常上下文。

    该函数存在的原因：logger 的 ``exc_info=True`` 在异常处理块之外调用时会退化为
    ``None``，日志里只出现 ``NoneType: None``。这里用 ``__traceback__`` 做一次显式
    判断，避免写出这种无效堆栈。

    Args:
        exc: 捕获到的异常。

    Returns:
        bool: 异常对象是否带 traceback。
    """
    return exc.__traceback__ is not None
