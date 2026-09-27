"""启发式热点生命周期算法 v1 的契约级测试。

覆盖 modules/hotspot/algorithm/heuristic_v1.py 的全部公开与非公开契约点：
- HeuristicV1.__init__ / version / config_schema（默认域与显式配置）
- _percentile：空列表、包含自身的百分位
- _growth：原始增速、按真实时间回看的平滑增速、零播放兜底
- _decline_reason：单次噪声不判衰退、连续两间隔、累计回撤三间隔
- _stage：数据不足 / 衰退期 / 出现期 / 上升期 / 成熟期 / 观察期
- detect：分组、排序、跳过空 bvid、metrics/explain/confidence 契约

全部为纯内存计算，不触网、不落库。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from modules.hotspot.algorithm.base import Snapshot
from modules.hotspot.algorithm.config import HeuristicConfig
from modules.hotspot.algorithm.heuristic_v1 import HeuristicV1


# --------------------------------------------------------------------- 构造工具

def make_rows(
    views: list[int],
    *,
    bvid: str = "BV1",
    tid: int = 4,
    step_days: float = 1.0,
    owner_mid: int = 0,
    start: datetime | None = None,
) -> list[Snapshot]:
    """按等间隔天数构造同一视频的快照序列。"""
    base = start or datetime(2026, 1, 1)
    return [
        Snapshot(
            bvid=bvid,
            tid=tid,
            captured_at=base + timedelta(days=index * step_days),
            view=view,
            title=f"标题-{bvid}",
            owner_mid=owner_mid,
            owner_name=f"UP-{owner_mid}",
        )
        for index, view in enumerate(views)
    ]


def _two_rows(view_last: int = 20000) -> list[Snapshot]:
    """构造满足 _stage 长度要求的两条快照。"""
    return make_rows([view_last // 2, view_last])


# --------------------------------------------------------------------- 初始化契约

def test_init_uses_domain_config_by_default() -> None:
    """未传配置时应按领域读取阈值。"""
    detector = HeuristicV1(domain="game")

    assert detector.domain == "game"
    assert detector.config.base_view == 10_000


def test_init_prefers_explicit_config() -> None:
    """显式配置应覆盖领域默认配置。"""
    custom = HeuristicConfig(p_candidate=80.0, base_view=500)

    detector = HeuristicV1(domain="game", config=custom)

    assert detector.config is custom
    assert detector.config_schema["base_view"] == 500


def test_version_and_config_schema() -> None:
    """版本号与配置描述应可被前端直接消费。"""
    detector = HeuristicV1(domain="anime")

    assert detector.version == "heuristic_v1"
    schema = detector.config_schema
    assert schema["version"] == "heuristic_v1"
    assert schema["domain"] == "anime"
    assert schema["p_candidate"] == 90.0
    assert schema["window"] == 7


# --------------------------------------------------------------------- 百分位

def test_percentile_returns_zero_for_empty_values() -> None:
    """没有任何样本时百分位为 0。"""
    assert HeuristicV1._percentile(5.0, []) == 0.0


def test_percentile_includes_current_value() -> None:
    """百分位计算应包含当前值本身。"""
    assert HeuristicV1._percentile(3, [1, 2, 3]) == 100.0
    assert HeuristicV1._percentile(1, [1, 2, 3]) == pytest.approx(100.0 / 3)


def test_percentile_handles_value_between_samples() -> None:
    """介于样本之间的取值按小于等于计数。"""
    assert HeuristicV1._percentile(2.5, [1, 2, 3]) == pytest.approx(100.0 * 2 / 3)


# --------------------------------------------------------------------- 增速

def test_growth_returns_empty_for_single_or_empty_rows() -> None:
    """不足两条快照时增速与平滑序列均为空。"""
    detector = HeuristicV1()

    assert detector._growth([]) == ([], [])
    assert detector._growth(make_rows([100])) == ([], [])


def test_growth_computes_relative_rates() -> None:
    """相邻快照增速应为 (后-前)/前。"""
    detector = HeuristicV1()

    rates, smooth = detector._growth(make_rows([100, 150, 300]))

    assert rates == [0.5, 1.0]
    assert smooth == [0.5, pytest.approx(0.75)]


def test_growth_guards_zero_previous_view() -> None:
    """前一条播放为 0 时增速按 0 处理，避免除零。"""
    detector = HeuristicV1()

    rates, _ = detector._growth(make_rows([0, 100]))

    assert rates == [0.0]


def test_growth_window_trims_old_rates() -> None:
    """平滑增速只统计窗口内的相邻增速。"""
    detector = HeuristicV1(config=HeuristicConfig(window=3))
    # 每天翻倍：近 3 天窗口内不应把 10 天前的增速算进来。
    views = [100 * (2 ** index) for index in range(6)]

    _, smooth = detector._growth(make_rows(views))

    # 最后一条的窗口内只有最近 3 个相邻增速，且全部相同为 1.0。
    assert smooth[-1] == pytest.approx(1.0)
    assert len(smooth) == len(views) - 1


# --------------------------------------------------------------------- 衰退依据

def test_decline_reason_none_for_empty_and_single_rate() -> None:
    """无增速或仅一条增速时不构成衰退依据。"""
    assert HeuristicV1._decline_reason([]) is None
    assert HeuristicV1._decline_reason([0.5]) is None


def test_decline_reason_none_for_single_negative_noise() -> None:
    """单次轻微负增长属噪声，不判衰退。"""
    assert HeuristicV1._decline_reason([0.5, -0.02, 0.3]) is None


def test_decline_reason_detects_two_consecutive_drops() -> None:
    """最近两个采集间隔均下降至少 1% 时给出连续下降依据。"""
    reason = HeuristicV1._decline_reason([0.4, -0.02, -0.03])

    assert reason is not None
    assert "连续2个采集间隔" in reason


def test_decline_reason_boundary_equals_minus_one_percent() -> None:
    """恰好 -1% 视为达到阈值的下降。"""
    reason = HeuristicV1._decline_reason([-0.02, -0.01])

    assert reason is not None


def test_decline_reason_detects_cumulative_drawdown() -> None:
    """两次显著下跌、累计回撤超 5% 时给出累计回撤依据。"""
    reason = HeuristicV1._decline_reason([-0.3, 0.02, -0.3])

    assert reason is not None
    assert "累计回撤" in reason


def test_decline_reason_none_when_cumulative_below_threshold() -> None:
    """累计回撤不足 5% 时不判衰退。"""
    assert HeuristicV1._decline_reason([0.05, -0.001, -0.001]) is None


# --------------------------------------------------------------------- 阶段判定

def test_stage_insufficient_when_rows_too_few() -> None:
    """少于两天数据判定为数据不足。"""
    rows = make_rows([100])

    assert HeuristicV1()._stage(rows, [], [], 0.0) == ("数据不足", None)


def test_stage_insufficient_when_no_rates() -> None:
    """没有增速序列时判定为数据不足。"""
    rows = _two_rows()

    assert HeuristicV1()._stage(rows, [], [0.1, 0.2], 99.0) == ("数据不足", None)


def test_stage_insufficient_when_smooth_too_short() -> None:
    """平滑序列不足两条时判定为数据不足。"""
    rows = _two_rows()

    assert HeuristicV1()._stage(rows, [0.1], [0.1], 99.0) == ("数据不足", None)


def test_stage_decline_takes_priority() -> None:
    """命中衰退依据时优先判衰退期，并返回依据。"""
    rows = _two_rows()

    stage, reason = HeuristicV1()._stage(rows, [-0.02, -0.03], [0.5, 0.4], 100.0)

    assert stage == "衰退期"
    assert reason is not None


def test_stage_appearance_when_low_view_high_percentile() -> None:
    """播放低于基准但同分区百分位极高时判出现期。"""
    rows = make_rows([4000, 5000])

    stage, reason = HeuristicV1()._stage(rows, [0.5], [0.3, 0.37], 96.0)

    assert stage == "出现期"
    assert reason is None


def test_stage_rising_when_high_percentile_and_accelerating() -> None:
    """百分位高且二阶导为正时判上升期。"""
    rows = make_rows([12000, 20000])

    stage, _ = HeuristicV1()._stage(rows, [0.2], [0.1, 0.4], 95.0)

    assert stage == "上升期"


def test_stage_mature_when_decelerating_with_mid_percentile() -> None:
    """二阶导为负且百分位超过 70 时判成熟期。"""
    rows = make_rows([12000, 20000])

    stage, _ = HeuristicV1()._stage(rows, [0.2], [0.4, 0.1], 80.0)

    assert stage == "成熟期"


def test_stage_observation_fallback() -> None:
    """不满足任何阶段条件时回落观察期。"""
    rows = make_rows([12000, 20000])

    stage, _ = HeuristicV1()._stage(rows, [0.2], [0.4, 0.1], 50.0)

    assert stage == "观察期"


# --------------------------------------------------------------------- detect

def test_detect_empty_snapshots() -> None:
    """空快照列表返回空检测结果。"""
    assert HeuristicV1().detect([]) == []


def test_detect_skips_snapshots_without_bvid() -> None:
    """缺少 bvid 的快照应被跳过。"""
    rows = [Snapshot(bvid="", tid=4, captured_at=datetime(2026, 1, 1), view=100)]

    assert HeuristicV1().detect(rows) == []


def test_detect_sorts_snapshots_by_time() -> None:
    """同一视频快照应按采集时间升序参与计算。"""
    rows = make_rows([1000, 2000, 4000])
    shuffled = [rows[2], rows[0], rows[1]]

    result = HeuristicV1().detect(shuffled)

    assert len(result) == 1
    assert result[0].bvid == "BV1"
    assert result[0].metrics["snapshot_count"] == 3


def test_detect_single_snapshot_is_insufficient_with_zero_confidence() -> None:
    """只有一条快照时阶段为数据不足，置信度为 0。"""
    result = HeuristicV1().detect(make_rows([1000]))[0]

    assert result.stage == "数据不足"
    assert result.confidence == 0.0
    assert result.metrics["growth"] is None
    assert result.metrics["daily_growth"] is None


def test_detect_metrics_contract_and_explanation() -> None:
    """检测结果应包含展示层依赖的全部指标与人话解释。"""
    rows = make_rows([1000, 2000, 4000, 8000])
    result = HeuristicV1().detect(rows)[0]

    assert {
        "growth",
        "daily_growth",
        "first_derivative",
        "second_derivative",
        "percentile",
        "days",
        "observed_days",
        "window_span_days",
        "window_days",
        "snapshot_count",
        "up_count",
        "decline_reason",
    } <= set(result.metrics)
    assert result.explain
    assert result.algorithm_version == "heuristic_v1"
    assert 0.0 <= result.confidence <= 1.0


def test_detect_confidence_scales_with_real_span() -> None:
    """置信度按真实观测跨度计算，密集采样不伪装成多日覆盖。"""
    dense = make_rows([1000 + index for index in range(6)], step_days=0.1)
    result = HeuristicV1().detect(dense)[0]

    assert result.metrics["window_span_days"] < 1.0
    assert result.confidence < 0.05


def test_detect_confidence_full_for_long_span() -> None:
    """观测跨度达到算法窗口上限（>=30 天）时置信度达到上限 1。"""
    rows = make_rows([1000 * (index + 1) for index in range(31)], step_days=1.0)
    result = HeuristicV1(config=HeuristicConfig(window=30)).detect(rows)[0]

    assert result.metrics["window_span_days"] >= 30
    assert result.confidence == 1.0


def test_detect_dense_window_reports_actual_span_text() -> None:
    """密集快照的文案应说明真实覆盖天数，不冒充最近 7 日。"""
    rows = make_rows([1000 + index * 100 for index in range(7)], step_days=0.04)
    result = HeuristicV1().detect(rows)[0]

    assert "实际覆盖" in result.explain
    assert "最近7日" not in result.explain


def test_detect_counts_distinct_owners_per_tid() -> None:
    """同一分区内不同 UP 数应统计进 up_count 指标。"""
    rows = (
        make_rows([1000, 2000], bvid="BV1", tid=4, owner_mid=1)
        + make_rows([1000, 2000], bvid="BV2", tid=4, owner_mid=2)
        + make_rows([1000, 2000], bvid="BV3", tid=4, owner_mid=2)
    )

    result = HeuristicV1().detect(rows)

    assert len(result) == 3
    assert all(item.metrics["up_count"] == 2 for item in result)


def test_detect_handles_multiple_zones_independently() -> None:
    """不同分区各自计算百分位，互不干扰。"""
    rows = make_rows([1000, 2000, 4000], bvid="BVA", tid=4) + make_rows([100, 110, 120], bvid="BVB", tid=5)

    result = HeuristicV1().detect(rows)

    assert {item.bvid for item in result} == {"BVA", "BVB"}
    assert all(item.metrics["percentile"] == 100.0 for item in result)


def test_detect_output_sorted_by_stage_then_percentile() -> None:
    """输出应按 (阶段, 百分位降序) 稳定排序。"""
    rows = make_rows([1000], bvid="BVSingle", tid=4) + make_rows([1000, 2000, 4000], bvid="BVMulti", tid=5)

    result = HeuristicV1().detect(rows)

    stages = [item.stage for item in result]
    assert stages == sorted(stages)


def test_detect_declining_series_reports_reason() -> None:
    """持续衰减序列应判衰退并在 explain 中给出依据。"""
    rows = make_rows([1000, 1500, 2200, 3000, 2800, 2500])
    result = HeuristicV1().detect(rows)[0]

    assert result.stage == "衰退期"
    assert result.metrics["decline_reason"]
    assert "衰退依据" in result.explain
    assert result.title == "标题-BV1"
    assert result.tid == 4
