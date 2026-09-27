"""web.routers.lottery 抽奖工具接口测试（第4批 · web 段）。

覆盖对象：
- get_service / _create_task / _progress（内部辅助）
- _run_filter_task / _run_draw_task（后台任务，含日期区间校验与中奖快照）
- POST /preview 、POST /quick-filter 、POST /verify-winners
- POST /filter/tasks 、POST /draw/tasks 、GET /tasks/{task_id}

验证维度：
服务单例组装 / 任务状态生命周期 / 进度钳制 / 预览四类返回 / 校验名单来源与空名单 400 /
后台任务成功与失败落状态 / 中奖名单快照复用。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- 用契约级假 LotteryService 与假 fetch_target_metadata，避免真实网络与抽奖。
- 仓库未安装 pytest-asyncio，async 用例统一用 asyncio.run 驱动。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers import lottery as lottery_module


class _FakeLotteryService:
    """契约级假抽奖服务，所有方法返回预置值或抛出注入异常。"""

    state: dict = {}

    def __init__(self, api=None) -> None:
        self.api = api if api is not None else object()

    @classmethod
    def reset(cls) -> None:
        """重置类级预置状态。"""
        cls.state = {
            "preview": {"title": "视频标题", "owner": "UP主"},
            "preview_error": None,
            "quick": {"uid": 1, "is_real": True},
            "quick_error": None,
            "verify": {"results": []},
            "verify_error": None,
            "filter": {"real": [], "fake": []},
            "filter_error": None,
            "draw": {"winners": [{"uid": 9, "uname": "中奖者"}]},
            "draw_error": None,
        }

    async def preview(self, target):
        """返回预置预览结果。"""
        if type(self).state["preview_error"] is not None:
            raise type(self).state["preview_error"]
        return type(self).state["preview"]

    async def quick_filter(self, uid, focus_template):
        """返回预置快速筛选结果。"""
        if type(self).state["quick_error"] is not None:
            raise type(self).state["quick_error"]
        return type(self).state["quick"]

    async def verify_winners(self, winners, focus_template):
        """返回预置校验结果。"""
        type(self).state["last_winners"] = winners
        if type(self).state["verify_error"] is not None:
            raise type(self).state["verify_error"]
        return type(self).state["verify"]

    async def filter_real_users(self, target, focus_template, progress_callback=None):
        """按需回调进度后返回预置筛选结果。"""
        if progress_callback is not None:
            progress_callback("profiling", 50, "分析中")
        if type(self).state["filter_error"] is not None:
            raise type(self).state["filter_error"]
        return type(self).state["filter"]

    async def draw(self, target, winner_count, unique_users, progress_callback=None, **kwargs):
        """按需回调进度后返回预置抽奖结果。"""
        type(self).state["draw_kwargs"] = kwargs
        if progress_callback is not None:
            progress_callback("drawing", 80, "抽取中")
        if type(self).state["draw_error"] is not None:
            raise type(self).state["draw_error"]
        return type(self).state["draw"]


async def _fake_fetch_target(api, target):
    """目标元数据替身，直接回显请求目标。"""
    return {"target": target}


async def _noop_async(*args, **kwargs) -> None:
    """后台任务替身：不执行真实流程。"""
    return None


@pytest.fixture()
def client(monkeypatch):
    """挂载 lottery router 的测试客户端，并注入假服务与隔离任务表。"""
    _FakeLotteryService.reset()
    service = _FakeLotteryService()
    monkeypatch.setattr(lottery_module, "get_service", lambda: service)
    monkeypatch.setattr(lottery_module, "fetch_target_metadata", _fake_fetch_target)
    monkeypatch.setattr(lottery_module, "_tasks", {})
    monkeypatch.setattr(lottery_module, "_latest_winners", [])
    monkeypatch.setattr(lottery_module, "_run_filter_task", _noop_async)
    monkeypatch.setattr(lottery_module, "_run_draw_task", _noop_async)

    app = FastAPI()
    app.include_router(lottery_module.router, prefix="/api")
    return TestClient(app)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def test_get_service_builds_singleton(monkeypatch):
    """get_service 应组装限频器与 Cookie 池，并缓存单例。"""
    created = {}

    def fake_api(**kwargs):
        """记录构造参数并返回替身。"""
        created.update(kwargs)
        return object()

    pool = object()
    monkeypatch.setattr(lottery_module, "_service", None)
    monkeypatch.setattr(lottery_module, "BilibiliAPI", fake_api)
    monkeypatch.setattr(lottery_module, "RateLimiter", lambda: "limiter")
    monkeypatch.setattr(lottery_module, "get_cookie_pool", lambda: pool)
    monkeypatch.setattr(lottery_module, "LotteryService", lambda api: ("service", api))

    first = lottery_module.get_service()
    second = lottery_module.get_service()

    assert first is second
    assert created["cookie_pool"] is pool
    assert created["rate_limiter"] == "limiter"


def test_create_task_records_running_state(monkeypatch):
    """新建任务应处于 running/queued 且进度为 0。"""
    monkeypatch.setattr(lottery_module, "_tasks", {})
    task_id = lottery_module._create_task("filter")

    task = lottery_module._tasks[task_id]
    assert task["kind"] == "filter"
    assert task["status"] == "running"
    assert task["stage"] == "queued"
    assert task["progress"] == 0
    assert task["result"] is None


def test_progress_clamps_and_estimates(monkeypatch):
    """进度应钳制在 1-99，并按已完成比例估算剩余秒数。"""
    monkeypatch.setattr(lottery_module, "_tasks", {})
    task_id = lottery_module._create_task("draw")

    lottery_module._progress(task_id, "s", 0, "低")
    assert lottery_module._tasks[task_id]["progress"] == 1
    assert lottery_module._tasks[task_id]["estimated_seconds"] is None

    lottery_module._progress(task_id, "s", 200, "高")
    assert lottery_module._tasks[task_id]["progress"] == 99


def test_progress_missing_task_is_noop(monkeypatch):
    """任务不存在时进度更新应静默跳过。"""
    monkeypatch.setattr(lottery_module, "_tasks", {})
    lottery_module._progress("missing", "s", 50, "msg")


# ---------------------------------------------------------------------------
# POST /preview
# ---------------------------------------------------------------------------


def test_preview_success(client):
    """预览成功应返回目标元数据。"""
    body = client.post("/api/lottery/preview", json={"target": "BV1xx411c7mD"}).json()
    assert body == {"success": True, "data": {"title": "视频标题", "owner": "UP主"}}


def test_preview_invalid_target_returns_400(client):
    """输入非法应转 400。"""
    _FakeLotteryService.state["preview_error"] = ValueError("无效的目标")
    response = client.post("/api/lottery/preview", json={"target": "BV1xx"})
    assert response.status_code == 400


def test_preview_service_error_returns_502(client):
    """服务端异常应转 502。"""
    _FakeLotteryService.state["preview_error"] = RuntimeError("上游挂了")
    response = client.post("/api/lottery/preview", json={"target": "BV1xx"})
    assert response.status_code == 502
    assert "读取目标信息失败" in response.json()["detail"]


def test_preview_rejects_short_target(client):
    """target 长度不足 3 应返回 422。"""
    assert client.post("/api/lottery/preview", json={"target": "BV"}).status_code == 422


# ---------------------------------------------------------------------------
# POST /quick-filter
# ---------------------------------------------------------------------------


def test_quick_filter_success(client):
    """快速筛选成功应返回分析结果。"""
    body = client.post("/api/lottery/quick-filter", json={"uid": 123}).json()
    assert body == {"success": True, "data": {"uid": 1, "is_real": True}}


def test_quick_filter_failure_returns_502(client):
    """快速筛选异常应转 502。"""
    _FakeLotteryService.state["quick_error"] = RuntimeError("分析失败")
    response = client.post("/api/lottery/quick-filter", json={"uid": 123})
    assert response.status_code == 502


def test_quick_filter_rejects_non_positive_uid(client):
    """uid 必须为正整数，否则 422。"""
    assert client.post("/api/lottery/quick-filter", json={"uid": 0}).status_code == 422


# ---------------------------------------------------------------------------
# POST /verify-winners
# ---------------------------------------------------------------------------


def test_verify_winners_requires_non_empty_list(client):
    """请求与服务端均无名单时应返回 400。"""
    response = client.post("/api/lottery/verify-winners", json={"winners": []})
    assert response.status_code == 400
    assert response.json()["detail"] == "请先进行抽奖！"


def test_verify_winners_reuses_latest_snapshot(client, monkeypatch):
    """请求未带名单时应复用最近一次中奖快照。"""
    monkeypatch.setattr(lottery_module, "_latest_winners", [{"uid": 5, "uname": "旧中奖者"}])

    response = client.post("/api/lottery/verify-winners", json={})
    assert response.status_code == 200
    assert _FakeLotteryService.state["last_winners"] == [{"uid": 5, "uname": "旧中奖者"}]


def test_verify_winners_uses_request_list(client):
    """请求带名单时应优先使用请求名单。"""
    winners = [{"uid": 8, "uname": "指定"}]
    response = client.post("/api/lottery/verify-winners", json={"winners": winners})
    assert response.status_code == 200
    assert _FakeLotteryService.state["last_winners"] == winners


def test_verify_winners_value_error_returns_400(client):
    """业务校验错误应转 400。"""
    _FakeLotteryService.state["verify_error"] = ValueError("名单格式错误")
    response = client.post("/api/lottery/verify-winners", json={"winners": [{"uid": 1}]})
    assert response.status_code == 400


def test_verify_winners_failure_returns_502(client):
    """服务端异常应转 502。"""
    _FakeLotteryService.state["verify_error"] = RuntimeError("校验失败")
    response = client.post("/api/lottery/verify-winners", json={"winners": [{"uid": 1}]})
    assert response.status_code == 502


# ---------------------------------------------------------------------------
# POST /filter/tasks 与 /draw/tasks
# ---------------------------------------------------------------------------


def test_start_filter_task_returns_202_and_task_id(client):
    """真人筛选任务应返回 202 与任务 ID 并登记状态。"""
    response = client.post("/api/lottery/filter/tasks", json={"target": "BV1xx411c7mD"})
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    assert lottery_module._tasks[task_id]["kind"] == "filter"


def test_start_draw_task_returns_202_and_task_id(client):
    """随机抽奖任务应返回 202 与任务 ID 并登记状态。"""
    response = client.post(
        "/api/lottery/draw/tasks", json={"target": "BV1xx411c7mD", "winner_count": 3}
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    assert lottery_module._tasks[task_id]["kind"] == "draw"


# ---------------------------------------------------------------------------
# GET /tasks/{task_id}
# ---------------------------------------------------------------------------


def test_get_task_hides_internal_timing(client, monkeypatch):
    """任务查询应隐藏 started_monotonic。"""
    monkeypatch.setattr(
        lottery_module,
        "_tasks",
        {
            "t-1": {
                "task_id": "t-1",
                "kind": "filter",
                "status": "completed",
                "stage": "completed",
                "progress": 100,
                "message": "完成",
                "estimated_seconds": 0,
                "started_monotonic": 1.23,
                "result": {"real": []},
            }
        },
    )
    body = client.get("/api/lottery/tasks/t-1").json()
    assert body["data"]["progress"] == 100
    assert "started_monotonic" not in body["data"]


def test_get_task_missing_returns_404(client):
    """任务不存在应返回 404。"""
    response = client.get("/api/lottery/tasks/none")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------------------


def test_run_filter_task_success(monkeypatch):
    """筛选后台任务成功应落 completed 与结果。"""
    _FakeLotteryService.reset()
    service = _FakeLotteryService()
    monkeypatch.setattr(lottery_module, "get_service", lambda: service)
    monkeypatch.setattr(lottery_module, "fetch_target_metadata", _fake_fetch_target)
    monkeypatch.setattr(lottery_module, "_tasks", {})
    task_id = lottery_module._create_task("filter")

    request = type("Req", (), {"target": "BV1xx411c7mD", "focus_template": None})()
    asyncio.run(lottery_module._run_filter_task(task_id, request))

    task = lottery_module._tasks[task_id]
    assert task["status"] == "completed"
    assert task["progress"] == 100
    assert task["result"] == {"real": [], "fake": []}


def test_run_filter_task_failure(monkeypatch):
    """筛选后台任务异常应落 failed 与错误文案。"""
    _FakeLotteryService.reset()
    _FakeLotteryService.state["filter_error"] = RuntimeError("采集失败")
    monkeypatch.setattr(lottery_module, "get_service", lambda: _FakeLotteryService())
    monkeypatch.setattr(lottery_module, "fetch_target_metadata", _fake_fetch_target)
    monkeypatch.setattr(lottery_module, "_tasks", {})
    task_id = lottery_module._create_task("filter")

    request = type("Req", (), {"target": "BV1xx411c7mD", "focus_template": None})()
    asyncio.run(lottery_module._run_filter_task(task_id, request))

    task = lottery_module._tasks[task_id]
    assert task["status"] == "failed"
    assert "筛选失败" in task["message"]


def test_run_draw_task_rejects_reversed_date_range(monkeypatch):
    """开始日期晚于结束日期时应失败，且不调用抽奖。"""
    from datetime import date

    _FakeLotteryService.reset()
    monkeypatch.setattr(lottery_module, "get_service", lambda: _FakeLotteryService())
    monkeypatch.setattr(lottery_module, "fetch_target_metadata", _fake_fetch_target)
    monkeypatch.setattr(lottery_module, "_tasks", {})
    task_id = lottery_module._create_task("draw")

    request = type(
        "Req",
        (),
        {
            "target": "BV1xx411c7mD",
            "winner_count": 1,
            "unique_users": True,
            "vip_only": False,
            "min_level": None,
            "real_only": False,
            "include_indeterminate": False,
            "focus_template": None,
            "date_start": date(2030, 5, 10),
            "date_end": date(2030, 5, 1),
        },
    )()
    asyncio.run(lottery_module._run_draw_task(task_id, request))

    task = lottery_module._tasks[task_id]
    assert task["status"] == "failed"
    assert "开始日期不能晚于结束日期" in task["message"]
    assert "draw_kwargs" not in _FakeLotteryService.state


def test_run_draw_task_success_updates_latest_winners(monkeypatch):
    """抽奖成功应落 completed 并把中奖名单写入快照。"""
    _FakeLotteryService.reset()
    monkeypatch.setattr(lottery_module, "get_service", lambda: _FakeLotteryService())
    monkeypatch.setattr(lottery_module, "fetch_target_metadata", _fake_fetch_target)
    monkeypatch.setattr(lottery_module, "_tasks", {})
    monkeypatch.setattr(lottery_module, "_latest_winners", [])
    task_id = lottery_module._create_task("draw")

    request = type(
        "Req",
        (),
        {
            "target": "BV1xx411c7mD",
            "winner_count": 1,
            "unique_users": True,
            "vip_only": True,
            "min_level": 2,
            "real_only": True,
            "include_indeterminate": True,
            "focus_template": "侧重点",
            "date_start": None,
            "date_end": None,
        },
    )()
    asyncio.run(lottery_module._run_draw_task(task_id, request))

    task = lottery_module._tasks[task_id]
    assert task["status"] == "completed"
    assert lottery_module._latest_winners == [{"uid": 9, "uname": "中奖者"}]

    kwargs = _FakeLotteryService.state["draw_kwargs"]
    assert kwargs["vip_only"] is True
    assert kwargs["min_level"] == 2
    assert kwargs["date_start"] is None
    assert kwargs["date_end"] is None
