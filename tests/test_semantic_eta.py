"""PR 5：耗时统计、实测中位数与运行中剩余时间（ETA）。"""
import pytest

from core.database import DatabaseManager
from core.database.models_comment_semantic import SemanticRun
from modules.comment.semantic.store import SemanticStore
from modules.comment.semantic.service import _eta_for_task, _start_estimate
from modules.comment.semantic.contracts import PHASE_BATCH, PHASE_CROSS

BVID = "BVeta05"
IDENTITY = {"api_base": "http://local.test", "model": "fake-chat"}


@pytest.fixture()
def store(tmp_path):
    manager = DatabaseManager(str(tmp_path / "semantic.db"))
    return SemanticStore(session_factory=manager.get_session), manager


def _seed_video(manager, bvid=BVID):
    from core.database import Video
    session = manager.get_session()
    try:
        v = Video(bvid=bvid, title="ETA")
        session.add(v)
        session.commit()
    finally:
        session.close()


def _create(store_obj, key="rk-eta"):
    snap = store_obj.snapshot(BVID, limit=10)
    return store_obj.create_task(
        snap, IDENTITY,
        {"max_attempts": 100, "task_token_budget": 10 ** 6, "daily_token_budget": 10 ** 6},
        key)


def test_settle_accumulates_elapsed_ms_and_timed_calls(store):
    """settle 带 call_ms：累计耗时与次数；void 与 quiet（不传 call_ms）不计。"""
    store_obj, manager = store
    _seed_video(manager)
    state = _create(store_obj)
    tid = state["task_id"]
    state = store_obj.reserve_attempt(tid, state["revision"], 100)
    state = store_obj.settle_attempt(tid, state["revision"], usage_tokens=120, call_ms=2500)
    state = store_obj.reserve_attempt(tid, state["revision"], 100)
    state = store_obj.settle_attempt(tid, state["revision"], usage_tokens=110, call_ms=3500)
    budget = state["budget"]
    assert budget["elapsed_call_ms"] == 6000
    assert budget["timed_calls"] == 2

    # 不传 call_ms（取消/异常路径）不计入耗时
    state = store_obj.reserve_attempt(tid, state["revision"], 100)
    state = store_obj.settle_attempt(tid, state["revision"], usage_tokens=90)
    assert state["budget"]["elapsed_call_ms"] == 6000
    assert state["budget"]["timed_calls"] == 2

    # void 不计
    state = store_obj.reserve_attempt(tid, state["revision"], 100)
    state = store_obj.void_attempt(tid, state["revision"])
    assert state["budget"]["elapsed_call_ms"] == 6000
    assert state["budget"]["timed_calls"] == 2


def test_recent_call_ms_median_uses_per_run_averages(store):
    """中位数按每个任务的平均单次耗时取，不是把全部调用混在一起。"""
    store_obj, manager = store
    _seed_video(manager)
    # 任务 1：平均 1000ms；任务 2：平均 9000ms；任务 3：无计时（不参与）
    for key, avg in (("rk-a", 1000), ("rk-b", 9000)):
        state = _create(store_obj, key=key)
        tid = state["task_id"]
        for _ in range(2):
            state = store_obj.reserve_attempt(tid, state["revision"], 100)
            state = store_obj.settle_attempt(tid, state["revision"], 100, call_ms=avg)
    state = _create(store_obj, key="rk-c")
    assert store_obj.recent_call_ms_median() == 5000
    assert store_obj.recent_call_ms_median(limit_runs=1) == 9000  # 最近的是 rk-b


def test_recent_call_ms_median_empty_returns_none(store):
    """没有历史数据时返回 None，由调用方回退到固定区间。"""
    store_obj, manager = store
    assert store_obj.recent_call_ms_median() is None


def test_recent_call_ms_median_ignores_single_call_tasks(store):
    """只计时过一次的任务偶然性太大，不参与中位数（避免拉偏）。"""
    store_obj, manager = store
    _seed_video(manager)
    state = _create(store_obj, key="rk-one")
    tid = state["task_id"]
    state = store_obj.reserve_attempt(tid, state["revision"], 100)
    state = store_obj.settle_attempt(tid, state["revision"], 100, call_ms=99000)
    assert store_obj.recent_call_ms_median() is None  # 只有 1 次调用的任务被排除


def test_start_estimate_uses_median_when_available():
    """有实测中位数：开始前的耗时区间用它；没有则回退固定 10~30 秒。"""
    items = [{"id": f"c{i}", "content": "内容"} for i in range(3)]
    snapshot = {"items": items, "eligible_ids": ["c0", "c1", "c2"]}
    with_median = _start_estimate(snapshot, median_call_ms=20000)
    assert with_median["seconds_min"] == 20  # 一批 × 20 秒
    assert with_median["seconds_max"] == 60  # 三轮上限 × 20 秒
    assert with_median["median_call_ms"] == 20000

    without = _start_estimate(snapshot)
    assert without["seconds_min"] == 10
    assert without["seconds_max"] == 90
    assert without["median_call_ms"] is None


def test_eta_hidden_before_three_timed_calls():
    """前 3 次调用之前不显示剩余时间，避免数字乱跳。"""
    state = {"status": "running", "phase": PHASE_BATCH,
             "budget": {"timed_calls": 2, "elapsed_call_ms": 4000},
             "checkpoint": {"phase1": {"total": 5, "batches": {}}}}
    assert _eta_for_task(state) is None


def test_eta_batch_phase_uses_checkpoint_total():
    """批内阶段：剩余 = checkpoint.total - 已完成批；含跨批下限；平均耗时用实测。"""
    state = {
        "status": "running", "phase": PHASE_BATCH,
        "budget": {"timed_calls": 4, "elapsed_call_ms": 8000},
        "checkpoint": {"phase1": {"total": 10, "batches": {
            "b0": {"status": "done"}, "b1": {"status": "done"}}}},
    }
    eta = _eta_for_task(state)
    assert eta["avg_call_ms"] == 2000
    assert eta["calls_left"] == 8
    assert eta["remaining_ms"] == 16000
    assert eta["at_least"] is True  # 合并复核不可预知，显示「至少」


def test_eta_single_batch_has_no_cross_phase_call():
    """只有一批时没有跨批阶段：批内跑完后剩余调用为 0，不能虚报还有一次。"""
    state = {
        "status": "running", "phase": PHASE_BATCH,
        "budget": {"timed_calls": 3, "elapsed_call_ms": 6000},
        "checkpoint": {"phase1": {"total": 1, "batches": {"b0": {"status": "done"}}}},
    }
    eta = _eta_for_task(state)
    assert eta["calls_left"] == 0
    assert eta["remaining_ms"] == 0


def test_eta_cross_phase_uses_window_plan():
    """跨批阶段：剩余 = 计划窗口数 - 已完成窗口（PR 0c 的窗口计划）。"""
    state = {
        "status": "running", "phase": PHASE_CROSS,
        "budget": {"timed_calls": 5, "elapsed_call_ms": 15000},
        "checkpoint": {"cross": {"plan": [["a"], ["b"], ["c"]], "windows_done": [0]}},
    }
    eta = _eta_for_task(state)
    assert eta["calls_left"] == 2
    assert eta["total"] == 3 and eta["done"] == 1
    assert eta["remaining_ms"] == 6000


def test_eta_queued_shows_queue_note_not_time():
    """排队时不显示剩余时间，显示排队说明（语义任务全局串行）。"""
    eta = _eta_for_task({"status": "queued", "budget": {}, "checkpoint": {}})
    assert eta["queued_note"]
    assert "remaining_ms" not in eta


def test_eta_absent_when_not_running():
    """已停止/完成的任务不显示剩余时间。"""
    assert _eta_for_task({"status": "partial", "budget": {"timed_calls": 5}}) is None
    assert _eta_for_task({"status": "completed", "budget": {"timed_calls": 5}}) is None
