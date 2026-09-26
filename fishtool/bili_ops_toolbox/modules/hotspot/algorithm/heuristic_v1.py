"""启发式热点生命周期算法 v1。"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Iterable

from .base import Detection, LifecycleDetector, Snapshot
from .config import HeuristicConfig, get_config


class HeuristicV1(LifecycleDetector):
    """使用7日平滑增速、导数和同分区百分位判定生命周期。"""

    def __init__(self, domain: str = "default", config: HeuristicConfig | None = None) -> None:
        """初始化算法。

        Args:
            domain: 领域名称，用于选择阈值桶。
            config: 可选的显式配置，便于回测和实验。
        """
        self.domain = domain
        self.config = config or get_config(domain)

    @property
    def version(self) -> str:
        """返回当前算法版本。"""
        return "heuristic_v1"

    @property
    def config_schema(self) -> dict[str, object]:
        """返回前端可展示的算法配置。"""
        return {"version": self.version, "domain": self.domain, **self.config.as_dict()}

    @staticmethod
    def _percentile(value: float, values: list[float]) -> float:
        """计算包含当前值的百分位，范围为0到100。"""
        if not values:
            return 0.0
        return 100.0 * sum(item <= value for item in values) / len(values)

    @staticmethod
    def _growth(rows: list[Snapshot]) -> tuple[list[float], list[float]]:
        """计算原始日增速与滑动平均增速。"""
        rates: list[float] = []
        for previous, current in zip(rows, rows[1:]):
            if previous.view <= 0:
                rates.append(0.0)
            else:
                rates.append((current.view - previous.view) / previous.view)
        smooth = [statistics.fmean(rates[max(0, index - 6): index + 1]) for index in range(len(rates))]
        return rates, smooth

    def _stage(self, rows: list[Snapshot], rates: list[float], smooth: list[float], percentile: float) -> str:
        """根据规则优先级判定阶段，负原始日增速优先判衰退。"""
        if len(rows) < 2 or not rates:
            return "数据不足"
        latest_raw = rates[-1]
        # 该优先级用于避免平滑值掩盖最新真实下跌。
        if latest_raw < 0:
            return "衰退期"
        if len(smooth) < 2:
            return "数据不足"
        first = smooth[-1] - smooth[-2]
        second = smooth[-1] - 2 * smooth[-2] + (smooth[-3] if len(smooth) >= 3 else smooth[-2])
        latest_view = rows[-1].view
        if latest_view < self.config.base_view and percentile > self.config.p_appear:
            return "出现期"
        if percentile > self.config.p_candidate and second > 0:
            return "上升期"
        if second < 0 and percentile > 70:
            return "成熟期"
        if percentile < self.config.p_decline or latest_raw < 0:
            return "衰退期"
        return "观察期"

    def detect(self, snapshots: list[Snapshot]) -> list[Detection]:
        """按 BV 号分析快照并返回统一 Detection 列表。"""
        grouped: dict[str, list[Snapshot]] = defaultdict(list)
        for snapshot in snapshots:
            if snapshot.bvid:
                grouped[snapshot.bvid].append(snapshot)
        rows_by_video: dict[str, list[Snapshot]] = {}
        growth_by_video: dict[str, tuple[list[float], list[float]]] = {}
        for bvid, rows in grouped.items():
            rows.sort(key=lambda row: row.captured_at)
            rows_by_video[bvid] = rows
            growth_by_video[bvid] = self._growth(rows)
        all_growth_by_tid: dict[int, list[float]] = defaultdict(list)
        for bvid, (_, smooth) in growth_by_video.items():
            if smooth:
                all_growth_by_tid[rows_by_video[bvid][-1].tid].append(smooth[-1])
        # 统计每个 tid 下参与视频的唯一 UP 主数量，用于判断模仿潮扩散程度。
        # UP 数越多说明该热点正被多人跟进，上升期信号越强；账号关联在展示层消费此值。
        up_ids_by_tid: dict[int, set[int]] = defaultdict(set)
        for bvid, rows in rows_by_video.items():
            for row in rows:
                if row.tid and row.owner_mid:
                    up_ids_by_tid[row.tid].add(row.owner_mid)
        detections: list[Detection] = []
        for bvid, rows in rows_by_video.items():
            rates, smooth = growth_by_video[bvid]
            current_growth = smooth[-1] if smooth else None
            same_tid_growth = all_growth_by_tid.get(rows[-1].tid, [])
            percentile = self._percentile(current_growth, same_tid_growth) if current_growth is not None else 0.0
            stage = self._stage(rows, rates, smooth, percentile)
            first = smooth[-1] - smooth[-2] if len(smooth) >= 2 else None
            second = smooth[-1] - 2 * smooth[-2] + (smooth[-3] if len(smooth) >= 3 else smooth[-2]) if len(smooth) >= 2 else None
            days = len({row.captured_at.date() for row in rows})
            # 数据积累不足时只给低置信度，避免展示层误读阶段结论。
            confidence = min(1.0, days / 30.0) * (0.65 if days < 7 else 0.85 if days < 30 else 1.0)
            latest = rows[-1]
            metrics = {"growth": current_growth, "daily_growth": rates[-1] if rates else None, "first_derivative": first, "second_derivative": second, "percentile": percentile, "days": days, "up_count": len(up_ids_by_tid.get(rows[-1].tid, set()))}
            explain = f"近{days}天播放增速位于同分区第{percentile:.0f}百分位，当前为{stage}。"
            detections.append(Detection(bvid=bvid, stage=stage, confidence=round(confidence, 4), metrics=metrics, explain=explain, algorithm_version=self.version, title=latest.title, tid=latest.tid, owner_mid=latest.owner_mid, owner_name=latest.owner_name))
        return sorted(detections, key=lambda item: (item.stage, -float(item.metrics.get("percentile") or 0)))
