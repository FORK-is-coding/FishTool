"""FishTool 04 · 第三批 b：事件仓储层专项测试（CAS + 历史 revision 查询）。

依据：
- ``FishTool_04_R5执行规格_第三批b_事件归属与发现围栏.md`` §2「必须覆盖」与
  原文硬要求 L1457/1459：**新增 ``tests/test_event_repository.py`` 用于 CAS 和历史
  revision 查询 —— 不是用函数 mock 替代数据库行为**；
- 上游 §4.3 版本化（E30：某成员先 accepted 后 rejected，历史取 latest revision 后判断
  状态；当前集合**不复活**旧 accepted）。

测试策略：
- 用 ``DatabaseManager`` 在临时目录建真实 SQLite 文件（含 3a 六表），再换普通引擎产出
  会话工厂；**不开任何函数 mock**，CAS 与历史查询全部打到真实 SQLite 事务；
- 并发用 ``threading.Barrier`` 让两个线程真正同时 CAS，验证「只有一方成功」。
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import DatabaseManager
from core.database.event_discovery_repository import EventDiscoveryRepository
from core.database.hot_event_repository import HotEventRepository
from core.database.models_hot_event import HotEventMember

#: 统一测试时钟：2026-09-01T00:00:00Z 的 epoch 秒。
T: int = 1788220800


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：经 ``DatabaseManager`` 建六表 → 换可跨线程引擎产出会话工厂。"""
    path = tmp_path / "event_repository.db"
    manager = DatabaseManager(str(path))  # 经 DatabaseManager 建表（含六张表）
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


@pytest.fixture()
def history_repo(db):
    """3a 的 HotEventRepository（历史 revision 查询），真库。"""
    return HotEventRepository(session_factory=db, clock=lambda: T)


@pytest.fixture()
def fence_repo():
    """发现围栏仓储（CAS / claim），固定时钟。"""
    return EventDiscoveryRepository(clock=lambda: T)


def _new_active_event(history_repo, event_id="ev1", *, rule_version=1, policy_hash="h1"):
    """建一个 active 事件（带冻结规则/策略），供围栏用例引用。"""
    return history_repo.create_hot_event(
        event_id=event_id,
        name="测试事件",
        now_s=T,
        status="active",
        current_rule_version=rule_version,
        source_policy_hash=policy_hash,
        discovery_due_s=T - 10,
        revision=0,
    )


# ===========================================================================
# CAS：带 revision 谓词的条件更新（真实 DB 行为）
# ===========================================================================


def test_cas_success_then_stale_revision_fails(
    db, fence_repo, history_repo
):
    """CAS 契约：``revision`` 命中才成功；随后旧 revision 必定失败。"""
    _new_active_event(history_repo)
    session = db()
    try:
        ok = fence_repo.compare_and_swap_event(
            session,
            event_id="ev1",
            expected_revision=0,
            values={"updated_s": T + 5, "status": "paused"},
        )
        assert ok is True
        session.commit()
        # 同一个（已过期的）revision 再次 CAS 必须失败。
        stale = fence_repo.compare_and_swap_event(
            session,
            event_id="ev1",
            expected_revision=0,
            values={"updated_s": T + 6, "status": "active"},
        )
        assert stale is False
        session.commit()
    finally:
        session.close()

    refreshed = history_repo.get_hot_event("ev1")
    assert refreshed.revision == 1
    assert refreshed.status == "paused"


def test_cas_concurrent_only_one_wins(db, fence_repo, history_repo):
    """并发 CAS：两个线程同时用 ``expected_revision=0`` → 只有一个 rowcount==1。"""
    _new_active_event(history_repo)
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []

    def worker(tag: int) -> None:
        session = db()
        try:
            barrier.wait(timeout=5)
            ok = fence_repo.compare_and_swap_event(
                session,
                event_id="ev1",
                expected_revision=0,
                values={"updated_s": T + tag, "name": f"改名{tag}"},
            )
            session.commit()
            outcomes.append(ok)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == [False, True], f"并发 CAS 结果异常: {outcomes}"
    assert history_repo.get_hot_event("ev1").revision == 1


# ===========================================================================
# 历史 revision 查询：先限 decision_at_s<=cutoff，再取最新 revision
# ===========================================================================


def test_history_latest_revision_at_cutoff(db, history_repo):
    """历史状态：``status_at`` 先在截止时刻截断，再取最新 revision。"""
    _new_active_event(history_repo)
    history_repo.create_member_revision(
        event_id="ev1",
        bvid="BV1",
        revision=1,
        status="accepted",
        first_seen_s=T,
        rule_version=1,
        decision_source="strict_rule",
        now_s=T,
    )
    history_repo.create_member_revision(
        event_id="ev1",
        bvid="BV1",
        revision=2,
        status="rejected",
        first_seen_s=T,
        rule_version=1,
        decision_source="manual",
        now_s=T + 100,
    )
    # 截止在两次决定之间 → 只能看到 rev1 accepted。
    mid = history_repo.status_at("ev1", "BV1", T + 50)
    assert mid is not None and mid.revision == 1 and mid.status == "accepted"
    # 截止在第二次决定之后 → 看到 rev2 rejected。
    after = history_repo.status_at("ev1", "BV1", T + 200)
    assert after is not None and after.revision == 2 and after.status == "rejected"
    # 当前状态 = 最新 revision。
    assert history_repo.latest_status("ev1", "BV1").status == "rejected"


def test_E30_accepted_then_rejected_no_revival(db, history_repo):
    """E30：先 accepted 后 rejected → 历史取 latest revision 判断；当前集合不复活旧 accepted。

    反向对照（错误写法）会先过滤 ``status='accepted'`` 再取最新，从而把已撤销成员复活。
    """
    _new_active_event(history_repo)
    history_repo.create_member_revision(
        event_id="ev1",
        bvid="BV1",
        revision=1,
        status="accepted",
        first_seen_s=T,
        rule_version=1,
        decision_source="strict_rule",
        now_s=T,
    )
    history_repo.create_member_revision(
        event_id="ev1",
        bvid="BV1",
        revision=2,
        status="rejected",
        first_seen_s=T,
        rule_version=1,
        decision_source="manual",
        now_s=T + 100,
    )

    # 正确：截止在最晚决定之后，latest revision = rejected（不复活 rev1 accepted）。
    assert history_repo.status_at("ev1", "BV1", T + 1000).status == "rejected"
    assert history_repo.latest_status("ev1", "BV1").status == "rejected"

    # 反向对照：先过滤 accepted 再取最新（错误顺序）会复活旧 accepted。
    session = db()
    try:
        naive = (
            session.query(HotEventMember)
            .filter(
                HotEventMember.event_id == "ev1",
                HotEventMember.bvid == "BV1",
                HotEventMember.decision_at_s <= T + 1000,
                HotEventMember.status == "accepted",  # <-- 错误：先按状态过滤
            )
            .order_by(HotEventMember.revision.desc())
            .first()
        )
    finally:
        session.close()
    assert naive is not None and naive.status == "accepted", "反向对照用例本身失效"


# ===========================================================================
# claim CAS：同一 event 并发 / 反复领取只有一方拿到可执行 run
# ===========================================================================


def test_claim_discovery_concurrent_only_one_creates_run(db, fence_repo, history_repo):
    """D01（仓储层）：同 event 并发 claim，仅一方成功且创建 running run。"""
    _new_active_event(history_repo)
    barrier = threading.Barrier(2)
    claimed_flags: list[bool] = []

    def worker(run_id: str, token: str) -> None:
        session = db()
        try:
            barrier.wait(timeout=5)
            outcome = fence_repo.claim_discovery(
                session,
                event_id="ev1",
                run_id=run_id,
                lease_token=token,
                trigger="scheduled",
                now_s=T,
                rule_version=1,
                source_policy_hash="h1",
            )
            if outcome.claimed:
                session.commit()
            else:
                session.rollback()
            claimed_flags.append(outcome.claimed)
        finally:
            session.close()

    threads = [
        threading.Thread(target=worker, args=("run_a", "tok_a")),
        threading.Thread(target=worker, args=("run_b", "tok_b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(claimed_flags) == [False, True], f"并发 claim 结果异常: {claimed_flags}"
    # 只有一个可执行 run 落库。
    session = db()
    try:
        from core.database.models_hot_event import EventDiscoveryRun

        runs = session.query(EventDiscoveryRun).filter(EventDiscoveryRun.event_id == "ev1").all()
        assert len(runs) == 1 and runs[0].status == "running"
    finally:
        session.close()


def test_claim_not_due_does_not_create_run(db, fence_repo, history_repo):
    """scheduled 未到期：reason=not_due，且**不创建**任何 run。"""
    history_repo.create_hot_event(
        event_id="ev2",
        name="未到期事件",
        now_s=T,
        status="active",
        current_rule_version=1,
        source_policy_hash="h1",
        discovery_due_s=T + 10_000,  # 尚未到期
        revision=0,
    )
    session = db()
    try:
        outcome = fence_repo.claim_discovery(
            session,
            event_id="ev2",
            run_id="run_due",
            lease_token="tok_due",
            trigger="scheduled",
            now_s=T,
            rule_version=1,
            source_policy_hash="h1",
        )
        session.rollback()
    finally:
        session.close()
    assert outcome.claimed is False and outcome.reason == "not_due"
    from core.database.models_hot_event import EventDiscoveryRun

    session = db()
    try:
        assert session.query(EventDiscoveryRun).count() == 0
    finally:
        session.close()
