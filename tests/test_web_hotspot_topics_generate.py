"""web.routers.hotspot.routes_topics_generate AI 选题生成接口测试（第4批 · web 段）。

覆盖对象：
- POST /topics/generate -> generate_topics

验证维度：
成功透传 / 请求参数透传到生成器 / 生成异常转 500 / LLM 客户端透传。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- 用契约级假 TopicGenerator 与假 api/llm 对象，避免真实 LLM 与网络调用。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers.hotspot import routes_topics_generate


class _FakeTopicGenerator:
    """记录构造与调用参数的假选题生成器。"""

    calls: list[dict] = []
    result: dict = {}
    error: Exception | None = None
    init_args: tuple = ()

    def __init__(self, api, llm_client) -> None:
        type(self).init_args = (api, llm_client)

    async def generate_topics(self, direction, zone_name, count, use_llm):
        """记录参数后返回预置结果或抛出注入异常。"""
        type(self).calls.append(
            {"direction": direction, "zone_name": zone_name, "count": count, "use_llm": use_llm}
        )
        if type(self).error is not None:
            raise type(self).error
        return type(self).result


@pytest.fixture()
def client(monkeypatch):
    """挂载 hotspot router 的测试客户端，并重置假生成器状态。"""
    from web.routers import hotspot

    _FakeTopicGenerator.calls = []
    _FakeTopicGenerator.result = {"topics": [{"title": "选题一"}], "count": 1, "llm_used": True}
    _FakeTopicGenerator.error = None
    _FakeTopicGenerator.init_args = ()

    api_sentinel = object()
    llm_sentinel = object()
    monkeypatch.setattr(routes_topics_generate, "TopicGenerator", _FakeTopicGenerator)
    monkeypatch.setattr(routes_topics_generate, "get_api", lambda: api_sentinel)
    monkeypatch.setattr(routes_topics_generate, "get_llm_client", lambda: llm_sentinel)

    app = FastAPI()
    app.include_router(hotspot.router, prefix="/api/hotspot")
    return TestClient(app)


def test_generate_topics_success_passthrough(client):
    """成功时应原样返回生成结果。"""
    response = client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "游戏攻略", "zone_name": "游戏", "count": 3, "use_llm": True},
    )
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": _FakeTopicGenerator.result}


def test_generate_topics_passes_request_fields(client):
    """direction/zone_name/count/use_llm 应完整透传。"""
    client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "搞怪", "zone_name": "生活", "count": 7, "use_llm": False},
    )
    assert _FakeTopicGenerator.calls[-1] == {
        "direction": "搞怪",
        "zone_name": "生活",
        "count": 7,
        "use_llm": False,
    }


def test_generate_topics_injects_api_and_llm_client(client):
    """生成器应以 get_api/get_llm_client 的返回值构造。"""
    client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "d", "zone_name": "z"},
    )
    api, llm = _FakeTopicGenerator.init_args
    assert api is not None
    assert llm is not None
    assert api is not llm


def test_generate_topics_applies_defaults(client):
    """缺省 count/use_llm 时使用 schema 默认值 10/True。"""
    client.post("/api/hotspot/topics/generate", json={"direction": "d", "zone_name": "z"})
    assert _FakeTopicGenerator.calls[-1]["count"] == 10
    assert _FakeTopicGenerator.calls[-1]["use_llm"] is True


def test_generate_topics_failure_returns_500(client):
    """生成抛错应转为 HTTP 500 并带错误详情。"""
    _FakeTopicGenerator.error = RuntimeError("LLM 超时")
    response = client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "d", "zone_name": "z"},
    )
    assert response.status_code == 500
    assert "生成选题失败" in response.json()["detail"]


# ===========================================================================
# 第三批 g：带键 200/202/409 + 生成账本只读 GET
# ===========================================================================


class _FakeStore:
    """契约级假账本 store：只实现只读 ``read_state``。"""

    def __init__(self, view):
        """保存预置视图。"""
        self._view = view

    def read_state(self, request_id):
        """返回预置账本视图（None 表示尚无该键）。"""
        return self._view


class _FakeGenerationService:
    """契约级假生成服务：记录调用并返回预置结果 / 抛预置异常。"""

    def __init__(self, *, result=None, exc=None, view=None):
        """初始化。"""
        self._result = result
        self._exc = exc
        self.store = _FakeStore(view)
        self.calls = []

    async def generate(self, request):
        """记录请求并按预置行为返回 / 抛错。"""
        self.calls.append(request)
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture()
def keyed(monkeypatch):
    """返回 ``(client, make_service)``：可注入预置带键生成服务。"""
    from web.routers import hotspot
    from web.routers.hotspot import routes_topics_generate

    def _make(service):
        routes_topics_generate.set_generation_service(service)
        app = FastAPI()
        app.include_router(hotspot.router, prefix="/api/hotspot")
        return TestClient(app)

    yield _make
    routes_topics_generate.set_generation_service(None)


def test_generate_keyed_running_returns_202(keyed):
    """运行中 → 202 + generation_request_id + status_url（不能被旧 renderTopicsResult 当已生成）。"""
    from modules.hotspot.topic_generation_service import GenerationRequest  # noqa: F401

    service = _FakeGenerationService(
        result={
            "success": True,
            "accepted": True,
            "status": "running",
            "http_status": 202,
            "generation_request_id": "gen-1",
            "status_url": "/api/hotspot/topics/generation-runs/gen-1",
        }
    )
    client = keyed(service)
    resp = client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "d", "zone_name": "z", "generation_request_id": "gen-1"},
    )
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["status"] == "running"
    assert data["status_url"].endswith("gen-1")


def test_generate_keyed_completed_returns_200(keyed):
    """已完成 → 200 + 完整结果。"""
    service = _FakeGenerationService(
        result={"topics": [{"title": "选题一"}], "saved_ids": [1], "replayed": False}
    )
    client = keyed(service)
    resp = client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "d", "zone_name": "z", "generation_request_id": "gen-2"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["saved_ids"] == [1]


def test_generate_keyed_conflict_returns_409(keyed):
    """同键不同内容 → 409。"""
    from modules.hotspot.topic_generation_service import GenerationKeyConflict

    service = _FakeGenerationService(exc=GenerationKeyConflict("generation_key_conflict"))
    client = keyed(service)
    resp = client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "d", "zone_name": "z", "generation_request_id": "gen-3"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "generation_key_conflict"


def test_generate_keyed_validation_422(keyed):
    """请求非法 → 422（不被吞成 500）。"""
    from modules.hotspot.topic_generation_service import GenerationValidationError

    service = _FakeGenerationService(
        exc=GenerationValidationError("selected_event_ids_requires_run_id")
    )
    client = keyed(service)
    resp = client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "d", "zone_name": "z", "generation_request_id": "gen-4"},
    )
    assert resp.status_code == 422


def test_generate_keyed_unavailable_503(keyed):
    """完成状态未知 → 503（保留同 key）。"""
    from modules.hotspot.topic_generation_service import GenerationUnavailable

    service = _FakeGenerationService(exc=GenerationUnavailable("generation_state_unknown"))
    client = keyed(service)
    resp = client.post(
        "/api/hotspot/topics/generate",
        json={"direction": "d", "zone_name": "z", "generation_request_id": "gen-5"},
    )
    assert resp.status_code == 503


def test_generation_run_get_404_when_absent(keyed):
    """尚无该键 → 404；该 GET 不触发生成。"""
    service = _FakeGenerationService(view=None)
    client = keyed(service)
    resp = client.get("/api/hotspot/topics/generation-runs/gen-absent")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "generation_run_not_found"
    assert service.calls == []  # 未触发生成


def test_generation_run_get_completed_view(keyed):
    """completed 账本 → 200 + 持久 result。"""
    service = _FakeGenerationService(
        view={"generation_request_id": "gen-6", "status": "completed", "result": {"saved_ids": [7]}}
    )
    client = keyed(service)
    resp = client.get("/api/hotspot/topics/generation-runs/gen-6")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "completed"
    assert resp.json()["data"]["result"]["saved_ids"] == [7]


def test_generation_run_interrupted_terminal_view(keyed):
    """其它终态（interrupted）→ 200 + reason_code（轮询可结束）。"""
    service = _FakeGenerationService(
        view={"generation_request_id": "gen-7", "status": "interrupted", "reason_code": "lease_expired"}
    )
    client = keyed(service)
    resp = client.get("/api/hotspot/topics/generation-runs/gen-7")
    assert resp.status_code == 200
    assert resp.json()["data"]["reason_code"] == "lease_expired"


def test_generate_without_key_does_not_touch_service(client, monkeypatch):
    """无键 tag_only 走旧路径，不触碰带键服务（服务保持未装配）。"""
    from web.routers.hotspot import routes_topics_generate

    routes_topics_generate.set_generation_service(None)
    resp = client.post("/api/hotspot/topics/generate", json={"direction": "d", "zone_name": "z"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
