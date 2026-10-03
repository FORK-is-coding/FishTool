"""第二批 A · watch 池有界准入（FishTool 04 · 第二批「少量重点追踪」）。

被测：
- ``modules/hotspot/watch_queue.py``（``WatchPool`` / 容量常量）；
- ``modules/hotspot/watch_service.py::WatchService.admit`` / ``drain_admissions``。

覆盖点（逐条对齐派单 A / 02 §8 L594 / 04 §6.3 L623）：
- 普通 watch active 上限 60：前 60 个 ``admitted``，第 61 个 ``queued_capacity`` 且**不落库**；
- 队列本身有界：超 ``max_pending`` 显式 ``queue_full``（不静默丢弃）；超 deadline 的排队项被淘汰；
- 同 bvid 重复 POST 幂等：已在池 -> ``existing`` 且**不重置连续窗状态**（state_json / next_due /
  代际一字不动）；已在队 -> 仍是 ``queued_capacity`` 且不重复计数；
- 有空位时 ``drain`` 把排队项放行；
- ``manual_stop`` 行不得被 ``admit`` 自动重开；
- 不新增 ``manual_block`` 之类的新列。

测试用临时文件 SQLite（经 ``DatabaseManager`` 建表 + 04 幂等迁移补列），不触网、不读密钥。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

from core.database import DatabaseManager
from modules.hotspot.watch_queue import (
    DEFAULT_ACTIVE_WATCH_CAPACITY,
    STATUS_ADMITTED,
    STATUS_BLOCKED_BY_USER,
    STATUS_EXISTING,
    STATUS_QUEUED_CAPACITY,
    STATUS_QUEUE_FULL,
    WatchPool,
)
from modules.hotspot.watch_service import WatchService
from modules.hotspot.watch_store import upsert_watch

#: 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int）。
E: int = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：经 ``DatabaseManager`` 建表并跑 04 幂等迁移补 ``source_demands`` 等列。

    Yields:
        sessionmaker: 绑定临时库、使用独立连接池的会话工厂。
    """
    path = tmp_path / "batch2_pool.db"
    mgr = DatabaseManager(str(path))
    mgr.engine.dispose()
    engine = create_engine(f"sqlite:///{path}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _columns(db) -> set:
    """读 ``hotspot_watch`` 的列名集合。"""
    session = db()
    try:
        rows = session.execute(text('PRAGMA table_info("hotspot_watch")')).all()
        return {str(row[1]) for row in rows}
    finally:
        session.close()


def _count_active(db) -> int:
    """统计 active=1 的行数。"""
    session = db()
    try:
        return int(session.execute(text("SELECT COUNT(*) FROM hotspot_watch WHERE active = 1")).scalar() or 0)
    finally:
        session.close()


def _row(db, bvid):
    """读回一行的关键列（dict）。"""
    session = db()
    try:
        row = session.execute(
            text(
                "SELECT active, stop_reason, next_due_epoch_s, state_json, state_revision, sample_interval_s "
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
            "state_json": row[3],
            "state_revision": row[4],
            "sample_interval_s": row[5],
        }
    finally:
        session.close()


# --------------------------------------------------------------------------- 活跃上限 60


def test_active_capacity_60_then_61st_queued_capacity(db):
    """前 60 个正常入池，第 61 个得 ``queued_capacity``，且不假装已入队（不落库）。"""
    session = db()
    try:
        svc = WatchService()
        statuses = []
        for index in range(DEFAULT_ACTIVE_WATCH_CAPACITY):
            statuses.append(svc.admit(session, bvid=f"BV1CAP{index:04d}", now_s=E).status)
        session.commit()

        assert statuses == [STATUS_ADMITTED] * DEFAULT_ACTIVE_WATCH_CAPACITY
        assert _count_active(db) == DEFAULT_ACTIVE_WATCH_CAPACITY

        queued = svc.admit(session, bvid="BV1CAPOVER0", now_s=E)
        session.commit()
        assert queued.status == STATUS_QUEUED_CAPACITY
        assert queued.active_count == DEFAULT_ACTIVE_WATCH_CAPACITY
        assert queued.pending_count == 1

        # 不许假装已入队：排队项没有任何 watch 行；active 数也不变。
        assert _row(db, "BV1CAPOVER0") is None
        assert _count_active(db) == DEFAULT_ACTIVE_WATCH_CAPACITY
        assert svc.pool.is_pending("BV1CAPOVER0") is True
    finally:
        session.close()


def test_repeat_post_existing_is_idempotent_and_keeps_window_state(db):
    """已在池的 bvid 重复 POST -> ``existing``，且不重置连续窗状态（state_json / next_due / 代际）。"""
    session = db()
    try:
        svc = WatchService()
        svc.admit(session, bvid="BV1IDEM0001", now_s=E, sample_interval_s=3600)
        session.commit()
        # 人为制造「连续窗状态」：写 state_json / 特殊 next_due / 代际推进到 7。
        session.execute(
            text(
                "UPDATE hotspot_watch SET state_json = :sj, next_due_epoch_s = :nd, "
                "state_revision = 7 WHERE bvid = :b"
            ),
            {"sj": '{"stage": "上升期", "count": 3}', "nd": E + 123, "b": "BV1IDEM0001"},
        )
        session.commit()
        before = _row(db, "BV1IDEM0001")

        second = svc.admit(session, bvid="BV1IDEM0001", now_s=E + 5000)
        session.commit()

        assert second.status == STATUS_EXISTING
        after = _row(db, "BV1IDEM0001")
        assert after == before  # 幂等：一行不变，连续窗状态全保留
    finally:
        session.close()


# --------------------------------------------------------------------------- 队列有界


def test_pending_queue_is_bounded_and_enforces_deadline(db):
    """队列超 ``max_pending`` 显式 ``queue_full``；超 deadline 的排队项被 drain 淘汰。"""
    session = db()
    try:
        svc = WatchService(pool=WatchPool(active_capacity=1, max_pending=2, queue_deadline_s=100))
        assert svc.admit(session, bvid="BV1Q0000001", now_s=E).status == STATUS_ADMITTED
        assert svc.admit(session, bvid="BV1Q0000002", now_s=E).status == STATUS_QUEUED_CAPACITY
        assert svc.admit(session, bvid="BV1Q0000003", now_s=E).status == STATUS_QUEUED_CAPACITY
        # 队列本身也有界：第 3 个排队项被显式拒绝，绝不静默丢弃。
        assert svc.admit(session, bvid="BV1Q0000004", now_s=E).status == STATUS_QUEUE_FULL
        session.commit()

        # 同 bvid 重复 POST 幂等：仍在队、不重复计数、不刷新入队时刻。
        again = svc.admit(session, bvid="BV1Q0000002", now_s=E + 1)
        assert again.status == STATUS_QUEUED_CAPACITY
        assert svc.pool.pending_bvids() == ["BV1Q0000002", "BV1Q0000003"]

        # deadline：超时的排队项在 drain 时过期移除。
        drained = svc.drain_admissions(session, now_s=E + 1000)
        assert set(drained["expired"]) == {"BV1Q0000002", "BV1Q0000003"}
        assert svc.pool.pending_bvids() == []
    finally:
        session.close()


def test_drain_admits_pending_when_slot_frees(db):
    """有空位时 ``drain`` 把排队项放进池。"""
    session = db()
    try:
        svc = WatchService(pool=WatchPool(active_capacity=1, max_pending=5, queue_deadline_s=100000))
        svc.admit(session, bvid="BV1DR000001", now_s=E)
        svc.admit(session, bvid="BV1DR000002", now_s=E)
        session.commit()
        assert svc.pool.pending_bvids() == ["BV1DR000002"]

        # 释放占位行，制造一个空位。
        session.execute(text("UPDATE hotspot_watch SET active = 0 WHERE bvid = :b"), {"b": "BV1DR000001"})
        session.commit()

        drained = svc.drain_admissions(session, now_s=E + 10)
        session.commit()

        assert drained["admitted"] == ["BV1DR000002"]
        assert svc.pool.pending_bvids() == []
        assert _count_active(db) == 1  # 放行的是排队那个
    finally:
        session.close()


# --------------------------------------------------------------------------- manual_stop / 不新增列


def test_admit_never_reopens_manual_stop(db):
    """``stop_reason='manual_stop'`` 的行不得被 ``admit`` 自动重开。"""
    session = db()
    try:
        upsert_watch(session, bvid="BV1MS000001", now_epoch_s=E)
        session.commit()
        session.execute(
            text("UPDATE hotspot_watch SET active = 0, stop_reason = 'manual_stop' WHERE bvid = :b"),
            {"b": "BV1MS000001"},
        )
        session.commit()

        result = WatchService().admit(session, bvid="BV1MS000001", now_s=E + 1)
        session.commit()

        assert result.status == STATUS_BLOCKED_BY_USER
        row = _row(db, "BV1MS000001")
        assert row["active"] is False
        assert row["stop_reason"] == "manual_stop"
    finally:
        session.close()


def test_no_new_manual_block_column(db):
    """绝不新增 ``manual_block`` 之类的新列（manual_stop 本身就是持久标记）。"""
    assert "manual_block" not in _columns(db)
