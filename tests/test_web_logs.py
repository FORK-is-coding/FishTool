"""web.routers.logs 日志查询/增量轮询/导出接口测试（第4批 · web 段）。

覆盖对象：
- 纯函数：_normalize_level / _serialize_text / _parse_json_log / _parse_text_log /
  parse_log_lines / _encode_cursor / _decode_cursor / _selected_files /
  _filter_records / _read_records
- 端点：GET / 、GET /export 、GET /types

验证维度：
文本与 JSON 日志解析 / ANSI 清理 / 堆栈续行合并 / 游标往返与非法游标 / 文件选择 /
级别与关键词过滤 / 增量读取与尾部截断 / 导出问题日志 / 非法参数 400。

测试策略：
- 日志目录用 tmp_path 真实文件，断言真实 IO 行为。
- 端点用 fastapi.testclient.TestClient 挂载真实 router，并把 logger_manager 指向 tmp 目录。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.exceptions import ValidationError
from web.routers import logs as logs_module


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_normalize_level_uppercases_and_defaults_to_info():
    """已知级别大写化，未知或空值统一回落 INFO。"""
    assert logs_module._normalize_level("error") == "ERROR"
    assert logs_module._normalize_level("Weird") == "INFO"
    assert logs_module._normalize_level(None) == "INFO"


def test_serialize_text_passthrough_and_json():
    """字符串原样返回，非字符串做 JSON 序列化。"""
    assert logs_module._serialize_text("plain") == "plain"
    assert logs_module._serialize_text({"a": 1}) == '{"a": 1}'


def test_parse_json_log_extracts_and_unwraps_nested_message():
    """结构化 JSON 日志应统一字段，并解开 message 内嵌 JSON。"""
    line = (
        '{"timestamp": "2026-01-01 00:00:00", "level": "error", '
        '"logger": "web.app", "message": "{\\"message\\": \\"内层消息\\"}"}'
    )
    record = logs_module._parse_json_log(line, "app")
    assert record["level"] == "ERROR"
    assert record["source"] == "web.app"
    assert record["message"] == "内层消息"
    assert record["is_rate_limit"] is False


def test_parse_json_log_marks_risk_control_source():
    """风控来源应打上 is_rate_limit 标记。"""
    record = logs_module._parse_json_log('{"level": "INFO", "message": "x"}', "risk_control")
    assert record["is_rate_limit"] is True


def test_parse_json_log_rejects_non_object_and_invalid():
    """非对象或非法 JSON 应返回 None。"""
    assert logs_module._parse_json_log("[1, 2]", "app") is None
    assert logs_module._parse_json_log("not json", "app") is None


def test_parse_text_log_strips_ansi_and_extracts_fields():
    """文本日志应剥离 ANSI 颜色码并解析时间/级别/来源/消息。"""
    line = "\x1b[32m2026-01-02 03:04:05 [INFO] web.app - 启动完成\x1b[0m"
    record = logs_module._parse_text_log(line, "app")
    assert record["timestamp"] == "2026-01-02 03:04:05"
    assert record["level"] == "INFO"
    assert record["source"] == "web.app"
    assert record["message"] == "启动完成"


def test_parse_text_log_returns_none_for_unmatched():
    """不符合格式的行返回 None。"""
    assert logs_module._parse_text_log("随便一行文本", "app") is None


def test_parse_log_lines_merges_stacktrace_continuation():
    """无法解析的行应并入上一条消息，模拟异常堆栈。"""
    lines = [
        "2026-01-02 03:04:05 [ERROR] web.app - 出错了",
        "Traceback (most recent call last):",
        "  File \"x.py\", line 1",
    ]
    records = logs_module.parse_log_lines(lines, "app")
    assert len(records) == 1
    assert "Traceback" in records[0]["message"]
    assert "line 1" in records[0]["message"]


def test_parse_log_lines_keeps_leading_unparsed_line():
    """首行即无法解析时直接作为 INFO 记录，避免丢日志。"""
    records = logs_module.parse_log_lines(["孤立首行"], "app")
    assert records[0]["message"] == "孤立首行"
    assert records[0]["timestamp"] == "时间未知"


def test_parse_log_lines_skips_blank_lines():
    """空行应被跳过。"""
    assert logs_module.parse_log_lines(["", "   ".strip()], "app") == []


def test_cursor_roundtrip():
    """游标编解码应可逆。"""
    offsets = {"/a/app.log": 128, "/b/risk.log": 0}
    encoded = logs_module._encode_cursor(offsets)
    assert logs_module._decode_cursor(encoded) == offsets


def test_decode_cursor_none_returns_empty():
    """空游标解出空字典。"""
    assert logs_module._decode_cursor(None) == {}


def test_decode_cursor_rejects_non_object_payload():
    """游标内容不是对象应抛 ValidationError。"""
    import base64
    import json

    bad = base64.urlsafe_b64encode(json.dumps([1, 2]).encode()).decode()
    with pytest.raises(ValidationError):
        logs_module._decode_cursor(bad)


def test_decode_cursor_rejects_garbage():
    """非法 Base64 游标应抛 ValidationError。"""
    with pytest.raises(ValidationError):
        logs_module._decode_cursor("!!!not-base64!!!")


def test_filter_records_by_level_and_keyword():
    """过滤应同时应用级别集合与关键词（大小写无关）。"""
    records = [
        {"level": "INFO", "source": "a", "message": "hello"},
        {"level": "ERROR", "source": "b", "message": "BOOM"},
        {"level": "DEBUG", "source": "c", "message": "boom detail"},
    ]
    assert [r["level"] for r in logs_module._filter_records(records, {"ERROR"}, None)] == ["ERROR"]
    matched = logs_module._filter_records(records, {"INFO", "ERROR", "DEBUG"}, "boom")
    assert [r["level"] for r in matched] == ["ERROR", "DEBUG"]


# ---------------------------------------------------------------------------
# 文件选择
# ---------------------------------------------------------------------------


@pytest.fixture()
def log_env(tmp_path, monkeypatch):
    """把 logger_manager.log_dir 指向 tmp 目录，并返回目录路径。"""
    manager = SimpleNamespace(log_dir=tmp_path, export_logs=None)
    fake_module = SimpleNamespace(logger_manager=manager, init_logger=lambda: None)
    monkeypatch.setattr(logs_module, "logger_module", fake_module)
    return tmp_path


def test_selected_files_all_uses_app_and_risk(log_env):
    """all 视图应只读 app + 风控两个主文件。"""
    selected = logs_module._selected_files("all")
    sources = [source for source, _ in selected]
    assert sources == ["app", "risk_control"]


def test_selected_files_risk_maps_to_risk_control_source(log_env):
    """risk 视图文件名为 risk_control.log，但来源标记统一为 risk_control。"""
    selected = logs_module._selected_files("risk")
    assert selected == [("risk_control", log_env / "risk_control.log")]


def test_selected_files_invalid_type_raises(log_env):
    """未知日志类型应抛 ValidationError。"""
    with pytest.raises(ValidationError):
        logs_module._selected_files("nope")


# ---------------------------------------------------------------------------
# _read_records 增量读取
# ---------------------------------------------------------------------------


def test_read_records_first_call_returns_tail(log_env):
    """首次读取（无游标）应返回文件尾部 limit 条。"""
    (log_env / "app.log").write_text(
        "\n".join(
            f"2026-01-0{i} 00:00:00 [INFO] web.app - msg{i}" for i in range(1, 6)
        ),
        encoding="utf-8",
    )
    records, cursor = logs_module._read_records("app", None, 2)
    assert [r["message"] for r in records] == ["msg4", "msg5"]
    assert cursor


def test_read_records_incremental_only_returns_new(log_env):
    """带游标的二次读取只返回新增内容。"""
    path = log_env / "app.log"
    path.write_text("2026-01-01 00:00:00 [INFO] web.app - first\n", encoding="utf-8")
    _, cursor = logs_module._read_records("app", None, 10)

    path.write_text(
        "2026-01-01 00:00:00 [INFO] web.app - first\n"
        "2026-01-01 00:00:01 [INFO] web.app - second\n",
        encoding="utf-8",
    )
    records, _ = logs_module._read_records("app", cursor, 10)
    assert [r["message"] for r in records] == ["second"]


def test_read_records_rollback_cursor_when_file_shrinks(log_env):
    """文件被轮转变小时游标应回退到 0 重新读，而不是报错。"""
    path = log_env / "app.log"
    path.write_text(
        "2026-01-01 00:00:00 [INFO] web.app - longlonglonglong\n", encoding="utf-8"
    )
    _, cursor = logs_module._read_records("app", None, 10)

    path.write_text("2026-01-01 00:00:00 [INFO] web.app - a\n", encoding="utf-8")
    records, _ = logs_module._read_records("app", cursor, 10)
    assert [r["message"] for r in records] == ["a"]


def test_read_records_missing_file_is_skipped(log_env):
    """文件不存在时偏移归零且不产生记录。"""
    records, cursor = logs_module._read_records("app", None, 10)
    assert records == []
    assert cursor


def test_read_records_includes_error_record_on_oserror(log_env):
    """读取目录当作日志文件会触发 OSError，应生成 ERROR 记录而非崩溃。"""
    (log_env / "app.log").mkdir()
    records, _ = logs_module._read_records("app", None, 10)
    assert records[0]["level"] == "ERROR"
    assert "读取日志文件失败" in records[0]["message"]


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(log_env):
    """挂载 logs router 的测试客户端。"""
    app = FastAPI()
    app.include_router(logs_module.router, prefix="/api/logs")
    return TestClient(app)


def test_get_log_types_returns_views(client):
    """类型列表应含 all/app/error/crawler/risk 五项。"""
    body = client.get("/api/logs/types").json()
    assert [item["value"] for item in body["log_types"]] == ["all", "app", "error", "crawler", "risk"]


def test_get_logs_returns_records_and_cursor(client, log_env):
    """查询接口应返回结构化记录与后续游标。"""
    (log_env / "app.log").write_text(
        "2026-01-01 00:00:00 [WARNING] web.app - 警告\n"
        "2026-01-01 00:00:01 [ERROR] web.app - 失败\n",
        encoding="utf-8",
    )
    body = client.get("/api/logs/", params={"log_type": "app"}).json()
    assert body["success"] is True
    assert body["returned"] == 2
    assert {r["level"] for r in body["records"]} == {"WARNING", "ERROR"}
    assert body["cursor"]


def test_get_logs_filters_by_levels(client, log_env):
    """levels 参数应过滤返回级别。"""
    (log_env / "app.log").write_text(
        "2026-01-01 00:00:00 [INFO] web.app - 正常\n"
        "2026-01-01 00:00:01 [ERROR] web.app - 失败\n",
        encoding="utf-8",
    )
    body = client.get("/api/logs/", params={"log_type": "app", "levels": "ERROR"}).json()
    assert [r["level"] for r in body["records"]] == ["ERROR"]


def test_get_logs_invalid_type_returns_400(client):
    """非法 log_type 应返回 400。"""
    assert client.get("/api/logs/", params={"log_type": "unknown"}).status_code == 400


def test_get_logs_invalid_cursor_returns_400(client):
    """非法游标应返回 400，提示前端重置。"""
    response = client.get("/api/logs/", params={"log_type": "app", "cursor": "!!!bad!!!"})
    assert response.status_code == 400


def test_export_problem_logs_returns_attachment(client, log_env):
    """导出应只保留 WARNING/ERROR/CRITICAL 并作为附件返回。"""
    (log_env / "app.log").write_text(
        "2026-01-01 00:00:00 [INFO] web.app - 正常\n"
        "2026-01-01 00:00:01 [ERROR] web.app - 失败\n",
        encoding="utf-8",
    )
    response = client.get("/api/logs/export", params={"log_type": "app"})
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "[ERROR]" in response.text
    assert "[INFO]" not in response.text


def test_export_problem_logs_handles_empty(client, log_env):
    """无问题日志时应返回占位文案。"""
    (log_env / "app.log").write_text("2026-01-01 00:00:00 [INFO] web.app - 正常\n", encoding="utf-8")
    response = client.get("/api/logs/export", params={"log_type": "app"})
    assert response.status_code == 200
    assert "暂无 WARNING / ERROR 日志" in response.text


def test_export_full_mode_uses_logger_manager(client, log_env, tmp_path):
    """full=true 应复用 LoggerManager.export_logs 的内容。"""
    export_file = tmp_path / "full_export.txt"
    export_file.write_text("全量导出内容", encoding="utf-8")
    logs_module.logger_module.logger_manager.export_logs = lambda log_types=None: export_file

    response = client.get("/api/logs/export", params={"full": "true", "log_type": "all"})
    assert response.status_code == 200
    assert "全量导出内容" in response.text
    assert "bili_ops_full_" in response.headers["content-disposition"]


def test_export_invalid_type_returns_400(client):
    """非 full 模式下非法 log_type 应返回 400。"""
    response = client.get("/api/logs/export", params={"log_type": "bad"})
    assert response.status_code == 400
