"""异常处理规范回归测试。

覆盖：
- 静态检查器不得发现静默宽泛捕获；
- toast 总线的注册/注销/去重/故障隔离；
- 低优先级只记日志、高优先级记日志并投递 toast。
"""
from __future__ import annotations

import logging

import pytest

from core.error_policy import (
    ErrorPriority,
    infer_priority,
    log_low_priority,
    report_exception,
    report_high_priority,
)
from core.exceptions import (
    AuthenticationError,
    ConfigNotFoundError,
    NetworkError,
    RecordNotFoundError,
)
from core.toast import (
    active_toast_sink_count,
    clear_toast_sinks,
    emit_toast,
    register_toast_sink,
    unregister_toast_sink,
)
from tools.check_exception_handling import find_violations


@pytest.fixture(autouse=True)
def _clean_sinks():
    """每个用例前后清空 toast sink，避免相互污染。"""
    clear_toast_sinks()
    yield
    clear_toast_sinks()


def test_no_silent_broad_except_in_repo():
    """仓库中不得存在裸 except 或静默吞掉的宽泛捕获。"""
    violations = find_violations()
    assert not violations, "\n".join(
        f"{item.file}:{item.line} [{item.kind}] {item.detail}" for item in violations
    )


def test_register_emit_and_unregister_sink():
    """注册的 sink 能收到 toast，注销后不再收到。"""
    received = []
    unregister = register_toast_sink(received.append)
    assert active_toast_sink_count() == 1

    assert emit_toast("标题", "正文", dedupe_window_s=0) == 1
    assert received[0].title == "标题"
    assert received[0].message == "正文"

    unregister()
    assert active_toast_sink_count() == 0
    assert emit_toast("标题", "正文", dedupe_window_s=0) == 0


def test_unregister_same_sink_twice_is_idempotent():
    """重复注销同一个 sink 不抛异常。"""
    sink = lambda _payload: None  # noqa: E731 - 测试用最小 sink
    register_toast_sink(sink)
    unregister_toast_sink(sink)
    unregister_toast_sink(sink)
    assert active_toast_sink_count() == 0


def test_sink_failure_does_not_block_other_sinks():
    """单个 sink 抛错时，其余 sink 仍收到消息。"""
    good = []
    register_toast_sink(lambda _payload: (_ for _ in ()).throw(RuntimeError("boom")))
    register_toast_sink(good.append)

    assert emit_toast("错误", "正文", dedupe_window_s=0) == 1
    assert len(good) == 1


def test_emit_deduplicates_within_window():
    """同一标题+正文在去重窗口内只投递一次。"""
    received = []
    register_toast_sink(received.append)

    assert emit_toast("重复", "正文", dedupe_window_s=60) == 1
    assert emit_toast("重复", "正文", dedupe_window_s=60) == 0
    assert len(received) == 1


def test_empty_message_is_ignored():
    """空正文不投递。"""
    received = []
    register_toast_sink(received.append)
    assert emit_toast("标题", "   ") == 0
    assert not received


def test_infer_priority_uses_exception_type():
    """认证/数据库类异常默认高优先级，网络类默认低优先级。"""
    assert infer_priority(AuthenticationError()) is ErrorPriority.HIGH
    assert infer_priority(NetworkError()) is ErrorPriority.LOW
    # 记录不存在是常见 404 分支，不因继承 DatabaseError 而弹窗。
    assert infer_priority(RecordNotFoundError("Video", "BV1")) is ErrorPriority.LOW


def test_exception_subclasses_keep_specific_codes():
    """子类必须能构造，且保留自己的错误码（修复父类拒绝 code 参数的回归）。"""
    config_error = ConfigNotFoundError("llm.api_key")
    record_error = RecordNotFoundError("Video", "BV1")
    assert config_error.code == "CONFIG_NOT_FOUND"
    assert record_error.code == "RECORD_NOT_FOUND"


def test_low_priority_logs_without_toast(caplog):
    """低优先级只写日志，不投递 toast。"""
    received = []
    register_toast_sink(received.append)
    logger = logging.getLogger("tests.error_policy.low")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        report = log_low_priority(ValueError("bad value"), logger=logger, action="解析输入")

    assert report.priority is ErrorPriority.LOW
    assert report.notified is False
    assert not received
    assert any("解析输入失败" in record.getMessage() for record in caplog.records)


def test_high_priority_logs_and_notifies(caplog):
    """高优先级写日志并投递 toast；异常消息直接给用户。"""
    received = []
    register_toast_sink(received.append)
    logger = logging.getLogger("tests.error_policy.high")

    with caplog.at_level(logging.ERROR, logger=logger.name):
        report = report_high_priority(
            AuthenticationError("登录态失效"),
            logger=logger,
            action="刷新登录态",
            source="test",
        )

    assert report.priority is ErrorPriority.HIGH
    assert report.notified is True
    assert len(received) == 1
    assert received[0].message == "登录态失效"
    assert received[0].source == "test"
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_high_priority_without_sink_does_not_raise():
    """没有 UI sink 时高优先级只记日志，不因通知失败抛异常。"""
    logger = logging.getLogger("tests.error_policy.no_sink")
    report = report_exception(
        RuntimeError("保存失败"),
        logger=logger,
        action="保存配置",
        priority=ErrorPriority.HIGH,
    )
    assert report.notified is False


def test_report_records_traceback_for_unexpected_error(caplog):
    """在 except 块内上报时，日志记录必须带异常堆栈。"""
    logger = logging.getLogger("tests.error_policy.traceback")
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        with caplog.at_level(logging.ERROR, logger=logger.name):
            report_exception(exc, logger=logger, action="执行任务", priority=ErrorPriority.LOW)

    records = [record for record in caplog.records if record.name == logger.name]
    assert records
    assert records[-1].exc_info is not None
