"""机会反馈前端静态契约（app.events.js）。

后端 ``FeedbackRequest`` 必填 ``expected_revision``、``topic_id`` 最少 1 字符；
前端原先不带 ``expected_revision``、无已保存选题时仍以 ``topic_id: ''`` 渲染反馈按钮，
导致采用 / 不采用 / 已发布必定 422。仓库无 JS 测试框架，按惯例读取源码做静态断言。
"""
from __future__ import annotations

from pathlib import Path

EVENTS_JS = Path(__file__).resolve().parents[1] / "web" / "frontend" / "static" / "js" / "app.events.js"


def _function_body(source: str, name: str) -> str:
    """按花括号配对截取 ``function name(...) { ... }`` 的函数体。"""
    start = source.index(f"function {name}(")
    open_at = source.index("{", source.index(")", start))
    depth = 0
    for pos in range(open_at, len(source)):
        char = source[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[open_at:pos + 1]
    raise AssertionError(f"函数 {name} 花括号未闭合")


def _js() -> str:
    """读取 app.events.js（UTF-8）。"""
    return EVENTS_JS.read_text(encoding="utf-8")


def test_submit_feedback_sends_expected_revision_from_current_run():
    """反馈请求体带 expected_revision，取自 __eventCurrentRun，且成功后回写新 revision。"""
    body = _function_body(_js(), "submitOpportunityFeedback")
    assert "expected_revision" in body
    assert "__eventCurrentRun" in body
    # run 不一致时不盲发：比对按钮所属 runId 与当前 run。
    assert "opportunity_run_id !== runId" in body
    # 成功后用响应的新 revision 更新，避免第二次反馈 409。
    assert ".revision = data.revision" in body


def test_render_opportunity_list_hides_feedback_without_topic():
    """无已保存选题（topicId 为空）时不渲染反馈按钮，改为提示文案。"""
    body = _function_body(_js(), "renderOpportunityList")
    assert "请先生成并保存选题后再反馈" in body
    branch_at = body.index("${topicId ?")
    # 三个反馈按钮都在 topicId 条件分支之后（即只在有选题时渲染）。
    first_feedback_at = body.index("submitOpportunityFeedback(")
    assert branch_at < first_feedback_at
    hint_at = body.index("请先生成并保存选题后再反馈")
    assert body.rindex("submitOpportunityFeedback(") < hint_at
