"""core.logger 底座测试（第1批补齐 · core 段）。

覆盖范围：
- LogLevel / RiskControlLevel 枚举
- ColoredFormatter.format
- StructuredFormatter._serialize / format
- RiskControlLogger.__init__ / log_event / log_429 / log_cookie_expired / log_circuit_break
- LoggerManager.__init__ / _setup_root_logger / get_logger / get_crawler_logger / export_logs
- 模块级 init_logger / get_logger

测试策略：
- 日志目录一律指向 tmp_path，不写仓库 data/logs。
- LoggerManager 会重配根 logger，测试用 fixture 快照并还原全局 logging 状态，
  避免污染其它用例。
- 风控日志用真实文件 + 真实 JSON 解析验证，不做字符串 Mock。
"""
from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime
from pathlib import Path

import pytest


logger_module = importlib.import_module("core.logger")


# ---------------------------------------------------------------------------
# 全局 logging 状态隔离
# ---------------------------------------------------------------------------


def _close_and_restore(target: logging.Logger, saved_handlers: list) -> None:
    """关闭目标 logger 当前 handler，并恢复测试前快照。"""
    for handler in target.handlers[:]:
        target.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    target.handlers[:] = saved_handlers


@pytest.fixture()
def isolated_logging():
    """快照根 / crawler / risk_control logger 状态，测试后完整还原。"""
    root = logging.getLogger()
    saved_root_handlers = root.handlers[:]
    saved_root_level = root.level
    saved_crawler = logging.getLogger("crawler").handlers[:]
    saved_risk = logging.getLogger("risk_control").handlers[:]
    saved_manager = logger_module.logger_manager
    logger_module.logger_manager = None
    try:
        yield
    finally:
        logger_module.logger_manager = saved_manager
        _close_and_restore(logging.getLogger("risk_control"), saved_risk)
        _close_and_restore(logging.getLogger("crawler"), saved_crawler)
        _close_and_restore(root, saved_root_handlers)
        root.setLevel(saved_root_level)


def _flush(target: logging.Logger) -> None:
    """把目标 logger 的所有 handler 立即刷盘，供断言读取。"""
    for handler in target.handlers:
        handler.flush()


# ---------------------------------------------------------------------------
# 枚举与格式化器
# ---------------------------------------------------------------------------


def test_log_level_enum_values():
    """标准日志级别枚举值正确，且可与字符串比较。"""
    assert logger_module.LogLevel.DEBUG.value == "DEBUG"
    assert logger_module.LogLevel.INFO.value == "INFO"
    assert logger_module.LogLevel.INFO == "INFO"
    assert logger_module.LogLevel.CRITICAL == "CRITICAL"


def test_risk_control_level_members():
    """风控级别枚举成员与取值齐全。"""
    names = {member.name for member in logger_module.RiskControlLevel}
    assert names == {
        "RATE_LIMIT", "STATUS_429", "COOKIE_EXPIRED", "IP_BANNED",
        "ACCOUNT_RISK", "CAPTCHA", "CIRCUIT_BREAK",
    }
    assert logger_module.RiskControlLevel.STATUS_429.value == "STATUS_429"


def test_colored_formatter_colors_known_level():
    """已知级别会被包上 ANSI 颜色码，消息体保持原样。"""
    formatter = logger_module.ColoredFormatter("%(levelname)s|%(message)s")
    record = logging.LogRecord("probe", logging.INFO, "file.py", 1, "hello", (), None)
    output = formatter.format(record)
    assert "\033[32mINFO\033[0m" in output
    assert output.endswith("hello")


def test_colored_formatter_leaves_unknown_level():
    """非标准级别不着色。"""
    formatter = logger_module.ColoredFormatter("%(levelname)s|%(message)s")
    record = logging.LogRecord("probe", 25, "file.py", 1, "msg", (), None)
    output = formatter.format(record)
    assert "Level 25" in output
    assert "\033[" not in output


def test_structured_formatter_serialize_types():
    """_serialize 对字符串、字典、None、自定义对象给出稳定文本。"""
    serialize = logger_module.StructuredFormatter._serialize
    assert serialize("text") == "text"
    assert json.loads(serialize({"a": 1})) == {"a": 1}
    assert serialize(None) == "null"

    class _Weird:
        def __repr__(self) -> str:
            return "<weird>"

    # 非基础类型经 default=str 序列化为 JSON 字符串
    assert json.loads(serialize(_Weird())) == "<weird>"


def test_structured_formatter_outputs_json_line():
    """StructuredFormatter 输出带基础字段与 extra 的 JSON 行。"""
    formatter = logger_module.StructuredFormatter()
    record = logging.LogRecord(
        "risk", logging.WARNING, "mod.py", 7, "hello %s", ("world",), None, func="myfunc"
    )
    record.extra_data = {"k": "v"}
    data = json.loads(formatter.format(record))
    assert data["level"] == "WARNING"
    assert data["logger"] == "risk"
    assert data["message"] == "hello world"
    assert data["module"] == "mod"
    assert data["function"] == "myfunc"
    assert data["line"] == 7
    assert data["extra"] == {"k": "v"}
    assert "timestamp" in data


def test_structured_formatter_includes_exception():
    """带 exc_info 的记录会附上异常堆栈。"""
    formatter = logger_module.StructuredFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("risk", logging.ERROR, "mod.py", 1, "err", (), sys.exc_info())
    data = json.loads(formatter.format(record))
    assert "ValueError" in data["exception"]
    assert "boom" in data["exception"]


# ---------------------------------------------------------------------------
# RiskControlLogger
# ---------------------------------------------------------------------------


def test_risk_logger_creates_dir_and_disables_propagation(isolated_logging, tmp_path):
    """构造风控日志器会创建目录，并禁止向上冒泡。"""
    risk = logger_module.RiskControlLogger(tmp_path / "logs")
    assert risk.risk_log_path.parent.exists()
    assert risk.logger.propagate is False


def test_risk_log_event_writes_structured_json(isolated_logging, tmp_path):
    """log_event 落盘为可解析的 JSON，字段完整。"""
    risk = logger_module.RiskControlLogger(tmp_path)
    risk.log_event(
        logger_module.RiskControlLevel.STATUS_429,
        "hit",
        endpoint="/x",
        status_code=429,
        retry_after=30,
        extra={"n": 1},
    )
    _flush(risk.logger)

    lines = [line for line in risk.risk_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["level"] == "WARNING"
    assert data["logger"] == "risk_control"

    event = data["extra"]
    assert event["risk_level"] == "STATUS_429"
    assert event["message"] == "hit"
    assert event["endpoint"] == "/x"
    assert event["status_code"] == 429
    assert event["retry_after"] == 30
    assert event["extra"] == {"n": 1}
    assert "timestamp" in event


def test_risk_logger_convenience_methods(isolated_logging, tmp_path):
    """log_429 / log_cookie_expired / log_circuit_break 各自写入正确事件。"""
    risk = logger_module.RiskControlLogger(tmp_path)
    risk.log_429("/api", 30, count=3)
    risk.log_cookie_expired("SESSDATA")
    risk.log_circuit_break("连续429")
    _flush(risk.logger)

    lines = [line for line in risk.risk_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3

    first = json.loads(lines[0])["extra"]
    assert first["risk_level"] == "STATUS_429"
    assert first["status_code"] == 429
    assert first["retry_after"] == 30
    assert first["endpoint"] == "/api"
    assert "第3次" in first["message"]

    second = json.loads(lines[1])["extra"]
    assert second["risk_level"] == "COOKIE_EXPIRED"
    assert second["extra"] == {"cookie": "SESSDATA"}

    third = json.loads(lines[2])["extra"]
    assert third["risk_level"] == "CIRCUIT_BREAK"
    assert third["extra"] == {"reason": "连续429"}


# ---------------------------------------------------------------------------
# LoggerManager
# ---------------------------------------------------------------------------


def test_logger_manager_setup_paths_and_handlers(isolated_logging, tmp_path):
    """LoggerManager 初始化目录、路径与根 handler（含 error.log 专用 handler）。"""
    log_dir = tmp_path / "logs"
    manager = logger_module.LoggerManager(log_dir=str(log_dir), log_level="DEBUG")
    assert manager.log_level == logging.DEBUG
    assert log_dir.exists()
    assert manager.app_log_path == log_dir / "app.log"
    assert manager.error_log_path == log_dir / "error.log"
    assert manager.crawler_log_path == log_dir / "crawler.log"
    assert manager.risk_logger is not None

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    basenames = [getattr(handler, "baseFilename", "") for handler in root.handlers]
    assert any(name.endswith("app.log") for name in basenames)
    assert any(name.endswith("error.log") for name in basenames)


def test_error_log_receives_only_errors(isolated_logging, tmp_path):
    """error.log 只收 ERROR+，app.log 收全量。"""
    log_dir = tmp_path / "logs"
    logger_module.LoggerManager(log_dir=str(log_dir), log_level="INFO")
    root = logging.getLogger()
    root.info("info-line-marker")
    root.error("error-line-marker")
    _flush(root)

    error_text = (log_dir / "error.log").read_text(encoding="utf-8")
    app_text = (log_dir / "app.log").read_text(encoding="utf-8")
    assert "error-line-marker" in error_text
    assert "info-line-marker" not in error_text
    assert "info-line-marker" in app_text
    assert "error-line-marker" in app_text


def test_get_logger_returns_named_logger(isolated_logging, tmp_path):
    """get_logger 返回标准库按名缓存的同名 logger。"""
    manager = logger_module.LoggerManager(log_dir=str(tmp_path / "logs"))
    named = manager.get_logger("demo.module")
    assert named is logging.getLogger("demo.module")
    assert named.name == "demo.module"


def test_crawler_logger_handler_added_only_once(isolated_logging, tmp_path):
    """get_crawler_logger 多次调用只添加一个文件 handler。"""
    manager = logger_module.LoggerManager(log_dir=str(tmp_path / "logs"))
    first = manager.get_crawler_logger()
    handler_count = len(first.handlers)
    second = manager.get_crawler_logger()
    assert second is first
    assert handler_count == 1
    assert len(second.handlers) == 1
    assert second.level == manager.log_level


def test_export_logs_merges_and_filters_by_date(isolated_logging, tmp_path):
    """export_logs 合并多类型日志，并按行首时间戳过滤。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "app.log").write_text(
        "2026-08-19 10:00:00 [INFO] keep-early\n"
        "2026-08-20 12:00:00 [INFO] keep-mid\n"
        "2026-08-21 09:00:00 [INFO] keep-late\n",
        encoding="utf-8",
    )
    (log_dir / "crawler.log").write_text("crawl-line-without-timestamp\n", encoding="utf-8")

    manager = logger_module.LoggerManager(log_dir=str(log_dir))
    output = manager.export_logs(
        start_date=datetime(2026, 8, 20),
        end_date=datetime(2026, 8, 20, 23, 59, 59),
        log_types=["app", "crawler"],
        output_path=str(tmp_path / "out.log"),
    )
    text = Path(output).read_text(encoding="utf-8")
    assert "日志类型: APP" in text
    assert "日志类型: CRAWLER" in text
    assert "keep-mid" in text
    assert "keep-early" not in text
    assert "keep-late" not in text
    # crawler 行无时间戳 → 解析失败保留该行
    assert "crawl-line-without-timestamp" in text


def test_export_logs_default_output_path(isolated_logging, tmp_path):
    """未指定 output_path 时导出到 log_dir 内。"""
    log_dir = tmp_path / "logs"
    manager = logger_module.LoggerManager(log_dir=str(log_dir))
    (log_dir / "app.log").write_text("2026-08-20 12:00:00 [INFO] default-out\n", encoding="utf-8")

    output = manager.export_logs(log_types=["app"])
    output_path = Path(output)
    assert output_path.exists()
    assert output_path.parent == log_dir
    assert "default-out" in output_path.read_text(encoding="utf-8")


def test_init_logger_and_module_get_logger_reuse(isolated_logging, tmp_path):
    """init_logger 建立全局单例，模块级 get_logger 复用同一管理器。"""
    manager = logger_module.init_logger(log_dir=str(tmp_path / "logs"))
    assert logger_module.logger_manager is manager
    assert logger_module.get_logger("reuse.module") is logging.getLogger("reuse.module")
    assert logger_module.logger_manager is manager
