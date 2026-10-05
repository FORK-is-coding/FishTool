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
from modules.hotspot.risk_control import (
    AdmissionResult,
    BudgetDecision,
    LogicalAdmission,
    RequestBudget,
)
from modules.hotspot.watch_service import (
    BUDGET_CATEGORY_FAST,
    BUDGET_CATEGORY_NORMAL,
    TickResult,
    WATCH_SOURCE,
    WatchService,
)
from modules.hotspot.watch_store import commit_state, find_due_for_eval, upsert_watch

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

    async def collect_admitted(self, bvid, *, admission, collection_tid=None, source=WATCH_SOURCE):
        """带单请求准用凭证的采集替身：记录并转调 collect（单扣由真 collector 负责）。"""
        return await self.collect(bvid, collection_tid=collection_tid, source=source)


class FakeBudget:
    """预算替身：指定类别一律拒绝（带 retry_at），其余准许；记录每次 try_acquire。"""

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
        """替身兑换：记录即可（真兑换只发生在真 RequestBudget + 真 collector 上）。"""
        self.redeem_calls.append((admission, operation_key, now_mono))

    def release_unused(self, admission):
        """替身释放：总是成功。"""
        return True


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


# ===========================================================================
# W2 · 类别窗口与公平查询（全部使用**真实** RequestBudget，不用 FakeBudget 冒充类别隔离）
#
# 覆盖 07 执行案 §9.1 / §9.2 / §9.4：
# - limit*8 反例：8 条被拒 fast 后第 9 条 normal 仍入选（两类规模均 > limit*8）；
# - 两类都可用、连续两轮 limit=1：两类轮流拿到执行机会，同类内部 (next_due, bvid) 稳定；
# - 两类各有 limit 候选：合计 admitted 仍 <= limit，不因两条查询让单轮处理量翻倍；
# - 全部 budget 拒绝：无采集、failure_count 不增长、等待可用停止信号取消。
# ===========================================================================

#: W2 统一单调时钟读数（只喂 RequestBudget，绝不落库）。
W2_MONO: float = 2000.0


def _budget_partitioned(
    *,
    normal: tuple = (10, 150, 1500),
    fast: tuple = (8, 120, 1200),
    total: tuple = (40, 400, 4000),
) -> RequestBudget:
    """构造**分桶真预算**：normal / fast 各自 ``(per_minute, per_hour, per_day)``，全局窗给足。

    Args:
        normal: normal_watch 的 (per_minute, per_hour, per_day)。
        fast: fast_watch 的 (per_minute, per_hour, per_day)。
        total: 全局窗 (per_minute, per_hour, per_day)。

    Returns:
        RequestBudget: 固定时钟的真预算器（类别窗真正生效）。
    """
    def _spec(triple: tuple) -> dict:
        pm, ph, pd = triple
        return {"per_minute": pm, "per_hour": ph, "per_day": pd}

    return RequestBudget(
        per_minute=total[0],
        per_hour=total[1],
        per_day=total[2],
        category_limits={
            BUDGET_CATEGORY_NORMAL: _spec(normal),
            BUDGET_CATEGORY_FAST: _spec(fast),
        },
        clock=lambda: W2_MONO,
    )


def _failure_count(db, bvid: str) -> int:
    """读某行的 ``failure_count``（断言容量延期不算平台失败）。"""
    session = db()
    try:
        return int(
            session.execute(
                text("SELECT failure_count FROM hotspot_watch WHERE bvid = :b"),
                {"b": bvid},
            ).scalar()
        )
    finally:
        session.close()


def test_w2_counterexample_limit8_misses_normal_but_category_query_finds_it(db):
    """§9.1 核心反例：更早到期的 fast 占满扫描窗时，normal 被错过；按类查询后仍能选中。

    - 旧选择器只读 ``limit * 8`` 条（本例 8）：最早的 8 条全是 fast，normal 完全在窗外；
    - 新选择器**按类分别取候选**（每类读取上限 = limit），normal 不会被 fast 遮住。
    两类规模均 > ``limit * 8``（各 12 条），不靠提高扫描倍数「碰巧通过」。
    """
    limit = 1
    for index in range(12):
        _seed(db, f"BVFAST{index:05d}", next_due=E - 100 + index, fast_until=E + HOUR)
    for index in range(12):
        _seed(db, f"BVNORM{index:05d}", next_due=E - 50 + index)
    normal_head = "BVNORM00000"

    # ---- 旧口径证据：`limit*8` 窗口被更早的 fast 占满，normal 压根不在窗口内 ----
    session = db()
    try:
        old_window = find_due_for_eval(session, E, limit=max(limit, limit * 8))
        old_bvids = [str(row.bvid) for row in old_window]
    finally:
        session.close()
    assert len(old_bvids) == 8, "旧选择器只读 limit*8 = 8 条"
    assert all(bvid.startswith("BVFAST") for bvid in old_bvids), "旧窗口 8 条全是 fast"
    assert normal_head not in old_bvids, "旧口径下 normal 落在扫描窗外，根本看不见"

    # ---- 新口径：fast 类额度已满被跳过，normal 仍被按类查询选中 ----
    budget = _budget_partitioned(fast=(1, 120, 1200), normal=(10, 150, 1500))
    seeded = budget.reserve(BUDGET_CATEGORY_FAST, W2_MONO, operation_key="SEEDFAST")
    assert seeded.decision.granted is True
    budget.redeem(seeded.admission, operation_key="SEEDFAST", now_mono=W2_MONO)

    service = WatchService(
        session_factory=db, now_fn=lambda: E, now_mono_fn=lambda: W2_MONO, budget=budget
    )
    session = db()
    try:
        selection = service._select_targets(
            session, E, limit, budget=budget, now_mono=W2_MONO
        )
    finally:
        session.close()

    assert [target.bvid for target in selection.targets] == [normal_head]
    assert selection.budget_skipped == 1
    assert selection.budget_exhausted is False


def test_w2_rotation_alternates_categories_across_rounds(db):
    """§9.2：两类都有候选 + limit=1，连续两轮两类轮流拿到执行机会；同类内部 FIFO 稳定。"""
    for index in range(3):
        _seed(db, f"BVFAST{index:05d}", next_due=E - 100 + index, fast_until=E + HOUR)
        _seed(db, f"BVNORM{index:05d}", next_due=E - 50 + index)

    budget = _budget_partitioned()
    collector = RecordingCollector()
    service = WatchService(
        collector_port=collector,
        session_factory=db,
        now_fn=lambda: E,
        now_mono_fn=lambda: W2_MONO,
        budget=budget,
    )

    first = asyncio.run(service.run_tick(limit=1))
    second = asyncio.run(service.run_tick(limit=1))

    # 起始类别 = fast（轮转环首位）；授予后游标轮转到 normal —— 两类轮流，不是永远同一类先。
    assert collector.calls == ["BVFAST00000", "BVNORM00000"]
    assert first.admitted == 1 and second.admitted == 1
    assert first.due_count == 1 and second.due_count == 1
    # 同类内部稳定：剩余 fast 候选仍按 (next_due, bvid) 升序，等待下一轮轮到 fast。
    assert first.candidates_considered == 2  # 两类各读 limit=1 条
    assert first.committed == 1 and second.committed == 1


def test_w2_total_admitted_never_exceeds_limit(db):
    """§9.2：两类各有 limit 个候选，合计 admitted 仍 <= limit，不因两条查询翻倍。"""
    for index in range(3):
        _seed(db, f"BVFAST{index:05d}", next_due=E - 100 + index, fast_until=E + HOUR)
        _seed(db, f"BVNORM{index:05d}", next_due=E - 50 + index)

    budget = _budget_partitioned()
    collector = RecordingCollector()
    service = WatchService(
        collector_port=collector,
        session_factory=db,
        now_fn=lambda: E,
        now_mono_fn=lambda: W2_MONO,
        budget=budget,
    )

    result = asyncio.run(service.run_tick(limit=2))

    assert result.candidates_considered == 4, "两类各读 limit=2 条候选（读取上限，不是处理上限）"
    assert result.due_count <= 2
    assert result.admitted == 2, "本轮 admitted 恒 <= limit"
    assert len(collector.calls) == 2
    assert len(collector.calls) == len(set(collector.calls)), "同一目标不得被处理两次"


def test_w2_all_denied_no_collect_no_failure_and_wait_cancellable(db):
    """§9.4：全部预算拒绝 → 无采集、failure_count 不增长、等待可被停止信号取消。"""
    _seed(db, "BVFAST0001", next_due=E - 100, fast_until=E + HOUR)
    _seed(db, "BVNORMAL001", next_due=E - 50)

    # 真分桶预算：两类各自 per_minute=1，先把两类都占满 -> 两类 peek 均被拒。
    budget = _budget_partitioned(fast=(1, 120, 1200), normal=(1, 150, 1500))
    for kind, key in ((BUDGET_CATEGORY_FAST, "SEEDF"), (BUDGET_CATEGORY_NORMAL, "SEEDN")):
        granted = budget.reserve(kind, W2_MONO, operation_key=key)
        assert granted.decision.granted is True
        budget.redeem(granted.admission, operation_key=key, now_mono=W2_MONO)

    collector = RecordingCollector()
    service = WatchService(
        collector_port=collector,
        session_factory=db,
        now_fn=lambda: E,
        now_mono_fn=lambda: W2_MONO,
        budget=budget,
    )

    result = asyncio.run(service.run_tick(limit=5))

    assert collector.calls == [], "全部被拒时不得发起任何采集"
    assert result.admitted == 0
    assert result.budget_deferred == 0, "选择阶段就没选出可运行目标，故无逐目标延期"
    assert result.budget_skipped == 2, "两类各被跳过 1 次"
    assert result.budget_exhausted is True
    assert result.failed == 0
    assert _failure_count(db, "BVNORMAL001") == 0, "容量延期不是平台失败，不增 failure_count"
    # 等待口径：预算耗尽 -> 等最早 retry（>0），不是忙转。
    assert result.retry_delay_s is not None and result.retry_delay_s > 0
    assert WatchService._compute_wait_s(interval_s=60, result=result) == pytest.approx(
        result.retry_delay_s
    )

    # 等待可取消：停止信号已置位时 _loop 立即返回（不真 sleep）。
    stop_event = asyncio.Event()
    stop_event.set()
    asyncio.run(service._loop(interval_s=60, stop_event=stop_event, limit=5))
    assert collector.calls == []


class _DenyFirstReserveBudget:
    """包一层真 ``RequestBudget``：``peek`` 透传，第一次 ``reserve`` 拒绝、之后放行。

    模拟「peek 与 reserve 之间状态变化」——用于验证有界补选，**不是**拿替身冒充分桶隔离。
    """

    def __init__(self, inner: RequestBudget) -> None:
        """绑定内层真预算。"""
        self._inner = inner
        self.denied_once = False

    def peek(self, kind: str, now_mono: float):
        """透传到内层真预算（类别窗与总窗仍由真实现裁决）。"""
        return self._inner.peek(kind, now_mono)

    def reserve(self, kind: str, now_mono: float, *, operation_key: str):
        """第一次调用返回拒绝，之后透传到内层真预算。"""
        if not self.denied_once:
            self.denied_once = True
            return AdmissionResult(
                decision=BudgetDecision(
                    granted=False, retry_at_mono=now_mono + 10.0, reason_code="rate_limited"
                )
            )
        return self._inner.reserve(kind, now_mono, operation_key=operation_key)

    def release_unused(self, admission) -> bool:
        """透传到内层真预算。"""
        return self._inner.release_unused(admission)

    def clock(self) -> float:
        """与内层真预算同源时钟。"""
        return self._inner.clock()


def test_w2_reserve_denied_backfills_from_bounded_pool(db):
    """§9.2：``reserve`` 因状态变化被拒时，从**有界补选池**补选；本轮 admitted 仍 <= limit。"""
    for index in range(3):
        _seed(db, f"BVFAST{index:05d}", next_due=E - 100 + index, fast_until=E + HOUR)
        _seed(db, f"BVNORM{index:05d}", next_due=E - 50 + index)

    budget = _DenyFirstReserveBudget(_budget_partitioned())
    collector = RecordingCollector()
    service = WatchService(
        collector_port=collector,
        session_factory=db,
        now_fn=lambda: E,
        now_mono_fn=lambda: W2_MONO,
        budget=budget,
    )

    result = asyncio.run(service.run_tick(limit=2))

    assert result.budget_deferred == 1, "第 1 个候选 reserve 被拒 -> 计一次预算延期"
    assert result.admitted == 2, "补选后本轮 admitted 仍 <= limit=2"
    assert len(collector.calls) == 2
    assert len(collector.calls) == len(set(collector.calls)), "补选不得重复处理同一目标"
    assert result.candidates_considered == 4


def test_v2_scheduling_golden_output_stable(db):
    """W3 回归（§13-W3）：v2 调度（按类轮转 + 有界补选）在固定输入下输出确定不变（金标准）。

    覆盖 07 执行案 W3 的「v2 算法回归不变」：真实分桶预算 + 真按类查询，起始类别 ``fast``，
    首个必为更早到期的 fast 目标；授予后游标轮转到 normal。锁定 ``admitted`` / 候选数 /
    调用顺序，防止后续接线改动悄悄改了调度输出。
    """
    for index in range(3):
        _seed(db, f"BVFAST{index:05d}", next_due=E - 100 + index, fast_until=E + HOUR)
        _seed(db, f"BVNORM{index:05d}", next_due=E - 50 + index)

    budget = _budget_partitioned()
    collector = RecordingCollector()
    service = WatchService(
        collector_port=collector,
        session_factory=db,
        now_fn=lambda: E,
        now_mono_fn=lambda: W2_MONO,
        budget=budget,
    )

    result = asyncio.run(service.run_tick(limit=2))

    assert result.admitted == 2
    assert collector.calls == ["BVFAST00000", "BVNORM00000"]
    assert result.candidates_considered == 4
    assert result.committed == 2
    assert result.failed == 0
