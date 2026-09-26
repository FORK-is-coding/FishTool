"""热点生命周期算法回归测试。"""
from datetime import datetime, timedelta

from modules.hotspot.algorithm import HeuristicV1, Snapshot


def make_rows(bvid: str, views: list[int], tid: int = 4) -> list[Snapshot]:
    """构造连续日快照。"""
    start = datetime(2026, 1, 1)
    return [Snapshot(bvid=bvid, tid=tid, captured_at=start + timedelta(days=index), view=view, title=bvid) for index, view in enumerate(views)]


def test_negative_latest_raw_growth_has_decline_priority():
    """最新原始日增速为负时，即使平滑增速仍高也必须判衰退。"""
    rows = make_rows("BV1", [1000, 2000, 4000, 8000, 16000, 32000, 64000, 60000])
    result = HeuristicV1().detect(rows)[0]
    assert result.stage == "衰退期"


def test_insufficient_samples_and_confidence():
    """少于两天返回数据不足，七天内置信度受压低。"""
    one_day = HeuristicV1().detect(make_rows("BV1", [1000]))[0]
    assert one_day.stage == "数据不足"
    assert one_day.confidence < 0.25


def test_detection_contains_stable_metrics_and_explanation():
    """输出必须包含展示层所需核心指标和人话解释。"""
    rows = make_rows("BV1", [1000, 1100, 1250, 1500]) + make_rows("BV2", [1000, 1050, 1080, 1090])
    result = HeuristicV1().detect(rows)
    assert len(result) == 2
    assert {"growth", "first_derivative", "second_derivative", "percentile"} <= set(result[0].metrics)
    assert result[0].explain
    assert 0 <= result[0].confidence <= 1
