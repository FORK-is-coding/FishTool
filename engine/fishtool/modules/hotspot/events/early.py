"""§8 两小时早期信号（early 2h）：补时效，但**不冒充日级确认**。

``early2h`` 与 ``daily24h`` **并存，状态和配置绝不互相覆盖**。早期信号 =
「最近两个完整 2h 窗内已观测的变化」，**不是预测未来**；界面只能显示
``early_growth_signal`` / ``early_cooling_signal``，**不能写「已确认上升期」**。

关键口径：
- 早期快窗用**原始 delta**；若显示速率须标「播放/小时」，不得乘 12 称日实测；
- 快通道质量门用 ``|P2∩F|/|F| >= 0.7``，另显示事件覆盖 ``|F|/|U|``；
  **不能用 U 当快通道覆盖分母**（会让 20 成员事件永远无法达标）；
- 低基数：A>0 时 绝对平均变化>=20 **且** 相对>=50% 才算增强；A=0 且 B/N>=20 →
  ``emerging_from_zero``，``relative_change`` 仍 NULL；A=B=0 → 仅 no_attention；
  **不为了输出方向而除 epsilon**；
- 冷启动：2h 是计算粒度，首次通常需 4—6 小时再加右端实际采样等待；API 返回
  ``earliest_evaluation_end_s`` 与缺失条件；
- ``fast_ttl_seconds`` **从证据 window_end_s 算**，不从刷新时间算；
- §8.4：实际采样超 40 分钟间隔 → ``status=insufficient``、``reason`` 含
  ``insufficient_fast_coverage``，保留发现线索、停快趋势标签。

``F`` panel 的**冻结/激活/容量 12/持久化**属第四批；本批 ``F`` 只作为**传入输入**。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .aggregator import PairedMeasure, event_direction
from .config import EARLY_W, EarlyPolicy
from .windows import (
    MemberRevision,
    PanelResult,
    SnapshotPoint,
    build_panel_for,
    status_at,
)

#: 信号枚举。
SIGNAL_GROWTH = "growth"
SIGNAL_COOLING = "cooling"
SIGNAL_EMERGING_FROM_ZERO = "emerging_from_zero"
SIGNAL_NO_ATTENTION = "no_attention"
SIGNAL_NO_SIGNIFICANT_CHANGE = "no_significant_change"
SIGNAL_SUPPRESSED = "suppressed"

#: 速率展示口径：若显示速率必须标「播放/小时」，不得乘 12 称日实测。
RATE_LABEL: str = "播放/小时"


def _ceil_to_grid(value: int, window_s: int) -> int:
    """把时刻向上取整到 ``window_s`` 网格边界（UTC 零点锚定）。"""
    return ((value + window_s - 1) // window_s) * window_s


def earliest_evaluation_end_s(
    accepted_s: int, *, window_s: int = EARLY_W, policy: EarlyPolicy | None = None
) -> int:
    """冷启动：给出「最早可评估的窗右边界」。

    需要「最早比较窗起点 S = T-2W 不早于最早 accepted」，故
    ``T = ceil_grid(accepted_s + 2W, W)``。2h 只是计算粒度，**不保证 2h 后就有信号**。

    Args:
        accepted_s: 成员最早 accepted 时刻（epoch 秒）。
        window_s: 窗口宽度（秒），缺省 7200。
        policy: 可选策略（仅用于校验窗口宽度一致性）。

    Returns:
        int: 最早可评估窗右边界（epoch 秒）。

    Raises:
        ValueError: ``accepted_s`` 非法（非 int / bool / 负数）。
    """
    if type(accepted_s) is not int or isinstance(accepted_s, bool) or accepted_s < 0:
        raise ValueError("invalid_accepted_s")
    if policy is not None:
        window_s = policy.window_seconds
    if type(window_s) is not int or window_s <= 0:
        raise ValueError("invalid_window_s")
    return _ceil_to_grid(accepted_s + 2 * window_s, window_s)


def _panel_earliest(
    panel: PanelResult,
    revisions_by_bvid: Mapping[str, Sequence[MemberRevision]],
    policy: EarlyPolicy,
) -> int:
    """用 U 内成员在 S 时的实际 accepted 时刻反推最早可评估窗右边界。"""
    latest_accepted = 0
    for bvid in panel.eligible_bvids:
        at_cutoff = status_at(revisions_by_bvid.get(bvid, ()), panel.knowledge_cutoff_s)
        if at_cutoff is not None:
            latest_accepted = max(latest_accepted, at_cutoff.decision_at_s)
    return earliest_evaluation_end_s(latest_accepted, policy=policy)


def evaluate_early(
    revisions_by_bvid: Mapping[str, Sequence[MemberRevision]],
    points_by_bvid: Mapping[str, Sequence[SnapshotPoint]],
    *,
    fast_panel_bvids: Sequence[str],
    as_of_s: int,
    policy: EarlyPolicy | None = None,
    request_as_of_s: int | None = None,
    fast_last_observation_s: int | None = None,
    observed_sample_interval_s: int | None = None,
    replay: bool = False,
) -> dict[str, Any]:
    """评估两小时早期信号（含低基数三分支 / 覆盖门 / TTL / 冷启动）。

    Args:
        revisions_by_bvid / points_by_bvid: 成员与快照数据。
        fast_panel_bvids: 传入的快 panel ``F``（**本批不冻结，只作输入**）。
        as_of_s: 本次执行时钟上限。
        policy: early 策略（各阈值不与日级共享）。
        request_as_of_s: 请求时刻（新鲜度 / TTL 用）；缺省等于 ``as_of_s``。
        fast_last_observation_s: 快采末次观察时刻；与 40min 门比较。
        observed_sample_interval_s: 实际快采间隔；>40min 判 ``insufficient_fast_coverage``。
        replay: 显式历史重放。

    Returns:
        dict: ``status`` / ``reason_codes`` / ``signal`` / ``early_growth_signal`` /
        ``early_cooling_signal`` / ``fast_coverage`` / ``event_coverage`` / ``ttl_*`` /
        ``earliest_evaluation_end_s`` / ``missing_conditions`` 等。
    """
    policy = policy or EarlyPolicy()
    request_as_of = as_of_s if request_as_of_s is None else request_as_of_s
    panel = build_panel_for(
        revisions_by_bvid,
        points_by_bvid,
        as_of_s=as_of_s,
        window_s=policy.window_seconds,
        n_windows=2,
        gap_max_s=policy.gap_max_seconds,
        require_coverage=policy.min_window_coverage,
    )

    fast_panel = [bvid for bvid in fast_panel_bvids]
    fast_set = set(fast_panel)
    paired = set(panel.paired_bvids)
    intersected = sorted(paired & fast_set)

    fast_coverage: float | None
    if fast_panel:
        fast_coverage = len(intersected) / len(fast_panel)
    else:
        fast_coverage = None
    event_coverage: float | None = (
        len(fast_set) / panel.eligible_member_count if panel.eligible_member_count else None
    )

    reason_codes: list[str] = []
    # 快通道覆盖不足（含 F 为空 / |P2∩F|/|F| 低于门）。
    if fast_coverage is None or fast_coverage < policy.fast_coverage_min:
        reason_codes.append("insufficient_fast_coverage")
    # §8.4：实际采样超 40 分钟 → 覆盖不足可见。
    if observed_sample_interval_s is not None and observed_sample_interval_s > policy.gap_max_seconds:
        reason_codes.append("insufficient_fast_coverage")

    paired_author_count = len(
        {
            panel.owner_by_bvid.get(bvid)
            for bvid in intersected
            if panel.owner_by_bvid.get(bvid)
        }
    )
    if len(intersected) < policy.min_paired_videos:
        reason_codes.append("insufficient_sample")
    if paired_author_count < policy.min_paired_authors:
        reason_codes.append("insufficient_sample")

    # TTL / 新鲜度（均相对证据 window_end_s，不从刷新时间算）。
    ttl_expires_s = panel.window_end_s + policy.fast_ttl_seconds
    ttl_active = request_as_of <= ttl_expires_s
    window_stale = (request_as_of - panel.window_end_s) > policy.max_staleness_seconds
    last_stale = (
        fast_last_observation_s is not None
        and (request_as_of - fast_last_observation_s) > policy.max_staleness_seconds
    )
    if not ttl_active:
        reason_codes.append("stale_evidence")
    if window_stale or last_stale:
        reason_codes.append("stale_evidence")

    missing_conditions: list[str] = []
    if not intersected:
        missing_conditions.append("no_paired_fast_member")
    if len(intersected) < policy.min_paired_videos:
        missing_conditions.append("insufficient_paired_videos")
    if paired_author_count < policy.min_paired_authors:
        missing_conditions.append("insufficient_paired_authors")
    if not fast_panel:
        missing_conditions.append("fast_panel_empty")
    if fast_last_observation_s is None:
        missing_conditions.append("right_end_support_sample")

    base: dict[str, Any] = {
        "window_kind": "early2h",
        "window_end_s": panel.window_end_s,
        "knowledge_cutoff_s": panel.knowledge_cutoff_s,
        "fast_panel_size": len(fast_panel),
        "fast_coverage": fast_coverage,
        "event_coverage": event_coverage,
        "paired_fast_count": len(intersected),
        "paired_fast_author_count": paired_author_count,
        "eligible_member_count": panel.eligible_member_count,
        "rate_label": RATE_LABEL,
        "raw_delta_used": True,
        "newly_discovered_bvids": panel.newly_discovered_bvids,
        "exclusions": panel.exclusions,
        "fast_ttl_seconds": policy.fast_ttl_seconds,
        "ttl_expires_s": ttl_expires_s,
        "ttl_active": ttl_active,
        "earliest_evaluation_end_s": _panel_earliest(panel, revisions_by_bvid, policy),
        "missing_conditions": missing_conditions,
        "mode": "historical" if replay else "realtime",
        "policy_version": policy.policy_version,
        # 早期信号绝不冒充日级确认。
        "claimed_daily_confirmation": False,
    }

    if reason_codes:
        # 保留发现线索，停快趋势标签（不给方向）。
        return {
            **base,
            "status": "insufficient",
            "signal": SIGNAL_SUPPRESSED,
            "early_growth_signal": False,
            "early_cooling_signal": False,
            "early_action_hint": None,
            "before_delta": None,
            "after_delta": None,
            "mean_delta": None,
            "relative_change": None,
            "reason_codes": sorted(set(reason_codes)),
        }

    # ---- 计算 P2∩F 的两窗原始增量 ----
    a = sum(panel.per_member_deltas[bvid][0].delta or 0.0 for bvid in intersected)
    b = sum(panel.per_member_deltas[bvid][1].delta or 0.0 for bvid in intersected)
    n = len(intersected)
    mean_delta = (b - a) / n if n else None

    if a > 0:
        relative: float | None = (b - a) / a
        if (
            mean_delta is not None
            and abs(mean_delta) >= policy.absolute_mean_delta_min
            and relative >= policy.relative_change_min
        ):
            signal = SIGNAL_GROWTH
        elif (
            mean_delta is not None
            and abs(mean_delta) >= policy.absolute_mean_delta_min
            and relative <= -policy.relative_change_min
        ):
            signal = SIGNAL_COOLING
        else:
            # 低值下降未达绝对门只作无显著变化，不为了输出方向而除 epsilon。
            signal = SIGNAL_NO_SIGNIFICANT_CHANGE
    elif a == 0:
        if n and (b / n) >= policy.absolute_mean_delta_min:
            signal = SIGNAL_EMERGING_FROM_ZERO
        elif b == 0:
            signal = SIGNAL_NO_ATTENTION
        else:
            signal = SIGNAL_NO_SIGNIFICANT_CHANGE
        relative = None  # A=0：相对变化仍 NULL，不塞常规百分比比较。
    else:  # 理论不可达（负增量已在上面被拒绝）
        relative = None
        signal = SIGNAL_NO_SIGNIFICANT_CHANGE

    growth = signal in (SIGNAL_GROWTH, SIGNAL_EMERGING_FROM_ZERO)
    cooling = signal == SIGNAL_COOLING
    fresh = not reason_codes
    return {
        **base,
        "status": "complete",
        "signal": signal,
        "early_growth_signal": growth,
        "early_cooling_signal": cooling,
        "early_action_hint": "prepare_or_pilot" if (growth and fresh) else None,
        "before_delta": a,
        "after_delta": b,
        "mean_delta": mean_delta,
        "relative_change": relative,
        "reason_codes": [],
    }


__all__ = [
    "evaluate_early",
    "earliest_evaluation_end_s",
    "SIGNAL_GROWTH",
    "SIGNAL_COOLING",
    "SIGNAL_EMERGING_FROM_ZERO",
    "SIGNAL_NO_ATTENTION",
    "SIGNAL_NO_SIGNIFICANT_CHANGE",
    "SIGNAL_SUPPRESSED",
    "RATE_LABEL",
]
