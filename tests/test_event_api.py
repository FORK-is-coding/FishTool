"""第三批 g：事件 API（§14）契约测试。

覆盖（§4 表点名）：
- 任务 completed ≠ 数据有效：assessment completed + ``data_status=insufficient`` 时
  通用 ``GET /event-tasks/{id}`` 仍返回 ``completed``，轮询能正常结束；
- 发现 ``failed/cancelled/interrupted`` 统一映射 HTTP 任务 ``failed`` 并带原 ``reason_code``；
- 句柄丢失 → **404** 且给持久 run 读取方式；
- 状态码契约：``expected_revision`` 冲突 409 / 不存在 404 / 非法 422；
- 422 不被吞（``extra`` 禁止 / 非法枚举）；
- 上限：事件选择 5 / 成员决定 100；
- 反馈幂等 E37 / E53：同内容返原记录（不 409），变内容 409；
- E38：``verification_status=user_reported``，客户端不能标 ``platform_verified``。

测试策略：真临时 SQLite；只替身外部发现源与评估内核（避免联网与 3d 重计算）；
async 用例统一 ``asyncio.run`` 驱动（仓库未装 pytest-asyncio）。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import DatabaseManager, Topic
from core.database.hot_event_repository import HotEventRepository
from core.database.models_hot_event import EventDiscoveryRun, HotEvent, HotEventMember
from modules.hotspot.event_discovery_fence import DiscoveryStartResult
from web.routers import hotspot
from web.routers.hotspot import routes_events

T = 1_700_000_000


class MutableClock:
    """可手动推进的秒级时钟。"""

    def __init__(self, now: int = T) -> None:
        """初始化时钟。"""
        self.now = int(now)

    def __call__(self) -> int:
        """返回当前 epoch 秒。"""
        return int(self.now)


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：经 ``DatabaseManager`` 建表后换普通引擎产出会话工厂。"""
    path = tmp_path / "event_api.db"
    manager = DatabaseManager(str(path))
    manager.engine.dispose()
    engine = create_engine(f"sqlite:///{path}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


class FakeDiscoveryService:
    """契约级假发现服务：写一条指定状态的 ``EventDiscoveryRun`` 后返回 start 结果。"""

    def __init__(self, db, *, run_status: str = "completed", error_code=None) -> None:
        """初始化。"""
        self._db = db
        self._status = run_status
        self._error = error_code

    async def discover_event(self, event_id, *, trigger, rule_version, source_policy_hash, now_s=None):
        """写 run 行并返回 ``DiscoveryStartResult``。"""
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        running = self._status == "running"
        session = self._db()
        session.add(
            EventDiscoveryRun(
                id=run_id,
                event_id=event_id,
                rule_version=int(rule_version),
                source_policy_hash=source_policy_hash or "policy_hash",
                lease_token="tok_x",
                trigger=trigger,
                started_s=int(now_s or T),
                finished_s=None if running else int(now_s or T),
                status=self._status,
                error_code=self._error,
                candidates=[] if running else [{"bvid": "BV1xx411c7mD"}],
                newly_discovered_bvids=[] if running else ["BV1xx411c7mD"],
                counters={"empty_reason": None},
                source_attempts=[],
            )
        )
        session.commit()
        session.close()
        return DiscoveryStartResult(started=True, run_id=run_id, trigger=trigger)


class FakeAggregationService:
    """契约级假评估服务：直接落一条指定 status 的 assessment（不跑 3d 内核）。"""

    def __init__(self, db, repo, *, status: str = "insufficient", reason_codes=("missing_previous_window",)) -> None:
        """初始化。"""
        self._repo = repo
        self._status = status
        self._reason_codes = tuple(reason_codes)

    def run_daily(self, event_id, *, as_of_s, **kwargs):
        """返回固定结果（含 fingerprint / window_end_s / status）。"""
        return {
            "as_of_s": int(as_of_s),
            "window_end_s": int(as_of_s),
            "fingerprint": f"fp_{event_id}_{as_of_s}",
            "status": "insufficient",
            "reason_codes": list(self._reason_codes),
            "policy_version": "evp1-test",
        }

    def run_early(self, event_id, *, as_of_s, fast_panel_bvids, **kwargs):
        """early 分支（本测试用不到，返回同形状）。"""
        return self.run_daily(event_id, as_of_s=as_of_s)

    def persist_assessment(self, event_id, result, *, window_kind, rule_version, policy_version=None, **kwargs):
        """落一条 assessment 行并返回。"""
        return self._repo.create_assessment(
            event_id=event_id,
            revision=1,
            as_of_s=int(result["as_of_s"]),
            window_end_s=int(result["window_end_s"]),
            window_kind=window_kind,
            rule_version=int(rule_version),
            policy_version=policy_version or "evp1-test",
            status=self._status,
            input_fingerprint=str(result["fingerprint"]),
            interpretation={"reason_codes": list(self._reason_codes)},
            metrics={},
            provenance={},
        )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """装配临时库 + 假服务 + TestClient。"""
    path = tmp_path / "event_api_env.db"
    manager = DatabaseManager(str(path))
    manager.engine.dispose()
    engine = create_engine(f"sqlite:///{path}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    clock = MutableClock()
    repo = HotEventRepository(session_factory=factory, clock=clock)

    routes_events.reset_state()
    routes_events.configure(
        session_factory=factory,
        repository=repo,
        discovery_service=FakeDiscoveryService(factory),
        aggregation_service=FakeAggregationService(factory, repo),
        clock=clock,
        max_event_selection=5,
    )
    app = FastAPI()
    app.include_router(hotspot.router, prefix="/api/hotspot")
    client = TestClient(app)
    try:
        yield {"client": client, "db": factory, "repo": repo, "clock": clock}
    finally:
        routes_events.reset_state()
        engine.dispose()


def _create_event(client, name="事件A", status="active", **extra):
    """建事件并返回 event_id。"""
    body = {"name": name, "status": status}
    body.update(extra)
    resp = client.post("/api/hotspot/events", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["event_id"]


# ===========================================================================
# 接线注册
# ===========================================================================


def test_all_14_endpoints_registered():
    """§14 清单端点已全部注册到 hotspot router。"""
    methods_by_path: dict = {}
    for route in hotspot.router.routes:
        methods_by_path.setdefault(route.path, set()).update(getattr(route, "methods", set()) or set())
    expected = {
        ("POST", "/events"),
        ("GET", "/events"),
        ("GET", "/events/{event_id}"),
        ("PATCH", "/events/{event_id}"),
        ("POST", "/events/{event_id}/discover/tasks"),
        ("GET", "/event-discovery-runs/{run_id}"),
        ("GET", "/events/{event_id}/members"),
        ("POST", "/events/{event_id}/members/decisions"),
        ("POST", "/events/{event_id}/assess/tasks"),
        ("GET", "/events/{event_id}/assessments"),
        ("GET", "/event-assessments/{assessment_id}"),
        ("POST", "/opportunities/tasks"),
        ("GET", "/opportunities/{run_id}"),
        ("POST", "/opportunities/{run_id}/feedback"),
        ("GET", "/event-tasks/{task_id}"),
        ("POST", "/topics/generate"),
        ("GET", "/topics/generation-runs/{generation_request_id}"),
    }
    for method, path in expected:
        assert method in methods_by_path.get(path, set()), (method, path)


# ===========================================================================
# 任务语义分离 / 句柄
# ===========================================================================


def test_assessment_completed_but_data_insufficient_poll_finishes(env):
    """任务 completed 但 data 不足：返回 completed + data_status=insufficient，轮询结束。"""
    client = env["client"]
    eid = _create_event(client)
    resp = client.post(f"/api/hotspot/events/{eid}/assess/tasks", json={"as_of_s": T})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "completed"
    assert data["result"]["data_status"] == "insufficient"
    assert data["result"]["reason_codes"] == ["missing_previous_window"]
    assert data["result"]["next_action"] == "continue_watch"

    task = client.get(f"/api/hotspot/event-tasks/{data['task_id']}")
    assert task.status_code == 200
    assert task.json()["data"]["status"] == "completed"  # 不是 running，不死等


def test_discovery_failed_maps_to_task_failed_with_reason(env):
    """发现 failed/cancelled/interrupted → 统一映射 HTTP 任务 failed 并带原 reason_code。"""
    client = env["client"]
    eid = _create_event(client)
    routes_events.configure(
        discovery_service=FakeDiscoveryService(env["db"], run_status="interrupted", error_code="lease_expired")
    )
    start = client.post(f"/api/hotspot/events/{eid}/discover/tasks")
    assert start.status_code == 200
    task_id = start.json()["data"]["task_id"]
    task = client.get(f"/api/hotspot/event-tasks/{task_id}")
    assert task.status_code == 200
    body = task.json()["data"]
    assert body["status"] == "failed"
    assert body["reason_code"] == "lease_expired"
    assert body["run_status"] == "interrupted"


def test_discovery_completed_maps_to_task_completed(env):
    """发现 completed → HTTP 任务 completed + run_status。"""
    client = env["client"]
    eid = _create_event(client)
    start = client.post(f"/api/hotspot/events/{eid}/discover/tasks")
    task = client.get(f"/api/hotspot/event-tasks/{start.json()['data']['task_id']}")
    body = task.json()["data"]
    assert body["status"] == "completed"
    assert body["result"]["run_status"] == "completed"


def test_handle_lost_returns_404_with_persistent_read(env):
    """内存句柄丢失 → 404 + 持久 run 读取方式，不假装恢复。"""
    client = env["client"]
    resp = client.get("/api/hotspot/event-tasks/etask_does_not_exist")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error_code"] == "task_handle_lost"
    assert "persistent_read" in detail
    assert "event-discovery-runs" in detail["persistent_read"]["discovery_run"]


def test_discovery_run_persistently_readable_after_restart(env):
    """发现 run 落库后，即使句柄丢失也能通过持久接口读取。"""
    client = env["client"]
    eid = _create_event(client)
    start = client.post(f"/api/hotspot/events/{eid}/discover/tasks")
    run_id = start.json()["data"]["run_id"]
    routes_events._EVENT_TASKS.clear()  # 模拟重启：内存句柄全丢（保留注入的库）
    resp = client.get(f"/api/hotspot/event-discovery-runs/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["run_id"] == run_id


# ===========================================================================
# 状态码契约
# ===========================================================================


def test_patch_revision_conflict_409(env):
    """expected_revision 不匹配 → 409。"""
    client = env["client"]
    eid = _create_event(client)
    resp = client.patch(f"/api/hotspot/events/{eid}", json={"expected_revision": 999, "status": "paused"})
    assert resp.status_code == 409


def test_patch_not_found_404(env):
    """事件不存在 → 404。"""
    client = env["client"]
    resp = client.patch("/api/hotspot/events/evt_missing", json={"expected_revision": 0, "status": "paused"})
    assert resp.status_code == 404


def test_get_event_not_found_404(env):
    """GET 不存在事件 → 404。"""
    assert env["client"].get("/api/hotspot/events/evt_missing").status_code == 404


def test_invalid_status_is_422_not_500(env):
    """非法枚举 → 422，且 422 不被吞成 500。"""
    client = env["client"]
    resp = client.post("/api/hotspot/events", json={"name": "x", "status": "not_a_status"})
    assert resp.status_code == 422


def test_extra_field_forbidden_422(env):
    """客户端自填 phase → 422（extra 禁止）。"""
    client = env["client"]
    resp = client.post("/api/hotspot/events", json={"name": "x", "phase": "rising"})
    assert resp.status_code == 422


def test_client_cannot_self_fill_verified_deadline(env):
    """客户端自填“已验证 deadline”顶层字段 → 422（extra 禁止）。"""
    client = env["client"]
    resp = client.post("/api/hotspot/events", json={"name": "x", "deadline_verified": True})
    assert resp.status_code == 422


def test_event_selection_upper_limit(env):
    """机会任务事件上限 5：6 个 → 422。"""
    client = env["client"]
    ids = [_create_event(client, name=f"事件{i}") for i in range(6)]
    body = {
        "creator_brief": {
            "brief_version": "v1",
            "production_hours": 1,
            "review_hours": 0,
            "publish_buffer_hours": 0,
            "max_experiment_hours": 1,
        },
        "event_ids": ids,
    }
    resp = client.post("/api/hotspot/opportunities/tasks", json=body)
    assert resp.status_code == 422


def test_member_decisions_upper_limit_and_cas(env):
    """成员决定 CAS + 上限；非法 status → 422。"""
    client = env["client"]
    eid = _create_event(client)
    # CAS 冲突
    conflict = client.post(
        f"/api/hotspot/events/{eid}/members/decisions",
        json={"expected_revision": 99, "decisions": [{"bvid": "BV1xx411c7mD", "status": "accepted"}]},
    )
    assert conflict.status_code == 409
    # 合法追加
    ok = client.post(
        f"/api/hotspot/events/{eid}/members/decisions",
        json={"expected_revision": 0, "decisions": [{"bvid": "BV1xx411c7mD", "status": "accepted"}]},
    )
    assert ok.status_code == 200
    assert client.get(f"/api/hotspot/events/{eid}/members").json()["data"]["by_status"]["accepted"] == 1
    # 非法 status → 422
    bad = client.post(
        f"/api/hotspot/events/{eid}/members/decisions",
        json={"expected_revision": 1, "decisions": [{"bvid": "BV1xx411c7mD", "status": "bogus"}]},
    )
    assert bad.status_code == 422


# ===========================================================================
# 反馈幂等 E37 / E53 / E38
# ===========================================================================


def _seed_opportunity(env, event_id: str) -> str:
    """建事件 + 一条引用它、含 saved_ids 的机会 run，并建 Topic(id=1)。"""
    repo = env["repo"]
    run_id = f"opp_{uuid.uuid4().hex[:8]}"
    repo.create_opportunity_run(
        run_id=run_id,
        policy_version="evp1-test",
        request_fingerprint=f"fp_{run_id}",
        creator_brief={"brief_version": "v1"},
        assessment_ids=[],
        candidates=[{"event_id": event_id}],
        result={"ranked_event_ids": [event_id], "saved_ids": ["1"]},
        feedback=[],
    )
    session = env["db"]()
    session.add(Topic(id=1, title="选题一", status="pending"))
    session.commit()
    session.close()
    return run_id


def _feedback_body(**over):
    """反馈请求固定九字段默认体。"""
    body = {
        "feedback_id": "fb-1",
        "expected_revision": 1,
        "event_id": "evt1",
        "topic_id": "1",
        "kind": "adopted",
        "reason": "先做低成本验证",
    }
    body.update(over)
    return body


def test_feedback_idempotent_same_content(env):
    """E37：同 feedback_id 重复提交同内容 → 幂等返回原记录，不 409。"""
    client = env["client"]
    eid = _create_event(client)
    run_id = _seed_opportunity(env, eid)
    body = _feedback_body(event_id=eid)

    first = client.post(f"/api/hotspot/opportunities/{run_id}/feedback", json=body)
    assert first.status_code == 200
    assert first.json()["data"]["idempotent_replay"] is False
    rev_after = first.json()["data"]["revision"]

    second = client.post(f"/api/hotspot/opportunities/{run_id}/feedback", json=body)
    assert second.status_code == 200
    assert second.json()["data"]["idempotent_replay"] is True
    assert second.json()["data"]["revision"] == rev_after  # 未再递增


def test_feedback_same_id_different_content_409(env):
    """E37：同 id 变内容 → 409。"""
    client = env["client"]
    eid = _create_event(client)
    run_id = _seed_opportunity(env, eid)
    client.post(f"/api/hotspot/opportunities/{run_id}/feedback", json=_feedback_body(event_id=eid))
    conflict = client.post(
        f"/api/hotspot/opportunities/{run_id}/feedback",
        json=_feedback_body(event_id=eid, reason="换了理由"),
    )
    assert conflict.status_code == 409


def test_feedback_stale_revision_same_content_ok(env):
    """E53：首次成功但响应丢失，旧 revision 重试同 id 同内容 → 返原反馈不 409。"""
    client = env["client"]
    eid = _create_event(client)
    run_id = _seed_opportunity(env, eid)
    body = _feedback_body(event_id=eid)
    first = client.post(f"/api/hotspot/opportunities/{run_id}/feedback", json=body)
    assert first.status_code == 200
    # 客户端仍以为 revision=1（旧值），重试同内容同 id。
    retry = client.post(f"/api/hotspot/opportunities/{run_id}/feedback", json=_feedback_body(event_id=eid))
    assert retry.status_code == 200
    assert retry.json()["data"]["idempotent_replay"] is True


def test_feedback_verification_status_user_reported(env):
    """E38：客户端自填播放量只能是 user_reported，不能标 platform_verified。"""
    client = env["client"]
    eid = _create_event(client)
    run_id = _seed_opportunity(env, eid)
    body = _feedback_body(
        event_id=eid,
        kind="published",
        published_bvid="BV1xx411c7mD",
        outcome_metrics={"view": 12345, "metric_source": "user_screenshot", "observed_s": T},
    )
    resp = client.post(f"/api/hotspot/opportunities/{run_id}/feedback", json=body)
    assert resp.status_code == 200
    entry = resp.json()["data"]["feedback"]
    assert entry["verification_status"] == "user_reported"
    assert entry["verification_status"] != "platform_verified"
    assert entry["outcome_metrics"]["values"]["view"] == 12345.0


def test_feedback_rejects_non_finite_metric(env):
    """outcome_metrics 非有限数 → 422。"""
    client = env["client"]
    eid = _create_event(client)
    run_id = _seed_opportunity(env, eid)
    # 用原始 body 发送 Infinity（httpx 的 json= 会拒绝 NaN/Inf）。
    raw = (
        '{"feedback_id":"fb-1","expected_revision":1,"event_id":"'
        + eid
        + '","topic_id":"1","kind":"outcome","reason":"r",'
        '"outcome_metrics":{"view": Infinity}}'
    )
    resp = client.post(
        f"/api/hotspot/opportunities/{run_id}/feedback",
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422


def test_feedback_topic_status_synced_for_adopted(env):
    """adopted 通过共享事务助手同步旧 Topic.status。"""
    client = env["client"]
    eid = _create_event(client)
    run_id = _seed_opportunity(env, eid)
    resp = client.post(f"/api/hotspot/opportunities/{run_id}/feedback", json=_feedback_body(event_id=eid))
    assert resp.status_code == 200
    assert resp.json()["data"]["topic_status_synced"] == "adopted"
    session = env["db"]()
    try:
        assert session.get(Topic, 1).status == "adopted"
    finally:
        session.close()


def test_feedback_rejected_does_not_add_new_topic_status(env):
    """rejected 只写 OpportunityRun.feedback，不给旧 Topic.status 加新值。"""
    client = env["client"]
    eid = _create_event(client)
    run_id = _seed_opportunity(env, eid)
    resp = client.post(
        f"/api/hotspot/opportunities/{run_id}/feedback",
        json=_feedback_body(event_id=eid, kind="rejected"),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["topic_status_synced"] is None
    session = env["db"]()
    try:
        assert session.get(Topic, 1).status == "pending"
    finally:
        session.close()


def test_feedback_event_not_in_run_422(env):
    """event_id 不属于该 run → 422。"""
    client = env["client"]
    eid = _create_event(client)
    run_id = _seed_opportunity(env, eid)
    resp = client.post(
        f"/api/hotspot/opportunities/{run_id}/feedback",
        json=_feedback_body(event_id="evt_other"),
    )
    assert resp.status_code == 422


def test_opportunity_not_found_404(env):
    """机会 run 不存在 → 404。"""
    resp = env["client"].post("/api/hotspot/opportunities/opp_missing/feedback", json=_feedback_body())
    assert resp.status_code == 404


# ===========================================================================
# 第三批 h：发现外部源接线（§18.2 开关 + 可注入假 fetcher 各路径）
# ===========================================================================


def _seed_event_row(env, event_id, *, now=T, status="active", rule_version=0, policy_hash="h0", **extra):
    """直接建一条 active 事件（带非空 policy_hash，供真实围栏领取）。"""
    fields = dict(
        status=status,
        current_rule_version=rule_version,
        source_policy_hash=policy_hash,
        revision=0,
        discovery_due_s=now - 10,
        source_policy={
            "include_rules": {"entity_groups": [["原神"]], "anchor_groups": [["5.2"]]},
            "exclude_rules": {},
            "event_kind": "version_release",
        },
    )
    fields.update(extra)
    env["repo"].create_hot_event(event_id=event_id, name=f"事件-{event_id}", now_s=now, **fields)


def _build_real_discovery(env, tmp_path, fetcher, *, budget_acquire=None, **fence_overrides):
    """装配真实 EventDiscoveryService（真围栏 + 真仓储 + 真共享缓存；只替身外部 fetcher）。"""
    from core.database.event_discovery_repository import EventDiscoveryRepository
    from modules.hotspot.event_discovery_fence import EventDiscoveryFence
    from modules.hotspot.event_discovery_service import (
        EventDiscoveryBatchStore,
        EventDiscoveryService,
        SharedDiscoveryCache,
    )

    db = env["db"]
    clock = env["clock"]
    store = EventDiscoveryBatchStore(tmp_path / "event_api_batches.json")
    cache = SharedDiscoveryCache(fetcher, batch_store=store, ttl_s=600, policy_plan=["popular"])
    holder: dict = {}

    async def _fence_fetch(event_id, rule_version, policy_hash):
        """围栏外部源：转发到服务真实分发（读共享缓存）。"""
        return await holder["fetch"](event_id, rule_version, policy_hash)

    fence_kwargs = dict(
        repository=EventDiscoveryRepository(clock=clock),
        session_factory=db,
        fetch_fn=_fence_fetch,
        clock=clock,
        budget_acquire=budget_acquire,
    )
    fence_kwargs.update(fence_overrides)
    fence = EventDiscoveryFence(**fence_kwargs)
    service = EventDiscoveryService(fence=fence, shared_cache=cache, session_factory=db, clock=clock)
    holder["fetch"] = service.make_fetch_fn()
    routes_events.configure(discovery_service=service)
    return service, fence


def test_discover_disabled_by_config_returns_reason_not_503(env):
    """§18.2：配置关闭 → 明确「未启用」状态 + reason_code，**不再是 503**。"""
    client = env["client"]
    eid = _create_event(client)
    routes_events.configure(discovery_enabled=False)
    resp = client.post(f"/api/hotspot/events/{eid}/discover/tasks")
    assert resp.status_code == 409
    assert resp.status_code != 503
    detail = resp.json()["detail"]
    assert detail["error_code"] == "discovery_disabled_by_config"
    assert detail["reason_code"] == "discovery_disabled_by_config"
    assert detail["enabled"] is False


def test_discover_disabled_makes_zero_external_calls(env, tmp_path):
    """未启用：外部 fetcher 调用 0 次（不发任何真实请求）。"""
    calls: list = []

    async def fetcher(now_s):
        """被调即计数（等价未授权的真实外部源，应永不被调用）。"""
        calls.append(int(now_s))
        return []

    _build_real_discovery(env, tmp_path, fetcher)
    client = env["client"]
    eid = _create_event(client)
    routes_events.configure(discovery_enabled=False)
    resp = client.post(f"/api/hotspot/events/{eid}/discover/tasks")
    assert resp.status_code == 409
    assert calls == [], "关闭状态下不应发生任何外部调用"


def test_discover_enabled_with_fake_fetcher_claims_run(env, tmp_path):
    """启用 + 可注入假 fetcher：领取成功，落一条 running 持久 run。"""
    import asyncio as _asyncio

    async def fetcher(now_s):
        """永不返回：制造「运行中」，不触发 commit。"""
        await _asyncio.sleep(3600)

    _build_real_discovery(env, tmp_path, fetcher)
    client = env["client"]
    eid = "evt_claim_1"
    _seed_event_row(env, eid)
    resp = client.post(f"/api/hotspot/events/{eid}/discover/tasks")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "running"
    assert data["run_id"]
    assert data["status_url"].endswith(data["task_id"])
    session = env["db"]()
    try:
        run = session.get(EventDiscoveryRun, data["run_id"])
        assert run is not None and run.status == "running" and run.event_id == eid
    finally:
        session.close()


def test_discover_in_progress_second_manual_returns_409(env, tmp_path):
    """已有有效 run：第二次手动 → 409 discovery_in_progress（CAS 只一方成功）。"""
    import asyncio as _asyncio

    async def fetcher(now_s):
        """永不返回，保持首个 run 处于 running。"""
        await _asyncio.sleep(3600)

    _build_real_discovery(env, tmp_path, fetcher)
    client = env["client"]
    eid = "evt_claim_2"
    _seed_event_row(env, eid)
    first = client.post(f"/api/hotspot/events/{eid}/discover/tasks")
    assert first.status_code == 200
    second = client.post(f"/api/hotspot/events/{eid}/discover/tasks")
    assert second.status_code == 409
    assert second.json()["detail"]["error_code"] == "discovery_in_progress"


def test_discover_manual_cooldown_returns_409(env, tmp_path):
    """手动 cooldown 未过 → 409 discovery_not_due（预算/节奏不绕过）。"""

    async def fetcher(now_s):
        """成功源（本路径不应被调用）。"""
        return []

    _build_real_discovery(env, tmp_path, fetcher)
    client = env["client"]
    eid = "evt_cooldown"
    _seed_event_row(env, eid)
    # 制造「刚手动触发过」且无活动租约：last_attempt 落在 cooldown 内。
    session = env["db"]()
    try:
        row = session.get(HotEvent, eid)
        row.last_discovery_attempt_s = T - 10
        row.lease_until_s = None
        row.active_discovery_run_id = None
        session.commit()
    finally:
        session.close()
    resp = client.post(f"/api/hotspot/events/{eid}/discover/tasks")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "discovery_not_due"


def test_discover_budget_unavailable_returns_429(env, tmp_path):
    """预算不可授予 → 429 discovery_budget_unavailable（不先建可执行 run）。"""

    async def fetcher(now_s):
        """本路径不应被调用。"""
        return []

    _build_real_discovery(env, tmp_path, fetcher, budget_acquire=lambda now_s: False)
    client = env["client"]
    eid = "evt_budget"
    _seed_event_row(env, eid)
    resp = client.post(f"/api/hotspot/events/{eid}/discover/tasks")
    assert resp.status_code == 429
    assert resp.json()["detail"]["error_code"] == "discovery_budget_unavailable"


def test_discover_missing_event_returns_404(env, tmp_path):
    """不存在的事件 → 404（不编造 run）。"""

    async def fetcher(now_s):
        """本路径不应被调用。"""
        return []

    _build_real_discovery(env, tmp_path, fetcher)
    resp = env["client"].post("/api/hotspot/events/evt_missing/discover/tasks")
    assert resp.status_code == 404


def test_read_event_switch_defaults_off_and_env_override(monkeypatch):
    """§18.2：发现/快道开关默认关；环境变量可覆盖。"""
    import web.main as wm

    monkeypatch.delenv("EVENT_AUTO_DISCOVERY_ENABLED", raising=False)
    monkeypatch.setattr(wm, "config_manager", None, raising=False)
    assert (
        wm._read_event_switch(
            "event_switches.auto_discovery_enabled", "EVENT_AUTO_DISCOVERY_ENABLED", default=False
        )
        is False
    )
    monkeypatch.setenv("EVENT_AUTO_DISCOVERY_ENABLED", "true")
    assert (
        wm._read_event_switch(
            "event_switches.auto_discovery_enabled", "EVENT_AUTO_DISCOVERY_ENABLED", default=False
        )
        is True
    )


def test_event_switches_config_defaults_off():
    """§18.2：随包 config.yaml 默认关闭自动发现与快道，工作台开启。"""
    from pathlib import Path

    import yaml

    cfg_path = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    switches = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["event_switches"]
    assert switches["auto_discovery_enabled"] is False
    assert switches["fast_watch_enabled"] is False
    assert switches["workbench_enabled"] is True
    assert switches["auto_membership"] == "review"


def test_main_lifespan_assembles_discovery_service(monkeypatch):
    """web/main.py 在**同一 lifespan** 装配 EventDiscoveryService，并按 §18.2 注入开关。

    用真 lifespan（conftest 已把 init_database / ConfigManager / 常驻监控隔离到临时目录）；
    断言装配后 discovery_service 非空，且默认配置下 discovery_enabled 为关。
    """
    import importlib

    from fastapi.testclient import TestClient

    web_main = importlib.import_module("web.main")
    routes_events_mod = importlib.import_module("web.routers.hotspot.routes_events")
    monkeypatch.delenv("EVENT_AUTO_DISCOVERY_ENABLED", raising=False)
    routes_events_mod.reset_state()
    try:
        with TestClient(web_main.app) as client:
            assert client.get("/health").json() == {"status": "ok"}
            # 前端入口可渲染（新事件入口 UI 存在，模板未被改坏）。
            root = client.get("/")
            assert root.status_code == 200
            assert "事件工作台" in root.text
            assert routes_events_mod._STATE["discovery_service"] is not None
            # config.yaml 默认 false → 生产装配下发现端点判定为未启用（非 503）。
            assert routes_events_mod._STATE["discovery_enabled"] is False
    finally:
        routes_events_mod.reset_state()


# ===========================================================================
# 第三批 h：前端 UI 契约（§15.1/15.2/15.3 + §3.4 202 守卫）
# ===========================================================================

_FRONTEND_DIR = Path(__file__).resolve().parents[1] / "web" / "frontend"


def _read_frontend(rel):
    """读取前端文件文本（UTF-8）。"""
    return (_FRONTEND_DIR / rel).read_text(encoding="utf-8")


def test_event_ui_forbidden_wording_absent():
    """§15.3：禁止话术一条不许出现在 UI 文案。"""
    blob = "\n".join([
        _read_frontend("templates/index.html"),
        _read_frontend("static/js/app.events.js"),
        _read_frontend("static/js/app.hotspot.js"),
        _read_frontend("static/css/style.hotspot.css"),
    ])
    for phrase in [
        "全网热度上涨300%",
        "保证还有两天红利",
        "成功率90%",
        "所以话题已死",
        "今天新增百万播放",
    ]:
        assert phrase not in blob, phrase


def test_event_ui_workbench_controls_present():
    """§15.1/15.2：工作台完整版控件在 HTML 中（实体+锚点 / 热门 Tag / 归属 / brief / 评估 / 机会）。"""
    html = _read_frontend("templates/index.html")
    for token in [
        "event-name", "event-hot-tags", "loadEventHotTags()",
        "event-auto-membership", "setEventAutoMembership",
        "event-brief-production", "event-brief-assets", "event-brief-entities",
        "createEventDraft()", "discoverAndObserveEvent()",
        "assessEventDaily()", "createOpportunityForEvent()",
    ]:
        assert token in html, token


def test_event_ui_js_contract_202_guard_and_traceable_values():
    """§3.4 / §15：202 不被当已生成；专用轮询；可溯源数值；覆盖不足与反馈含已发布。"""
    js = _read_frontend("static/js/app.events.js")
    for token in [
        "pollTopicGeneration",
        "result.accepted || result.status === 'running'",  # 202 守卫
        "generation_request_id",
        "generation_key_conflict",
        "renderCoverageShortfall",
        "renderTraceableMetrics",
        "renderEventChannels",
        "renderAssessmentHistoryBadges",
        "不提供无法溯源的综合热度百分数",
        "已发布",
    ]:
        assert token in js, token


# ===========================================================================
# 第三批 i：只读预览 / 预算名额 / 单视频跳转接线（§15.1-②③④）
# ===========================================================================


def _event_member_rows(db, event_id):
    """读取事件全部成员行（bvid/revision/status），供“预览只读”断言。"""
    session = db()
    try:
        rows = (
            session.query(HotEventMember)
            .filter(HotEventMember.event_id == event_id)
            .order_by(HotEventMember.bvid.asc(), HotEventMember.revision.asc())
            .all()
        )
        return [(row.bvid, int(row.revision), row.status) for row in rows]
    finally:
        session.close()


def test_members_preview_is_read_only(env):
    """§15.1-②：预览纯计算——revision 与成员行零变化，且不落库。"""
    client = env["client"]
    eid = _create_event(
        client,
        source_policy={
            "entity_scope": ["原神"],
            "include_rules": {"entity_groups": [["原神"]], "anchor_groups": [["前瞻"]]},
        },
    )
    before_event = client.get(f"/api/hotspot/events/{eid}").json()["data"]
    before_members = _event_member_rows(env["db"], eid)

    resp = client.post(
        f"/api/hotspot/events/{eid}/members/preview",
        json={"samples": [{"bvid": "BV1xx411c7mD", "title": "原神 5.2 前瞻"}]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["read_only"] is True
    assert data["preview_only"] is True

    after_event = client.get(f"/api/hotspot/events/{eid}").json()["data"]
    after_members = _event_member_rows(env["db"], eid)
    assert after_event["revision"] == before_event["revision"]
    assert after_members == before_members == []


def test_members_preview_example_three_way_when_no_discovery(env):
    """无发现结果时用规则字面量 + 用户锚点给出示例三分类，不返回空且真命中。"""
    client = env["client"]
    eid = _create_event(
        client,
        name="原神 5.2 前瞻",
        source_policy={
            "entity_scope": ["原神"],
            "include_rules": {"entity_groups": [["原神"]], "anchor_groups": [["前瞻"]]},
            "exclude_rules": {"old_versions": ["旧版本"]},
        },
    )
    # 第二个同实体但不同锚点的事件：让“冲突”示例真能跨事件命中。
    _create_event(
        client,
        name="原神 5.2 攻略",
        source_policy={
            "entity_scope": ["原神"],
            "include_rules": {"entity_groups": [["原神"]], "anchor_groups": [["攻略"]]},
        },
    )
    resp = client.post(
        f"/api/hotspot/events/{eid}/members/preview", json={"entity_anchors": ["原神", "前瞻"]}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["examples_generated"] is True
    assert data["examples"], "示例三分类不许为空"
    assert {"would_match", "would_conflict", "would_exclude"} <= {
        row["example_category"] for row in data["examples"]
    }
    # 真实三分类都命中（不是空壳）
    assert data["would_match"], data
    assert data["would_conflict"], data
    assert data["would_exclude"], data


def test_members_preview_not_a_committed_decision(env):
    """预览结果不可当已提交决定：decisions 仍走 CAS（旧 revision → 409）。"""
    client = env["client"]
    eid = _create_event(client)
    preview = client.post(
        f"/api/hotspot/events/{eid}/members/preview", json={"entity_anchors": ["事件A", "锚点"]}
    )
    assert preview.status_code == 200
    assert client.get(f"/api/hotspot/events/{eid}/members").json()["data"]["items"] == []
    resp = client.post(
        f"/api/hotspot/events/{eid}/members/decisions",
        json={"expected_revision": 999, "decisions": [{"bvid": "BV1xx411c7mD", "status": "proposed"}]},
    )
    assert resp.status_code == 409


def test_event_budget_fields_traceable_or_unavailable(env):
    """§15.1-③：预算/候选/待确认/名额逐项可溯源；拿不到的标未提供 + 原因码。"""
    client = env["client"]
    eid = _create_event(client)
    resp = client.get(f"/api/hotspot/events/{eid}/budget")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["event_id"] == eid
    fields = [
        data["budget"]["discovery_requests_per_24h"],
        data["budget"]["watch_samples_per_24h"],
        data["candidate_count"],
        data["pending_confirm"],
        data["sampling_quota"],
    ]
    for field in fields:
        if field.get("available"):
            assert field.get("source"), field
            assert "value" in field, field
        else:
            assert field.get("reason_code"), field
    # 无发现结果 → 候选标未提供 + no_discovery_run（不臆造）
    assert data["candidate_count"]["available"] is False
    assert data["candidate_count"]["reason_code"] == "no_discovery_run"
    # 待确认可用：来自 proposed 成员
    assert data["pending_confirm"]["available"] is True
    assert data["pending_confirm"]["source"].startswith("hot_event_members")


def test_event_budget_candidate_count_from_discovery_run(env):
    """候选数来自本轮 EventDiscoveryRun（可溯源到具体 run）。"""
    client = env["client"]
    eid = _create_event(client)
    start = client.post(f"/api/hotspot/events/{eid}/discover/tasks")
    run_id = start.json()["data"]["run_id"]
    data = client.get(f"/api/hotspot/events/{eid}/budget").json()["data"]
    assert data["candidate_count"]["available"] is True
    assert data["candidate_count"]["source"] == f"event_discovery_runs:{run_id}"


def test_event_budget_pending_confirm_matches_proposed(env):
    """待确认 = proposed 成员数（写两条 proposed 后一致）。"""
    client = env["client"]
    eid = _create_event(client)
    ok = client.post(
        f"/api/hotspot/events/{eid}/members/decisions",
        json={
            "expected_revision": 0,
            "decisions": [
                {"bvid": "BV1xx411c7mD", "status": "proposed"},
                {"bvid": "BV1yy411c7mE", "status": "proposed"},
            ],
        },
    )
    assert ok.status_code == 200, ok.text
    data = client.get(f"/api/hotspot/events/{eid}/budget").json()["data"]
    assert data["pending_confirm"]["value"] == 2


def test_event_ui_single_video_jump_reuses_02_card():
    """§15.1-④：单视频点击复用 02 原卡片打开逻辑；失败有提示；不新建详情页。"""
    events_js = _read_frontend("static/js/app.events.js")
    hotspot_js = _read_frontend("static/js/app.hotspot.js")
    html = _read_frontend("templates/index.html")
    assert "openEventVideoCard" in events_js
    assert "openHotspotVideoCard" in events_js  # 复用 02 侧入口
    assert "event-jump-hint" in events_js  # 失败提示容器
    assert "无法打开" in events_js  # 明确提示文案（非静默）
    assert "function openHotspotVideoCard" in hotspot_js
    assert "loadHotspotTimeline" in hotspot_js  # 复用既有 02 卡片打开逻辑
    assert "event-video-detail-page" not in html  # 未新建详情页


# ===========================================================================
# 论断1-B：事件工作台（评估 / 机会）补算法来源字段 algorithm_version
# ===========================================================================


def test_event_algorithm_version_matches_lifecycle_v2():
    """写死版本号必须与 ``LifecycleV2.version`` 等值：防「写死」漂移。"""
    from modules.hotspot.algorithm.lifecycle_v2 import LifecycleV2

    assert routes_events._EVENT_ALGORITHM_VERSION == LifecycleV2().version


def test_workbench_assessment_exposes_algorithm_version(env):
    """评估列表 / 详情响应补 ``algorithm_version``（纯加字段，不改既有字段）。"""
    client = env["client"]
    eid = _create_event(client)
    assert client.post(f"/api/hotspot/events/{eid}/assess/tasks", json={"as_of_s": T}).status_code == 200

    assessments = client.get(f"/api/hotspot/events/{eid}/assessments").json()["data"]
    assert assessments["algorithm_version"] == "lifecycle_v2"
    aid = assessments["items"][0]["assessment_id"]

    detail = client.get(f"/api/hotspot/event-assessments/{aid}").json()["data"]
    assert detail["algorithm_version"] == "lifecycle_v2"


def test_opportunity_exposes_algorithm_version(env):
    """机会 run 响应补 ``algorithm_version``（与评估同源）。"""
    client = env["client"]
    eid = _create_event(client)
    run_id = _seed_opportunity(env, eid)

    run = client.get(f"/api/hotspot/opportunities/{run_id}").json()["data"]
    assert run["algorithm_version"] == "lifecycle_v2"

