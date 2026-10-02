"""热点生命周期算法的统一数据模型与抽象接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Snapshot:
    """表示一个视频在某个采集时刻的统计快照。

    Args:
        bvid: 视频 BV 号。
        tid: B站分区 ID。
        captured_at: 采集时间。
        view: 播放量。
        title: 视频标题。
        owner_mid: UP 主 UID。
        owner_name: UP 主名称。
        source: 快照来源。
    """

    bvid: str
    tid: int
    captured_at: datetime
    view: int | None
    title: str = ""
    owner_mid: int = 0
    owner_name: str = ""
    source: str = "unknown"
    # ---- 03 读端质量/时间元数据（规格 §4.3 / §4.4）----
    # 由 routes_lifecycle 从 VideoStats 质量列读出；02 生命周期 adapter 消费这些
    # marker 做断段，03 只负责传递，不在此改动 02 算法。
    captured_epoch_s: int | None = None
    view_quality: str = "unknown"
    raw_view: int | None = None
    metric_status: dict[str, str] | None = None
    collection_tid: int | None = None
    raw_tid: int | None = None


@dataclass(frozen=True)
class Detection:
    """算法输出的统一热点检测结果，展示层只依赖此结构。

    ``metadata`` 为非数值项通道（如 ``coverage_state`` / ``confidence_kind``），
    与约定「仅数值或 None」的 ``metrics`` 分离，二者键集互斥。
    """

    bvid: str
    stage: str
    confidence: float
    metrics: dict[str, float | int | None] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)  # 非数值项通道（coverage_state / confidence_kind 等）
    explain: str = ""
    algorithm_version: str = ""
    title: str = ""
    tid: int = 0
    owner_mid: int = 0
    owner_name: str = ""


class LifecycleDetector(ABC):
    """所有生命周期算法实现必须遵守的唯一契约。"""

    @abstractmethod
    def detect(self, snapshots: list[Snapshot]) -> list[Detection]:
        """根据快照列表计算生命周期检测结果。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def version(self) -> str:
        """返回算法版本号。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def config_schema(self) -> dict[str, Any]:
        """返回可序列化的算法配置描述。"""
        raise NotImplementedError
