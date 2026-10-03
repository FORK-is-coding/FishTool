"""04 两条通道的**薄接线层**：从真实库读成员/快照，喂纯内核，再把评估写回仓储。

- 内核（``channel_a`` / ``daily`` / ``early`` / ``aggregator``）保持纯函数，**不打桩**；
- 本层只做 IO：``video_stats`` / ``videos`` / ``hot_event_members`` / ``event_discovery_runs``；
- 评估落库走 3a 的 ``HotEventRepository.create_assessment``（同 ``input_fingerprint`` 命中即复用）。

边界：本层**不**冻结 / 激活 ``F`` panel（第四批），``F`` 由调用方传入。

**第四批 a 追加（§6.5 L639-648）**：:meth:`EventAggregationService.activate_fast_panel`
在一个短 SQLite 事务里原子冻结快采 panel：带 ``revision`` 谓词的 UPDATE 取写锁（CAS）→
读所有未过期 fast 需求的 BVID **并集**算全局剩余额度 → 容量检查 + 选 F → 追加
``fast_panel_history`` → 调 04 侧 :class:`EventWatchDemandReconciler`（flush-only）→ 最后一次
``commit``。网络 / 预算等待一律在事务外；任何异常整体回滚。
"""
from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import bindparam, text, update
from sqlalchemy.orm import Session

from core.database.hot_event_repository import HotEventRepository
from core.database.models_hot_event import EventDiscoveryRun, HotEvent, HotEventMember
from core.database.models_video import Video, VideoStats

from modules.hotspot.algorithm.window_metrics import window_end_s

from .channel_a import channel_a_trend
from .channel_b import DiscoveryRunView, discovery_signals
from .supply import SupplyMember
from .config import DAY_W, EarlyPolicy, DailyPolicy
from .daily import aggregate_daily_triplet
from .early import evaluate_early
from .opportunity import evaluate_choice
from .policy import EventPolicy
from .windows import MemberRevision, SnapshotPoint

SessionFactory = Any  # 无参调用返回 Session

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- 快采 panel 常量
#: 快采 panel 全局容量：所有事件共享同一池，最多 12 个**独立** BVID（§6.3 L624 / §6.5 L643）。
FAST_PANEL_CAPACITY: int = 12
#: 快采 panel 有效期（§6.3 L624：最多持续 24 小时后显式续期）。
FAST_PANEL_TTL_S: int = 24 * 3600
#: 快采采样间隔（§6.3 L624：每 20 分钟一次）。
FAST_PANEL_INTERVAL_S: int = 1200
#: 最低门：成功取得 ≥3 个视频名额才设 ``effective_s``（§6.5 L644）。
FAST_PANEL_MIN_VIDEOS: int = 3
#: 最低门：成功取得 ≥2 个作者名额才设 ``effective_s``（§6.5 L644）。
FAST_PANEL_MIN_AUTHORS: int = 2

#: 结果状态码（稳定字符串，供调用方 / 测试断言）。
FAST_PANEL_ACTIVATED: str = "activated"
FAST_PANEL_QUEUED: str = "queued_capacity"
FAST_PANEL_CONFLICT: str = "revision_conflict"
FAST_PANEL_REJECTED: str = "rejected"


def _read_fast_watch_switch() -> bool:
    """读 §18.2 快道开关（环境变量 > ``config.yaml`` > 默认 false）。

    Returns:
        bool: 是否打开快道 watch；**默认 false**，关闭时申请一律被拒，绝不静默成功。
    """
    raw_env = os.environ.get("EVENT_FAST_WATCH_ENABLED")
    if raw_env is not None:
        return str(raw_env).strip().lower() in ("1", "true", "yes", "on")
    try:
        # 延迟导入，避免模块导入期触达 ConfigManager；不可用时按默认关处理。
        from core.config import config as _config

        raw = _config.get("event_switches.fast_watch_enabled")
    except Exception:  # noqa: BLE001 - 配置不可用不应影响默认关的语义
        return False
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _balance_by_author(candidates: Sequence[tuple]) -> list[tuple]:
    """按作者轮转（round-robin）重排候选，尽量做到「分作者均衡」选 F（§6.3 L624）。

    Args:
        candidates: ``(bvid, owner_mid, revision)`` 序列。

    Returns:
        list[tuple]: 同一作者被拆散轮转后的候选列表（保序、无丢失）。
    """
    buckets: "OrderedDict[Any, list]" = OrderedDict()
    for item in candidates:
        buckets.setdefault(item[1], []).append(item)
    ordered: list[tuple] = []
    while True:
        progressed = False
        for owner in list(buckets.keys()):
            bucket = buckets[owner]
            if bucket:
                ordered.append(bucket.pop(0))
                progressed = True
        if not progressed:
            break
    return ordered


def _panel_member_rows(session: Session, event_id: str) -> list[tuple]:
    """读某事件每个 bvid 的**最新 revision**，返回仍 ``accepted`` 的候选。

    Args:
        session: 调用方会话（只读）。
        event_id: 事件 ID。

    Returns:
        list[tuple[str, Any, int]]: ``(bvid, owner_mid, revision)``，按 bvid 升序。
    """
    rows = (
        session.query(HotEventMember)
        .filter(HotEventMember.event_id == event_id)
        .order_by(
            HotEventMember.bvid.asc(),
            HotEventMember.revision.asc(),
            HotEventMember.decision_at_s.asc(),
            HotEventMember.id.asc(),
        )
        .all()
    )
    latest: dict = {}
    for row in rows:
        # 同 (event, bvid) 取最新 revision（顺序与 04 纯内核 status_at/latest_revision 一致）。
        latest[str(row.bvid)] = (str(row.status), row.owner_mid, int(row.revision))
    out: list[tuple] = []
    for bvid in sorted(latest):
        status, owner_mid, revision = latest[bvid]
        if status == "accepted":
            out.append((bvid, owner_mid, revision))
    return out


def _manual_stop_bvids(session: Session, bvids: Sequence[str]) -> set:
    """读给定 bvid 中处于 ``active=0 AND stop_reason='manual_stop'`` 的集合（§6.5 L648）。

    用户手动停止的命令优先级最高：该 BVID 从**未来 panel 申请中排除**，04 不自动重开。

    Args:
        session: 调用方会话（只读）。
        bvids: 待检查的 BVID 列表。

    Returns:
        set[str]: 命中 manual_stop 的 BVID 集合；缺表 / 缺列时返回空集。
    """
    wanted = [str(b) for b in bvids if str(b).strip()]
    if not wanted:
        return set()
    try:
        stmt = text(
            "SELECT bvid FROM hotspot_watch "
            "WHERE active = 0 AND stop_reason = 'manual_stop' AND bvid IN :bvids"
        ).bindparams(bindparam("bvids", expanding=True))
        rows = session.execute(stmt, {"bvids": wanted}).all()
    except Exception:  # noqa: BLE001 - 缺表 / 缺列时按「无 manual_stop」处理
        return set()
    return {str(row[0]) for row in rows}


def _load_fast_union(session: Session, now_s: int) -> set:
    """读**所有未过期 fast 需求**的 BVID **并集**（§6.5 L641 / L643）。

    全局容量按 BVID 并集计：被多个事件共享的 BVID **只占 1 个名额**。两处来源取并集：

    1. 所有事件 ``fast_panel_history`` 里 ``effective_s`` 非空且 ``expires_s > now_s`` 的 panel BVIDs；
    2. ``hotspot_watch.fast_until_s > now_s`` 的行（防御性并集，覆盖手工置位的快采标记）。

    Args:
        session: 调用方会话（只读）。
        now_s: 判定时刻（UTC 秒）。

    Returns:
        set[str]: 当前已占用快采名额的 BVID 并集。
    """
    used: set = set()
    rows = (
        session.query(HotEvent.fast_panel_history)
        .filter(HotEvent.fast_panel_history.isnot(None))
        .all()
    )
    for (history,) in rows:
        for entry in history or []:
            if not isinstance(entry, dict):
                continue
            # 「已激活」panel 必有 effective_s；草稿（未激活）不计名额。
            if entry.get("effective_s") is None:
                continue
            expires = entry.get("expires_s")
            if type(expires) is int and expires > now_s:
                for bvid in entry.get("bvids") or []:
                    used.add(str(bvid))
    try:
        stmt = text(
            "SELECT bvid FROM hotspot_watch "
            "WHERE fast_until_s IS NOT NULL AND fast_until_s > :now"
        )
        for (bvid,) in session.execute(stmt, {"now": now_s}).all():
            used.add(str(bvid))
    except Exception:  # noqa: BLE001 - 未跑迁移的旧库无该列：只用 panel 历史口径
        pass
    return used


def _mark_fast_until(session: Session, bvids: Sequence[str], until_s: int) -> int:
    """把 panel 成员主表行的 ``fast_until_s`` 置为到期时刻（快采标记，§6.4 L635）。

    Args:
        session: 调用方会话；只 flush。
        bvids: panel 成员 BVID 列表。
        until_s: 到期时刻（UTC 秒）。

    Returns:
        int: 被更新的行数；缺列 / 无行时 0。
    """
    wanted = [str(b) for b in bvids if str(b).strip()]
    if not wanted:
        return 0
    try:
        stmt = text(
            "UPDATE hotspot_watch SET fast_until_s = :until WHERE bvid IN :bvids"
        ).bindparams(bindparam("bvids", expanding=True))
        result = session.execute(stmt, {"until": int(until_s), "bvids": wanted})
        session.flush()
        return int(result.rowcount or 0)
    except Exception:  # noqa: BLE001 - 缺列（未跑迁移的库）时静默跳过，不炸整笔
        logger.warning("fast_until_s 列不可用，跳过快采标记")
        return 0


# --------------------------------------------------------------------------- 读取


def load_member_revisions(
    session_factory: SessionFactory, event_id: str
) -> dict[str, list[MemberRevision]]:
    """从 ``hot_event_members`` 读取某事件的全部成员版本（按 bvid 分组）。

    Args:
        session_factory: 无参调用返回 ``Session`` 的工厂。
        event_id: 事件 ID。

    Returns:
        dict: bvid -> 版本列表（未排序，交由纯函数按 revision/decision 排序）。
    """
    session: Session = session_factory()
    try:
        rows = (
            session.query(HotEventMember)
            .filter(HotEventMember.event_id == event_id)
            .order_by(HotEventMember.bvid.asc(), HotEventMember.revision.asc())
            .all()
        )
        grouped: dict[str, list[MemberRevision]] = {}
        for row in rows:
            grouped.setdefault(row.bvid, []).append(
                MemberRevision(
                    bvid=row.bvid,
                    owner_mid=row.owner_mid,
                    status=row.status,
                    revision=int(row.revision),
                    decision_at_s=int(row.decision_at_s),
                    first_seen_s=int(row.first_seen_s),
                )
            )
        return grouped
    finally:
        session.close()


def load_snapshots(
    session_factory: SessionFactory,
    bvids: Sequence[str],
    *,
    start_s: int,
    end_s: int,
) -> dict[str, list[SnapshotPoint]]:
    """从 ``video_stats``（join ``videos``）读取一批 BVID 在窗口内的快照点。

    Args:
        session_factory: 无参调用返回 ``Session`` 的工厂。
        bvids: 需要读取的 BVID 集合。
        start_s / end_s: 采集时间下 / 上限（含）。

    Returns:
        dict: bvid -> 快照点列表（按时间升序）。
    """
    wanted = [bvid for bvid in bvids]
    grouped: dict[str, list[SnapshotPoint]] = {bvid: [] for bvid in wanted}
    if not wanted:
        return grouped
    session: Session = session_factory()
    try:
        rows = (
            session.query(
                Video.bvid,
                VideoStats.captured_epoch_s,
                VideoStats.view,
                VideoStats.view_status,
            )
            .join(Video, VideoStats.video_id == Video.id)
            .filter(
                Video.bvid.in_(wanted),
                VideoStats.captured_epoch_s.isnot(None),
                VideoStats.captured_epoch_s >= int(start_s),
                VideoStats.captured_epoch_s <= int(end_s),
            )
            .order_by(Video.bvid.asc(), VideoStats.captured_epoch_s.asc())
            .all()
        )
        for bvid, captured_epoch_s, view, view_status in rows:
            if type(captured_epoch_s) is not int:
                continue
            view_ok = view_status == "ok" and isinstance(view, int) and not isinstance(view, bool) and view >= 0
            grouped.setdefault(bvid, []).append(
                SnapshotPoint(
                    epoch_s=int(captured_epoch_s),
                    view=(int(view) if view_ok else None),
                    view_ok=view_ok,
                )
            )
        for bvid in grouped:
            grouped[bvid].sort(key=lambda p: p.epoch_s)
        return grouped
    finally:
        session.close()


def snapshot_load_range(as_of_s: int, *, window_s: int, n_windows: int) -> tuple[int, int]:
    """给出读取快照的安全时间范围 ``[S - W, T + W]``。

    Args:
        as_of_s: 本次执行时钟上限。
        window_s: 窗口宽度。
        n_windows: 窗口数。

    Returns:
        ``(start_s, end_s)``。
    """
    T = window_end_s(as_of_s, window_s)
    S = T - n_windows * window_s
    return (S - window_s, T + window_s)


def supply_members_from_counters(counters: Mapping[str, Any]) -> tuple[SupplyMember, ...]:
    """把仓储 ``counters`` 的供给成员记录映射为 :class:`SupplyMember` 元组（第四批 b 缺口②）。

    现状（4b 之前）：:func:`discovery_run_view` 落下了 panel / fast 标记，却没把仓储
    ``counters`` 映射为 ``DiscoveryRunView.supply_members``，导致 4c 的 ``supply.py`` 三件套
    在生产链路永远拿不到成员、恒降级。本函数补上该映射：**能给的给，给不了的留空由
    supply 层判 unknown**（非破坏性，仅补字段）。

    Args:
        counters: ``EventDiscoveryRun.counters`` 映射；供给成员在 ``counters["supply_members"]``
            （通常为 dict 列表，键含 format / angle / need_keys / content_depth /
            classification_source / positive_evidence_refs 等）。

    Returns:
        tuple[SupplyMember, ...]: 映射成功的供给成员。**缺失 / 非法条目一律跳过**（安全降级）；
        空输入或缺键返回空元组（``supply.py`` 据此给出 ``angle_share`` 全 ``None``）。
    """
    raw = counters.get("supply_members")
    if not isinstance(raw, (list, tuple)):
        return ()
    members: list[SupplyMember] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        bvid = str(item.get("bvid") or "").strip()
        if not bvid:
            # 无唯一 BVID 无法进入 |E| / |C| 分母，跳过（safe 降级）。
            continue
        try:
            members.append(
                SupplyMember(
                    bvid=bvid,
                    format=str(item.get("format", "unknown")),
                    angle=str(item.get("angle", "unclassified")),
                    need_keys=tuple(item.get("need_keys", ()) or ()),
                    content_depth=str(item.get("content_depth", "title_only")),
                    classification_source=str(item.get("classification_source", "strict_rule")),
                    positive_evidence_refs=tuple(item.get("positive_evidence_refs", ()) or ()),
                    secondary_angles=tuple(item.get("secondary_angles", ()) or ()),
                    need_states=dict(item.get("need_states", {}) or {}),
                    author_mid=item.get("author_mid"),
                )
            )
        except (ValueError, TypeError):
            # 非法枚举 / 形状 → 跳过该条，绝不拖垮整跑。
            continue
    return tuple(members)


def discovery_run_view(row: EventDiscoveryRun) -> DiscoveryRunView:
    """把 ``event_discovery_runs`` 行归一为 :class:`DiscoveryRunView`。

    计划 / 页数 / 顺序等信息从 ``counters``（整对象）读取；缺省保守为「不可比」语义。

    Args:
        row: 发现 run 行。

    Returns:
        :class:`DiscoveryRunView`。
    """
    counters: Mapping[str, Any] = row.counters or {}
    return DiscoveryRunView(
        run_id=row.id,
        window_start_s=int(counters.get("window_start_s", row.started_s or 0)),
        window_end_s=int(counters.get("window_end_s", row.finished_s or row.started_s or 0)),
        plan_hash=str(row.source_policy_hash or counters.get("plan_hash", "")),
        query_count=int(counters.get("query_count", 0)),
        page_count=int(counters.get("page_count", 0)),
        query_order=tuple(counters.get("query_order", []) or []),
        interval_s=int(counters.get("interval_s", 0)),
        completed=bool(row.status == "completed"),
        newly_discovered_bvids=tuple(row.newly_discovered_bvids or []),
        newly_discovered_authors=tuple(counters.get("new_authors", []) or []),
        recently_published_bvids=tuple(counters.get("recently_published", []) or []),
        source_mix=dict(counters.get("source_mix", {}) or {}),
        max_result_cap=counters.get("max_result_cap"),
        unknown_pubdate_count=int(counters.get("unknown_pubdate_count", 0)),
        observed_delta_by_author=dict(counters.get("delta_by_author", {}) or {}),
        angle_tokens=tuple(counters.get("angle_tokens", []) or []),
        new_member_delta=dict(counters.get("new_member_delta", {}) or {}),
        supply_members=supply_members_from_counters(counters),
    )


# --------------------------------------------------------------------------- 历史回放（第四批 f）
#
# 回放口径：把「执行时钟」钉死在 request_as_of，只吃 as_of 之前落库的行，串起四个内核，
# 统一标 ``mode=historical``，且**绝不进入即时机会队列**（本模块不调用
# ``opportunity.get_or_create_opportunity_run``）。
#
# 四个 as_of 加载器各自守一个时间列，任何 as_of 之后的行都取不到——这是 E22「未来混入」
# 在 DB 层的兜底（内核层的 ``validate_outer_inputs`` 是第二道门）：
#     run      -> event_discovery_runs.started_s
#     capture  -> video_stats.captured_epoch_s
#     decision -> hot_event_members.decision_at_s
#     panel    -> hot_events.fast_panel_history[*].effective_s


def _load_as_of_member_revisions(
    session_factory: SessionFactory, event_id: str, *, as_of_s: int
) -> dict[str, list[MemberRevision]]:
    """只加载 ``decision_at_s <= as_of_s`` 的成员版本（**decision 门**）。

    Args:
        session_factory: 会话工厂。
        event_id: 事件 ID。
        as_of_s: 回放时刻（闭区间上限，epoch 秒）。

    Returns:
        dict: bvid -> 版本列表。``as_of_s`` 之后提交的版本**一律不在内**（含同 bvid 的更高
        revision）；因此回放不会看到「未来才接受的成员」。
    """
    cutoff = int(as_of_s)
    session: Session = session_factory()
    try:
        rows = (
            session.query(HotEventMember)
            .filter(
                HotEventMember.event_id == event_id,
                HotEventMember.decision_at_s <= cutoff,  # decision 过闸
            )
            .order_by(HotEventMember.bvid.asc(), HotEventMember.revision.asc())
            .all()
        )
        grouped: dict[str, list[MemberRevision]] = {}
        for row in rows:
            grouped.setdefault(row.bvid, []).append(
                MemberRevision(
                    bvid=row.bvid,
                    owner_mid=row.owner_mid,
                    status=row.status,
                    revision=int(row.revision),
                    decision_at_s=int(row.decision_at_s),
                    first_seen_s=int(row.first_seen_s),
                )
            )
        return grouped
    finally:
        session.close()


def _load_as_of_snapshots(
    session_factory: SessionFactory,
    bvids: Sequence[str],
    *,
    as_of_s: int,
    start_s: int,
    end_s: int,
) -> dict[str, list[SnapshotPoint]]:
    """只加载 ``captured_epoch_s <= as_of_s`` 的快照（**capture 门**）。

    与 :func:`load_snapshots` 同构，唯一区别：右端取 ``min(end_s, as_of_s)``，保证窗口
    即使天然越过 ``as_of``（``T + W``）也吃不到未来采集点。

    Args:
        session_factory: 会话工厂。
        bvids: 需要读取的 BVID 集合。
        as_of_s: 回放时刻（capture 硬上限）。
        start_s: 采集时间下限（含）。
        end_s: 采集时间上限（含，会再被 ``as_of_s`` 收紧）。

    Returns:
        dict: bvid -> 快照点列表（按时间升序）。
    """
    wanted = [str(bvid) for bvid in bvids]
    grouped: dict[str, list[SnapshotPoint]] = {bvid: [] for bvid in wanted}
    if not wanted:
        return grouped
    lo = int(start_s)
    hi = min(int(end_s), int(as_of_s))  # capture 过闸：紧到 as_of
    session: Session = session_factory()
    try:
        rows = (
            session.query(
                Video.bvid,
                VideoStats.captured_epoch_s,
                VideoStats.view,
                VideoStats.view_status,
            )
            .join(Video, VideoStats.video_id == Video.id)
            .filter(
                Video.bvid.in_(wanted),
                VideoStats.captured_epoch_s.isnot(None),
                VideoStats.captured_epoch_s >= lo,
                VideoStats.captured_epoch_s <= hi,
            )
            .order_by(Video.bvid.asc(), VideoStats.captured_epoch_s.asc())
            .all()
        )
        for bvid, captured_epoch_s, view, view_status in rows:
            if type(captured_epoch_s) is not int:
                continue
            view_ok = (
                view_status == "ok"
                and isinstance(view, int)
                and not isinstance(view, bool)
                and view >= 0
            )
            grouped.setdefault(bvid, []).append(
                SnapshotPoint(
                    epoch_s=int(captured_epoch_s),
                    view=(int(view) if view_ok else None),
                    view_ok=view_ok,
                )
            )
        for bvid in grouped:
            grouped[bvid].sort(key=lambda p: p.epoch_s)
        return grouped
    finally:
        session.close()


def _load_as_of_discovery_runs(
    session_factory: SessionFactory, event_id: str, *, as_of_s: int
) -> list[DiscoveryRunView]:
    """只加载 ``started_s <= as_of_s`` 的发现 run（**run 门**），并在会话内归一为视图。

    Args:
        session_factory: 会话工厂。
        event_id: 事件 ID。
        as_of_s: 回放时刻（闭区间上限）。

    Returns:
        list[DiscoveryRunView]: 按 ``started_s`` 升序的、as_of 之内的发现 run 视图；
        ``as_of_s`` 之后开始的 run 一律不在内。在会话内构造，避免 ORM 行脱管。
    """
    cutoff = int(as_of_s)
    session: Session = session_factory()
    try:
        rows = (
            session.query(EventDiscoveryRun)
            .filter(
                EventDiscoveryRun.event_id == event_id,
                EventDiscoveryRun.started_s <= cutoff,  # run 过闸
            )
            .order_by(EventDiscoveryRun.started_s.asc(), EventDiscoveryRun.id.asc())
            .all()
        )
        return [discovery_run_view(row) for row in rows]
    finally:
        session.close()


def _load_as_of_panel_effective(
    session_factory: SessionFactory, event_id: str, *, as_of_s: int
) -> tuple[str, ...]:
    """只取 ``hot_events.fast_panel_history`` 中 ``effective_s <= as_of_s`` 的 panel BVID。

    **panel effective 门**：未激活（无 ``effective_s``）或晚于 ``as_of_s`` 激活的 panel
    不占名额、不进回放；非法条目跳过（safe 降级，不填 0、不抛异常）。

    Args:
        session_factory: 会话工厂。
        event_id: 事件 ID。
        as_of_s: 回放时刻（闭区间上限）。

    Returns:
        tuple[str, ...]: as_of 之前已生效 panel 的 BVID 并集（保序去重）。
    """
    cutoff = int(as_of_s)
    session: Session = session_factory()
    try:
        row = session.get(HotEvent, event_id)
        if row is None:
            return ()
        bvids: list[str] = []
        for entry in list(row.fast_panel_history or []):
            if not isinstance(entry, Mapping):
                continue
            effective_s = entry.get("effective_s")
            if type(effective_s) is not int:  # 未激活 / 非法 → 不进（不填 0）
                continue
            if effective_s > cutoff:  # panel effective 过闸
                continue
            for bvid in entry.get("bvids") or []:
                token = str(bvid)
                if token and token not in bvids:
                    bvids.append(token)
        return tuple(bvids)
    finally:
        session.close()


def run_historical_replay(
    event_id: str,
    *,
    request_as_of_s: int,
    brief: Any,
    session_factory: SessionFactory | None = None,
    repository: Optional[HotEventRepository] = None,
    daily_policy: DailyPolicy | None = None,
    early_policy: EarlyPolicy | None = None,
    event_policy: EventPolicy | None = None,
    fast_panel_bvids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """历史回放统一编排入口（第四批 f）：DB 层按 as_of 加载 → 串四内核 → 统一 historical。

    串联顺序钉死（前三个内核一律 ``replay=True``，机会层统一 ``facts['mode']='historical'``）：

    1. 通道 A（``channel_a_trend``）；
    2. 日级三窗（``aggregate_daily_triplet``）；
    3. 早期 2h（``evaluate_early``）；
    4. 机会判定（``evaluate_choice``，历史模式只允许 ``differentiate_research`` /
       ``watch_and_collect``）。

    **``request_as_of_s`` 必传、无默认值、不许省**：缺失由签名直接 ``TypeError``；显式传入
    非法值（``None`` / 负数 / 非 int）一律 ``ValueError('invalid_request_as_of_s')``，绝不回退成
    「现在」。回放时刻同时充当执行时钟上限（``as_of_s``），DB 层据此只取 ``<= request_as_of_s`` 的行。

    **不得进即时机会队列**：本入口只做纯评估，**不**调用
    ``opportunity.get_or_create_opportunity_run``，因此不会创建 ``hotspot_opportunity_runs`` 行。

    Args:
        event_id: 事件 ID。
        request_as_of_s: 回放/请求时刻（epoch 秒）；必传。
        brief: ``CreatorBrief`` 或等价映射（机会层所需，不可为空）。
        session_factory: 会话工厂；缺省用 ``core.database.get_session``。
        repository: 3a 仓储；缺省用同一 ``session_factory`` 构造（本入口不写机会 run）。
        daily_policy / early_policy / event_policy: 各层策略；缺省用各自默认。
        fast_panel_bvids: 显式传入的 ``F`` panel；``None`` 时从 as_of 内 panel 历史加载。

    Returns:
        dict: ``mode``（恒 ``'historical'``）/ ``event_id`` / ``request_as_of_s`` /
        ``channel_a`` / ``daily`` / ``early`` / ``discovery`` / ``opportunity`` / ``action`` /
        ``entered_realtime_queue``（恒 ``False``）。

    Raises:
        ValueError: ``event_id`` / ``request_as_of_s`` / ``brief`` 非法。
    """
    # ---- 参数校验：request_as_of_s 必传且非法即拒，绝不回退墙钟 ----
    if type(request_as_of_s) is not int or isinstance(request_as_of_s, bool) or request_as_of_s < 0:
        raise ValueError("invalid_request_as_of_s")
    clean_event = str(event_id or "").strip()
    if not clean_event:
        raise ValueError("invalid_event_id")
    if brief is None:
        raise ValueError("invalid_brief")

    factory: SessionFactory = session_factory
    if factory is None:
        from core.database import get_session as factory  # 延迟导入，避免导入期副作用

    daily_policy = daily_policy or DailyPolicy()
    early_policy = early_policy or EarlyPolicy()
    event_policy = event_policy or EventPolicy()

    as_of = int(request_as_of_s)  # 回放：执行时钟上限 = 重放请求时刻

    # ---- 1. DB 层按 as_of 加载（四个时间列各自过闸，未来行取不到）----
    revisions = _load_as_of_member_revisions(factory, clean_event, as_of_s=as_of)
    start_s, end_s = snapshot_load_range(as_of, window_s=daily_policy.window_seconds, n_windows=3)
    points = _load_as_of_snapshots(
        factory, list(revisions.keys()), as_of_s=as_of, start_s=start_s, end_s=end_s
    )
    runs = _load_as_of_discovery_runs(factory, clean_event, as_of_s=as_of)
    panel_bvids: tuple[str, ...] = (
        tuple(str(b) for b in fast_panel_bvids)
        if fast_panel_bvids is not None
        else _load_as_of_panel_effective(factory, clean_event, as_of_s=as_of)
    )

    # ---- 2. 串四内核（前三个 replay=True → 各自 mode=historical）----
    channel_a = channel_a_trend(
        revisions,
        points,
        as_of_s=as_of,
        policy=daily_policy,
        request_as_of_s=request_as_of_s,
        replay=True,
    )
    daily = aggregate_daily_triplet(
        revisions,
        points,
        as_of_s=as_of,
        policy=daily_policy,
        request_as_of_s=request_as_of_s,
        replay=True,
    )
    early = evaluate_early(
        revisions,
        points,
        fast_panel_bvids=list(panel_bvids),
        as_of_s=as_of,
        policy=early_policy,
        request_as_of_s=request_as_of_s,
        replay=True,
    )

    # ---- 3. 发现侧：取 as_of 内最后一次（可比）run 计算通道 B 信号 ----
    discovery_view = runs[-1] if runs else None
    prev_view = runs[-2] if len(runs) >= 2 else None
    discovery = (
        discovery_signals(discovery_view, prev_run=prev_view)
        if discovery_view is not None
        else {}
    )

    # ---- 4. 机会层：统一 historical，**只评估不落队列** ----
    facts: dict[str, Any] = {
        "event_id": clean_event,
        "mode": "historical",  # 硬标历史：机会层据此只出研究 / 观察
        "daily": daily,
        "early": early,
        "discovery": discovery,
    }
    opportunity = evaluate_choice(facts, brief, request_as_of_s, policy=event_policy)

    return {
        "mode": "historical",
        "event_id": clean_event,
        "request_as_of_s": as_of,
        "channel_a": channel_a,
        "daily": daily,
        "early": early,
        "discovery": discovery,
        "opportunity": opportunity,
        "action": opportunity.get("action"),
        # 明示：本入口不写 hotspot_opportunity_runs（不进即时机会队列）。
        "entered_realtime_queue": False,
    }


# --------------------------------------------------------------------------- 服务


class EventAggregationService:
    """把仓储读取 + 纯内核 + 评估落库串起来的薄服务。"""

    def __init__(
        self,
        session_factory: SessionFactory,
        repository: Optional[HotEventRepository] = None,
    ) -> None:
        """初始化服务。

        Args:
            session_factory: 会话工厂（可注入临时库）。
            repository: 3a 仓储；缺省用同一 ``session_factory`` 构造。
        """
        self._session_factory = session_factory
        self._repo = repository or HotEventRepository(session_factory=session_factory)

    @property
    def repository(self) -> HotEventRepository:
        """返回底层 3a 仓储。"""
        return self._repo

    def run_channel_a(
        self, event_id: str, *, as_of_s: int, policy: DailyPolicy | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """读取事件数据并计算通道 A。

        Args:
            event_id: 事件 ID。
            as_of_s: 本次执行时钟上限。
            policy: 策略（缺省 ``DailyPolicy``）。
            **kwargs: 透传 ``channel_a_trend``（如 ``replay`` / 来源策略 hash）。

        Returns:
            dict: 通道 A 结果。
        """
        policy = policy or DailyPolicy()
        revisions = load_member_revisions(self._session_factory, event_id)
        start_s, end_s = snapshot_load_range(as_of_s, window_s=policy.window_seconds, n_windows=2)
        points = load_snapshots(
            self._session_factory, list(revisions.keys()), start_s=start_s, end_s=end_s
        )
        return channel_a_trend(revisions, points, as_of_s=as_of_s, policy=policy, **kwargs)

    def run_daily(
        self, event_id: str, *, as_of_s: int, policy: DailyPolicy | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """读取事件数据并计算日级三窗。

        Args:
            event_id: 事件 ID。
            as_of_s: 本次执行时钟上限。
            policy: 策略（缺省 ``DailyPolicy``）。
            **kwargs: 透传 ``aggregate_daily_triplet``。

        Returns:
            dict: 日级三窗结果（含 ``fingerprint``）。
        """
        policy = policy or DailyPolicy()
        revisions = load_member_revisions(self._session_factory, event_id)
        start_s, end_s = snapshot_load_range(as_of_s, window_s=policy.window_seconds, n_windows=3)
        points = load_snapshots(
            self._session_factory, list(revisions.keys()), start_s=start_s, end_s=end_s
        )
        provenance = self._video_provenance(list(revisions.keys()), start_s=start_s, end_s=end_s)
        return aggregate_daily_triplet(
            revisions, points, as_of_s=as_of_s, policy=policy, video_provenance=provenance, **kwargs
        )

    def run_early(
        self,
        event_id: str,
        *,
        as_of_s: int,
        fast_panel_bvids: Sequence[str],
        policy: EarlyPolicy | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """读取事件数据并计算 early 2h（``F`` 作为传入输入）。

        Args:
            event_id: 事件 ID。
            as_of_s: 本次执行时钟上限。
            fast_panel_bvids: 传入快 panel ``F``。
            policy: early 策略。
            **kwargs: 透传 ``evaluate_early``（如 ``observed_sample_interval_s``）。

        Returns:
            dict: early 结果。
        """
        policy = policy or EarlyPolicy()
        revisions = load_member_revisions(self._session_factory, event_id)
        start_s, end_s = snapshot_load_range(as_of_s, window_s=policy.window_seconds, n_windows=2)
        points = load_snapshots(
            self._session_factory, list(revisions.keys()), start_s=start_s, end_s=end_s
        )
        return evaluate_early(
            revisions,
            points,
            fast_panel_bvids=fast_panel_bvids,
            as_of_s=as_of_s,
            policy=policy,
            **kwargs,
        )

    def _video_provenance(
        self, bvids: Sequence[str], *, start_s: int, end_s: int
    ) -> list[dict[str, Any]]:
        """取进 fingerprint 的 VideoStats 行（含 ``id`` / ``bvid`` / ``view``）。"""
        if not bvids:
            return []
        session: Session = self._session_factory()
        try:
            rows = (
                session.query(VideoStats.id, Video.bvid, VideoStats.view)
                .join(Video, VideoStats.video_id == Video.id)
                .filter(
                    Video.bvid.in_(list(bvids)),
                    VideoStats.captured_epoch_s.isnot(None),
                    VideoStats.captured_epoch_s >= int(start_s),
                    VideoStats.captured_epoch_s <= int(end_s),
                )
                .all()
            )
            return [{"id": row[0], "bvid": row[1], "view": row[2]} for row in rows]
        finally:
            session.close()

    def persist_assessment(
        self,
        event_id: str,
        result: Mapping[str, Any],
        *,
        window_kind: str,
        revision: int = 1,
        rule_version: int = 0,
        policy_version: str | None = None,
        status: str | None = None,
        metrics: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> Any:
        """把一次评估写回 ``hot_event_assessments``（同指纹命中即复用）。

        Args:
            event_id: 事件 ID。
            result: 纯内核结果（须含 ``window_end_s`` / ``fingerprint``）。
            window_kind: ``daily24h`` / ``early2h``。
            revision / rule_version: 评估版本与规则版本。
            policy_version: 政策版本；缺省取结果的 ``policy_version``。
            status: 评估状态；缺省按结果推断（``complete`` / ``collecting`` / ``stale``）。
            metrics / provenance: 附加 JSON。

        Returns:
            新建或指纹命中的 ``HotEventAssessment``。
        """
        window_end = int(result["window_end_s"])
        inferred_status = self._infer_status(result)
        finalized_status = status or inferred_status
        interpretation = {
            "topic_phase": result.get("topic_phase"),
            "stage_reason": result.get("stage_reason"),
            "signal": result.get("signal"),
            "reason_codes": list(result.get("reason_codes") or []),
            "attention_present": result.get("attention_present"),
            "label": result.get("label"),
            "mode": result.get("mode"),
        }
        return self._repo.create_assessment(
            event_id=event_id,
            revision=revision,
            as_of_s=int(result.get("as_of_s", window_end)),
            window_end_s=window_end,
            window_kind=window_kind,
            rule_version=rule_version,
            policy_version=policy_version or str(result.get("policy_version", "unknown")),
            status=finalized_status,
            input_fingerprint=str(result["fingerprint"]),
            interpretation=interpretation,
            metrics=dict(metrics or {}),
            provenance=dict(provenance or {}),
        )

    # ------------------------------------------------------------- 快采 panel（第四批 a）
    def activate_fast_panel(
        self,
        event_id: str,
        expected_revision: int,
        now_s: int,
        *,
        reconciler: Any | None = None,
        fast_watch_enabled: bool | None = None,
    ) -> dict[str, Any]:
        """原子冻结快采 panel：CAS 取写锁 → 容量 → 选 F → 记历史 → 整编需求 → 一次 commit。

        顺序钉死（§6.5 L641，不许换）：

        1. 对 ``hot_events`` 做带 ``revision`` 谓词的 UPDATE 取写锁（CAS；不匹配 → 回滚返冲突）；
        2. 读**所有未过期 fast 需求**的 BVID **并集**，算全局剩余额度（§1.2）；
        3. 容量检查 + 选 F（按作者均衡；最低门 ≥3 视频 / ≥2 作者）；
        4. 追加 ``fast_panel_history``（ID / effective_s / expires / BVIDs / member revisions）+ 置快采标记；
        5. 调 04 侧 :class:`EventWatchDemandReconciler`（内部走 02 ``reconcile_demands``，**flush-only，不 commit**）；
        6. **最后一次 commit**（本事务内不发网络、不等待预算）。

        任何异常整体回滚；额度不足回滚并返 ``queued_capacity``（**不保存「已激活」panel、
        不设 effective_s**，草稿需求保持原样）。

        Args:
            event_id: 目标事件 ID。
            expected_revision: 调用方持有的 ``HotEvent.revision``（CAS 期望值）。
            now_s: 本次执行时刻（UTC 秒级整数）。
            reconciler: 04 侧需求对账器；缺省惰性构造真实
                :class:`modules.hotspot.event_watch_demands.EventWatchDemandReconciler`。
            fast_watch_enabled: 快道开关；None 时读 ``EVENT_FAST_WATCH_ENABLED``（默认 false）。

        Returns:
            dict: ``status`` / ``reason_code`` / ``capacity`` / ``panel`` 等结构化结果。

        Raises:
            ValueError: 参数非法。
            Exception: 事务内任一步失败（先回滚再上抛，保证 panel 与 watch 需求同存同亡）。
        """
        clean_event = str(event_id or "").strip()
        if not clean_event:
            raise ValueError("invalid_event_id")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid_expected_revision")
        if type(now_s) is not int or now_s < 0:
            raise ValueError("invalid_now_s")

        enabled = _read_fast_watch_switch() if fast_watch_enabled is None else bool(fast_watch_enabled)
        session: Session = self._session_factory()
        try:
            # ---- 0. 开关默认关：关闭时申请被拒，绝不静默成功 ----
            if not enabled:
                session.rollback()
                return {
                    "status": FAST_PANEL_REJECTED,
                    "reason_code": "fast_watch_disabled",
                    "activated": False,
                    "event_id": clean_event,
                    "panel": None,
                }

            # ---- 1. CAS 取写锁（带 revision 谓词；不匹配 → 回滚返冲突）----
            cas = session.execute(
                update(HotEvent)
                .where(HotEvent.id == clean_event, HotEvent.revision == expected_revision)
                .values(revision=HotEvent.revision + 1, updated_s=now_s)
            )
            if int(cas.rowcount or 0) != 1:
                session.rollback()
                return {
                    "status": FAST_PANEL_CONFLICT,
                    "reason_code": "revision_conflict",
                    "activated": False,
                    "event_id": clean_event,
                    "expected_revision": expected_revision,
                }

            session.expire_all()
            event = session.get(HotEvent, clean_event)
            if event is None or str(event.status) != "active":
                session.rollback()
                return {
                    "status": FAST_PANEL_REJECTED,
                    "reason_code": "event_not_active",
                    "activated": False,
                    "event_id": clean_event,
                }

            # ---- 2. 全局并集：所有未过期 fast 需求占用的 BVID ----
            used = _load_fast_union(session, now_s)
            remaining = max(0, FAST_PANEL_CAPACITY - len(used))

            # ---- 3. 候选 + 容量检查 + 选 F ----
            candidates = _panel_member_rows(session, clean_event)
            blocked = _manual_stop_bvids(session, [b for b, _, _ in candidates])
            candidates = [c for c in candidates if c[0] not in blocked]

            # 共享 BVID（已在全局池）不占新名额；新 BVID 才增加全局计数（§6.5 L643）。
            shared = [c for c in candidates if c[0] in used]
            fresh = _balance_by_author([c for c in candidates if c[0] not in used])
            selected = shared + fresh[:remaining]
            seen: set = set()
            f_rows: list[tuple] = []
            for bvid, owner_mid, revision in selected:
                if bvid in seen:
                    continue
                seen.add(bvid)
                f_rows.append((bvid, owner_mid, revision))

            authors = {owner for _, owner, _ in f_rows if owner is not None}
            capacity_report = {
                "limit": FAST_PANEL_CAPACITY,
                "used": len(used),
                "remaining": remaining,
                "candidate_count": len(candidates),
            }
            # 最低门：<3 视频 或 <2 作者 → 不设 effective_s、不留「已激活」panel、保持草稿需求。
            if len(f_rows) < FAST_PANEL_MIN_VIDEOS or len(authors) < FAST_PANEL_MIN_AUTHORS:
                session.rollback()
                return {
                    "status": FAST_PANEL_QUEUED,
                    "reason_code": "queued_capacity",
                    "activated": False,
                    "event_id": clean_event,
                    "capacity": capacity_report,
                    "min_videos": FAST_PANEL_MIN_VIDEOS,
                    "min_authors": FAST_PANEL_MIN_AUTHORS,
                    "selected_videos": len(f_rows),
                    "selected_authors": len(authors),
                    "panel": None,
                }

            # ---- 4. 记 panel 历史 + 快采标记 ----
            panel_bvids = [bvid for bvid, _, _ in f_rows]
            member_revisions = {bvid: int(revision) for bvid, _, revision in f_rows}
            entry = {
                "panel_id": f"{clean_event}:{now_s}",
                "effective_s": now_s,
                "expires_s": now_s + FAST_PANEL_TTL_S,
                "ttl_s": FAST_PANEL_TTL_S,
                "interval_s": FAST_PANEL_INTERVAL_S,
                "bvids": panel_bvids,
                "member_revisions": member_revisions,
                "actual_videos": len(f_rows),
                "actual_authors": len(authors),
                "capacity_limit": FAST_PANEL_CAPACITY,
                "capacity_used": len(used | set(panel_bvids)),
                "rule_version": int(getattr(event, "current_rule_version", 0) or 0),
            }
            history = list(event.fast_panel_history or [])
            history.append(entry)
            event.fast_panel_history = history
            session.flush()
            _mark_fast_until(session, panel_bvids, entry["expires_s"])

            # ---- 5. 整编 events 完整需求快照（flush-only，不 commit）----
            active_reconciler = reconciler or self._default_reconciler()
            active_reconciler.reconcile(session, now_s=now_s)

            # ---- 6. 最后一次 commit ----
            session.commit()
            logger.info(
                "快采 panel 已激活 event=%s videos=%s authors=%s used=%s",
                clean_event,
                len(f_rows),
                len(authors),
                len(used | set(panel_bvids)),
            )
            return {
                "status": FAST_PANEL_ACTIVATED,
                "reason_code": "activated",
                "activated": True,
                "event_id": clean_event,
                "capacity": {**capacity_report, "used_after": len(used | set(panel_bvids))},
                "panel": entry,
            }
        except Exception:
            session.rollback()
            logger.exception("快采 panel 激活失败，已整体回滚 event=%s", clean_event)
            raise
        finally:
            session.close()

    def _default_reconciler(self) -> Any:
        """惰性构造 04 侧需求对账器（复用真实 :class:`EventWatchDemandReconciler`）。

        Returns:
            Any: :class:`modules.hotspot.event_watch_demands.EventWatchDemandReconciler` 实例。
        """
        from modules.hotspot.event_watch_demands import EventWatchDemandReconciler

        return EventWatchDemandReconciler()

    @staticmethod
    def _infer_status(result: Mapping[str, Any]) -> str:
        """按结果推断评估状态（受控枚举之一）。"""
        from core.database.models_hot_event import ASSESSMENT_STATUSES

        if result.get("stale"):
            candidate = "stale"
        elif result.get("status") == "insufficient":
            candidate = "insufficient"
        elif result.get("available_windows", 3) < 3 and result.get("window_kind") != "early2h":
            candidate = "collecting"
        else:
            candidate = "complete"
        return candidate if candidate in ASSESSMENT_STATUSES else "partial"


def activate_fast_panel(
    event_id: str,
    expected_revision: int,
    now_s: int,
    *,
    session_factory: SessionFactory | None = None,
    repository: Optional[HotEventRepository] = None,
    reconciler: Any | None = None,
    fast_watch_enabled: bool | None = None,
) -> dict[str, Any]:
    """模块级入口：等价于 :meth:`EventAggregationService.activate_fast_panel`（§6.5 L641）。

    Args:
        event_id: 目标事件 ID。
        expected_revision: CAS 期望的 ``HotEvent.revision``。
        now_s: 本次执行时刻（UTC 秒）。
        session_factory: 会话工厂；缺省用 ``core.database.get_session``。
        repository: 3a 仓储；缺省用同一 ``session_factory`` 构造。
        reconciler: 04 侧需求对账器；缺省惰性构造真实实现。
        fast_watch_enabled: 快道开关；None 时读环境变量（默认 false）。

    Returns:
        dict: 与 :meth:`EventAggregationService.activate_fast_panel` 相同的结构化结果。
    """
    factory = session_factory
    if factory is None:
        from core.database import get_session as factory  # 延迟导入，避免导入期副作用

    service = EventAggregationService(session_factory=factory, repository=repository)
    return service.activate_fast_panel(
        event_id,
        expected_revision,
        now_s,
        reconciler=reconciler,
        fast_watch_enabled=fast_watch_enabled,
    )


__all__ = [
    "EventAggregationService",
    "activate_fast_panel",
    "FAST_PANEL_ACTIVATED",
    "FAST_PANEL_CAPACITY",
    "FAST_PANEL_CONFLICT",
    "FAST_PANEL_INTERVAL_S",
    "FAST_PANEL_MIN_AUTHORS",
    "FAST_PANEL_MIN_VIDEOS",
    "FAST_PANEL_QUEUED",
    "FAST_PANEL_REJECTED",
    "FAST_PANEL_TTL_S",
    "load_member_revisions",
    "load_snapshots",
    "snapshot_load_range",
    "discovery_run_view",
    "supply_members_from_counters",
    # 历史回放（第四批 f）：编排入口 + DB 层 as_of 加载（缺口②最小导出）
    "run_historical_replay",
    "_load_as_of_member_revisions",
    "_load_as_of_snapshots",
    "_load_as_of_discovery_runs",
    "_load_as_of_panel_effective",
]
