"""§7.3 通道 B：扩散、供给与机会线索（**独立显示，不硬压成单一阶段标签**）。

老成员减速 + 新作者增加必须能**并列出现**，不能塌成「话题衰退」。

``sampling_changed`` 门（§7.3 L760）：只有发现计划 hash、查询次数/页数/顺序、间隔和完成度
**可比**的两次 run，才输出「发现线索增加/减少」；扩页或换关键词引起的增加一律标
``sampling_changed``，**不作为热度增强**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .supply import SupplyMember, compute_angle_density

#: 「角度拥挤」只称已见样本线索，不称市场饱和率。
ANGLE_DENSITY_LABEL: str = "已见样本角度拥挤线索"


@dataclass(frozen=True)
class DiscoveryRunView:
    """一次发现 run 的只读视图（从 ``event_discovery_runs`` 归一而来）。

    Args:
        run_id: 发现 run ID。
        window_start_s / window_end_s: 本周期窗口起止。
        plan_hash: 发现计划 hash（= 冻结的 ``source_policy_hash``）。
        query_count: 查询次数。
        page_count: 抓取页数。
        query_order: 查询顺序（关键词/分页序列）。
        interval_s: 采样间隔。
        completed: 本轮发现是否完成（完成度）。
        newly_discovered_bvids: 本周期第一次被本工具发现的 BVID（**不等同本周期发布**）。
        newly_discovered_authors: 本事件此前未见且 owner 可确认的作者 mid。
        recently_published_bvids: pubdate 确在该窗口内且被本轮发现的 BVID。
        source_mix: 来源入口 -> 发现数。
        max_result_cap: 结果上限。
        unknown_pubdate_count: 无法确认发布时间的数量。
        observed_delta_by_author: 作者 -> 本次已观测增量（用于 ``dominance``）。
        angle_tokens: 规范化角度 token（历史证据字段，保留；不再充当角度密度分母）。
        new_member_delta: 新成员 bvid -> 自首个可信观察后的实际增量；**无左端的成员缺席**。
        supply_members: 已接受成员的供给记录（§7.1/§7.2 三件套输入）。
            缺省为空 → 只能给「证据不足」，不给拥挤 / 稀缺结论。
    """

    run_id: str
    window_start_s: int
    window_end_s: int
    plan_hash: str
    query_count: int
    page_count: int
    query_order: tuple[str, ...]
    interval_s: int
    completed: bool
    newly_discovered_bvids: tuple[str, ...] = ()
    newly_discovered_authors: tuple[int, ...] = ()
    recently_published_bvids: tuple[str, ...] = ()
    source_mix: Mapping[str, int] = field(default_factory=dict)
    max_result_cap: int | None = None
    unknown_pubdate_count: int = 0
    observed_delta_by_author: Mapping[int, float] = field(default_factory=dict)
    angle_tokens: tuple[str, ...] = ()
    new_member_delta: Mapping[str, float] = field(default_factory=dict)
    supply_members: tuple[SupplyMember, ...] = ()


def sampling_comparable(prev: DiscoveryRunView, cur: DiscoveryRunView) -> bool:
    """两次 run 的采样是否可比（计划 hash / 查询次数 / 页数 / 顺序 / 间隔 / 完成度）。

    Args:
        prev: 上一次发现 run。
        cur: 本次发现 run。

    Returns:
        bool: 全部一致才 ``True``；任一项不同即不可比（应标 ``sampling_changed``）。
    """
    return (
        prev.plan_hash == cur.plan_hash
        and prev.query_count == cur.query_count
        and prev.page_count == cur.page_count
        and tuple(prev.query_order) == tuple(cur.query_order)
        and prev.interval_s == cur.interval_s
        and bool(prev.completed) == bool(cur.completed)
    )


def discovery_signals(
    run: DiscoveryRunView, *, prev_run: DiscoveryRunView | None = None
) -> dict[str, Any]:
    """汇总通道 B 的独立信号。

    Args:
        run: 本次发现 run。
        prev_run: 上一次可比 run；为 ``None`` 则不给「发现线索增加/减少」。

    Returns:
        dict: 扩散 / 供给 / 机会线索；``sampling_changed`` 为真时 ``discovery_delta`` 为
        ``None``（**不给发现增速**）。
    """
    sampling_changed = False
    discovery_delta: int | None = None
    if prev_run is not None:
        if sampling_comparable(prev_run, run):
            discovery_delta = len(run.newly_discovered_bvids) - len(prev_run.newly_discovered_bvids)
        else:
            sampling_changed = True

    # 新成员增量：只保留「自首个可信观察后有左端」的条目，没左端不填累计播放差。
    observed_new = {bvid: float(delta) for bvid, delta in run.new_member_delta.items() if delta is not None}

    # dominance：top1 作者占本次已观测增量；分母 0 → null。
    total_observed = sum(float(v) for v in run.observed_delta_by_author.values())
    dominance: float | None
    if total_observed > 0 and run.observed_delta_by_author:
        dominance = max(float(v) for v in run.observed_delta_by_author.values()) / total_observed
    else:
        dominance = None

    # angle_density：§7.2 三件套（分母是 |C| 不是 |E|；C 空 → angle_share 全 None；
    # 小样本 / 低 coverage 只列已见内容，不评价拥挤 / 稀缺）。只称「已见样本角度拥挤线索」。
    angle_density = compute_angle_density(run.supply_members)

    authors = set(run.newly_discovered_authors)
    return {
        "newly_discovered_videos": len(run.newly_discovered_bvids),
        "newly_discovered_authors": len(authors),
        "recently_published_discovered_videos": len(run.recently_published_bvids),
        "new_member_delta_observed": observed_new,
        "source_mix": dict(run.source_mix),
        "discovery_completeness": bool(run.completed),
        "query_policy_hash": run.plan_hash,
        "max_result_cap": run.max_result_cap,
        "unknown_pubdate_count": run.unknown_pubdate_count,
        "content_supply": {
            "videos": len(run.newly_discovered_bvids),
            "authors": len(authors),
        },
        "dominance": dominance,
        "angle_density": angle_density.as_dict(),
        "angle_density_label": ANGLE_DENSITY_LABEL,
        "discovery_delta": discovery_delta,
        "sampling_changed": sampling_changed,
        "reason_codes": ["sampling_changed"] if sampling_changed else [],
    }


__all__ = ["DiscoveryRunView", "sampling_comparable", "discovery_signals", "ANGLE_DENSITY_LABEL"]
