"""FishTool 04 · 第三批 c：有限外部发现接入验收（真事务 + 真并发，只 mock 外部源）。

依据：
- ``FishTool_04_..._02补充执行案(1).md`` §3.2（L32-38）/ §16.2 L1362（点名的 ``tests/test_event_discovery.py``）；
- ``FishTool_04_R5执行规格_第三批c`` §2 / §5。

口径（逐条对齐 3b 测试）：
- 外部**聚合源**（``fetcher``）是**唯一**被替身化的东西（等价于 mock 外部 API）；
- claim / commit / finish 全部打到真实临时 SQLite，**围栏未被打桩**（断言 ``event_discovery_runs`` 真实落库）；
- 只数外部调用次数，不数“内部换皮”。

覆盖：
- 两事件同轮**只读一次榜单**（数外部调用）；
- 共享缓存**防迟到**：变更 rule/hash 后旧结果不得提交；
- 四因（合法空 / 接口失败 / 页重复 / 达上限）各有实测；
- 批次记录字段齐（批次 ID / 起止 / 各来源成功失败 / 策略 hash / 候选身份 / 保存时刻 / 保留期限 / 并发更新方式）；
- 预算不可绕过（沿用既有共享 RequestBudget 契约）。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import DatabaseManager
from core.database.event_discovery_repository import EventDiscoveryRepository
from core.database.hot_event_repository import HotEventRepository
from core.database.models_hot_event import (
    DISCOVERY_EMPTY_CAP_REACHED,
    DISCOVERY_EMPTY_INTERFACE_FAILURE,
    DISCOVERY_EMPTY_LEGITIMATE,
    DISCOVERY_EMPTY_PAGE_DUPLICATE,
    EventDiscoveryRun,
    HotEventMember,
)
from modules.hotspot.event_discovery_fence import EventDiscoveryFence
from modules.hotspot.event_discovery_service import (
    BATCH_CONCURRENCY,
    EventDiscoveryBatchStore,
    EventDiscoveryService,
    SharedDiscoveryCache,
    SourceOutcome,
    classify_empty_reason,
)

T = 1788220800

#: 命中事件的候选（标题含实体 + 锚点）。
MATCHING_CANDIDATE = {"bvid": "BV_MATCH", "title": "原神 5.2 版本前瞻", "sources": ["popular"]}
#: 不命中事件的候选。
NON_MATCHING_CANDIDATE = {"bvid": "BV_NOISE", "title": "无关视频合集", "sources": ["popular"]}


class MutableClock:
    """可推进的秒级时钟。"""

    def __init__(self, value: int) -> None:
        """记录初始时刻。"""
        self.value = int(value)

    def __call__(self) -> int:
        """返回当前时刻。"""
        return self.value

    def set(self, value: int) -> None:
        """推进时刻。"""
        self.value = int(value)


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：经 ``DatabaseManager`` 建表 → 跨线程引擎产出会话工厂。"""
    path = tmp_path / "event_discovery.db"
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


def make_event(
    history: HotEventRepository,
    event_id: str = "ev1",
    *,
    rule_version: int = 1,
    policy_hash: str = "h1",
    include_rules: dict | None = None,
    due_past: bool = True,
) -> None:
    """建一个 active 事件（带冻结规则/策略与发现计划）。"""
    history.create_hot_event(
        event_id=event_id,
        name=f"事件-{event_id}",
        now_s=T,
        status="active",
        current_rule_version=rule_version,
        source_policy_hash=policy_hash,
        discovery_due_s=(T - 10) if due_past else (T + 100_000),
        revision=0,
        source_policy={
            "include_rules": include_rules
            or {"entity_groups": [["原神"]], "anchor_groups": [["5.2"]]},
            "exclude_rules": {},
            "event_kind": "version_release",
        },
    )


def build_cache(tmp_path, fetcher, *, ttl_s: int = 600, max_candidates: int = 100, clock=None, **kwargs):
    """构造全局共享轮询缓存（绑定临时批次文件；时钟须与测试时钟一致）。"""
    store = EventDiscoveryBatchStore(tmp_path / "event_discovery_batches.json")
    return SharedDiscoveryCache(
        fetcher,
        batch_store=store,
        ttl_s=ttl_s,
        max_candidates=max_candidates,
        policy_plan=["popular", "ranking", "search_square"],
        clock=clock,
        **kwargs,
    )


def build_fence(db, *, fetch_fn, clock, **overrides) -> EventDiscoveryFence:
    """构造 3b 围栏服务（repo 与 service 共用同一时钟）。"""
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


def _make_service(db, clock, cache, *, strict_auto=True):
    """构造服务：围栏的 fetch_fn 惰性转发到服务真实分发（避免循环构造）。"""
    holder: dict = {}

    async def fetch_fn(event_id, rule_version, policy_hash):
        """围栏外部源：转发到服务的真实分发（读共享缓存）。"""
        return await holder["fetch"](event_id, rule_version, policy_hash)

    fence = build_fence(db, fetch_fn=fetch_fn, clock=clock)
    service = EventDiscoveryService(
        fence=fence,
        shared_cache=cache,
        session_factory=db,
        clock=clock,
        strict_auto=strict_auto,
    )
    holder["fetch"] = service.make_fetch_fn()
    return service, fence


def _runs(db) -> list[dict]:
    """读出全部发现 run 的关键字段。"""
    session = db()
    try:
        return [
            {"id": r.id, "status": r.status, "error_code": r.error_code, "counters": r.counters}
            for r in session.query(EventDiscoveryRun).order_by(EventDiscoveryRun.id.asc()).all()
        ]
    finally:
        session.close()


def _member_bvids(db, event_id: str) -> list[str]:
    """读出某事件的成员 bvid（去重）。"""
    session = db()
    try:
        rows = session.query(HotEventMember).filter(HotEventMember.event_id == event_id).all()
        return sorted({str(r.bvid) for r in rows})
    finally:
        session.close()


# ===========================================================================
# 1) 两事件同轮只读一次榜单（数外部调用）
# ===========================================================================


def test_two_events_share_one_round_poll_reads_source_once(db, tmp_path):
    """两事件同轮各自要数据，但全局榜单**只被读一次**，且两事件都按围栏落成员。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_event(history, "ev1")
    make_event(history, "ev2")

    calls: list[int] = []

    async def fetcher(now_s: int):
        """被调即计数（等价 mock 外部聚合 API）。"""
        calls.append(int(now_s))
        return [SourceOutcome("popular", candidates=[dict(MATCHING_CANDIDATE)])]

    async def scenario():
        cache = build_cache(tmp_path, fetcher, clock=clock)
        service, fence = _make_service(db, clock, cache)
        first = await service.discover_event(
            "ev1", trigger="scheduled", rule_version=1, source_policy_hash="h1"
        )
        second = await service.discover_event(
            "ev2", trigger="scheduled", rule_version=1, source_policy_hash="h1"
        )
        assert first.started is True and second.started is True
        await fence.wait_for_tasks()

    asyncio.run(scenario())

    assert calls == [T], f"两事件同轮应只读一次榜单，实际 {len(calls)} 次"
    assert _member_bvids(db, "ev1") == ["BV_MATCH"]
    assert _member_bvids(db, "ev2") == ["BV_MATCH"]
    runs = _runs(db)
    assert len(runs) == 2 and all(r["status"] == "completed" for r in runs), "围栏未真实落 run"


def test_second_round_expiry_triggers_refresh(db, tmp_path):
    """跨越 TTL 后进入新一轮，必须重新读一次榜单（证明缓存不是“恒不读”）。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_event(history, "ev1")
    calls: list[int] = []

    async def fetcher(now_s: int):
        """被调即计数。"""
        calls.append(int(now_s))
        return [SourceOutcome("popular", candidates=[dict(MATCHING_CANDIDATE)])]

    async def scenario():
        cache = build_cache(tmp_path, fetcher, ttl_s=600, clock=clock)
        # 第一轮（T）
        await cache.snapshot(T)
        # 同一轮内再取 → 命中缓存
        await cache.snapshot(T + 10)
        # 跨过 TTL → 新一轮重新读
        clock.set(T + 700)
        await cache.snapshot(T + 700)

    asyncio.run(scenario())
    assert calls == [T, T + 700]


# ===========================================================================
# 2) 共享缓存防迟到：变更 rule/hash 后旧结果不得提交
# ===========================================================================


def test_shared_cache_result_not_committed_after_rule_change(db, tmp_path):
    """共享缓存分发结果仍走围栏 rule/hash 校验：改规则后旧结果不落成员。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_event(history, "ev1")

    async def scenario():
        gate = asyncio.Event()

        async def fetcher(now_s: int):
            """读取被 gate 挡住，制造“旧结果迟到”。"""
            await gate.wait()
            return [SourceOutcome("popular", candidates=[dict(MATCHING_CANDIDATE)])]

        cache = build_cache(tmp_path, fetcher, clock=clock)
        service, fence = _make_service(db, clock, cache)
        start = await service.discover_event(
            "ev1", trigger="scheduled", rule_version=1, source_policy_hash="h1"
        )
        assert start.started is True

        # 请求进行中改规则：旧 run 失效（cancelled），event 的 rule/hash 已变。
        outcome = await fence.change_rule(
            "ev1", rule_version=2, source_policy_hash="h2", cancel_tasks=False
        )
        assert outcome.cancelled_run_id == start.run_id

        # 释放 gate：旧结果迟到，但共享缓存结果仍必须在围栏处被 rule/hash 校验挡住。
        gate.set()
        await fence.wait_for_tasks()
        return start.run_id

    run_id = asyncio.run(scenario())
    assert _member_bvids(db, "ev1") == [], "改规则后旧结果仍被提交"
    run = next(r for r in _runs(db) if r["id"] == run_id)
    assert run["status"] == "cancelled"


# ===========================================================================
# 3) 四因各有实测（合法空 / 接口失败 / 页重复 / 达上限）
# ===========================================================================


def test_four_empty_reasons_distinct(db, tmp_path):
    """四因必须可分且互不混：四种来源/批次状态各产出一条对应 run。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_event(history, "ev1")

    scenarios = {
        DISCOVERY_EMPTY_LEGITIMATE: (
            lambda: [SourceOutcome("popular", candidates=[])],
            "completed",
        ),
        DISCOVERY_EMPTY_INTERFACE_FAILURE: (
            lambda: [SourceOutcome("popular", state="error", error_code="http_500")],
            "failed",
        ),
        DISCOVERY_EMPTY_PAGE_DUPLICATE: (
            lambda: [
                SourceOutcome("popular", reason="page_duplicate",
                              candidates=[dict(NON_MATCHING_CANDIDATE)])
            ],
            "completed",
        ),
        DISCOVERY_EMPTY_CAP_REACHED: (
            lambda: [
                SourceOutcome("popular", cap_reached=True,
                              candidates=[dict(NON_MATCHING_CANDIDATE)])
            ],
            "completed",
        ),
    }

    seen = {}
    for expected_reason, (make_outcomes, expected_status) in scenarios.items():
        # 每类一个独立事件 + 独立批次目录，避免互相污染。
        event_id = f"ev_{expected_reason}"
        make_event(history, event_id)

        async def scenario(event_id=event_id, make_outcomes=make_outcomes):
            async def fetcher(now_s: int):
                """返回本类来源结果。"""
                return make_outcomes()

            cache = build_cache(tmp_path / event_id, fetcher, clock=clock)
            service, fence = _make_service(db, clock, cache)
            await service.discover_event(
                event_id, trigger="scheduled", rule_version=1, source_policy_hash="h1"
            )
            await fence.wait_for_tasks()

        asyncio.run(scenario())

        session = db()
        try:
            rows = (
                session.query(EventDiscoveryRun)
                .filter(EventDiscoveryRun.event_id == event_id)
                .all()
            )
        finally:
            session.close()
        assert len(rows) == 1, f"{expected_reason}：应恰好一条 run"
        seen[expected_reason] = rows[0].counters.get("empty_reason")
        assert rows[0].counters.get("empty_reason") == expected_reason
        assert rows[0].status == expected_status

    # 四因两两不同（不混）。
    assert len(set(seen.values())) == 4


def test_classify_empty_reason_is_pure_and_ordered():
    """四因判定纯函数：命中即 None；失败优先于上限/重复。"""
    assert classify_empty_reason(matched_count=1, candidate_count=1) is None
    assert (
        classify_empty_reason(matched_count=0, candidate_count=0)
        == DISCOVERY_EMPTY_LEGITIMATE
    )
    assert (
        classify_empty_reason(matched_count=0, candidate_count=0, failed_sources=["popular"])
        == DISCOVERY_EMPTY_INTERFACE_FAILURE
    )
    assert (
        classify_empty_reason(matched_count=0, candidate_count=3, cap_reached=True)
        == DISCOVERY_EMPTY_CAP_REACHED
    )
    assert (
        classify_empty_reason(matched_count=0, candidate_count=3, page_duplicate=True)
        == DISCOVERY_EMPTY_PAGE_DUPLICATE
    )
    # 失败优先于上限（有失败且零候选 → 接口失败）。
    assert (
        classify_empty_reason(
            matched_count=0, candidate_count=0, failed_sources=["popular"], cap_reached=True
        )
        == DISCOVERY_EMPTY_INTERFACE_FAILURE
    )


# ===========================================================================
# 4) 批次记录字段齐（批次记录 = 全局发现状态，非每事件一份）
# ===========================================================================


def test_batch_record_has_all_required_fields(db, tmp_path):
    """批次记录含：批次 ID / 起止 / 各来源成功失败 / 策略 hash / 候选身份 / 保存时刻 / 保留期限 / 并发更新方式。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_event(history, "ev1")

    async def fetcher(now_s: int):
        """一成功一失败，验证“各来源成功/失败”字段。"""
        return [
            SourceOutcome("popular", candidates=[dict(MATCHING_CANDIDATE)], returned_count=1),
            SourceOutcome("ranking", state="error", error_code="http_500"),
        ]

    async def scenario():
        cache = build_cache(tmp_path, fetcher, clock=clock)
        return await cache.snapshot(T, force=True)

    batch = asyncio.run(scenario())
    record = batch.to_dict()

    for key in (
        "batch_id", "started_s", "finished_s", "saved_s", "policy_hash",
        "sources", "candidates", "retention", "concurrency",
    ):
        assert key in record, f"批次记录缺字段 {key}"
    assert record["batch_id"]
    assert record["started_s"] == T and record["saved_s"] == T
    # 各来源成功/失败均可读。
    states = {s["source"]: s["state"] for s in record["sources"]}
    assert states == {"popular": "ok", "ranking": "error"}
    # 策略 hash 为稳定 64 位指纹。
    assert len(record["policy_hash"]) == 64
    # 候选身份（bvid）落库。
    assert [c["bvid"] for c in record["candidates"]] == ["BV_MATCH"]
    # 保留期限 + 并发更新方式。
    assert record["retention"]["max_batches"] >= 1
    assert record["concurrency"] == BATCH_CONCURRENCY

    # 落盘可回读。
    store = EventDiscoveryBatchStore(tmp_path / "event_discovery_batches.json")
    assert store.latest()["batch_id"] == record["batch_id"]


def test_batch_store_retention_bounds_history(tmp_path):
    """history 只保留最近 N 批（保留期限），超出裁掉最旧。"""
    store = EventDiscoveryBatchStore(tmp_path / "batches.json", retention_batches=2)
    for index in range(5):
        store.save({"batch_id": f"b{index}", "saved_s": T + index, "sources": [], "candidates": []})
    history = store.history()
    assert len(history) == 2
    assert [h["batch_id"] for h in history] == ["b3", "b4"]
    assert store.latest()["batch_id"] == "b4"


# ===========================================================================
# 5) 预算不可绕过：沿用既有共享 RequestBudget（拒绝即不读榜单）
# ===========================================================================


def test_budget_denied_does_not_read_source(db, tmp_path):
    """预算不可授予 → 本轮不读来源，落一条 quota_exceeded 失败批次。"""
    clock = MutableClock(T)
    history = HotEventRepository(session_factory=db, clock=clock)
    make_event(history, "ev1")
    calls: list[int] = []

    async def fetcher(now_s: int):
        """被调即计数。"""
        calls.append(int(now_s))
        return [SourceOutcome("popular", candidates=[dict(MATCHING_CANDIDATE)])]

    async def scenario():
        cache = build_cache(
            tmp_path, fetcher, clock=clock, budget_acquire=lambda kind, mono: False
        )
        service, fence = _make_service(db, clock, cache)
        await service.discover_event(
            "ev1", trigger="scheduled", rule_version=1, source_policy_hash="h1"
        )
        await fence.wait_for_tasks()

    asyncio.run(scenario())
    assert calls == [], "预算被拒时仍读了榜单"
    run = _runs(db)[0]
    assert run["status"] == "failed"
    assert run["counters"].get("empty_reason") == DISCOVERY_EMPTY_INTERFACE_FAILURE
