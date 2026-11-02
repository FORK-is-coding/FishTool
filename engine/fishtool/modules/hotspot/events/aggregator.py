"""``events/aggregator.py`` —— 04 纯计算契约（§9.2 原文照搬 + §7.2.2 日级方向纯函数）。

本模块**只有纯函数/纯数据**：不触库、不触网、不看时钟。DB 与外部 API 由
:mod:`modules.hotspot.events.service` 负责，二者解耦，便于把聚合内核单独回放。

§9.2 原文（``PairedMeasure`` / ``matched_totals``）**逐字照抄**，不做发挥：
重复 BVID 直接 ``raise ValueError('duplicate_bvid')``，避免静默双计。
§7.2.2 原文（``event_direction`` / ``interpret_triplet``）同样照抄，并额外给出
调用前的**外层校验**（未知作者 / 覆盖 / 来源策略 / 浓度 / 坏点 / as_of / 新鲜度）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# ===========================================================================
# §9.2 纯计算契约（原文照搬）
# ===========================================================================


@dataclass(frozen=True)
class PairedMeasure:
    """一个已配对成员的「前窗 / 本窗」增量测量。

    Args:
        bvid: 视频 BV 号（**同一份 rows 内必须唯一**）。
        owner_mid: UP 主 mid；``None`` 表示作者不可核实（进未知作者桶）。
        before: 前窗有效增量；``None`` 表示该窗无效。
        after: 本窗有效增量；``None`` 表示该窗无效。
        quality_ok: 两窗是否都通过 02 质量分段与双边支撑。
    """

    bvid: str
    owner_mid: int | None
    before: float | None
    after: float | None
    quality_ok: bool


def matched_totals(rows: list[PairedMeasure]) -> dict:
    """汇总配对成员的前/后窗增量（§9.2 原文照抄，逐字实现）。

    Args:
        rows: 已按 BVID 去重的配对测量；**重复 BVID 直接报错**。

    Returns:
        dict: ``paired_member_count`` / ``paired_author_count`` / ``before_delta`` /
        ``after_delta`` / ``delta_difference`` / ``relative_change`` /
        ``mean_difference``。A=0（前窗为 0）时 ``relative_change`` 为 ``None``，
        **不用 epsilon 制造爆炸百分比**。

    Raises:
        ValueError: ``rows`` 中存在重复 BVID（``'duplicate_bvid'``）。
    """
    # rows必须已按BVID去重，重复输入直接报错，避免静默双计
    if len({r.bvid for r in rows}) != len(rows):
        raise ValueError("duplicate_bvid")
    valid = [
        r
        for r in rows
        if r.quality_ok and r.before is not None and r.after is not None
    ]
    a = sum(r.before for r in valid)
    b = sum(r.after for r in valid)
    n = len(valid)
    return {
        "paired_member_count": n,
        "paired_author_count": len({r.owner_mid for r in valid if r.owner_mid}),
        "before_delta": a if n else None,
        "after_delta": b if n else None,
        "delta_difference": b - a if n else None,
        "relative_change": (b - a) / a if n and a > 0 else None,
        "mean_difference": (b - a) / n if n else None,
    }


# ===========================================================================
# §7.2.2 日级事件方向共用纯函数（原文照抄）
# ===========================================================================


def event_direction(
    base, current, n, *, absolute_per_video=20.0, relative_min=0.2
):
    """单对窗口的方向纯函数（§7.2.2 原文）。

    Args:
        base: 基线窗有效总量（非负有限）。
        current: 对比窗有效总量（非负有限）。
        n: panel 规模（正整数视频数）。
        absolute_per_video: 每视频绝对阈值。
        relative_min: 相对阈值。

    Returns:
        ``'up'`` / ``'down'`` / ``'stable'``。

    Raises:
        ValueError: ``n`` 非正整数（``'invalid_panel_size'``），或基/现值为
            bool / 非数值 / 非有限 / 负数（``'invalid_delta'``）。
    """
    if type(n) is not int or n < 1:
        raise ValueError("invalid_panel_size")
    if any(
        isinstance(x, bool) or not isinstance(x, (int, float))
        or not math.isfinite(x) or x < 0
        for x in (base, current)
    ):
        raise ValueError("invalid_delta")
    diff = current - base
    if abs(diff) < absolute_per_video * n:
        return "stable"
    if base == 0:
        return "up" if current > 0 else "stable"
    if abs(diff) / base < relative_min:
        return "stable"
    return "up" if diff > 0 else "down"


def interpret_triplet(a, b, c, n, *, absolute_per_video=20.0, relative_min=0.2):
    """三窗方向解释纯函数（§7.2.2 原文）。

    调用前必须通过 P3 身份、非负有效指标、时序、覆盖和样本门；本函数**只**解释方向。

    Args:
        a: 最早窗总量（基线）。
        b: 中间窗总量。
        c: 最近窗总量。
        n: panel 视频数。
        absolute_per_video: 每视频绝对阈值。
        relative_min: 相对阈值。

    Returns:
        ``(topic_phase, stage_reason)``。
    """
    direction = lambda x, y: event_direction(
        x, y, n, absolute_per_video=absolute_per_video, relative_min=relative_min
    )
    if a == b == c == 0:
        return "stable", "stable_no_attention"
    d1, d2 = direction(a, b), direction(a, c)
    if d1 == d2 == "up":
        return "rising", "rising_low_base" if a == 0 else "confirmed_growth"
    if d1 == d2 == "down":
        return "declining", "confirmed_cooling"
    if direction(a, b) == direction(b, c) == "stable":
        return "stable", "stable_observed"
    return "undetermined", "turning_signal"


# ===========================================================================
# 外层校验（§1.6 / §9.2 L880：函数本身不判全部质量，外层必须补齐）
# ===========================================================================

#: 外层**硬错误**（接到即 ``raise``，不允许静默降级）。
HARD_ERROR_CODES: tuple = (
    "invalid_negative_delta",
    "invalid_as_of_s",
    "decision_after_as_of",
    "source_policy_mismatch",
    "unknown_source_policy",
)


def assert_non_negative_deltas(rows: Iterable[PairedMeasure]) -> None:
    """拒绝非法负增量 / 非有限值（§9.2 L880：非负有限 delta 由 window_metrics 保证）。

    ``window_metrics`` 只应产出 ``>= 0`` 的有限 delta；外层若接到负值或 NaN/Inf，
    说明上游质量分段被绕过，必须**拒绝**而非静默接受。

    Args:
        rows: 待校验的配对测量集合。

    Raises:
        ValueError: 任一**参与聚合**的增量是负数或非有限（``'invalid_negative_delta'``）。
    """
    for row in rows:
        for value in (row.before, row.after):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("invalid_negative_delta")
            if not math.isfinite(value) or value < 0:
                raise ValueError("invalid_negative_delta")


def validate_outer_inputs(
    *,
    rows: Iterable[PairedMeasure],
    as_of_s: Any,
    request_as_of_s: Any = None,
    window_end_s: Any = None,
    decision_times_s: Iterable[int] = (),
    source_policy_hash: str | None = None,
    expected_source_policy_hash: str | None = None,
    member_coverage: float | None = None,
    min_member_coverage: float | None = None,
    top_author_share: float | None = None,
    unknown_author_delta_share: float | None = None,
    stale: bool = False,
    sampling_changed: bool = False,
) -> dict[str, Any]:
    """外层综合校验：把**硬错误**抛成 ``ValueError``，把**软问题**收成 ``reason_codes``。

    覆盖 §1.6 / §9.2 要求：未知作者数量、member coverage、来源策略、样本浓度、
    坏点、as_of、decision 时间和数据新鲜度。

    Args:
        rows: 参与聚合的配对测量（用于负增量/坏点检查）。
        as_of_s: 本次执行时钟上限（必须合法整数 epoch）。
        request_as_of_s: 请求时刻（用于新鲜度）；缺省等于 ``as_of_s``。
        window_end_s: 所用网格边界；与 ``request_as_of_s`` 一起判新鲜度。
        decision_times_s: 参与成员的 decision 时刻；晚于 ``as_of_s`` 判为未来混入。
        source_policy_hash / expected_source_policy_hash: 来源策略一致性核对。
        member_coverage: 成员覆盖率（paired / eligible）。
        min_member_coverage: 覆盖率下限。
        top_author_share: top1 作者占本次已观测增量比例。
        unknown_author_delta_share: 未知作者有效增量占比。
        stale: 是否已判定过期（由调用方按时钟算好）。
        sampling_changed: 发现计划不可比（扩页/换关键词）导致的变化。

    Returns:
        dict: ``{'reason_codes': [...], 'hard_ok': True, 'attention_present': bool}``。

    Raises:
        ValueError: ``invalid_negative_delta`` / ``invalid_as_of_s`` /
            ``decision_after_as_of`` / ``source_policy_mismatch`` /
            ``unknown_source_policy``。
    """
    # ---- 硬门 0：非法负增量 / 坏点 ----
    assert_non_negative_deltas(rows)

    # ---- 硬门 1：as_of 合法 ----
    if type(as_of_s) is not int or isinstance(as_of_s, bool) or as_of_s < 0:
        raise ValueError("invalid_as_of_s")
    request_as_of = as_of_s if request_as_of_s is None else request_as_of_s
    if type(request_as_of) is not int or isinstance(request_as_of, bool) or request_as_of < 0:
        raise ValueError("invalid_as_of_s")

    # ---- 硬门 2：decision 时间不得越过本次执行时钟（未来混入） ----
    for decision_s in decision_times_s:
        if decision_s is None:
            continue
        if type(decision_s) is not int or decision_s > as_of_s:
            raise ValueError("decision_after_as_of")

    # ---- 硬门 3：来源策略一致性 ----
    if expected_source_policy_hash is not None:
        if source_policy_hash is None:
            raise ValueError("unknown_source_policy")
        if source_policy_hash != expected_source_policy_hash:
            raise ValueError("source_policy_mismatch")

    # ---- 软问题：收集成 reason_codes（不抛异常，仍出原始数字） ----
    reason_codes: list[str] = []
    if member_coverage is not None and min_member_coverage is not None:
        if member_coverage < min_member_coverage:
            reason_codes.append("member_coverage_below_min")
    if top_author_share is not None and top_author_share > 0.7:
        reason_codes.append("concentrated")
    if unknown_author_delta_share is not None and unknown_author_delta_share > 0.2:
        reason_codes.append("author_coverage_insufficient")
    if stale:
        reason_codes.append("stale_evidence")
    if sampling_changed:
        reason_codes.append("sampling_changed")

    return {
        "reason_codes": reason_codes,
        "hard_ok": True,
        "attention_present": bool(list(rows)),
    }


def sample_gate_passed(
    *,
    video_count: int,
    author_count: int,
    member_coverage: float | None,
    min_videos: int,
    min_authors: int,
    min_member_coverage: float,
) -> bool:
    """样本门判定（§7.2 L681）：``>=3 视频``、``>=2 已知作者``、覆盖率 ``>=0.7``。

    不达门槛仍显示原始明细与单视频趋势，但**不称多作者话题确认**。

    Args:
        video_count: panel 视频数。
        author_count: panel **已知**作者数。
        member_coverage: 成员覆盖率（None 视为不达）。
        min_videos / min_authors / min_member_coverage: 门槛。

    Returns:
        bool: 是否通过样本门。
    """
    if member_coverage is None:
        return False
    return (
        video_count >= min_videos
        and author_count >= min_authors
        and member_coverage >= min_member_coverage
    )


__all__ = [
    "PairedMeasure",
    "matched_totals",
    "event_direction",
    "interpret_triplet",
    "assert_non_negative_deltas",
    "validate_outer_inputs",
    "sample_gate_passed",
    "HARD_ERROR_CODES",
]
