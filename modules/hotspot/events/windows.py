"""窗口与 panel 核（04 通道 A / 日级三窗 / early 共用的纯计算）。

本模块**只调用** ``algorithm/`` 的共享内核，绝不修改它：

- ``modules.hotspot.algorithm.window_metrics.window_end_s`` —— 唯一的 UTC 网格右边界实现；
- ``modules.hotspot.algorithm.lifecycle_v2`` 的 ``Point`` / ``valid_segments`` /
  ``window_measure`` —— 02 的纯窗口分段与双边插值。

设计口径：
- ``T = window_end_s(as_of_s, W)``，daily ``W=86400``、early ``W=7200``，**UTC 零点锚定**；
- ``U``：以最早比较窗起点 ``S = T - n*W`` 为 ``knowledge_cutoff`` 取**当时 latest revision**，
  冻结「已发现且 accepted」的成员分母；起点后撤销 / 规则不兼容成员**不进有效 panel，
  但仍在 U 覆盖分母中**并记录排除原因 —— **不许缩小分母制造 100% 覆盖**；
- ``P``：U 中在**每个** ``(end-W, end]`` 窗口都完整有效的 BVID 交集；
- 负回撤 / 缺失 / 超 gap → ``valid_segments`` 断段 → 两窗都不参与比较。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from modules.hotspot.algorithm.lifecycle_v2 import Point, valid_segments, window_measure
from modules.hotspot.algorithm.window_metrics import window_end_s

#: 成员状态合法值（与 3a ``MEMBER_STATUSES`` 一致；此处只做只读判定）。
_ACCEPTED = "accepted"


@dataclass(frozen=True)
class MemberRevision:
    """一条成员版本（只读视图，来自 ``hot_event_members``）。

    Args:
        bvid: 视频 BV 号。
        owner_mid: UP 主 mid；``None`` = 作者不可核实。
        status: ``proposed`` / ``accepted`` / ``rejected``。
        revision: 版本号（同 (event,bvid) 严格递增）。
        decision_at_s: 该版本实际提交时刻（epoch 秒）。
        first_seen_s: 首次发现时刻（不清回填 pubdate）。
    """

    bvid: str
    owner_mid: int | None
    status: str
    revision: int
    decision_at_s: int
    first_seen_s: int


@dataclass(frozen=True)
class SnapshotPoint:
    """一个快照观测点（来自 ``video_stats``）。

    Args:
        epoch_s: 采集到达时刻（UTC 秒级 int）。
        view: 累计播放；``None`` = 缺失/非法（不可当真实 0）。
        view_ok: ``view_status == 'ok'`` 且数值合法。
    """

    epoch_s: int
    view: int | None
    view_ok: bool


@dataclass(frozen=True)
class WindowDelta:
    """单个成员在某个窗口上的测量结果。

    Args:
        bvid: 视频 BV 号。
        end_s: 该窗口右边界。
        delta: 有效增量；``None`` = 不可用（右端缺括点 / 覆盖不足等）。
        coverage: 窗口支撑占比（0~1）。
        ok: 是否**完整有效**（``delta`` 非空且 ``coverage >= require_coverage``）。
        reason: 不可用原因（``ok`` 为真时为 ``None``）。
    """

    bvid: str
    end_s: int
    delta: float | None
    coverage: float
    ok: bool
    reason: str | None = None


@dataclass
class PanelResult:
    """一次 panel 构建结果（U 分母 + P 交集 + 排除清单）。"""

    #: 所用网格边界。
    window_end_s: int
    #: 最早比较窗起点 ``S``（= knowledge_cutoff）。
    knowledge_cutoff_s: int
    #: ``U``：冻结的 eligible 分母（accepted at S）。
    eligible_bvids: tuple[str, ...]
    #: ``P``：每个窗口都完整有效、且当前仍 accepted 的交集。
    paired_bvids: tuple[str, ...]
    #: 逐成员三窗（或两窗）明细。
    per_member_deltas: dict[str, tuple[WindowDelta, ...]]
    #: 逐成员当前作者（用于作者数 / 集中度）。
    owner_by_bvid: dict[str, int | None]
    #: 排除清单（含原因与是否仍在分母内）。
    exclusions: list[dict[str, Any]] = field(default_factory=list)
    #: 本周期第一次被本工具发现的 BVID（decision 晚于 S）。
    newly_discovered_bvids: tuple[str, ...] = ()

    @property
    def eligible_member_count(self) -> int:
        """``len(U)`` —— 冻结分母（**不缩小**）。"""
        return len(self.eligible_bvids)

    @property
    def paired_member_count(self) -> int:
        """``len(P)`` —— 双窗（三窗）交集。"""
        return len(self.paired_bvids)

    @property
    def member_coverage(self) -> float | None:
        """成员覆盖率 = ``len(P)/len(U)``；无分母返回 ``None``。"""
        if not self.eligible_bvids:
            return None
        return len(self.paired_bvids) / len(self.eligible_bvids)


# --------------------------------------------------------------------------- 内部工具


def _sort_key(revision: MemberRevision) -> tuple[int, int]:
    """成员版本排序键：revision 主序、decision 次序（与仓储层一致）。"""
    return (revision.revision, revision.decision_at_s)


def status_at(revisions: Sequence[MemberRevision], cutoff_s: int) -> MemberRevision | None:
    """历史状态：**先限 decision_at_s <= cutoff，再取最新 revision**（顺序不许颠倒）。

    Args:
        revisions: 同一 (event,bvid) 的所有版本。
        cutoff_s: 历史截止时刻（含）。

    Returns:
        截止时刻下最新的成员版本；无记录返回 ``None``。
    """
    eligible = [r for r in revisions if r.decision_at_s <= cutoff_s]
    if not eligible:
        return None
    return max(eligible, key=_sort_key)


def latest_revision(revisions: Sequence[MemberRevision]) -> MemberRevision | None:
    """当前状态：**最新 revision**（无 cutoff）。"""
    if not revisions:
        return None
    return max(revisions, key=_sort_key)


def supporting_segment(
    points: Sequence[SnapshotPoint], end_s: int, *, max_gap_s: int
) -> list[Point]:
    """取「支撑截至 ``end_s`` 的最新有效单调段」。

    Args:
        points: 该 bvid 的快照点序列。
        end_s: 需要支撑的右边界。
        max_gap_s: 相邻点允许的最大间隔（超则断段）。

    Returns:
        支撑段（``Point`` 列表）；无可用段返回空列表。
    """
    as_points = [
        Point(
            epoch_s=int(p.epoch_s),
            view=(int(p.view) if (p.view_ok and isinstance(p.view, int) and not isinstance(p.view, bool)) else None),
            ok=bool(p.view_ok and isinstance(p.view, int) and not isinstance(p.view, bool) and p.view >= 0),
        )
        for p in points
    ]
    segments = valid_segments(as_points, max_gap_s=max_gap_s)
    if not segments:
        return []
    # 优先取「右端点 >= end_s」的最新段；否则回退最后一段（供覆盖率判定）。
    for segment in reversed(segments):
        if segment and segment[-1].epoch_s >= end_s:
            return segment
    return segments[-1]


def window_deltas(
    points: Sequence[SnapshotPoint],
    *,
    bvid: str,
    T: int,
    window_s: int,
    n_windows: int,
    gap_max_s: int,
    require_coverage: float = 1.0,
) -> tuple[WindowDelta, ...]:
    """计算某成员在 ``n_windows`` 个连续窗口上的增量（最早窗在前）。

    窗口 ``i``（0=最早）右边界 ``end_i = T - (n-1-i)*window_s``。

    Args:
        points: 该 bvid 的快照点。
        bvid: 视频 BV 号（写入 :class:`WindowDelta`)。
        T: 最新窗右边界（网格）。
        window_s: 窗口宽度（秒）。
        n_windows: 窗口数（2 或 3）。
        gap_max_s: 相邻点最大间隔。
        require_coverage: 判定「完整有效」所需最小覆盖（默认 1.0）。

    Returns:
        按时间升序的 :class:`WindowDelta` 元组；窗口不可用时 ``delta=None``。
    """
    if type(n_windows) is not int or n_windows < 1:
        raise ValueError("invalid_n_windows")
    segment = supporting_segment(points, T, max_gap_s=gap_max_s)
    results: list[WindowDelta] = []
    for index in range(n_windows):
        end_s = T - (n_windows - 1 - index) * window_s
        delta, coverage, _observed = window_measure(
            segment, end_s, window_s=window_s, gap_max_s=gap_max_s
        )
        if delta is not None and coverage >= require_coverage:
            results.append(WindowDelta(bvid, end_s, float(delta), coverage, True, None))
        else:
            if delta is None:
                reason = "no_right_bracket" if coverage >= require_coverage else "insufficient_coverage"
            else:
                reason = "partial_coverage"
            results.append(WindowDelta(bvid, end_s, None, coverage, False, reason))
    return tuple(results)


# --------------------------------------------------------------------------- panel 构建


def build_panel(
    revisions_by_bvid: Mapping[str, Sequence[MemberRevision]],
    points_by_bvid: Mapping[str, Sequence[SnapshotPoint]],
    *,
    T: int,
    window_s: int,
    n_windows: int,
    gap_max_s: int,
    require_coverage: float = 1.0,
) -> PanelResult:
    """构建 ``U`` 分母 + ``P`` 交集 panel。

    Args:
        revisions_by_bvid: bvid -> 该成员全部版本。
        points_by_bvid: bvid -> 该成员快照点。
        T: 最新窗右边界（网格）。
        window_s: 窗口宽度（秒）。
        n_windows: 窗口数。
        gap_max_s: 相邻点最大间隔。
        require_coverage: 每窗「完整有效」覆盖下限。

    Returns:
        :class:`PanelResult`。
    """
    if type(T) is not int or T < 0:
        raise ValueError("invalid_window_end_s")
    if type(window_s) is not int or window_s <= 0:
        raise ValueError("invalid_window_s")
    cutoff_s = T - n_windows * window_s

    eligible: list[str] = []
    paired: list[str] = []
    per_member: dict[str, tuple[WindowDelta, ...]] = {}
    owner_by_bvid: dict[str, int | None] = {}
    exclusions: list[dict[str, Any]] = []
    newly_discovered: list[str] = []

    for bvid, revisions in revisions_by_bvid.items():
        at_cutoff = status_at(revisions, cutoff_s)
        if at_cutoff is None or at_cutoff.status != _ACCEPTED:
            # S 之后才首次进入（含从未出现的 bvid）→ 只进发现通道，不进分母。
            newly_discovered.append(bvid)
            exclusions.append(
                {
                    "bvid": bvid,
                    "reason": "discovered_after_cutoff",
                    "in_denominator": False,
                }
            )
            continue

        # accepted at S → 进 U 分母（**无论后续是否被撤销，分母都不缩小**）。
        eligible.append(bvid)
        current = latest_revision(revisions)
        owner_by_bvid[bvid] = None if current is None else current.owner_mid

        if current is None or current.status != _ACCEPTED:
            exclusions.append(
                {
                    "bvid": bvid,
                    "reason": "revoked_or_incompatible",
                    "in_denominator": True,
                }
            )
            continue

        deltas = window_deltas(
            points_by_bvid.get(bvid, ()),
            bvid=bvid,
            T=T,
            window_s=window_s,
            n_windows=n_windows,
            gap_max_s=gap_max_s,
            require_coverage=require_coverage,
        )
        per_member[bvid] = deltas
        if all(d.ok for d in deltas):
            paired.append(bvid)
        else:
            reason = next((d.reason for d in deltas if not d.ok), "window_invalid")
            exclusions.append(
                {"bvid": bvid, "reason": reason, "in_denominator": True}
            )

    # 稳定输出顺序，便于 fingerprint 与断言。
    eligible.sort()
    paired.sort()
    newly_discovered.sort()

    return PanelResult(
        window_end_s=T,
        knowledge_cutoff_s=cutoff_s,
        eligible_bvids=tuple(eligible),
        paired_bvids=tuple(paired),
        per_member_deltas=per_member,
        owner_by_bvid=owner_by_bvid,
        exclusions=exclusions,
        newly_discovered_bvids=tuple(newly_discovered),
    )


def build_panel_for(
    revisions_by_bvid: Mapping[str, Sequence[MemberRevision]],
    points_by_bvid: Mapping[str, Sequence[SnapshotPoint]],
    *,
    as_of_s: int,
    window_s: int,
    n_windows: int,
    gap_max_s: int,
    require_coverage: float = 1.0,
) -> PanelResult:
    """从 ``as_of_s`` 起算 ``T = window_end_s(as_of_s, window_s)`` 再构建 panel。

    Args:
        revisions_by_bvid / points_by_bvid: 见 :func:`build_panel`。
        as_of_s: 本次执行时钟上限。
        window_s: 窗口宽度（秒）。
        n_windows: 窗口数。
        gap_max_s: 相邻点最大间隔。
        require_coverage: 每窗覆盖下限。

    Returns:
        :class:`PanelResult`。

    Raises:
        ValueError: ``as_of_s`` / ``window_s`` 非法（由 ``window_end_s`` 抛出）。
    """
    T = window_end_s(as_of_s, window_s)
    return build_panel(
        revisions_by_bvid,
        points_by_bvid,
        T=T,
        window_s=window_s,
        n_windows=n_windows,
        gap_max_s=gap_max_s,
        require_coverage=require_coverage,
    )


__all__ = [
    "MemberRevision",
    "SnapshotPoint",
    "WindowDelta",
    "PanelResult",
    "status_at",
    "latest_revision",
    "supporting_segment",
    "window_deltas",
    "build_panel",
    "build_panel_for",
]
