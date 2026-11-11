"""伪人率融合与分档（纯函数）。

融合（设计稿 §5.5）：
    whitelist                 → final = 0
    AI 已判（ai_score 非 None）→ final = round((1-w)·rule + w·ai)，有硬命中时 max(final, hard_hit_floor)
    AI 未判 / 失败             → final = rule

分档按顺序判定（设计稿 §5.3.6）：whitelist → bot → unknown → suspect → human。
judge、store 读出后都必须经 ``rescore`` 计算，保证全局分档一致。
"""
from typing import Any, Dict, Optional, Tuple

from .contracts import (
    SOURCE_HARD_RULE,
    SOURCE_RULE,
    SOURCE_RULE_LLM,
    SOURCE_WHITELIST,
    TIER_BOT,
    TIER_HUMAN,
    TIER_SUSPECT,
    TIER_UNKNOWN,
    TIER_WHITELIST,
    PatrolSettings,
)


def fuse(rule_score: int, ai_score: Optional[int], *, hard: bool, whitelisted: bool,
         s: PatrolSettings) -> Tuple[int, str]:
    """返回 (final_score, score_source)。"""
    if whitelisted:
        return 0, SOURCE_WHITELIST
    rule_score = max(0, min(99, int(rule_score or 0)))
    if ai_score is not None:
        ai = max(0, min(100, int(ai_score)))
        final = int(round((1 - s.fusion_weight_llm) * rule_score + s.fusion_weight_llm * ai))
        if hard:
            final = max(final, s.hard_hit_floor)
        return max(0, min(100, final)), SOURCE_RULE_LLM
    return rule_score, SOURCE_HARD_RULE if hard else SOURCE_RULE


def is_disagreement(rule_score: int, ai_score: Optional[int], s: PatrolSettings) -> bool:
    """规则分与 AI 分明显不一致；AI 高但规则极低时也算（跨条注入兜底）。"""
    if ai_score is None:
        return False
    rule_score = int(rule_score or 0)
    ai = int(ai_score)
    return abs(rule_score - ai) >= s.disagreement_gap or (ai >= s.bot_threshold and rule_score < s.gray_low)


def tier_of(final_score: int, *, source: str, has_focus_hit: bool, content_len: int, profile_missing: bool,
            ai_judged_null: bool, rule_score: int, s: PatrolSettings) -> str:
    """按设计稿顺序分档。"""
    if source == SOURCE_WHITELIST:
        return TIER_WHITELIST
    if final_score >= s.bot_threshold:
        return TIER_BOT
    if not has_focus_hit and (content_len <= 4 or profile_missing or (ai_judged_null and rule_score < s.gray_low)):
        return TIER_UNKNOWN
    if final_score >= s.suspect_threshold:
        return TIER_SUSPECT
    return TIER_HUMAN


def rescore(item: Dict[str, Any], s: PatrolSettings, *, ai_judged_null: bool = False) -> Dict[str, Any]:
    """根据 item 现有字段重算 final_score / tier / score_source / disagreement。

    Args:
        item: 需含 rule_score, ai_score, hard_groups, has_focus_hit, content, level，
            可选 source（规则阶段给出的 "whitelist" 标记）或 score_source。
        s: 巡检配置。
        ai_judged_null: AI 已判但给出 null（证据不足）。

    Returns:
        需要更新到 item / 数据库的字段 dict。
    """
    whitelisted = (item.get("source") == SOURCE_WHITELIST) or (item.get("score_source") == SOURCE_WHITELIST)
    rule_score = int(item.get("rule_score") or 0)
    ai_score = item.get("ai_score")
    hard = bool(item.get("hard_groups"))
    final, source = fuse(rule_score, ai_score, hard=hard, whitelisted=whitelisted, s=s)
    tier = tier_of(
        final, source=source, has_focus_hit=bool(item.get("has_focus_hit")),
        content_len=len((item.get("content") or "").strip()), profile_missing=item.get("level") is None,
        ai_judged_null=ai_judged_null, rule_score=rule_score, s=s,
    )
    return {
        "final_score": final,
        "score_source": source,
        "tier": tier,
        "disagreement": False if whitelisted else is_disagreement(rule_score, ai_score, s),
    }
