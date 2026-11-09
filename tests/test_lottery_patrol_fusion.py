"""伪人率融合与分档。"""
import pytest

from modules.lottery.patrol.contracts import build_settings
from modules.lottery.patrol.fusion import fuse, is_disagreement, rescore, tier_of

S = build_settings({})


def test_whitelist_is_zero():
    assert fuse(90, 99, hard=True, whitelisted=True, s=S) == (0, "whitelist")


def test_rule_only_sources():
    assert fuse(40, None, hard=False, whitelisted=False, s=S) == (40, "rule")
    assert fuse(50, None, hard=True, whitelisted=False, s=S) == (50, "hard_rule")


def test_weighted_fusion():
    assert fuse(60, 90, hard=False, whitelisted=False, s=S) == (round(0.45 * 60 + 0.55 * 90), "rule+llm")


def test_hard_floor_only_when_ai_judged_and_not_into_bot():
    final, source = fuse(50, 0, hard=True, whitelisted=False, s=S)
    assert final == 60 and source == "rule+llm"
    assert final < S.bot_threshold
    # 未经 AI 时不抬保底
    assert fuse(50, None, hard=True, whitelisted=False, s=S)[0] == 50


@pytest.mark.parametrize("rule,ai,expected", [
    (10, 70, True),    # 差 60
    (60, 20, False),   # 差 40
    (5, 80, True),     # AI 高、规则 < 20（跨条注入兜底）
    (30, 80, True),    # 差 50
    (30, None, False),
])
def test_disagreement(rule, ai, expected):
    assert is_disagreement(rule, ai, S) is expected


def _tier(final, **kw):
    base = dict(source="rule", has_focus_hit=False, content_len=20, profile_missing=False,
                ai_judged_null=False, rule_score=final, s=S)
    base.update(kw)
    return tier_of(final, **base)


def test_tier_order():
    assert _tier(99, source="whitelist") == "whitelist"
    assert _tier(80) == "bot"
    assert _tier(50) == "suspect"
    assert _tier(10) == "human"
    assert _tier(10, content_len=2) == "unknown"
    assert _tier(10, profile_missing=True) == "unknown"
    assert _tier(10, ai_judged_null=True, rule_score=5) == "unknown"


def test_focus_hit_short_text_ranks_by_score():
    assert _tier(69, has_focus_hit=True, content_len=2) == "suspect"


def test_threshold_change_moves_tier():
    s2 = build_settings({"thresholds": {"bot": 60, "suspect": 40, "gray_low": 20, "gray_high": 85}, "soft_cap": 55,
                         "hard_hit_floor": 50})
    assert tier_of(65, source="rule", has_focus_hit=True, content_len=20, profile_missing=False,
                   ai_judged_null=False, rule_score=65, s=s2) == "bot"


def test_rescore_end_to_end():
    item = {"rule_score": 5, "ai_score": 95, "hard_groups": [], "has_focus_hit": False, "content": "正常评论内容",
            "level": 4}
    out = rescore(item, S)
    assert out["score_source"] == "rule+llm" and out["disagreement"] is True
    white = rescore({**item, "source": "whitelist"}, S)
    assert white["tier"] == "whitelist" and white["final_score"] == 0 and white["disagreement"] is False
