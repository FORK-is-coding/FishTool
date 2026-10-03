"""FishTool 04 · 第三批 b：发现围栏 D01—D07 验收（真事务 + 真并发）。

依据：
- ``FishTool_04_话题级热点研判与选题决策_02补充执行案(1).md`` §16.2.2 L1435-1459；
- ``FishTool_04_R5执行规格_第三批b_事件归属与发现围栏.md`` §1.2 / §5；
- 原文硬要求（L1457）：使用真实临时 SQLite 事务与并发异步调度，**只 mock 外部 API/LLM**；
  不能 mock 掉 claim/finish/store 后宣称通过。

本文件：
- 外部来源（``fetch_fn``）是**唯一**被替身化的东西（等价于 mock 外部 API）；
- claim / commit / finish / invalidate 全部打到真实临时 SQLite，**未打桩**；
- 覆盖 D01—D07 + §17-4 尾巴（重复 BVID / active 容量 / 暂停合并）。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from core.database import DatabaseManager
from core.database.event_discovery_repository import (
    DiscoverySuperseded,
    EventDiscoveryRepository,
)
from core.database.hot_event_repository import HotEventRepository
from core.database.models_hot_event import EventDiscoveryRun, HotEventMember
from modules.hotspot.event_discovery_fence import (
    DiscoveryBudgetUnavailable,
    DiscoveryCapacityExceeded,
    DiscoveryFetchResult,
    DiscoveryInProgress,
    DiscoveryNotDue,
    EventDiscoveryFence,
)

T = 1788220800


class MutableClock:
    """可推进的秒级时钟（D04/D07 用）。"""

    def __init__(self, value: int) -> None:
        self.value = int(value)

    def __call__(self) -> int:
        return self.value

    def set(self, value: int) -> None:
        self.value = int(value)


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：经 ``DatabaseManager`` 建六表 → 可跨线程引擎产出会话工厂。"""
    path = tmp_path / "event_discovery_fencing.db"
    manager = DatabaseManager(str(path))
    manager.engine.dispose()
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def make_active_event(
    history_repo: HotEventRepository,
    event_id: str = "ev1",
    *,
    rule_version: int = 1,
    policy_hash: str = "h1",
    due_past: bool = True,
) -> None:
    """建一个 active 事件（带冻结规则/策略）。"""
    history_repo.create_hot_event(
        event_id=event_id,
        name=f"事件-{event_id}",
        now_s=T,
        status="active",
        current_rule_version=rule_version,
        source_policy_hash=policy_hash,
        discovery_due_s=(T - 10) if due_past else (T + 100_000),
        revision=0,
    )


def count_members(db, event_id: str | None = None) -> int:
    """统计成员行数（可选按事件过滤）。"""
    session = db()
    try:
        query = session.query(HotEventMember)
        if event_id is not None:
            query = query.filter(HotEventMember.event_id == event_id)
        return int(query.count())
    finally:
        session.close()


def get_run(db, run_id: str):
    """读取发现 run（detach 前的快照字段）。"""
    session = db()
    try:
        run = session.get(EventDiscoveryRun, run_id)
        if run is None:
            return None
        return {
            "id": run.id,
            "status": run.status,
            "error_code": run.error_code,
            "finished_s": run.finished_s,
        }
    finally:
        session.close()


def build_fence(db, *, fetch_fn, clock, **overrides) -> EventDiscoveryFence:
    """构造围栏服务（repo 与 service 共用同一时钟）。"""
    repo = EventDiscoveryRepository(clock=clock)
    params = dict(
        repository=repo,
        session_factory=db,
        fetch_fn=fetch_fn,
        clock=clock,
        lease_seconds=300,
        deadline_seconds=120,
        manual_cooldown_seconds=60,
        due_interval_seconds=7200,
        max_active_events=5,
    )
    params.update(overrides)
    return EventDiscoveryFence(**params)


# ===========================================================================
# D01：同 event 手动和后台同时启动 → 仅一方拿到可执行 run，失败方无外部调用
# ===========================================================================


def test_D01_manual_and_scheduled_only_one_wins_other_no_external_call(db):
    """D01：后台先领跑（占 lease），手动再触发 → 409 discovery_in_progress + run_id，无外部调用。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)

    async def scenario():
        gate = asyncio.Event()
        calls: list[str] = []

        async def fetch(event_id, rule_version, policy_hash):
            calls.append(event_id)
            await gate.wait()
            return DiscoveryFetchResult(counters={"empty_reason": "legitimate_empty"})

        fence = build_fence(db, fetch_fn=fetch, clock=clock)
        first = await fence.start_discovery(
            "ev1", trigger="scheduled", rule_version=1, source_policy_hash="h1"
        )
        assert first.started is True and first.run_id

        # 手动同时启动 → 只能拿到 409/run_id；失败方绝不发起外部调用。
        with pytest.raises(DiscoveryInProgress) as excinfo:
            await fence.start_discovery(
                "ev1", trigger="manual", rule_version=1, source_policy_hash="h1"
            )
        assert excinfo.value.extra.get("run_id") == first.run_id

        gate.set()
        await fence.wait_for_tasks()
        assert calls == ["ev1"], "失败方发起了外部调用"
        return first.run_id

    run_id = asyncio.run(scenario())
    session = db()
    try:
        runs = session.query(EventDiscoveryRun).all()
    finally:
        session.close()
    assert len(runs) == 1 and runs[0].id == run_id and runs[0].status == "completed"


def test_D01_repeated_claim_idempotent(db):
    """反复 claim 幂等：同 event 连续 claim，只有一方拿到可执行 run。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)
    repo = EventDiscoveryRepository(clock=clock)

    session = db()
    try:
        first = repo.claim_discovery(
            session, event_id="ev1", run_id="run_1", lease_token="tok_1",
            trigger="scheduled", now_s=T, rule_version=1, source_policy_hash="h1",
        )
        session.commit()
        second = repo.claim_discovery(
            session, event_id="ev1", run_id="run_2", lease_token="tok_2",
            trigger="scheduled", now_s=T, rule_version=1, source_policy_hash="h1",
        )
        session.rollback()
    finally:
        session.close()
    assert first.claimed is True
    assert second.claimed is False and second.reason == "in_progress"
    assert second.current_run_id == "run_1"
    assert get_run(db, "run_2") is None


# ===========================================================================
# D02：请求中事件暂停/归档 → 先失效 DB token 再取消 task；迟到结果不落业务
# ===========================================================================


def test_D02_pause_invalidates_token_first_then_no_member(db):
    """D02：暂停先清 DB token/run（且旧 run 标 cancelled），迟到结果不能新增成员。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)

    async def scenario():
        gate = asyncio.Event()

        async def fetch(event_id, rule_version, policy_hash):
            await gate.wait()
            return DiscoveryFetchResult(
                member_decisions=[
                    {"bvid": "BV1", "status": "accepted", "first_seen_s": T,
                     "decision_source": "strict_rule", "evidence": {"src": "run"}},
                ]
            )

        fence = build_fence(db, fetch_fn=fetch, clock=clock)
        start = await fence.start_discovery(
            "ev1", trigger="scheduled", rule_version=1, source_policy_hash="h1"
        )
        # 暂停：cancel_tasks=False → 只验 DB 围栏；DB token 必须先失效。
        outcome = await fence.invalidate("ev1", reason="event_paused", cancel_tasks=False)
        assert outcome.cancelled_run_id == start.run_id

        event = history.get_hot_event("ev1")
        assert event.lease_token is None and event.active_discovery_run_id is None
        assert get_run(db, start.run_id)["status"] == "cancelled"

        # 迟到的网络结果现在才返回 → 尝试 commit，必须被围栏挡住。
        gate.set()
        await fence.wait_for_tasks()

    asyncio.run(scenario())
    assert count_members(db, "ev1") == 0
    # 旧 run 仍是被取消终态，未被迟到结果改写成 completed。
    session = db()
    try:
        run = session.query(EventDiscoveryRun).one()
    finally:
        session.close()
    assert run.status == "cancelled"


# ===========================================================================
# D03：请求中规则 / source_policy 改变 → 旧结果不能业务提交；新任务用新版本
# ===========================================================================


def test_D03_rule_change_blocks_old_result_new_task_uses_new_version(db):
    """D03：改规则 + 失效旧 run；旧 rule/hash 结果 commit 失败；新任务用新版本成功。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)
    calls: list[tuple] = []

    async def scenario():
        old_gate = asyncio.Event()

        async def fetch(event_id, rule_version, policy_hash):
            calls.append((rule_version, policy_hash))
            if rule_version == 1:
                await old_gate.wait()
                return DiscoveryFetchResult(
                    member_decisions=[
                        {"bvid": "BV1", "status": "accepted", "first_seen_s": T,
                         "decision_source": "strict_rule"},
                    ]
                )
            return DiscoveryFetchResult(counters={"empty_reason": "legitimate_empty"})

        fence = build_fence(db, fetch_fn=fetch, clock=clock)
        old = await fence.start_discovery(
            "ev1", trigger="scheduled", rule_version=1, source_policy_hash="h1"
        )
        # 改规则：同一事务更新 current_rule_version/source_policy_hash 并失效旧 run。
        outcome = await fence.change_rule(
            "ev1", rule_version=2, source_policy_hash="h2", cancel_tasks=False
        )
        assert outcome.cancelled_run_id == old.run_id
        event = history.get_hot_event("ev1")
        assert event.current_rule_version == 2 and event.source_policy_hash == "h2"

        # 旧版本结果迟到 → 不能业务提交。
        old_gate.set()
        await fence.wait_for_tasks()
        assert count_members(db, "ev1") == 0

        # 新任务用新版本，可正常领取并提交。
        new = await fence.start_discovery(
            "ev1", trigger="scheduled", rule_version=2, source_policy_hash="h2"
        )
        assert new.started is True
        await fence.wait_for_tasks()
        return old.run_id, new.run_id

    old_id, new_id = asyncio.run(scenario())
    assert old_id != new_id
    assert get_run(db, old_id)["status"] == "cancelled"
    assert get_run(db, new_id)["status"] == "completed"


# ===========================================================================
# D04：旧 lease 过期 + 新 worker 已领取 + 旧 worker 返回 → 旧 finish 不能动新 lease
# ===========================================================================


def test_D04_stale_worker_cannot_release_new_lease_or_write_member(db):
    """D04：旧 run 返回 → commit 抛 DiscoverySuperseded；不写成员、不释放新 worker 的 lease。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)
    repo = EventDiscoveryRepository(clock=clock)

    session = db()
    try:
        claim_a = repo.claim_discovery(
            session, event_id="ev1", run_id="runA", lease_token="tokA",
            trigger="scheduled", now_s=T, rule_version=1, source_policy_hash="h1",
        )
        session.commit()
    finally:
        session.close()
    assert claim_a.claimed is True

    # 旧 lease 过期；新 worker 领取。
    clock.set(T + 301)
    session = db()
    try:
        claim_b = repo.claim_discovery(
            session, event_id="ev1", run_id="runB", lease_token="tokB",
            trigger="scheduled", now_s=T + 301, rule_version=1, source_policy_hash="h1",
        )
        session.commit()
    finally:
        session.close()
    assert claim_b.claimed is True and claim_b.current_run_id == "runB"

    # 旧 worker A 返回：先尝试 finish（不得释放新 lease），再尝试 commit（不得写成员）。
    session = db()
    try:
        finish_a = repo.finish_discovery_error(
            session, run_id="runA", lease_token="tokA", event_id="ev1",
            error_code="late_stale_worker", now_s=T + 302,
        )
        session.commit()
    finally:
        session.close()
    assert finish_a.released is False and finish_a.lost is True

    session = db()
    try:
        with pytest.raises(DiscoverySuperseded):
            repo.commit_discovery(
                session, run_id="runA", lease_token="tokA", event_id="ev1",
                rule_version=1, source_policy_hash="h1", now_s=T + 302,
                member_decisions=[
                    {"bvid": "BV1", "status": "accepted", "first_seen_s": T,
                     "decision_source": "strict_rule"},
                ],
            )
        session.rollback()
    finally:
        session.close()

    event = history.get_hot_event("ev1")
    assert event.lease_token == "tokB", "旧 worker 释放了别人的 lease"
    assert event.active_discovery_run_id == "runB"
    assert count_members(db, "ev1") == 0
    assert get_run(db, "runA")["status"] == "interrupted"  # 只保留自身安全诊断


# ===========================================================================
# D05：run 结果写入后成员或 reconcile 故障 → 整事务 rollback
# ===========================================================================


def test_D05_member_write_failure_rolls_back_run(db):
    """D05：成员写入触发 CHECK 失败 → 整事务回滚，绝不出现「完成 run + 缺成员」。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)
    repo = EventDiscoveryRepository(clock=clock)

    session = db()
    try:
        repo.claim_discovery(
            session, event_id="ev1", run_id="runX", lease_token="tokX",
            trigger="scheduled", now_s=T, rule_version=1, source_policy_hash="h1",
        )
        session.commit()
    finally:
        session.close()

    session = db()
    try:
        with pytest.raises(IntegrityError):
            repo.commit_discovery(
                session, run_id="runX", lease_token="tokX", event_id="ev1",
                rule_version=1, source_policy_hash="h1", now_s=T + 5,
                member_decisions=[
                    {"bvid": "BV1", "status": "bogus_status", "first_seen_s": T,
                     "decision_source": "strict_rule"},
                ],
            )
        session.rollback()
    finally:
        session.close()

    assert get_run(db, "runX")["status"] == "running", "run 被错误标为完成终态"
    assert count_members(db, "ev1") == 0
    # lease 未被清（回滚恢复到领取后的状态）。
    assert history.get_hot_event("ev1").active_discovery_run_id == "runX"


def test_D05_reconcile_failure_rolls_back_whole_transaction(db):
    """D05：reconcile 故障 → run 终态 + 成员 + 需求写入全部回滚，无半成品。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)
    repo = EventDiscoveryRepository(clock=clock)

    session = db()
    try:
        repo.claim_discovery(
            session, event_id="ev1", run_id="runY", lease_token="tokY",
            trigger="scheduled", now_s=T, rule_version=1, source_policy_hash="h1",
        )
        session.commit()
    finally:
        session.close()

    def failing_reconcile(inner_session):
        """模拟 3c 的需求对账：先“已启动需求”（写一条成员），再故障。"""
        inner_session.add(
            HotEventMember(
                event_id="ev1", bvid="BV_DEMAND", revision=1, status="accepted",
                first_seen_s=T, decision_at_s=T, rule_version=1,
                decision_source="strict_rule",
            )
        )
        inner_session.flush()
        raise RuntimeError("reconcile_boom")

    session = db()
    try:
        with pytest.raises(RuntimeError):
            repo.commit_discovery(
                session, run_id="runY", lease_token="tokY", event_id="ev1",
                rule_version=1, source_policy_hash="h1", now_s=T + 5,
                member_decisions=[
                    {"bvid": "BV1", "status": "accepted", "first_seen_s": T,
                     "decision_source": "strict_rule"},
                ],
                reconcile=failing_reconcile,
            )
        session.rollback()
    finally:
        session.close()

    assert get_run(db, "runY")["status"] == "running"
    assert count_members(db, "ev1") == 0, "回滚后仍残留成员 / 需求写入"


# ===========================================================================
# D06：请求中用户手动拒绝候选 → finish 读取最新成员 revision，不用旧缓存覆盖拒绝
# ===========================================================================


def test_D06_manual_reject_during_request_is_not_overwritten(db):
    """D06：请求期间人工 rejected → 提交读最新 revision，不复活旧候选。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)
    repo = EventDiscoveryRepository(clock=clock)

    session = db()
    try:
        repo.claim_discovery(
            session, event_id="ev1", run_id="runZ", lease_token="tokZ",
            trigger="scheduled", now_s=T, rule_version=1, source_policy_hash="h1",
        )
        session.commit()
    finally:
        session.close()

    # 请求进行中，用户手动拒绝该候选。
    history.create_member_revision(
        event_id="ev1", bvid="BV1", revision=1, status="rejected",
        first_seen_s=T, rule_version=1, decision_source="manual", now_s=T + 3,
    )

    session = db()
    try:
        outcome = repo.commit_discovery(
            session, run_id="runZ", lease_token="tokZ", event_id="ev1",
            rule_version=1, source_policy_hash="h1", now_s=T + 10,
            member_decisions=[
                {"bvid": "BV1", "status": "accepted", "first_seen_s": T,
                 "decision_source": "strict_rule"},
            ],
        )
        session.commit()
    finally:
        session.close()

    assert outcome.member_revisions == []
    assert outcome.skipped and outcome.skipped[0]["reason"] == "manual_rejected_preserved"
    latest = history.latest_status("ev1", "BV1")
    assert latest.status == "rejected" and latest.revision == 1


# ===========================================================================
# D07：deadline / cooldown / 预算不绕过 / 恢复不拼旧新 run
# ===========================================================================


def test_D07_bounded_deadline_fails_run_without_members(db):
    """D07：硬 deadline 生效 → run 记 deadline_exceeded，释放自身 lease，不写成员。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)

    async def scenario():
        async def slow_fetch(event_id, rule_version, policy_hash):
            await asyncio.sleep(5)
            return DiscoveryFetchResult(
                member_decisions=[
                    {"bvid": "BV1", "status": "accepted", "first_seen_s": T,
                     "decision_source": "strict_rule"},
                ]
            )

        fence = build_fence(
            db, fetch_fn=slow_fetch, clock=clock, lease_seconds=5, deadline_seconds=1
        )
        start = await fence.start_discovery(
            "ev1", trigger="scheduled", rule_version=1, source_policy_hash="h1"
        )
        assert start.started is True
        await fence.wait_for_tasks()
        return start.run_id

    run_id = asyncio.run(scenario())
    run = get_run(db, run_id)
    assert run["status"] == "failed" and run["error_code"] == "deadline_exceeded"
    assert count_members(db, "ev1") == 0
    event = history.get_hot_event("ev1")
    assert event.lease_token is None and event.active_discovery_run_id is None


def test_D07_manual_cooldown_blocks_second_trigger(db):
    """D07：手动触发默认 cooldown=60s 内第二次被拒（不绕过最短触发间隔）。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)
    calls: list[str] = []

    async def scenario():
        async def fetch(event_id, rule_version, policy_hash):
            calls.append(event_id)
            return DiscoveryFetchResult(counters={"empty_reason": "legitimate_empty"})

        fence = build_fence(db, fetch_fn=fetch, clock=clock)
        first = await fence.start_discovery(
            "ev1", trigger="manual", rule_version=1, source_policy_hash="h1"
        )
        assert first.started is True
        await fence.wait_for_tasks()
        with pytest.raises(DiscoveryNotDue):
            await fence.start_discovery(
                "ev1", trigger="manual", rule_version=1, source_policy_hash="h1"
            )

    asyncio.run(scenario())
    assert calls == ["ev1"], "cooldown 内被拒的请求仍发起了外部调用"


def test_D07_budget_not_bypassed(db):
    """D07：预算不可授予 → 429，不创建 run、不发外部调用。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)
    calls: list[str] = []

    async def scenario():
        async def fetch(event_id, rule_version, policy_hash):
            calls.append(event_id)
            return DiscoveryFetchResult()

        fence = build_fence(
            db, fetch_fn=fetch, clock=clock, budget_acquire=lambda now_s: False
        )
        with pytest.raises(DiscoveryBudgetUnavailable):
            await fence.start_discovery(
                "ev1", trigger="scheduled", rule_version=1, source_policy_hash="h1"
            )

    asyncio.run(scenario())
    assert calls == []
    session = db()
    try:
        assert session.query(EventDiscoveryRun).count() == 0
    finally:
        session.close()


def test_D07_recovery_does_not_stitch_old_and_new_run(db):
    """D07：lease 过期后恢复只能建新 run，不把两个时段拼成一轮。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)
    repo = EventDiscoveryRepository(clock=clock)

    session = db()
    try:
        old = repo.claim_discovery(
            session, event_id="ev1", run_id="run_old", lease_token="tok_old",
            trigger="scheduled", now_s=T, rule_version=1, source_policy_hash="h1",
        )
        session.commit()
    finally:
        session.close()
    assert old.claimed is True

    clock.set(T + 400)
    session = db()
    try:
        new = repo.claim_discovery(
            session, event_id="ev1", run_id="run_new", lease_token="tok_new",
            trigger="scheduled", now_s=T + 400, rule_version=1, source_policy_hash="h1",
        )
        session.commit()
    finally:
        session.close()
    assert new.claimed is True
    # 恢复必须建立**新 run ID**（此处显式用 run_old / run_new），不把两时段拼成一轮。
    assert get_run(db, "run_old")["status"] == "interrupted"
    assert get_run(db, "run_new")["status"] == "running"
    # 新 run 的 started_s 只属于新时段（不与旧 run 拼接）。
    session = db()
    try:
        new_run = session.get(EventDiscoveryRun, "run_new")
        old_run = session.get(EventDiscoveryRun, "run_old")
        assert new_run.started_s == T + 400 and old_run.started_s == T
    finally:
        session.close()


# ===========================================================================
# §17-4 尾巴：重复 BVID / active 容量 / 暂停需求合并
# ===========================================================================


def test_repeated_bvid_in_same_event_deduped(db):
    """§17-4：重复 BVID 入同一事件 → 事件内去重，只留一个成员。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history)
    repo = EventDiscoveryRepository(clock=clock)

    session = db()
    try:
        repo.claim_discovery(
            session, event_id="ev1", run_id="runD", lease_token="tokD",
            trigger="scheduled", now_s=T, rule_version=1, source_policy_hash="h1",
        )
        outcome = repo.commit_discovery(
            session, run_id="runD", lease_token="tokD", event_id="ev1",
            rule_version=1, source_policy_hash="h1", now_s=T + 5,
            member_decisions=[
                {"bvid": "BV1", "status": "accepted", "first_seen_s": T, "decision_source": "strict_rule"},
                {"bvid": "BV1", "status": "accepted", "first_seen_s": T, "decision_source": "strict_rule"},
                {"bvid": "BV2", "status": "accepted", "first_seen_s": T, "decision_source": "strict_rule"},
            ],
        )
        session.commit()
    finally:
        session.close()
    assert count_members(db, "ev1") == 2  # BV1 去重后 + BV2
    assert any(s["reason"] == "duplicate_bvid_in_batch" for s in outcome.skipped)


def test_active_capacity_exceeded_no_external_call(db):
    """§17-4：active 事件容量（默认 5）上限后 → 拒绝且不发外部调用。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    for index in range(6):  # 6 个 active > 容量 5
        make_active_event(history, event_id=f"ev{index}")
    calls: list[str] = []

    async def scenario():
        async def fetch(event_id, rule_version, policy_hash):
            calls.append(event_id)
            return DiscoveryFetchResult()

        fence = build_fence(db, fetch_fn=fetch, clock=clock, max_active_events=5)
        with pytest.raises(DiscoveryCapacityExceeded):
            await fence.start_discovery(
                "ev0", trigger="scheduled", rule_version=1, source_policy_hash="h1"
            )

    asyncio.run(scenario())
    assert calls == []
    session = db()
    try:
        assert session.query(EventDiscoveryRun).count() == 0
    finally:
        session.close()


def test_paused_event_demand_merge_isolated(db):
    """§17-4：暂停一个事件只撤本事件需求；另一事件的 run/lease 与成员不受影响。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_active_event(history, event_id="ev1")
    make_active_event(history, event_id="ev2")
    member_written: list[str] = []

    async def scenario():
        gates = {"ev1": asyncio.Event(), "ev2": asyncio.Event()}

        async def fetch(event_id, rule_version, policy_hash):
            await gates[event_id].wait()
            if event_id == "ev2":
                return DiscoveryFetchResult(
                    member_decisions=[
                        {"bvid": "BV9", "status": "accepted", "first_seen_s": T,
                         "decision_source": "strict_rule"},
                    ]
                )
            return DiscoveryFetchResult()

        fence = build_fence(db, fetch_fn=fetch, clock=clock)
        start1 = await fence.start_discovery(
            "ev1", trigger="scheduled", rule_version=1, source_policy_hash="h1"
        )
        start2 = await fence.start_discovery(
            "ev2", trigger="scheduled", rule_version=1, source_policy_hash="h1"
        )
        assert start1.started and start2.started

        # 暂停 ev1：只撤 ev1 的 token/run，ev2 完全不受影响。
        await fence.invalidate("ev1", reason="event_paused", cancel_tasks=False)
        ev1 = history.get_hot_event("ev1")
        ev2 = history.get_hot_event("ev2")
        assert ev1.lease_token is None and ev1.active_discovery_run_id is None
        assert ev2.lease_token == start2.lease_token
        assert ev2.active_discovery_run_id == start2.run_id

        gates["ev1"].set()
        gates["ev2"].set()
        await fence.wait_for_tasks()
        member_written.append("ev2" if count_members(db, "ev2") else "none")
        return start1.run_id, start2.run_id

    run1, run2 = asyncio.run(scenario())
    assert member_written == ["ev2"]
    assert count_members(db, "ev1") == 0
    assert count_members(db, "ev2") == 1
    assert get_run(db, run1)["status"] == "cancelled"
    assert get_run(db, run2)["status"] == "completed"
