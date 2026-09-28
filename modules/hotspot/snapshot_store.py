"""热点快照的统一写入内核（FishTool 03 · 批 2）。

设计要点（对齐 03 规格 §4.1）：

- **flush-only**：本模块只 ``add`` + ``flush`` + ``refresh``，**不** commit/rollback/close、
  不请求网络、不依赖 web 层；事务生命周期由 caller 持有。
- **绝不落伪 0**：七个统计字段逐项用 ``core.data_quality.parse_count`` 解析，
  缺失/非法写 SQL ``NULL``，真实 0 才写 0。
- **用 ``null()`` 绕过列默认 0**：显式 SQL NULL 表达式才能可靠覆盖 Column 的
  ``default=0``；直接传 Python ``None`` 不保证绕过。
- **三态质量**：``view_status`` 保留 ok/missing/invalid，``metric_status`` 逐字段保存；
  读端据此区分真实 0 与缺失。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from sqlalchemy import null

from core.data_quality import parse_count
from core.database import Video, VideoStats

#: 需要判定质量的统计字段：播放量与六项互动量同源返回，待遇必须一致。
STAT_KEYS = ("view", "danmaku", "reply", "favorite", "coin", "share", "like")


def _validate_captured_epoch_s(captured_epoch_s: Any) -> int:
    """校验采集时间戳为有效 UTC 秒级 epoch。

    Args:
        captured_epoch_s: 期望为 int 的秒级时间戳。

    Returns:
        校验通过的整数时间戳。

    Raises:
        ValueError: 非 int、bool 或负数时抛 ``'invalid_captured_epoch'``，
            不伪造当前时间。
    """
    if type(captured_epoch_s) is not int or captured_epoch_s < 0:
        raise ValueError('invalid_captured_epoch')
    return captured_epoch_s


def _validate_source(source: Any) -> str:
    """校验 source 非空字符串。

    Raises:
        ValueError: 缺失或空白时抛 ``'missing_source'``。
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError('missing_source')
    return source


def _validate_run_id(run_id: Any) -> Any:
    """校验 run_id：允许 None；否则必须为非空且长度 <= 64 的字符串。

    Raises:
        ValueError: 非法时抛 ``'invalid_run_id'``。
    """
    if run_id is None:
        return None
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 64:
        raise ValueError('invalid_run_id')
    return run_id


def persist_snapshot(
    session,
    view_data: Dict[str, Any],
    *,
    source: str,
    run_id: Any,
    captured_epoch_s: int,
    collection_tid: Any,
) -> VideoStats:
    """将一次 view 详情写入 ``videos`` / ``video_stats``，返回新建快照行。

    Args:
        session: 调用方持有的 SQLAlchemy 会话；本函数不结束其事务。
        view_data: B站 ``/x/web-interface/view`` 响应中的 data 字典。
        source: 采集来源标记（非空）。
        run_id: 采集批次标识，可为 None。
        captured_epoch_s: 响应实际到达时间的 UTC 秒级 epoch（int）。
        collection_tid: 用户选择的采集分区 ID；与接口原始 tid 区分。

    Returns:
        flush + refresh 后的 ``VideoStats`` 行；``view`` 等属性已解析为 int 或 None。

    Raises:
        ValueError: bvid 缺失、时间戳非法、source 为空或 run_id 非法时抛出稳定错误码。
    """
    bvid = str((view_data or {}).get("bvid") or "").strip()
    if not bvid:
        raise ValueError('missing_bvid')

    captured_epoch_s = _validate_captured_epoch_s(captured_epoch_s)
    source = _validate_source(source)
    run_id = _validate_run_id(run_id)

    stat = view_data.get("stat")
    stat = stat if isinstance(stat, dict) else {}

    # 逐字段严格解析，得到 (value, status) 二元组。
    parsed = {key: parse_count(stat.get(key)) for key in STAT_KEYS}
    values = {key: pair[0] for key, pair in parsed.items()}
    status = {key: pair[1] for key, pair in parsed.items()}

    video = session.query(Video).filter(Video.bvid == bvid).first()
    if video is None:
        video = Video(bvid=bvid, aid=view_data.get("aid"))
        session.add(video)
        session.flush()

    # 基础字段：保留原采集逻辑（None 时回退旧值，不覆盖已有信息）。
    owner = view_data.get("owner") or {}
    video.title = view_data.get("title") or video.title
    video.aid = view_data.get("aid") or video.aid
    video.desc = view_data.get("desc") or video.desc
    video.duration = view_data.get("duration") or video.duration
    video.mid = owner.get("mid") or video.mid
    video.author = owner.get("name") or video.author
    # 归属分区用采集入口 tid；接口原始 tid 另存快照，避免混同。
    video.tid = int(collection_tid) if collection_tid is not None else (view_data.get("tid") or video.tid)
    video.tname = view_data.get("tname") or video.tname

    # 发布时间保持既有本地显示兼容（不擅自改成 UTC naive）。
    pubdate_raw = view_data.get("pubdate")
    if pubdate_raw:
        try:
            video.pubdate = datetime.fromtimestamp(int(pubdate_raw))
        except (ValueError, TypeError, OSError):
            pass

    # 统计字段：缺失/非法写 SQL NULL，真实 0 才写 0。
    for key, value in values.items():
        setattr(video, key, value if value is not None else null())

    quality_count = sum(1 for state in status.values() if state == "ok")
    row = VideoStats(
        video_id=video.id,
        **{key: value if value is not None else null() for key, value in values.items()},
        snapshot_time=datetime.now(),  # 兼容既有本地显示字段
        source=source,
        run_id=run_id,
        captured_epoch_s=captured_epoch_s,
        collection_tid=int(collection_tid) if collection_tid is not None else None,
        raw_tid=parse_count(view_data.get("tid"))[0],
        view_status=status["view"],  # ok/missing/invalid 原样保留
        stat_status=(
            "ok" if quality_count == len(STAT_KEYS)
            else "missing" if quality_count == 0
            else "partial"
        ),
        metric_status=status,
    )
    session.add(row)
    session.flush()
    # 将 SQL NULL 表达式解析为 Python None，避免把 SQL 表达式对象交给 DTO。
    session.refresh(row)
    return row
