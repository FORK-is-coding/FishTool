"""抽奖证据与展示契约测试（FishTool 03 · 批 4 · 规格 §7.1 / §7.3 / 验收 14）。

分两部分：
1. Python 证据门禁：关键资料缺失 -> indeterminate + 理由不足，不落 real。
2. 前端展示契约（静态源检查）：空值显示“未知”而非 0，时间轴缺失不画 0。
"""
from __future__ import annotations

from pathlib import Path

from modules.lottery.analyzer import (
    evaluate_evidence_quality,
    heuristic_classify,
    make_indeterminate_result,
)

_JS_DIR = Path(__file__).resolve().parents[1] / "web" / "frontend" / "static" / "js"


def test_evidence_gate_blocks_missing_key_data() -> None:
    """四关键字段缺一即证据不足，返回稳定原因码。"""
    profile = {
        "uid": 7,
        "level": 4,
        "recent_activity_count": 3,
        "lottery_repost_ratio": 0.5,
        "video_count": None,
    }
    ok, reasons = evaluate_evidence_quality(profile)
    assert ok is False
    assert "missing_video_count" in reasons


def test_heuristic_missing_evidence_is_indeterminate_not_real() -> None:
    """关键资料全缺时不得判 real，而是 indeterminate + 证据不足来源。"""
    result = heuristic_classify({
        "uid": 7,
        "level": None,
        "recent_activity_count": None,
        "lottery_repost_ratio": None,
        "video_count": None,
    })
    assert result["classification"] == "indeterminate"
    assert result["source"] == "evidence_gate"
    assert result["confidence"] is None
    assert result["reason_codes"]


def test_indeterminate_result_reason_is_insufficient_not_real() -> None:
    """未知分类的理由必须是“资料不足”，不能写已确认真人。"""
    result = make_indeterminate_result(9, ["missing_level"])
    assert result["classification"] == "indeterminate"
    assert "不足" in result["reasons"][0]


def test_lottery_js_has_known_formatters() -> None:
    """app.lottery.js 用质量感知 formatter，不再把空值当 0。"""
    source = (_JS_DIR / "app.lottery.js").read_text(encoding="utf-8")
    assert "function knownNumeric(" in source
    assert "function knownRatioPercent(" in source
    assert "投稿 ${knownNumeric(profile.video_count)}" in source
    assert "Number(profile.video_count) || 0" not in source


def test_hotspot_timeline_does_not_fill_missing_with_zero() -> None:
    """app.hotspot.js 时间轴缺失保持 null，不落 0。"""
    source = (_JS_DIR / "app.hotspot.js").read_text(encoding="utf-8")
    assert "p[m.key] || 0" not in source
    assert "p[m.key] === undefined ? null : p[m.key]" in source


def test_up_js_has_nullable_business_formatter() -> None:
    """app.up.js 新增可空业务 formatter，且不误伤词云权重用的 diagnosisNumber。"""
    source = (_JS_DIR / "app.up.js").read_text(encoding="utf-8")
    assert "function diagnosisNullableNumber(" in source
    assert "diagnosisNumber(count, 1)" in source
