"""04 · 第三批 d · 两小时早期信号（early 2h）测试。

覆盖 §16.2 / R5 规格表的 E 项：E11 / E47 / E54，外加 §8 冷启动最早可评估边界。

口径与红线：
    - 只**调用** ``modules/hotspot/events/`` 与 ``algorithm/window_metrics.py``；
    - **不修改** ``modules/hotspot/algorithm/`` 任何文件；
    - 全为纯计算断言：不触库、不触网、不看时钟（as_of / request_as_of 显式给定）。

关键约束（决定 fixture 形状）：
    - early 快窗 ``W=7200``、``gap_max=2400``（40 分钟）、``min_window_coverage=1.0``；
    - 因此快采点必须**稠密到 40 分钟以内**，且两端正好压在窗口边界上，
      否则窗口取不到括点、提前判 insufficient —— 这正是 ``_smoke_3d`` 里
      ``request_as_of_s`` 贴窗口边界翻车的那类坑，本文件按正确口径重建。
"""
from __future__ import annotations

import inspect

import pytest

from modules.hotspot.algorithm.window_metrics import window_end_s
from modules.hotspot.events.config import EARLY_W, EarlyPolicy
from modules.hotspot.events.early import earliest_evaluation_end_s, evaluate_early
from modules.hotspot.events.windows import MemberRevision, SnapshotPoint

#: 2026-09-01T00:00:00Z —— 86400 与 7200 的公共网格点。
T0 = 1788220800
#: 执行时钟；落在 early 网格上，使 ``window_end_s == T0``。
AS_OF = T0
#: early 快窗宽度（秒）。
EW = EARLY_W
#: 快采插点步长（秒）：远小于 40 分钟门，保证窗口满覆盖。
FAST_STEP = 1200


# --------------------------------------------------------------------------- 构造工具


def _rev(bvid: str, owner_mid: int, decision_at_s: int) -> MemberRevision:
    """构造一条 ``accepted`` 成员版本（revision=1）。"""
    return MemberRevision(
        bvid=bvid,
        owner_mid=owner_mid,
        status="accepted",
        revision=1,
        decision_at_s=decision_at_s,
        first_seen_s=decision_at_s,
    )


def _points(series: list[tuple[int, int]]) -> list[SnapshotPoint]:
    """把 ``(epoch_s, view)`` 序列转成有效快照点（``view_ok=True``）。"""
    return [SnapshotPoint(epoch_s=e, view=v, view_ok=True) for e, v in series]


def _render(anchors: list[tuple[int, int]], gap_s: int) -> list[tuple[int, int]]:
    """在锚点之间按 ``gap_s`` 步长线性插点，保证采样间隔满足 40 分钟门。

    Args:
        anchors: 已按 epoch 升序的 ``(epoch_s, view)`` 锚点（窗口边界处）。
        gap_s: 相邻点步长（秒）。

    Returns:
        list[tuple[int, int]]: 含锚点与插点的整数序列（累计播放单调不减）。
    """
    out: list[tuple[int, int]] = []
    for i, (epoch_s, view) in enumerate(anchors):
        out.append((epoch_s, int(view)))
        if i + 1 < len(anchors):
            nxt_epoch, nxt_view = anchors[i + 1]
            cur = epoch_s + gap_s
            while cur < nxt_epoch:
                ratio = (cur - epoch_s) / (nxt_epoch - epoch_s)
                out.append((cur, int(round(view + (nxt_view - view) * ratio))))
                cur += gap_s
    return out


def _fast_series(d1: int, d2: int, *, base: int = 100) -> list[tuple[int, int]]:
    """按两个 2h 快窗增量生成稠密点（两个窗口的分割点精确落在 T-W）。

    Args:
        d1: 前窗（``(T-2W, T-W]``）增量。
        d2: 本窗（``(T-W, T]``）增量。
        base: 最早锚点累计播放。

    Returns:
        list[tuple[int, int]]: 稠密点序列。
    """
    anchors = [
        (T0 - 2 * EW, base),
        (T0 - EW, base + d1),
        (T0, base + d1 + d2),
    ]
    return _render(anchors, FAST_STEP)


def _members(count: int, *, prefix: str = "F") -> dict[str, int]:
    """生成 ``{bvid: owner_mid}`` 映射（作者互不相同）。"""
    return {f"{prefix}{i}": i + 1 for i in range(1, count + 1)}


def _accepted_map(members: dict[str, int]) -> dict[str, list[MemberRevision]]:
    """把成员映射转成 accepted 版本映射（在知识截止前接受）。"""
    return {b: [_rev(b, mid, T0 - 4 * EW)] for b, mid in members.items()}


# --------------------------------------------------------------------------- E11


def test_E11_fast_window_gap_blocks_trend_but_keeps_discovery() -> None:
    """E11：2h 快窗中间 gap 50 分钟 → 不给快趋势；普通发现事实保留。"""
    members = _members(3)
    revisions = _accepted_map(members)
    # F9 在知识截止（T-2W）之后才接受 → 属于「发现」事实。
    revisions["F9"] = [_rev("F9", 9, T0 - EW)]

    # 快窗中间刻意留一个 3000 秒（50 分钟）空洞，其余间隔均为 40 分钟内。
    gapped = [
        (T0 - 14400, 100), (T0 - 12000, 105), (T0 - 9600, 105), (T0 - 7200, 110),
        (T0 - 4200, 115), (T0 - 1800, 120), (T0, 130),
    ]
    points = {b: _points(gapped) for b in members}
    points["F9"] = _points([(T0, 5)])

    sig = evaluate_early(
        revisions,
        points,
        fast_panel_bvids=list(members),
        as_of_s=AS_OF,
        request_as_of_s=AS_OF,
        fast_last_observation_s=AS_OF,
    )

    assert sig["status"] == "insufficient"
    assert "insufficient_fast_coverage" in sig["reason_codes"]
    assert sig["signal"] == "suppressed"
    assert sig["early_growth_signal"] is False
    assert sig["early_cooling_signal"] is False
    # 普通发现事实保留（不给快趋势，但发现线索不丢）。
    assert "F9" in sig["newly_discovered_bvids"]

    # §8.4 显式采样间隔门：实际间隔 > 40 分钟同样必须判 insufficient。
    healthy = {b: _points(_fast_series(10, 30)) for b in members}
    sig2 = evaluate_early(
        revisions,
        healthy,
        fast_panel_bvids=list(members),
        as_of_s=AS_OF,
        request_as_of_s=AS_OF,
        fast_last_observation_s=AS_OF,
        observed_sample_interval_s=3000,
    )
    assert sig2["status"] == "insufficient"
    assert "insufficient_fast_coverage" in sig2["reason_codes"]


# --------------------------------------------------------------------------- E47


def test_E47_independent_of_02_stage_and_rejects_bad_raw_quality() -> None:
    """E47：02 stage_current=false 但 04 快窗原始证据合格 → 可独立出信号；原始质量差则拒绝。"""
    # 独立性：early 入口不接受任何 02 单视频 stage / current_valid 入参。
    params = set(inspect.signature(evaluate_early).parameters)
    assert not ({"stage", "stage_current", "current_valid"} & params)

    members = _members(3, prefix="G")
    revisions = _accepted_map(members)

    # 原始快窗证据合格 → 可独立输出早期增长信号（不依赖 02 单视频 stage）。
    good = {b: _points(_fast_series(10, 30)) for b in members}
    ok = evaluate_early(
        revisions,
        good,
        fast_panel_bvids=list(members),
        as_of_s=AS_OF,
        request_as_of_s=AS_OF,
        fast_last_observation_s=AS_OF,
    )
    assert ok["status"] == "complete"
    assert ok["signal"] == "growth"
    assert ok["early_growth_signal"] is True
    assert ok["relative_change"] == pytest.approx(2.0)  # (30-10)/10
    assert ok["claimed_daily_confirmation"] is False

    # 反之：原始质量差（累计播放回撤断段）→ 拒绝聚合，不出快趋势。
    bad_series = [
        (T0 - 14400, 200), (T0 - 12000, 200), (T0 - 9600, 200), (T0 - 7200, 150),
        (T0 - 6000, 155), (T0 - 4800, 160), (T0 - 3600, 165),
        (T0 - 2400, 170), (T0 - 1200, 175), (T0, 180),
    ]
    bad = {b: _points(bad_series) for b in members}
    rejected = evaluate_early(
        revisions,
        bad,
        fast_panel_bvids=list(members),
        as_of_s=AS_OF,
        request_as_of_s=AS_OF,
        fast_last_observation_s=AS_OF,
    )
    assert rejected["status"] == "insufficient"
    assert rejected["early_growth_signal"] is False
    assert rejected["signal"] == "suppressed"


# --------------------------------------------------------------------------- E54


def test_E54_fast_coverage_below_gate() -> None:
    """E54：fast 覆盖不足 → status=insufficient 且 reason=insufficient_fast_coverage。"""
    members = _members(3)
    revisions = _accepted_map(members)
    points = {b: _points(_fast_series(10, 30)) for b in members}

    # F panel 里混入两个不属于面板的 bvid → |P2∩F|/|F| = 3/5 = 0.6 < 0.7。
    fast_panel = list(members) + ["X1", "X2"]
    sig = evaluate_early(
        revisions,
        points,
        fast_panel_bvids=fast_panel,
        as_of_s=AS_OF,
        request_as_of_s=AS_OF,
        fast_last_observation_s=AS_OF,
    )

    assert sig["status"] == "insufficient"
    assert sig["reason_codes"] == ["insufficient_fast_coverage"]  # 固定枚举，前端可直接映射
    assert sig["fast_coverage"] == pytest.approx(0.6)
    assert sig["paired_fast_count"] == 3
    assert sig["signal"] == "suppressed"


# --------------------------------------------------------------------------- 冷启动


def test_early_cold_start_earliest_evaluation_end() -> None:
    """§8 冷启动：最早可评估右边界 = ``ceil_grid(accepted + 2W, W)``。"""
    assert earliest_evaluation_end_s(T0) == T0 + 2 * EW
    # 非网格时刻向上取整到下一个 early 边界。
    assert earliest_evaluation_end_s(T0 + 1) == T0 + 3 * EW
    # 与 ``window_end_s`` 口径一致（同一 UTC 网格）。
    assert earliest_evaluation_end_s(T0) == window_end_s(T0 + 2 * EW, EW)

    # 显式 policy 只用于校验窗口宽度一致性。
    assert earliest_evaluation_end_s(T0, policy=EarlyPolicy()) == T0 + 2 * EW
