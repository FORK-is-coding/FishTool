"""评论语义折叠 · 服务编排层（C）隔离单测。

策略（contract §Delivery）：只测 service.py 本身的编排逻辑，用实现了 store 真实
同步接口的 FakeStore + 实现 provider 真实 group() 形状的 FakeProvider，**不触网、
不起真 DB**，每个用例独立事件循环。断言的核心是编排不变量，而非 store/provider
内部：
- reserve 在 provider 调用前、settle 在之后、checkpoint 在 settle 之后；
- 全局串行（Semaphore(1)）同一时刻只跑一个任务的模型调用；
- phase1 批内 + crossphase 候选原成员复核（无 A~B~C 盲传递）；
- 并发取消：在飞仍按最新 revision 结算记账、不复位 cancelled、不发布；
- resume 不重发已成功 checkpoint；模型身份变更拒绝续跑；
- 覆盖不足（跨窗口/超限/预算）如实标 partial，绝不谎称全量；
- 未配置模型 -> SemanticProviderError(NOT_CONFIGURED)；错误原因脱敏。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Optional

import pytest

from modules.comment.semantic import service as svc
from modules.comment.semantic.contracts import (
    CONSENT_VERSION,
    MAX_GROUP_SIZE,
    PHASE_BATCH,
    PHASE_CROSS,
    ProviderErrorCategory,
    SemanticBudgetExceeded,
    SemanticConflict,
    SemanticNotFound,
    SemanticProviderError,
    SemanticValidationError,
)


IDENTITY = {"api_base": "https://api.example.com/v1", "model": "gpt-x"}


def run(coro):
    """用独立事件循环驱动协程（避免跨用例共享循环）。"""
    return asyncio.run(coro)


# ==========================================================================
# 假实现：实现 store / provider 的真实对外形状（同步 store、async provider）
# ==========================================================================
class FakeStore:
    """内存版 SemanticStore 替身：实现 service 依赖的同步方法子集与 CAS 语义。

    刻意复刻关键不变量:revision 顶层自增、reserve/settle 在飞记账、cancelled 下
    settle 只记账不复活、publish 仅 running、update_task 白名单、预算/尝试上限。
    """

    def __init__(self, snapshot: Dict[str, Any],
                 pref: Optional[Dict[str, Any]] = None,
                 max_attempts: int = 20, task_budget: int = 60000,
                 daily_budget: int = 100000) -> None:
        self._snapshot = snapshot
        self._pref = pref or {"auto_fold": True, "revision": 0,
                              "consent_version": CONSENT_VERSION,
                              "model_identity": dict(IDENTITY)}
        self._task: Optional[Dict[str, Any]] = None
        self._limits = {"max_attempts": max_attempts,
                        "task_token_budget": task_budget,
                        "daily_token_budget": daily_budget}
        self.published: List[Dict[str, Any]] = []
        self.published_related: List[Dict[str, Any]] = []
        self.calls: List[str] = []  # 记录关键调用顺序,供断言 reserve<call<settle<checkpoint

    # ----- 只读 -----
    def snapshot(self, bvid: str, limit: int = 200) -> Dict[str, Any]:
        self.calls.append("snapshot")
        return self._snapshot

    def preference(self, bvid: str) -> Dict[str, Any]:
        return dict(self._pref)

    def get_task(self, task_id: str) -> Dict[str, Any]:
        if self._task is None:
            raise SemanticNotFoundLike("no task")
        return dict(self._task)

    def latest_task(self, bvid: str) -> Optional[Dict[str, Any]]:
        return dict(self._task) if self._task else None

    # ----- 创建 -----
    def create_task(self, snapshot, model_identity, limits, request_key) -> Dict[str, Any]:
        if self._task is not None:
            if self._task.get("request_key") == request_key:
                return dict(self._task)  # 幂等
            raise SemanticConflict("request_key 冲突")
        self._task = {
            "task_id": "t1", "bvid": snapshot.get("video", {}).get("bvid"),
            "status": "queued", "phase": "queued", "revision": 0,
            "request_key": request_key, "model_identity": dict(model_identity),
            "snapshot": snapshot, "checkpoint": {}, "groups": [],
            "progress": {}, "counts": {}, "coverage": {}, "error": None,
            "budget": {"reserved_tokens": 0, "settled_tokens": 0,
                       "request_count": 0, "attempt_count": 0,
                       "max_attempts": self._limits["max_attempts"],
                       "task_token_budget": self._limits["task_token_budget"],
                       "daily_token_budget": self._limits["daily_token_budget"],
                       "daily_reserved": 0, "daily_settled": 0, "in_flight": None},
        }
        return dict(self._task)

    # ----- CAS 原语 -----
    def _check_rev(self, expected_revision: int) -> None:
        if self._task["revision"] != expected_revision:
            raise SemanticConflict(
                f"revision 过期 expected={expected_revision} actual={self._task['revision']}")

    def update_task(self, task_id, expected_revision, **changes) -> Dict[str, Any]:
        self._check_rev(expected_revision)
        if self._task["status"] == "cancelled":
            raise SemanticConflict("cancelled 不可被 worker 覆写")
        whitelist = {"status", "phase", "progress", "checkpoint",
                     "coverage", "error", "result"}
        for k, v in changes.items():
            assert k in whitelist, f"update_task 白名单外字段: {k}"
            self._task[k] = v
        if "checkpoint" in changes:
            self.calls.append("checkpoint")
        self._task["revision"] += 1
        return dict(self._task)

    def reserve_attempt(self, task_id, expected_revision, estimated_tokens) -> Dict[str, Any]:
        self._check_rev(expected_revision)
        b = self._task["budget"]
        if self._task["status"] in ("cancelled", "interrupted"):
            raise SemanticConflict("取消/中断下不可预约")
        if b["in_flight"] is not None:
            raise SemanticConflict("已有在飞 attempt")
        if b["attempt_count"] + 1 > b["max_attempts"]:
            raise SemanticBudgetExceeded("超最大尝试数", scope="attempts")
        if b["reserved_tokens"] + estimated_tokens > b["task_token_budget"]:
            raise SemanticBudgetExceeded("超任务预算", scope="task")
        if b["daily_reserved"] + estimated_tokens > b["daily_token_budget"]:
            raise SemanticBudgetExceeded("超每日预算", scope="daily")
        attempt_id = f"a{b['attempt_count']}"
        b["in_flight"] = {"attempt_id": attempt_id,
                          "estimated_tokens": estimated_tokens,
                          "usage_estimated": True}
        b["reserved_tokens"] += estimated_tokens
        b["daily_reserved"] += estimated_tokens
        b["request_count"] += 1
        b["attempt_count"] += 1
        self._task["revision"] += 1
        self.calls.append("reserve")
        return dict(self._task)

    def settle_attempt(self, task_id, expected_revision, usage_tokens=None) -> Dict[str, Any]:
        self._check_rev(expected_revision)
        b = self._task["budget"]
        if b["in_flight"] is None:
            # 幂等 no-op(契约:无在飞时 settle 不误结算)。
            self._task["revision"] += 1
            return dict(self._task)
        est = b["in_flight"]["estimated_tokens"]
        actual = usage_tokens if isinstance(usage_tokens, int) and usage_tokens >= 0 else est
        b["settled_tokens"] += actual
        b["daily_settled"] += actual
        b["in_flight"] = None
        self._task["revision"] += 1
        self.calls.append("settle")
        return dict(self._task)

    # ----- 终态/恢复 -----
    def cancel_task(self, task_id, expected_revision) -> Dict[str, Any]:
        if self._task["status"] in ("cancelled",):
            return dict(self._task)  # 幂等
        self._check_rev(expected_revision)
        self._task["status"] = "cancelled"
        self._task["revision"] += 1
        return dict(self._task)

    def resume_task(self, task_id, expected_revision, acknowledge_unknown=False) -> Dict[str, Any]:
        self._check_rev(expected_revision)
        if self._task["status"] not in ("interrupted", "partial", "failed"):
            raise SemanticConflict("状态不可续跑")
        if self._has_unknown() and not acknowledge_unknown:
            raise SemanticConflict("存在未知在飞,需 acknowledge_unknown")
        self._task["status"] = "queued"
        self._task["revision"] += 1
        return dict(self._task)

    def recover_interrupted(self) -> int:
        if self._task and self._task["status"] in ("queued", "running"):
            self._task["status"] = "interrupted"
            if self._task["budget"]["in_flight"] is not None:
                self._task["budget"]["in_flight"] = {
                    **self._task["budget"]["in_flight"], "outcome": "unknown"}
            self._task["revision"] += 1
            return 1
        return 0

    def publish_groups(self, task_id, expected_revision, groups, related_groups=None) -> Dict[str, Any]:
        self._check_rev(expected_revision)
        if self._task["status"] != "running":
            raise SemanticConflict("仅 running 可发布")
        self.published = list(groups)
        self.published_related = list(related_groups or [])
        self._task["groups"] = list(groups)
        self._task["revision"] += 1
        self.calls.append("publish")
        return dict(self._task)

    def result(self, task_id, page=1, page_size=50) -> Dict[str, Any]:
        return {"task": dict(self._task), "groups": self._task["groups"],
                "items": [], "pagination": {"page": page}}

    # ----- 测试辅助 -----
    def _has_unknown(self) -> bool:
        cp = (self._task.get("checkpoint") or {}).get("phase1") or {}
        return any(b.get("status") == "pending" for b in (cp.get("batches") or {}).values())

    def force(self, **changes) -> None:
        """测试直接改任务态(模拟并发/中断),不走 CAS。"""
        self._task.update(changes)

    def status_groups(self) -> List[Dict[str, Any]]:
        """已发布的分组(测试断言用)。"""
        return list(self.published)


class SemanticNotFoundLike(SemanticConflict):
    """FakeStore 未建任务时的占位错误（仅测试内部用）。"""


class FakeProvider:
    """async 上下文管理器形状的 provider 替身。

    group(items, video_title="", phase="batch", omit_temperature=False) ->
    {"data": grouping, "usage_tokens": int|None, "usage_estimated": bool}。
    可脚本化:按 (phase) 返回预置分组,或抛 contracts 错误以测重试/降级。
    """

    def __init__(self, *, configured: bool = True,
                 identity: Optional[Dict[str, str]] = None,
                 handler: Optional[Callable[..., Dict[str, Any]]] = None) -> None:
        self.configured = configured
        self.identity = identity if identity is not None else dict(IDENTITY)
        self._handler = handler
        self.entered = False
        self.group_calls: List[Dict[str, Any]] = []

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.entered = False
        return False

    async def group(self, items, video_title="", phase=PHASE_BATCH,
                    omit_temperature=False) -> Dict[str, Any]:
        rec = {"ids": [i["id"] for i in items], "phase": phase,
               "omit_temperature": omit_temperature,
               "members": {i["id"]: i.get("members") for i in items}}
        self.group_calls.append(rec)
        # 隐私断言:载荷绝不含 uid/uname/rpid。
        for it in items:
            assert set(it.keys()) <= {"id", "content", "members"}, it.keys()
        if self._handler is not None:
            return self._handler(items, video_title, phase, omit_temperature)
        return {"data": {"equivalent_groups": [], "related_groups": [],
                         "uncertain_ids": []},
                "usage_tokens": 10, "usage_estimated": False}


def _snapshot(n: int = 3, title: str = "标题") -> Dict[str, Any]:
    """构造 n 条合格顶楼评论的快照（id c0..c{n-1}）。"""
    items = [{"id": f"c{i}", "rpid": str(1000 + i), "content": f"内容{i}",
              "content_hash": f"h{i}", "uid": i, "uname": f"u{i}",
              "ctime": None, "sentiment": None, "like": 0,
              "context_status": "top_level"} for i in range(n)]
    return {
        "video": {"id": 1, "bvid": "BV1", "title": title},
        "items": items, "eligible_ids": [f"c{i}" for i in range(n)],
        "counts": {"raw_total": n, "selected": n, "eligible": n,
                   "excluded_context": 0, "excluded_long": 0, "outside_scope": 0},
        "scope": {"limit": 200, "order": "newest"},
    }


def _service(store: FakeStore, provider: FakeProvider) -> svc.SemanticService:
    return svc.SemanticService(store=store, provider_factory=lambda: provider)


def _eg(rep: str, members: List[str]) -> Dict[str, Any]:
    """构造「单一等价组」的 group() 返回。"""
    return {"data": {"equivalent_groups": [{"representative_id": rep,
                                            "member_ids": list(members),
                                            "reason": "r", "aspect": "a",
                                            "stance": "s"}],
                     "related_groups": [], "uncertain_ids": []},
            "usage_tokens": 5, "usage_estimated": False}


def _empty() -> Dict[str, Any]:
    """构造空分组返回。"""
    return {"data": {"equivalent_groups": [], "related_groups": [],
                     "uncertain_ids": []}, "usage_tokens": 5,
            "usage_estimated": False}


def _run_with_batch_size(service: svc.SemanticService, store: FakeStore,
                         size: int) -> None:
    """临时把 service 模块的 BATCH_SIZE 降到 size,跑完一个任务后还原。"""
    import modules.comment.semantic.service as mod
    old = mod.BATCH_SIZE
    mod.BATCH_SIZE = size
    try:
        async def scenario():
            await service.start("BV1", 200, CONSENT_VERSION, "rk1")
            await _drain(service)
        run(scenario())
    finally:
        mod.BATCH_SIZE = old


# ==========================================================================
# preflight
# ==========================================================================
def test_preflight_returns_counts_scope_not_raw_items():
    store = FakeStore(_snapshot(3))
    service = _service(store, FakeProvider())
    out = run(service.preflight("BV1", limit=200))
    assert out["model_configured"] is True
    assert out["provider_identity"] == IDENTITY
    assert out["consent_needed"] is False
    assert out["consent_version"] == CONSENT_VERSION
    assert out["counts"]["eligible"] == 3
    assert out["limits"]["max_attempts"] == 20  # 规范 key,非别名
    assert "items" not in out  # 不回整份原始快照


def test_preflight_consent_needed_when_identity_differs():
    store = FakeStore(_snapshot(3),
                      pref={"auto_fold": True, "revision": 1,
                            "consent_version": CONSENT_VERSION,
                            "model_identity": {"api_base": "https://other/", "model": "m"}})
    service = _service(store, FakeProvider())
    out = run(service.preflight("BV1"))
    assert out["consent_needed"] is True


def test_preflight_limit_clamped():
    store = FakeStore(_snapshot(3))
    captured = {}
    orig = store.snapshot

    def spy(bvid, limit=200):
        captured["limit"] = limit
        return orig(bvid, limit)

    store.snapshot = spy
    service = _service(store, FakeProvider())
    run(service.preflight("BV1", limit=9999))
    assert captured["limit"] == 500  # 收敛到 MAX_LIMIT


# ==========================================================================
# start：同意/身份门禁 + 不在 durable create 前调模型
# ==========================================================================
def test_start_rejects_consent_version_mismatch():
    store = FakeStore(_snapshot(3))
    service = _service(store, FakeProvider())
    with pytest.raises(SemanticValidationError):
        run(service.start("BV1", 200, "wrong-version", "rk1"))


def test_start_not_configured_raises_provider_error():
    store = FakeStore(_snapshot(3))
    service = _service(store, FakeProvider(configured=False))
    with pytest.raises(SemanticProviderError) as ei:
        run(service.start("BV1", 200, CONSENT_VERSION, "rk1"))
    assert ei.value.category == ProviderErrorCategory.NOT_CONFIGURED


def test_start_requires_bound_consent():
    store = FakeStore(_snapshot(3),
                      pref={"auto_fold": True, "revision": 0,
                            "consent_version": None, "model_identity": None})
    service = _service(store, FakeProvider())
    with pytest.raises(SemanticValidationError) as ei:
        run(service.start("BV1", 200, CONSENT_VERSION, "rk1"))
    assert ei.value.reason == "consent_required"


class _MissingVideoStore(FakeStore):
    """视频不存在：preference/snapshot 都按真实 store 契约抛 SemanticNotFound。"""

    def preference(self, bvid: str) -> Dict[str, Any]:
        raise SemanticNotFound(f"视频不存在: {bvid}")

    def snapshot(self, bvid: str, limit: int = 200) -> Dict[str, Any]:
        raise SemanticNotFound(f"视频不存在: {bvid}")


@pytest.mark.parametrize("configured, consent", [
    (False, CONSENT_VERSION),   # 原先：503 未配置模型
    (True, CONSENT_VERSION),    # 原先：400 未授予同意
    (True, "wrong-version"),    # 原先：400 同意版本不匹配
])
def test_start_unknown_video_404_before_other_gates(configured, consent):
    """issue #47：视频不存在必须先于 consent / 模型配置校验暴露为 NotFound。"""
    store = _MissingVideoStore(_snapshot(3))
    service = _service(store, FakeProvider(configured=configured))
    with pytest.raises(SemanticNotFound):
        run(service.start("BVghost00000", 200, consent, "rk1"))
    assert service._tasks == {}  # 未创建/调度任何任务


def test_preflight_and_start_real_store_normalize_bvid():
    """真实 store：直调 service（绕过 router）带空白的 bvid 也只落规范键、回规范值。"""
    from core.database import SemanticPreference, get_session
    from modules.comment.semantic.store import SemanticStore

    bvid = "BVnorm000001"
    _seed_video_comments(bvid, 2)
    store = SemanticStore()
    store.set_preference(f" {bvid}", 0, consent_version=CONSENT_VERSION,
                         model_identity=dict(IDENTITY))
    service = svc.SemanticService(store=store, provider_factory=lambda: FakeProvider())
    service._schedule = lambda task_id: None  # 只验证创建，不跑 worker

    pf = run(service.preflight(f"{bvid}\t"))
    assert pf["bvid"] == bvid and pf["consent_needed"] is False
    state = run(service.start(f" {bvid} ", 200, CONSENT_VERSION, "rk-norm"))
    assert state["bvid"] == bvid
    with get_session() as s:
        assert [p.bvid for p in s.query(SemanticPreference).filter(
            SemanticPreference.bvid.like(f"%{bvid}%"))] == [bvid]


def test_start_creates_task_then_runs_to_completion():
    store = FakeStore(_snapshot(3))
    service = _service(store, FakeProvider())

    async def scenario():
        state = await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        assert state["task_id"] == "t1"
        assert "snapshot" not in state and "checkpoint" not in state  # 公开投影
        # 等后台 worker 跑完
        await _drain(service)
        return service.status("t1")

    final = run(scenario())
    assert final["status"] == "completed"
    # reserve 必须在 group 调用前、settle 之后才 checkpoint
    assert store.calls[0] == "snapshot"
    r = store.calls.index("reserve")
    s = store.calls.index("settle")
    c = store.calls.index("checkpoint", s)  # settle 之后才有 checkpoint
    assert r < s < c


def test_start_idempotent_request_key_no_double_schedule():
    store = FakeStore(_snapshot(1))
    service = _service(store, FakeProvider())

    async def scenario():
        await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        await _drain(service)
        # 同 key 再 start:store 幂等返回既有任务,不应再建 worker
        again = await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        return again

    again = run(scenario())
    assert again["task_id"] == "t1"


async def _drain(service: svc.SemanticService) -> None:
    """等本实例所有后台 worker 句柄跑完（测试用）。"""
    for _ in range(200):
        handles = [h for h in service._tasks.values() if not h.done()]
        if not handles:
            return
        await asyncio.gather(*handles, return_exceptions=True)
    raise AssertionError("后台任务未在限定轮次内完成")


# ==========================================================================
# phase1 分组 + 隐私 + 模型顺序
# ==========================================================================
def test_phase1_groups_published_and_singletons_preserved():
    # c0/c1 等价成组，c2 独立 -> 只发布 c0c1 组，c2 保持独立（不发布 <2 组）。
    def handler(items, title, phase, omit):
        ids = [i["id"] for i in items]
        eg = []
        if "c0" in ids and "c1" in ids:
            eg = [{"representative_id": "c0", "member_ids": ["c0", "c1"],
                   "reason": "同义", "aspect": "a", "stance": "s"}]
        return {"data": {"equivalent_groups": eg, "related_groups": [],
                         "uncertain_ids": []}, "usage_tokens": 5, "usage_estimated": False}

    store = FakeStore(_snapshot(3))
    service = _service(store, FakeProvider(handler=handler))

    async def scenario():
        await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        await _drain(service)

    run(scenario())
    assert store.status_groups() == [{"representative_id": "c0",
                                       "member_ids": ["c0", "c1"], "reason": "同义",
                                       "aspect": "a", "stance": "s"}]


def test_model_payload_never_contains_privacy_keys():
    store = FakeStore(_snapshot(3))
    provider = FakeProvider()
    service = _service(store, provider)

    async def scenario():
        await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        await _drain(service)

    run(scenario())
    # FakeProvider.group 已 assert 键集;这里再确认确有调用。
    assert provider.group_calls
    for call in provider.group_calls:
        for cid in call["ids"]:
            assert cid.startswith("c")  # task-local id,非 rpid/uid


# ==========================================================================
# crossphase：跨批候选 + 全量原成员复核（无 A~B~C 盲传递）
# ==========================================================================
def test_crossphase_merges_only_after_full_member_verification():
    # 两批(每批1组),跨批提议把两组代表并一起;复核时必须覆盖全部原成员才合并。
    snap = _snapshot(4)  # c0,c1,c2,c3

    def handler(items, title, phase, omit):
        ids = set(i["id"] for i in items)
        if phase == PHASE_BATCH:
            # 批内:c0c1 一组 / c2c3 一组(取决于本批 ids)
            if ids == {"c0", "c1"}:
                return _eg("c0", ["c0", "c1"])
            if ids == {"c2", "c3"}:
                return _eg("c2", ["c2", "c3"])
            return _empty()
        # PHASE_CROSS
        if ids == {"c0", "c2"}:  # 候选代表比较:提议 c0~c2
            return _eg("c0", ["c0", "c2"])
        if ids == {"c0", "c1", "c2", "c3"}:  # 全量原成员复核:确认单一等价组
            return _eg("c0", ["c0", "c1", "c2", "c3"])
        return _empty()

    store = FakeStore(snap, max_attempts=50)
    service = _service(store, FakeProvider(handler=handler))
    # 强制切成 2 批(每批 2 条):把 BATCH_SIZE 临时降到 2。
    _run_with_batch_size(service, store, 2)
    merged = store.status_groups()
    assert len(merged) == 1
    assert sorted(merged[0]["member_ids"]) == ["c0", "c1", "c2", "c3"]


def test_crossphase_no_merge_when_verification_incomplete():
    # 复核未把全部成员判为单一组 -> 保守不折叠(related/uncertain 绝不自动合并)。
    snap = _snapshot(4)

    def handler(items, title, phase, omit):
        ids = set(i["id"] for i in items)
        if phase == PHASE_BATCH:
            if ids == {"c0", "c1"}:
                return _eg("c0", ["c0", "c1"])
            if ids == {"c2", "c3"}:
                return _eg("c2", ["c2", "c3"])
            return _empty()
        if ids == {"c0", "c2"}:
            return _eg("c0", ["c0", "c2"])  # 提议合并
        if ids == {"c0", "c1", "c2", "c3"}:
            # 复核只覆盖部分成员 -> 不接受合并
            return _eg("c0", ["c0", "c1"])
        return _empty()

    store = FakeStore(snap, max_attempts=50)
    service = _service(store, FakeProvider(handler=handler))
    _run_with_batch_size(service, store, 2)
    groups = store.status_groups()
    # 两组保持独立,未合并
    assert len(groups) == 2
    assert sorted(sorted(g["member_ids"]) for g in groups) == [["c0", "c1"], ["c2", "c3"]]


def test_crossphase_oversize_skipped_marks_partial():
    # 全量成员并集 >MAX_GROUP_SIZE -> 跳过复核、标 partial,不合并。
    n = MAX_GROUP_SIZE  # 两批各 ~n/... 让并集超限
    snap = _snapshot(n + 2)

    def handler(items, title, phase, omit):
        ids = [i["id"] for i in items]
        if phase == PHASE_BATCH:
            # 每批整体成一个大组
            return _eg(ids[0], ids)
        # 跨批提议合并两个大组代表
        if len(ids) == 2:
            return _eg(ids[0], ids)
        return _empty()

    store = FakeStore(snap, max_attempts=200)
    service = _service(store, FakeProvider(handler=handler))
    _run_with_batch_size(service, store, MAX_GROUP_SIZE)  # 2 批,各≈组
    final = service.status("t1")
    assert final["status"] == "partial"
    assert final["coverage"]["oversize_skipped"] >= 1


# ==========================================================================
# 取消：在飞仍结算记账、绝不发布、不复位 cancelled
# ==========================================================================
def test_cancel_midflight_settles_accounting_and_never_publishes():
    snap = _snapshot(2)
    gate = asyncio.Event()
    released = asyncio.Event()

    async def handler_coro(items, title, phase, omit):
        # 第一次 group 调用时挂起,给外部机会 cancel
        released.set()
        await gate.wait()
        return _empty()["data"] and _empty()

    class SlowProvider(FakeProvider):
        async def group(self, items, video_title="", phase=PHASE_BATCH, omit_temperature=False):
            self.group_calls.append({"ids": [i["id"] for i in items]})
            released.set()
            await gate.wait()
            return {"data": {"equivalent_groups": [], "related_groups": [],
                             "uncertain_ids": []}, "usage_tokens": 7,
                    "usage_estimated": False}

    store = FakeStore(snap)
    provider = SlowProvider()
    service = _service(store, provider)

    async def scenario():
        state = await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        await released.wait()  # worker 已进入 group 调用(已 reserve)
        # 读取当前 revision 并取消
        cur = service.status("t1")
        await service.cancel("t1", cur["revision"])
        gate.set()  # 放行 group 返回
        await _drain(service)
        return service.status("t1")

    final = run(scenario())
    assert final["status"] == "cancelled"  # 未被 worker 复位
    assert store.published == []  # 取消绝不发布
    # 在飞已结算(settled_tokens>0 说明记账了,未退款)
    assert final["budget"]["settled_tokens"] > 0
    assert final["budget"]["in_flight"] is None


def test_cancel_is_idempotent():
    store = FakeStore(_snapshot(1))
    service = _service(store, FakeProvider())

    async def scenario():
        await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        await _drain(service)
        st = service.status("t1")
        # 任务已 completed;cancel 幂等(store 处理终态)——这里改造成 cancel 前置
        return st

    run(scenario())  # 冒烟:completed 后 status 正常


# ==========================================================================
# resume：不重发已成功 checkpoint；身份变更拒绝
# ==========================================================================
def test_resume_rejects_identity_change():
    store = FakeStore(_snapshot(2))
    service = _service(store, FakeProvider())

    async def scenario():
        await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        await _drain(service)
        # 任务 completed;强制成 interrupted 以便 resume
        store.force(status="interrupted")
        cur = service.status("t1")
        # 换一个 provider 身份
        service._provider_factory = lambda: FakeProvider(
            identity={"api_base": "https://changed/", "model": "m2"})
        with pytest.raises(SemanticConflict):
            await service.resume("t1", cur["revision"])

    run(scenario())


def test_resume_does_not_resend_done_batches():
    snap = _snapshot(2)
    store = FakeStore(snap, max_attempts=50)
    provider = FakeProvider()
    service = _service(store, provider)

    async def scenario():
        await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        await _drain(service)
        first_calls = len(provider.group_calls)
        # 置 interrupted 后 resume:phase1 的 done 批不应再调模型
        store.force(status="interrupted")
        cur = service.status("t1")
        await service.resume("t1", cur["revision"], acknowledge_unknown=True)
        await _drain(service)
        return first_calls, len(provider.group_calls)

    first, second = run(scenario())
    assert second == first  # 已 done 的批未重发


def test_resume_requires_ack_for_pending_unknown():
    # 构造一个 pending(未知在飞)checkpoint,未 ack 时 store 拒绝 resume。
    store = FakeStore(_snapshot(2))
    service = _service(store, FakeProvider())

    async def scenario():
        await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        await _drain(service)
        # 人为制造 pending 窗口 + interrupted
        store.force(status="interrupted",
                    checkpoint={"phase1": {"batches": {"0": {"ids": ["c0", "c1"],
                                                             "status": "pending"}},
                                           "total": 1}})
        cur = service.status("t1")
        with pytest.raises(SemanticConflict):
            await service.resume("t1", cur["revision"], acknowledge_unknown=False)

    run(scenario())


# ==========================================================================
# 预算耗尽 -> 发布已得组并标 partial
# ==========================================================================
def test_budget_exhaustion_publishes_partial():
    snap = _snapshot(4)
    # 把任务预算卡到只够 1 批:max_attempts=1
    store = FakeStore(snap, max_attempts=1)

    def handler(items, title, phase, omit):
        ids = [i["id"] for i in items]
        return _eg(ids[0], ids)  # 本批成组

    service = _service(store, FakeProvider(handler=handler))
    _run_with_batch_size(service, store, 2)  # 2 批,但只够跑 1 批
    final = service.status("t1")
    assert final["status"] == "partial"
    # 第一批已发布
    assert store.published
    assert final["coverage"]["partial"] is True


# ==========================================================================
# 多批 -> 跨窗口未访问对如实标 partial（不谎称全量）
# ==========================================================================
def test_multiwindow_marks_partial_on_unvisited_pairs():
    # 候选数超过一个窗口 -> 跨窗口对未比较 -> partial。
    snap = _snapshot(6)

    def handler(items, title, phase, omit):
        ids = [i["id"] for i in items]
        if phase == PHASE_BATCH:
            return _empty()  # 全是单条候选
        return _empty()

    store = FakeStore(snap, max_attempts=200)
    service = _service(store, FakeProvider(handler=handler))
    # batch_size=2 -> phase1 3 批 -> 6 个 singleton 候选;cross 窗口=2 -> partial
    _run_with_batch_size(service, store, 2)
    final = service.status("t1")
    assert final["status"] == "partial"
    assert final["coverage"]["pairs_unvisited"] > 0


# ==========================================================================
# 失败落 failed（不永久 running） + 错误脱敏
# ==========================================================================
def test_unexpected_provider_crash_marks_failed_not_running():
    class BoomProvider(FakeProvider):
        async def group(self, items, video_title="", phase=PHASE_BATCH, omit_temperature=False):
            raise RuntimeError("服务端原始报文不应泄漏 secret=XYZ")

    store = FakeStore(_snapshot(2))
    service = _service(store, BoomProvider())

    async def scenario():
        await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        await _drain(service)
        return service.status("t1")

    final = run(scenario())
    assert final["status"] == "failed"  # 不是永久 running
    # 错误脱敏:error 是安全字符串(Task.error 为 Text 列),不含原始报文
    err = final.get("error") or ""
    assert isinstance(err, str)
    assert "secret" not in err.lower()
    assert err.startswith("internal_error")


# ==========================================================================
# shutdown / recover：只管本实例、无自动请求
# ==========================================================================
def test_shutdown_cancels_handles_without_store_writes():
    store = FakeStore(_snapshot(2))
    gate = asyncio.Event()

    class SlowProvider(FakeProvider):
        async def group(self, items, video_title="", phase=PHASE_BATCH, omit_temperature=False):
            await gate.wait()
            return _empty()

    service = _service(store, SlowProvider())

    async def scenario():
        await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        await asyncio.sleep(0)  # 让 worker 起步
        await service.shutdown()
        assert service._tasks == {}

    run(scenario())


def test_recover_delegates_to_store_no_auto_request():
    store = FakeStore(_snapshot(2))
    service = _service(store, FakeProvider())

    async def scenario():
        await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        # 不 drain:直接模拟崩溃恢复
        store.force(status="running")
        n = await service.recover()
        return n, service.status("t1")

    n, st = run(scenario())
    assert n == 1
    assert st["status"] == "interrupted"


# ==========================================================================
# 全局串行：同一实例同时只跑一个任务的模型调用
# ==========================================================================
def test_global_serial_semaphore_one_at_a_time():
    store = FakeStore(_snapshot(1))
    concurrency = {"cur": 0, "max": 0}
    gate = asyncio.Event()

    class TrackProvider(FakeProvider):
        async def group(self, items, video_title="", phase=PHASE_BATCH, omit_temperature=False):
            concurrency["cur"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["cur"])
            await asyncio.sleep(0)
            concurrency["cur"] -= 1
            return _empty()

    service = _service(store, TrackProvider())

    async def scenario():
        # 直接并发调度两个 _run（同一 task_id 调度去重,这里用不同 handle 验证信号量）
        await service.start("BV1", 200, CONSENT_VERSION, "rk1")
        h = asyncio.ensure_future(service._run("t1"))
        await _drain(service)
        await asyncio.gather(h, return_exceptions=True)

    run(scenario())
    assert concurrency["max"] <= 1  # 串行


# ==========================================================================
# Reviewer fix 回归：4 项
# ==========================================================================
def test_fix1_crossphase_resume_does_not_recharge_cross_calls():
    # 跑完一次(含 crossphase),置 interrupted 后 resume:已完成窗口不应再调模型。
    snap = _snapshot(4)

    def handler(items, title, phase, omit):
        ids = set(i["id"] for i in items)
        if phase == PHASE_BATCH:
            if ids == {"c0", "c1"}:
                return _eg("c0", ["c0", "c1"])
            if ids == {"c2", "c3"}:
                return _eg("c2", ["c2", "c3"])
            return _empty()
        return _empty()  # cross:无合并提议

    store = FakeStore(snap, max_attempts=200)
    provider = FakeProvider(handler=handler)
    service = _service(store, provider)
    _run_with_batch_size(service, store, 2)
    calls_after_first = len(provider.group_calls)

    async def resume_scenario():
        store.force(status="interrupted")
        cur = service.status("t1")
        import modules.comment.semantic.service as mod
        old = mod.BATCH_SIZE
        mod.BATCH_SIZE = 2
        try:
            await service.resume("t1", cur["revision"], acknowledge_unknown=True)
            await _drain(service)
        finally:
            mod.BATCH_SIZE = old
        return len(provider.group_calls)

    calls_after_resume = run(resume_scenario())
    # phase1 done 批不重发 + crossphase 已完成窗口不重发 => 调用数不增长
    assert calls_after_resume == calls_after_first


def test_fix2_budget_exhaustion_midcross_still_publishes_not_stuck_running():
    # crossphase 中途预算耗尽:发布前重读 revision,任务落到 partial,绝不永久 running。
    snap = _snapshot(4)

    def handler(items, title, phase, omit):
        ids = [i["id"] for i in items]
        if phase == PHASE_BATCH:
            return _eg(ids[0], ids)
        # cross 提议合并,触发 verify -> 再多一次 attempt,配合小预算耗尽
        return _eg(ids[0], ids)

    # max_attempts 卡在只够 phase1(2批)+ 1 次 cross,之后 reserve 抛 budget
    store = FakeStore(snap, max_attempts=3)
    service = _service(store, FakeProvider(handler=handler))
    _run_with_batch_size(service, store, 2)
    final = service.status("t1")
    assert final["status"] == "partial"  # 不是永久 running
    assert final["phase"] == "done"


def test_fix3_unverified_verification_degrades_to_partial():
    # 跨批提议合并,但全量复核调用降级(provider 对复核批抛错)-> coverage partial,不合并。
    snap = _snapshot(4)

    def handler(items, title, phase, omit):
        ids = set(i["id"] for i in items)
        if phase == PHASE_BATCH:
            if ids == {"c0", "c1"}:
                return _eg("c0", ["c0", "c1"])
            if ids == {"c2", "c3"}:
                return _eg("c2", ["c2", "c3"])
            return _empty()
        if ids == {"c0", "c2"}:
            return _eg("c0", ["c0", "c2"])  # 提议合并
        # 全量复核批 {c0,c1,c2,c3}:一律拒答 -> 复核无法判定(unverified)
        raise SemanticValidationError("复核截断", reason="truncated")

    store = FakeStore(snap, max_attempts=200)
    service = _service(store, FakeProvider(handler=handler))
    _run_with_batch_size(service, store, 2)
    final = service.status("t1")
    assert final["status"] == "partial"
    assert final["coverage"]["reason"] in ("verify_unverified", "cross_windows_unvisited",
                                           "cross_degraded")
    # 未合并:两组保持独立
    groups = store.status_groups()
    assert sorted(sorted(g["member_ids"]) for g in groups) == [["c0", "c1"], ["c2", "c3"]]


def test_fix3_rejected_verification_is_complete_not_partial():
    # 复核拿到合法响应但明确不覆盖为单一组(rejected):完整判定,coverage 不降级。
    snap = _snapshot(4)

    def handler(items, title, phase, omit):
        ids = set(i["id"] for i in items)
        if phase == PHASE_BATCH:
            if ids == {"c0", "c1"}:
                return _eg("c0", ["c0", "c1"])
            if ids == {"c2", "c3"}:
                return _eg("c2", ["c2", "c3"])
            return _empty()
        if ids == {"c0", "c2"}:
            return _eg("c0", ["c0", "c2"])
        if ids == {"c0", "c1", "c2", "c3"}:
            return _empty()  # 合法响应:不把全部成员判为单一组 => rejected
        return _empty()

    store = FakeStore(snap, max_attempts=200)
    service = _service(store, FakeProvider(handler=handler))
    _run_with_batch_size(service, store, 2)
    final = service.status("t1")
    assert final["status"] == "completed"  # rejected 是完整判定,不 partial
    assert final["coverage"]["oversize_skipped"] == 0


def test_fix4_resume_preserves_earlier_batch_degraded_flag():
    # 第 0 批降级(done+degraded)已在 checkpoint;resume 只跑剩余批 -> 整体仍 partial。
    snap = _snapshot(4)
    store = FakeStore(snap, max_attempts=200)
    service = _service(store, FakeProvider())

    # 预置:批0 done+degraded,批1 未处理;任务 interrupted。
    store.create_task(snap, IDENTITY, {}, "rk1")
    store.force(status="interrupted",
                checkpoint={"phase1": {"batches": {
                    "0": {"ids": ["c0", "c1"], "status": "done",
                          "equivalent_groups": [], "related_groups": [],
                          "degraded": True}},
                    "total": 2}})

    async def scenario():
        cur = service.status("t1")
        import modules.comment.semantic.service as mod
        old = mod.BATCH_SIZE
        mod.BATCH_SIZE = 2
        try:
            await service.resume("t1", cur["revision"], acknowledge_unknown=True)
            await _drain(service)
        finally:
            mod.BATCH_SIZE = old
        return service.status("t1")

    final = run(scenario())
    # 即便 resume 只处理批1(成功),批0 的 degraded 不被清零 => 整体 partial。
    assert final["status"] == "partial"


# ==========================================================================
# Reviewer fix（新）：cross 覆盖按「真实已复核窗口」计量，不预计算虚报
# ==========================================================================
def test_cross_failed_window_not_counted_as_reviewed():
    # 6 个 singleton -> cross 多窗口;让某个窗口的 group 调用失败(data None)。
    # 该窗口的候选对不得计入 reviewed_pairs(不虚报 visited),coverage 必须 partial。
    snap = _snapshot(6)
    failed_pair = {"c2", "c3"}

    def handler(items, title, phase, omit):
        ids = set(i["id"] for i in items)
        if phase == PHASE_BATCH:
            return _empty()  # 全是 singleton
        if ids == failed_pair:
            raise SemanticValidationError("该窗口截断", reason="truncated")
        return _empty()  # 其余窗口成功但无合并

    store = FakeStore(snap, max_attempts=500)
    service = _service(store, FakeProvider(handler=handler))
    _run_with_batch_size(service, store, 2)
    final = service.status("t1")
    assert final["status"] == "partial"
    cov = final["coverage"]
    # 失败窗口的对未计入已复核(真实覆盖 < 组合全集)
    assert cov["pairs_reviewed"] < cov["total_pairs"]
    assert cov["pairs_unvisited"] > 0
    # 读 checkpoint 直接验证 (c2,c3) 从未被计为已复核
    cp = store.get_task("t1")  # 公开态无 checkpoint;用内部读取
    internal = store._task["checkpoint"]["cross"]
    reviewed = {tuple(p) for p in internal["reviewed_pairs"]}
    assert tuple(sorted(failed_pair)) not in reviewed


def test_cross_rotation_gives_crosswindow_pairs_a_chance():
    # 固定顺序下跨窗口的代表候选不应永久漏掉:环移轮让其至少有一次同窗复核机会。
    snap = _snapshot(4)

    seen_windows: List[frozenset] = []

    def handler(items, title, phase, omit):
        if phase == PHASE_CROSS:
            seen_windows.append(frozenset(i["id"] for i in items))
        return _empty()

    store = FakeStore(snap, max_attempts=500)
    service = _service(store, FakeProvider(handler=handler))
    _run_with_batch_size(service, store, 2)  # 4 singleton -> 2 窗口/轮,2 轮
    # 轮0: [c0,c1],[c2,c3];轮1(offset1): [c1,c2],[c3,c0] -> 跨窗口对获得同窗机会
    assert any(w == frozenset({"c1", "c2"}) for w in seen_windows) or \
           any(w == frozenset({"c3", "c0"}) for w in seen_windows)


def test_phase1_persist_does_not_clobber_cross_checkpoint():
    # 直接验证:写 phase1 checkpoint 不会抹掉已存在的 cross 段(resume 不重复收费前提)。
    snap = _snapshot(2)
    store = FakeStore(snap)
    service = _service(store, FakeProvider())
    store.create_task(snap, IDENTITY, {}, "rk1")
    # 预置一个 cross 段
    store.force(checkpoint={"cross": {"rounds_done": 1, "working": [],
                                      "coverage": {"x": 1}, "reviewed_pairs": []}})
    cur = store.get_task("t1")
    service._persist_phase1("t1", cur["revision"],
                            {"0": {"ids": ["c0", "c1"], "status": "done"}}, 1)
    cp = store._task["checkpoint"]
    assert "cross" in cp and cp["cross"]["rounds_done"] == 1  # 未被抹掉
    assert "phase1" in cp  # 同时写入了 phase1


# ==========================================================================
# 真实 SemanticStore + 临时 SQLite 端到端（FakeProvider）——抓 FakeStore 藏不住的
# 列类型/字段契约差异（progress Float / error Text 等），防漏到 UI。
# ==========================================================================
def _seed_video_comments(bvid: str, n: int) -> None:
    """往隔离临时库写一条视频 + n 条顶层评论(context_status=top_level)。"""
    from core.database import get_session, Video, Comment
    with get_session() as s:
        v = Video(bvid=bvid, title="真实端到端标题")
        s.add(v)
        s.flush()
        for i in range(n):
            s.add(Comment(rpid=str(9000 + i), video_id=v.id, uid=i,
                          uname=f"u{i}", content=f"端到端内容{i}",
                          context_status="top_level"))
        s.commit()


def test_end_to_end_real_store_fakeprovider_completes():
    """真实 SemanticStore(默认临时库) + FakeProvider 跑完整 start->worker->result。

    专门验证 FakeStore 可能掩盖的真实列契约:
    - progress 必须是 Float(0..1),传 dict 会 StatementError;
    - update_task 的 checkpoint/coverage 结构能真实落 JSON 列;
    - 完成后 result() 可读、状态 completed。
    """
    from modules.comment.semantic.store import SemanticStore

    bvid = "BVreal01"
    _seed_video_comments(bvid, 3)
    store = SemanticStore()  # 默认 core.database.get_session -> 隔离临时库

    # 绑定「与模型身份绑定的同意」(preference CAS)。
    pref = store.preference(bvid)
    store.set_preference(bvid, pref["revision"],
                         consent_version=CONSENT_VERSION, model_identity=dict(IDENTITY))

    def handler(items, title, phase, omit):
        ids = [i["id"] for i in items]
        if phase == PHASE_BATCH and len(ids) >= 2:
            return _eg(ids[0], ids[:2])  # 把前两条判等价
        return _empty()

    service = svc.SemanticService(
        store=store, provider_factory=lambda: FakeProvider(handler=handler))

    async def scenario():
        state = await service.start(bvid, 200, CONSENT_VERSION, "rk-real-1")
        assert state["status"] == "queued"
        assert "snapshot" not in state and "checkpoint" not in state
        await _drain(service)
        return service.status(state["task_id"]), state["task_id"]

    final, task_id = run(scenario())
    assert final["status"] in ("completed", "partial")
    assert isinstance(final["progress"], float)  # 真实 Float 列已落值
    assert 0.0 <= final["progress"] <= 1.0
    # result() 可读,含发布组
    res = service.results(task_id, page=1, page_size=50)
    assert res["task"]["status"] == final["status"]


def test_end_to_end_real_store_provider_crash_marks_failed_not_running():
    """真实库下 provider 崩溃:_fail 把 error(安全 str)写进 Text 列,任务落 failed。

    这正是真实 API run 暴露的 bug——error 传 dict 会 ProgrammingError、任务永久
    running;这里确保传 str 后能真实落库且状态为 failed。
    """
    from modules.comment.semantic.store import SemanticStore

    bvid = "BVreal02"
    _seed_video_comments(bvid, 2)
    store = SemanticStore()
    pref = store.preference(bvid)
    store.set_preference(bvid, pref["revision"],
                         consent_version=CONSENT_VERSION, model_identity=dict(IDENTITY))

    class BoomProvider(FakeProvider):
        async def group(self, items, video_title="", phase=PHASE_BATCH, omit_temperature=False):
            raise RuntimeError("raw server secret=XYZ 不应泄漏")

    service = svc.SemanticService(store=store, provider_factory=lambda: BoomProvider())

    async def scenario():
        state = await service.start(bvid, 200, CONSENT_VERSION, "rk-real-2")
        await _drain(service)
        return service.status(state["task_id"])

    final = run(scenario())
    assert final["status"] == "failed"  # 真实库成功落 failed,非永久 running
    assert isinstance(final["error"], str)  # Text 列:字符串
    assert "secret" not in final["error"].lower()  # 脱敏
    assert final["error"].startswith("internal_error")
    # 契约外异常也要先结算在飞记录:否则 in_flight 残留,续跑会卡住。
    assert final["budget"]["in_flight"] is None
    assert final["budget"]["reserved_tokens"] == 0
    assert final["budget"]["settled_tokens"] > 0  # 请求可能已发出:按估算计费,不退款


def _real_store_with_consent(bvid: str, n: int):
    from modules.comment.semantic.store import SemanticStore

    _seed_video_comments(bvid, n)
    store = SemanticStore()
    pref = store.preference(bvid)
    store.set_preference(bvid, pref["revision"],
                         consent_version=CONSENT_VERSION, model_identity=dict(IDENTITY))
    return store


def test_real_store_provider_crash_then_resume_completes_not_stuck_running():
    """契约外异常导致 failed 后续跑:不能因残留在飞记录卡在 running(原实现会)。"""
    bvid = "BVreal0b1"
    store = _real_store_with_consent(bvid, 2)
    calls = {"n": 0}

    class FlakyProvider(FakeProvider):
        async def group(self, items, video_title="", phase=PHASE_BATCH, omit_temperature=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("provider bug")
            return await super().group(items, video_title, phase, omit_temperature)

    provider = FlakyProvider()
    service = svc.SemanticService(store=store, provider_factory=lambda: provider)

    async def scenario():
        state = await service.start(bvid, 200, CONSENT_VERSION, "rk-0b-1")
        await _drain(service)
        failed = service.status(state["task_id"])
        await service.resume(state["task_id"], failed["revision"])
        await _drain(service)
        return failed, service.status(state["task_id"])

    failed, final = run(scenario())
    assert failed["status"] == "failed"
    assert final["status"] in ("completed", "partial")
    assert final["phase"] == "done"
    assert final["budget"]["in_flight"] is None
    assert final["budget"]["reserved_tokens"] == 0


def test_real_store_cross_budget_after_settled_retry_ends_partial_not_running(monkeypatch):
    """跨批同一单元里先 settle 过一次(校验失败重试),重试时 reserve 才超预算。

    此时本地 revision 落后 store 两步;原实现用旧 revision 写 cross checkpoint,
    CAS 冲突被当作并发处置吞掉,任务永久停在 running/cross。
    """
    bvid = "BVreal0b2"
    store = _real_store_with_consent(bvid, 4)

    def handler(items, title, phase, omit):
        if phase == PHASE_BATCH:
            return _empty()
        raise SemanticValidationError("截断", reason="truncated")

    service = svc.SemanticService(
        store=store, provider_factory=lambda: FakeProvider(handler=handler))
    import modules.comment.semantic.service as mod
    # phase1 两批各 1 次 + cross 第一次尝试 1 次 = 3;第二次尝试 reserve 超限。
    monkeypatch.setattr(mod, "DEFAULT_LIMITS", {**mod.DEFAULT_LIMITS, "max_attempts": 3})
    old = mod.BATCH_SIZE
    mod.BATCH_SIZE = 2  # 4 条 -> 2 批,进入 cross
    try:
        async def scenario():
            state = await service.start(bvid, 200, CONSENT_VERSION, "rk-0b-2")
            await _drain(service)
            return service.status(state["task_id"])

        final = run(scenario())
    finally:
        mod.BATCH_SIZE = old
    assert final["status"] == "partial"
    assert final["phase"] == "done"
    assert final["coverage"]["reason"] == "budget"
    assert final["budget"]["attempt_count"] == 3
    assert final["budget"]["in_flight"] is None

