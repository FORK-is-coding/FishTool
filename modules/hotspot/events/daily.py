"""§7.2 / §7.2.1 日级三窗（P3）与八条顺序判定。

``aggregate_daily_triplet()`` 出 A/B/C + 作者数 + 覆盖 + 集中度 → ``interpret_daily()``
按 §7.2.1 **八条顺序**判定；三窗方向部分直接复用 §7.2.2 的 ``interpret_triplet``。

关键口径：
- ``P3`` = 三个连续完整日窗都有效、且在最早日窗开始前已 known/accepted 的成员交集；
- 阈值：绝对 **20 播放/视频/天** + 总量相对 **20%**（版本化业务规则）；
- 样本门：``>=3 视频``、``>=2 已知作者``、覆盖率 ``>=0.7``；
- 只有 已知作者>=2 且 top_author_share<=0.7 且 未知贡献<=0.2 才过广泛程度门，**仍只称「多作者样本」**；
- ``canonical fingerprint`` 必须含排序后的 VideoStats ID 及必要值、成员 revision、规则 hash、
  panel_id、窗口起止、政策 hash —— **仅依赖可变行 ID 不够**；
- 非实时显式重放标 ``mode=historical``，**不得自动进入即时机会队列**；
- 跨级新鲜度：daily 须 ``request_as_of_s - window_end_s <= 36h``。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from .aggregator import (
    assert_non_negative_deltas,
    event_direction,
    interpret_triplet,
    sample_gate_passed,
    validate_outer_inputs,
)
from .config import DailyPolicy
from .windows import (
    MemberRevision,
    PanelResult,
    SnapshotPoint,
    build_panel_for,
    latest_revision,
)

_SCOPE = "observed_event_members"


def _as_float(value: float | None) -> float | None:
    """把可选数值归一为 float（保留 ``None``，不填 0）。"""
    return None if value is None else float(value)


def _author_stats(
    panel: PanelResult, *, latest_index: int
) -> tuple[dict[int, float], float, float, int | None]:
    """按作者聚合**最近一个可用窗**的已观测增量。

    Args:
        panel: panel 结果。
        latest_index: 最近窗在 ``per_member_deltas`` 中的下标。

    Returns:
        ``(per_author_delta, known_delta, unknown_delta, top_author_share_denom)``。
    """
    per_author: dict[int, float] = {}
    known = 0.0
    unknown = 0.0
    for bvid in panel.paired_bvids:
        deltas = panel.per_member_deltas[bvid]
        value = deltas[latest_index].delta or 0.0
        owner = panel.owner_by_bvid.get(bvid)
        if owner is None:
            unknown += value
        else:
            per_author[owner] = per_author.get(owner, 0.0) + value
            known += value
    return per_author, known, unknown, None


def canonical_fingerprint(
    *,
    video_rows: Iterable[Mapping[str, Any]],
    member_rows: Iterable[Mapping[str, Any]],
    rule_hash: str | None,
    panel_id: str | None,
    window_start_s: int,
    window_end_s: int,
    policy_hash: str | None,
) -> str:
    """生成 canonical 输入指纹（排序后序列化再 sha256）。

    Args:
        video_rows: 每项至少含 ``id`` / ``bvid`` / ``view``（VideoStats 行）。
        member_rows: 每项至少含 ``bvid`` / ``revision``。
        rule_hash: 规则 hash。
        panel_id: panel 标识。
        window_start_s / window_end_s: 窗口起止。
        policy_hash: 政策 hash。

    Returns:
        64 位 hex 指纹字符串。
    """
    videos = sorted(
        (
            {
                "id": row.get("id"),
                "bvid": row.get("bvid"),
                "view": row.get("view"),
            }
            for row in video_rows
        ),
        key=lambda r: (str(r["bvid"]), str(r["id"])),
    )
    members = sorted(
        ({"bvid": row.get("bvid"), "revision": row.get("revision")} for row in member_rows),
        key=lambda r: (str(r["bvid"]), int(r["revision"] or 0)),
    )
    payload = {
        "videos": videos,
        "members": members,
        "rule_hash": rule_hash,
        "panel_id": panel_id,
        "window_start_s": window_start_s,
        "window_end_s": window_end_s,
        "policy_hash": policy_hash,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def interpret_daily(
    *,
    a: float | None,
    b: float | None,
    c: float | None,
    window_count: int,
    panel_size: int,
    video_count: int,
    author_count: int,
    member_coverage: float | None,
    top_author_share: float | None,
    unknown_author_delta_share: float | None,
    author_coverage_insufficient: bool,
    concentrated: bool,
    policy: DailyPolicy,
) -> dict[str, Any]:
    """按 §7.2.1 八条顺序解释日级三窗（不把未知值填 0）。

    Args:
        a / b / c: 最早 / 中间 / 最近窗总量（缺窗为 ``None``）。
        window_count: 可用完整窗数（0/1/2/3）。
        panel_size: P3 视频数 ``N``。
        video_count / author_count / member_coverage: 样本门三要素。
        top_author_share: top1 作者占比（分母 0 → ``None``）。
        unknown_author_delta_share: 未知作者占比（分母 0 → ``None``）。
        author_coverage_insufficient: 是否未知作者过多 / 无法核实作者数。
        concentrated: 是否 ``top_author_share>0.7``。
        policy: 日级策略（阈值各用各的，不与 early 共享）。

    Returns:
        dict: ``topic_phase`` / ``stage_reason`` / ``attention_present`` / ``low_base`` /
        ``relative_change`` / ``sample_gate_passed`` / ``confirmed_multi_author`` /
        ``broad_diffusion`` / ``reason_codes`` / ``scope``。
    """
    gate = sample_gate_passed(
        video_count=video_count,
        author_count=author_count,
        member_coverage=member_coverage,
        min_videos=policy.min_videos,
        min_authors=policy.min_authors,
        min_member_coverage=policy.min_member_coverage,
    )
    reason_codes: list[str] = []
    if not gate:
        reason_codes.append("insufficient_sample")
    if concentrated:
        reason_codes.append("concentrated")
    if author_coverage_insufficient:
        reason_codes.append("author_coverage_insufficient")

    # 广泛程度提示门：已知作者>=2、top_author_share<=0.7、未知贡献<=0.2（仍只称「多作者样本」）。
    broad_diffusion = (
        author_count >= policy.min_authors
        and not author_coverage_insufficient
        and (top_author_share is None or top_author_share <= policy.top_author_share_max)
        and (unknown_author_delta_share is None or unknown_author_delta_share <= policy.unknown_author_share_max)
    )

    base = {
        "scope": _SCOPE,
        "panel_size": panel_size,
        "video_count": video_count,
        "author_count": author_count,
        "member_coverage": member_coverage,
        "top_author_share": top_author_share,
        "unknown_author_delta_share": unknown_author_delta_share,
        "sample_gate_passed": gate,
        "broad_diffusion": broad_diffusion,
        "concentrated": concentrated,
        "author_coverage_insufficient": author_coverage_insufficient,
        "reason_codes": reason_codes,
    }

    # 条件 1：没有完整有效窗口 → undetermined/collecting；不把未知值填 0。
    if window_count <= 0:
        return {
            **base,
            "topic_phase": "undetermined",
            "stage_reason": "collecting",
            "attention_present": False,
            "low_base": False,
            "relative_change": None,
            "confirmed_multi_author": False,
        }

    # 条件 2：只有一个完整窗口 → emerging（须过样本门且平均每视频 delta>=20）。
    if window_count == 1:
        mean = (a / panel_size) if (a is not None and panel_size) else None
        if gate and mean is not None and mean >= policy.absolute_per_video:
            phase, reason, attention = "emerging", "emerging", True
        else:
            phase, reason, attention = "undetermined", "collecting", False
        return {
            **base,
            "topic_phase": phase,
            "stage_reason": reason,
            "attention_present": attention,
            "low_base": False,
            "relative_change": None,
            "confirmed_multi_author": False,
        }

    # 条件 3：只有两个窗口可比 → 只返回 pending / stable_observed，不写确认阶段。
    if window_count == 2:
        direction = event_direction(
            a,
            b,
            panel_size,
            absolute_per_video=policy.absolute_per_video,
            relative_min=policy.relative_min,
        )
        phase, reason = {
            "up": ("rising", "rising_pending"),
            "down": ("declining", "declining_pending"),
            "stable": ("stable", "stable_observed"),
        }[direction]
        return {
            **base,
            "topic_phase": phase,
            "stage_reason": reason,
            "attention_present": bool((a or 0) + (b or 0)),
            "low_base": False,
            "relative_change": None,
            "confirmed_multi_author": False,
        }

    # 条件 4-8：三窗同 P3 可比 —— 直接复用 §7.2.2 interpret_triplet。
    phase, reason = interpret_triplet(
        a,
        b,
        c,
        panel_size,
        absolute_per_video=policy.absolute_per_video,
        relative_min=policy.relative_min,
    )
    attention_present = reason != "stable_no_attention"
    low_base = reason == "rising_low_base"
    relative_change = None
    if a is not None and a > 0 and phase in ("rising", "declining"):
        relative_change = (b - a) / a if b is not None else None
    confirmed = gate and reason in ("confirmed_growth", "confirmed_cooling", "rising_low_base")
    return {
        **base,
        "topic_phase": phase,
        "stage_reason": reason,
        "attention_present": attention_present,
        "low_base": low_base,
        "relative_change": relative_change,
        "confirmed_multi_author": confirmed,
    }


def aggregate_daily_triplet(
    revisions_by_bvid: Mapping[str, Sequence[MemberRevision]],
    points_by_bvid: Mapping[str, Sequence[SnapshotPoint]],
    *,
    as_of_s: int,
    policy: DailyPolicy | None = None,
    request_as_of_s: int | None = None,
    source_policy_hash: str | None = None,
    expected_source_policy_hash: str | None = None,
    replay: bool = False,
    panel_id: str | None = None,
    rule_hash: str | None = None,
    video_provenance: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """聚合日级三窗并解释（同一 P3 内算 A/B/C，禁止拼不同成员集合的三个 sum）。

    自动退级：三窗都有效用 P3；否则依次尝试最近 2 窗、最近 1 窗，并在 ``available_windows``
    如实回报，绝不把未知值填 0。

    Args:
        revisions_by_bvid / points_by_bvid: 成员与快照数据。
        as_of_s: 本次执行时钟上限。
        policy: 日级策略。
        request_as_of_s: 请求时刻（新鲜度用）。
        source_policy_hash / expected_source_policy_hash: 来源策略核对。
        replay: 显式历史重放 → ``mode='historical'``，不进即时机会队列。
        panel_id / rule_hash / video_provenance: 进 fingerprint 的输入。
        video_provenance: 排序进指纹的 VideoStats 行（含 id/bvid/view）。

    Returns:
        dict: ``available_windows`` / ``a_delta`` / ``b_delta`` / ``c_delta`` /
        ``topic_phase`` / ``stage_reason`` / ``fingerprint`` / ``mode`` /
        ``stale`` / ``action_allowed`` 等。

    Raises:
        ValueError: 负增量 / as_of 非法 / decision 越界 / 来源策略不一致。
    """
    policy = policy or DailyPolicy()
    request_as_of = as_of_s if request_as_of_s is None else request_as_of_s

    # 从三窗往下退级，取第一个非空 P。
    chosen: PanelResult | None = None
    available = 0
    for k in (3, 2, 1):
        panel = build_panel_for(
            revisions_by_bvid,
            points_by_bvid,
            as_of_s=as_of_s,
            window_s=policy.window_seconds,
            n_windows=k,
            gap_max_s=policy.gap_max_seconds,
            require_coverage=policy.window_require_coverage,
        )
        if panel.paired_bvids:
            chosen = panel
            available = k
            break
    if chosen is None:
        # 连一窗都没有：仍给出 collecting 结果（不静默报错）。
        panel = build_panel_for(
            revisions_by_bvid,
            points_by_bvid,
            as_of_s=as_of_s,
            window_s=policy.window_seconds,
            n_windows=1,
            gap_max_s=policy.gap_max_seconds,
            require_coverage=policy.window_require_coverage,
        )
        chosen = panel
        available = 0

    panel = chosen
    per_window = [
        sum(panel.per_member_deltas[bvid][idx].delta or 0.0 for bvid in panel.paired_bvids)
        for idx in range(available)
    ]
    a = per_window[0] if available >= 1 else None
    b = per_window[1] if available >= 2 else None
    c = per_window[2] if available >= 3 else None

    author_count = len(
        {panel.owner_by_bvid.get(bvid) for bvid in panel.paired_bvids if panel.owner_by_bvid.get(bvid)}
    )
    per_author, known_delta, unknown_delta, _ = _author_stats(
        panel, latest_index=max(available - 1, 0)
    )
    total_delta = known_delta + unknown_delta
    top_author_share = (
        max(per_author.values()) / total_delta if (per_author and total_delta > 0) else None
    )
    unknown_share = unknown_delta / total_delta if total_delta > 0 else None
    unknown_owner_present = any(panel.owner_by_bvid.get(bvid) is None for bvid in panel.paired_bvids)
    concentrated = top_author_share is not None and top_author_share > policy.top_author_share_max
    author_coverage_insufficient = bool(unknown_owner_present) or (
        unknown_share is not None and unknown_share > policy.unknown_author_share_max
    )

    # 外层硬门：负增量 / as_of / decision 越界 / 来源策略。
    rows = [
        _paired_row(panel, bvid)
        for bvid in panel.paired_bvids
    ]
    assert_non_negative_deltas(rows)
    deadline_times = [
        latest_revision(revisions_by_bvid.get(bvid, ())).decision_at_s
        for bvid in panel.paired_bvids
        if latest_revision(revisions_by_bvid.get(bvid, ())) is not None
    ]
    outer = validate_outer_inputs(
        rows=rows,
        as_of_s=as_of_s,
        request_as_of_s=request_as_of,
        window_end_s=panel.window_end_s,
        decision_times_s=deadline_times,
        source_policy_hash=source_policy_hash,
        expected_source_policy_hash=expected_source_policy_hash,
        member_coverage=panel.member_coverage,
        min_member_coverage=policy.min_member_coverage,
        top_author_share=top_author_share,
        unknown_author_delta_share=unknown_share,
    )

    # 新鲜度：daily 须 request_as_of_s - window_end_s <= 36h。
    stale = (request_as_of - panel.window_end_s) > policy.daily_staleness_max_s
    if stale:
        outer["reason_codes"].append("stale_evidence")

    interpretation = interpret_daily(
        a=a,
        b=b,
        c=c,
        window_count=available,
        panel_size=panel.paired_member_count,
        video_count=panel.paired_member_count,
        author_count=author_count,
        member_coverage=panel.member_coverage,
        top_author_share=top_author_share,
        unknown_author_delta_share=unknown_share,
        author_coverage_insufficient=author_coverage_insufficient,
        concentrated=concentrated,
        policy=policy,
    )

    provenance = list(video_provenance or [])
    if not provenance:
        provenance = [
            {"id": None, "bvid": bvid, "view": None}
            for bvid in panel.paired_bvids
        ]
    member_rows = [
        {"bvid": bvid, "revision": latest_revision(revisions_by_bvid.get(bvid, ())).revision}
        for bvid in panel.paired_bvids
        if latest_revision(revisions_by_bvid.get(bvid, ())) is not None
    ]
    fingerprint = canonical_fingerprint(
        video_rows=provenance,
        member_rows=member_rows,
        rule_hash=rule_hash,
        panel_id=panel_id,
        window_start_s=panel.knowledge_cutoff_s,
        window_end_s=panel.window_end_s,
        policy_hash=policy.policy_version,
    )

    return {
        "window_end_s": panel.window_end_s,
        "knowledge_cutoff_s": panel.knowledge_cutoff_s,
        "available_windows": available,
        "a_delta": _as_float(a),
        "b_delta": _as_float(b),
        "c_delta": _as_float(c),
        "panel_video_count": panel.paired_member_count,
        "panel_author_count": author_count,
        "eligible_member_count": panel.eligible_member_count,
        "member_coverage": panel.member_coverage,
        "top_author_share": top_author_share,
        "unknown_author_delta_share": unknown_share,
        "exclusions": panel.exclusions,
        "fingerprint": fingerprint,
        "mode": "historical" if replay else "realtime",
        "policy_version": policy.policy_version,
        "stale": stale,
        # action 生成属 3e；本批只保证「过期时不许 make_candidate/prepare_or_pilot」。
        "action_allowed": (not stale) and interpretation["sample_gate_passed"],
        "reason_codes": sorted(set(outer["reason_codes"]) | set(interpretation["reason_codes"])),
        **interpretation,
    }


def _paired_row(panel: PanelResult, bvid: str):
    """构造校验用 ``PairedMeasure``（最近两窗；缺窗时用 0 占位仅用于负值检查）。"""
    from .aggregator import PairedMeasure

    deltas = panel.per_member_deltas[bvid]
    before = deltas[0].delta if deltas else None
    after = deltas[-1].delta if deltas else None
    return PairedMeasure(
        bvid=bvid,
        owner_mid=panel.owner_by_bvid.get(bvid),
        before=before,
        after=after,
        quality_ok=True,
    )


__all__ = ["aggregate_daily_triplet", "interpret_daily", "canonical_fingerprint"]
