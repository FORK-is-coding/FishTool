"""采集快照和展示 DTO 的边界适配。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.data_quality import parse_pubdate

from .base import ConfidenceKind, Detection, Snapshot


def snapshot_from_mapping(data: dict[str, Any]) -> Snapshot:
    """将采集层字典转换为算法 Snapshot。"""
    captured = data.get("captured_at") or data.get("snapshot_time")
    if isinstance(captured, str):
        captured = datetime.fromisoformat(captured)
    if not isinstance(captured, datetime):
        captured = datetime.now()
    pubdate_epoch_s, pubdate_status = parse_pubdate(data.get("pubdate", data.get("pubdate_epoch_s")))
    # 08 案 §J3 第 7 条（B6b）：**只接显式** first_seen_epoch_s；无值即 None，
    # 绝不拿 pubdate / captured_at / now 顶替——否则会把「旧视频」伪装成「刚发现 / 刚发布」。
    raw_first_seen = data.get("first_seen_epoch_s")
    first_seen_epoch_s = raw_first_seen if type(raw_first_seen) is int and raw_first_seen >= 0 else None
    return Snapshot(bvid=str(data.get("bvid", "")), tid=int(data.get("tid") or 0), captured_at=captured, view=int(data.get("view") or 0), title=str(data.get("title") or ""), owner_mid=int(data.get("owner_mid") or data.get("mid") or 0), owner_name=str(data.get("owner_name") or data.get("author") or ""), source=str(data.get("source") or "unknown"), pubdate_epoch_s=pubdate_epoch_s, pubdate_status=pubdate_status, first_seen_epoch_s=first_seen_epoch_s)


def _normalize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """metadata 通道浅拷贝；枚举值归一为 wire 小写字符串，其余原样。

    ``confidence_kind`` 以 :class:`ConfidenceKind` 给出时取 ``.value`` 落线，保证 DTO
    上出现的永远是 ``'not_estimated'`` 这种小写串，而不是随 Python 版本漂移的枚举 repr。
    """
    return {
        key: value.value if isinstance(value, ConfidenceKind) else value
        for key, value in dict(metadata or {}).items()
    }


def detection_to_dto(detection: Detection) -> dict[str, Any]:
    """将统一 Detection 转为稳定的 JSON 展示 DTO。

    metadata 为与仅数值 ``metrics`` 互斥的非数值项通道（confidence_kind / coverage_state）；
    v1 Detection 无该通道时其默认为 ``{}``，此处统一浅拷贝为 dict（恒非 None），保证 JSON
    可序列化，消费端无需额外 null 分支。
    """
    return {"bvid": detection.bvid, "title": detection.title, "tid": detection.tid, "owner_mid": detection.owner_mid, "owner_name": detection.owner_name, "stage": detection.stage, "confidence": detection.confidence, "metrics": detection.metrics, "metadata": _normalize_metadata(detection.metadata), "explain": detection.explain, "algorithm_version": detection.algorithm_version}
