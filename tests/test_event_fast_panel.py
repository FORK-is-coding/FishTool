"""FishTool 04 · 第四批 a：快采 panel 原子接线验收（真临时 SQLite / 真并发）。

依据：
- ``FishTool_04_R5执行规格_第四批a_fast_panel原子接线.md`` §1 / §2 / §3；
- 原案 ``FishTool_04_..._02补充执行案(1).md`` §6.5（L639-648）、§6.3 L624、§18.2 L1528-1538。

硬约束：
    - **真临时 SQLite**：E41 / E42 / E43 用真实库；E42 用**两个独立引擎 / 连接**制造真并发；
    - ``WatchService.reconcile_demands`` **不打桩、不重写**（E41 的故障注入打在 04 侧
      ``EventWatchDemandReconciler`` 上，真实 ``reconcile_demands`` 仍被执行）；
    - 覆盖：E41 / E42 / E43 + 容量并集 + 最低门 + queued_capacity + 草稿需求 + manual_stop +
      完整快照 + 不倒写 / 新版本 + 降级 + 事务内不发网络 + 一次 commit + CAS + 开关默认关。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from core.database import DatabaseManager
from core.database.models_hot_event import HotEvent, HotEventMember
from modules.hotspot.event_watch_demands import EVENTS_NAMESPACE, EventWatchDemandReconciler
from modules.hotspot.events.config import (
    DEFAULT_DISCOVERY_DEADLINE_SECONDS,
    DEFAULT_DISCOVERY_LEASE_SECONDS,
    DEFAULT_MANUAL_DISCOVERY_COOLDOWN_SECONDS,
    DiscoveryPolicy,
)
from modules.hotspot.events.early import evaluate_early
from modules.hotspot.events.service import (
    FAST_PANEL_ACTIVATED,
    FAST_PANEL_CAPACITY,
    FAST_PANEL_CONFLICT,
    FAST_PANEL_QUEUED,
    FAST_PANEL_REJECTED,
    EventAggregationService,
    activate_fast_panel,
    _read_fast_watch_switch,
)
from modules.hotspot.events.windows import MemberRevision, SnapshotPoint
from modules.hotspot.watch_service import WatchService
from modules.hotspot.watch_store import upsert_watch

#: 2026-09-01T00:00:00Z（86400 与 7200 的公共网格点，与既有 early 测试一致）。
E: int = 1788220800
#: early 快窗宽度（秒）。
EW: int = 7200
#: 快采插点步长（秒）：远小于 40 分钟门。
FAST_STEP: int = 1200


# ============================================================================
# 通用装配
# ============================================================================


def _service(path, *, session_factory=None) -> EventAggregationService:
    """构造一个基于临时库的 04 服务。"""
    if session_factory is None:
        mgr = DatabaseManager(str(path))
        session_factory = mgr.get_session
    return EventAggregationService(session_factory=session_factory)


def _seed_event(
    session,
    event_id: str,
    members,
    *,
    status: str = "active",
    revision: int = 0,
    decision_at_s: int = E,
) -> None:
    """建一个事件与若干 accepted 成员（``members`` 为 ``(bvid, owner_mid)`` 或 bvid）。"""
    session.add(
        HotEvent(
            id=event_id,
            name=f"事件-{event_id}",
            created_s=E,
            updated_s=E,
            status=status,
            current_rule_version=1,
            revision=revision,
            source_policy_hash="h1",
        )
    )
    for item in members:
        if isinstance(item, tuple):
            bvid, owner_mid = item
        else:
            bvid, owner_mid = item, None
        session.add(
            HotEventMember(
                event_id=event_id,
                bvid=bvid,
                revision=1,
                status="accepted",
                first_seen_s=E,
                decision_at_s=decision_at_s,
                rule_version=1,
                decision_source="manual",
                owner_mid=owner_mid,
            )
        )
    session.flush()


def _seed_watch(session, *bvids: str, now: int = E) -> None:
    """把若干 bvid 入 watch 池（不提交）。"""
    for bvid in bvids:
        upsert_watch(session, bvid=bvid, now_epoch_s=now)
    session.flush()


def _activate(svc, event_id, expected_revision, now_s, **kwargs):
    """默认打开快道开关地调用激活。"""
    kwargs.setdefault("fast_watch_enabled", True)
    return svc.activate_fast_panel(event_id, expected_revision, now_s, **kwargs)


def _event_row(session, event_id: str) -> dict:
    """读事件的 ``revision`` / ``status`` / ``fast_panel_history``（已解析）。"""
    row = session.execute(
        text("SELECT revision, status, fast_panel_history FROM hot_events WHERE id = :e"),
        {"e": event_id},
    ).first()
    if row is None:
        return {}
    return {
        "revision": int(row[0]),
        "status": row[1],
        "history": None if row[2] is None else json.loads(row[2]),
    }


def _watch_row(session, bvid: str) -> dict:
    """读某 bvid 的关键列（demands 已解析）。"""
    row = session.execute(
        text(
            "SELECT active, stop_reason, source_demands, sample_interval_s, fast_until_s "
            "FROM hotspot_watch WHERE bvid = :b"
        ),
        {"b": bvid},
    ).first()
    if row is None:
        return {}
    return {
        "active": bool(row[0]),
        "stop_reason": row[1],
        "demands": None if row[2] is None else json.loads(row[2]),
        "sample_interval_s": row[3],
        "fast_until_s": row[4],
    }


def _db_fast_union(session, now_s: int) -> set:
    """从库里重算快采 BVID 并集（panel 历史 ∪ fast_until_s）。"""
    used: set = set()
    for (history,) in session.execute(
        text("SELECT fast_panel_history FROM hot_events WHERE fast_panel_history IS NOT NULL")
    ).all():
        for entry in (json.loads(history) if history else []):
            if entry.get("effective_s") is None:
                continue
            if type(entry.get("expires_s")) is int and entry["expires_s"] > now_s:
                used.update(str(b) for b in entry.get("bvids") or [])
    for (bvid,) in session.execute(
        text("SELECT bvid FROM hotspot_watch WHERE fast_until_s IS NOT NULL AND fast_until_s > :n"),
        {"n": now_s},
    ).all():
        used.add(str(bvid))
    return used


@pytest.fixture()
def db_path(tmp_path):
    """临时库文件路径。"""
    return tmp_path / "event_fast_panel.db"


@pytest.fixture()
def mgr(db_path):
    """临时库管理器。"""
    manager = DatabaseManager(str(db_path))
    try:
        yield manager
    finally:
        manager.engine.dispose()


@pytest.fixture()
def session(mgr):
    """临时库上的独立会话。"""
    db = mgr.get_session()
    try:
        yield db
    finally:
        db.close()


# ============================================================================
# §18.2 开关：默认 false；关闭时申请被拒（不许静默成功）
# ============================================================================


def test_fast_switch_default_off_and_rejects(tmp_path, session, monkeypatch) -> None:
    """开关默认 false；关闭时申请被拒，且零副作用。"""
    monkeypatch.delenv("EVENT_FAST_WATCH_ENABLED", raising=False)
    # 随包配置默认关（config.yaml event_switches.fast_watch_enabled=false）。
    assert _read_fast_watch_switch() is False

    _seed_event(session, "e1", [("BV1", 1), ("BV2", 2), ("BV3", 3)])
    _seed_watch(session, "BV1", "BV2", "BV3")
    session.commit()

    svc = _service(tmp_path / "switch.db", session_factory=lambda: session)
    result = svc.activate_fast_panel("e1", 0, E)  # 不传开关 -> 读环境变量 -> 关
    assert result["status"] == FAST_PANEL_REJECTED
    assert result["reason_code"] == "fast_watch_disabled"
    assert result["activated"] is False
    assert _event_row(session, "e1")["history"] is None
    assert _watch_row(session, "BV1")["fast_until_s"] is None


# ============================================================================
# CAS：revision 不匹配 → 冲突且零副作用
# ============================================================================


def test_cas_revision_conflict_has_zero_side_effects(tmp_path, session) -> None:
    """CAS：revision 不匹配 → 返冲突，事件 / panel / 需求 / 快采标记全不变。"""
    _seed_event(session, "e1", [("BV1", 1), ("BV2", 2), ("BV3", 3)])
    _seed_watch(session, "BV1", "BV2", "BV3")
    session.commit()

    svc = _service(tmp_path / "cas.db", session_factory=lambda: session)
    result = _activate(svc, "e1", 7, E)  # 期望 revision=7，实际 0
    assert result["status"] == FAST_PANEL_CONFLICT
    assert result["reason_code"] == "revision_conflict"
    assert result["activated"] is False

    row = _event_row(session, "e1")
    assert row["revision"] == 0, "CAS 冲突不得推进 revision"
    assert row["history"] is None
    for bvid in ("BV1", "BV2", "BV3"):
        assert _watch_row(session, bvid)["fast_until_s"] is None
        assert _watch_row(session, bvid)["demands"] is None


# ============================================================================
# 最低门：<3 视频 或 <2 作者 → 不设 effective_s、不留「已激活」panel
# ============================================================================


def test_min_gate_rejects_too_few_videos_or_authors(tmp_path, session) -> None:
    """最低门：2 视频 / 单作者均不得成 panel。"""
    _seed_event(session, "e_small", [("BV1", 1), ("BV2", 2)])
    _seed_event(session, "e_single_author", [("BV6", 9), ("BV7", 9), ("BV8", 9)])
    _seed_watch(session, "BV1", "BV2", "BV6", "BV7", "BV8")
    session.commit()

    svc = _service(tmp_path / "gate.db", session_factory=lambda: session)
    r1 = _activate(svc, "e_small", 0, E)
    assert r1["status"] == FAST_PANEL_QUEUED
    assert r1["reason_code"] == "queued_capacity"
    assert r1["selected_videos"] == 2 and r1["selected_authors"] == 2

    r2 = _activate(svc, "e_single_author", 0, E)
    assert r2["status"] == FAST_PANEL_QUEUED
    assert r2["selected_videos"] == 3 and r2["selected_authors"] == 1

    assert _event_row(session, "e_small")["history"] is None
    assert _event_row(session, "e_single_author")["history"] is None
    for bvid in ("BV1", "BV2", "BV6", "BV7", "BV8"):
        assert _watch_row(session, bvid)["fast_until_s"] is None


# ============================================================================
# 容量并集：共享 BVID 只占 1 个全局名额
# ============================================================================


def test_capacity_union_shared_bvid_counts_once(tmp_path, session) -> None:
    """两事件共享 BV1/BV2：全局并集计数，共享 BVID 只占 1 个名额。"""
    _seed_event(session, "e1", [("BV1", 1), ("BV2", 2), ("BV3", 3)])
    _seed_event(session, "e2", [("BV1", 1), ("BV2", 2), ("BV4", 4), ("BV5", 5)])
    _seed_watch(session, "BV1", "BV2", "BV3", "BV4", "BV5")
    session.commit()

    svc = _service(tmp_path / "union.db", session_factory=lambda: session)
    r1 = _activate(svc, "e1", 0, E)
    assert r1["status"] == FAST_PANEL_ACTIVATED
    assert r1["capacity"]["used_after"] == 3

    r2 = _activate(svc, "e2", 0, E)
    assert r2["status"] == FAST_PANEL_ACTIVATED
    # e2 选取 {BV1,BV2（共享，免费）,BV4,BV5}：全局占用 5，而非 3+4=7。
    assert r2["capacity"]["used"] == 3
    assert r2["capacity"]["used_after"] == 5
    assert set(r2["panel"]["bvids"]) == {"BV1", "BV2", "BV4", "BV5"}

    assert _db_fast_union(session, E) == {"BV1", "BV2", "BV3", "BV4", "BV5"}


# ============================================================================
# queued_capacity：保持草稿需求、不设 effective_s
# ============================================================================


def test_queued_capacity_keeps_draft_demand(tmp_path, session) -> None:
    """额度不足：不得保存「已激活」panel，返 queued_capacity 并保持草稿需求。"""
    _seed_event(session, "e1", [("BV1", 1), ("BV2", 2), ("BV3", 3)])
    _seed_watch(session, "BV1", "BV2", "BV3")
    # 先建 e1 的草稿（events 普通 1h）需求。
    EventWatchDemandReconciler().reconcile(session, now_s=E)
    session.commit()
    assert _watch_row(session, "BV1")["demands"]["events"] == {
        "e1": {"bvids": ["BV1", "BV2", "BV3"]}
    }
    assert _watch_row(session, "BV1")["sample_interval_s"] == 3600

    # 用另一事件占满 12 个全局名额。
    full = [(f"BV_F{i}", 100 + (i % 2)) for i in range(1, 13)]
    _seed_event(session, "e_full", full)
    _seed_watch(session, *[b for b, _ in full])
    session.commit()
    svc = _service(tmp_path / "queued.db", session_factory=lambda: session)
    r_full = _activate(svc, "e_full", 0, E)
    assert r_full["status"] == FAST_PANEL_ACTIVATED
    assert r_full["capacity"]["used_after"] == FAST_PANEL_CAPACITY

    # 此时剩余额度 = 0，e1 全新 BVID → 无法过最低门 → queued_capacity。
    r1 = _activate(svc, "e1", 0, E)
    assert r1["status"] == FAST_PANEL_QUEUED
    assert r1["reason_code"] == "queued_capacity"
    assert r1["capacity"]["remaining"] == 0
    assert r1["panel"] is None

    # 草稿需求保持原样，未升级为快采、未删除。
    draft = _watch_row(session, "BV1")
    assert draft["demands"]["events"] == {"e1": {"bvids": ["BV1", "BV2", "BV3"]}}
    assert draft["sample_interval_s"] == 3600
    assert draft["fast_until_s"] is None
    assert _event_row(session, "e1")["history"] is None
    assert _event_row(session, "e1")["revision"] == 0, "queued 不得推进 revision"


# ============================================================================
# E41：panel 写入后 watch 需求更新故障 → 同事务全部 rollback
# ============================================================================


class _FaultyReconciler:
    """包装真实 reconciler：先真实执行（flush-only），再抛故障（模拟 watch 需求更新失败）。"""

    def __init__(self, inner: EventWatchDemandReconciler) -> None:
        self._inner = inner

    @property
    def watch_service(self):
        """暴露底层 02 WatchService。"""
        return self._inner.watch_service

    def reconcile(self, session, *, now_s=None):
        """真实整编 events 需求后立即抛错。"""
        self._inner.reconcile(session, now_s=now_s)
        raise RuntimeError("watch_demand_update_failed")


def test_E41_panel_and_demand_rollback_together(tmp_path, session) -> None:
    """E41：panel 写入后需求更新故障 → panel 与 watch 需求同存同亡。"""
    _seed_event(session, "e1", [("BV1", 1), ("BV2", 2), ("BV3", 3)])
    _seed_watch(session, "BV1", "BV2", "BV3")
    session.commit()

    svc = _service(tmp_path / "e41.db", session_factory=lambda: session)
    faulty = _FaultyReconciler(EventWatchDemandReconciler(watch_service=WatchService()))

    with pytest.raises(RuntimeError):
        _activate(svc, "e1", 0, E, reconciler=faulty)

    # 整体回滚：panel 历史、revision、快采标记、watch 需求全部无残留。
    row = _event_row(session, "e1")
    assert row["history"] is None, "故障下 panel 历史不得残留"
    assert row["revision"] == 0, "故障下 CAS revision 必须回滚"
    for bvid in ("BV1", "BV2", "BV3"):
        wrow = _watch_row(session, bvid)
        assert wrow["fast_until_s"] is None, "故障下不得留下「panel 已生效但 watch 未提速」"
        assert wrow["demands"] is None, "故障下不得留下部分 watch 需求"


# ============================================================================
# E42：两事件并发快采申请、部分 BVID 重叠 → 全局并集容量、最多 12
# ============================================================================


def test_E42_concurrent_events_global_union_capacity(tmp_path, db_path) -> None:
    """E42：真并发两事件（部分重叠）→ 全局并集容量，最多 12，竞争失败返 queued_capacity。"""
    a_members = [(f"BV_A{i}", 1000 + (i % 2)) for i in range(1, 13)]
    b_members = [("BV_A1", 1000), ("BV_A2", 1001)] + [
        (f"BV_B{i}", 2000 + (i % 2)) for i in range(3, 13)
    ]

    seed = DatabaseManager(str(db_path))
    s = seed.get_session()
    _seed_event(s, "eA", a_members)
    _seed_event(s, "eB", b_members)
    _seed_watch(s, *[b for b, _ in a_members], *[b for b, _ in b_members])
    s.commit()
    s.close()
    seed.engine.dispose()

    mgr_a = DatabaseManager(str(db_path))
    mgr_b = DatabaseManager(str(db_path))
    svc_a = EventAggregationService(session_factory=mgr_a.get_session)
    svc_b = EventAggregationService(session_factory=mgr_b.get_session)

    barrier = threading.Barrier(2)
    results: dict = {}
    errors: dict = {}

    def run(name: str, svc: EventAggregationService) -> None:
        """在屏障后同时发起激活。"""
        try:
            barrier.wait(timeout=10)
            results[name] = svc.activate_fast_panel(
                name, 0, E, fast_watch_enabled=True
            )
        except Exception as exc:  # noqa: BLE001 - 收集线程异常供断言
            errors[name] = exc

    t_a = threading.Thread(target=run, args=("eA", svc_a))
    t_b = threading.Thread(target=run, args=("eB", svc_b))
    t_a.start()
    t_b.start()
    t_a.join(timeout=30)
    t_b.join(timeout=30)

    assert not errors, f"并发激活出现异常：{errors}"
    assert set(results) == {"eA", "eB"}

    statuses = sorted([results["eA"]["status"], results["eB"]["status"]])
    # 全局容量 12：两个各 12 成员、重叠 2 的事件，必有一个拿到 12，另一个只剩共享的 2 → 最低门失败。
    assert statuses == [FAST_PANEL_ACTIVATED, FAST_PANEL_QUEUED], statuses
    loser = next(r for r in results.values() if r["status"] == FAST_PANEL_QUEUED)
    assert loser["reason_code"] == "queued_capacity"
    assert loser["selected_videos"] == 2
    assert loser["panel"] is None

    check = DatabaseManager(str(db_path))
    cs = check.get_session()
    try:
        union = _db_fast_union(cs, E)
        # 全局并集：绝不超过 12（朴素「每事件各 12」会是 24）。
        assert len(union) == FAST_PANEL_CAPACITY, union
        # 唯一胜者的 panel 记 12 个 BVID；失败者不留 panel。
        activated = next(r for r in results.values() if r["status"] == FAST_PANEL_ACTIVATED)
        assert set(activated["panel"]["bvids"]) == union
        for event_id in ("eA", "eB"):
            history = _event_row(cs, event_id)["history"]
            if results[event_id]["status"] == FAST_PANEL_QUEUED:
                assert not history, f"{event_id} 失败不得留下 panel"
            else:
                assert history and len(history) == 1
    finally:
        cs.close()
        check.engine.dispose()
        mgr_a.engine.dispose()
        mgr_b.engine.dispose()


# ============================================================================
# E43：冻结 F=4 成员，窗中 1 个被撤销 → 分子 3、分母仍 4、不回写 panel、不伪装 1.0
# ============================================================================


def _pure_rev(bvid: str, owner_mid, *, status: str = "accepted", revision: int = 1,
              decision_at_s: int = E - 4 * EW) -> MemberRevision:
    """构造一条成员版本。"""
    return MemberRevision(
        bvid=bvid,
        owner_mid=owner_mid,
        status=status,
        revision=revision,
        decision_at_s=decision_at_s,
        first_seen_s=decision_at_s,
    )


def _render(anchors: list, gap_s: int) -> list:
    """在锚点间按 ``gap_s`` 线性插点，保证采样间隔满足 40 分钟门。"""
    out: list = []
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


def _fast_points() -> list:
    """生成两窗满覆盖的稠密快照点。"""
    anchors = [(E - 2 * EW, 100), (E - EW, 200), (E, 300)]
    return [SnapshotPoint(epoch_s=e, view=v, view_ok=True) for e, v in _render(anchors, FAST_STEP)]


def test_E43_revoked_member_keeps_panel_denominator(tmp_path, session) -> None:
    """E43：冻结 F=4，窗中撤销 1 个 → 覆盖分子 3 / 分母 4，不回写 panel，不伪装 1.0。"""
    panel_members = [("BV1", 1), ("BV2", 2), ("BV3", 3), ("BV4", 4)]
    _seed_event(session, "e1", panel_members)
    _seed_watch(session, "BV1", "BV2", "BV3", "BV4")
    session.commit()

    svc = _service(tmp_path / "e43.db", session_factory=lambda: session)
    r = _activate(svc, "e1", 0, E)
    assert r["status"] == FAST_PANEL_ACTIVATED
    frozen = list(r["panel"]["bvids"])
    assert sorted(frozen) == ["BV1", "BV2", "BV3", "BV4"]
    frozen_effective = r["panel"]["effective_s"]
    frozen_revisions = dict(r["panel"]["member_revisions"])

    # 窗中撤销 BV4（追加 rejected 版本）。
    session.add(
        HotEventMember(
            event_id="e1", bvid="BV4", revision=2, status="rejected",
            first_seen_s=E, decision_at_s=E + 100, rule_version=1, decision_source="manual",
            owner_mid=4,
        )
    )
    session.commit()

    # 后台 reconcile：撤回 BV4 的 events 需求，但**不回写 panel**。
    EventWatchDemandReconciler().reconcile(session, now_s=E + 200)
    session.commit()

    history = _event_row(session, "e1")["history"]
    assert len(history) == 1
    assert history[0]["effective_s"] == frozen_effective
    assert sorted(history[0]["bvids"]) == ["BV1", "BV2", "BV3", "BV4"], "panel 分母不得被缩小"
    assert history[0]["member_revisions"] == frozen_revisions
    # BV4 的 events 需求被撤，其余三个仍在。
    assert (_watch_row(session, "BV4")["demands"] or {}).get("events") is None
    for bvid in ("BV1", "BV2", "BV3"):
        assert "e1" in _watch_row(session, bvid)["demands"]["events"]

    # 覆盖口径：用冻结的 F（4 个）与「当前仍 accepted 的 3 个」算快通道覆盖。
    revisions = {
        "BV1": [_pure_rev("BV1", 1)],
        "BV2": [_pure_rev("BV2", 2)],
        "BV3": [_pure_rev("BV3", 3)],
        "BV4": [
            _pure_rev("BV4", 4, status="accepted", revision=1, decision_at_s=E - 4 * EW),
            _pure_rev("BV4", 4, status="rejected", revision=2, decision_at_s=E - EW),
        ],
    }
    points = {"BV1": _fast_points(), "BV2": _fast_points(), "BV3": _fast_points(), "BV4": []}
    sig = evaluate_early(revisions, points, fast_panel_bvids=frozen, as_of_s=E)

    assert sig["fast_panel_size"] == 4, "分母仍为冻结的 4"
    assert sig["paired_fast_count"] == 3, "可用分子降至 3"
    assert sig["fast_coverage"] == pytest.approx(0.75), "覆盖如实下降"
    assert sig["fast_coverage"] != 1.0, "不得把覆盖伪装 1.0"
    assert sig["eligible_member_count"] == 4, "U 分母不因撤销而缩小"


# ============================================================================
# manual_stop：blocked_by_user / 不自动重开 / 从未来 panel 排除 / 覆盖下降如实显示
# ============================================================================


def test_manual_stop_excluded_from_future_panel_and_not_reopened(tmp_path, session) -> None:
    """manual_stop 的 BVID 从未来 panel 申请中排除，且 04 不自动重开。"""
    _seed_event(session, "e1", [("BV1", 1), ("BV2", 2), ("BV3", 3), ("BV4", 4)])
    _seed_watch(session, "BV1", "BV2", "BV3", "BV4")
    # 02 显式用户停止：BV1
    session.execute(
        text("UPDATE hotspot_watch SET active = 0, stop_reason = 'manual_stop' WHERE bvid = 'BV1'")
    )
    session.commit()

    svc = _service(tmp_path / "manual.db", session_factory=lambda: session)
    r = _activate(svc, "e1", 0, E)
    assert r["status"] == FAST_PANEL_ACTIVATED
    assert set(r["panel"]["bvids"]) == {"BV2", "BV3", "BV4"}, "manual_stop 的 BV1 必须被排除"

    # 不自动重开：active 仍 False、stop_reason 仍 manual_stop、fast_until 未写。
    bv1 = _watch_row(session, "BV1")
    assert bv1["active"] is False and bv1["stop_reason"] == "manual_stop"
    assert bv1["fast_until_s"] is None
    # 派生 reason 现算可见（不落新列）。
    assert EventWatchDemandReconciler().demand_eligibility(session, "BV1") == "blocked_by_user"


# ============================================================================
# 完整快照：只传单事件必须被证伪（别的事件需求不许被静默删除）
# ============================================================================


def test_full_snapshot_single_event_would_be_falsified(tmp_path, session) -> None:
    """只传单事件必须被证伪：激活 e1 不得误撤 e2 的 events 需求。"""
    _seed_event(session, "e1", [("BV1", 1), ("BV2", 2), ("BV3", 3)])
    _seed_event(session, "e2", [("BV4", 4), ("BV5", 5), ("BV6", 6)])
    _seed_watch(session, "BV1", "BV2", "BV3", "BV4", "BV5", "BV6")
    session.commit()

    # 先建完整 events 快照（两事件都在）。
    EventWatchDemandReconciler().reconcile(session, now_s=E)
    session.commit()
    assert "e2" in _watch_row(session, "BV4")["demands"]["events"]

    svc = _service(tmp_path / "snapshot.db", session_factory=lambda: session)
    r = _activate(svc, "e1", 0, E)
    assert r["status"] == FAST_PANEL_ACTIVATED

    # 若内部只传 e1，e2 的需求会被静默移除 —— 断言其仍在。
    for bvid in ("BV4", "BV5", "BV6"):
        events = _watch_row(session, bvid)["demands"]["events"]
        assert "e2" in events, f"{bvid} 的 e2 需求被误撤（未传完整快照）"
    for bvid in ("BV1", "BV2", "BV3"):
        assert "e1" in _watch_row(session, bvid)["demands"]["events"]


# ============================================================================
# 不倒写 + 配置变更产生新 panel 版本
# ============================================================================


def test_no_backfill_effective_s_and_new_panel_version(tmp_path, session) -> None:
    """后台 reconcile 不得倒写 effective_s；再次激活产生新 panel 版本，旧版保留。"""
    _seed_event(session, "e1", [("BV1", 1), ("BV2", 2), ("BV3", 3)])
    _seed_watch(session, "BV1", "BV2", "BV3")
    session.commit()

    svc = _service(tmp_path / "version.db", session_factory=lambda: session)
    r1 = _activate(svc, "e1", 0, E)
    assert r1["status"] == FAST_PANEL_ACTIVATED
    assert r1["panel"]["effective_s"] == E

    # 后台 reconcile 在更晚时刻运行：只修复需求派生状态，不得把 effective_s 倒写 / 前推。
    EventWatchDemandReconciler().reconcile(session, now_s=E + 3600)
    session.commit()
    history = _event_row(session, "e1")["history"]
    assert len(history) == 1 and history[0]["effective_s"] == E

    # 配置变更（此处以再次激活表达）→ 新 panel 版本，旧版一字不动。
    r2 = _activate(svc, "e1", 1, E + 7200)
    assert r2["status"] == FAST_PANEL_ACTIVATED
    history = _event_row(session, "e1")["history"]
    assert len(history) == 2
    assert history[0] == r1["panel"], "旧 panel 版本不得被改写"
    assert history[1]["effective_s"] == E + 7200
    assert history[1]["panel_id"] != history[0]["panel_id"]
    assert _event_row(session, "e1")["revision"] == 2


# ============================================================================
# 降级：关 fast 仍有 events 普通需求（1h）；关 04 撤 events、保 manual/ranking
# ============================================================================


def test_degradation_fast_off_keeps_events_normal_1h(tmp_path, session) -> None:
    """关 fast：申请被拒；events 普通需求仍存在且节奏为 1h。"""
    _seed_event(session, "e1", [("BV1", 1), ("BV2", 2), ("BV3", 3)])
    _seed_watch(session, "BV1", "BV2", "BV3")
    session.commit()

    svc = _service(tmp_path / "deg1.db", session_factory=lambda: session)
    r = svc.activate_fast_panel("e1", 0, E, fast_watch_enabled=False)
    assert r["status"] == FAST_PANEL_REJECTED
    assert _event_row(session, "e1")["history"] is None

    EventWatchDemandReconciler().reconcile(session, now_s=E)
    session.commit()
    row = _watch_row(session, "BV1")
    assert row["demands"]["events"] == {"e1": {"bvids": ["BV1", "BV2", "BV3"]}}
    assert row["sample_interval_s"] == 3600, "关 fast 时 events 普通需求降为 1h"
    assert row["fast_until_s"] is None


def test_degradation_close_04_clears_events_keeps_manual_ranking(tmp_path, session) -> None:
    """关 04：撤全部 events 需求；manual / ranking 原需求继续；无孤儿快采。"""
    _seed_event(session, "e1", [("BV_EV", 1), ("BV_EV2", 2)])
    _seed_watch(session, "BV_EV", "BV_EV2", "BV_MANUAL")
    session.commit()

    rec = EventWatchDemandReconciler(watch_service=WatchService())
    rec.reconcile(session, now_s=E)
    WatchService().reconcile_demands(
        session, namespace="manual", desired={"BV_MANUAL": {"pinned": True}}, now_s=E
    )
    WatchService().reconcile_demands(
        session, namespace="ranking", desired={"BV_MANUAL": {"rank": 3}}, now_s=E
    )
    session.execute(
        text("UPDATE hotspot_watch SET fast_until_s = :f WHERE bvid = 'BV_EV'"), {"f": E + 600}
    )
    session.commit()

    # 04 关闭：事件归档，再重启。
    session.get(HotEvent, "e1").status = "archived"
    session.commit()
    rec.startup_reconcile(session, now_s=E + 1000)
    session.commit()

    ev = _watch_row(session, "BV_EV")
    assert ev["active"] is False and ev["stop_reason"] == "events_revoked"
    assert (ev["demands"] or {}).get("events") in (None, {})
    assert ev["fast_until_s"] is None, "不得留下孤儿快采"

    man = _watch_row(session, "BV_MANUAL")
    assert man["active"] is True
    assert set(man["demands"]) == {"manual", "ranking"}
    assert man["sample_interval_s"] == 3600


# ============================================================================
# 事务内不发网络 + 最后一次 commit（恰好一次）
# ============================================================================


def test_transaction_no_network_and_single_commit(tmp_path, monkeypatch) -> None:
    """网络 / 预算等待在事务外：激活全程不发网络，且恰好一次 commit。"""
    aiohttp = pytest.importorskip("aiohttp")
    from unittest import mock

    mgr = DatabaseManager(str(tmp_path / "nonet.db"))
    seed = mgr.get_session()
    _seed_event(seed, "e1", [("BV1", 1), ("BV2", 2), ("BV3", 3)])
    _seed_watch(seed, "BV1", "BV2", "BV3")
    seed.commit()
    seed.close()

    commits: list = []

    def factory():
        """返回一个 commit 被计数的会话。"""
        db = mgr.get_session()
        original = db.commit

        def _counted():
            commits.append(1)
            return original()

        db.commit = _counted
        return db

    svc = EventAggregationService(session_factory=factory)
    with mock.patch.object(
        aiohttp.ClientSession, "_request", side_effect=AssertionError("network_inside_transaction")
    ):
        result = svc.activate_fast_panel("e1", 0, E, fast_watch_enabled=True)

    assert result["status"] == FAST_PANEL_ACTIVATED
    assert commits == [1], "激活必须恰好 commit 一次（最后一次）"
    mgr.engine.dispose()


# ============================================================================
# 模块级入口等价性
# ============================================================================


def test_module_level_entrypoint(tmp_path) -> None:
    """模块级 ``activate_fast_panel`` 与类方法同语义。"""
    mgr = DatabaseManager(str(tmp_path / "entry.db"))
    s = mgr.get_session()
    _seed_event(s, "e1", [("BV1", 1), ("BV2", 2), ("BV3", 3)])
    _seed_watch(s, "BV1", "BV2", "BV3")
    s.commit()
    s.close()

    result = activate_fast_panel(
        "e1", 0, E, session_factory=mgr.get_session, fast_watch_enabled=True
    )
    assert result["status"] == FAST_PANEL_ACTIVATED
    assert set(result["panel"]["bvids"]) == {"BV1", "BV2", "BV3"}

    chk = mgr.get_session()
    try:
        assert _event_row(chk, "e1")["history"][0]["effective_s"] == E
    finally:
        chk.close()
        mgr.engine.dispose()


# ============================================================================
# discovery 三配置（原案 L1258）：可读且有默认值
# ============================================================================


def test_discovery_config_defaults_and_readable() -> None:
    """discovery 三项：内置默认值正确，且能从 config.yaml 的 hotspot.events 段读取。"""
    assert DEFAULT_DISCOVERY_LEASE_SECONDS == 300
    assert DEFAULT_DISCOVERY_DEADLINE_SECONDS == 120
    assert DEFAULT_MANUAL_DISCOVERY_COOLDOWN_SECONDS == 60

    # 空配置 → 用默认值。
    policy = DiscoveryPolicy.from_config(None)
    assert policy.discovery_lease_seconds == 300
    assert policy.discovery_deadline_seconds == 120
    assert policy.manual_discovery_cooldown_seconds == 60

    # 随包 config.yaml 的 hotspot.events 段可读，并满足 deadline < lease。
    import yaml
    from pathlib import Path

    class _Conf:
        """最小 ConfigManager 替身，直接喂真实 config.yaml 的 hotspot.events 段。"""

        def __init__(self, section):
            self._section = section

        def get(self, key, default=None):
            return self._section if key == "hotspot.events" else default

    cfg_path = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    section = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["hotspot"]["events"]
    tuned = DiscoveryPolicy.from_config(_Conf(section))
    assert tuned.discovery_lease_seconds == 300
    assert tuned.discovery_deadline_seconds == 120
    assert tuned.manual_discovery_cooldown_seconds == 60
    assert tuned.discovery_deadline_seconds < tuned.discovery_lease_seconds

    # 非法值不静默降级。
    with pytest.raises(ValueError):
        DiscoveryPolicy.from_config(_Conf({"discovery_lease_seconds": 100, "discovery_deadline_seconds": 100}))
    with pytest.raises(ValueError):
        DiscoveryPolicy.from_config(_Conf({"discovery_lease_seconds": -1}))


def test_events_namespace_marker_present() -> None:
    """events 命名空间在被整编白名单内（快照口径依赖）。"""
    assert EVENTS_NAMESPACE == "events"
