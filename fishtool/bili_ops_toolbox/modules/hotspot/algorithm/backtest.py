"""算法回测工具，纯内存计算，不触发网络采集。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .base import Detection, LifecycleDetector, Snapshot


@dataclass(frozen=True)
class BacktestReport:
    """回测统计结果。"""

    stage_hit_rate: float
    average_delay_days: float
    false_positive_rate: float
    samples: int


def backtest(detector: LifecycleDetector, cases: Iterable[tuple[list[Snapshot], str]]) -> BacktestReport:
    """根据历史热点事件标签统计命中率、延迟和误报率。

    Args:
        detector: 待评估算法。
        cases: (快照列表, 真实阶段) 元组迭代器。
    Returns:
        BacktestReport：阶段命中率、平均首次命中延迟、误报率和样本数。
    """
    total = hit = false_positive = 0
    delays: list[int] = []
    for snapshots, expected in cases:
        total += 1
        results = detector.detect(snapshots)
        actual = results[0] if results else None
        if actual is None:
            continue
        if actual.stage == expected:
            hit += 1
            delays.append(max(0, int(actual.metrics.get("days") or 0) - 1))
        elif actual.stage not in {"数据不足", "观察期"}:
            false_positive += 1
    return BacktestReport(stage_hit_rate=hit / total if total else 0.0, average_delay_days=sum(delays) / len(delays) if delays else 0.0, false_positive_rate=false_positive / total if total else 0.0, samples=total)
