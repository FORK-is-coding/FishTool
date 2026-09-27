"""采集快照和展示 DTO 的边界适配。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import Detection, Snapshot


def snapshot_from_mapping(data: dict[str, Any]) -> Snapshot:
    """将采集层字典转换为算法 Snapshot。"""
    captured = data.get("captured_at") or data.get("snapshot_time")
    if isinstance(captured, str):
        captured = datetime.fromisoformat(captured)
    if not isinstance(captured, datetime):
        captured = datetime.now()
    return Snapshot(bvid=str(data.get("bvid", "")), tid=int(data.get("tid") or 0), captured_at=captured, view=int(data.get("view") or 0), title=str(data.get("title") or ""), owner_mid=int(data.get("owner_mid") or data.get("mid") or 0), owner_name=str(data.get("owner_name") or data.get("author") or ""), source=str(data.get("source") or "unknown"))


def detection_to_dto(detection: Detection) -> dict[str, Any]:
    """将统一 Detection 转为稳定的 JSON 展示 DTO。"""
    return {"bvid": detection.bvid, "title": detection.title, "tid": detection.tid, "owner_mid": detection.owner_mid, "owner_name": detection.owner_name, "stage": detection.stage, "confidence": detection.confidence, "metrics": detection.metrics, "explain": detection.explain, "algorithm_version": detection.algorithm_version}
