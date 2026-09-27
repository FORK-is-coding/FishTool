"""热点生命周期算法回归测试。"""
from datetime import datetime, timedelta

from modules.hotspot.algorithm import HeuristicV1, Snapshot


def make_rows(bvid: str, views: list[int], tid: int = 4) -> list[Snapshot]:
    """构造连续日快照。"""
    start = datetime(2026, 1, 1)
    return [Snapshot(bvid=bvid, tid=tid, captured_at=start + timedelta(days=index), view=view, title=bvid) for index, view in enumerate(views)]


def test_single_negative_growth_does_not_flip_to_decline():
    """单次负增长属于噪声，不得直接把生命周期翻转为衰退。"""
    rows = make_rows("BV1", [1000, 2000, 4000, 8000, 16000, 32000, 64000, 60000])
    result = HeuristicV1().detect(rows)[0]
    assert result.stage != "衰退期"
    assert result.metrics["decline_reason"] is None


def test_sustained_negative_growth_has_explainable_decline_reason():
    """连续显著下降时应判衰退，并返回可解释的趋势依据。"""
    rows = make_rows("BV1", [1000, 1500, 2200, 3000, 2800, 2500])
    result = HeuristicV1().detect(rows)[0]
    assert result.stage == "衰退期"
    assert "连续2个采集间隔" in result.metrics["decline_reason"]
    assert "衰退依据" in result.explain


def test_insufficient_samples_and_confidence():
    """少于两天返回数据不足，七天内置信度受压低。"""
    one_day = HeuristicV1().detect(make_rows("BV1", [1000]))[0]
    assert one_day.stage == "数据不足"
    assert one_day.confidence < 0.25


def test_time_window_uses_timestamp_span_instead_of_snapshot_count():
    """密集的七个快照只能代表实际小时跨度，不能冒充近七日。"""
    start = datetime(2026, 1, 1)
    rows = [
        Snapshot(bvid="BV1", tid=4, captured_at=start + timedelta(hours=index), view=1000 + index * 100)
        for index in range(7)
    ]

    result = HeuristicV1().detect(rows)[0]

    assert result.metrics["snapshot_count"] == 7
    assert result.metrics["window_span_days"] == 0.25
    assert "实际覆盖0.25天" in result.explain
    assert "最近7日" not in result.explain


def test_explanation_distinguishes_observed_days_from_elapsed_span():
    """稀疏采集文案必须同时如实说明采集日数和真实时间跨度。"""
    start = datetime(2026, 1, 1)
    rows = [
        Snapshot(bvid="BV1", tid=4, captured_at=start + timedelta(days=offset), view=view)
        for offset, view in [(0, 1000), (3, 1300), (6, 1600)]
    ]

    result = HeuristicV1().detect(rows)[0]

    assert result.metrics["observed_days"] == 3
    assert result.metrics["window_span_days"] == 6.0
    assert "实际覆盖6.00天（共3个采集日）" in result.explain
    assert "近3天" not in result.explain


def test_detection_contains_stable_metrics_and_explanation():
    """输出必须包含展示层所需核心指标和人话解释。"""
    rows = make_rows("BV1", [1000, 1100, 1250, 1500]) + make_rows("BV2", [1000, 1050, 1080, 1090])
    result = HeuristicV1().detect(rows)
    assert len(result) == 2
    assert {"growth", "first_derivative", "second_derivative", "percentile"} <= set(result[0].metrics)
    assert result[0].explain
    assert 0 <= result[0].confidence <= 1
