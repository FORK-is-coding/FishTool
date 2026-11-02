"""FishTool 04 · 第三批 b：事件归属 resolver（纯函数，不碰数据库 / 网络）。

依据：
- ``FishTool_04_话题级热点研判与选题决策_02补充执行案(1).md`` **§4（L385-425）**：
  §4.1 事件定义结构 / §4.2 归属规则 8 条 / §4.3 版本化 6 条；
- ``FishTool_04_R5执行规格_第三批b_事件归属与发现围栏.md`` §1.1（逐条抄录）。

本模块只做「文本 → 归属决定」的可解释计算与版本化辅助，**不写库、不发请求**：
- 规范化（规则 1）**不得吃掉版本号 / 日期 / 数字**；
- 关键词一律 **字面量匹配**（``str.find``），**不执行用户正则**（规则 2）；
- **排除规则命中优先**（规则 3）；冲突事件都命中 → 候选**待确认**（规则 3）；
- 只有 **标题 / 标签 / 已取得简介** 的**可定位文本**参与自动接受；
  **评论聚合词只作发现提示**，不单独证明归属（规则 4）；
- ``proposed → accepted/rejected``，用户逐条确认；可启用已审核规则的 strict-auto（规则 5）；
- 每次决定携带 ``rule_version`` / match spans / 证据来源 / ``decision_source`` / ``decision_at``（规则 6）；
- **LLM 语义建议不能自动归并事件或改已有成员**（规则 7）；
- 同 BVID 可属多事件；事件内去重，站内合计以 **BVID 并集**去重，**不能把各事件之和当总体**（规则 8）。
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "EVENT_KINDS",
    "MEMBER_DECISIONS",
    "DECISION_SOURCES",
    "MATCH_DECISIONS",
    "OVERLAP_NOTE",
    "EventDefinition",
    "VideoEvidence",
    "MatchResult",
    "ResolutionResult",
    "MemberFacts",
    "HistoricalBackfillError",
    "normalize_text",
    "evaluate_event",
    "evaluate_events",
    "dedupe_members",
    "merge_member_evidence",
    "union_bvid",
    "site_total",
    "member_facts",
    "guard_no_backfill",
    "pin_assessment",
    "next_assessment_revision",
    "revoke_mismatch",
    "propose_event_draft",
    "merge_events",
    "split_event",
    "three_window_continuous",
    "needs_rewarmup",
    "rule_review_state",
]

# ===========================================================================
# 受控枚举（与 models_hot_event.py 的六表口径保持一致，禁止别处随手拼）
# ===========================================================================

#: §4.1 ``event_kind`` 合法值。
EVENT_KINDS: tuple = (
    "version_release",
    "activity",
    "controversy",
    "guide_demand",
    "trend",
    "other",
)

#: 成员决定状态（与 ``hot_event_members.status`` 一致）。
MEMBER_DECISIONS: tuple = ("proposed", "accepted", "rejected")

#: 归属结论（比成员状态多一个 ``ignored``：连候选都不是）。
MATCH_DECISIONS: tuple = ("accepted", "proposed", "rejected", "ignored")

#: 决定来源（规则 6 要求落库；``auto`` 表示系统提出、待人工确认）。
DECISION_SOURCES: tuple = ("manual", "auto", "strict_rule", "llm_suggestion")

#: 规则 8（§4.2）界面必须说明：事件重叠、各事件 delta 不可加总。
OVERLAP_NOTE: str = "事件可能重叠，站内合计以 BVID 并集去重，各事件之和 ≠ 总体"


class HistoricalBackfillError(ValueError):
    """违反了 §4.3「今日接受成员不得回写昨天当时已知道的趋势」。"""


# ===========================================================================
# 规则 1：规范化（版本号 / 日期 / 数字绝不能被吃掉）
# ===========================================================================

#: HTML 标签剥离（只用于定位可读文本，不做任何“清洗真相”的删除）。
_HTML_TAG_RE = re.compile(r"<[^>]*>")

#: 连续空白折叠为单个空格。
_WS_RE = re.compile(r"\s+")

#: 少量标点归一映射（全角已在 NFKC 处理；此处统一破折号/引号形态）。
_PUNCT_MAP: dict[str, str] = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
}

#: 允许在数字之间保留的分隔符（版本号 ``3.2``、日期 ``2026-09-01``、时间 ``20:30``）。
_NUMERIC_SEPARATORS: frozenset = frozenset({".", "-", "/", ":", "+", "%"})


def normalize_text(raw: Any) -> str:
    """规则 1：Unicode 规范化 + HTML 标签剥离 + 空白/标点规范化。

    关键约束：**绝不能去掉版本号、日期或数字**。因此对「夹在两个数字之间的」数值分隔符
    （``.`` / ``-`` / ``/`` / ``:`` 等）一律保留，只把其余标点折成空白。

    Args:
        raw: 原始文本（可为 None / 非字符串）。

    Returns:
        str: 规范化后的文本（保留所有数字与数值分隔符）。
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    # HTML 实体先解码，再 NFKC（全角数字/标点 → 半角），最后剥标签。
    text = html.unescape(raw)
    text = unicodedata.normalize("NFKC", text)
    text = _HTML_TAG_RE.sub(" ", text)

    out: list[str] = []
    length = len(text)
    for index, ch in enumerate(text):
        ch = _PUNCT_MAP.get(ch, ch)
        prev_ch = text[index - 1] if index > 0 else ""
        next_ch = text[index + 1] if index + 1 < length else ""
        if ch.isalnum() or ch.isspace():
            out.append(" " if ch.isspace() else ch)
        elif ch in _NUMERIC_SEPARATORS and prev_ch.isdigit() and next_ch.isdigit():
            # 版本号 / 日期 / 时间 / 百分比的分隔符：原样保留，绝不合并数字。
            out.append(ch)
        else:
            out.append(" ")
    return _WS_RE.sub(" ", "".join(out)).strip()


def _as_list(value: Any) -> list[str]:
    """把 include/exclude 里的「词条」字段归一为字符串列表。

    Args:
        value: 单个字符串或可迭代字符串（忽略 None / 空串）。

    Returns:
        list[str]: 去重后的字符串列表。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            result.append(item)
    return result


def _as_groups(value: Any) -> list[list[str]]:
    """把「实体组 / 锚点组」归一为 ``[[别名, ...], ...]``。

    一个组命中 = 组内**任一**别名命中；规则 2 要求至少命中一个实体组 + 一个锚点组。

    Args:
        value: 组结构（组内可为字符串或字符串列表）。

    Returns:
        list[list[str]]: 归一后的组列表（忽略空组）。
    """
    if value is None:
        return []
    groups: list[list[str]] = []
    for group in value:
        aliases = _as_list(group)
        if aliases:
            groups.append(aliases)
    return groups


def _literal_spans(haystack: str, keyword: str) -> list[tuple[int, int]]:
    """字面量匹配（规则 2：**不执行用户正则**）。

    Args:
        haystack: 已规范化文本。
        keyword: 关键词（同样按字面量处理，即使含 ``.*`` 等元字符也不当正则）。

    Returns:
        list[tuple[int, int]]: 命中区间 ``(start, end)`` 列表（可为空）。
    """
    spans: list[tuple[int, int]] = []
    if not haystack or not keyword:
        return spans
    start = 0
    while True:
        index = haystack.find(keyword, start)  # 字面量，绝不 re.search
        if index < 0:
            break
        spans.append((index, index + len(keyword)))
        start = index + 1
    return spans


def _keyword_hits(text: str, keywords: Sequence[str], *, group: str = "") -> list[dict]:
    """在已规范化文本上对一组关键词做字面量命中，产出带 span 的证据。

    Args:
        text: 已规范化文本。
        keywords: 关键词序列。
        group: 证据所属组名（便于落库/回看）。

    Returns:
        list[dict]: 每条命中 ``{"group","keyword","spans"}``。
    """
    hits: list[dict] = []
    for keyword in keywords:
        normalized_keyword = normalize_text(keyword)
        spans = _literal_spans(text, normalized_keyword)
        if spans:
            hits.append(
                {
                    "group": group,
                    "keyword": keyword,
                    "spans": [{"start": s, "end": e} for s, e in spans],
                }
            )
    return hits


def _group_hits(text: str, groups: Sequence[Sequence[str]]) -> list[dict]:
    """对「实体组 / 锚点组」逐组命中（组内任取首个命中别名）。

    Args:
        text: 已规范化文本。
        groups: 归一后的组列表。

    Returns:
        list[dict]: 每个命中组的证据 ``{"group_index","alias","spans"}``。
    """
    hits: list[dict] = []
    for group_index, aliases in enumerate(groups):
        for alias in aliases:
            spans = _literal_spans(text, normalize_text(alias))
            if spans:
                hits.append(
                    {
                        "group_index": group_index,
                        "alias": alias,
                        "spans": [{"start": s, "end": e} for s, e in spans],
                    }
                )
                break  # 一个组只记一次，避免同一实体重复计数
    return hits


# ===========================================================================
# §4.1 事件定义 / §4.2 输入输出结构
# ===========================================================================

@dataclass(frozen=True)
class EventDefinition:
    """§4.1 事件定义（**不是一个热词**）。

    Attributes:
        canonical_name: 规范事件名。
        entity_scope: 明确对象（游戏/品牌/人物等）。
        event_kind: :data:`EVENT_KINDS` 之一。
        time_scope: 版本号 / 日期区间 / 具体赛季。
        include_rules: 必需实体组、事件锚点组、别名。
        exclude_rules: 同名歧义、旧版本、无关词。
        source_scope: 允许采集入口（**不等于已验证原始投稿 tid**）。
        expiry_s: 事件**配置复核时间**（不是热度寿命预测）。
        review_at_s: 配置复核提醒时间（不是寿命预测）。
        event_id: 事件 ID（用于归属结果回指）。
        supersedes: 合并/拆分来源事件 ID（历史身份不覆盖）。
    """

    canonical_name: str
    entity_scope: tuple[str, ...] = ()
    event_kind: str = "other"
    time_scope: str = ""
    include_rules: Mapping[str, Any] = field(default_factory=dict)
    exclude_rules: Mapping[str, Any] = field(default_factory=dict)
    source_scope: tuple[str, ...] = ()
    expiry_s: Optional[int] = None
    review_at_s: Optional[int] = None
    event_id: str = ""
    supersedes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验受控枚举，避免非法 ``event_kind`` 混入。"""
        if self.event_kind not in EVENT_KINDS:
            raise ValueError(f"invalid_event_kind:{self.event_kind}")


@dataclass(frozen=True)
class VideoEvidence:
    """待归属视频的可定位证据。

    ``title`` / ``tags`` / ``summary`` 是**可定位文本**，可参与自动接受；
    ``comment_terms`` 是**评论聚合词**，只作发现提示，不单独证明归属（规则 4）。

    Attributes:
        bvid: 视频 BV 号。
        title: 标题。
        tags: 标签。
        summary: 已取得简介。
        comment_terms: 评论聚合词（仅发现提示）。
        sources: 发现来源（E02：同 BVID 三来源 → 一个成员多条证据）。
        published_epoch_s: 视频发布时间（可能为空）。
        owner_mid: UP 主 mid（可能为空）。
        raw_tid: 原始分区 ID（可能为空）。
        discovered_at_s: 首次发现时刻（**不是**发布时间）。
    """

    bvid: str
    title: str = ""
    tags: tuple[str, ...] = ()
    summary: str = ""
    comment_terms: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    published_epoch_s: Optional[int] = None
    owner_mid: Optional[int] = None
    raw_tid: Optional[int] = None
    discovered_at_s: Optional[int] = None


@dataclass
class MatchResult:
    """单事件归属结论（规则 6：决定必须可追溯）。"""

    event_id: str
    matched: bool
    included: bool
    excluded: bool
    decision: str
    decision_source: str
    rule_version: int
    reason_codes: list[str] = field(default_factory=list)
    entity_hits: list[dict] = field(default_factory=list)
    anchor_hits: list[dict] = field(default_factory=list)
    alias_hits: list[dict] = field(default_factory=list)
    exclude_hits: list[dict] = field(default_factory=list)
    match_spans: list[dict] = field(default_factory=list)
    evidence_sources: list[str] = field(default_factory=list)
    comment_hint: bool = False
    conflict: bool = False
    llm_suggested: bool = False
    decision_at_s: Optional[int] = None


@dataclass
class ResolutionResult:
    """跨事件归属汇总（含冲突判定，规则 3）。"""

    results: dict[str, MatchResult] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    llm_recommendation: Any = None
    llm_applied: bool = False

    def accepted_event_ids(self) -> list[str]:
        """返回被自动/人工接受为成员的事件 ID 列表。"""
        return [eid for eid, r in self.results.items() if r.decision == "accepted"]

    def proposed_event_ids(self) -> list[str]:
        """返回待确认（含冲突）的事件 ID 列表。"""
        return [eid for eid, r in self.results.items() if r.decision == "proposed"]


# ===========================================================================
# §4.2 归属主逻辑
# ===========================================================================

def _collect_match_spans(result: MatchResult) -> list[dict]:
    """把各类命中展平成统一 match spans 列表（规则 6 落库用）。"""
    spans: list[dict] = []
    for bucket, kind in (
        (result.entity_hits, "entity"),
        (result.anchor_hits, "anchor"),
        (result.alias_hits, "alias"),
        (result.exclude_hits, "exclude"),
    ):
        for hit in bucket:
            for span in hit.get("spans", []):
                spans.append(
                    {
                        "kind": kind,
                        "keyword": hit.get("alias") or hit.get("keyword"),
                        "source": hit.get("group", ""),
                        "start": span["start"],
                        "end": span["end"],
                    }
                )
    return spans


def evaluate_event(
    evidence: VideoEvidence,
    event_def: EventDefinition,
    *,
    rule_version: int,
    strict_auto: bool = False,
    conflict: bool = False,
    llm_suggestion: Any = None,
    decision_at_s: Optional[int] = None,
) -> MatchResult:
    """对单个事件判定某视频的归属（规则 1-7）。

    Args:
        evidence: 待归属视频证据。
        event_def: 事件定义。
        rule_version: 判定所用规则版本（规则 6）。
        strict_auto: 是否启用「已审核规则的 strict-auto」（规则 5）。
        conflict: 是否与其他事件冲突（冲突则只 proposed，规则 3/5）。
        llm_suggestion: LLM 语义建议（**只记录，不改变结论**，规则 7）。
        decision_at_s: 决定时刻（epoch 秒）。

    Returns:
        MatchResult: 归属结论（含 match spans 与证据来源）。
    """
    # --- 规则 1：规范化（保留数字/版本号/日期）---
    title = normalize_text(evidence.title)
    tags = [normalize_text(tag) for tag in evidence.tags]
    summary = normalize_text(evidence.summary)
    # 规则 4：只有可定位文本参与自动接受。
    locatable = " ".join([title, *tags, summary]).strip()
    comment_text = " ".join(normalize_text(term) for term in evidence.comment_terms)

    include = event_def.include_rules or {}
    exclude = event_def.exclude_rules or {}
    entity_groups = _as_groups(include.get("entity_groups"))
    anchor_groups = _as_groups(include.get("anchor_groups"))
    aliases = _as_list(include.get("aliases"))

    # --- 规则 2：字面量匹配、至少一个实体组 + 一个锚点组 ---
    entity_hits = _group_hits(locatable, entity_groups)
    anchor_hits = _group_hits(locatable, anchor_groups)
    alias_hits = _keyword_hits(locatable, aliases, group="alias")
    included = bool(entity_hits) and bool(anchor_hits)

    # --- 规则 3：排除规则命中优先 ---
    exclude_terms: list[str] = []
    for key in ("same_name_ambiguity", "old_versions", "unrelated_terms"):
        exclude_terms.extend(_as_list(exclude.get(key)))
    exclude_hits = _keyword_hits(locatable, exclude_terms, group="exclude")
    excluded = bool(exclude_hits)

    # --- 规则 4：评论聚合词仅作发现提示（绝不能因此接受）---
    comment_keywords = aliases + [alias for group in entity_groups + anchor_groups for alias in group]
    comment_hint = bool(_keyword_hits(comment_text, comment_keywords, group="comment")) or bool(
        evidence.comment_terms
    )

    # --- 规则 5/3：决定 ---
    reasons: list[str] = []
    if excluded:
        decision, decision_source = "rejected", "auto"
        reasons.append("exclude_hit")
    elif not included:
        decision, decision_source = "ignored", "auto"
        reasons.append("no_entity_hit" if not entity_hits else "no_anchor_hit")
    elif conflict:
        decision, decision_source = "proposed", "auto"
        reasons.append("conflict_multi_event")
    elif strict_auto:
        decision, decision_source = "accepted", "strict_rule"
        reasons.append("strict_rule_accept")
    else:
        decision, decision_source = "proposed", "auto"
        reasons.append("await_manual_confirm")

    if comment_hint and not included:
        reasons.append("comment_hint_only_not_proof")

    result = MatchResult(
        event_id=event_def.event_id or event_def.canonical_name,
        matched=included,
        included=included,
        excluded=excluded,
        decision=decision,
        decision_source=decision_source,
        rule_version=int(rule_version),
        reason_codes=reasons,
        entity_hits=entity_hits,
        anchor_hits=anchor_hits,
        alias_hits=alias_hits,
        exclude_hits=exclude_hits,
        evidence_sources=list(evidence.sources),
        comment_hint=comment_hint,
        conflict=conflict,
        llm_suggested=llm_suggestion is not None,
        decision_at_s=decision_at_s,
    )
    result.match_spans = _collect_match_spans(result)
    return result


def evaluate_events(
    evidence: VideoEvidence,
    event_defs: Iterable[EventDefinition],
    *,
    rule_version: int,
    strict_auto: bool = False,
    llm_suggestion: Any = None,
    decision_at_s: Optional[int] = None,
) -> ResolutionResult:
    """跨事件归属：先各自判定，再做冲突检测（规则 3）。

    「冲突事件都命中」= 有 ≥2 个事件同时 include 命中且未被排除 → 这些事件一律降级为
    ``proposed``（待确认），**不自动接受**。

    Args:
        evidence: 待归属视频证据。
        event_defs: 候选事件定义集合。
        rule_version: 判定规则版本。
        strict_auto: 是否启用 strict-auto。
        llm_suggestion: LLM 建议（只记录，不生效，规则 7）。
        decision_at_s: 决定时刻。

    Returns:
        ResolutionResult: 各事件结论 + 冲突清单 + LLM 建议回执。
    """
    defs = list(event_defs)
    first_pass = [
        evaluate_event(
            evidence,
            event_def,
            rule_version=rule_version,
            strict_auto=strict_auto,
            conflict=False,
            llm_suggestion=llm_suggestion,
            decision_at_s=decision_at_s,
        )
        for event_def in defs
    ]
    conflicts = [r.event_id for r in first_pass if r.included and not r.excluded]
    conflict_ids = set(conflicts) if len(conflicts) > 1 else set()

    results: dict[str, MatchResult] = {}
    for event_def, result in zip(defs, first_pass):
        if event_def.event_id in conflict_ids or event_def.canonical_name in conflict_ids:
            # 冲突：重算一次，强制 proposed（不自动接受）。
            result = evaluate_event(
                evidence,
                event_def,
                rule_version=rule_version,
                strict_auto=False,
                conflict=True,
                llm_suggestion=llm_suggestion,
                decision_at_s=decision_at_s,
            )
        results[result.event_id] = result

    return ResolutionResult(
        results=results,
        conflicts=sorted(conflict_ids),
        llm_recommendation=llm_suggestion,
        # 规则 7：LLM 建议永远不会被自动应用。
        llm_applied=False,
    )


# ===========================================================================
# 规则 8：事件内去重 / 站内 BVID 并集去重 / 不可加总
# ===========================================================================

def dedupe_members(members: Iterable[VideoEvidence]) -> list[VideoEvidence]:
    """事件内按 BVID 去重，保留首次出现（规则 8）。"""
    seen: set[str] = set()
    out: list[VideoEvidence] = []
    for member in members:
        if member.bvid in seen:
            continue
        seen.add(member.bvid)
        out.append(member)
    return out


def merge_member_evidence(evidence_list: Iterable[VideoEvidence]) -> list[dict]:
    """E02：同 BVID 多来源 → **一个成员，多条证据**（事件内去重）。

    Args:
        evidence_list: 同一事件内收集到的证据（可能同一 BVID 多条来源）。

    Returns:
        list[dict]: 每个 BVID 一条 ``{"bvid","sources","evidence"}``。
    """
    by_bvid: dict[str, dict] = {}
    for evidence in evidence_list:
        current = by_bvid.get(evidence.bvid)
        if current is None:
            by_bvid[evidence.bvid] = {
                "bvid": evidence.bvid,
                "sources": list(dict.fromkeys(evidence.sources)),
                "evidence": [evidence],
            }
            continue
        for source in evidence.sources:
            if source not in current["sources"]:
                current["sources"].append(source)
        current["evidence"].append(evidence)
    return list(by_bvid.values())


def union_bvid(event_members: Mapping[str, Iterable[str]]) -> set[str]:
    """站内合计以 **BVID 并集**去重（规则 8）。

    Args:
        event_members: ``event_id -> 该事件成员 BVID 集合``。

    Returns:
        set[str]: 去重后的 BVID 并集。
    """
    union: set[str] = set()
    for bvids in event_members.values():
        union.update(bvids)
    return union


def site_total(event_deltas: Mapping[str, Mapping[str, int]]) -> dict:
    """规则 8：每事件记全额有效 delta，但站内合计取 BVID 并集，**不许把各事件之和当总体**。

    Args:
        event_deltas: ``event_id -> {bvid: delta}``（同一 BVID 的 delta 是同一事实，跨事件相同）。

    Returns:
        dict: 含每事件全额、并集 BVID 数、并集总量、各事件之和，且 ``summable=False``。
    """
    per_event_totals = {eid: sum(delta_map.values()) for eid, delta_map in event_deltas.items()}
    union_map: dict[str, int] = {}
    for delta_map in event_deltas.values():
        for bvid, delta in delta_map.items():
            union_map[bvid] = delta
    return {
        "per_event_totals": per_event_totals,
        "sum_of_events": sum(per_event_totals.values()),
        "union_bvid_count": len(union_map),
        "union_total": sum(union_map.values()),
        "summable": False,
        "note": OVERLAP_NOTE,
    }


# ===========================================================================
# §4.3 版本化 6 条
# ===========================================================================

@dataclass(frozen=True)
class MemberFacts:
    """§4.3 第 1 条：三种不同事实（**必须分开**）。"""

    discovered_at_s: Optional[int]
    accepted_at_s: Optional[int]
    published_epoch_s: Optional[int]

    def are_distinct_fields(self) -> bool:
        """结构断言：三者是独立字段（不因相等就合并成一种事实）。"""
        return True


def member_facts(
    *,
    discovered_at_s: Optional[int],
    accepted_at_s: Optional[int],
    published_epoch_s: Optional[int],
) -> MemberFacts:
    """构造成员的三类时间事实（首次发现 / 归属接受 / 视频发布）。"""
    return MemberFacts(discovered_at_s, accepted_at_s, published_epoch_s)


def guard_no_backfill(*, as_of_s: int, accepted_at_s: Optional[int]) -> None:
    """§4.3 第 2 条：今日接受成员**不得回写**昨天「当时已知道的趋势」。

    Args:
        as_of_s: 历史 assessment 的截止时刻。
        accepted_at_s: 成员归属被接受的时刻（可为空）。

    Raises:
        HistoricalBackfillError: ``accepted_at_s`` 晚于 ``as_of_s``（即用未来信息回填历史）。
    """
    if accepted_at_s is not None and int(accepted_at_s) > int(as_of_s):
        raise HistoricalBackfillError(
            f"backfill_forbidden:accepted_at_s={accepted_at_s}>as_of_s={as_of_s}"
        )


def pin_assessment(
    *,
    event_id: str,
    rule_version: int,
    policy_version: str,
    window_end_s: int,
    member_decisions: Mapping[str, int],
    input_snapshot_id: str,
) -> dict:
    """§4.3 第 3 条：历史 assessment 钉住 rule_version / 成员 decision revision / 输入快照 ID。

    Returns:
        dict: 冻结的 assessment 输入指纹。
    """
    return {
        "event_id": event_id,
        "rule_version": int(rule_version),
        "policy_version": policy_version,
        "window_end_s": int(window_end_s),
        "member_decisions": {k: int(v) for k, v in sorted(member_decisions.items())},
        "input_snapshot_id": input_snapshot_id,
    }


def next_assessment_revision(previous_revision: int) -> int:
    """§4.3 第 4 条：纠错只产生**新** assessment 版本（不原地改旧版）。"""
    return int(previous_revision) + 1


def revoke_mismatch(
    *,
    member_id: int,
    previous_status: str,
    rule_version: int,
    now_s: int,
    reason: str,
    decision_source: str = "manual",
) -> dict:
    """§4.3 第 5 条：错配撤销**保存状态变更**，不删除事实、不静默重算旧推荐。

    Returns:
        dict: 一条**追加**的状态变更记录（调用方据此 append 新成员 revision）。
    """
    if previous_status not in MEMBER_DECISIONS:
        raise ValueError(f"invalid_previous_status:{previous_status}")
    if decision_source not in DECISION_SOURCES:
        raise ValueError(f"invalid_decision_source:{decision_source}")
    return {
        "member_id": int(member_id),
        "previous_status": previous_status,
        "status": "rejected",
        "reason": reason,
        "rule_version": int(rule_version),
        "decision_source": decision_source,
        "decision_at_s": int(now_s),
        "deletes_fact": False,
    }


def propose_event_draft(
    *,
    canonical_name: str,
    bvid_cluster: Iterable[str],
    entity_scope: Sequence[str] = (),
    event_kind: str = "other",
) -> dict:
    """§4.3 第 6 条：自动聚类只用来提出**新事件草稿**，不把同词视频直接当真值。"""
    return {
        "canonical_name": canonical_name,
        "entity_scope": tuple(entity_scope),
        "event_kind": event_kind,
        "status": "draft",
        "candidate_bvids": list(dict.fromkeys(bvid_cluster)),
        "is_truth": False,
    }


def merge_events(
    *,
    source_events: Sequence[EventDefinition],
    new_event_id: str,
    canonical_name: str,
    include_rules: Mapping[str, Any],
    exclude_rules: Optional[Mapping[str, Any]] = None,
    event_kind: str = "other",
) -> EventDefinition:
    """§4.3 第 6 条：合并创建**新事件 ID** + ``supersedes`` 关系，**不原地覆盖历史主键**。"""
    if not source_events:
        raise ValueError("merge_requires_source_events")
    source_ids = tuple(ev.event_id for ev in source_events)
    entity_scope: list[str] = []
    for event in source_events:
        for entity in event.entity_scope:
            if entity not in entity_scope:
                entity_scope.append(entity)
    return EventDefinition(
        canonical_name=canonical_name,
        entity_scope=tuple(entity_scope),
        event_kind=event_kind,
        include_rules=dict(include_rules),
        exclude_rules=dict(exclude_rules or {}),
        event_id=new_event_id,
        supersedes=source_ids,
    )


def split_event(
    *,
    source_event: EventDefinition,
    new_event_id: str,
    canonical_name: str,
    include_rules: Mapping[str, Any],
    exclude_rules: Optional[Mapping[str, Any]] = None,
    event_kind: Optional[str] = None,
) -> EventDefinition:
    """§4.3 第 6 条：拆分同样创建**新事件 ID** + ``supersedes`` 原事件。"""
    return EventDefinition(
        canonical_name=canonical_name,
        entity_scope=tuple(source_event.entity_scope),
        event_kind=event_kind or source_event.event_kind,
        include_rules=dict(include_rules),
        exclude_rules=dict(exclude_rules or {}),
        event_id=new_event_id,
        supersedes=(source_event.event_id,),
    )


# ---------------------------------------------------------------------------
# E31 / E32 辅助（三窗连续性、规则切换）
# ---------------------------------------------------------------------------

def three_window_continuous(window_statuses: Sequence[str]) -> bool:
    """E31：某成员是否构成「全三窗连续合格」。

    ``accepted→rejected→accepted`` 发生在三窗中间 → **不算**全三窗连续合格。

    Args:
        window_statuses: 三个窗口（按时间顺序）各自的成员状态。

    Returns:
        bool: 三窗且每窗都 ``accepted`` 才为 True。
    """
    return len(window_statuses) == 3 and all(status == "accepted" for status in window_statuses)


def needs_rewarmup(window_statuses: Sequence[str]) -> bool:
    """E31：不满足全三窗连续合格 → 需要重新预热（旧 assessment 不因此改动）。"""
    return not three_window_continuous(window_statuses)


def rule_review_state(
    *,
    event_rule_version: int,
    member_records: Sequence[Mapping[str, Any]],
) -> dict:
    """E32：规则新版本未完成成员复核 → 不把旧 accepted 自动提升；显示规则切换/collecting。

    Args:
        event_rule_version: 事件当前规则版本。
        member_records: 成员记录 ``[{"bvid","rule_version","status"}...]``。

    Returns:
        dict: ``assessment_status`` / 待复核清单 / ``auto_promote=False`` / ``rule_switch``。
    """
    pending = [
        record
        for record in member_records
        if int(record.get("rule_version", 0)) < int(event_rule_version)
    ]
    stale_accepted = [
        record.get("bvid") for record in pending if record.get("status") == "accepted"
    ]
    return {
        "assessment_status": "collecting" if pending else "complete",
        "rule_switch": bool(pending),
        "pending_member_count": len(pending),
        "pending_bvids": [record.get("bvid") for record in pending],
        "stale_accepted_bvids": stale_accepted,
        # 关键：旧 accepted 绝不自动提升到新规则版本。
        "auto_promote": False,
        "note": "规则新版本未完成成员复核，不把旧 accepted 自动提升",
    }
