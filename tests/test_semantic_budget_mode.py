"""预算模式（default/unlimited）的契约与 store 层测试。

全部用 tmp_path 下的真实 SQLite + 真实 ORM。覆盖：
- 迁移幂等（缺列旧库补四列、旧行默认 default）
- unlimited 不检查/不写入 semantic_daily_usage；同日默认档不受影响
- 续跑单向升级（default->unlimited），降级/重复升级拒绝；残留在飞先按原标记结算
- 同 request_key 不同模式返回冲突（409）
- ORM 列默认值与 contracts 常量一致（models 不能 import contracts，靠本测试钉住）
"""
import sqlite3

import pytest

from core.database import DatabaseManager
from core.database.models_comment_semantic import SemanticRun
from modules.comment.semantic.contracts import (
    BUDGET_MODE_DEFAULT,
    BUDGET_MODE_UNLIMITED,
    BUDGET_PROFILES,
    DAILY_TOKEN_BUDGET,
    MAX_ATTEMPTS,
    TASK_TOKEN_BUDGET,
    resolve_budget_mode,
    budget_profile,
)
from modules.comment.semantic.store import (
    SemanticStore,
    SemanticConflict,
    SemanticBudgetExceeded,
    SemanticValidationError,
)

BVID = "BVbudget03"
IDENTITY = {"api_base": "http://local.test", "model": "fake-chat"}


@pytest.fixture()
def store(tmp_path):
    manager = DatabaseManager(str(tmp_path / "semantic.db"))
    return SemanticStore(session_factory=manager.get_session), manager


def _seed_video(manager, bvid=BVID):
    from core.database import Video
    session = manager.get_session()
    try:
        v = Video(bvid=bvid, title="预算模式")
        session.add(v)
        session.commit()
    finally:
        session.close()


def _create(store_obj, bvid=BVID, mode=BUDGET_MODE_DEFAULT, key=None):
    snap = store_obj.snapshot(bvid, limit=10)
    profile = budget_profile(mode)
    return store_obj.create_task(
        snap, IDENTITY, profile, key or f"rk-{mode}", budget_mode=mode)


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------

def test_semantic_run_budget_columns_migration_idempotent(tmp_path):
    """缺列旧库：两次建表后四列齐全，旧行 budget_mode 回落 default、计数列为 0。"""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    try:
        conn.execute(
            "CREATE TABLE semantic_runs (id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, "
            "bvid VARCHAR(50) NOT NULL, request_key VARCHAR(128) NOT NULL, "
            "snapshot_hash VARCHAR(64) NOT NULL, revision INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO semantic_runs (task_id, bvid, request_key, snapshot_hash) "
                     "VALUES (1, 'BVold', 'rk-old', 'h')")
        conn.commit()
    finally:
        conn.close()

    DatabaseManager(str(legacy))  # 第一次：补列
    DatabaseManager(str(legacy))  # 第二次：幂等

    conn = sqlite3.connect(legacy)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_runs)")}
        assert {"budget_mode", "stop_seq", "elapsed_call_ms", "timed_calls"} <= cols
        row = conn.execute(
            "SELECT budget_mode, stop_seq, elapsed_call_ms, timed_calls "
            "FROM semantic_runs WHERE bvid='BVold'").fetchone()
        assert row == ("default", 0, 0, 0)
    finally:
        conn.close()


def test_orm_defaults_match_contracts():
    """models 不能 import contracts（循环导入）：默认值字面量靠本测试与常量钉死。"""
    from modules.comment.semantic import contracts
    table = SemanticRun.__table__
    assert table.c.budget_mode.default.arg == contracts.BUDGET_MODE_DEFAULT
    assert BUDGET_PROFILES[BUDGET_MODE_DEFAULT]["max_attempts"] == MAX_ATTEMPTS
    assert BUDGET_PROFILES[BUDGET_MODE_DEFAULT]["task_token_budget"] == TASK_TOKEN_BUDGET
    assert BUDGET_PROFILES[BUDGET_MODE_DEFAULT]["daily_token_budget"] == DAILY_TOKEN_BUDGET


# ---------------------------------------------------------------------------
# 模式解析
# ---------------------------------------------------------------------------

def test_resolve_budget_mode():
    assert resolve_budget_mode(None) == BUDGET_MODE_DEFAULT
    assert resolve_budget_mode("default") == BUDGET_MODE_DEFAULT
    assert resolve_budget_mode("unlimited") == BUDGET_MODE_UNLIMITED
    with pytest.raises(SemanticValidationError):
        resolve_budget_mode("infinite")
    with pytest.raises(SemanticValidationError):
        budget_profile("infinite")


# ---------------------------------------------------------------------------
# 创建：幂等键遇不同模式返回冲突
# ---------------------------------------------------------------------------

def test_same_request_key_different_mode_conflicts(store):
    store_obj, manager = store
    _seed_video(manager)
    _create(store_obj, mode=BUDGET_MODE_DEFAULT, key="rk-dup")
    with pytest.raises(SemanticConflict):
        _create(store_obj, mode=BUDGET_MODE_UNLIMITED, key="rk-dup")
    # 同模式同快照：幂等返回，不报冲突
    again = _create(store_obj, mode=BUDGET_MODE_DEFAULT, key="rk-dup")
    assert again["budget"]["budget_mode"] == BUDGET_MODE_DEFAULT


def test_unlimited_task_uses_profile_limits(store):
    store_obj, manager = store
    _seed_video(manager)
    state = _create(store_obj, mode=BUDGET_MODE_UNLIMITED)
    budget = state["budget"]
    assert budget["budget_mode"] == BUDGET_MODE_UNLIMITED
    assert budget["max_attempts"] == BUDGET_PROFILES["unlimited"]["max_attempts"]
    assert budget["task_token_budget"] == BUDGET_PROFILES["unlimited"]["task_token_budget"]


# ---------------------------------------------------------------------------
# 日用量：无限制档不检查、不记账
# ---------------------------------------------------------------------------

def test_unlimited_skips_daily_budget_and_ledger(store):
    """无限制档：daily_token_budget 为 0 也能预约；全程不写 semantic_daily_usage。"""
    store_obj, manager = store
    _seed_video(manager)
    state = _create(store_obj, mode=BUDGET_MODE_UNLIMITED)
    tid = state["task_id"]

    state = store_obj.reserve_attempt(tid, state["revision"], 10 ** 6)  # 远超默认日预算
    assert state["budget"]["in_flight"]["daily_tracked"] is False
    state = store_obj.settle_attempt(tid, state["revision"], usage_tokens=10 ** 6)

    session = manager.get_session()
    try:
        from core.database import SemanticDailyUsage
        assert session.query(SemanticDailyUsage).count() == 0
    finally:
        session.close()

    # 同一天的默认档任务照常记日用量，互不影响
    state2 = _create(store_obj, key="rk-def")
    state2 = store_obj.reserve_attempt(state2["task_id"], state2["revision"], 100)
    session = manager.get_session()
    try:
        from core.database import SemanticDailyUsage
        day = session.query(SemanticDailyUsage).first()
        assert day is not None and int(day.reserved_tokens) == 100
    finally:
        session.close()


def test_default_mode_daily_gate_unchanged(store):
    """默认档：日预算闸门照旧（回归保护）。"""
    store_obj, manager = store
    _seed_video(manager)
    snap = store_obj.snapshot(BVID, limit=10)
    state = store_obj.create_task(
        snap, IDENTITY,
        {"max_attempts": 100, "task_token_budget": 10 ** 6, "daily_token_budget": 50},
        "rk-dg", budget_mode=BUDGET_MODE_DEFAULT)
    with pytest.raises(SemanticBudgetExceeded) as ei:
        store_obj.reserve_attempt(state["task_id"], state["revision"], 100)
    assert ei.value.scope == "daily"


# ---------------------------------------------------------------------------
# 续跑升级
# ---------------------------------------------------------------------------

def _make_partial(store_obj, mode=BUDGET_MODE_DEFAULT, key="rk-up"):
    state = _create(store_obj, mode=mode, key=key)
    tid = state["task_id"]
    state = store_obj.update_task(tid, state["revision"], status="running", phase="batch")
    state = store_obj.update_task(tid, state["revision"], status="partial", phase="done",
                                  coverage={"stop_reason": "daily_budget"})
    return state


def test_resume_upgrade_default_to_unlimited(store):
    store_obj, manager = store
    _seed_video(manager)
    state = _make_partial(store_obj)
    tid = state["task_id"]
    state = store_obj.resume_task(tid, state["revision"], upgrade_to="unlimited")
    assert state["status"] == "queued"
    assert state["budget"]["budget_mode"] == BUDGET_MODE_UNLIMITED
    assert state["budget"]["task_token_budget"] == BUDGET_PROFILES["unlimited"]["task_token_budget"]
    # 无限制档日预算是占位 0：DTO 必须如实返回 0，不能被 `or` 兜底谎报成默认档 10 万
    assert state["budget"]["daily_token_budget"] == 0
    # 升级后预约不再检查日预算
    state = store_obj.reserve_attempt(tid, state["revision"], 10 ** 6)
    assert state["budget"]["in_flight"]["daily_tracked"] is False


def test_resume_downgrade_rejected(store):
    store_obj, manager = store
    _seed_video(manager)
    state = _make_partial(store_obj, mode=BUDGET_MODE_UNLIMITED, key="rk-no-down")
    with pytest.raises(SemanticValidationError):
        store_obj.resume_task(state["task_id"], state["revision"], upgrade_to="default")
    # 已是无限制：再次升级也拒绝
    with pytest.raises(SemanticValidationError):
        store_obj.resume_task(state["task_id"], state["revision"], upgrade_to="unlimited")
    # 非法值
    state = _make_partial(store_obj, key="rk-bad")
    with pytest.raises(SemanticValidationError):
        store_obj.resume_task(state["task_id"], state["revision"], upgrade_to="infinite")


def test_resume_upgrade_settles_leftover_in_flight_first(store):
    """升级前先把残留的在飞预约按原标记（default=记日用量）结算，再切换口径。"""
    store_obj, manager = store
    _seed_video(manager)
    state = _create(store_obj, key="rk-leftover")
    tid = state["task_id"]
    state = store_obj.update_task(tid, state["revision"], status="running", phase="batch")
    state = store_obj.reserve_attempt(tid, state["revision"], 500)
    # 模拟崩溃恢复留下的在飞记录（outcome_unknown 需确认）
    session = manager.get_session()
    try:
        run = session.query(SemanticRun).filter_by(task_id=int(tid)).first()
        run.in_flight = dict(run.in_flight, outcome_unknown=True)
        session.commit()
    finally:
        session.close()
    state = store_obj.update_task(tid, state["revision"], status="partial", phase="done")

    state = store_obj.resume_task(tid, state["revision"], acknowledge_unknown=True,
                                  upgrade_to="unlimited")
    assert state["budget"]["budget_mode"] == BUDGET_MODE_UNLIMITED
    assert state["budget"]["in_flight"] is None
    assert state["budget"]["settled_tokens"] == 500  # 按估算结算，不退款
    # 残留预约属于默认档时期：日用量照常入账（该日有一行 settled=500）
    session = manager.get_session()
    try:
        from core.database import SemanticDailyUsage
        day = session.query(SemanticDailyUsage).first()
        assert day is not None and int(day.settled_tokens) == 500
    finally:
        session.close()


def test_stop_seq_increments_on_partial_and_failed_not_completed(store):
    """stop_seq：进入 partial/failed/interrupted 各 +1，completed/cancelled 不加。"""
    store_obj, manager = store
    _seed_video(manager)
    state = _create(store_obj, key="rk-seq")
    tid = state["task_id"]
    assert state["budget"]["stop_seq"] == 0
    state = store_obj.update_task(tid, state["revision"], status="running", phase="batch")
    state = store_obj.update_task(tid, state["revision"], status="partial", phase="done")
    assert state["budget"]["stop_seq"] == 1
    state = store_obj.resume_task(tid, state["revision"])
    state = store_obj.update_task(tid, state["revision"], status="running", phase="batch")
    state = store_obj.update_task(tid, state["revision"], status="failed",
                                  error="x", coverage={"stop_reason": "failed:auth"})
    assert state["budget"]["stop_seq"] == 2


# ---------------------------------------------------------------------------
# 复核补充：路由字面量与契约常量一致；全局日额度读取与 LLMClient 同口径
# ---------------------------------------------------------------------------

def test_router_literals_match_budget_profiles():
    """路由请求模型用 Literal 写死档位名：必须与 contracts.BUDGET_PROFILES 完全一致，
    升级目标只能是无限制档。新增档位而忘改路由时这里会失败。"""
    import typing
    from web.routers.comment_semantic import ResumeRequest, TaskCreateRequest
    create_modes = set(typing.get_args(TaskCreateRequest.model_fields["budget_mode"].annotation))
    assert create_modes == set(BUDGET_PROFILES)
    upgrade_ann = ResumeRequest.model_fields["upgrade_to"].annotation
    upgrade_modes = {a for arg in typing.get_args(upgrade_ann) for a in typing.get_args(arg)}
    assert upgrade_modes == {BUDGET_MODE_UNLIMITED}


def test_global_daily_remaining_uses_client_default_when_unset(monkeypatch):
    """缺 llm.daily_token_limit 配置时按 LLMClient 的默认上限算剩余，不能当成 0（不限制）。"""
    import llm.client as lc
    from core.config import LLM_DAILY_TOKEN_LIMIT_DEFAULT
    from modules.comment.semantic import service as svc

    monkeypatch.setattr(svc.config, "get",
                        lambda key, default=None: default)
    monkeypatch.setattr(lc, "_db_tokens_used_on", lambda day: 1000)
    gd = svc._global_daily_remaining()
    assert gd["limit"] == LLM_DAILY_TOKEN_LIMIT_DEFAULT
    assert gd["remaining"] == LLM_DAILY_TOKEN_LIMIT_DEFAULT - 1000


def test_global_daily_remaining_zero_limit_is_unlimited(monkeypatch):
    """上限配置为 0 表示不限制：remaining 为 None，前端不出额度提示。"""
    import llm.client as lc
    from modules.comment.semantic import service as svc

    monkeypatch.setattr(svc.config, "get", lambda key, default=None: 0)
    monkeypatch.setattr(lc, "_db_tokens_used_on", lambda day: 5)
    assert svc._global_daily_remaining()["remaining"] is None
