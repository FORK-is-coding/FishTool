"""自诊报告导出：AI 调研两节、数据采集时间、页面快照 uid 校验与排名来源（#14）。

覆盖：
- ReportGenerator：ai_report 透传到 Markdown / HTML，两节位于「改进建议」之前；
  AI 失败或缺失时写明未生成且不报错；页头含数据采集时间（兼容 ISO 字符串）。
- /api/analysis/report-html：页面快照 uid 与请求不一致 → 409；
  不采信客户端传入的 creator_ranking，按 benchmark_run_id 由服务端读取；ai_report 透传。
- 前端 app.up.js：导出 payload 携带 diagnosisState.result.ai_report。

策略：报告生成走真实 ReportGenerator（markdown2 已在 requirements）；接口层用 TestClient
挂真实 analysis router，生成器保持真实，只替身排名服务读取。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.self_diagnosis.report_generator import ReportGenerator, _format_timestamp
from web.routers import analysis as analysis_module

SELF_DATA = {
    "uid": 7,
    "fetched_at": "2026-10-26T14:30:05.123456",
    "basic_info": {"name": "示例UP主", "level": 6},
    "fan_stats": {"follower": 1000, "following": 10},
    "video_stats": {"total_count": 3, "total_play": 9000, "avg_play": 3000},
    "post_rhythm": {"videos_per_week": 1.5},
    "engagement_metrics": {"play_to_fans_ratio": 30.0, "comment_to_play_ratio": 1.0},
    "data_availability": {"basic_info": True},
}
AI_OK = {
    "success": True,
    "report": "教程类内容是主要增长来源。",
    "commentary": "建议固定周更教程栏目。",
    "highlights": ["教程收藏率高"],
    "model": "gpt-test",
    "generated_at": 1792999800,
}


# ---------------------------------------------------------------- ReportGenerator


def test_markdown_includes_ai_sections_before_suggestions(tmp_path):
    """AI 调研报告 / 运营点评两节出现，且位于「改进建议」之前，含要点与溯源。"""
    md = ReportGenerator(str(tmp_path)).generate_markdown_report(SELF_DATA, ai_report=AI_OK)

    assert "## 🤖 AI 调研报告" in md and "## 📝 运营点评" in md
    assert md.index("## 🤖 AI 调研报告") < md.index("## 📝 运营点评") < md.index("## 💡 改进建议")
    assert "教程类内容是主要增长来源。" in md
    assert "- 教程收藏率高" in md
    assert "gpt-test" in md


@pytest.mark.parametrize("ai_report", [{"success": False, "message": "LLM 未配置"}, {}, "坏数据"])
def test_markdown_ai_sections_degrade_without_error(tmp_path, ai_report):
    """AI 失败、空结果或类型不对时两节写明未生成，不抛错。"""
    md = ReportGenerator(str(tmp_path)).generate_markdown_report(SELF_DATA, ai_report=ai_report)

    assert md.count("ℹ️ **未生成**") == 2
    if isinstance(ai_report, dict) and ai_report.get("message"):
        assert "LLM 未配置" in md


def test_markdown_without_ai_report_has_no_ai_sections(tmp_path):
    """未传 ai_report（旧调用方）时不出现 AI 两节，保持原报告结构。"""
    md = ReportGenerator(str(tmp_path)).generate_markdown_report(SELF_DATA)
    assert "AI 调研报告" not in md


def test_markdown_header_shows_fetched_at(tmp_path):
    """页头标明数据采集时间（ISO 字符串统一格式化）。"""
    md = ReportGenerator(str(tmp_path)).generate_markdown_report(SELF_DATA)
    assert "**数据采集时间**: 2026-10-26 14:30:05" in md


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "未记录"),
        ("", "未记录"),
        (True, "未记录"),
        ("2026-10-26T14:30:05", "2026-10-26 14:30:05"),
        ("不是时间", "不是时间"),
        ("nan", "nan"),
    ],
)
def test_format_timestamp_edge_cases(value, expected):
    """时间格式化对缺失 / 布尔 / 非法字符串降级，不抛错。"""
    assert _format_timestamp(value) == expected


def test_html_report_passes_ai_report(tmp_path):
    """HTML 报告（桌面 PDF 与浏览器打印共用）同样包含 AI 两节。"""
    html = ReportGenerator(str(tmp_path)).generate_html_report(SELF_DATA, ai_report=AI_OK)
    assert "AI 调研报告" in html and "建议固定周更教程栏目。" in html


# ---------------------------------------------------------------- /report-html


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """挂真实 analysis router；生成器真实，输出目录指向 tmp。"""
    monkeypatch.setattr(analysis_module, "ReportGenerator", lambda *a, **k: ReportGenerator(str(tmp_path)))
    app = FastAPI()
    app.include_router(analysis_module.router, prefix="/api")
    return TestClient(app)


def test_report_html_rejects_snapshot_of_other_uid(client):
    """页面快照属于别的账号时 409，避免把上一个账号的诊断导出成当前账号。"""
    response = client.post("/api/analysis/report-html", json={"uid": 8, "self_data": SELF_DATA})
    assert response.status_code == 409
    assert response.json()["detail"] == "diagnosis_uid_mismatch"


def test_report_html_ignores_client_supplied_ranking(client, monkeypatch):
    """不采信客户端传入的 creator_ranking：未带 run id 时报告不含同行排名。"""
    forged = {"schema": 3, "valid_count": 5, "leaderboard": [{"uid": 7, "rank": 1}]}
    response = client.post(
        "/api/analysis/report-html",
        json={"uid": 7, "self_data": SELF_DATA, "creator_ranking": forged},
    )
    assert response.status_code == 200
    assert "同行排名" not in response.json()["data"]["html"]


def test_report_html_loads_ranking_by_run_id_with_uid_check(client, monkeypatch):
    """带 run id 时由服务端读取冻结结果，并沿用跨 UID 拒绝。"""
    calls = []

    def fake_load(uid, run_id):
        calls.append((uid, run_id))
        raise analysis_module.HTTPException(status_code=409, detail="benchmark_run_target_mismatch")

    monkeypatch.setattr(analysis_module, "_load_creator_ranking", fake_load)
    response = client.post(
        "/api/analysis/report-html",
        json={"uid": 7, "self_data": SELF_DATA, "benchmark_run_id": "run-x"},
    )
    assert calls == [(7, "run-x")]
    assert response.status_code == 409


def test_report_html_includes_page_ai_report(client):
    """页面 ai_report 随导出写入报告。"""
    response = client.post(
        "/api/analysis/report-html", json={"uid": 7, "self_data": SELF_DATA, "ai_report": AI_OK}
    )
    assert response.status_code == 200
    html = response.json()["data"]["html"]
    assert "教程类内容是主要增长来源。" in html


# ---------------------------------------------------------------- 前端契约


def test_frontend_export_payload_carries_ai_report():
    """导出 payload 必须携带页面上的 ai_report。"""
    source = Path("web/frontend/static/js/app.up.js").read_text(encoding="utf-8")
    body = source.split("async function exportDiagnosisReport", 1)[1].split("\n}\n", 1)[0]
    assert "ai_report: diagnosisState.result.ai_report" in body
