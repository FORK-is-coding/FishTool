"""FishTool 04 · 第三批 c：事件 → watch 共享需求映射验收（真事务，围栏不可打桩）。

依据：
- ``FishTool_02_..._Agent执行(1).md`` §8.3（L677-682）；
- ``FishTool_04_R5执行规格_第三批c`` §1.2 / §5（E19 / E48 / E49 / E51）。

口径：
- 被测是 **04 侧调用方** :class:`EventWatchDemandReconciler`，它调用**第二批已验收**的
  ``WatchService.reconcile_demands``（**不 mock、不重写**）；
- 真实临时 SQLite 事务；``reconcile_demands`` 只 flush 不 commit（钉死），事务未提交可被 rollback 证伪；
- 覆盖 E19 / E48 / E49 / E51 + 2 事件→1 事件撤销 + 非法 namespace + manual/ranking 两轮一字不变。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest import mock

import pytest
from sqlalchemy import text

from core.database import DatabaseManager
from core.database.models_hot_event import HotEvent, HotEventMember
from modules.hotspot.event_watch_demands import (
    EVENTS_NAMESPACE,
    EventWatchDemandReconciler,
)
from modules.hotspot.watch_service import DEMAND_NAMESPACES, WatchService
from modules.hotspot.watch_store import upsert_watch

#: 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int）。
E: int = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())


@pytest.fixture()
def session(tmp_path):
    """临时文件库（含 04 迁移列）上的独立会话。"""
    mgr = DatabaseManager(str(tmp_path / "event_watch_demand.db"))
    db = mgr.get_session()
    try:
        yield db
    finally:
        db.close()
        mgr.engine.dispose()


def _seed_watch(session, *bvids: str, now: int = E) -> None:
    """把若干 bvid 入 watch 池并提交。"""
    for bvid in bvids:
        upsert_watch(session, bvid=bvid, now_epoch_s=now)
    session.commit()


def _seed_event(session, event_id: str, bvids, *, status: str = "active", member_status: str = "accepted") -> None:
    """建一个事件 + 成员（直接落 ORM，避免额外会话）。"""
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
    for index, bvid in enumerate(bvids):
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


def _set_event_status(session, event_id: str, status: str) -> None:
    """改事件状态并提交（模拟暂停 / 归档）。"""
    event = session.get(HotEvent, event_id)
    event.status = status
    session.commit()


def _row(session, bvid: str) -> dict:
    """读出某 bvid 的关键调度列（demands 已解析）。"""
    row = session.execute(
        text(
            "SELECT active, stop_reason, source_demands, sample_interval_s, fast_until_s "
            "FROM hotspot_watch WHERE bvid = :b"
        ),
        {"b": bvid},
    ).first()
    if row is None:
        return {}
    demands = None if row[2] is None else json.loads(row[2])
    return {
        "active": bool(row[0]),
        "stop_reason": row[1],
        "demands": demands,
        "sample_interval_s": row[3],
        "fast_until_s": row[4],
    }


def _events(session, bvid: str) -> dict:
    """取某 bvid 的 events 子快照（无则 {}）。"""
    demands = _row(session, bvid).get("demands") or {}
    return demands.get("events") or {}


def _make_reconciler() -> EventWatchDemandReconciler:
    """构造 04 侧对账器（复用真实 WatchService）。"""
    return EventWatchDemandReconciler(watch_service=WatchService(), clock=lambda: E)


# ===========================================================================
# 只 flush：reconcile 后事务未提交（钉死）
# ===========================================================================


def test_reconcile_flush_only_not_committed(session) -> None:
    """``reconcile`` 只 flush：调用后事务未 commit，rollback 即整段回退。"""
    _seed_watch(session, "BV1")
    _seed_event(session, "e1", ["BV1"], status="active")
    session.commit()

    reconciler = _make_reconciler()
    with mock.patch.object(session, "commit", wraps=session.commit) as commit_spy:
        reconciler.reconcile(session, now_s=E)
        assert commit_spy.call_count == 0, "reconcile 不得 commit"

    # flush 已生效：同一会话内可见。
    assert _events(session, "BV1") == {"e1": {"bvids": ["BV1"]}}

    # 未提交 → rollback 后需求消失（证明确实没落库）。
    session.rollback()
    assert _row(session, "BV1")["demands"] is None


# ===========================================================================
# E19：两事件共享 watch，暂停其一 → 其它需求仍在，不停掉共享采样
# ===========================================================================


def test_E19_pause_one_event_keeps_shared_demand(session) -> None:
    """E19：e1/e2 共享 BV_SHARED；暂停 e1 后 e2 需求仍在，BV_SHARED 不被停采。"""
    _seed_watch(session, "BV_SHARED", "BV_ONLY1")
    _seed_event(session, "e1", ["BV_SHARED", "BV_ONLY1"])
    _seed_event(session, "e2", ["BV_SHARED"])
    session.commit()

    reconciler = _make_reconciler()
    reconciler.reconcile(session, now_s=E)
    session.commit()
    assert set(_events(session, "BV_SHARED")) == {"e1", "e2"}

    # 暂停 e1（不是 02 的显式 DELETE）。
    _set_event_status(session, "e1", "paused")

    reconciler.reconcile(session, now_s=E + 10)
    session.commit()

    # 共享目标仍保留 e2 需求，样本不被停采。
    shared = _row(session, "BV_SHARED")
    assert set(shared["demands"]["events"]) == {"e2"}
    assert shared["active"] is True and shared["stop_reason"] != "events_revoked"

    # 仅 e1 独占的 BV_ONLY1 无任何需求 → 停采（保留历史行）。
    only1 = _row(session, "BV_ONLY1")
    assert only1["active"] is False and only1["stop_reason"] == "events_revoked"


# ===========================================================================
# E48：用户手动停止的 bvid，reconcile 不重启、blocked_by_user 可见
# ===========================================================================


def test_E48_manual_stop_not_restarted_blocked_by_user(session) -> None:
    """E48：02 显式 DELETE（manual_stop）的 bvid，被多事件引用也不被 04 重启。"""
    _seed_watch(session, "BV_STOP")
    # 模拟 02 的全局用户停止命令。
    session.execute(
        text(
            "UPDATE hotspot_watch SET active = 0, stop_reason = 'manual_stop' "
            "WHERE bvid = 'BV_STOP'"
        )
    )
    session.commit()

    _seed_event(session, "e1", ["BV_STOP"])
    _seed_event(session, "e2", ["BV_STOP"])
    session.commit()

    reconciler = _make_reconciler()
    reconciler.reconcile(session, now_s=E)
    session.commit()

    row = _row(session, "BV_STOP")
    assert row["active"] is False, "04 reconcile 重启了被用户手动停止的目标"
    assert row["stop_reason"] == "manual_stop"
    # blocked_by_user 现算可见（不落新列）。
    assert reconciler.demand_eligibility(session, "BV_STOP") == "blocked_by_user"


# ===========================================================================
# E49：04 关闭后重启，清理遗留 events 需求并重算节奏；manual/ranking 继续、无孤儿快采
# ===========================================================================


def test_E49_restart_clears_events_keeps_manual_ranking_no_orphan_fast(session) -> None:
    """E49：重启后遗留 events 需求被清、节奏重算；manual/ranking 继续、无孤儿快采。"""
    _seed_watch(session, "BV_EVENTS", "BV_MANUAL")
    _seed_event(session, "e1", ["BV_EVENTS"], status="active")
    session.commit()

    reconciler = _make_reconciler()
    # 先制造「上一轮遗留」：events 需求 + manual/ranking 需求 + 一个孤儿快采。
    reconciler.reconcile(session, now_s=E)
    WatchService().reconcile_demands(
        session, namespace="manual", desired={"BV_MANUAL": {"pinned": True}}, now_s=E
    )
    WatchService().reconcile_demands(
        session, namespace="ranking", desired={"BV_MANUAL": {"rank": 3}}, now_s=E
    )
    session.execute(
        text("UPDATE hotspot_watch SET fast_until_s = :f WHERE bvid = 'BV_MANUAL'"),
        {"f": E + 600},
    )
    session.commit()
    assert _row(session, "BV_MANUAL")["fast_until_s"] == E + 600

    # 04 关闭：事件服务停止（无 active 事件），再重启应用。
    _set_event_status(session, "e1", "archived")

    reconciler.startup_reconcile(session, now_s=E + 1000)
    session.commit()

    # 遗留 events 需求被清；BV_EVENTS 无任何需求 → 停采，历史行保留。
    events_row = _row(session, "BV_EVENTS")
    assert _events(session, "BV_EVENTS") == {}
    assert events_row["active"] is False and events_row["stop_reason"] == "events_revoked"

    # manual / ranking 继续。
    manual_row = _row(session, "BV_MANUAL")
    assert manual_row["active"] is True
    assert set(manual_row["demands"]) == {"manual", "ranking"}
    assert manual_row["sample_interval_s"] == 3600  # 退回普通节奏

    # 无孤儿快采。
    assert manual_row["fast_until_s"] is None


# ===========================================================================
# E51：事件 A 更新、B 仍 active → 输入完整 events 映射，只撤 A 不误撤全 namespace
# ===========================================================================


def test_E51_update_one_event_only_affects_that_event(session) -> None:
    """E51：e1 更新（撤 BV1 / 增 BV3）而 e2 仍 active → 只撤 e1，e2 需求不动。"""
    _seed_watch(session, "BV1", "BV2", "BV3")
    _seed_event(session, "e1", ["BV1"])
    _seed_event(session, "e2", ["BV2"])
    session.commit()

    reconciler = _make_reconciler()
    reconciler.reconcile(session, now_s=E)
    session.commit()
    assert set(_events(session, "BV1")) == {"e1"}
    assert set(_events(session, "BV2")) == {"e2"}

    # e1 更新：拒掉 BV1、接受 BV3；e2 不变。
    session.add(
        HotEventMember(
            event_id="e1", bvid="BV1", revision=2, status="rejected",
            first_seen_s=E, decision_at_s=E, rule_version=1, decision_source="manual",
        )
    )
    session.add(
        HotEventMember(
            event_id="e1", bvid="BV3", revision=1, status="accepted",
            first_seen_s=E, decision_at_s=E, rule_version=1, decision_source="manual",
        )
    )
    session.commit()

    desired = reconciler.reconcile(session, now_s=E + 20)
    session.commit()

    # 输入是完整 events 映射（e1 + e2 都在）。
    assert set(desired) == {"e1", "e2"}
    # 只撤 A：BV1 的 e1 需求被撤（无残留）。
    assert _events(session, "BV1") == {}
    # B 需求保持 → 未误撤整个 namespace。
    assert set(_events(session, "BV2")) == {"e2"}
    # e1 的新成员进入需求。
    assert set(_events(session, "BV3")) == {"e1"}


# ===========================================================================
# 完整快照语义：2 事件 → 1 事件，消失者需求被撤销
# ===========================================================================


def test_two_events_then_one_revokes_disappeared(session) -> None:
    """完整快照：先 {e1,e2} 再 {e2}，e1 需求被撤销、不永久残留。"""
    _seed_watch(session, "BV1", "BV2")
    _seed_event(session, "e1", ["BV1"])
    _seed_event(session, "e2", ["BV2"])
    session.commit()

    reconciler = _make_reconciler()
    reconciler.reconcile(session, now_s=E)
    session.commit()
    assert set(_events(session, "BV1")) == {"e1"}

    # e1 消失（归档）。
    _set_event_status(session, "e1", "archived")
    reconciler.reconcile(session, now_s=E + 5)
    session.commit()

    assert _events(session, "BV1") == {}
    assert set(_events(session, "BV2")) == {"e2"}


# ===========================================================================
# namespace 隔离：非法报错 + manual/ranking 两轮一字不变
# ===========================================================================


def test_invalid_namespace_raises(session) -> None:
    """非法 namespace 必须报错；events 是唯一被本层整编的命名空间。"""
    _seed_watch(session, "BV1")
    assert EVENTS_NAMESPACE in DEMAND_NAMESPACES
    with pytest.raises(ValueError):
        WatchService().reconcile_demands(session, namespace="other", desired={}, now_s=E)


def test_manual_ranking_unchanged_across_two_rounds(session) -> None:
    """两轮 reconcile 之间，manual / ranking 子快照一字不变。"""
    _seed_watch(session, "BV1")
    WatchService().reconcile_demands(
        session, namespace="manual", desired={"BV1": {"pinned": True}}, now_s=E
    )
    WatchService().reconcile_demands(
        session, namespace="ranking", desired={"BV1": {"rank": 1}}, now_s=E
    )
    session.commit()
    before = _row(session, "BV1")["demands"]
    manual_before = json.dumps(before["manual"], sort_keys=True)
    ranking_before = json.dumps(before["ranking"], sort_keys=True)

    # 第一轮：e1 引用 BV1；第二轮：e1 归档（events 撤销）。
    _seed_event(session, "e1", ["BV1"])
    session.commit()
    reconciler = _make_reconciler()
    reconciler.reconcile(session, now_s=E + 10)
    session.commit()
    assert set(_events(session, "BV1")) == {"e1"}

    _set_event_status(session, "e1", "archived")
    reconciler.reconcile(session, now_s=E + 20)
    session.commit()

    after = _row(session, "BV1")["demands"]
    assert json.dumps(after["manual"], sort_keys=True) == manual_before
    assert json.dumps(after["ranking"], sort_keys=True) == ranking_before
