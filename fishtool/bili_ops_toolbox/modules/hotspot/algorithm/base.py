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
    view: int
    title: str = ""
    owner_mid: int = 0
    owner_name: str = ""
    source: str = "unknown"


@dataclass(frozen=True)
class Detection:
    """算法输出的统一热点检测结果，展示层只依赖此结构。"""

    bvid: str
    stage: str
    confidence: float
    metrics: dict[str, float | int | None] = field(default_factory=dict)
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
