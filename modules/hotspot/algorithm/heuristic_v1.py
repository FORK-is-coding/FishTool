"""启发式热点生命周期算法 v1。"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
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

    def _growth(self, rows: list[Snapshot]) -> tuple[list[float], list[float]]:
        """计算原始增速与按真实时间回看的平滑增速。

        Args:
            rows: 已按采集时间升序排列的快照。

        Returns:
            相邻快照原始增速，以及每个时点真实 ``window`` 日内的平均增速。
        """
        rates: list[float] = []
        for previous, current in zip(rows, rows[1:]):
            if previous.view <= 0:
                rates.append(0.0)
            else:
                rates.append((current.view - previous.view) / previous.view)

        smooth: list[float] = []
        window_delta = timedelta(days=self.config.window)
        for index, current in enumerate(rows[1:]):
            cutoff = current.captured_at - window_delta
            window_rates = [
                rate
                for rate_index, rate in enumerate(rates[: index + 1])
                if rows[rate_index + 1].captured_at >= cutoff
            ]
            smooth.append(statistics.fmean(window_rates))
        return rates, smooth

    @staticmethod
    def _decline_reason(rates: list[float]) -> str | None:
        """返回抗噪衰退依据，单次负增长不会触发衰退。

        Args:
            rates: 按时间升序排列的相邻快照增速。

        Returns:
            可展示的衰退依据；趋势不足时返回 ``None``。
        """
        recent = rates[-3:]
        if len(recent) >= 2 and all(rate <= -0.01 for rate in recent[-2:]):
            return "最近连续2个采集间隔均下降至少1%"
        if len(recent) >= 3 and sum(rate < 0 for rate in recent) >= 2:
            cumulative = math.prod(1.0 + rate for rate in recent) - 1.0
            if cumulative <= -0.05:
                return f"最近3个采集间隔累计回撤{abs(cumulative):.1%}"
        return None

    def _stage(
        self,
        rows: list[Snapshot],
        rates: list[float],
        smooth: list[float],
        percentile: float,
    ) -> tuple[str, str | None]:
        """根据趋势、阈值和同分区位置判定阶段并返回依据。"""
        if len(rows) < 2 or not rates or len(smooth) < 2:
            return "数据不足", None
        decline_reason = self._decline_reason(rates)
        first = smooth[-1] - smooth[-2]
        second = smooth[-1] - 2 * smooth[-2] + (smooth[-3] if len(smooth) >= 3 else smooth[-2])
        latest_view = rows[-1].view
        if decline_reason:
            return "衰退期", decline_reason
        if latest_view < self.config.base_view and percentile > self.config.p_appear:
            return "出现期", None
        if percentile > self.config.p_candidate and second > 0:
            return "上升期", None
        if second < 0 and percentile > 70:
            return "成熟期", None
        return "观察期", None

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
            stage, decline_reason = self._stage(rows, rates, smooth, percentile)
            first = smooth[-1] - smooth[-2] if len(smooth) >= 2 else None
            second = smooth[-1] - 2 * smooth[-2] + (smooth[-3] if len(smooth) >= 3 else smooth[-2]) if len(smooth) >= 2 else None
            latest = rows[-1]
            cutoff = latest.captured_at - timedelta(days=self.config.window)
            window_rows = [row for row in rows if row.captured_at >= cutoff]
            observed_days = len({row.captured_at.date() for row in window_rows})
            span_days = (
                (window_rows[-1].captured_at - window_rows[0].captured_at).total_seconds() / 86400
                if len(window_rows) >= 2
                else 0.0
            )
            # 置信度按真实观测跨度计算，密集采样不会伪装成多日覆盖。
            confidence = min(1.0, span_days / 30.0) * (
                0.65 if span_days < 7 else 0.85 if span_days < 30 else 1.0
            )
            metrics = {
                "growth": current_growth,
                "daily_growth": rates[-1] if rates else None,
                "first_derivative": first,
                "second_derivative": second,
                "percentile": percentile,
                # days 保留兼容，但语义明确为窗口内采集日数。
                "days": observed_days,
                "observed_days": observed_days,
                "window_span_days": round(span_days, 3),
                "window_days": self.config.window,
                "snapshot_count": len(window_rows),
                "up_count": len(up_ids_by_tid.get(rows[-1].tid, set())),
                "decline_reason": decline_reason,
            }
            if span_days >= self.config.window:
                window_text = f"最近{self.config.window}日窗口（共{observed_days}个采集日）"
            else:
                window_text = f"当前窗口实际覆盖{span_days:.2f}天（共{observed_days}个采集日）"
            reason_text = f"，衰退依据：{decline_reason}" if decline_reason else ""
            explain = (
                f"{window_text}播放增速位于同分区第{percentile:.0f}百分位，"
                f"当前为{stage}{reason_text}。"
            )
            detections.append(Detection(bvid=bvid, stage=stage, confidence=round(confidence, 4), metrics=metrics, explain=explain, algorithm_version=self.version, title=latest.title, tid=latest.tid, owner_mid=latest.owner_mid, owner_name=latest.owner_name))
        return sorted(detections, key=lambda item: (item.stage, -float(item.metrics.get("percentile") or 0)))
