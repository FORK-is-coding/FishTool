"""第二批 C / F · 防迟到提交与预算分类不互相饿死。

被测：``modules/hotspot/watch_service.py``（``_process_target`` 写入围栏 / ``_select_targets`` /
``_compute_wait_s`` / ``_loop``）+ ``modules/hotspot/watch_store.commit_state(require_active=True)``。

覆盖点（逐条对齐派单 C / F）：
- C：worker 完成写入前再检查 lease 与 active；构造「需求撤销 + 迟到写入」场景，断言迟到结果
  **被拦下**（不是写进去之后再判）——行上评估列一个都没落，``status='dropped'``；
- C：``commit_state(require_active=True)`` 对已释放行原子拒绝；
- F：``_loop`` 只从当前可授予预算的类别选 due 项；fast 配额用尽**立即跳过**，不挡住 normal；
  无类别可运行才等最早 ``retry_at``（用假时钟断言，不真 sleep）。

测试用临时文件 SQLite（独立连接池，可真实模拟「采集期间被别的会话撤销」），不触网、不读密钥。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import DatabaseManager, Video, VideoStats
from modules.hotspot.risk_control import BudgetDecision
from modules.hotspot.watch_service import (
    BUDGET_CATEGORY_FAST,
    BUDGET_CATEGORY_NORMAL,
    TickResult,
    WATCH_SOURCE,
    WatchService,
)
from modules.hotspot.watch_store import commit_state, upsert_watch

#: 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int）。
E: int = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
HOUR: int = 3600
MONO: float = 1000.0


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：经 ``DatabaseManager`` 建表 + 04 幂等迁移补列，独立连接池。"""
    path = tmp_path / "batch2_fence.db"
    mgr = DatabaseManager(str(path))
    mgr.engine.dispose()
    engine = create_engine(f"sqlite:///{path}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


class RecordingCollector:
    """采集端口替身：记录 bvid，可在采集中触发回调（模拟并发撤销 / 停循环），绝不触网。"""

    def __init__(self, *, on_collect=None) -> None:
        self.calls: list = []
        self._on_collect = on_collect

    async def collect(self, bvid, *, collection_tid=None, source=WATCH_SOURCE):
        self.calls.append(bvid)
        if self._on_collect is not None:
            self._on_collect(bvid)
        return 1


class FakeBudget:
    """预算替身：指定类别一律拒绝（带 retry_at），其余准许；记录每次 try_acquire。"""

    def __init__(self, *, denied=(), retry_after: float = 30.0) -> None:
        self.denied = set(denied)
        self.retry_after = retry_after
        self.calls: list = []

    def try_acquire(self, kind, now_mono):
        self.calls.append((kind, now_mono))
        if kind in self.denied:
            return BudgetDecision(
                granted=False, retry_at_mono=now_mono + self.retry_after, reason_code="rate_limited"
            )
        return BudgetDecision(granted=True)


def _seed(db, bvid, *, next_due, interval=HOUR, ttl=HOUR, fast_until=None):
    """入库一行；可选设置 ``fast_until_s``（04 迁移列，ORM 未声明，走原生 SQL）。"""
    session = db()
    try:
        upsert_watch(
            session,
            bvid=bvid,
            now_epoch_s=E,
            ttl_end_epoch_s=E + ttl,
            next_due_epoch_s=next_due,
            sample_interval_s=interval,
        )
        session.commit()
        if fast_until is not None:
            session.execute(
                text("UPDATE hotspot_watch SET fast_until_s = :f WHERE bvid = :b"),
                {"f": fast_until, "b": bvid},
            )
            session.commit()
    finally:
        session.close()


def _seed_snapshots(db, bvid, points):
    """写入 ``videos`` / ``video_stats``（质量 ok），供算法评估。"""
    session = db()
    try:
        video = Video(bvid=bvid, aid=1, tid=1008, title="t", mid=1, author="a")
        session.add(video)
        session.flush()
        for epoch_s, view in points:
            session.add(
                VideoStats(
                    video_id=video.id,
                    view=view,
                    snapshot_time=datetime.fromtimestamp(epoch_s),
                    source=WATCH_SOURCE,
                    captured_epoch_s=epoch_s,
                    collection_tid=1008,
                    raw_tid=1008,
                    view_status="ok",
                    stat_status="ok",
                    metric_status={"view": "ok"},
                )
            )
        session.commit()
    finally:
        session.close()


def _row(db, bvid):
    """读回一行的关键列（dict）。"""
    session = db()
    try:
        row = session.execute(
            text(
                "SELECT active, next_due_epoch_s, state_json, last_evaluation_epoch_s, "
                "coverage_state, state_revision FROM hotspot_watch WHERE bvid = :b"
            ),
            {"b": bvid},
        ).first()
        if row is None:
            return None
        return {
            "active": bool(row[0]),
            "next_due_epoch_s": row[1],
            "state_json": row[2],
            "last_evaluation_epoch_s": row[3],
            "coverage_state": row[4],
            "state_revision": row[5],
        }
    finally:
        session.close()


# --------------------------------------------------------------------------- C · 防迟到提交


def test_commit_state_requires_active_for_late_writes(db):
    """``require_active=True``：已释放行原子拒绝；行仍在池则照常提交。"""
    session = db()
    try:
        upsert_watch(session, bvid="BV1FENCEACT", now_epoch_s=E, next_due_epoch_s=E - 1)
        session.commit()
        # 在池：提交成功
        assert commit_state(session, "BV1FENCEACT", claim_revision=0, require_active=True) is True
        session.commit()
        # 释放（模拟需求撤销 / manual_stop）
        session.execute(text("UPDATE hotspot_watch SET active = 0 WHERE bvid = 'BV1FENCEACT'"))
        session.commit()
        # 已释放：迟到写入被原子拒绝，不落一列
        assert commit_state(session, "BV1FENCEACT", claim_revision=1, require_active=True) is False
        session.commit()
        assert _row(db, "BV1FENCEACT")["state_json"] is None
    finally:
        session.close()


def test_late_write_after_demand_revoked_is_fenced(db):
    """需求撤销 + 迟到写入：结果被拦下，不是写进去之后再判。"""
    bvid = "BV1LATE0001"
    _seed(db, bvid, next_due=E - 1)
    _seed_snapshots(db, bvid, [(E - 86400, 100), (E, 900)])

    svc = WatchService(now_fn=lambda: E)
    session = db()
    try:
        # 挂一个仅 events 的需求，使其可被调度；记下当时的代际。
        svc.reconcile_demands(session, namespace="events", desired={"e1": {"bvid": bvid}}, now_s=E)
        # 让它本轮确实到点（reconcile 会把 next_due 排到未来，这里压回到点态）。
        session.execute(
            text("UPDATE hotspot_watch SET next_due_epoch_s = :nd WHERE bvid = :b"),
            {"nd": E - 1, "b": bvid},
        )
        session.commit()
    finally:
        session.close()
    revision_before = _row(db, bvid)["state_revision"]

    def revoke_during_collect(_bvid):
        """采集期间由「别的会话」撤销需求（真实独立连接 + commit）。"""
        other = db()
        try:
            WatchService().reconcile_demands(other, namespace="events", desired={}, now_s=E + 1)
            other.commit()
        finally:
            other.close()

    collector = RecordingCollector(on_collect=revoke_during_collect)
    service = WatchService(collector_port=collector, session_factory=db, now_fn=lambda: E)

    result = asyncio.run(service.run_tick())

    # 本轮被丢弃（不是失败），且**没有落任何评估列**（证明是「写之前」判掉的）。
    assert collector.calls == [bvid]
    assert result.committed == 0
    assert result.dropped == 1
    assert result.failed == 0

    after = _row(db, bvid)
    assert after["active"] is False  # 已被撤销释放
    assert after["state_revision"] == revision_before + 1  # 撤销推进了代际
    assert after["state_json"] is None  # 迟到结果一个评估列都没落
    assert after["last_evaluation_epoch_s"] is None
    assert after["coverage_state"] is None
    assert after["next_due_epoch_s"] == E - 1  # 调度列也没动


def test_write_fence_rejects_bumped_revision(db):
    """写入围栏：代际被别的写入者推进后返回 False。"""
    session = db()
    try:
        upsert_watch(session, bvid="BV1FENCEGEN", now_epoch_s=E, next_due_epoch_s=E - 1)
        session.commit()
        claim = 0
        assert WatchService._write_fence_ok(session, "BV1FENCEGEN", claim) is True
        session.execute(
            text("UPDATE hotspot_watch SET state_revision = 1 WHERE bvid = 'BV1FENCEGEN'")
        )
        session.commit()
        assert WatchService._write_fence_ok(session, "BV1FENCEGEN", claim) is False
    finally:
        session.close()


# --------------------------------------------------------------------------- F · 预算不互相饿死


def test_fast_exhausted_does_not_block_normal(db):
    """fast 配额用尽立即跳过，normal 照常选出（用假时钟，不 sleep）。"""
    _seed(db, "BV1FAST0001", next_due=E - 100, fast_until=E + HOUR)  # 更早到期，但 fast 配额尽
    _seed(db, "BV1NORM0001", next_due=E - 50)
    budget = FakeBudget(denied=(BUDGET_CATEGORY_FAST,), retry_after=30.0)
    collector = RecordingCollector()
    service = WatchService(
        collector_port=collector,
        session_factory=db,
        now_fn=lambda: E,
        now_mono_fn=lambda: MONO,
        budget=budget,
    )

    result = asyncio.run(service.run_tick(limit=10))

    # fast 被跳过、normal 正常采集
    assert collector.calls == ["BV1NORM0001"]
    assert result.committed == 1
    assert result.budget_skipped == 1
    assert result.budget_exhausted is False
    # 先试 fast（更早到期）再选 normal —— 证明是「跳过」而不是被挡死
    assert [kind for kind, _ in budget.calls] == [BUDGET_CATEGORY_FAST, BUDGET_CATEGORY_NORMAL]
    assert _row(db, "BV1FAST0001")["next_due_epoch_s"] == E - 100  # fast 行未被触碰
    assert _row(db, "BV1NORM0001")["next_due_epoch_s"] == E + HOUR  # normal 行已推进


def test_no_runnable_category_waits_earliest_retry(db):
    """无类别可运行：上报最早 retry 等待时长，并标记预算耗尽。"""
    _seed(db, "BV1ALLDEN01", next_due=E - 100, fast_until=E + HOUR)
    _seed(db, "BV1ALLDEN02", next_due=E - 50)
    budget = FakeBudget(denied=(BUDGET_CATEGORY_FAST, BUDGET_CATEGORY_NORMAL), retry_after=30.0)
    collector = RecordingCollector()
    service = WatchService(
        collector_port=collector,
        session_factory=db,
        now_fn=lambda: E,
        now_mono_fn=lambda: MONO,
        budget=budget,
    )

    result = asyncio.run(service.run_tick(limit=10))

    assert collector.calls == []
    assert result.due_count == 0
    assert result.budget_skipped == 2
    assert result.budget_exhausted is True
    assert result.retry_delay_s == pytest.approx(30.0)
    # 纯函数口径：预算耗尽 -> 等最早 retry；否则 -> 等正常间隔
    assert WatchService._compute_wait_s(interval_s=60, result=result) == pytest.approx(30.0)
    assert WatchService._compute_wait_s(
        interval_s=60, result=TickResult(budget_exhausted=False)
    ) == pytest.approx(60.0)


def test_loop_skips_fast_and_processes_normal_without_sleep(db):
    """``_loop``：fast 配额尽时不挡住 normal，跑完一轮即按停止信号退出（不真 sleep）。"""
    _seed(db, "BV1LOOPFST1", next_due=E - 100, fast_until=E + HOUR)
    _seed(db, "BV1LOOPNRM1", next_due=E - 50)
    budget = FakeBudget(denied=(BUDGET_CATEGORY_FAST,))
    stop_event = asyncio.Event()
    collector = RecordingCollector(on_collect=lambda _b: stop_event.set())
    service = WatchService(
        collector_port=collector,
        session_factory=db,
        now_fn=lambda: E,
        now_mono_fn=lambda: MONO,
        budget=budget,
    )

    asyncio.run(service._loop(interval_s=1, stop_event=stop_event, limit=10))

    assert collector.calls == ["BV1LOOPNRM1"]
    assert _row(db, "BV1LOOPNRM1")["next_due_epoch_s"] == E + HOUR
    assert _row(db, "BV1LOOPFST1")["next_due_epoch_s"] == E - 100


def test_no_budget_keeps_legacy_tick_behavior(db):
    """不注入预算时行为不变：所有到点目标都处理（向后兼容）。"""
    _seed(db, "BV1NOBUD001", next_due=E - 100, fast_until=E + HOUR)
    _seed(db, "BV1NOBUD002", next_due=E - 50)
    collector = RecordingCollector()
    service = WatchService(collector_port=collector, session_factory=db, now_fn=lambda: E)

    result = asyncio.run(service.run_tick(limit=10))

    assert set(collector.calls) == {"BV1NOBUD001", "BV1NOBUD002"}
    assert result.committed == 2
    assert result.budget_skipped == 0
    assert result.budget_exhausted is False
    assert result.due_count == 2
