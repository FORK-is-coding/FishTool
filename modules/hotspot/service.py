"""热点业务服务：采集数据与算法解耦，业务层只调用 analyze。"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .algorithm import Detection, Snapshot, create_detector
from .algorithm.adapter import detection_to_dto


class HotspotService:
    """热点分析业务门面，不在业务层内联阶段判定逻辑。"""

    def __init__(self, algorithm_name: str = "lifecycle_v2", domain: str = "default") -> None:
        """创建热点服务并注入可插拔算法（默认 lifecycle_v2，与路由 Query 默认一致）。"""
        self.detector = create_detector(algorithm_name, domain=domain)

    def analyze(self, snapshots: Iterable[Snapshot]) -> list[dict[str, Any]]:
        """分析快照并输出展示 DTO。"""
        try:
            detections: list[Detection] = self.detector.detect(list(snapshots))
            return [detection_to_dto(item) for item in detections]
        except Exception as exc:
            raise RuntimeError(f"热点分析失败: {exc}") from exc
