"""web.routers.analysis UP分析/账号自诊接口测试（第4批 · web 段）。

覆盖对象：
- _update_analysis_task / _run_analysis_task（内部辅助与后台任务）
- GET  /categories 、POST /category-top-ups
- POST /analyze-up 、POST /analyze-up/tasks 、GET /analyze-up/tasks/{task_id}
- POST /self-diagnosis 、POST /export-report 、GET /llm-status

验证维度：
内置分区表 / 头部UP主抓取成功与失败 / UP主分析 LLM 降级 / 请求参数非法 400 /
后台任务状态生命周期 / 自诊 benchmark 可选 / 报告导出格式分支 / LLM 状态探测。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- 抓取/LLM/报告生成全部用契约级假对象，避免真实网络与文件写入。
- 仓库未安装 pytest-asyncio，async 用例统一用 asyncio.run 驱动。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.exceptions import ValidationError
from web.routers import analysis as analysis_module


class _FakeFetcher:
    """契约级假 UPDataFetcher，支持 async with 协议。"""

    top_ups: list = [{"mid": 1, "name": "UP甲"}]
    up_data: dict = {"uid": 7, "basic_info": {"name": "UP甲"}}
    top_error: Exception | None = None
    up_error: Exception | None = None
    calls: list = []

    def __init__(self, api, rate_limiter) -> None:
        self.api = api
        self.rate_limiter = rate_limiter

    async def __aenter__(self):
        """进入异步上下文。"""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """退出上下文不吞异常。"""
        return False

    async def fetch_category_top_ups(self, category, limit):
        """返回预置头部UP主列表或抛错。"""
        type(self).calls.append(("top", category, limit))
        if type(self).top_error is not None:
            raise type(self).top_error
        return type(self).top_ups

    async def fetch_up_data(self, uid_or_url):
        """返回预置UP主数据或抛错。"""
        type(self).calls.append(("up", uid_or_url))
        if type(self).up_error is not None:
            raise type(self).up_error
        return type(self).up_data


class _FakeStrategyAnalyzer:
    """契约级假策略分析器。"""

    result: dict = {"success": True, "summary": "分析结论"}
    error: Exception | None = None

    async def analyze_strategy(self, up_data):
        """返回预置分析结果或抛错。"""
        if type(self).error is not None:
            raise type(self).error
        return type(self).result


class _FakeSelfAnalyzer:
    """契约级假自诊分析器。"""

    self_data: dict = {"uid": 7, "fan_stats": {"follower": 100}}
    benchmark: dict = {"category": "游戏", "median": 50}
    error: Exception | None = None

    def __init__(self, api, rate_limiter) -> None:
        pass

    async def fetch_self_data(self, uid):
        """返回预置自诊数据或抛错。"""
        if type(self).error is not None:
            raise type(self).error
        return type(self).self_data

    def benchmark_with_category(self, self_data, category):
        """返回预置对比基准。"""
        return type(self).benchmark


class _FakeReportGenerator:
    """契约级假报告生成器。"""

    calls: list = []
    result_error: Exception | None = None

    def generate_pdf_report(self, self_data, benchmark):
        """记录调用并返回预置路径。"""
        type(self).calls.append(("pdf", benchmark))
        if type(self).result_error is not None:
            raise type(self).result_error
        return "/tmp/report.pdf"

    def save_markdown_report(self, self_data, benchmark):
        """记录调用并返回预置路径。"""
        type(self).calls.append(("markdown", benchmark))
        if type(self).result_error is not None:
            raise type(self).result_error
        return "/tmp/report.md"


class _FakeAIReporter:
    """契约级假 AI 调研报告器。"""

    result: dict = {"success": True, "report": "报告"}

    async def generate(self, self_data):
        """返回预置 AI 报告。"""
        return type(self).result


class _FakeLLMClient:
    """契约级假 LLM 客户端。"""

    error: Exception | None = None

    def __init__(self) -> None:
        if type(self).error is not None:
            raise type(self).error
        self.model = "gpt-fake"
        self.api_base = "https://api.fake/v1"


@pytest.fixture()
def client(monkeypatch):
    """挂载 analysis router 的测试客户端，并注入全部假依赖。"""
    _FakeFetcher.top_ups = [{"mid": 1, "name": "UP甲"}]
    _FakeFetcher.up_data = {"uid": 7, "basic_info": {"name": "UP甲"}}
    _FakeFetcher.top_error = None
    _FakeFetcher.up_error = None
    _FakeFetcher.calls = []
    _FakeStrategyAnalyzer.result = {"success": True, "summary": "分析结论"}
    _FakeStrategyAnalyzer.error = None
    _FakeSelfAnalyzer.self_data = {"uid": 7, "fan_stats": {"follower": 100}}
    _FakeSelfAnalyzer.error = None
    _FakeReportGenerator.calls = []
    _FakeReportGenerator.result_error = None
    _FakeAIReporter.result = {"success": True, "report": "报告"}
    _FakeLLMClient.error = None

    monkeypatch.setattr(analysis_module, "init_clients", lambda: None)
    monkeypatch.setattr(analysis_module, "UPDataFetcher", _FakeFetcher)
    monkeypatch.setattr(analysis_module, "StrategyAnalyzer", _FakeStrategyAnalyzer)
    monkeypatch.setattr(analysis_module, "SelfAnalyzer", _FakeSelfAnalyzer)
    monkeypatch.setattr(analysis_module, "ReportGenerator", _FakeReportGenerator)
    monkeypatch.setattr(analysis_module, "AIDiagnosisReporter", _FakeAIReporter)
    monkeypatch.setattr(analysis_module, "LLMClient", _FakeLLMClient)
    monkeypatch.setattr(analysis_module, "_analysis_tasks", {})
    monkeypatch.setattr(analysis_module, "_run_analysis_task", _noop_async)

    app = FastAPI()
    app.include_router(analysis_module.router, prefix="/api")
    return TestClient(app)


async def _noop_async(*args, **kwargs) -> None:
    """后台任务替身：不执行真实流程。"""
    return None


# ---------------------------------------------------------------------------
# GET /categories
# ---------------------------------------------------------------------------


def test_categories_returns_builtin_table(client):
    """应返回内置分区表，且数码与科技共用 tid 188。"""
    body = client.get("/api/analysis/categories").json()
    assert body["success"] is True
    mapping = {item["name"]: item["tid"] for item in body["data"]}
    assert mapping["游戏"] == 4
    assert mapping["数码"] == 188
    assert mapping["科技"] == 188
    assert len(body["data"]) == 17


# ---------------------------------------------------------------------------
# POST /category-top-ups
# ---------------------------------------------------------------------------


def test_category_top_ups_success(client):
    """头部UP主抓取成功应回显分区与数量。"""
    body = client.post(
        "/api/analysis/category-top-ups", json={"category": "游戏", "limit": 10}
    ).json()
    assert body["success"] is True
    assert body["data"]["category"] == "游戏"
    assert body["data"]["count"] == 1
    assert body["data"]["up_list"] == [{"mid": 1, "name": "UP甲"}]


def test_category_top_ups_failure_returns_500(client):
    """抓取异常应转 500。"""
    _FakeFetcher.top_error = RuntimeError("榜单失败")
    response = client.post("/api/analysis/category-top-ups", json={"category": "游戏"})
    assert response.status_code == 500


def test_category_top_ups_rejects_out_of_range_limit(client):
    """limit 超出 1-50 应返回 422。"""
    response = client.post("/api/analysis/category-top-ups", json={"category": "游戏", "limit": 99})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /analyze-up
# ---------------------------------------------------------------------------


def test_analyze_up_success(client):
    """分析成功应返回 UP 数据与 LLM 结论。"""
    body = client.post("/api/analysis/analyze-up", json={"uid_or_url": "123"}).json()
    assert body["success"] is True
    assert body["data"]["up_data"]["uid"] == 7
    assert body["data"]["analysis"]["summary"] == "分析结论"


def test_analyze_up_degrades_when_llm_fails(client):
    """LLM 失败应降级为提示，原始数据仍返回。"""
    _FakeStrategyAnalyzer.error = RuntimeError("no key")
    body = client.post("/api/analysis/analyze-up", json={"uid_or_url": "123"}).json()
    assert body["success"] is True
    assert body["data"]["analysis"]["error"] == "llm_not_configured"
    assert body["data"]["up_data"]["uid"] == 7


def test_analyze_up_value_error_returns_400(client):
    """输入非法应转 400。"""
    _FakeFetcher.up_error = ValueError("无效UID")
    response = client.post("/api/analysis/analyze-up", json={"uid_or_url": "bad"})
    assert response.status_code == 400


def test_analyze_up_validation_error_returns_400(client):
    """ValidationError 也应转 400。"""
    _FakeFetcher.up_error = ValidationError("uid_or_url", "格式错误")
    response = client.post("/api/analysis/analyze-up", json={"uid_or_url": "bad"})
    assert response.status_code == 400


def test_analyze_up_other_error_returns_500(client):
    """其他异常应转 500。"""
    _FakeFetcher.up_error = RuntimeError("抓取失败")
    response = client.post("/api/analysis/analyze-up", json={"uid_or_url": "123"})
    assert response.status_code == 500


def test_analyze_up_http_exception_is_not_wrapped(client):
    """try 内抛出的 HTTPException 必须原样透传，不得被 except Exception 兜底成 500。"""
    _FakeFetcher.up_error = HTTPException(status_code=400, detail="无效UID")
    response = client.post("/api/analysis/analyze-up", json={"uid_or_url": "bad"})
    assert response.status_code == 400
    assert response.json()["detail"] == "无效UID"


# ---------------------------------------------------------------------------
# 任务接口
# ---------------------------------------------------------------------------


def test_start_analysis_task_returns_task_id(client):
    """创建任务应返回 running 任务与 ID。"""
    body = client.post("/api/analysis/analyze-up/tasks", json={"uid_or_url": "123"}).json()
    assert body["success"] is True
    task = analysis_module._analysis_tasks[body["task_id"]]
    assert task["status"] == "running"
    assert task["stage"] == "queued"


def test_get_analysis_task_hides_timing(client, monkeypatch):
    """查询任务应隐藏 started_monotonic。"""
    monkeypatch.setattr(
        analysis_module,
        "_analysis_tasks",
        {
            "t1": {
                "task_id": "t1",
                "status": "completed",
                "stage": "completed",
                "progress": 100,
                "message": "完成",
                "estimated_seconds": 0,
                "started_monotonic": 9.9,
                "result": {"x": 1},
            }
        },
    )
    body = client.get("/api/analysis/analyze-up/tasks/t1").json()
    assert body["data"]["result"] == {"x": 1}
    assert "started_monotonic" not in body["data"]


def test_get_analysis_task_missing_returns_404(client):
    """任务不存在应返回 404。"""
    assert client.get("/api/analysis/analyze-up/tasks/none").status_code == 404


# ---------------------------------------------------------------------------
# POST /self-diagnosis
# ---------------------------------------------------------------------------


def test_self_diagnosis_without_category(client):
    """未指定分区时 benchmark 应为 None，但 AI 报告仍返回。"""
    body = client.post("/api/analysis/self-diagnosis", json={"uid": 7}).json()
    assert body["data"]["self_data"]["uid"] == 7
    assert body["data"]["benchmark"] is None
    assert body["data"]["ai_report"]["report"] == "报告"


def test_self_diagnosis_with_category_adds_benchmark(client):
    """指定分区时应附带对比基准。"""
    body = client.post("/api/analysis/self-diagnosis", json={"uid": 7, "category": "游戏"}).json()
    assert body["data"]["benchmark"]["category"] == "游戏"


def test_self_diagnosis_failure_returns_500(client):
    """自诊异常应转 500。"""
    _FakeSelfAnalyzer.error = RuntimeError("抓取失败")
    response = client.post("/api/analysis/self-diagnosis", json={"uid": 7})
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# POST /export-report
# ---------------------------------------------------------------------------


def test_export_report_markdown_branch(client):
    """默认格式应调用 Markdown 生成器。"""
    body = client.post("/api/analysis/export-report", json={"uid": 7}).json()
    assert body["data"]["format"] == "markdown"
    assert _FakeReportGenerator.calls[-1][0] == "markdown"


def test_export_report_pdf_branch(client):
    """format=pdf 应调用 PDF 生成器。"""
    body = client.post("/api/analysis/export-report", json={"uid": 7, "format": "pdf"}).json()
    assert body["data"]["filepath"] == "/tmp/report.pdf"
    assert _FakeReportGenerator.calls[-1][0] == "pdf"


def test_export_report_failure_returns_500(client):
    """报告生成异常应转 500。"""
    _FakeReportGenerator.result_error = RuntimeError("写盘失败")
    response = client.post("/api/analysis/export-report", json={"uid": 7})
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# GET /llm-status
# ---------------------------------------------------------------------------


def test_llm_status_configured(client):
    """可实例化 LLM 客户端时应返回 configured=True 与模型信息。"""
    body = client.get("/api/analysis/llm-status").json()
    assert body["data"]["configured"] is True
    assert body["data"]["model"] == "gpt-fake"
    assert body["data"]["api_base"] == "https://api.fake/v1"


def test_llm_status_not_configured(client):
    """构造失败时应返回 configured=False 与原因。"""
    _FakeLLMClient.error = RuntimeError("缺少 API Key")
    body = client.get("/api/analysis/llm-status").json()
    assert body["data"]["configured"] is False
    assert "缺少 API Key" in body["data"]["message"]


# ---------------------------------------------------------------------------
# 内部辅助与后台任务
# ---------------------------------------------------------------------------


def test_update_analysis_task_estimates_when_progress_high(monkeypatch):
    """progress>=10 时应写入阶段字段并估算剩余秒数。"""
    monkeypatch.setattr(
        analysis_module,
        "_analysis_tasks",
        {"t": {"started_monotonic": 0.0, "estimated_seconds": None}},
    )
    # 让 monotonic 时钟返回固定值，保证估算确定
    monkeypatch.setattr(analysis_module.time, "monotonic", lambda: 10.0)

    analysis_module._update_analysis_task("t", "fetching", 50, "爬取中")

    task = analysis_module._analysis_tasks["t"]
    assert task["stage"] == "fetching"
    assert task["progress"] == 50
    assert task["estimated_seconds"] == 10


def test_update_analysis_task_skips_estimate_when_progress_low(monkeypatch):
    """progress<10 时不估算剩余时间。"""
    monkeypatch.setattr(
        analysis_module,
        "_analysis_tasks",
        {"t": {"started_monotonic": 0.0, "estimated_seconds": 999}},
    )
    analysis_module._update_analysis_task("t", "start", 5, "刚开始")
    assert analysis_module._analysis_tasks["t"]["estimated_seconds"] is None


def test_update_analysis_task_missing_is_noop(monkeypatch):
    """任务不存在时应静默返回。"""
    monkeypatch.setattr(analysis_module, "_analysis_tasks", {})
    analysis_module._update_analysis_task("missing", "s", 50, "msg")


def test_run_analysis_task_success(monkeypatch):
    """后台分析成功应落 completed 与结果。"""
    _FakeFetcher.up_data = {"uid": 7}
    _FakeStrategyAnalyzer.result = {"success": True}
    monkeypatch.setattr(analysis_module, "init_clients", lambda: None)
    monkeypatch.setattr(analysis_module, "UPDataFetcher", _FakeFetcher)
    monkeypatch.setattr(analysis_module, "StrategyAnalyzer", _FakeStrategyAnalyzer)
    monkeypatch.setattr(analysis_module, "_analysis_tasks", {})
    monkeypatch.setattr(analysis_module, "_update_analysis_task", lambda *a, **k: None)
    analysis_module._analysis_tasks["t"] = {"status": "running", "estimated_seconds": None}

    request = type("Req", (), {"uid_or_url": "123"})()
    asyncio.run(analysis_module._run_analysis_task("t", request))

    task = analysis_module._analysis_tasks["t"]
    assert task["status"] == "completed"
    assert task["result"]["up_data"] == {"uid": 7}


def test_run_analysis_task_degrades_llm_failure(monkeypatch):
    """后台分析 LLM 失败应降级而非整体失败。"""
    _FakeFetcher.up_data = {"uid": 7}
    _FakeStrategyAnalyzer.error = RuntimeError("no key")
    monkeypatch.setattr(analysis_module, "init_clients", lambda: None)
    monkeypatch.setattr(analysis_module, "UPDataFetcher", _FakeFetcher)
    monkeypatch.setattr(analysis_module, "StrategyAnalyzer", _FakeStrategyAnalyzer)
    monkeypatch.setattr(analysis_module, "_analysis_tasks", {})
    monkeypatch.setattr(analysis_module, "_update_analysis_task", lambda *a, **k: None)
    analysis_module._analysis_tasks["t"] = {"status": "running", "estimated_seconds": None}

    request = type("Req", (), {"uid_or_url": "123"})()
    asyncio.run(analysis_module._run_analysis_task("t", request))

    task = analysis_module._analysis_tasks["t"]
    assert task["status"] == "completed"
    assert task["result"]["analysis"]["error"] == "llm_not_configured"


def test_run_analysis_task_failure(monkeypatch):
    """后台分析抓取失败应落 failed。"""
    _FakeFetcher.up_error = RuntimeError("抓取炸了")
    monkeypatch.setattr(analysis_module, "init_clients", lambda: None)
    monkeypatch.setattr(analysis_module, "UPDataFetcher", _FakeFetcher)
    monkeypatch.setattr(analysis_module, "_analysis_tasks", {})
    monkeypatch.setattr(analysis_module, "_update_analysis_task", lambda *a, **k: None)
    analysis_module._analysis_tasks["t"] = {"status": "running", "estimated_seconds": None}

    request = type("Req", (), {"uid_or_url": "123"})()
    asyncio.run(analysis_module._run_analysis_task("t", request))

    task = analysis_module._analysis_tasks["t"]
    assert task["status"] == "failed"
    assert "分析失败" in task["message"]
