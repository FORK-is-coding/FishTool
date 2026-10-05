"""热点生命周期算法 v2 的契约级测试（对应 02 方案 §11 M1—M26 与本批口径）。

覆盖点：
- 配置校验与 ``config_schema``；
- coverage 两级输出（``coverage_ratio`` 数值 + ``coverage_state`` 枚举）；
- ``valid_segments`` / ``boundary`` / ``window_measure`` 的纯数学契约；
- ``direction`` 的 AND 门、边界值与低基数规则；
- ``advance`` 状态机的确认计数、固定基线、缺口清零；
- ``detect`` 的分阶段产出、全零热度、单点观测、时间乱序；
- **核心靶子**：90% 覆盖必须能出阶段（原设计会判数据不足）；
- 低覆盖 20% 不阻断输出，但 ``confidence`` 恒为 0.0（§10.1 归位），覆盖差异只走 ``coverage_*``；
- M17 长 gap：窗内长 gap 造成的部分覆盖只出 coverage，不推进阶段（candidate/count/stable_count 不变）；
- ``state_revision`` fencing：旧代际写入丢弃。

全部为纯内存计算，使用固定时钟；不触网、不落库、不读取任何密钥。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modules.hotspot.algorithm.base import Snapshot
from modules.hotspot.algorithm.lifecycle_v2 import (
    DAY_S,
    DEFAULT_GAP_S,
    Analysis,
    CoverageState,
    LifecycleV2,
    LifecycleV2Config,
    Point,
    Stage,
    TrendState,
    advance,
    boundary,
    classify_coverage,
    direction,
    valid_segments,
    window_measure,
)

# 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int，是 86400 的整数倍）。
E: int = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
DAY: int = DAY_S
HOUR: int = 3600
CFG = LifecycleV2Config()
# B6a（08 案 §J）起出现期要消费发布时间证据：默认给「测试时钟起点发布」，年龄恒 1 日左右，
# 稳过 emerge_age_days=7；验证「证据缺失 / 老龄 / 未来」的用例必须显式覆盖。
PUBDATE: int = E


# --------------------------------------------------------------------- 构造工具


def _snap(
    epoch_s: int,
    view,
    *,
    bvid: str = "BV1",
    tid: int = 4,
    quality: str = "ok",
    mid: int = 1001,
    pubdate_epoch_s: int | None = PUBDATE,
    pubdate_status: str = "ok",
) -> Snapshot:
    """构造带 UTC 秒级时间、质量位与发布时间证据的快照。"""
    return Snapshot(
        bvid=bvid,
        tid=tid,
        captured_at=datetime.fromtimestamp(epoch_s, tz=timezone.utc),
        view=view,
        captured_epoch_s=epoch_s,
        view_quality=quality,
        title=f"标题-{bvid}",
        owner_mid=mid,
        owner_name=f"UP-{mid}",
        pubdate_epoch_s=pubdate_epoch_s,
        pubdate_status=pubdate_status,
    )


def make_rows(
    views: list,
    *,
    bvid: str = "BV1",
    tid: int = 4,
    step_s: int = DAY,
    start_epoch: int = E,
    quality: str = "ok",
    mid: int = 1001,
) -> list[Snapshot]:
    """按等间隔秒数构造同一视频的快照序列。"""
    return [
        _snap(start_epoch + index * step_s, view, bvid=bvid, tid=tid, quality=quality, mid=mid)
        for index, view in enumerate(views)
    ]


def as_of_daily(views: list, *, start_epoch: int = E, step_s: int = DAY) -> int:
    """等间隔序列的默认计算截止时刻（对齐到最后一个观测点）。"""
    return start_epoch + (len(views) - 1) * step_s


# --------------------------------------------------------------------- 配置契约


def test_config_defaults() -> None:
    """默认阈值应与 02 方案 §5.1 一致（coverage 门槛已按裁定三降到 0.85）。"""
    config = LifecycleV2Config()

    assert config.window_seconds == DAY_S
    assert config.gap_max_seconds == 36 * 3600
    assert config.min_window_coverage == 0.85
    assert config.strict_full_support is False
    assert config.confirmation_windows == 2
    assert config.threshold_version == "lifecycle_v2_age_gate_2"


def test_config_as_dict_covers_all_fields() -> None:
    """as_dict 必须覆盖全部配置字段，供 config_schema/回放消费。"""
    dumped = LifecycleV2Config().as_dict()

    assert set(dumped) == set(LifecycleV2Config.__dataclass_fields__)
    assert dumped["window_seconds"] == DAY_S


@pytest.mark.parametrize(
    "overrides",
    [
        {"window_seconds": 0},
        {"window_seconds": -1},
        {"window_seconds": 86400.0},
        {"gap_max_seconds": 0},
        {"max_staleness_seconds": 0},
        {"min_window_coverage": 0.0},
        {"min_window_coverage": 1.5},
        {"confirmation_windows": 1},
        {"absolute_delta": 0.0},
        {"low_base": 0.0},
        {"relative_delta": -0.1},
        {"history_days": -1},
        {"percentile_min_n": 0},
    ],
)
def test_config_rejects_invalid_values(overrides: dict) -> None:
    """非法阈值必须在构造检测器时显式报错，禁止静默产出错误阶段。"""
    with pytest.raises(ValueError):
        LifecycleV2(config=LifecycleV2Config(**overrides))


@pytest.mark.parametrize("bad_as_of", [-1, 1.5, "bad"])
def test_invalid_as_of_rejected(bad_as_of) -> None:
    """as_of_epoch_s 必须是秒级非负整数。"""
    with pytest.raises(ValueError):
        LifecycleV2(as_of_epoch_s=bad_as_of)


def test_version_and_config_schema() -> None:
    """版本号与配置描述应可被展示层直接消费。"""
    detector = LifecycleV2(domain="game")

    assert detector.version == "lifecycle_v2"
    schema = detector.config_schema
    assert schema["version"] == "lifecycle_v2"
    assert schema["domain"] == "game"
    assert schema["min_window_coverage"] == 0.85
    assert schema["threshold_version"] == "lifecycle_v2_age_gate_2"


# --------------------------------------------------------------------- coverage 两级输出


def test_classify_coverage_thresholds() -> None:
    """coverage 到枚举的映射：1.0 完整、[0.85,1.0) 暂定、其余不足。"""
    assert classify_coverage(1.0) is CoverageState.FULL_SUPPORT
    assert classify_coverage(1.2) is CoverageState.FULL_SUPPORT
    assert classify_coverage(0.9) is CoverageState.PROVISIONAL
    assert classify_coverage(0.85) is CoverageState.PROVISIONAL
    assert classify_coverage(0.8499) is CoverageState.INSUFFICIENT
    assert classify_coverage(0.2) is CoverageState.INSUFFICIENT
    assert classify_coverage(0.0) is CoverageState.INSUFFICIENT


def test_classify_coverage_strict_full_support() -> None:
    """strict_full_support 打开后只有 1.0 才算可用（保留原设计精确模式）。"""
    assert classify_coverage(0.99, strict_full_support=True) is CoverageState.INSUFFICIENT
    assert classify_coverage(1.0, strict_full_support=True) is CoverageState.FULL_SUPPORT


def test_coverage_two_level_output_combinations() -> None:
    """coverage_ratio（数值）+ coverage_state（枚举）必须同时给出且组合正确。"""
    cases = [
        (make_rows([1000, 1100, 1200]), E + 2 * DAY, "full_support"),
        ([_snap(E + 8640, 100), _snap(E + DAY, 190)], E + DAY, "provisional"),
        ([_snap(E + 69120, 100), _snap(E + DAY, 120)], E + DAY, "insufficient"),
    ]

    for rows, as_of, expected_state in cases:
        detection = LifecycleV2(as_of_epoch_s=as_of).detect(rows)[0]

        assert detection.metadata["coverage_state"] == expected_state
        assert isinstance(detection.metrics["coverage_ratio"], float)
        assert detection.metadata["coverage_state"] in {state.value for state in CoverageState}
        # 三种覆盖级别只要还有可用证据就都应产出真实阶段（而非 None）。
        assert detection.stage in {
            Stage.OBSERVING,
            Stage.EMERGING,
            Stage.RISING,
            Stage.MATURE,
            Stage.DECLINING,
        }


# --------------------------------------------------------------------- 方向判定


def test_direction_and_gate_requires_both_absolute_and_relative() -> None:
    """base>=low_base 时绝对与相对阈值必须是 AND，不能 OR（M6）。"""
    assert direction(1000.0, 1030.0) == "stable"  # 绝对 30 达标，但相对仅 3%
    assert direction(50.0, 65.0) == "stable"      # 相对 30% 达标，但绝对仅 15
    assert direction(1000.0, 1250.0) == "up"      # 绝对 250 / 相对 25% 同时达标


def test_direction_boundary_exact_thresholds() -> None:
    """恰好满足 abs>=20 且 rel>=20% 的边界应进入候选（M7）。"""
    assert direction(100.0, 120.0) == "up"
    assert direction(100.0, 80.0) == "down"
    assert direction(100.0, 119.9) == "stable"  # 相对 19.9% 差一点


def test_direction_low_base_rule() -> None:
    """低基数只用绝对阈值，避免百分比爆炸（M8）。"""
    assert direction(0.0, 20.0) == "up"
    assert direction(10.0, 0.0) == "stable"
    assert direction(20.0, 0.0) == "down"


# --------------------------------------------------------------------- 分段与插值


def test_valid_segments_splits_on_counter_decrease() -> None:
    """累计播放回撤必须切段，保证段内无负速度。"""
    points = [
        Point(E, 1000, True),
        Point(E + DAY, 1100, True),
        Point(E + 2 * DAY, 1099, True),
        Point(E + 3 * DAY, 1199, True),
    ]

    segments = valid_segments(points)

    assert [[point.view for point in seg] for seg in segments] == [[1000, 1100], [1099, 1199]]


def test_valid_segments_splits_on_long_gap() -> None:
    """相邻点间隔超过 gap_max_seconds 必须断段，禁止跨空洞插值。"""
    points = [Point(E, 1000, True), Point(E + 40 * HOUR, 1100, True)]

    segments = valid_segments(points)

    assert len(segments) == 2


def test_valid_segments_conflicting_duplicate_splits() -> None:
    """同 epoch 不同值属冲突，保守断段而不取最大值。"""
    points = [Point(E, 1000, True), Point(E, 1001, True), Point(E + DAY, 1100, True)]

    segments = valid_segments(points)

    assert [[point.view for point in seg] for seg in segments] == [[1100]]


def test_valid_segments_handles_invalid_view_type_without_typeerror() -> None:
    """非法 view 类型只断段，不得因 set 推导抛 TypeError（M25）。"""
    points = [Point(E, 1000, True), Point(E + DAY, {"a": 1}, True), Point(E + 2 * DAY, 1200, True)]

    segments = valid_segments(points)  # 不应抛异常

    assert [[point.view for point in seg] for seg in segments] == [[1000], [1200]]


def test_valid_segments_rejects_unlocated_time() -> None:
    """无法定位时间的 marker 必须显式报错，不能伪造 epoch。"""
    with pytest.raises(ValueError):
        valid_segments([Point("bad", 1000, True)])


def test_boundary_interpolates_and_rejects_outside_range() -> None:
    """只允许段内双边线性插值，段外一律 None（不外推）。"""
    seg = [Point(E, 1000, True), Point(E + DAY, 1200, True)]

    assert boundary(seg, E) == 1000.0
    assert boundary(seg, E + DAY) == 1200.0
    assert boundary(seg, E + DAY // 2) == pytest.approx(1100.0)
    assert boundary(seg, E - DAY) is None
    assert boundary(seg, E + 2 * DAY) is None


def test_boundary_respects_max_gap() -> None:
    """跨超长 gap 的插值请求应被拒绝。"""
    seg = [Point(E, 1000, True), Point(E + 40 * HOUR, 1200, True)]

    assert boundary(seg, E + 19 * HOUR, max_gap_s=DEFAULT_GAP_S) is None


# --------------------------------------------------------------------- 窗口测量


def test_window_measure_full_span_is_full_coverage() -> None:
    """整窗被有效区间支撑时覆盖率为 1，增量为窗内播放差。"""
    seg = [Point(E, 1000, True), Point(E + DAY, 1100, True)]

    delta, coverage, observed = window_measure(seg, E + DAY)

    assert delta == pytest.approx(100.0)
    assert coverage == pytest.approx(1.0)
    assert observed == DAY


def test_window_measure_partial_span_reports_ratio() -> None:
    """左端只有部分支撑时给出已观察区间增量与其占比（裁定三核心）。"""
    seg = [Point(E + 8640, 100, True), Point(E + DAY, 190, True)]

    delta, coverage, observed = window_measure(seg, E + DAY)

    assert delta == pytest.approx(90.0)
    assert coverage == pytest.approx(0.9)
    assert observed == DAY - 8640


def test_window_measure_no_forward_extrapolation() -> None:
    """右端缺括点时返回空增量（不向前外推，M15）。"""
    seg = [Point(E, 1000, True), Point(E + DAY - HOUR, 1100, True)]

    delta, coverage, observed = window_measure(seg, E + DAY)

    assert delta is None
    assert coverage == pytest.approx((DAY - HOUR) / DAY)


# --------------------------------------------------------------------- 状态机


def test_advance_requires_two_confirmations() -> None:
    """上升需要两个连续日窗相对固定基线显著增强。"""
    state = TrendState()
    stages = []
    for index, rate in enumerate([100.0, 150.0, 150.0, 150.0, 150.0]):
        emerging = state.prev_rate is None and rate >= CFG.emerge_rate
        advance(state, E + (index + 1) * DAY, rate, config=CFG, emerging=emerging)
        stages.append(state.stage)

    assert stages == [Stage.EMERGING, Stage.EMERGING, Stage.RISING, Stage.RISING, Stage.MATURE]


def test_advance_decline_plateau_sequence() -> None:
    """R=[200,100,100,100,100]：第3窗衰退、第4窗仍衰退、第5窗转成熟（回落后平台期）。"""
    state = TrendState()
    stages = []
    for index, rate in enumerate([200.0, 100.0, 100.0, 100.0, 100.0]):
        emerging = state.prev_rate is None and rate >= CFG.emerge_rate
        advance(state, E + (index + 1) * DAY, rate, config=CFG, emerging=emerging)
        stages.append(state.stage)

    assert stages == [
        Stage.EMERGING,
        Stage.EMERGING,
        Stage.DECLINING,
        Stage.DECLINING,
        Stage.MATURE,
    ]


def test_advance_resets_on_gap() -> None:
    """非相邻日窗必须清空候选与稳定计数，缺口后只建立新基线。"""
    state = TrendState(
        last_evaluation_epoch_s=E,
        prev_rate=100.0,
        candidate="down",
        baseline=200.0,
        count=1,
        stable_count=1,
        stage=Stage.DECLINING,
    )

    advance(state, E + 2 * DAY, 100.0, config=CFG)

    assert state.stage == Stage.OBSERVING
    assert state.candidate is None
    assert state.count == 0
    assert state.stable_count == 0
    assert state.prev_rate == 100.0


def test_advance_rejects_negative_rate() -> None:
    """强度不允许为负，负值必须显式报错而不是被当成衰退。"""
    with pytest.raises(ValueError):
        advance(TrendState(), E + DAY, -1.0, config=CFG)


# --------------------------------------------------------------------- 阶段产出


def test_detect_constant_series_is_mature() -> None:
    """恒速 100/天：两个 stable 比较后成熟，既不上升也不衰退（M1）。"""
    rows = make_rows([1000, 1100, 1200, 1300])

    detection = LifecycleV2(as_of_epoch_s=E + 3 * DAY).detect(rows)[0]

    assert detection.stage == Stage.MATURE
    assert detection.metrics["rate_current"] == pytest.approx(100.0)
    assert detection.metadata["coverage_state"] == "full_support"
    assert detection.metrics["observed_windows"] == 3


def test_detect_first_drop_is_pending_not_confirmed_decline() -> None:
    """200→100 只有一次证据，方向下降但不得确认衰退（M2）。"""
    rows = make_rows([1000, 1200, 1300])

    analysis = LifecycleV2(as_of_epoch_s=E + 2 * DAY).analyze_one("BV1", rows)

    assert analysis.stage != Stage.DECLINING
    assert analysis.state.candidate == "down"
    assert analysis.state.count == 1
    assert analysis.rate_current == pytest.approx(100.0)
    assert analysis.rate_delta == pytest.approx(-100.0)
    assert analysis.relative_change == pytest.approx(-0.5)


def test_detect_second_drop_confirms_decline_on_frozen_baseline() -> None:
    """200→100→100 两窗对冻结基线 200 降低 50%，确认衰退（M3）。"""
    rows = make_rows([1000, 1200, 1300, 1400])

    analysis = LifecycleV2(as_of_epoch_s=E + 3 * DAY).analyze_one("BV1", rows)

    assert analysis.stage == Stage.DECLINING
    assert analysis.rate_current == pytest.approx(100.0)


def test_detect_recovery_cancels_decline_candidate() -> None:
    """200→100→200 撤销下降候选，绝不确认衰退（M4）。"""
    rows = make_rows([1000, 1200, 1300, 1500])

    analysis = LifecycleV2(as_of_epoch_s=E + 3 * DAY).analyze_one("BV1", rows)

    assert analysis.stage != Stage.DECLINING
    assert analysis.state.candidate == "up"
    assert analysis.state.count == 1


def test_detect_strengthening_confirms_rising() -> None:
    """100→150→150 连续两窗相对 100 增强，确认上升（M5）。"""
    rows = make_rows([1000, 1100, 1250, 1400])

    assert LifecycleV2(as_of_epoch_s=E + 3 * DAY).detect(rows)[0].stage == Stage.RISING


def test_detect_emerging_on_first_window() -> None:
    """首个完整有效窗 R>=20 即出现期，不被三窗冷启动门挡住（M20）。"""
    rows = make_rows([1000, 1050])

    analysis = LifecycleV2(as_of_epoch_s=E + DAY).analyze_one("BV1", rows)

    assert analysis.stage == Stage.EMERGING
    assert analysis.rate_current == pytest.approx(50.0)


def test_detect_observing_when_first_window_below_emerge_rate() -> None:
    """首窗强度不足出现门槛时停在观察期。"""
    rows = make_rows([1000, 1010])

    assert LifecycleV2(as_of_epoch_s=E + DAY).detect(rows)[0].stage == Stage.OBSERVING


def test_detect_rising_then_matures() -> None:
    """上升确认后连续两次稳定比较转成熟期（上升期退出边界）。"""
    rows = make_rows([1000, 1100, 1250, 1400, 1550, 1700])

    assert LifecycleV2(as_of_epoch_s=E + 5 * DAY).detect(rows)[0].stage == Stage.MATURE


def test_detect_mature_then_declines() -> None:
    """成熟后强度阶梯下滑，连续两窗确认衰退（成熟期退出边界）。"""
    rows = make_rows([1000, 1100, 1200, 1300, 1350, 1360])

    assert LifecycleV2(as_of_epoch_s=E + 5 * DAY).detect(rows)[0].stage == Stage.DECLINING


def test_detect_rollback_segments_then_matures_after_three_windows() -> None:
    """负回撤切段：负 1 不算衰退，新段 R=100 累计三窗后成熟（M10）。"""
    rows = make_rows([1000, 1100, 1099, 1199, 1299, 1399])

    analysis = LifecycleV2(as_of_epoch_s=E + 5 * DAY).analyze_one("BV1", rows)

    assert analysis.stage == Stage.MATURE
    assert len(analysis.rates) == 3
    assert all(rate == pytest.approx(100.0) for rate in analysis.rates)
    assert min(analysis.rates) >= 0  # 回撤从未作为负速度进入阶段比较


def test_detect_all_zero_is_mature_not_missing() -> None:
    """全零热度且质量全部 ok：完整窗强度为 0，不能被当数据缺失（M13）。"""
    rows = make_rows([0, 0, 0, 0])

    detection = LifecycleV2(as_of_epoch_s=E + 3 * DAY).detect(rows)[0]

    assert detection.stage == Stage.MATURE
    assert detection.metrics["rate_current"] == pytest.approx(0.0)
    assert detection.metadata["coverage_state"] == "full_support"


def test_detect_missing_marker_does_not_decline() -> None:
    """质量 missing 的坏点切断窗口，不得跨坏点生成日窗或触发衰退（M12）。"""
    rows = [
        _snap(E, 1000),
        _snap(E + DAY, 0, quality="missing"),
        _snap(E + 2 * DAY, 1200),
        _snap(E + 3 * DAY, 1300),
    ]

    detection = LifecycleV2(as_of_epoch_s=E + 3 * DAY).detect(rows)[0]

    assert detection.stage != Stage.DECLINING
    assert detection.stage == Stage.EMERGING


def test_detect_gap_resets_candidate() -> None:
    """缺口后的首个有效窗只建立新基线，count=0、不拼成连续两窗（M24）。"""
    rows = [
        _snap(E, 1000),
        _snap(E + DAY, 1200),
        _snap(E + 2 * DAY, 1300),
        _snap(E + 4 * DAY, 1400),
        _snap(E + 5 * DAY, 1410),
    ]

    analysis = LifecycleV2(as_of_epoch_s=E + 5 * DAY).analyze_one("BV1", rows)

    assert analysis.stage == Stage.OBSERVING
    assert analysis.state.count == 0
    assert analysis.state.candidate is None
    assert analysis.rates == [pytest.approx(10.0)]


# --------------------------------------------------------------------- 边界输入


def test_detect_single_point_is_insufficient_but_returns_detection() -> None:
    """单点观测：数据不足，但仍返回 Detection 而非 None。"""
    detection = LifecycleV2(as_of_epoch_s=E).detect(make_rows([1000]))[0]

    assert detection.stage == Stage.INSUFFICIENT
    assert detection.metadata["coverage_state"] == "insufficient"
    assert detection.metrics["observed_windows"] == 0
    assert detection.confidence == 0.0


def test_detect_empty_input_returns_empty_list() -> None:
    """空输入返回空列表。"""
    assert LifecycleV2().detect([]) == []


def test_detect_out_of_order_input_is_order_independent() -> None:
    """时间乱序输入必须得到与升序一致的结果。"""
    rows = make_rows([1000, 1200, 1300, 1400])

    ordered = LifecycleV2(as_of_epoch_s=E + 3 * DAY).analyze_one("BV1", rows)
    shuffled = LifecycleV2(as_of_epoch_s=E + 3 * DAY).analyze_one("BV1", list(reversed(rows)))

    assert ordered.stage == shuffled.stage
    assert ordered.rates == shuffled.rates
    assert ordered.coverage_ratio == shuffled.coverage_ratio


# --------------------------------------------------------------------- 核心靶子：90% 覆盖


def test_high_coverage_90_percent_produces_stage_core_target() -> None:
    """核心靶子：90% 覆盖的正常曲线必须能出阶段。

    构造：窗口 (E, E+DAY]，首个观测落在 E+0.1day，右端有括点。
    覆盖率 = 0.9，落在「暂定观察」区间，必须产出出现期。
    """
    rows = [_snap(E + 8640, 100), _snap(E + DAY, 190)]

    detection = LifecycleV2(as_of_epoch_s=E + DAY).detect(rows)[0]

    assert detection.metrics["coverage_ratio"] == pytest.approx(0.9)
    assert detection.metadata["coverage_state"] == "provisional"
    assert detection.stage == Stage.EMERGING
    assert detection.stage != Stage.INSUFFICIENT


def test_high_coverage_under_original_design_would_be_insufficient() -> None:
    """对照：原设计 min_window_coverage=1.0 会把 0.9 判成窗不可用 → 数据不足。

    这正是上单核心靶子会挂在原设计下的原因：高覆盖反而被冷启动/完整窗门挡住。
    """
    rows = [_snap(E + 8640, 100), _snap(E + DAY, 190)]

    detection = LifecycleV2(
        config=LifecycleV2Config(strict_full_support=True),
        as_of_epoch_s=E + DAY,
    ).detect(rows)[0]

    assert detection.stage == Stage.INSUFFICIENT
    assert detection.metrics["observed_windows"] == 0


def test_low_coverage_still_outputs_stage_with_lower_confidence() -> None:
    """低覆盖 20%：枚举为 insufficient，但既不阻断出阶段，也不折算置信度。

    口径出处：02 方案 §10.1 —— 第一版 ``confidence`` 固定 0.0、``confidence_kind`` 固定
    ``not_estimated``，不得临时拼一个覆盖分数称概率；覆盖强弱改由 ``coverage_ratio`` /
    ``coverage_state`` 表达。「低覆盖不阻断出阶段」这条保持不变。
    """
    rows = [_snap(E + 69120, 100), _snap(E + DAY, 120)]
    detector = LifecycleV2(as_of_epoch_s=E + DAY)

    analysis = detector.analyze_one("BV1", rows)
    detection = detector.detect(rows)[0]

    assert analysis.coverage_ratio == pytest.approx(0.2)
    assert analysis.coverage_state is CoverageState.INSUFFICIENT
    assert analysis.stage != Stage.INSUFFICIENT      # 低覆盖不阻断出阶段
    assert analysis.stage == Stage.EMERGING
    assert analysis.confidence == 0.0                # 固定 0.0，不随覆盖折算
    assert analysis.confidence_kind == "not_estimated"

    assert detection.metrics["coverage_ratio"] == pytest.approx(0.2)
    assert detection.metadata["coverage_state"] == "insufficient"
    assert detection.stage == Stage.EMERGING
    assert detection.confidence == 0.0


def test_confidence_is_flat_and_independent_of_coverage() -> None:
    """confidence 与覆盖无关：20% 与 90% 覆盖一律 0.0，差异只体现在 coverage_* 上。

    原用例 ``test_confidence_rises_with_coverage`` 断言「覆盖越高置信度越高」，属于上批
    临时拼装的变量化口径；按 §10.1 归位后不再成立，故改为断言恒 0.0，并把覆盖差异如实
    记在 ``coverage_ratio`` / ``coverage_state`` 上。
    """
    low = LifecycleV2(as_of_epoch_s=E + DAY).analyze_one("BV1", [_snap(E + 69120, 100), _snap(E + DAY, 120)])
    high = LifecycleV2(as_of_epoch_s=E + DAY).analyze_one("BV1", [_snap(E + 8640, 100), _snap(E + DAY, 190)])

    # 不再随覆盖上升：两条曲线的 confidence / confidence_kind 完全相同。
    assert low.confidence == high.confidence == 0.0
    assert low.confidence_kind == high.confidence_kind == "not_estimated"
    assert low.stage == high.stage == Stage.EMERGING
    # 覆盖差异仍被如实记录，只是不借 confidence 表达强弱。
    assert low.coverage_ratio < high.coverage_ratio
    assert low.coverage_state is CoverageState.INSUFFICIENT
    assert high.coverage_state is CoverageState.PROVISIONAL


def test_internal_gap_blocks_stage_advance_m17() -> None:
    """M17：窗内长 gap（48h）造成覆盖不完整 → 该窗被识别，只出 coverage，不推进阶段。

    构造：0.5d 与 2.5d 相隔 48h（> gap_max 36h）断开；窗 (2d, 3d] 的两个边界都能插到值
    （「边界都有值」），但左端 2d~2.5d 落在长 gap 里、coverage=0.5 —— 属于 M17，不得用它
    推进 candidate / count / stable_count，也不计入 observed_windows。
    若沿用旧口径把该窗当有效窗推进，后续 400→250→300 会被推成衰退；M17 下必须停在出现期。
    """
    rows = [
        _snap(E + 43200, 1000),    # 0.5d：观测历史起点
        _snap(E + 216000, 1000),   # 2.5d：与起点相隔 48h → 断段
        _snap(E + 259200, 1200),   # 3d
        _snap(E + 345600, 1450),   # 4d
        _snap(E + 432000, 1750),   # 5d
    ]

    analysis = LifecycleV2(as_of_epoch_s=E + 5 * DAY).analyze_one("BV1", rows)

    assert analysis.gap_blocked_windows == 1          # gap 窗被识别
    assert analysis.observed_windows == 2             # 只计右端两个完整窗，不因它增长
    assert analysis.stage == Stage.EMERGING           # 阶段未被推进
    assert analysis.stage != Stage.DECLINING

    # 对照：把 48h 空洞用 1.5d 的真实点补成连续段 → 无 gap 窗，四个完整窗全部计入。
    bridged = [_snap(E + 43200, 1000), _snap(E + 129600, 1000)] + rows[1:]
    control = LifecycleV2(as_of_epoch_s=E + 5 * DAY).analyze_one("BV1", bridged)

    assert control.gap_blocked_windows == 0
    assert control.observed_windows == 5
    assert control.observed_windows > analysis.observed_windows


# --------------------------------------------------------------------- 指标契约


def test_metrics_is_purely_numeric_with_coverage_state_in_metadata() -> None:
    """coverage_state 已归位 metadata：metrics 必须全为数值或 None，字符串只出现在 metadata。"""
    detection = LifecycleV2(as_of_epoch_s=E + 2 * DAY).detect(make_rows([1000, 1100, 1200]))[0]

    assert "coverage_ratio" in detection.metrics
    assert "coverage_state" not in detection.metrics
    assert detection.metadata["coverage_state"] == "full_support"
    for value in detection.metrics.values():
        assert value is None or isinstance(value, (int, float))


def test_detection_metadata_has_confidence_kind_not_estimated() -> None:
    """metadata 通道必带 confidence_kind，且第一版固定为 'not_estimated'（§10.1）。"""
    detections = LifecycleV2(as_of_epoch_s=E + 2 * DAY).detect(make_rows([1000, 1100, 1200]))

    assert detections
    for detection in detections:
        assert detection.metadata["confidence_kind"] == "not_estimated"


@pytest.mark.parametrize(
    "rows, as_of, expected",
    [
        (make_rows([1000, 1100, 1200]), E + 2 * DAY, "full_support"),
        ([_snap(E + 8640, 100), _snap(E + DAY, 190)], E + DAY, "provisional"),
        ([_snap(E + 69120, 100), _snap(E + DAY, 120)], E + DAY, "insufficient"),
    ],
)
def test_detection_metadata_coverage_state_matches_analysis(rows, as_of, expected) -> None:
    """三态各一条：Detection.metadata['coverage_state'] 与 Analysis.coverage_state.value 一致。"""
    detector = LifecycleV2(as_of_epoch_s=as_of)

    detection = detector.detect(rows)[0]
    analysis = detector.analyze_one("BV1", rows)

    assert detection.metadata["coverage_state"] == analysis.coverage_state.value == expected


def test_detection_metrics_all_keys_numeric() -> None:
    """metrics 全键皆数值或 None（isinstance(v,(int,float)) or v is None），供前端直接绘图。"""
    detection = LifecycleV2(as_of_epoch_s=E + 2 * DAY).detect(make_rows([1000, 1100, 1200]))[0]

    assert detection.metrics
    for key, value in detection.metrics.items():
        assert value is None or isinstance(value, (int, float)), f"metrics[{key!r}] 非数值: {value!r}"


def test_detection_metadata_and_metrics_disjoint() -> None:
    """metadata 与 metrics 键集互斥（∩ == ∅），非数值项只走 metadata。"""
    detections = LifecycleV2(as_of_epoch_s=E + 2 * DAY).detect(make_rows([1000, 1100, 1200]))

    assert detections
    for detection in detections:
        assert set(detection.metadata) & set(detection.metrics) == set()


def test_time_fields_all_use_epoch_s_suffix() -> None:
    """口径二：时间字段统一 *_epoch_s 秒级 int，不得出现 _ts/_at/毫秒。"""
    schema = LifecycleV2().config_schema
    assert "as_of_epoch_s" in schema
    for key in schema:
        assert not key.endswith("_ts")
        assert not key.endswith("_at")

    detection = LifecycleV2(as_of_epoch_s=E + 2 * DAY).detect(make_rows([1000, 1100, 1200]))[0]
    for key in detection.metrics:
        assert not key.endswith("_ts")
        assert not key.endswith("_at")

    assert "epoch_s" in Point.__dataclass_fields__
    assert "last_evaluation_epoch_s" in TrendState.__dataclass_fields__


def test_percentile_none_for_small_sample() -> None:
    """同源样本不足 percentile_min_n 时给 None，但不阻断阶段（M22）。"""
    detection = LifecycleV2(as_of_epoch_s=E + DAY).detect(make_rows([1000, 1200]))[0]

    assert detection.metrics["percentile"] is None
    assert detection.stage == Stage.EMERGING  # 小样本不影响绝对趋势判定


def test_percentile_computed_with_enough_peers() -> None:
    """同源样本足够时计算百分位，最强样本应为 100。"""
    rows: list[Snapshot] = []
    for index in range(20):
        rows += make_rows([1000, 1200 if index == 0 else 1100], bvid=f"BV{index:02d}", tid=4)

    detections = {item.bvid: item for item in LifecycleV2(as_of_epoch_s=E + DAY).detect(rows)}

    assert detections["BV00"].metrics["percentile"] == pytest.approx(100.0)
    assert detections["BV01"].metrics["percentile"] == pytest.approx(95.0)


# --------------------------------------------------------------------- 配置生效与陈旧


def test_custom_window_and_gap_config_supported() -> None:
    """同一内核支持 2h 窗 / 40min gap，参数改变确实生效（M26）。"""
    config = LifecycleV2Config(window_seconds=2 * HOUR, gap_max_seconds=40 * 60)
    rows = [
        _snap(E, 100),
        _snap(E + 1800, 110),
        _snap(E + 3600, 120),
        _snap(E + 5400, 130),
        _snap(E + 7200, 140),
    ]

    custom = LifecycleV2(config=config, as_of_epoch_s=E + 7200).detect(rows)[0]
    assert custom.metadata["coverage_state"] == "full_support"
    assert custom.stage == Stage.EMERGING
    assert custom.metrics["rate_current"] == pytest.approx(480.0)

    # 默认 24h 窗对同样只覆盖 2h 的数据构不成完整窗口 → 数据不足。
    default = LifecycleV2(as_of_epoch_s=E + 7200).detect(rows)[0]
    assert default.stage == Stage.INSUFFICIENT


def test_detect_stale_series_keeps_history_not_decline() -> None:
    """采样陈旧超 36h：保留历史阶段、不判衰退，并以陈旧度如实标注。"""
    rows = make_rows([1000, 1100, 1200])
    as_of = E + 2 * DAY + 40 * HOUR

    detection = LifecycleV2(as_of_epoch_s=as_of).detect(rows)[0]

    assert detection.metrics["staleness_hours"] > 36
    assert detection.stage != Stage.DECLINING


# --------------------------------------------------------------------- state_revision fencing


def test_commit_state_rejects_stale_revision() -> None:
    """口径一：写回必须带 revision 校验，旧代际写入丢弃，新代际才自增落盘。"""
    detector = LifecycleV2()

    revision, claim = detector.claim_state("BV1")
    assert revision == 0
    assert claim.stage == Stage.OBSERVING

    assert detector.commit_state("BV1", TrendState(stage=Stage.RISING), claim_revision=0) is True
    assert detector.revision_of("BV1") == 1
    assert detector.states["BV1"].stage == Stage.RISING

    # 仍拿旧代际 0 提交：必须被丢弃，不覆盖已落盘的新 owner。
    assert detector.commit_state("BV1", TrendState(stage=Stage.DECLINING), claim_revision=0) is False
    assert detector.states["BV1"].stage == Stage.RISING
    assert detector.revision_of("BV1") == 1

    # 用最新代际领取后再提交才成功。
    revision, _ = detector.claim_state("BV1")
    assert revision == 1
    assert detector.commit_state("BV1", TrendState(stage=Stage.DECLINING), claim_revision=1) is True
    assert detector.states["BV1"].stage == Stage.DECLINING
    assert detector.revision_of("BV1") == 2


def test_detect_is_read_only_and_idempotent() -> None:
    """GET 语义：detect 不推进代际，重复调用结果一致（M23）。"""
    rows = make_rows([1000, 1100, 1200])
    detector = LifecycleV2(as_of_epoch_s=E + 2 * DAY)

    first = detector.detect(rows)[0]
    for _ in range(20):
        again = detector.detect(rows)[0]
        assert (again.stage, again.confidence) == (first.stage, first.confidence)
        assert again.metrics == first.metrics

    assert detector.states == {}
    assert detector.revision_of("BV1") == 0


def test_initial_states_are_copied() -> None:
    """续算起点必须拷贝，外部修改种子不影响检测器内部代际。"""
    seed = TrendState(
        last_evaluation_epoch_s=E,
        prev_rate=100.0,
        stage=Stage.RISING,
        state_revision=7,
    )
    detector = LifecycleV2(as_of_epoch_s=E + DAY, initial_states={"BV1": seed})

    assert detector.revision_of("BV1") == 7
    assert detector.states["BV1"].stage == Stage.RISING

    seed.stage = Stage.DECLINING
    assert detector.states["BV1"].stage == Stage.RISING


def test_relative_change_none_when_base_is_zero() -> None:
    """上一窗为 0 时相对变化给 None，不输出 Infinity 或伪造 100%。"""
    analysis = Analysis(
        bvid="BV1", tid=4, title="", owner_mid=0, owner_name="", stage=Stage.OBSERVING,
        state=TrendState(), rates=[0.0, 5.0], coverage_ratio=1.0,
        coverage_state=CoverageState.FULL_SUPPORT, observed_windows=2, sample_count=2,
        staleness_hours=0.0, data_status="ok", unlocated_count=0, threshold_version="test",
    )

    assert analysis.rate_previous == 0.0
    assert analysis.rate_delta == pytest.approx(5.0)
    assert analysis.relative_change is None
