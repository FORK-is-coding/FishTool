"""第二批 B / D / E · 需求驱动节奏重算、启动清理、manual_stop 优先。

被测：``modules/hotspot/watch_service.py``（``reconcile_demands`` / ``recover_on_startup`` /
``demand_eligibility``）+ ``modules/hotspot/watch_demand.py``。

覆盖点（逐条对齐派单 B / D / E）：
- B：仅 events 需求的 watch，最后一个事件撤销后**停采**；有 manual 的**退回其原节奏**
  （events 的 600s 撤销后回到 manual 的 3600s）；撤销后重算 ``next_due_epoch_s``，但**不清**
  02 阶段历史 / 原始快照 / 其他命名空间需求；
- D（E49）：启动清理撤遗留 events 需求并重算节奏；manual/ranking 继续；清孤儿 ``fast_until_s``；
- E：``manual_stop`` 行 reconcile 只能返回 ``blocked_by_user``，**不改 active、不自动重开**；
  不新增 ``manual_block`` 列。

测试用临时文件 SQLite，不触网、不读密钥。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import DatabaseManager
from modules.hotspot.watch_demand import (
    REASON_BLOCKED_BY_USER,
    REASON_RELEASED,
    REASON_TRACKING,
    demand_reason,
    resolve_interval_s,
)
from modules.hotspot.watch_service import STOP_REASON_EVENTS_REVOKED, WatchService
from modules.hotspot.watch_store import upsert_watch

#: 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int）。
E: int = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
HOUR: int = 3600


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：经 ``DatabaseManager`` 建表并跑 04 幂等迁移补列。"""
    path = tmp_path / "batch2_demands.db"
    mgr = DatabaseManager(str(path))
    mgr.engine.dispose()
    engine = create_engine(f"sqlite:///{path}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _seed(db, bvid, *, now=E, interval=HOUR, next_due=None):
    """入库一行（幂等）。"""
    session = db()
    try:
        upsert_watch(
            session,
            bvid=bvid,
            now_epoch_s=now,
            sample_interval_s=interval,
            next_due_epoch_s=next_due if next_due is not None else now + interval,
        )
        session.commit()
    finally:
        session.close()


def _raw(db, bvid):
    """读一行的关键列（dict）。"""
    session = db()
    try:
        row = session.execute(
            text(
                "SELECT active, stop_reason, next_due_epoch_s, sample_interval_s, source_demands, "
                "state_json, last_confirmed_stage, coverage_state, fast_until_s "
                "FROM hotspot_watch WHERE bvid = :b"
            ),
            {"b": bvid},
        ).first()
        if row is None:
            return None
        return {
            "active": bool(row[0]),
            "stop_reason": row[1],
            "next_due_epoch_s": row[2],
            "sample_interval_s": row[3],
            "source_demands": None if row[4] is None else json.loads(row[4]),
            "state_json_raw": row[5],
            "last_confirmed_stage": row[6],
            "coverage_state": row[7],
            "fast_until_s": row[8],
        }
    finally:
        session.close()


def _set_stage_history(db, bvid, *, stage="上升期", state_json='{"stage": "上升期", "count": 2}'):
    """人为写入 02 阶段历史 / 原始评估列，用于断言「撤销不清历史」。"""
    session = db()
    try:
        session.execute(
            text(
                "UPDATE hotspot_watch SET last_confirmed_stage = :stage, state_json = :sj, "
                "coverage_state = 'full_support', last_evaluation_epoch_s = :e WHERE bvid = :b"
            ),
            {"stage": stage, "sj": state_json, "e": E, "b": bvid},
        )
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------------- 纯函数口径


def test_resolve_interval_takes_min_across_namespaces():
    """生效间隔 = 各需求命名空间间隔的最小值；无需求 -> None。"""
    assert resolve_interval_s({}) is None
    assert resolve_interval_s({"manual": {"BV1": {}}}) == 3600
    assert resolve_interval_s({"manual": {"BV1": {}}, "events": {"e1": {"bvid": "BV1", "interval_s": 600}}}) == 600
    # 撤销 events 后自动退回 manual 的 3600
    assert resolve_interval_s({"manual": {"BV1": {}}}) == 3600


def test_demand_reason_priority():
    """派生 reason：manual_stop -> blocked_by_user（最高优先）；释放 -> released。"""
    assert demand_reason(active=False, stop_reason="manual_stop", source_demands={}) == REASON_BLOCKED_BY_USER
    assert demand_reason(active=False, stop_reason="expired", source_demands={}) == REASON_RELEASED
    assert demand_reason(active=True, stop_reason=None, source_demands={"manual": {"BV1": {}}}) == REASON_TRACKING


# --------------------------------------------------------------------------- B · 撤销回退


def test_events_revoked_reverts_to_manual_rhythm(db):
    """有 manual 的 bvid：events 撤掉后退回 manual 原节奏，且不清阶段历史。"""
    bvid = "BV1MIX00001"
    _seed(db, bvid, interval=HOUR)
    _set_stage_history(db, bvid)
    svc = WatchService()
    session = db()
    try:
        # 先挂 manual，再挂更激进的 events（600s）
        svc.reconcile_demands(session, namespace="manual", desired={bvid: {"note": "pin"}}, now_s=E)
        svc.reconcile_demands(
            session,
            namespace="events",
            desired={"e1": {"bvid": bvid, "interval_s": 600}},
            now_s=E + 10,
        )
        session.commit()
        mid = _raw(db, bvid)
        assert mid["sample_interval_s"] == 600
        assert mid["next_due_epoch_s"] == E + 10 + 600

        # 撤销 events（传空快照）
        svc.reconcile_demands(session, namespace="events", desired={}, now_s=E + 20)
        session.commit()

        after = _raw(db, bvid)
        # 退回 manual 的原节奏（3600）
        assert after["sample_interval_s"] == HOUR
        assert after["next_due_epoch_s"] == E + 20 + HOUR
        assert after["active"] is True
        # 其他命名空间需求保留，events 撤销
        assert after["source_demands"] == {"manual": {bvid: {"note": "pin"}}}
        # 02 阶段历史 / 原始快照一字不动
        assert after["state_json_raw"] == mid["state_json_raw"]
        assert after["last_confirmed_stage"] == "上升期"
        assert after["coverage_state"] == "full_support"
    finally:
        session.close()


def test_events_only_target_stops_after_events_revoked(db):
    """仅 events 需求的目标：最后一个事件撤销后停采（active=0，历史全留）。"""
    bvid = "BV1EVT00001"
    _seed(db, bvid, interval=HOUR)
    _set_stage_history(db, bvid)
    svc = WatchService()
    session = db()
    try:
        svc.reconcile_demands(session, namespace="events", desired={"e1": {"bvid": bvid}}, now_s=E)
        session.commit()
        assert _raw(db, bvid)["active"] is True

        svc.reconcile_demands(session, namespace="events", desired={}, now_s=E + 5)
        session.commit()

        after = _raw(db, bvid)
        assert after["active"] is False
        assert after["stop_reason"] == STOP_REASON_EVENTS_REVOKED
        assert after["source_demands"] is None
        # 「保留历史」：阶段 / 覆盖 / 原始快照不清
        assert after["last_confirmed_stage"] == "上升期"
        assert after["coverage_state"] == "full_support"
    finally:
        session.close()


# --------------------------------------------------------------------------- D · 启动清理


def test_startup_cleanup_drops_events_and_keeps_manual(db):
    """E49：重启清遗留 events 需求并重算节奏；manual/ranking 继续；清孤儿快采。"""
    mix, evt, man, stopped = "BV1RC000MIX", "BV1RC000EVT", "BV1RC000MAN", "BV1RC000STP"
    _seed(db, mix, interval=HOUR)
    _seed(db, evt, interval=HOUR)
    _seed(db, man, interval=2 * HOUR, next_due=E + 2 * HOUR)
    _seed(db, stopped, interval=HOUR)

    svc = WatchService()
    session = db()
    try:
        # 模拟重启前的现场：manual 需求 + 两个事件需求 + 一个 manual_stop 行的事件需求。
        svc.reconcile_demands(session, namespace="manual", desired={mix: {}, man: {}}, now_s=E)
        session.execute(
            text("UPDATE hotspot_watch SET active = 0, stop_reason = 'manual_stop' WHERE bvid = :b"),
            {"b": stopped},
        )
        session.commit()
        svc.reconcile_demands(
            session,
            namespace="events",
            desired={
                "e1": {"bvid": mix, "interval_s": 600},
                "e2": {"bvid": evt},
                "e3": {"bvid": stopped},
            },
            now_s=E,
        )
        session.commit()
        session.execute(
            text("UPDATE hotspot_watch SET fast_until_s = :f WHERE bvid IN (:a, :b, :c, :d)"),
            {"f": E + 99999, "a": mix, "b": evt, "c": man, "d": stopped},
        )
        session.commit()
        man_before = _raw(db, man)
        assert _raw(db, evt)["active"] is True  # 重启前 evt 还在采样
        assert _raw(db, stopped)["active"] is False
    finally:
        session.close()

    # ---- 模拟重启：跑一次启动清理 ----
    restart_session = db()
    try:
        recovery = WatchService().recover_on_startup(restart_session, now_s=E + 1000)
        restart_session.commit()
    finally:
        restart_session.close()

    assert recovery["events_cleared"] == 3  # mix / evt / stopped 各有一个 events 需求
    assert recovery["blocked"] == 1  # stopped 是 manual_stop
    assert recovery["released"] == 1  # evt 只剩自己且无需求 -> 停采

    mix_after = _raw(db, mix)
    assert mix_after["source_demands"] == {"manual": {mix: {}}}
    assert mix_after["active"] is True
    assert mix_after["sample_interval_s"] == HOUR
    assert mix_after["next_due_epoch_s"] == E + 1000 + HOUR

    evt_after = _raw(db, evt)
    assert evt_after["source_demands"] is None
    assert evt_after["active"] is False
    assert evt_after["stop_reason"] == STOP_REASON_EVENTS_REVOKED

    man_after = _raw(db, man)
    # manual 无 events -> 不动一行（节奏 / next_due 保持重启前现场）
    assert man_after["source_demands"] == man_before["source_demands"]
    assert man_after["active"] is True
    assert man_after["next_due_epoch_s"] == man_before["next_due_epoch_s"]
    assert man_after["sample_interval_s"] == man_before["sample_interval_s"]

    stopped_after = _raw(db, stopped)
    # manual_stop 行：events 需求被清，但绝不重开
    assert stopped_after["active"] is False
    assert stopped_after["stop_reason"] == "manual_stop"
    assert stopped_after["source_demands"] is None

    # 不留孤儿快采：所有 fast_until_s 置空
    assert all(row["fast_until_s"] is None for row in (_raw(db, b) for b in (mix, evt, man, stopped)))
    assert recovery["fast_cleared"] == 4


# --------------------------------------------------------------------------- E · manual_stop 优先


def test_reconcile_manual_stop_is_blocked_and_not_reopened(db):
    """manual_stop 行 reconcile 只能返回 blocked_by_user、不改 active、不重排。"""
    bvid = "BV1STOP0001"
    _seed(db, bvid, interval=HOUR)
    session = db()
    try:
        session.execute(
            text(
                "UPDATE hotspot_watch SET active = 0, stop_reason = 'manual_stop', "
                "next_due_epoch_s = :nd WHERE bvid = :b"
            ),
            {"nd": E + 777, "b": bvid},
        )
        session.commit()

        svc = WatchService()
        # reconcile 仍会记录需求，但绝不重开 / 不重排
        svc.reconcile_demands(session, namespace="manual", desired={bvid: {"pinned": True}}, now_s=E)
        svc.reconcile_demands(session, namespace="events", desired={"e1": {"bvid": bvid}}, now_s=E)
        session.commit()

        after = _raw(db, bvid)
        assert after["active"] is False
        assert after["stop_reason"] == "manual_stop"
        assert after["next_due_epoch_s"] == E + 777  # 未被重排
        assert svc.demand_eligibility(session, bvid) == REASON_BLOCKED_BY_USER
    finally:
        session.close()


def test_demand_eligibility_variants(db):
    """``demand_eligibility``：manual_stop -> blocked_by_user；有需求 -> tracking；查无行 -> released。"""
    _seed(db, "BV1ELG00001")
    svc = WatchService()
    session = db()
    try:
        assert svc.demand_eligibility(session, "BV1MISSING0") == REASON_RELEASED
        svc.reconcile_demands(session, namespace="manual", desired={"BV1ELG00001": {}}, now_s=E)
        session.commit()
        assert svc.demand_eligibility(session, "BV1ELG00001") == REASON_TRACKING
    finally:
        session.close()
