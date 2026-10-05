"""FishTool 04 · 第四批 e：快采 20 分钟节奏接线验收（真临时 SQLite，不 mock reconcile_demands）。

依据：
- ``FishTool_04_R5执行规格_第四批e_快采节奏接线.md`` §3（快照格式）/ §4（开关与降级）/ §6（预算）/ §7（E-A1~E-A9）；
- 02 案 §6.3 L623-624（快信号：≤12、每 20 分钟、分作者均衡）、§6.5 L648（manual_stop 优先）。

口径：
- 被测是 04 侧 :meth:`EventWatchDemandReconciler.build_events_desired` 的拆键 + ``WatchService``
  的可选需求整编 hook（默认 ``None`` 行为不变）；
- **真临时 SQLite**、真调 02 既有 ``reconcile_demands``（不 mock、不重写）；真读 ``source_demands``
  落库值验节奏（``resolve_interval_s`` / ``namespace_intervals``）；
- 覆盖 E-A1~E-A9 + 「默认 None 行为不变」回归 + 全局 ``#fast`` 并集容量。
"""
from __future__ import annotations

import asyncio
import json
import zlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import DatabaseManager, Video, VideoStats
from core.database.models_hot_event import HotEvent, HotEventMember
from modules.hotspot.event_watch_demands import (
    FAST_WATCH_CAPACITY,
    FAST_WATCH_INTERVAL_S,
    FAST_WATCH_KEY_SUFFIX,
    EventWatchDemandReconciler,
)
from modules.hotspot.risk_control import AdmissionResult, BudgetDecision, LogicalAdmission
from modules.hotspot.watch_demand import namespace_intervals, resolve_interval_s
from modules.hotspot.watch_service import (
    BUDGET_CATEGORY_FAST,
    BUDGET_CATEGORY_NORMAL,
    WATCH_SOURCE,
    WatchService,
)
from modules.hotspot.watch_store import upsert_watch

#: 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int）。
E: int = 1788220800
HOUR: int = 3600
MONO: float = 1000.0
TID: int = 1008


# ============================================================================
# 夹具与装配
# ============================================================================


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：经 ``DatabaseManager`` 建表 + 04 幂等迁移补列，独立连接池。"""
    path = tmp_path / "event_fast_cadence.db"
    mgr = DatabaseManager(str(path))
    mgr.engine.dispose()
    engine = create_engine(f"sqlite:///{path}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def session(db):
    """临时库上的独立会话。"""
    db_session = db()
    try:
        yield db_session
    finally:
        db_session.close()


class RecordingCollector:
    """采集端口替身：记录 bvid，绝不触网。"""

    def __init__(self) -> None:
        self.calls: list = []

    async def collect(self, bvid, *, collection_tid=None, source=WATCH_SOURCE):
        self.calls.append(bvid)
        return 1

    async def collect_admitted(self, bvid, *, admission, collection_tid=None, source=WATCH_SOURCE):
        """带单请求准用凭证的采集替身：记录并转调 collect。"""
        return await self.collect(bvid, collection_tid=collection_tid, source=source)


class FakeBudget:
    """预算替身：指定类别一律拒绝（带 retry_at），其余准许；记录每次 ``try_acquire``。"""

    def __init__(self, *, denied=(), retry_after: float = 30.0) -> None:
        self.denied = set(denied)
        self.retry_after = retry_after
        self.calls: list = []
        self.reserve_calls: list = []
        self.redeem_calls: list = []
        self._seq: int = 0

    def try_acquire(self, kind, now_mono):
        self.calls.append((kind, now_mono))
        if kind in self.denied:
            return BudgetDecision(
                granted=False, retry_at_mono=now_mono + self.retry_after, reason_code="rate_limited"
            )
        return BudgetDecision(granted=True)

    def clock(self):
        """单调时钟替身：返回与本文件固定 MONO 一致的读数。"""
        return MONO

    def peek(self, kind, now_mono):
        """只读视图：记一次查询，不改任何占用（单请求协议的选择器入口）。"""
        return self.try_acquire(kind, now_mono)

    def reserve(self, kind, now_mono, *, operation_key):
        """签发替身准用凭证；denied 类别拒发。reserve 单独记录，不混进 peek/try 序列。"""
        self.reserve_calls.append((kind, now_mono, operation_key))
        if kind in self.denied:
            return AdmissionResult(
                decision=BudgetDecision(
                    granted=False,
                    retry_at_mono=now_mono + self.retry_after,
                    reason_code="rate_limited",
                )
            )
        self._seq += 1
        admission = LogicalAdmission(
            entry_id=f"fake-{self._seq}",
            operation_key=str(operation_key),
            kind=kind,
            issuer=self,
        )
        return AdmissionResult(decision=BudgetDecision(granted=True), admission=admission)

    def redeem(self, admission, *, operation_key, now_mono):
        """替身兑换：记录即可。"""
        self.redeem_calls.append((admission, operation_key, now_mono))

    def release_unused(self, admission):
        """替身释放：总是成功。"""
        return True


def _seed_watch(session, *bvids: str, now: int = E) -> None:
    """把若干 bvid 入 watch 池并提交。"""
    for bvid in bvids:
        upsert_watch(session, bvid=bvid, now_epoch_s=now)
    session.commit()


def _seed_event(session, event_id: str, members, *, status: str = "active") -> None:
    """建一个事件 + 成员（``members`` 为 bvid 或 ``(bvid, status)``）。"""
    session.add(
        HotEvent(
            id=event_id,
            name=f"事件-{event_id}",
            created_s=E,
            updated_s=E,
            status=status,
            current_rule_version=1,
            revision=0,
            source_policy_hash="h1",
        )
    )
    for index, item in enumerate(members):
        if isinstance(item, tuple):
            bvid, member_status = item
        else:
            bvid, member_status = item, "accepted"
        session.add(
            HotEventMember(
                event_id=event_id,
                bvid=bvid,
                revision=1,
                status=member_status,
                first_seen_s=E,
                decision_at_s=E,
                rule_version=1,
                decision_source="manual",
            )
        )
    session.flush()


def _set_panel(session, event_id: str, bvids, *, effective_s: int = E, expires_s: int | None = None) -> None:
    """直接落一条「已生效」的 ``fast_panel_history``（绕开激活流程，聚焦节奏口径）。"""
    event = session.get(HotEvent, event_id)
    history = list(event.fast_panel_history or [])
    history.append(
        {
            "panel_id": f"{event_id}:{effective_s}",
            "effective_s": effective_s,
            "expires_s": (E + 7200 if expires_s is None else expires_s),
            "ttl_s": 7200,
            "interval_s": FAST_WATCH_INTERVAL_S,
            "bvids": [str(b) for b in bvids],
            "member_revisions": {},
        }
    )
    event.fast_panel_history = history
    session.flush()


def _reject_member(session, event_id: str, bvid: str, *, revision: int = 2) -> None:
    """追加一条 ``rejected`` 成员版本（最新 revision 生效）。"""
    session.add(
        HotEventMember(
            event_id=event_id,
            bvid=bvid,
            revision=revision,
            status="rejected",
            first_seen_s=E,
            decision_at_s=E,
            rule_version=1,
            decision_source="manual",
        )
    )
    session.flush()


def _manual_stop(session, bvid: str) -> None:
    """模拟 02 全局用户停止命令（``active=0 AND stop_reason='manual_stop'``）。"""
    session.execute(
        text("UPDATE hotspot_watch SET active = 0, stop_reason = 'manual_stop' WHERE bvid = :b"),
        {"b": bvid},
    )


def _row(session, bvid: str) -> dict:
    """读某 bvid 的关键调度列（``demands`` 已解析，另留原始字节）。"""
    row = session.execute(
        text(
            "SELECT active, stop_reason, source_demands, sample_interval_s, state_revision "
            "FROM hotspot_watch WHERE bvid = :b"
        ),
        {"b": bvid},
    ).first()
    if row is None:
        return {}
    raw = row[2]
    return {
        "active": bool(row[0]),
        "stop_reason": row[1],
        "demands_raw": raw,
        "demands": None if raw is None else json.loads(raw),
        "sample_interval_s": row[3],
        "state_revision": row[4],
    }


def _events(session, bvid: str) -> dict:
    """取某 bvid 的 events 子快照（无则 {}）。"""
    return (_row(session, bvid).get("demands") or {}).get("events") or {}


def _next_due(session, bvid: str):
    """读某 bvid 的 ``next_due_epoch_s``。

    供 E-A7b 用落库层证据证明「被跳过的一行原地不动、入选的一行才被推进」——这是
    「fast 桶用尽 → fast 被跳过、normal 仍入选（不被饿死）」的硬证据。

    Args:
        session: 只读会话。
        bvid: 目标 BV 号。

    Returns:
        int | None: 该行的 next_due_epoch_s；行不存在时 None。
    """
    return session.execute(
        text("SELECT next_due_epoch_s FROM hotspot_watch WHERE bvid = :b"), {"b": bvid}
    ).scalar()


def _reconciler(**kwargs) -> EventWatchDemandReconciler:
    """构造 04 侧对账器（复用真实 ``WatchService``；显式给开关，不依赖环境变量）。"""
    kwargs.setdefault("watch_service", WatchService())
    kwargs.setdefault("clock", lambda: E)
    return EventWatchDemandReconciler(**kwargs)


def _seed_snapshots(db, bvid: str, points) -> None:
    """写入 ``videos`` / ``video_stats``（质量 ok），供算法评估。"""
    db_session = db()
    try:
        digits = "".join(char for char in bvid if char.isdigit())
        # aid 全局唯一（videos.aid unique）：无数字的 bvid（如 BV_FAST/BV_NORM）用稳定 CRC32 派生，避免撞键
        aid = int(digits) if digits else (zlib.crc32(bvid.encode("utf-8")) & 0x7FFFFFFF)
        video = Video(bvid=bvid, aid=aid, tid=TID, title="t", mid=1, author="a")
        db_session.add(video)
        db_session.flush()
        for epoch_s, view in points:
            db_session.add(
                VideoStats(
                    video_id=video.id,
                    view=view,
                    snapshot_time=datetime.fromtimestamp(epoch_s),
                    source=WATCH_SOURCE,
                    captured_epoch_s=epoch_s,
                    collection_tid=TID,
                    raw_tid=TID,
                    view_status="ok",
                    stat_status="ok",
                    metric_status={"view": "ok"},
                )
            )
        db_session.commit()
    finally:
        db_session.close()


# ============================================================================
# E-A1：开关关 → 无 #fast 且逐字节一致
# ============================================================================


def test_EA1_fast_off_snapshot_has_no_fast_and_is_byte_identical(session) -> None:
    """E-A1：开关关时，即使存在生效 panel，快照也**无** ``#fast``，且与旧实现逐字节一致。"""
    _seed_watch(session, "BV_A", "BV_B")
    _seed_event(session, "e1", ["BV_A", "BV_B"])
    _set_panel(session, "e1", ["BV_A"])  # panel 存在，但开关关
    session.commit()

    rec = _reconciler(fast_watch_enabled=False)
    desired = rec.reconcile(session, now_s=E)
    session.commit()

    # 无 #fast 键，只有本体键。
    assert set(desired) == {"e1"}
    assert fast_key_in(desired) is False
    assert desired == {"e1": {"bvids": ["BV_A", "BV_B"]}}

    # 逐字节一致：落库 JSON == 旧实现（仅本体键）的序列化结果。
    legacy = json.dumps({"events": {"e1": {"bvids": ["BV_A", "BV_B"]}}}, ensure_ascii=False, sort_keys=True)
    assert _row(session, "BV_A")["demands_raw"] == legacy
    assert _row(session, "BV_B")["demands_raw"] == legacy

    # 无抖动：再跑一轮，字节完全不变。
    rec.reconcile(session, now_s=E)
    session.commit()
    assert _row(session, "BV_A")["demands_raw"] == legacy


def fast_key_in(desired: dict) -> bool:
    """报告中用的小工具：desired 里是否出现任意 ``#fast`` 键。"""
    return any(str(key).endswith(FAST_WATCH_KEY_SUFFIX) for key in desired)


# ============================================================================
# E-A2：panel 成员取到 1200；非 panel 仍 3600
# ============================================================================


def test_EA2_panel_members_get_1200_others_stay_3600(session) -> None:
    """E-A2：panel 成员经 ``namespace_intervals``/``resolve_interval_s`` 取到 1200，非 panel 仍 3600。"""
    _seed_watch(session, "BV_P1", "BV_P2", "BV_OTHER")
    _seed_event(session, "e1", ["BV_P1", "BV_P2", "BV_OTHER"])
    _set_panel(session, "e1", ["BV_P1", "BV_P2"], expires_s=E + 7200)
    session.commit()

    _reconciler(fast_watch_enabled=True).reconcile(session, now_s=E)
    session.commit()

    panel = _row(session, "BV_P1")
    # 键后缀不产生节奏：节奏只来自 descriptor 内 interval_s。
    assert set(panel["demands"]["events"]) == {"e1", "e1#fast"}
    assert panel["demands"]["events"]["e1#fast"] == {
        "bvids": ["BV_P1", "BV_P2"],
        "interval_s": FAST_WATCH_INTERVAL_S,
    }
    assert namespace_intervals(panel["demands"]) == {"events": 1200}
    assert resolve_interval_s(panel["demands"]) == 1200
    assert panel["sample_interval_s"] == 1200  # 02 侧 reschedule_watch 真落库

    other = _row(session, "BV_OTHER")
    assert set(other["demands"]["events"]) == {"e1"}  # 非 panel 只有本体键
    assert resolve_interval_s(other["demands"]) == 3600
    assert other["sample_interval_s"] == 3600


# ============================================================================
# E-A3：panel 过期 → 下轮无 #fast 且退回 3600
# ============================================================================


def test_EA3_expired_panel_drops_fast_and_falls_back_to_3600(session) -> None:
    """E-A3：panel 过期后下一轮快照无 ``#fast``，``reschedule_watch`` 把节奏退回 3600。"""
    _seed_watch(session, "BV_P1", "BV_P2", "BV_OTHER")
    _seed_event(session, "e1", ["BV_P1", "BV_P2", "BV_OTHER"])
    _set_panel(session, "e1", ["BV_P1", "BV_P2"], effective_s=E, expires_s=E + 600)
    session.commit()

    rec = _reconciler(fast_watch_enabled=True)
    rec.reconcile(session, now_s=E)
    session.commit()
    assert "e1#fast" in _events(session, "BV_P1")
    assert _row(session, "BV_P1")["sample_interval_s"] == 1200

    # 下一轮：panel 已过期（now > expires_s）。
    rec.reconcile(session, now_s=E + 601)
    session.commit()

    p1 = _row(session, "BV_P1")
    assert "e1#fast" not in p1["demands"]["events"]
    assert p1["sample_interval_s"] == 3600  # 退回普通节奏
    assert resolve_interval_s(p1["demands"]) == 3600
    # 本体键一字不少（成员仍是有效成员）。
    assert p1["demands"]["events"]["e1"] == {"bvids": ["BV_OTHER", "BV_P1", "BV_P2"]}


# ============================================================================
# E-A4：被拒 / manual_stop → 从 #fast 移除，不影响本体键其它成员
# ============================================================================


def test_EA4_rejected_and_manual_stop_removed_from_fast_only(session) -> None:
    """E-A4：``rejected``/``manual_stop`` 成员从 ``#fast`` 移除；本体键与其它成员不受影响。"""
    _seed_watch(session, "BV_A", "BV_B", "BV_C")
    _seed_event(session, "e1", ["BV_A", "BV_B", "BV_C"])
    _set_panel(session, "e1", ["BV_A", "BV_B", "BV_C"], expires_s=E + 7200)
    session.commit()

    rec = _reconciler(fast_watch_enabled=True)
    rec.reconcile(session, now_s=E)
    session.commit()
    assert _events(session, "BV_A")["e1#fast"]["bvids"] == ["BV_A", "BV_B", "BV_C"]

    # BV_A 被拒（最新 revision）；BV_B 被用户手动停止。
    _reject_member(session, "e1", "BV_A", revision=2)
    _manual_stop(session, "BV_B")
    session.commit()

    rec.reconcile(session, now_s=E + 10)
    session.commit()

    # BV_A 被拒：本体键与 #fast 都移除。
    assert "e1" not in _events(session, "BV_A")
    # BV_B manual_stop：本体键仍在（accepted），#fast 移除；不被自动重开。
    b_events = _events(session, "BV_B")
    assert "e1" in b_events and "e1#fast" not in b_events
    assert _row(session, "BV_B")["active"] is False
    # BV_C 不受影响：#fast 仍含 C，本体键仍含 B/C。
    c_events = _events(session, "BV_C")
    assert c_events["e1#fast"]["bvids"] == ["BV_C"]
    assert c_events["e1"] == {"bvids": ["BV_B", "BV_C"]}


# ============================================================================
# E-A5：共享 BVID 只占 1、并集 ≤12
# ============================================================================


def test_EA5a_single_event_panel_union_capped_at_12(session) -> None:
    """E-A5：单事件 panel 超过 12 时，``#fast`` 并集截到 12。"""
    many = [f"BV_M{i:02d}" for i in range(13)]
    _seed_watch(session, *many)
    _seed_event(session, "e1", many)
    _set_panel(session, "e1", many, expires_s=E + 7200)
    session.commit()

    _reconciler(fast_watch_enabled=True).reconcile(session, now_s=E)
    session.commit()

    fast = _events(session, many[0])["e1#fast"]["bvids"]
    assert len(fast) == FAST_WATCH_CAPACITY == 12
    assert len(set(fast)) == len(fast)  # 无重复


def test_EA5b_shared_bvid_counts_once_across_events(session) -> None:
    """E-A5：跨事件共享 BVID 只占 1 名额（并集按 set 计）。"""
    _seed_watch(session, "BV_S", "BV_A1", "BV_A2", "BV_B1", "BV_B2")
    _seed_event(session, "e1", ["BV_S", "BV_A1", "BV_A2"])
    _seed_event(session, "e2", ["BV_S", "BV_B1", "BV_B2"])
    _set_panel(session, "e1", ["BV_S", "BV_A1", "BV_A2"], expires_s=E + 7200)
    _set_panel(session, "e2", ["BV_S", "BV_B1", "BV_B2"], expires_s=E + 7200)
    session.commit()

    _reconciler(fast_watch_enabled=True).reconcile(session, now_s=E)
    session.commit()

    # 共享 BV_S 的行拿到两个事件的本体键 + 两个 #fast 键。
    shared = _events(session, "BV_S")
    assert set(shared) == {"e1", "e1#fast", "e2", "e2#fast"}
    # 每个 #fast 内 BV_S 只出现 1 次。
    assert shared["e1#fast"]["bvids"].count("BV_S") == 1
    assert shared["e2#fast"]["bvids"].count("BV_S") == 1

    union: set = set()
    for key in ("e1#fast", "e2#fast"):
        union.update(shared[key]["bvids"])
    # 共享 BVID 计一次：5 个独立 BVID，而不是 6。
    assert union == {"BV_S", "BV_A1", "BV_A2", "BV_B1", "BV_B2"}
    assert len(union) == 5 <= FAST_WATCH_CAPACITY


# ============================================================================
# E-A6：键稳定（无抖动）
# ============================================================================


def test_EA6_stable_keys_no_churn_on_second_round(session) -> None:
    """E-A6：连续两轮相同需求，第二轮 ``merged == current`` 跳过（代际不动、字节不变）。"""
    _seed_watch(session, "BV_P1", "BV_P2", "BV_OTHER")
    _seed_event(session, "e1", ["BV_P1", "BV_P2", "BV_OTHER"])
    _set_panel(session, "e1", ["BV_P1", "BV_P2"], expires_s=E + 7200)
    session.commit()

    rec = _reconciler(fast_watch_enabled=True)
    rec.reconcile(session, now_s=E)
    session.commit()
    raw1 = _row(session, "BV_P1")["demands_raw"]
    rev1 = _row(session, "BV_P1")["state_revision"]

    rec.reconcile(session, now_s=E)  # 完全相同需求
    session.commit()

    assert _row(session, "BV_P1")["demands_raw"] == raw1
    assert _row(session, "BV_P1")["state_revision"] == rev1  # 无 UPDATE -> 代际不动


# ============================================================================
# E-A7：fast 桶用尽 → fast 跳过、normal 仍入选（不互相饿死）
# ============================================================================


def test_EA7a_fast_budget_exhausted_skips_fast_keeps_normal(session) -> None:
    """E-A7：``fast`` 桶用尽立即跳过该类，``normal`` 仍入选。"""
    _seed_watch(session, "BV_FAST", "BV_NORM")
    session.execute(
        text("UPDATE hotspot_watch SET next_due_epoch_s = :nd WHERE bvid = 'BV_FAST'"),
        {"nd": E - 100},
    )
    session.execute(
        text("UPDATE hotspot_watch SET next_due_epoch_s = :nd WHERE bvid = 'BV_NORM'"),
        {"nd": E - 50},
    )
    session.execute(
        text("UPDATE hotspot_watch SET fast_until_s = :f WHERE bvid = 'BV_FAST'"),
        {"f": E + HOUR},
    )
    session.commit()

    budget = FakeBudget(denied=(BUDGET_CATEGORY_FAST,), retry_after=30.0)
    svc = WatchService(now_fn=lambda: E, now_mono_fn=lambda: MONO)
    selection = svc._select_targets(session, E, 10, budget=budget)

    assert [t.bvid for t in selection.targets] == ["BV_NORM"]
    assert selection.budget_skipped == 1
    assert selection.budget_exhausted is False
    # 先试 fast（更早到期、被拒）→ 再选 normal（未被挡死）。
    assert [kind for kind, _ in budget.calls] == [BUDGET_CATEGORY_FAST, BUDGET_CATEGORY_NORMAL]


def test_EA7b_run_tick_skips_fast_still_collects_normal(db) -> None:
    """E-A7（端到端）：真跑一轮 tick，fast 桶用尽 → fast 被跳过、normal 仍入选并照常采集。

    证明要点（排除「恰好跑过」式假绿）：
    1. 两行都到点，BV_FAST 带生效中的 ``fast_until_s`` → 归 ``fast_watch`` 桶；
    2. ``fast_watch`` 桶被拒 → BV_FAST 被跳过：不采集、``next_due_epoch_s`` 原地不动；
    3. ``normal_watch`` 桶未耗尽 → BV_NORM 仍入选：真采集、真推进 ``next_due_epoch_s``；
    4. 轮级 ``budget_exhausted`` 仍为 False（fast 桶耗尽 ≠ 整轮无一项可跑）。
    """
    db_session = db()
    try:
        _seed_watch(db_session, "BV_FAST", "BV_NORM")
        db_session.execute(
            text("UPDATE hotspot_watch SET next_due_epoch_s = :nd WHERE bvid = 'BV_FAST'"),
            {"nd": E - 100},
        )
        db_session.execute(
            text("UPDATE hotspot_watch SET next_due_epoch_s = :nd WHERE bvid = 'BV_NORM'"),
            {"nd": E - 50},
        )
        db_session.execute(
            text("UPDATE hotspot_watch SET fast_until_s = :f WHERE bvid = 'BV_FAST'"),
            {"f": E + HOUR},
        )
        db_session.commit()
    finally:
        db_session.close()
    _seed_snapshots(db, "BV_FAST", [(E - 86400, 100), (E, 900)])
    _seed_snapshots(db, "BV_NORM", [(E - 86400, 100), (E, 900)])

    collector = RecordingCollector()
    budget = FakeBudget(denied=(BUDGET_CATEGORY_FAST,), retry_after=30.0)
    svc = WatchService(
        collector_port=collector,
        session_factory=db,
        now_fn=lambda: E,
        now_mono_fn=lambda: MONO,
        budget=budget,
    )
    result = asyncio.run(svc.run_tick(limit=10))

    assert collector.calls == ["BV_NORM"]  # fast 未被采集、normal 照常
    assert result.committed == 1
    assert result.budget_skipped == 1

    # ---- 真证明：不是「恰好跑过」，而是「fast 桶用尽 → fast 被跳过、normal 不被饿死」----
    # 1) fast 桶确实被「用尽」：BV_FAST 先归 fast_watch 且被拒，其后才轮到 normal_watch 放行。
    assert [kind for kind, _ in budget.calls] == [
        BUDGET_CATEGORY_FAST,
        BUDGET_CATEGORY_NORMAL,
    ]

    db_session = db()
    try:
        fast_due = _next_due(db_session, "BV_FAST")
        norm_due = _next_due(db_session, "BV_NORM")
    finally:
        db_session.close()

    # 2) 被跳过的确实是 BV_FAST：它原地不动（未采集、调度未被推进）。
    assert fast_due == E - 100
    assert "BV_FAST" not in collector.calls
    # 3) normal 未被 fast 连坐（不互相饿死）：真被采集、且调度真被推进到下一拍。
    assert norm_due == E + HOUR
    # 4) 并非「无一项可跑」，故 budget_exhausted=False（fast 桶耗尽 ≠ 整轮耗尽）。
    assert result.budget_exhausted is False
    assert result.due_count == 1


# ============================================================================
# E-A8：events descriptor 缺 bvids → invalid_desired_entry（守住 02 硬门）
# ============================================================================


def test_EA8_events_descriptor_without_bvids_raises(session) -> None:
    """E-A8：events 条目既无 ``bvid`` 也无 ``bvids`` → ``invalid_desired_entry``。"""
    _seed_watch(session, "BV1")
    with pytest.raises(ValueError, match="invalid_desired_entry"):
        WatchService().reconcile_demands(
            session, namespace="events", desired={"e1": {"foo": "bar"}}, now_s=E
        )


# ============================================================================
# E-A9：关 04 → events 撤净、manual/ranking 一字不动
# ============================================================================


def test_EA9_disable_04_clears_events_keeps_manual_ranking(session) -> None:
    """E-A9：关 04（无 active 事件）后 events 需求全撤（含 ``#fast``），manual/ranking 一字不动。"""
    _seed_watch(session, "BV_EV", "BV_MAN")
    _seed_event(session, "e1", ["BV_EV"])
    _set_panel(session, "e1", ["BV_EV"], expires_s=E + 7200)
    WatchService().reconcile_demands(
        session, namespace="manual", desired={"BV_MAN": {"pinned": True}}, now_s=E
    )
    WatchService().reconcile_demands(
        session, namespace="ranking", desired={"BV_MAN": {"rank": 3}}, now_s=E
    )
    session.commit()

    rec = _reconciler(fast_watch_enabled=True)
    rec.reconcile(session, now_s=E)
    session.commit()
    assert set(_events(session, "BV_EV")) == {"e1", "e1#fast"}

    man = _row(session, "BV_MAN")
    manual_before = json.dumps(man["demands"]["manual"], sort_keys=True)
    ranking_before = json.dumps(man["demands"]["ranking"], sort_keys=True)

    # 关 04：事件归档（不再有 active 事件）。
    event = session.get(HotEvent, "e1")
    event.status = "archived"
    session.commit()

    rec.reconcile(session, now_s=E + 10)
    session.commit()

    # events（含 #fast）撤净；仅 events 目标停采，保留历史行。
    ev_row = _row(session, "BV_EV")
    assert _events(session, "BV_EV") == {}
    assert ev_row["active"] is False and ev_row["stop_reason"] == "events_revoked"

    # manual / ranking 一字不动。
    man_after = _row(session, "BV_MAN")
    assert json.dumps(man_after["demands"]["manual"], sort_keys=True) == manual_before
    assert json.dumps(man_after["demands"]["ranking"], sort_keys=True) == ranking_before


# ============================================================================
# WatchService hook：默认 None 行为不变 + 可注入 reconcile（4e §5）
# ============================================================================


def _setup_tick_db(db) -> None:
    """铺一条到点的 watch 行 + 快照，供 tick 跑满一轮。"""
    db_session = db()
    try:
        _seed_watch(db_session, "BV_HOOK")
        db_session.execute(
            text("UPDATE hotspot_watch SET next_due_epoch_s = :nd WHERE bvid = 'BV_HOOK'"),
            {"nd": E - 1},
        )
        db_session.commit()
    finally:
        db_session.close()
    _seed_snapshots(db, "BV_HOOK", [(E - 86400, 100), (E, 900)])


def _run_one_tick(path, *, hook):
    """在独立临时库里跑一轮 tick，返回（轮级标量, 行关键列, 采集调用）。"""
    mgr = DatabaseManager(str(path))
    mgr.engine.dispose()
    engine = create_engine(f"sqlite:///{path}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        _setup_tick_db(factory)
        collector = RecordingCollector()
        svc = WatchService(
            collector_port=collector,
            session_factory=factory,
            now_fn=lambda: E,
            demand_reconcile_hook=hook,
        )
        result = asyncio.run(svc.run_tick(limit=10))
        db_session = factory()
        try:
            row = _row(db_session, "BV_HOOK")
        finally:
            db_session.close()
        scalars = (
            result.now_epoch_s,
            result.released,
            result.due_count,
            result.committed,
            result.dropped,
            result.failed,
            result.budget_skipped,
            result.budget_exhausted,
            tuple(collector.calls),
        )
        return scalars, row
    finally:
        engine.dispose()


def test_hook_default_is_none() -> None:
    """默认构造的 ``WatchService`` 不接线 hook（行为与现状一致）。"""
    assert WatchService().demand_reconcile_hook is None


def test_hook_default_none_matches_noop_hook(tmp_path) -> None:
    """回归证明：默认 ``None`` 与「注入一个不做任何事的 hook」逐字段等价。

    证明 ``run_tick`` 里唯一差异就是 ``if hook is not None`` 这一次可选调用；hook 为
    ``None`` 时整段跳过 → 与既有 tick 行为（轮级结果 + 落库列）逐字段一致。
    """
    scalars_default, row_default = _run_one_tick(tmp_path / "default.db", hook=None)

    calls: list = []

    def _noop_hook(session, *, now_s):
        """不做任何库改动的 hook：用于隔离「被调用」与「被改变」。"""
        calls.append(now_s)

    scalars_noop, row_noop = _run_one_tick(tmp_path / "noop.db", hook=_noop_hook)

    assert calls == [E]  # 注入后每轮被调一次，且带上 now_s
    assert scalars_default == scalars_noop
    assert row_default == row_noop


def test_hook_injected_reconcile_is_called_once_per_tick(db) -> None:
    """真实注入缝：``EventWatchDemandReconciler.reconcile`` 可直接作为 hook 注入并被调用。"""
    _setup_tick_db(db)
    _seed_watch_db_events(db)

    rec = _reconciler(fast_watch_enabled=True)
    svc = WatchService(
        collector_port=RecordingCollector(),
        session_factory=db,
        now_fn=lambda: E,
        demand_reconcile_hook=rec.reconcile,  # 组装处直接注入 04 的 reconcile（签名 (session, *, now_s)）
    )
    asyncio.run(svc.run_tick(limit=10))

    # hook 真跑：events 需求（含 #fast）落库。
    db_session = db()
    try:
        assert "e1#fast" in _events(db_session, "BV_P1")
    finally:
        db_session.close()


def _seed_watch_db_events(db) -> None:
    """给 hook 注入用例铺一个带 panel 的事件。"""
    db_session = db()
    try:
        _seed_watch(db_session, "BV_P1", "BV_P2")
        _seed_event(db_session, "e1", ["BV_P1", "BV_P2"])
        _set_panel(db_session, "e1", ["BV_P1", "BV_P2"], expires_s=E + 7200)
        db_session.commit()
    finally:
        db_session.close()
