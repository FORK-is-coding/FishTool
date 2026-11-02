"""§7.1 通道 A：匹配成员的新增注意力趋势（双窗 P2 交集）。

明令禁止：``今天所有成员累计播放之和 − 昨天所有成员累计播放之和``（新入池百万播放旧视频会虚涨）。
本模块只累加**同一个 P2 内**逐成员由 02 质量分段与双边支撑得到的真实增量。

关键口径：
- ``W`` = 24h(86400) 或 2h(7200)；``T = window_end_s(as_of_s, W)``，UTC 零点锚定；
- 当前窗缺括点就等待，**不静默选旧 T 当当前**（``window_measure`` 右端缺括点返回 None）；
- ``U`` 以 ``S = T - 2W`` 为 knowledge_cutoff 冻结分母（撤销者留分母记原因）；
- ``P2`` = U 中两窗都完整有效的 BVID 交集；
- ``A = sum(delta_previous over P2)``、``B = sum(delta_current over P2)``；
- ``matched_growth = (B-A)/A``；**A=0 返回 null 并列绝对增量，不用 epsilon**。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .aggregator import PairedMeasure, matched_totals, validate_outer_inputs
from .config import DailyPolicy
from .windows import MemberRevision, SnapshotPoint, build_panel_for, latest_revision

#: 标题口径：只称「可比较成员观测增量」，不是话题全网播放。
OBSERVED_LABEL: str = "可比较成员观测增量"


def _decision_times(
    revisions_by_bvid: Mapping[str, Sequence[MemberRevision]], bvids: Sequence[str]
) -> list[int]:
    """取参与成员当前 revision 的 decision 时刻（供未来混入校验）。"""
    times: list[int] = []
    for bvid in bvids:
        current = latest_revision(revisions_by_bvid.get(bvid, ()))
        if current is not None:
            times.append(current.decision_at_s)
    return times


def channel_a_trend(
    revisions_by_bvid: Mapping[str, Sequence[MemberRevision]],
    points_by_bvid: Mapping[str, Sequence[SnapshotPoint]],
    *,
    as_of_s: int,
    policy: DailyPolicy | None = None,
    request_as_of_s: int | None = None,
    source_policy_hash: str | None = None,
    expected_source_policy_hash: str | None = None,
    replay: bool = False,
) -> dict[str, Any]:
    """计算通道 A 的双窗匹配增长。

    Args:
        revisions_by_bvid: bvid -> 该成员全部版本（含 proposed/accepted/rejected）。
        points_by_bvid: bvid -> 该成员快照点序列。
        as_of_s: 本次执行时钟上限。
        policy: 策略（缺省 ``DailyPolicy``）；W 由 ``policy.window_seconds`` 提供。
        request_as_of_s: 请求时刻（新鲜度用）；缺省等于 ``as_of_s``。
        source_policy_hash / expected_source_policy_hash: 来源策略核对。
        replay: 显式历史重放；为真时 ``mode='historical'``。

    Returns:
        dict: 含 ``window_end_s`` / ``eligible_member_count`` / ``paired_member_count`` /
        ``member_coverage`` / ``before_delta`` / ``after_delta`` / ``absolute_delta`` /
        ``matched_growth``（A=0 → ``None``）/ ``exclusions`` / ``newly_discovered_bvids`` /
        ``mode`` / ``policy_version`` / ``reason_codes``。

    Raises:
        ValueError: 非法负增量 / as_of 非法 / decision 越界 / 来源策略不一致。
    """
    policy = policy or DailyPolicy()
    T = build_panel_for(
        revisions_by_bvid,
        points_by_bvid,
        as_of_s=as_of_s,
        window_s=policy.window_seconds,
        n_windows=2,
        gap_max_s=policy.gap_max_seconds,
        require_coverage=policy.window_require_coverage,
    )

    panel = T
    rows: list[PairedMeasure] = []
    for bvid in panel.paired_bvids:
        deltas = panel.per_member_deltas[bvid]
        rows.append(
            PairedMeasure(
                bvid=bvid,
                owner_mid=panel.owner_by_bvid.get(bvid),
                before=deltas[0].delta,
                after=deltas[1].delta,
                quality_ok=True,
            )
        )

    # 外层校验：负增量 / as_of / decision 越界 / 来源策略（硬门），软问题收 reason_codes。
    outer = validate_outer_inputs(
        rows=rows,
        as_of_s=as_of_s,
        request_as_of_s=request_as_of_s,
        window_end_s=panel.window_end_s,
        decision_times_s=_decision_times(revisions_by_bvid, panel.paired_bvids),
        source_policy_hash=source_policy_hash,
        expected_source_policy_hash=expected_source_policy_hash,
        member_coverage=panel.member_coverage,
        min_member_coverage=policy.min_member_coverage,
    )

    totals = matched_totals(rows)
    return {
        "window_kind": "matched_delta",
        "window_end_s": panel.window_end_s,
        "knowledge_cutoff_s": panel.knowledge_cutoff_s,
        "label": OBSERVED_LABEL,
        "eligible_member_count": panel.eligible_member_count,
        "paired_member_count": totals["paired_member_count"],
        "paired_author_count": totals["paired_author_count"],
        "member_coverage": panel.member_coverage,
        "before_delta": totals["before_delta"],
        "after_delta": totals["after_delta"],
        "absolute_delta": totals["delta_difference"],
        "mean_delta": totals["mean_difference"],
        "matched_growth": totals["relative_change"],
        "exclusions": panel.exclusions,
        "newly_discovered_bvids": panel.newly_discovered_bvids,
        "mode": "historical" if replay else "realtime",
        "policy_version": policy.policy_version,
        "reason_codes": outer["reason_codes"],
    }


__all__ = ["channel_a_trend", "OBSERVED_LABEL"]
