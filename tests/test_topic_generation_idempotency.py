"""FishTool 04 · 第三批 f：生成账本与幂等的契约级测试（G01—G10，§16.2.2）。

口径：
- **真临时 SQLite 事务 + 真并发异步调度**，只 mock 外部 API / LLM；
- 不 mock 掉 claim / freeze / complete / store 后宣称通过；
- 对直接内核 ``_insert_topics`` 断言 **commit / rollback / close 调用 0 次**；
- 对成功账本断言 **Topic 写入与 state 变更在同一事务**。

覆盖：G01—G10 + ``deadline < lease`` 配置校验 + ``_insert_topics`` flush-only + 旧路径不退化。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from core.database import DatabaseManager, Topic
from core.database.hot_event_repository import HotEventRepository
from core.database.models_hot_event import TopicGenerationRun
from modules.hotspot import topic_generator as topic_module
from modules.hotspot.topic_generator import TopicGenerator
from modules.hotspot.topic_generation_service import (
    DEFAULT_GENERATION_DEADLINE_SECONDS,
    DEFAULT_GENERATION_LEASE_SECONDS,
    GenerationConfigError,
    GenerationKeyConflict,
    GenerationOwnershipLost,
    GenerationRequest,
    GenerationTerminalError,
    GenerationUnavailable,
    GenerationValidationError,
    TopicGenerationService,
    TopicGenerationStore,
    _sha256_payload,
    load_generation_config,
    normalize_generation_request,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ===========================================================================
# 测试替身 / 夹具
# ===========================================================================

class MutableClock:
    """可手动推进的秒级时钟。"""

    def __init__(self, now: int = 1_000) -> None:
        self.now = int(now)

    def __call__(self) -> int:
        """返回当前 epoch 秒。"""
        return int(self.now)

    def advance(self, seconds: int) -> None:
        """前进指定秒数。"""
        self.now += int(seconds)


class FakeLLM:
    """契约级假 LLM：可注入响应 / 配置状态 / 异常 / 延迟。"""

    def __init__(self, *, response=None, configured=True, error=None, delay=0.0) -> None:
        self._response = response
        self._configured = configured
        self._error = error
        self._delay = delay
        self.calls: list = []

    def is_configured(self) -> bool:
        """返回预置的配置状态。"""
        return self._configured

    async def chat_completion(self, **kwargs):
        """记录调用；可延时让出事件循环以制造并发。"""
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._response


class FakeTagGenerator:
    """契约级假词云生成器。"""

    def __init__(self, word_frequency=None, error=None) -> None:
        self._word_frequency = dict(word_frequency or {})
        self._error = error
        self.calls: list = []

    async def generate_cloud_data(self, zone_name, limit=50, top_n=20):
        """记录调用，返回契约结构或抛错。"""
        self.calls.append((zone_name, limit, top_n))
        if self._error is not None:
            raise self._error
        return {"word_frequency": dict(self._word_frequency)}


class _CountingSession:
    """包装真实 Session，统计 commit / rollback / close 次数（证明内核 flush-only）。"""

    def __init__(self, real) -> None:
        self._real = real
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def add(self, *args, **kwargs):
        """透传 add。"""
        return self._real.add(*args, **kwargs)

    def flush(self, *args, **kwargs):
        """透传 flush。"""
        return self._real.flush(*args, **kwargs)

    def commit(self, *args, **kwargs):
        """计数后透传 commit。"""
        self.commit_calls += 1
        return self._real.commit(*args, **kwargs)

    def rollback(self, *args, **kwargs):
        """计数后透传 rollback。"""
        self.rollback_calls += 1
        return self._real.rollback(*args, **kwargs)

    def close(self, *args, **kwargs):
        """计数后透传 close。"""
        self.close_calls += 1
        return self._real.close(*args, **kwargs)


class Env:
    """临时库 + 仓储 + 账本 store 的测试环境。"""

    def __init__(self, manager, clock, repo, store) -> None:
        self.manager = manager
        self.clock = clock
        self.repo = repo
        self.store = store


@pytest.fixture()
def env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Env:
    """构造真实临时库，并把模块级 get_session 接管到该库。"""
    manager = DatabaseManager(str(tmp_path / "generation.db"))
    monkeypatch.setattr(topic_module, "get_session", manager.get_session)
    clock = MutableClock()
    repo = HotEventRepository(session_factory=manager.get_session, clock=clock)
    store = TopicGenerationStore(session_factory=manager.get_session, clock=clock)
    return Env(manager, clock, repo, store)


def _llm_response(topics: list) -> dict:
    """把选题数组包成 OpenAI 风格响应。"""
    return {"choices": [{"message": {"content": json.dumps(topics, ensure_ascii=False)}}]}


def _build_service(env, *, llm=None, tag_frequency=None, tag_error=None):
    """构造服务 + 生成器 + 假依赖。"""
    llm = llm if llm is not None else FakeLLM(response=_llm_response([{"title": "AI选题"}]))
    tag_generator = FakeTagGenerator(tag_frequency or {"a": 3, "b": 2, "c": 1}, error=tag_error)
    generator = TopicGenerator(api=object(), llm_client=llm, tag_generator=tag_generator)
    service = TopicGenerationService(generator=generator, store=env.store, repository=env.repo)
    return service, generator, llm, tag_generator


def _topics(env) -> list:
    """读取全部 Topic 行（detach 副本）。"""
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


def _ledger_row(env, request_id: str):
    """读取账本行。"""
    session = env.manager.get_session()
    try:
        return session.get(TopicGenerationRun, request_id)
    finally:
        session.close()


def _seed_opportunity_run(env, *, run_id="opp-1", events=(("ev1", "make_candidate"),)):
    """写入一条冻结的 OpportunityRun（candidates 决定冻结事件顺序）。"""
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


def _tag_only_request(**overrides) -> GenerationRequest:
    """构造 tag_only 请求（默认不带键）。"""
    payload = {"direction": "方向", "zone_name": "游戏", "count": 3, "use_llm": True}
    payload.update(overrides)
    return GenerationRequest(**payload)


def _event_request(run_id="opp-1", **overrides) -> GenerationRequest:
    """构造 event 模式请求（默认带键）。"""
    payload = {
        "direction": "方向",
        "zone_name": "游戏",
        "count": 2,
        "use_llm": False,
        "opportunity_run_id": run_id,
        "selected_event_ids": ["ev1"],
        "generation_request_id": "req-ev",
    }
    payload.update(overrides)
    return GenerationRequest(**payload)


# ===========================================================================
# G01 — 同 key 同 payload 并发：仅一方生成，另一方 202，模型调用 1 次
# ===========================================================================

def test_g01_concurrent_same_key_only_one_generates(env) -> None:
    """并发同键只领到一方，另一方 202；外部模型调用 1 次、Topic 只有一批。"""
    service, _generator, llm, _tag = _build_service(
        env,
        llm=FakeLLM(response=_llm_response([{"title": f"T{index}"} for index in range(3)]), delay=0.05),
    )
    request = _tag_only_request(generation_request_id="req-1", count=3)

    async def _race():
        return await asyncio.gather(service.generate(request), service.generate(request))

    results = asyncio.run(_race())

    completed = [item for item in results if item.get("replayed") is False and item.get("accepted") is not True]
    accepted = [item for item in results if item.get("accepted") is True]
    assert len(completed) == 1 and len(accepted) == 1
    assert accepted[0]["status"] == "running" and accepted[0]["http_status"] == 202
    assert "topics" not in accepted[0]  # 202 不返回假 topics
    assert len(llm.calls) == 1  # 只有拿到 claim 的一方访问了模型
    assert _topic_count(env) == 3  # Topic 只有一批
    assert _ledger_row(env, "req-1").state == "completed"


# ===========================================================================
# G02 — 提交成功后响应丢失：同 key 重试返回原结果，外部调用 0 次、Topic 0 新增
# ===========================================================================

def test_g02_retry_after_lost_response_returns_saved_batch(env) -> None:
    """同 key 重试返回原 stored 响应（原 saved_ids / 原 generated_at）。"""
    service, _generator, llm, _tag = _build_service(
        env, llm=FakeLLM(response=_llm_response([{"title": "T1"}, {"title": "T2"}])),
    )
    request = _tag_only_request(generation_request_id="req-2", count=2)

    first = asyncio.run(service.generate(request))
    assert first["replayed"] is False and first["saved_ids"] and first["generated_at"]
    calls_after_first = len(llm.calls)
    count_after_first = _topic_count(env)

    replay = asyncio.run(service.generate(request))

    assert replay["replayed"] is True
    assert replay["saved_ids"] == first["saved_ids"]
    assert replay["generated_at"] == first["generated_at"]  # 不刷新时间
    assert len(llm.calls) == calls_after_first  # 外部调用 0 次
    assert _topic_count(env) == count_after_first  # Topic 新增 0 条


# ===========================================================================
# G03 — 同 key 不同内容 → 409，零模型调用零 Topic
# ===========================================================================

def test_g03_same_key_different_payload_conflicts(env) -> None:
    """count / direction / context_mode 变化 → 409 generation_key_conflict。"""
    service, _generator, llm, _tag = _build_service(
        env, llm=FakeLLM(response=_llm_response([{"title": "T1"}, {"title": "T2"}])),
    )
    request = _tag_only_request(generation_request_id="req-3", count=2)
    asyncio.run(service.generate(request))
    calls = len(llm.calls)
    count = _topic_count(env)

    for changed in (
        _tag_only_request(generation_request_id="req-3", count=5),
        _tag_only_request(generation_request_id="req-3", count=2, direction="别的方向"),
        _tag_only_request(generation_request_id="req-3", count=2, context_mode="historical"),
    ):
        with pytest.raises(GenerationKeyConflict) as excinfo:
            asyncio.run(service.generate(changed))
        assert excinfo.value.code == "generation_key_conflict"

    assert len(llm.calls) == calls  # 不调用模型
    assert _topic_count(env) == count  # 不插 Topic


# ===========================================================================
# G04 — 同内容新 key：允许新生成与新批次（不按内容 hash 永久去重）
# ===========================================================================

def test_g04_new_key_same_content_allowed(env) -> None:
    """用户明确重新生成用新键 → 新批次允许。"""
    service, _generator, llm, _tag = _build_service(
        env, llm=FakeLLM(response=_llm_response([{"title": "T1"}, {"title": "T2"}])),
    )
    first = asyncio.run(service.generate(_tag_only_request(generation_request_id="req-4a", count=2)))
    second = asyncio.run(service.generate(_tag_only_request(generation_request_id="req-4b", count=2)))

    assert first["saved_ids"] != second["saved_ids"]
    assert len(llm.calls) == 2
    assert _topic_count(env) == 4


# ===========================================================================
# G05 — 插第 2 个 Topic 或保存账本结果报错 → 全批与 completed 一起 rollback
# ===========================================================================

def test_g05_partial_insert_rolls_back_whole_batch(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """第 2 个 Topic flush 失败 → 无半批成功，且存储错误不触发模型回退。"""
    service, generator, llm, _tag = _build_service(
        env, llm=FakeLLM(response=_llm_response([{"title": "T1"}, {"title": "T2"}, {"title": "T3"}])),
    )
    original = generator._insert_topics

    def _partial(session, topics):
        """先插第 1 个，再在第 2 个时报错。"""
        original(session, list(topics)[:1])
        raise RuntimeError("flush boom on second topic")

    monkeypatch.setattr(generator, "_insert_topics", _partial)

    with pytest.raises(RuntimeError):
        asyncio.run(service.generate(_tag_only_request(generation_request_id="req-5", count=3)))

    assert _topic_count(env) == 0  # 全批回滚，无半批
    assert _ledger_row(env, "req-5").state != "completed"  # completed 一起回滚
    assert len(llm.calls) == 1  # 存储错误不触发再次生成


def test_g05b_response_not_serializable_rolls_back(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """响应构造/序列化失败同样回滚 Topic 与账本。"""
    service, generator, _llm, _tag = _build_service(env)
    original = generator.generate_topics

    async def _bad_draft(*args, **kwargs):
        """返回带不可序列化数值（NaN，allow_nan=False 会拒绝）的草稿。"""
        draft = await original(*args, **kwargs)
        draft["bad_nan"] = float("nan")
        return draft

    monkeypatch.setattr(generator, "generate_topics", _bad_draft)

    with pytest.raises(ValueError):
        asyncio.run(service.generate(_tag_only_request(generation_request_id="req-5b", count=1)))

    assert _topic_count(env) == 0
    assert _ledger_row(env, "req-5b").state != "completed"


# ===========================================================================
# G06 — 模型已调用后崩溃 / lease 过期 → interrupted，旧 token 禁止提交
# ===========================================================================

def test_g06_lease_expired_blocks_stale_token_and_blocks_model_restart(env) -> None:
    """lease 过期后旧 token 不能提交；同 key 不自动再调模型；恢复标 interrupted。"""
    service, generator, llm, _tag = _build_service(env)
    payload = normalize_generation_request(_tag_only_request(count=1))
    request_hash = _sha256_payload(payload)

    claim = env.store.claim_generation("req-6", payload, request_hash)
    assert claim.kind == "acquired"

    env.clock.advance(env.store.lease_seconds + 1)  # lease 过期

    with pytest.raises(GenerationOwnershipLost):
        env.store.complete_generation(claim, {"topics": [{"title": "T"}]}, generator._insert_topics)
    assert _topic_count(env) == 0  # 旧 token 禁止提交

    with pytest.raises(GenerationTerminalError) as excinfo:
        asyncio.run(service.generate(_tag_only_request(count=1, generation_request_id="req-6")))
    assert excinfo.value.error_code == "lease_expired"
    assert len(llm.calls) == 0  # 同 key 不自动再调模型

    assert env.store.recover_expired_generation_runs() == 1
    row = _ledger_row(env, "req-6")
    assert row.state == "interrupted" and row.error_code == "lease_expired" and row.lease_token is None


# ===========================================================================
# G07 — 已完成键重放：读冻结结果，不刷新时间/证据，不重调 LLM
# ===========================================================================

def test_g07_replay_reads_frozen_result_without_refresh(env) -> None:
    """原事件过期 / 模型配置改变时，重放仍读原冻结结果并标历史。"""
    _seed_opportunity_run(env)
    llm = FakeLLM(configured=False)  # 首次走规则回退，避免外部依赖
    service, _generator, _llm, _tag = _build_service(env, llm=llm)
    request = _event_request(count=2, use_llm=False, generation_request_id="req-7")

    first = asyncio.run(service.generate(request))
    assert first["replayed"] is False

    # 模型配置改变 + 证据过期（时钟推远），但重放不得重算
    env.clock.advance(10 * 24 * 3600)
    service2, _generator2, llm2, _tag2 = _build_service(env, llm=FakeLLM(error=RuntimeError("model changed")))
    replay = asyncio.run(service2.generate(request))

    assert replay["replayed"] is True
    assert replay["generated_at"] == first["generated_at"]
    assert replay["saved_ids"] == first["saved_ids"]
    assert replay["context_snapshot"] == first["context_snapshot"]  # 原 as_of 未刷新
    assert len(llm.calls) == 0 and len(llm2.calls) == 0  # 不重调 LLM
    assert _topic_count(env) == 2  # 不重新保存 Topic


# ===========================================================================
# G08 — 202 / failed / cancelled / interrupted 的轮询语义
# ===========================================================================

def test_g08_poll_状态_语义(env) -> None:
    """running 继续轮询；completed 结束；failed/cancelled/interrupted 结束并给原因。"""
    payload = normalize_generation_request(_tag_only_request(count=1))
    request_hash = _sha256_payload(payload)

    # running
    claim = env.store.claim_generation("req-8a", payload, request_hash)
    state = env.store.read_state("req-8a")
    assert state["status"] == "running"

    # failed → 终态错误，不偷偷重发
    env.store.fail_if_owned(claim, "model_error")
    state = env.store.read_state("req-8a")
    assert state["status"] == "failed" and state["reason_code"] == "model_error"
    service, _generator, _llm, _tag = _build_service(env)
    with pytest.raises(GenerationTerminalError):
        asyncio.run(service.generate(_tag_only_request(count=1, generation_request_id="req-8a")))

    # cancelled / interrupted
    claim_b = env.store.claim_generation("req-8b", payload, request_hash)
    env.store.cancel_if_owned(claim_b)
    assert env.store.read_state("req-8b")["status"] == "cancelled"

    claim_c = env.store.claim_generation("req-8c", payload, request_hash)
    env.store.interrupt_if_owned(claim_c, "deadline_exceeded")
    assert env.store.read_state("req-8c")["status"] == "interrupted"

    # 未知键 → 404（None）
    assert env.store.read_state("req-unknown") is None


def test_g08b_completed_优先于_lease_过期(env) -> None:
    """已完成结果永不因 lease 过期变失败。"""
    service, _generator, _llm, _tag = _build_service(env)
    asyncio.run(service.generate(_tag_only_request(count=1, generation_request_id="req-8d")))

    env.clock.advance(env.store.lease_seconds * 10)
    state = env.store.read_state("req-8d")
    assert state["status"] == "completed" and state["result"] is not None


# ===========================================================================
# G09 — 无键旧 tag_only 与缺键 event 请求
# ===========================================================================

def test_g09_keyless_tag_only_legacy_and_event_requires_key(env) -> None:
    """无键 tag_only 保留兼容并标 legacy_unprotected；缺键 event → 422。"""
    service, _generator, _llm, _tag = _build_service(env)

    legacy = asyncio.run(service.generate(_tag_only_request(count=2, use_llm=False)))
    assert legacy["generation_mode"] == "tag_only"
    assert legacy["idempotency"] == "legacy_unprotected"
    assert _topic_count(env) == 2  # 旧保存路径照常落库

    # 只给 selected_event_ids 没 run_id → 422
    with pytest.raises(GenerationValidationError):
        asyncio.run(service.generate(_tag_only_request(selected_event_ids=["ev1"])))

    # event 模式缺键 → 422
    _seed_opportunity_run(env)
    with pytest.raises(GenerationValidationError):
        asyncio.run(service.generate(GenerationRequest(
            direction="方向", zone_name="游戏", count=2, use_llm=False,
            opportunity_run_id="opp-1", selected_event_ids=["ev1"],
        )))


# ===========================================================================
# G10 — 存储不可用 / COMMIT 结果未知 → 503 且保留同 key；先读账本不猜失败
# ===========================================================================

def test_g10_commit_unknown_returns_503_and_keeps_key(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """COMMIT 未知且查询不可用 → 503 并保留同 key（不猜失败另插一批）。"""
    service, _generator, _llm, _tag = _build_service(env)

    def _boom(*args, **kwargs):
        """模拟存储整体不可用。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(env.store, "complete_generation", _boom)
    monkeypatch.setattr(env.store, "is_completed", _boom)  # 探测也失败 → 状态未知
    monkeypatch.setattr(env.store, "fail_if_owned", _boom)

    with pytest.raises(GenerationUnavailable) as excinfo:
        asyncio.run(service.generate(_tag_only_request(count=1, generation_request_id="req-10")))
    assert excinfo.value.code == "generation_state_unknown"

    monkeypatch.undo()
    # 同 key 仍保留（running），不猜失败、不另插一批
    assert _ledger_row(env, "req-10").state == "running"
    assert _topic_count(env) == 0


def test_g10b_commit_succeeded_response_lost_reads_ledger(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """COMMIT 成功但响应失败 → 先读账本返回原结果，不重复生成。"""
    service, _generator, _llm, _tag = _build_service(
        env, llm=FakeLLM(response=_llm_response([{"title": "T1"}, {"title": "T2"}])),
    )
    real_complete = env.store.complete_generation

    def _commit_then_raise(claim, draft, insert_topics):
        """先真正提交，再模拟响应丢失。"""
        response = real_complete(claim, draft, insert_topics)
        assert response["saved_ids"]
        raise RuntimeError("response lost after commit")

    monkeypatch.setattr(env.store, "complete_generation", _commit_then_raise)

    result = asyncio.run(service.generate(_tag_only_request(count=2, generation_request_id="req-10b")))

    assert result["replayed"] is True
    assert result["saved_ids"]
    assert _topic_count(env) == 2  # 不重复插入
    assert _ledger_row(env, "req-10b").state == "completed"


# ===========================================================================
# _insert_topics flush-only 内核断言
# ===========================================================================

def test_insert_topics_is_flush_only(env) -> None:
    """内核内 commit / rollback / close 调用 0 次，且不发网络。"""
    tag_generator = FakeTagGenerator({"a": 1})
    generator = TopicGenerator(api=object(), llm_client=None, tag_generator=tag_generator)

    session = env.manager.get_session()
    spy = _CountingSession(session)
    try:
        saved_ids = generator._insert_topics(
            spy, [{"title": "A", "keywords": ["k"]}, {"title": "B", "zone_name": "游戏"}]
        )
        assert len(saved_ids) == 2
        assert spy.commit_calls == 0 and spy.rollback_calls == 0 and spy.close_calls == 0
        session.commit()  # 由调用方提交
    finally:
        session.close()

    assert _topic_count(env) == 2
    assert tag_generator.calls == []  # 内核不发网络


def test_insert_topics_validates_before_writing(env) -> None:
    """先全部校验再写入：第 2 题非法时第 1 题也不落库。"""
    generator = TopicGenerator(api=object(), llm_client=None, tag_generator=FakeTagGenerator({"a": 1}))
    session = env.manager.get_session()
    try:
        with pytest.raises(ValueError):
            generator._insert_topics(session, [{"title": "A"}, {"keywords": []}])
        session.rollback()
    finally:
        session.close()
    assert _topic_count(env) == 0


# ===========================================================================
# 旧路径不退化 + deadline < lease
# ===========================================================================

def test_legacy_four_positional_args_and_persist_default_true(env) -> None:
    """旧四位置参数可用，persist 默认 True，generation_mode='tag_only'。"""
    generator = TopicGenerator(
        api=object(), llm_client=None, tag_generator=FakeTagGenerator({"a": 3, "b": 2}),
    )
    result = asyncio.run(generator.generate_topics("方向", "游戏", 2, False))  # 四位置参数

    assert result["success"] is True
    assert result["generation_mode"] == "tag_only"
    assert result["used_llm"] is False
    assert len(result["saved_ids"]) == 2  # persist 默认 True → 已落库
    assert _topic_count(env) == 2


def test_deadline_must_be_less_than_lease(env) -> None:
    """``generation_deadline_seconds`` 必须小于 ``generation_lease_seconds``。"""
    class _FakeConfig:
        """最小配置替身。"""

        def __init__(self, value):
            self._value = value

        def get(self, key, default=None):
            """只响应 ``hotspot.events``。"""
            return self._value if key == "hotspot.events" else default

    # 默认值可用
    default_config = load_generation_config()
    assert default_config["generation_lease_seconds"] == DEFAULT_GENERATION_LEASE_SECONDS
    assert default_config["generation_deadline_seconds"] == DEFAULT_GENERATION_DEADLINE_SECONDS

    # 违反 deadline < lease → 立即报错
    with pytest.raises(GenerationConfigError):
        load_generation_config(_FakeConfig({"generation_lease_seconds": 120, "generation_deadline_seconds": 300}))
    with pytest.raises(GenerationConfigError):
        load_generation_config(_FakeConfig({"generation_lease_seconds": 100, "generation_deadline_seconds": 100}))
    with pytest.raises(GenerationConfigError):
        load_generation_config(_FakeConfig("not-a-mapping"))
    with pytest.raises(GenerationConfigError):
        TopicGenerationStore(lease_seconds=100, deadline_seconds=100)

    # config.yaml 实际段满足 deadline < lease
    raw = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    events = raw["hotspot"]["events"]
    assert events["generation_deadline_seconds"] < events["generation_lease_seconds"]
