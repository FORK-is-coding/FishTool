"""web.routers.hotspot.deps 依赖与辅助函数测试（第4批 · web 段）。

覆盖对象：
- 全局单例 get_api / get_llm_client（延迟初始化与失败降级）
- _update_tag_cloud_task（进度与剩余时间估算，含缺失任务短路）
- _run_tag_cloud_task（后台采集成功/失败落状态）

测试策略：
- 通过 monkeypatch 替换模块级单例与协作类，使用契约级假对象，不使用 AsyncMock。
- 仓库未安装 pytest-asyncio，async 用例统一用 ``asyncio.run(...)`` 驱动。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from web.routers.hotspot import deps as deps_module


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    """每个用例前重置单例与任务表，避免跨用例串味。"""
    monkeypatch.setattr(deps_module, "_api", None)
    monkeypatch.setattr(deps_module, "_llm_client", None)
    monkeypatch.setattr(deps_module, "_tag_cloud_tasks", {})
    yield


# ---------------------------------------------------------------------------
# get_api 单例
# ---------------------------------------------------------------------------


class _FakeApi:
    """占位 BilibiliAPI 替身。"""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def test_get_api_creates_singleton_with_cookie_pool(monkeypatch):
    """首次调用应构造 API 并注入 cookie 池，二次调用复用同一实例。"""
    pool = object()
    captured = {}

    def fake_factory(**kwargs):
        """记录构造参数并返回替身。"""
        captured.update(kwargs)
        return _FakeApi(**kwargs)

    monkeypatch.setattr(deps_module, "BilibiliAPI", fake_factory)
    monkeypatch.setattr(deps_module, "get_cookie_pool", lambda: pool)

    first = deps_module.get_api()
    second = deps_module.get_api()

    assert first is second
    assert captured["cookie_pool"] is pool


# ---------------------------------------------------------------------------
# get_llm_client 降级
# ---------------------------------------------------------------------------


def test_get_llm_client_returns_cached_instance(monkeypatch):
    """构造成功时缓存实例，二次调用不再构造。"""
    calls = {"count": 0}
    sentinel = object()

    def fake_client():
        """统计构造次数并返回哨兵。"""
        calls["count"] += 1
        return sentinel

    monkeypatch.setattr(deps_module, "LLMClient", fake_client)
    assert deps_module.get_llm_client() is sentinel
    assert deps_module.get_llm_client() is sentinel
    assert calls["count"] == 1


def test_get_llm_client_degrades_to_none_on_error(monkeypatch):
    """构造失败时应降级为 None，且不抛异常。"""

    def boom():
        """模拟未配置密钥导致的构造失败。"""
        raise RuntimeError("no key")

    monkeypatch.setattr(deps_module, "LLMClient", boom)
    assert deps_module.get_llm_client() is None


# ---------------------------------------------------------------------------
# _update_tag_cloud_task
# ---------------------------------------------------------------------------


def test_update_task_missing_id_is_noop():
    """任务不存在时应静默返回，不抛 KeyError。"""
    deps_module._update_tag_cloud_task("missing", "stage", 50, "msg")


def test_update_task_sets_fields_and_estimates_seconds():
    """progress>8 时应写入阶段字段并给出剩余秒数估算。"""
    deps_module._tag_cloud_tasks["t1"] = {
        "started_monotonic": time.monotonic(),
        "progress": 2,
        "stage": "queued",
        "message": "等待",
        "estimated_seconds": None,
    }

    deps_module._update_tag_cloud_task("t1", "collecting", 50, "采集中")

    task = deps_module._tag_cloud_tasks["t1"]
    assert task["stage"] == "collecting"
    assert task["progress"] == 50
    assert task["message"] == "采集中"
    assert isinstance(task["estimated_seconds"], int)
    assert task["estimated_seconds"] >= 0


def test_update_task_omits_estimate_when_progress_low():
    """progress<=8 时不给剩余时间估算（样本不足）。"""
    deps_module._tag_cloud_tasks["t2"] = {
        "started_monotonic": time.monotonic(),
        "progress": 2,
        "stage": "queued",
        "message": "等待",
        "estimated_seconds": 999,
    }

    deps_module._update_tag_cloud_task("t2", "start", 5, "刚开始")
    assert deps_module._tag_cloud_tasks["t2"]["estimated_seconds"] is None


# ---------------------------------------------------------------------------
# _run_tag_cloud_task
# ---------------------------------------------------------------------------


class _FakeGenerator:
    """契约级词云生成器替身：回调一次进度后返回结果或抛错。"""

    def __init__(self, api, result=None, error=None) -> None:
        self.api = api
        self._result = result if result is not None else {"word_frequency": {"原神": 3}}
        self._error = error

    async def generate_cloud_data(self, zone_name, limit, top_n, progress_callback=None):
        """按状态选择回调进度并返回结果，或抛出注入异常。"""
        if progress_callback is not None:
            progress_callback("collecting", 60, "采集中")
        if self._error is not None:
            raise self._error
        return self._result


def _prepare_task(task_id: str = "task-1") -> None:
    """写入一条初始任务状态。"""
    deps_module._tag_cloud_tasks[task_id] = {
        "task_id": task_id,
        "status": "running",
        "stage": "queued",
        "progress": 2,
        "message": "任务已创建",
        "estimated_seconds": None,
        "started_monotonic": time.monotonic(),
        "result": None,
    }


def test_run_task_completes_and_stores_result(monkeypatch):
    """生成成功时任务应标记 completed 并写入结果。"""
    _prepare_task()
    monkeypatch.setattr(deps_module, "_api", object())
    monkeypatch.setattr(deps_module, "TagCloudGenerator", _FakeGenerator)
    monkeypatch.setattr(deps_module, "get_api", lambda: object())

    request = type("Req", (), {"zone_name": "游戏", "limit": 10, "top_n": 5})()
    asyncio.run(deps_module._run_tag_cloud_task("task-1", request))

    task = deps_module._tag_cloud_tasks["task-1"]
    assert task["status"] == "completed"
    assert task["progress"] == 100
    assert task["estimated_seconds"] == 0
    assert task["result"] == {"word_frequency": {"原神": 3}}


def test_run_task_marks_failed_on_exception(monkeypatch):
    """生成抛错时任务应标记 failed 并带错误文案。"""
    _prepare_task("task-2")

    def failing_generator(api):
        """返回一个必定失败的生成器替身。"""
        return _FakeGenerator(api, error=RuntimeError("采集炸了"))

    monkeypatch.setattr(deps_module, "TagCloudGenerator", failing_generator)
    monkeypatch.setattr(deps_module, "get_api", lambda: object())

    request = type("Req", (), {"zone_name": "游戏", "limit": 10, "top_n": 5})()
    asyncio.run(deps_module._run_tag_cloud_task("task-2", request))

    task = deps_module._tag_cloud_tasks["task-2"]
    assert task["status"] == "failed"
    assert "生成词云失败" in task["message"]
    assert task["estimated_seconds"] == 0
