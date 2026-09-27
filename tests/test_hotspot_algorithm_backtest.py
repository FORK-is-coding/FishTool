"""热点算法回测工具的契约级测试。

覆盖 modules/hotspot/algorithm/backtest.py 的公开函数 backtest 及 BacktestReport：
- 空用例集 -> 全零报告
- 阶段命中 -> 命中率与首次命中延迟（days-1，向下不小于 0）
- 未命中且阶段非「数据不足/观察期」 -> 误报
- 未命中但属「数据不足/观察期」 -> 不算误报
- 算法无输出 -> 样本计入但既不命中也不误报

回测只做纯内存计算，用例全部使用真实现算法契约的桩对象，不触网、不落库。
"""

from __future__ import annotations

from modules.hotspot.algorithm.backtest import BacktestReport, backtest
from modules.hotspot.algorithm.base import Detection, LifecycleDetector, Snapshot


class _ScriptedDetector(LifecycleDetector):
    """按脚本顺序返回检测结果的桩算法，用于固定回测输入。"""

    def __init__(self, script: list[list[Detection]]) -> None:
        self._script = list(script)
        self.calls = 0

    def detect(self, snapshots: list[Snapshot]) -> list[Detection]:
        """依次弹出脚本中的结果，超出脚本范围时返回空列表。"""
        self.calls += 1
        if not self._script:
            return []
        return self._script.pop(0)

    @property
    def version(self) -> str:
        """返回桩算法版本号。"""
        return "scripted_v1"

    @property
    def config_schema(self) -> dict:
        """返回桩算法配置描述。"""
        return {"version": self.version}


def _hit(stage: str, days: int | None = 3) -> list[Detection]:
    """构造一条命中指定阶段、附带 days 指标的检测结果。"""
    metrics = {"days": days}
    return [Detection(bvid="BV1", stage=stage, confidence=0.5, metrics=metrics)]


def test_backtest_empty_cases_returns_zero_report() -> None:
    """空用例集应返回稳定的全零报告。"""
    report = backtest(_ScriptedDetector([]), [])

    assert report == BacktestReport(
        stage_hit_rate=0.0,
        average_delay_days=0.0,
        false_positive_rate=0.0,
        samples=0,
    )


def test_backtest_full_hit_computes_rate_and_delay() -> None:
    """全部命中时命中率为 1，延迟按 days-1 计算。"""
    detector = _ScriptedDetector([_hit("上升期", days=3), _hit("上升期", days=5)])
    cases = [([], "上升期"), ([], "上升期")]

    report = backtest(detector, cases)

    assert report.samples == 2
    assert report.stage_hit_rate == 1.0
    assert report.average_delay_days == 3.0  # (2 + 4) / 2
    assert report.false_positive_rate == 0.0


def test_backtest_delay_never_goes_negative() -> None:
    """days 缺省或为 0 时延迟应夹到 0，不出现负数。"""
    detector = _ScriptedDetector([_hit("上升期", days=0), _hit("上升期", days=None)])
    cases = [([], "上升期"), ([], "上升期")]

    report = backtest(detector, cases)

    assert report.average_delay_days == 0.0


def test_backtest_missing_days_metric_defaults_to_zero_delay() -> None:
    """metrics 不含 days 时按 0 处理。"""
    detector = _ScriptedDetector([[Detection(bvid="BV1", stage="成熟期", confidence=0.1)]])
    cases = [([], "成熟期")]

    report = backtest(detector, cases)

    assert report.stage_hit_rate == 1.0
    assert report.average_delay_days == 0.0


def test_backtest_insufficient_and_observation_not_false_positive() -> None:
    """「数据不足」「观察期」属未定性阶段，不计误报。"""
    detector = _ScriptedDetector([_hit("数据不足"), _hit("观察期")])
    cases = [([], "上升期"), ([], "上升期")]

    report = backtest(detector, cases)

    assert report.stage_hit_rate == 0.0
    assert report.false_positive_rate == 0.0


def test_backtest_wrong_stage_counts_as_false_positive() -> None:
    """阶段判定错误且已定性时计误报。"""
    detector = _ScriptedDetector([_hit("衰退期")])
    cases = [([], "上升期")]

    report = backtest(detector, cases)

    assert report.stage_hit_rate == 0.0
    assert report.false_positive_rate == 1.0


def test_backtest_empty_detections_neither_hit_nor_false_positive() -> None:
    """算法无输出时样本计入，但不命中也不误报。"""
    detector = _ScriptedDetector([[]])
    cases = [([], "上升期")]

    report = backtest(detector, cases)

    assert report.samples == 1
    assert report.stage_hit_rate == 0.0
    assert report.false_positive_rate == 0.0


def test_backtest_mixed_aggregates_across_cases() -> None:
    """混合命中/误报/缺省阶段的综合统计应逐项正确。"""
    detector = _ScriptedDetector([_hit("上升期"), _hit("衰退期"), _hit("观察期"), []])
    cases = [([], "上升期"), ([], "上升期"), ([], "上升期"), ([], "上升期")]

    report = backtest(detector, cases)

    assert report.samples == 4
    assert report.stage_hit_rate == 0.25
    assert report.false_positive_rate == 0.25
    assert detector.calls == 4


def test_backtest_consumes_iterable_cases_once() -> None:
    """cases 允许传迭代器，只消费一次即可完成回测。"""
    detector = _ScriptedDetector([_hit("上升期")])
    cases = iter([([], "上升期")])

    report = backtest(detector, cases)

    assert report.samples == 1
    assert report.stage_hit_rate == 1.0
