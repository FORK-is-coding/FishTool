"""项目级日志查询、增量轮询与导出 API。"""
from __future__ import annotations

import asyncio
import base64
import heapq
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from core.exceptions import ValidationError
from core import logger as logger_module

router = APIRouter()

# 源码环境的默认目录；实际查询优先跟随当前 LoggerManager 的写入目录。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIR = PROJECT_ROOT / "data" / "logs"


def _get_log_files() -> Dict[str, Path]:
    """返回当前进程真实日志文件路径。

    LoggerManager 可能在 PyInstaller 环境中把日志写到 exe 旁边，不能依赖
    ``__file__`` 所在的临时解包目录。未初始化时才回退源码默认目录。

    Returns:
        日志类型到实际文件路径的映射。
    """
    manager = logger_module.logger_manager
    log_dir = Path(manager.log_dir) if manager is not None else DEFAULT_LOG_DIR
    return {
        "app": log_dir / "app.log",
        "error": log_dir / "error.log",
        "crawler": log_dir / "crawler.log",
        "risk": log_dir / "risk_control.log",
    }


# 保留只读兼容常量，外部诊断脚本仍可查看源码默认路径。
LOG_DIR = DEFAULT_LOG_DIR
LOG_FILES = _get_log_files()
# 合法日志级别集合，过滤与规范化共用。
LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
# 查询参数级别的别名表：前端与用户常用简写，参数侧必须在过滤前翻译成标准级别。
LEVEL_ALIASES = {"WARN": "WARNING", "ERR": "ERROR", "CRIT": "CRITICAL", "FATAL": "CRITICAL"}
# 「问题日志」视图（导出与排查）使用的级别集合。
PROBLEM_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})
# 清除控制台写入文件时残留的 ANSI 颜色转义序列。
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
TEXT_LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"\[(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\]\s+"
    r"(?:(?P<source>\S+?)(?:\s+\[[^\]]+\])?\s+-\s+)?(?P<message>.*)$"
)


def _serialize_text(value: object) -> str:
    """把任意日志字段稳定序列化为可展示文本。"""
    # 非字符串字段先尝试 JSON 序列化，失败则退化为 str()。
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError, ValidationError):
        return str(value)


def _normalize_level(value: object) -> str:
    """规范化**日志行**里的级别字段，未知级别按 INFO 展示。"""
    level = str(value or "INFO").upper()
    # 未知级别统一按 INFO 处理，避免前端渲染异常。
    return level if level in LEVELS else "INFO"


def _parse_levels_param(levels: Optional[str]) -> set[str]:
    """解析 ``levels`` 查询参数，返回实际生效的级别集合。

    与 :func:`_normalize_level` 的「行解析」语义刻意分开：行解析对未知值兜底成 INFO，
    但查询参数若照搬这套兜底，``levels=ERROR,WARN`` 会被折成 ``{INFO, ERROR}`` 并静默
    返回 INFO 行。参数侧因此做严格校验：支持 WARN/ERR/CRIT/FATAL 别名，其余未知值 422。

    Args:
        levels: 逗号分隔的级别参数；空值表示不过滤（全部级别）。

    Returns:
        set[str]: 标准级别集合。

    Raises:
        HTTPException: 422，携带未知级别与可选值列表。
    """
    items = [item.strip() for item in (levels or "").split(",") if item.strip()]
    if not items:
        # 不传或传空串时保持原有语义：返回全部级别。
        return set(LEVELS)
    parsed: set[str] = set()
    unknown: List[str] = []
    for item in items:
        token = item.upper()
        level = token if token in LEVELS else LEVEL_ALIASES.get(token)
        if level is None:
            unknown.append(item)
        else:
            parsed.add(level)
    if unknown:
        options = sorted(LEVELS | set(LEVEL_ALIASES))
        raise HTTPException(
            status_code=422,
            detail=f"未知日志级别: {', '.join(unknown)}；可选值: {', '.join(options)}",
        )
    return parsed


def _parse_json_log(line: str, source_hint: str) -> Optional[Dict[str, object]]:
    """解析结构化 JSON 日志并统一字段。"""
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    message: object = payload.get("message", "")
    # 兼容 message 字段里再包一层 JSON 的日志格式，取内层 message。
    try:
        nested = json.loads(message) if isinstance(message, str) else None
    except json.JSONDecodeError:
        nested = None
    if isinstance(nested, dict) and nested.get("message"):
        message = nested["message"]

    return {
        "timestamp": _serialize_text(payload.get("timestamp") or datetime.now().isoformat(timespec="seconds")),
        "level": _normalize_level(payload.get("level")),
        "source": _serialize_text(payload.get("logger") or payload.get("module") or source_hint),
        "message": _serialize_text(message),
        "is_rate_limit": source_hint == "risk_control",  # 风控来源标记，前端可据此高亮
    }


def _parse_text_log(line: str, source_hint: str) -> Optional[Dict[str, object]]:
    """解析项目文本日志格式并输出统一结构。"""
    # 控制台格式化器可能把 ANSI 颜色码写入文件，先清除转义序列再解析。
    normalized_line = ANSI_ESCAPE_PATTERN.sub("", line)
    match = TEXT_LOG_PATTERN.match(normalized_line)
    if not match:
        return None
    fields = match.groupdict()
    return {
        "timestamp": fields["timestamp"],
        "level": _normalize_level(fields["level"]),
        "source": fields.get("source") or source_hint,
        "message": fields.get("message") or "",
        "is_rate_limit": source_hint == "risk_control",
    }


def _parse_log_lines_with_offsets(
    lines: Iterable[Tuple[str, int]], source_hint: str,
) -> List[Tuple[Dict[str, object], int]]:
    """解析日志及每条记录末尾的字节偏移，堆栈续行随同一记录消费。"""
    records = []
    for raw_line, end_offset in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            if records:
                records[-1] = (records[-1][0], end_offset)
            continue
        record = _parse_json_log(line, source_hint) or _parse_text_log(line, source_hint)
        if record:
            records.append((record, end_offset))
        elif records:
            previous = records[-1][0]
            previous["message"] = f"{previous['message']}\n{line}"
            records[-1] = (previous, end_offset)
        else:
            records.append(({
                "timestamp": "时间未知",
                "level": "INFO",
                "source": source_hint,
                "message": _serialize_text(line),
                "is_rate_limit": source_hint == "risk_control",
            }, end_offset))
    return records


def parse_log_lines(lines: Iterable[str], source_hint: str) -> List[Dict[str, object]]:
    """解析日志行，并把异常堆栈续行合并到上一条消息。"""
    return [record for record, _ in _parse_log_lines_with_offsets(
        ((line, 0) for line in lines), source_hint
    )]


def _parse_log_bytes(
    content: bytes, source: str, start: int,
) -> List[Tuple[Dict[str, object], int]]:
    handle = io.BytesIO(content)
    return _parse_log_lines_with_offsets(
        ((line.decode("utf-8", errors="replace"), start + handle.tell()) for line in handle),
        source,
    )


def _selected_files(log_type: str) -> List[Tuple[str, Path]]:
    """按日志视图选择当前 LoggerManager 正在写入的文件。"""
    log_files = _get_log_files()
    # 全量视图只读 app + risk 两个主文件，error/crawler 的内容已含在其中。
    if log_type == "all":
        return [("app", log_files["app"]), ("risk_control", log_files["risk"])]
    if log_type not in log_files:
        raise ValidationError("log_type", f"无效的日志类型: {log_type}")
    source = "risk_control" if log_type == "risk" else log_type
    # 风控日志文件名为 risk，但来源标记统一为 risk_control。
    return [(source, log_files[log_type])]


def _encode_cursor(offsets: Dict[str, int]) -> str:
    """将文件偏移表编码为 URL 安全游标。"""
    raw = json.dumps(offsets, separators=(",", ":")).encode("utf-8")
    # URL 安全 Base64 编码，保证游标可直接放进查询参数。
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: Optional[str]) -> Dict[str, int]:
    """解析增量游标；非法游标交由接口返回 400。"""
    if not cursor:
        return {}
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        # 游标必须是 {文件路径: 字节偏移} 的字典结构。
        if not isinstance(payload, dict):
            raise ValidationError("cursor", "游标内容不是对象")
        return {str(key): max(int(value), 0) for key, value in payload.items()}
    except (ValueError, TypeError, ValidationError, json.JSONDecodeError, base64.binascii.Error) as exc:
        raise ValidationError("cursor", "无效的日志增量游标") from exc


def _read_records(
    log_type: str,
    cursor: Optional[str],
    limit: int,
    levels: Optional[set[str]] = None,
) -> Tuple[List[Dict[str, object]], str]:
    """读取首次尾部日志或游标之后的增量日志。

    Args:
        log_type: 日志视图（all/app/error/crawler/risk）。
        cursor: 增量游标；None 表示首次拉取。
        limit: 时间排序后最多返回的条数。
        levels: 需要保留的级别集合；None 表示不过滤。

    Returns:
        ``(records, next_cursor)``：按时间升序、已截断到 limit 的日志与下一个游标。
    """
    offsets = _decode_cursor(cursor)
    next_offsets: Dict[str, int] = {}
    records: List[Dict[str, object]] = []
    streams = []

    for source, path in _selected_files(log_type):
        key = str(path)
        if not path.exists():
            next_offsets[key] = 0
            continue
        start = offsets.get(key, 0) if cursor else 0
        try:
            with path.open("rb") as handle:
                file_size = os.fstat(handle.fileno()).st_size
                start = start if start <= file_size else 0
                handle.seek(start)
                content = handle.read(file_size - start)
            # 增量读取只消费完整行，避免把尚未写完的 UTF-8 字符或记录切断。
            if cursor:
                content = content[:content.rfind(b"\n") + 1]
            end = start + len(content)
            entries = _parse_log_bytes(content, source, start)
            pending = []
            position = start
            for record, record_end in entries:
                if levels is None or record["level"] in levels:
                    pending.append((record, position))
                position = record_end
            if cursor:
                streams.append((key, pending, end))
                next_offsets[key] = pending[0][1] if pending else end
            else:
                records.extend(record for record, _ in pending[-limit:])
                next_offsets[key] = end
        except OSError as exc:
            next_offsets[key] = start
            records.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "level": "ERROR",
                "source": "web.routers.logs",
                "message": f"读取日志文件失败 {path.name}: {exc}",
                "is_rate_limit": False,
            })

    if cursor:
        # 每个来源按文件顺序消费；只推进已返回记录及其后的过滤行，绝不越过待返回记录。
        queue = [(str(entries[0][0]["timestamp"]), i, 0)
                 for i, (_, entries, _) in enumerate(streams) if entries]
        heapq.heapify(queue)
        while queue and len(records) < limit:
            _, stream_index, index = heapq.heappop(queue)
            key, entries, end = streams[stream_index]
            records.append(entries[index][0])
            index += 1
            next_offsets[key] = entries[index][1] if index < len(entries) else end
            if index < len(entries):
                heapq.heappush(queue, (str(entries[index][0]["timestamp"]), stream_index, index))

    records.sort(key=lambda item: str(item["timestamp"]))
    return records[-limit:], _encode_cursor(next_offsets)


def _filter_records(
    records: Iterable[Dict[str, object]], levels: set[str], keyword: Optional[str]
) -> List[Dict[str, object]]:
    """按级别和关键词过滤结构化日志。"""
    keyword_lower = keyword.lower() if keyword else None
    # 关键词匹配来源与消息文本，忽略大小写。
    return [
        record for record in records
        if record["level"] in levels
        and (not keyword_lower or keyword_lower in f"{record['source']} {record['message']}".lower())
    ]


@router.get("/")
async def get_logs(
    log_type: str = "all",
    levels: str = "DEBUG,INFO,WARNING,ERROR",
    limit: int = Query(default=200, ge=1, le=1000),
    keyword: Optional[str] = None,
    cursor: Optional[str] = None,
):
    """返回结构化日志；传入 cursor 时只返回新增内容。"""
    # 参数侧严格解析级别（别名 + 422），与日志行解析的 INFO 兜底分开。
    requested_levels = _parse_levels_param(levels)
    # 读取日志是磁盘 IO，放到线程池避免阻塞事件循环；级别过滤下推到读取阶段，
    # 保证「先过滤再截断」，较早年月的问题日志不会被最近的低级别日志挤掉。
    try:
        records, next_cursor = await asyncio.to_thread(
            _read_records, log_type, cursor, limit, requested_levels
        )
    except (ValueError, ValidationError) as exc:
        # 非法游标直接 400，让前端重置游标重新拉取。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filtered = _filter_records(records, requested_levels, keyword)
    return {
        "success": True,
        "log_type": log_type,
        # 回显实际生效的级别，便于前端与排查时确认参数确实被识别。
        "levels": sorted(requested_levels),
        "records": filtered,
        "returned": len(filtered),
        "cursor": next_cursor,
    }


@router.get("/export")
async def export_problem_logs(log_type: str = "all", full: bool = False) -> PlainTextResponse:
    """导出日志。

    - 默认模式：导出 WARNING/ERROR/CRITICAL 问题日志为 UTF-8 文本附件。
    - full=true：复用 core.logger.LoggerManager.export_logs 全量合并导出
      （带类型分隔标题），避免与 logger 层重复实现。
    """
    # full 模式复用 logger 层导出能力，读取导出文件内容后以附件返回。
    if full:
        # log_type 会被 export_logs 拼成日志文件名，必须先按白名单校验，
        # 否则 ../ 或绝对路径可读取日志目录外的任意 .log 文件。
        if log_type != "all" and log_type not in _get_log_files():
            raise HTTPException(status_code=400, detail=f"无效的日志类型: {log_type}")
        if logger_module.logger_manager is None:
            # 单例未初始化时用默认配置初始化，保证导出可用。
            logger_module.init_logger()
        log_types = None if log_type == "all" else [log_type]
        export_path = Path(logger_module.logger_manager.export_logs(log_types=log_types))
        try:
            with open(export_path, "r", encoding="utf-8") as f:
                body = f.read()
        finally:
            # 导出文件只是中转，读完即删，避免每次导出都在日志目录残留 export_*.log
            export_path.unlink(missing_ok=True)
        filename = f"bili_ops_full_{datetime.now():%Y%m%d_%H%M%S}.txt"
        return PlainTextResponse(
            body,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    try:
        # 级别过滤在读取阶段完成，再按时间取最近 1000 条，避免「先截断再过滤」丢掉更早的问题日志。
        problems, _ = await asyncio.to_thread(
            _read_records, log_type, None, 1000, set(PROBLEM_LEVELS)
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    body = "\n".join(
        f"[{item['timestamp']}] [{item['level']}] [{item['source']}] {item['message']}"
        for item in problems
    ) or "暂无 WARNING / ERROR 日志"
    filename = f"bili_ops_errors_{datetime.now():%Y%m%d_%H%M%S}.txt"
    return PlainTextResponse(
        body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/types")
async def get_log_types():
    """返回日志来源视图列表。"""
    return {
        "success": True,
        "log_types": [
            {"value": "all", "label": "全项目日志"},
            {"value": "app", "label": "应用日志"},
            {"value": "error", "label": "错误日志"},
            {"value": "crawler", "label": "爬虫日志"},
            {"value": "risk", "label": "风控日志"},
        ],
    }