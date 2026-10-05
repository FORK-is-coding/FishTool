"""热点业务服务：采集数据与算法解耦，业务层只调用 analyze。"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .algorithm import Detection, Snapshot, create_detector
from .algorithm.adapter import detection_to_dto


class HotspotService:
    """热点分析业务门面，不在业务层内联阶段判定逻辑。"""

    def __init__(
        self,
        algorithm_name: str = "lifecycle_v2",
        domain: str = "default",
        *,
        as_of_epoch_s: int | None = None,
    ) -> None:
        """创建热点服务并注入可插拔算法（默认 lifecycle_v2，与路由 Query 默认一致）。

        08 案 §H3：``as_of_epoch_s`` 是 keyword-only 的「本次评估截止」。只向支持该参数的
        ``lifecycle_v2`` 透传；v1 与外部插件工厂不认这个 kwarg，统一补传会误伤 registry。
        """
        kwargs: dict[str, Any] = {"domain": domain}
        if algorithm_name == "lifecycle_v2":
            kwargs["as_of_epoch_s"] = as_of_epoch_s
        self.detector = create_detector(algorithm_name, **kwargs)

    def analyze(self, snapshots: Iterable[Snapshot]) -> list[dict[str, Any]]:
        """分析快照并输出展示 DTO。"""
        try:
            detections: list[Detection] = self.detector.detect(list(snapshots))
            return [detection_to_dto(item) for item in detections]
        except Exception as exc:
            raise RuntimeError(f"热点分析失败: {exc}") from exc
