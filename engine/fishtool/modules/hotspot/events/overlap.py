"""§7.4 多事件重叠口径。

- 事件内部一个 BVID **一次**，**全额** delta 记入该事件；
- 跨事件总体若要显示总增量，**取 BVID 并集一次**；
- 界面注明事件可重叠，**各事件数字不能直接相加**；
- **不用任意平均分配权重**把一个视频的播放均摊给多个事件。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

#: 界面提示：事件可重叠，数字不可直接相加。
OVERLAP_NOTICE: str = "事件可重叠，各事件数字不能直接相加"


def event_internal_measure(members: Iterable[tuple[str, float]]) -> dict[str, Any]:
    """单个事件内部去重后的全额增量汇总。

    Args:
        members: ``(bvid, delta)`` 序列；同一 BVID 只允许出现一次。

    Returns:
        dict: ``member_count`` / ``total_delta`` / ``members``（bvid -> 全额 delta）。

    Raises:
        ValueError: 事件内部出现重复 BVID（``'duplicate_bvid'``），避免静默双计。
    """
    pairs = list(members)
    seen = [bvid for bvid, _ in pairs]
    if len(set(seen)) != len(seen):
        raise ValueError("duplicate_bvid")
    members_map: dict[str, float] = {}
    for bvid, delta in pairs:
        members_map[bvid] = float(delta)
    return {
        "member_count": len(members_map),
        "total_delta": sum(members_map.values()),
        "members": members_map,
    }


def cross_event_union(
    events: Mapping[str, Mapping[str, float]]
) -> dict[str, Any]:
    """跨事件总体：取 BVID 并集一次（**不做权重均摊**）。

    Args:
        events: event_id -> ``{bvid: delta}``（每个事件的已去重全额增量）。

    Returns:
        dict: ``per_event``（逐事件明细）/ ``union_member_count`` / ``union_total_delta`` /
        ``overlap_notice``。同一 BVID 即使跨多个事件，并集里也只计一次全额。
    """
    per_event: dict[str, dict[str, float]] = {
        event_id: dict(members) for event_id, members in events.items()
    }
    union: dict[str, float] = {}
    for members in per_event.values():
        for bvid, delta in members.items():
            # 并集一次：首个事件的全额 delta 保留，绝不累加 / 均摊。
            union.setdefault(bvid, float(delta))
    return {
        "per_event": per_event,
        "union_member_count": len(union),
        "union_total_delta": sum(union.values()),
        "overlap_notice": OVERLAP_NOTICE,
        "members": union,
    }


__all__ = ["event_internal_measure", "cross_event_union", "OVERLAP_NOTICE"]
