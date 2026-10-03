"""FishTool 04 · 第三批 h：§16.3 真端到端（**只 mock B 站与 LLM**）。

链路（§16.3 原文）::

    新建事件 → 发现 → 归属 → 02 watch 写真实临时 SQLite → 事件 assessment
    → CreatorBrief → 机会排序 → 原 TopicGenerator → 选题库回读 → 反馈

硬要求（逐条对齐规格 §1）:

- 只替身**外部 B 站（fetcher）与 LLM 客户端**；``aggregator`` / ``resolver`` /
  ``opportunity`` / ``TopicGenerator`` / ``repository`` / 生成账本**全部真跑**；
- 增长必须由**真实窗口数值**算出（真 ``VideoStats`` 行 + 真窗口边界），
  **严禁**把 aggregator 直接 mock 成「上升」；
- 固定时钟模拟**多窗 / 掉榜 / 停机 / 接口恢复**，各至少一条用例。

窗口网格：daily 固定 ``W = 86400``、UTC 零点锚定 ``T = (as_of // W) * W``；
成员须在最早窗起点 ``S = T - 3W`` 之前 ``accepted`` 才进入冻结分母 ``U``。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import DatabaseManager, Topic
from core.database.event_discovery_repository import EventDiscoveryRepository
from core.database.hot_event_repository import HotEventRepository
from core.database.models_hot_event import EventDiscoveryRun, HotEventMember
from core.database.models_video import Video, VideoStats
from modules.hotspot import topic_generator as topic_module
from modules.hotspot.event_discovery_fence import EventDiscoveryFence
from modules.hotspot.event_discovery_service import (
    EventDiscoveryBatchStore,
    EventDiscoveryService,
    SharedDiscoveryCache,
    SourceOutcome,
)
from modules.hotspot.events.brief import CreatorBrief
from modules.hotspot.events.opportunity import (
    build_opportunity_result,
    get_or_create_opportunity_run,
    request_fingerprint,
)
from modules.hotspot.events.policy import EventPolicy
from modules.hotspot.events.service import EventAggregationService
from modules.hotspot.topic_generation_service import (
    GenerationRequest,
    TopicGenerationService,
    TopicGenerationStore,
)
from modules.hotspot.topic_generator import TopicGenerator
from web.routers import hotspot
from web.routers.hotspot import routes_events

# ===========================================================================
# 固定时钟 + 窗口网格
# ===========================================================================

DAY = 86400
T3 = 1_699_920_000          # 某 UTC 零点（= 19675 * 86400）
T2 = T3 - DAY
T1 = T2 - DAY
T0 = T1 - DAY
CUTOFF = T0                 # S = T3 - 3*DAY（成员须在此前 accepted）
DISCOVERY_S = T0 - 100      # 发现/归属发生在最早窗起点之前

#: 每成员在 4 个日窗边界的累计播放：每窗 +200 / +400 / +800。
VIEWS = {T0: 100, T1: 300, T2: 700, T3: 1500}
#: 合成总量：a=600, b=1200, c=2400（真窗口数值）。
EXPECT_A, EXPECT_B, EXPECT_C = 600.0, 1200.0, 2400.0


class MutableClock:
    """可手动推进的秒级时钟。"""

    def __init__(self, now: int) -> None:
        """记录初始时刻。"""
        self.now = int(now)

    def __call__(self) -> int:
        """返回当前 epoch 秒。"""
        return int(self.now)

    def set(self, value: int) -> None:
        """推进时刻。"""
        self.now = int(value)


class FakeLLM:
    """契约级假 LLM（只替身外部模型，逻辑仍走真 TopicGenerator）。"""

    def __init__(self, *, configured: bool = False, response=None, error=None) -> None:
        """初始化。"""
        self._configured = configured
        self._response = response
        self._error = error
        self.calls: list = []

    def is_configured(self) -> bool:
        """返回配置状态。"""
        return self._configured

    async def chat_completion(self, **kwargs):
        """记录调用并返回预置响应 / 抛错。"""
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """真实临时 SQLite + 仓储 + 可推进时钟。"""
    manager = DatabaseManager(str(tmp_path / "event_e2e.db"))
    monkeypatch.setattr(topic_module, "get_session", manager.get_session)
    clock = MutableClock(DISCOVERY_S)
    repo = HotEventRepository(session_factory=manager.get_session, clock=clock)
    return {"manager": manager, "clock": clock, "repo": repo, "tmp": tmp_path}


# ===========================================================================
# 工具
# ===========================================================================

def _candidates():
    """三条命中「实体 + 锚点」的真实候选（不同作者）。"""
    out = []
    for index, mid in enumerate((9001, 9002, 9003), start=1):
        out.append(
            {
                "bvid": f"BV1e2e00000{index}",
                "title": "原神 5.2 版本前瞻实测",
                "sources": ["popular"],
                "owner_mid": mid,
                "published_epoch_s": T0 - 5 * DAY,
                "raw_tid": 4,
            }
        )
    return out


def _seed_event(env, event_id: str = "ev_e2e", *, now_s: int = DISCOVERY_S) -> None:
    """建一个带冻结规则/策略的 active 事件。"""
    env["repo"].create_hot_event(
        event_id=event_id,
        name=f"事件-{event_id}",
        now_s=now_s,
        status="active",
        current_rule_version=1,
        source_policy_hash="policy-h1",
        revision=0,
        discovery_due_s=now_s - 10,
        entity_scope=["原神"],
        source_policy={
            "entity_scope": ["原神"],
            "include_rules": {"entity_groups": [["原神"]], "anchor_groups": [["5.2"]]},
            "exclude_rules": {},
            "event_kind": "version_release",
        },
    )


def _make_discovery(env, fetcher, *, strict_auto: bool = True, budget_acquire=None):
    """装配**真** EventDiscoveryService（真围栏 + 真仓储 + 真共享缓存；只替身 fetcher）。"""
    db = env["manager"].get_session
    clock = env["clock"]
    store = EventDiscoveryBatchStore(env["tmp"] / "event_e2e_batches.json")
    cache = SharedDiscoveryCache(fetcher, batch_store=store, ttl_s=0, policy_plan=["popular"], clock=clock)
    holder: dict = {}

    async def _fence_fetch(event_id, rule_version, policy_hash):
        """围栏外部源：转发到服务真实分发（读共享缓存）。"""
        return await holder["fetch"](event_id, rule_version, policy_hash)

    fence = EventDiscoveryFence(
        repository=EventDiscoveryRepository(clock=clock),
        session_factory=db,
        fetch_fn=_fence_fetch,
        clock=clock,
        due_interval_seconds=DAY,
        budget_acquire=budget_acquire,
    )
    service = EventDiscoveryService(
        fence=fence, shared_cache=cache, session_factory=db, clock=clock, strict_auto=strict_auto
    )
    holder["fetch"] = service.make_fetch_fn()
    return service, fence


def _run(coro):
    """在独立事件循环里驱动协程（仓库未装 pytest-asyncio）。"""
    return asyncio.run(coro)


def _discover(env, service, fence, event_id: str = "ev_e2e", *, trigger: str = "scheduled") -> str:
    """执行一轮发现并等待后台任务结束，返回 run_id。"""

    async def scenario():
        start = await service.discover_event(
            event_id, trigger=trigger, rule_version=1, source_policy_hash="policy-h1"
        )
        assert start.started is True, "围栏未领取"
        await fence.wait_for_tasks()
        return start.run_id

    return _run(scenario())


def _accept(env, event_id: str, bvids, *, now_s: int = DISCOVERY_S + 10) -> None:
    """人工确认成员为 accepted（decision 在 S 前，进冻结分母 U）。"""
    owner = {"BV1e2e000001": 9001, "BV1e2e000002": 9002, "BV1e2e000003": 9003}
    for bvid in bvids:
        env["repo"].create_member_revision(
            event_id=event_id,
            bvid=bvid,
            revision=2,
            status="accepted",
            first_seen_s=DISCOVERY_S,
            rule_version=1,
            decision_source="manual",
            now_s=now_s,
            owner_mid=owner.get(bvid),
        )


def _write_stats(env, bvid: str, views, *, owner_mid: int = 9001) -> None:
    """写一条 Video + 若干 VideoStats（**02 watch 写真实临时 SQLite**）。"""
    session = env["manager"].get_session()
    try:
        video = Video(bvid=bvid, title=f"原神5.2 视频 {bvid}", mid=owner_mid)
        session.add(video)
        session.flush()
        for ts, view in views.items():
            session.add(
                VideoStats(
                    video_id=video.id, view=int(view), view_status="ok", captured_epoch_s=int(ts)
                )
            )
        session.commit()
    finally:
        session.close()


def _set_view(env, bvid: str, ts: int, view: int) -> None:
    """改写某 BVID 在某边界的真实播放值（用于证明结果随真数值变化）。"""
    session = env["manager"].get_session()
    try:
        row = (
            session.query(VideoStats)
            .join(Video, VideoStats.video_id == Video.id)
            .filter(Video.bvid == bvid, VideoStats.captured_epoch_s == int(ts))
            .one()
        )
        row.view = int(view)
        session.commit()
    finally:
        session.close()


def _member_bvids(env, event_id: str) -> list:
    """读某事件成员 BVID（去重排序）。"""
    session = env["manager"].get_session()
    try:
        rows = session.query(HotEventMember).filter(HotEventMember.event_id == event_id).all()
        return sorted({str(r.bvid) for r in rows})
    finally:
        session.close()


def _discovery_run(env, run_id: str) -> EventDiscoveryRun:
    """读发现 run 行。"""
    session = env["manager"].get_session()
    try:
        return session.get(EventDiscoveryRun, run_id)
    finally:
        session.close()


def _topics(env) -> list:
    """读全部 Topic 行。"""
    session = env["manager"].get_session()
    try:
        return list(session.query(Topic).order_by(Topic.id).all())
    finally:
        session.close()


def _run_row(env, run_id: str):
    """读机会 run 行。"""
    session = env["manager"].get_session()
    try:
        from core.database.models_hot_event import OpportunityRun

        return session.get(OpportunityRun, run_id)
    finally:
        session.close()


def _agg(env) -> EventAggregationService:
    """真评估服务。"""
    return EventAggregationService(session_factory=env["manager"].get_session, repository=env["repo"])


def _brief() -> CreatorBrief:
    """一个允许「原神」、有可用素材与制作时间的 CreatorBrief。"""
    return CreatorBrief.from_dict(
        {
            "brief_version": "v1",
            "production_hours": 2,
            "review_hours": 1,
            "publish_buffer_hours": 0,
            "max_experiment_hours": 4,
            "available_assets": ["screen_record"],
            "supported_formats": ["video"],
            "allowed_entities": ["原神"],
        }
    )


def _facts(event_id: str, daily: dict) -> dict:
    """单事件事实包（实体与 CreatorBrief 对齐 → account match）。"""
    return {
        "event_id": event_id,
        "entities": ["原神"],
        "domains": [],
        "daily": dict(daily),
        "early": {},
        "discovery": {},
        "evidence_refs": [],
    }


def _api_client(env) -> TestClient:
    """用同一临时库配置路由，供反馈段真跑（Topic.status 与反馈同事务）。"""
    routes_events.reset_state()
    routes_events.configure(
        session_factory=env["manager"].get_session,
        repository=env["repo"],
        clock=env["clock"],
        discovery_enabled=True,
    )
    app = FastAPI()
    app.include_router(hotspot.router, prefix="/api/hotspot")
    return TestClient(app)


def _full_daily(env, event_id: str):
    """跑真日级聚合（真聚合内核 + 真 VideoStats），返回结果。"""
    return _agg(env).run_daily(event_id, as_of_s=T3)


# ===========================================================================
# 1) 全链真跑（§16.3 主链路，逐段断言）
# ===========================================================================

def test_full_chain_end_to_end(env) -> None:
    """新建事件 → 发现 → 归属 → 02 写库 → assessment → brief → 机会 → 选题 → 回读 → 反馈。"""
    eid = "ev_e2e"
    # -- 新建事件
    _seed_event(env, eid)
    # -- 发现 + 归属（真 resolver）：只替身外部 fetcher
    async def fetcher(now_s: int):
        return [SourceOutcome("popular", candidates=_candidates())]

    service, fence = _make_discovery(env, fetcher)
    run_id = _discover(env, service, fence, eid)
    assert _discovery_run(env, run_id).status == "completed", "发现 run 未真落库"
    bvids = _member_bvids(env, eid)
    assert bvids == ["BV1e2e000001", "BV1e2e000002", "BV1e2e000003"], "归属未真落成员"
    # -- 人工确认（decision 在 S 前）→ 进冻结分母
    _accept(env, eid, bvids)
    # -- 02 watch 写真实临时 SQLite
    for bvid in bvids:
        _write_stats(env, bvid, VIEWS)
    # -- 事件 assessment（真 aggregator：真窗口数值算出）
    result = _full_daily(env, eid)
    assert result["available_windows"] == 3
    assert (result["a_delta"], result["b_delta"], result["c_delta"]) == (EXPECT_A, EXPECT_B, EXPECT_C)
    assert result["topic_phase"] == "rising" and result["stage_reason"] != "rising_low_base"
    assert result["sample_gate_passed"] is True
    assessment = _agg(env).persist_assessment(
        eid, result, window_kind="daily24h", rule_version=1, metrics=result
    )
    assert assessment.status in ("complete", "completed", "collecting", "partial", "stale")
    # -- CreatorBrief + 机会排序（真 opportunity）
    brief = _brief()
    built = build_opportunity_result([_facts(eid, assessment.metrics or result)], brief, T3)
    top = built["executable"][0]
    assert top["action"] == "make_candidate", top
    assert top["rank_key"] is not None and top["event_id"] == eid
    fp = request_fingerprint(
        brief, assessment_ids=[assessment.id], request_as_of_s=T3, policy_version=built["policy_version"]
    )
    run = get_or_create_opportunity_run(
        env["repo"],
        brief=brief,
        request_fingerprint=fp,
        candidates=built["candidates"],
        result=built["result"],
        assessment_ids=[assessment.id],
        policy_version=built["policy_version"],
        now_s=T3,
    )
    assert run.revision == 1
    # -- 原 TopicGenerator（真生成 + 假 LLM）→ 选题落库
    gen_service = TopicGenerationService(
        generator=TopicGenerator(api=object(), llm_client=FakeLLM(configured=False), tag_generator=object()),
        store=TopicGenerationStore(session_factory=env["manager"].get_session, clock=env["clock"]),
        repository=env["repo"],
    )
    generated = _run(
        gen_service.generate(
            GenerationRequest(
                direction="游戏解说",
                zone_name="游戏",
                count=2,
                use_llm=False,
                opportunity_run_id=run.id,
                selected_event_ids=[eid],
                generation_request_id="e2e-req-1",
            )
        )
    )
    assert generated["success"] is True and generated["saved_ids"]
    # -- 选题库回读：上下文可追溯
    topics = _topics(env)
    assert len(topics) == len(generated["saved_ids"])
    assert topics[0].ai_suggestions["opportunity_run_id"] == run.id
    assert topics[0].ai_suggestions["hot_event_id"] == eid
    # -- 反馈（真 route）：revision 递增
    client = _api_client(env)
    topic_id = str(topics[0].id)
    body = {
        "feedback_id": "fb-e2e-1",
        "expected_revision": int(run.revision),
        "event_id": eid,
        "topic_id": topic_id,
        "kind": "adopted",
        "reason": "先做低成本验证",
    }
    resp = client.post(f"/api/hotspot/opportunities/{run.id}/feedback", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["revision"] == int(run.revision) + 1
    assert _run_row(env, run.id).revision == int(run.revision) + 1


# ===========================================================================
# 2) 增长由真窗口数值算出（反例：mock aggregator 成「上升」不算通过）
# ===========================================================================

def test_growth_computed_from_real_window_values(env) -> None:
    """改真实 ``VideoStats`` → 结果随之改变，证明增长来自真窗口而非被 mock 的结论。"""
    eid = "ev_growth"
    _seed_event(env, eid)

    async def fetcher(now_s: int):
        return [SourceOutcome("popular", candidates=_candidates())]

    service, fence = _make_discovery(env, fetcher)
    _discover(env, service, fence, eid)
    bvids = _member_bvids(env, eid)
    _accept(env, eid, bvids)
    for bvid in bvids:
        _write_stats(env, bvid, VIEWS)

    first = _full_daily(env, eid)
    assert (first["a_delta"], first["b_delta"], first["c_delta"]) == (EXPECT_A, EXPECT_B, EXPECT_C)

    # 把成员1 的末窗真实播放 +5000 → c_delta 必须同步 +5000（不是固定「上升」）。
    _set_view(env, "BV1e2e000001", T3, VIEWS[T3] + 5000)
    second = _full_daily(env, eid)
    assert second["c_delta"] == EXPECT_C + 5000.0, "增长未随真窗口数值变化（疑似被 mock 结论）"
    assert second["a_delta"] == EXPECT_A and second["b_delta"] == EXPECT_B


# ===========================================================================
# 3) 掉榜：某成员缺窗 → 不假增长、不误并
# ===========================================================================

def test_dropped_member_missing_window_no_fake_growth(env) -> None:
    """成员掉榜（缺一窗）→ 交集缩小 / 样本门不过，不凭空算增长、不误报确认上升。"""
    eid = "ev_drop"
    _seed_event(env, eid)

    async def fetcher(now_s: int):
        return [SourceOutcome("popular", candidates=_candidates())]

    service, fence = _make_discovery(env, fetcher)
    _discover(env, service, fence, eid)
    bvids = _member_bvids(env, eid)
    _accept(env, eid, bvids)
    # A、C 满窗；B 缺 T2（T1→T3 跨 2 天 > 36h 门）→ 断段，不硬补。
    _write_stats(env, "BV1e2e000001", VIEWS)
    _write_stats(env, "BV1e2e000002", {T0: 100, T1: 300, T3: 1500})
    _write_stats(env, "BV1e2e000003", VIEWS)

    result = _full_daily(env, eid)
    assert result["panel_video_count"] == 2, "掉榜成员被错误并入窗口"
    assert result["sample_gate_passed"] is False, "样本门未如实降级"
    # 机会层：样本门不过 → 不给 make_candidate（不假增长）
    built = build_opportunity_result([_facts(eid, result)], _brief(), T3)
    assert all(c["action"] != "make_candidate" for c in built["candidates"])


# ===========================================================================
# 4) 接口恢复：失败轮不落成员；恢复后只记新发现，不假装增长
# ===========================================================================

def test_discovery_interface_failure_then_recovery(env) -> None:
    """首轮接口失败 → run failed 且无成员；恢复后重新发现 → 新成员落库，不伪造增量。"""
    eid = "ev_recover"
    _seed_event(env, eid)
    state = {"fail": True}

    async def fetcher(now_s: int):
        if state["fail"]:
            return [SourceOutcome("popular", state="error", error_code="http_500")]
        return [SourceOutcome("popular", candidates=_candidates())]

    service, fence = _make_discovery(env, fetcher)
    run1 = _discover(env, service, fence, eid)
    assert _discovery_run(env, run1).status == "failed"
    assert _member_bvids(env, eid) == [], "接口失败轮不应落成员"

    # 越过租约（300s）与退避后恢复（手动触发：退避期内不重复自动发现）。
    env["clock"].set(DISCOVERY_S + 400)
    state["fail"] = False
    run2 = _discover(env, service, fence, eid, trigger="manual")
    assert _discovery_run(env, run2).status == "completed"
    assert _member_bvids(env, eid) == ["BV1e2e000001", "BV1e2e000002", "BV1e2e000003"]
    assert run2 != run1, "恢复轮必须是新 run，不拼旧 run"


# ===========================================================================
# 5) 停机：请求中暂停 → 迟到结果被围栏拒绝
# ===========================================================================

def test_pause_mid_discovery_drops_late_result(env) -> None:
    """发现请求中暂停事件 → 先失效 token 再取消；迟到结果不得写成员。"""
    eid = "ev_pause"
    _seed_event(env, eid)
    gate = asyncio.Event()

    async def fetcher(now_s: int):
        await gate.wait()
        return [SourceOutcome("popular", candidates=_candidates())]

    service, fence = _make_discovery(env, fetcher)

    async def scenario():
        start = await service.discover_event(
            eid, trigger="scheduled", rule_version=1, source_policy_hash="policy-h1"
        )
        assert start.started is True
        outcome = await fence.invalidate(eid, reason="event_paused", cancel_tasks=False)
        assert outcome.cancelled_run_id == start.run_id
        gate.set()  # 释放断点：旧结果迟到
        await fence.wait_for_tasks()
        return start.run_id

    run_id = _run(scenario())
    assert _member_bvids(env, eid) == [], "停机后迟到结果仍被写入"
    assert _discovery_run(env, run_id).status == "cancelled"
