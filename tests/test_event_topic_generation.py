"""FishTool 04 · 第三批 f：事件选题生成契约测试（E16/E17/E18/E35/E36/E52）。

口径：**真临时 SQLite + 真异步**，只 mock 外部 API / LLM；事件模式不依赖 TagCloud。

覆盖：
- E16：LLM 返回不存在的 ``evidence_ref`` → 校验失败/规则回退，不保存伪证据；
- E17：event 模式 TagCloud 接口失败 → 用 context 正常生成，不退回无关 tag；
- E18：LLM 不可用 → 保存规则选题与真实 context，``used_llm=False``；
- E35：LLM 超时/无效 JSON/未知引用 → 事件规则模板，``used_llm=false``，context 不丢；
- E36：Topic 事务中间 flush 失败 → 全部回滚，报保存失败，不 success + 空 saved_ids，不再次生成；
- E52：单次生成选 2 事件、``count=5`` → 最多保存 5 题、每题一个 primary event。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.database import DatabaseManager, Topic
from core.database.hot_event_repository import HotEventRepository
from modules.hotspot import topic_generator as topic_module
from modules.hotspot.topic_generator import TopicGenerator
from modules.hotspot.topic_generation_service import (
    GenerationRequest,
    TopicGenerationService,
    TopicGenerationStore,
)


# ===========================================================================
# 替身 / 夹具
# ===========================================================================

class MutableClock:
    """可手动推进的秒级时钟。"""

    def __init__(self, now: int = 1_000) -> None:
        self.now = int(now)

    def __call__(self) -> int:
        """返回当前 epoch 秒。"""
        return int(self.now)


class FakeLLM:
    """契约级假 LLM。"""

    def __init__(self, *, response=None, configured=True, error=None) -> None:
        self._response = response
        self._configured = configured
        self._error = error
        self.calls: list = []

    def is_configured(self) -> bool:
        """返回预置配置状态。"""
        return self._configured

    async def chat_completion(self, **kwargs):
        """记录调用，返回预置响应或抛错。"""
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class FakeTagGenerator:
    """契约级假词云生成器（可注入失败）。"""

    def __init__(self, word_frequency=None, error=None) -> None:
        self._word_frequency = dict(word_frequency or {})
        self._error = error
        self.calls: list = []

    async def generate_cloud_data(self, zone_name, limit=50, top_n=20):
        """记录调用。"""
        self.calls.append((zone_name, limit, top_n))
        if self._error is not None:
            raise self._error
        return {"word_frequency": dict(self._word_frequency)}


class Env:
    """测试环境容器。"""

    def __init__(self, manager, clock, repo, store) -> None:
        self.manager = manager
        self.clock = clock
        self.repo = repo
        self.store = store


@pytest.fixture()
def env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Env:
    """真实临时库 + 仓储 + 账本 store。"""
    manager = DatabaseManager(str(tmp_path / "event_generation.db"))
    monkeypatch.setattr(topic_module, "get_session", manager.get_session)
    clock = MutableClock()
    repo = HotEventRepository(session_factory=manager.get_session, clock=clock)
    store = TopicGenerationStore(session_factory=manager.get_session, clock=clock)
    return Env(manager, clock, repo, store)


def _llm_response(topics: list) -> dict:
    """包成 OpenAI 风格响应。"""
    return {"choices": [{"message": {"content": json.dumps(topics, ensure_ascii=False)}}]}


def _build_service(env, *, llm=None, tag_error=None):
    """构造服务与依赖。"""
    llm = llm if llm is not None else FakeLLM()
    tag_generator = FakeTagGenerator({"unrelated_tag": 9}, error=tag_error)
    generator = TopicGenerator(api=object(), llm_client=llm, tag_generator=tag_generator)
    service = TopicGenerationService(generator=generator, store=env.store, repository=env.repo)
    return service, generator, llm, tag_generator


def _topics(env) -> list:
    """读取全部 Topic 行。"""
    session = env.manager.get_session()
    try:
        return list(session.query(Topic).order_by(Topic.id).all())
    finally:
        session.close()


def _topic_count(env) -> int:
    """统计 Topic 行数。"""
    session = env.manager.get_session()
    try:
        return int(session.query(Topic).count())
    finally:
        session.close()


def _seed_opportunity_run(env, *, run_id="opp-1", events=(("ev1", "make_candidate"),)):
    """写入一条冻结的 OpportunityRun。"""
    candidates = [
        {
            "event_id": event_id,
            "action": action,
            "rank_key": [0, 0, 0.0, 0.0, 0.0, event_id],
            "deadline": {"deadline_known": False},
            "evidence": {"daily": {"topic_phase": "rising", "a_delta": 12.0, "window_end_s": 900}},
        }
        for event_id, action in events
    ]
    env.repo.create_opportunity_run(
        run_id=run_id,
        policy_version="evp1-test",
        request_fingerprint=f"fp-{run_id}",
        now_s=env.clock.now,
        revision=1,
        creator_brief={"brief_version": "v1", "supported_formats": ["video"], "available_assets": ["screen_record"]},
        assessment_ids=["asmt-1"],
        candidates=candidates,
        result={"ranked_event_ids": [event_id for event_id, _ in events]},
        feedback=[],
    )


def _event_request(**overrides) -> GenerationRequest:
    """构造 event 模式请求。"""
    payload = {
        "direction": "方向",
        "zone_name": "游戏",
        "count": 1,
        "use_llm": False,
        "opportunity_run_id": "opp-1",
        "selected_event_ids": ["ev1"],
        "generation_request_id": "req-ev",
    }
    payload.update(overrides)
    return GenerationRequest(**payload)


# ===========================================================================
# E16 — LLM 返回不存在的 evidence_ref
# ===========================================================================

def test_e16_unknown_evidence_ref_falls_back_without_fake_evidence(env) -> None:
    """未知 evidence_ref → 规则回退，不保存伪证据。"""
    _seed_opportunity_run(env)
    llm = FakeLLM(response=_llm_response([{"title": "模型题", "evidence_refs": ["evd:does-not-exist"]}]))
    service, _generator, _llm, _tag = _build_service(env, llm=llm)

    result = asyncio.run(service.generate(_event_request(count=1, use_llm=True, generation_request_id="e16")))

    assert result["used_llm"] is False  # 校验失败 → 回退
    assert result["topics"]
    assert all(topic["evidence_refs"] == [] for topic in result["topics"])  # 不落伪证据
    assert "模型题" not in [topic["title"] for topic in result["topics"]]

    rows = _topics(env)
    assert rows
    for row in rows:
        assert (row.ai_suggestions.get("evidence_refs") or []) == []
    assert len(llm.calls) == 1  # 只调了一次模型


def test_e16b_valid_evidence_ref_is_kept(env) -> None:
    """来自 context 的合法 evidence_ref 被保留，used_llm=True。"""
    _seed_opportunity_run(env)
    fact_id = "metric:ev1:daily.a_delta"
    llm = FakeLLM(response=_llm_response([{"title": "模型题", "evidence_refs": [fact_id]}]))
    service, _generator, _llm, _tag = _build_service(env, llm=llm)

    result = asyncio.run(service.generate(_event_request(count=1, use_llm=True, generation_request_id="e16b")))

    assert result["used_llm"] is True
    assert result["topics"][0]["evidence_refs"] == [fact_id]
    assert _topics(env)[0].ai_suggestions["evidence_refs"] == [fact_id]


def test_e16c_undeclared_ability_title_is_rejected(env) -> None:
    """未声明能力词（亲测/采访/独家…）标题 → 拒绝该模型结果并回退，绝不落库。"""
    _seed_opportunity_run(env)
    fact_id = "metric:ev1:daily.a_delta"
    llm = FakeLLM(response=_llm_response([{"title": "独家亲测：内幕采访", "evidence_refs": [fact_id]}]))
    service, _generator, _llm, _tag = _build_service(env, llm=llm)

    result = asyncio.run(service.generate(_event_request(count=1, use_llm=True, generation_request_id="e16c")))

    assert result["used_llm"] is False
    for topic in result["topics"]:
        assert "亲测" not in topic["title"]
        assert "采访" not in topic["title"]
        assert "独家" not in topic["title"]
    for row in _topics(env):
        assert "亲测" not in row.title and "独家" not in row.title


# ===========================================================================
# E17 — event 模式 TagCloud 接口失败
# ===========================================================================

def test_e17_event_mode_does_not_depend_on_tag_cloud(env) -> None:
    """TagCloud 失败仍用 context 正常生成，不退回无关 tag。"""
    _seed_opportunity_run(env)
    llm = FakeLLM(error=RuntimeError("llm down"))
    service, _generator, _llm, tag_generator = _build_service(
        env, llm=llm, tag_error=RuntimeError("tag cloud down"),
    )

    result = asyncio.run(service.generate(_event_request(count=1, use_llm=True, generation_request_id="e17")))

    assert result["success"] is True
    assert result["topics"]
    assert tag_generator.calls == []  # 事件模式完全没碰 TagCloud
    assert result["hot_tags"] == ["ev1"]  # 以事件锚点作标签
    assert "unrelated_tag" not in result["hot_tags"]
    assert result["used_llm"] is False


# ===========================================================================
# E18 — LLM 不可用
# ===========================================================================

def test_e18_no_llm_still_works_with_real_context(env) -> None:
    """LLM 不可用时保存规则选题和真实 context，功能仍可用。"""
    _seed_opportunity_run(env)
    service, _generator, llm, _tag = _build_service(env, llm=FakeLLM(configured=False))

    result = asyncio.run(service.generate(_event_request(count=2, use_llm=True, generation_request_id="e18")))

    assert result["used_llm"] is False
    assert len(result["topics"]) == 2
    assert llm.calls == []

    row = _topics(env)[0]
    ai = row.ai_suggestions
    assert ai["hot_event_id"] == "ev1"
    assert ai["opportunity_run_id"] == "opp-1"
    assert ai["action"] == "make_candidate"
    assert ai["context_schema_version"] == 1
    assert ai["generation_request_id"] == "e18"
    assert ai["hot_event_assessment_ids"] == ["asmt-1"]
    assert ai["creator_brief_version"] == "v1"
    assert row.hotspot_id is None  # 不得写 HotEvent ID 到 hotspot_id


# ===========================================================================
# E35 — LLM 超时 / 无效 JSON / 未知引用
# ===========================================================================

@pytest.mark.parametrize(
    "llm",
    [
        FakeLLM(error=TimeoutError("timeout")),
        FakeLLM(error=RuntimeError("api error")),
        FakeLLM(response={"choices": [{"message": {"content": "这不是 JSON"}}]}),
        FakeLLM(response=_llm_response([{"title": "模型题", "evidence_refs": ["nope"]}])),
    ],
)
def test_e35_model_failures_use_event_templates(env, llm) -> None:
    """模型超时/无效 JSON/未知引用 → 事件规则模板，used_llm=False，context 不丢。"""
    _seed_opportunity_run(env)
    service, _generator, _llm, _tag = _build_service(env, llm=llm)

    result = asyncio.run(service.generate(_event_request(count=1, use_llm=True, generation_request_id="e35")))

    assert result["used_llm"] is False
    assert result["topics"]
    assert result["context_snapshot"]["events"][0]["event_id"] == "ev1"
    assert _topics(env)[0].ai_suggestions["action"] == "make_candidate"


# ===========================================================================
# E36 — Topic 事务中间 flush 失败
# ===========================================================================

def test_e36_flush_failure_rolls_back_and_reports_failure(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """flush 失败 → 全部回滚，报保存失败（不 success + 空 saved_ids），不再次生成。"""
    _seed_opportunity_run(env)
    llm = FakeLLM(response=_llm_response([{"title": "A"}, {"title": "B"}]))
    service, generator, llm, _tag = _build_service(env, llm=llm)
    original = generator._insert_topics

    def _partial(session, topics):
        """先插第 1 题，再在 flush 第 2 题时报错。"""
        original(session, list(topics)[:1])
        raise RuntimeError("flush failed on second topic")

    monkeypatch.setattr(generator, "_insert_topics", _partial)

    with pytest.raises(RuntimeError):
        asyncio.run(service.generate(_event_request(count=2, use_llm=True, generation_request_id="e36")))

    assert _topic_count(env) == 0  # 全回滚
    assert len(llm.calls) == 1  # 不再次生成


# ===========================================================================
# E52 — 单次生成选 2 事件、count=5
# ===========================================================================

def test_e52_count_is_total_and_each_topic_has_one_primary_event(env) -> None:
    """最多保存 count 题，每题只绑定一个 primary event，其它进 related_event_ids。"""
    _seed_opportunity_run(env, events=(("ev1", "make_candidate"), ("ev2", "watch_and_collect")))
    service, _generator, _llm, _tag = _build_service(env, llm=FakeLLM(configured=False))

    result = asyncio.run(service.generate(_event_request(
        count=5, use_llm=False, selected_event_ids=["ev1", "ev2"], generation_request_id="e52",
    )))

    topics = result["topics"]
    assert len(topics) == 5  # 不是每事件 5 题
    assert [topic["primary_event_id"] for topic in topics] == ["ev1", "ev1", "ev1", "ev2", "ev2"]
    for topic in topics:
        other = "ev2" if topic["primary_event_id"] == "ev1" else "ev1"
        assert topic["related_event_ids"] == [other]  # 每题只一个 primary，其余关联
        assert topic["hot_event_id"] == topic["primary_event_id"]

    rows = _topics(env)
    assert len(rows) == 5
    for row in rows:
        assert row.ai_suggestions["hot_event_id"] in ("ev1", "ev2")


def test_e52_frozen_order_canonicalizes_selected_ids(env) -> None:
    """selected_event_ids 按 run 冻结顺序 canonical 化（乱序入参不改分配）。"""
    _seed_opportunity_run(env, events=(("ev1", "make_candidate"), ("ev2", "watch_and_collect")))
    service, _generator, _llm, _tag = _build_service(env, llm=FakeLLM(configured=False))

    result = asyncio.run(service.generate(_event_request(
        count=4, use_llm=False, selected_event_ids=["ev2", "ev1"], generation_request_id="e52-order",
    )))

    assert [topic["primary_event_id"] for topic in result["topics"]] == ["ev1", "ev1", "ev2", "ev2"]


def test_e52_count_less_than_events_stays_within_count(env) -> None:
    """count 小于事件数时，总量 <= count。"""
    _seed_opportunity_run(env, events=(("ev1", "make_candidate"), ("ev2", "watch_and_collect")))
    service, _generator, _llm, _tag = _build_service(env, llm=FakeLLM(configured=False))

    result = asyncio.run(service.generate(_event_request(
        count=1, use_llm=False, selected_event_ids=["ev1", "ev2"], generation_request_id="e52-one",
    )))

    assert len(result["topics"]) == 1
    assert result["topics"][0]["primary_event_id"] == "ev1"


def test_e52_no_executable_action_is_research_only(env) -> None:
    """没有可执行 action 的事件只出 research_only 草案，不写“优先制作”。"""
    _seed_opportunity_run(env, events=(("ev9", "not_suitable"),))
    service, _generator, _llm, _tag = _build_service(env, llm=FakeLLM(configured=False))

    result = asyncio.run(service.generate(_event_request(
        count=1, use_llm=False, selected_event_ids=["ev9"], generation_request_id="e52-research",
    )))

    topic = result["topics"][0]
    assert topic["research_only"] is True
    assert "研究" in topic["title"]
    assert "优先制作" not in topic["title"]
    assert "优先制作" not in topic["reason"]
    assert _topics(env)[0].ai_suggestions["research_only"] is True
