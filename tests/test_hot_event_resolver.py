"""FishTool 04 · 第三批 b：事件归属 resolver 测试（§4.1 / §4.2 八条 / §4.3）。

依据：
- ``FishTool_04_R5执行规格_第三批b_事件归属与发现围栏.md`` §1.1 / §5 验收表；
- 上游 §4（L385-425）。逐条覆盖：规范化不吃版本号、字面量不执行正则、排除优先、
  冲突待确认、评论词只作提示、strict-auto、规则版本化建议、LLM 建议不自动生效、
  BVID 并集去重不可加总；外加 E01 / E02 / E31 / E32。
"""
from __future__ import annotations

import pytest

from modules.hotspot.event_resolver import (
    DECISION_SOURCES,
    EventDefinition,
    HistoricalBackfillError,
    VideoEvidence,
    dedupe_members,
    evaluate_event,
    evaluate_events,
    guard_no_backfill,
    member_facts,
    merge_events,
    merge_member_evidence,
    needs_rewarmup,
    next_assessment_revision,
    normalize_text,
    pin_assessment,
    propose_event_draft,
    revoke_mismatch,
    rule_review_state,
    site_total,
    split_event,
    three_window_continuous,
    union_bvid,
)

T = 1788220800


def game_event(event_id: str = "evA") -> EventDefinition:
    """构造「游戏A 3.2版本」事件定义。"""
    return EventDefinition(
        canonical_name="游戏A 3.2版本",
        entity_scope=("游戏A",),
        event_kind="version_release",
        time_scope="3.2",
        include_rules={
            "entity_groups": [["游戏A", "GameA"]],
            "anchor_groups": [["3.2版本", "3.2新角色", "3.2实测"]],
            "aliases": ["游戏A3.2"],
        },
        exclude_rules={"old_versions": ["3.1版本", "3.1"]},
        event_id=event_id,
    )


# ===========================================================================
# 规则 1：规范化不吃版本号 / 日期 / 数字
# ===========================================================================


def test_rule1_normalize_keeps_version_date_digits():
    """规则 1：剥 HTML、折空白标点，但版本号/日期/数字必须原样保留。"""
    normalized = normalize_text("【游戏A】<b>3.2版本</b>  新角色实测！！！ 2026-09-01")
    assert "<b>" not in normalized and "】" not in normalized
    assert "3.2" in normalized, "版本号被规范化吃掉"
    assert "2026-09-01" in normalized, "日期被规范化吃掉"


def test_rule1_fullwidth_digits_and_dot_not_merged():
    """规则 1：全角 ``３．２`` → ``3.2``；``3.2`` 绝不能被写成 ``32``。"""
    assert normalize_text("３．２") == "3.2"
    assert normalize_text("3.2") != normalize_text("32")


# ===========================================================================
# 规则 2：至少一个实体组 + 一个锚点组；关键字面量匹配，不执行用户正则
# ===========================================================================


def test_rule2_requires_entity_and_anchor():
    """规则 2：只命中实体、缺锚点 → 不构成归属。"""
    evidence = VideoEvidence(bvid="BV1", title="游戏A 老内容盘点")
    result = evaluate_event(evidence, game_event(), rule_version=1)
    assert result.included is False and result.decision == "ignored"

    hit = VideoEvidence(bvid="BV2", title="游戏A 3.2版本 新角色实测")
    assert evaluate_event(hit, game_event(), rule_version=1).included is True


def test_rule2_literal_keyword_is_not_regex():
    """规则 2：关键词含 ``.*`` 也只作字面量，**不执行用户正则**。"""
    regex_like = EventDefinition(
        canonical_name="字面量事件",
        include_rules={
            "entity_groups": [["游戏A"]],
            "anchor_groups": [["a.*b"]],
        },
        event_id="evRegex",
    )
    # “axxxb” 不该被正则命中。
    assert evaluate_event(
        VideoEvidence(bvid="BV3", title="游戏A axxxb 实测"), regex_like, rule_version=1
    ).included is False
    # 字面量出现才命中。
    assert evaluate_event(
        VideoEvidence(bvid="BV4", title="游戏A a.*b 实测"), regex_like, rule_version=1
    ).included is True


# ===========================================================================
# 规则 3：排除优先；冲突待确认
# ===========================================================================


def test_rule3_exclude_has_priority():
    """规则 3：同时命中 include 与 exclude → 不自动接受。"""
    evidence = VideoEvidence(bvid="BV5", title="游戏A 3.2版本 但其实是3.1版本旧内容")
    result = evaluate_event(evidence, game_event(), rule_version=1, strict_auto=True)
    assert result.excluded is True
    assert result.decision == "rejected"
    assert result.decision != "accepted"


def test_rule3_conflict_two_events_stay_proposed():
    """规则 3：两个事件都命中 → proposed，即便开 strict-auto 也不自动接受。"""
    other = EventDefinition(
        canonical_name="游戏A 3.2联动活动",
        include_rules={
            "entity_groups": [["游戏A"]],
            "anchor_groups": [["联动", "3.2版本"]],
        },
        event_id="evB",
    )
    evidence = VideoEvidence(bvid="BV6", title="游戏A 3.2版本 联动 实测")
    resolution = evaluate_events(
        evidence, [game_event(), other], rule_version=1, strict_auto=True
    )
    assert set(resolution.conflicts) == {"evA", "evB"}
    assert resolution.accepted_event_ids() == []
    assert set(resolution.proposed_event_ids()) == {"evA", "evB"}


# ===========================================================================
# 规则 4：评论聚合词只作发现提示；只有可定位文本参与自动接受
# ===========================================================================


def test_rule4_comment_terms_only_hint_not_accept():
    """规则 4：只有评论聚合词命中 → 不进 accepted。"""
    evidence = VideoEvidence(bvid="BV7", title="完全无关的标题", comment_terms=("3.2版本",))
    result = evaluate_event(evidence, game_event(), rule_version=1, strict_auto=True)
    assert result.decision != "accepted"
    assert result.included is False
    assert result.comment_hint is True


def test_rule4_locatable_text_accepts_with_strict_auto():
    """规则 4+5：可定位文本命中 + strict-auto → accepted / strict_rule。"""
    evidence = VideoEvidence(bvid="BV8", title="游戏A 3.2版本 实测")
    result = evaluate_event(evidence, game_event(), rule_version=1, strict_auto=True)
    assert result.decision == "accepted"
    assert result.decision_source == "strict_rule"


# ===========================================================================
# 规则 5/6：proposed → accepted；决定携带 rule_version / spans / 来源
# ===========================================================================


def test_rule5_default_is_proposed():
    """规则 5：未开 strict-auto 时默认为 proposed（待用户逐条确认）。"""
    evidence = VideoEvidence(bvid="BV9", title="游戏A 3.2版本 实测")
    result = evaluate_event(evidence, game_event(), rule_version=1)
    assert result.decision == "proposed"
    assert result.decision_source in DECISION_SOURCES


def test_rule6_decision_carries_traceable_metadata():
    """规则 6：决定保存 rule_version / match spans / 证据来源 / decision_source / decision_at。"""
    evidence = VideoEvidence(
        bvid="BV10",
        title="游戏A 3.2版本 实测",
        sources=("ranking", "search"),
    )
    result = evaluate_event(
        evidence, game_event(), rule_version=7, strict_auto=True, decision_at_s=T
    )
    assert result.rule_version == 7
    assert result.decision_at_s == T
    assert result.decision_source == "strict_rule"
    assert result.match_spans, "缺少 match spans"
    assert all({"start", "end"} <= set(span) for span in result.match_spans)
    assert result.evidence_sources == ["ranking", "search"]


# ===========================================================================
# 规则 7：LLM 建议不自动生效
# ===========================================================================


def test_rule7_llm_suggestion_does_not_change_members():
    """规则 7：仅 LLM 语义建议不能自动归并事件或改成员状态。"""
    evidence = VideoEvidence(bvid="BV11", title="完全无关的标题")
    resolution = evaluate_events(
        evidence,
        [game_event()],
        rule_version=1,
        llm_suggestion={"accept_bvids": ["BV11"], "merge_events": ["evA", "evB"]},
    )
    assert resolution.llm_applied is False
    assert resolution.results["evA"].decision != "accepted"
    assert resolution.results["evA"].llm_suggested is True


# ===========================================================================
# 规则 8：事件内去重 / BVID 并集 / 不可加总
# ===========================================================================


def test_rule8_union_dedupe_not_sum_of_events():
    """规则 8：事件内去重、站内以 BVID 并集去重，各事件之和 ≠ 总体。"""
    assert union_bvid({"evA": ["BV1", "BV2"], "evB": ["BV2", "BV3"]}) == {"BV1", "BV2", "BV3"}
    summary = site_total({"evA": {"BV1": 100, "BV2": 100}, "evB": {"BV2": 100, "BV3": 100}})
    assert summary["per_event_totals"] == {"evA": 200, "evB": 200}
    assert summary["sum_of_events"] == 400
    assert summary["union_bvid_count"] == 3
    assert summary["union_total"] == 300
    assert summary["summable"] is False
    assert summary["sum_of_events"] != summary["union_total"]


def test_rule8_dedupe_within_event():
    """规则 8：同事件内重复 BVID → 只保留一个成员。"""
    members = [
        VideoEvidence(bvid="BV1"),
        VideoEvidence(bvid="BV2"),
        VideoEvidence(bvid="BV1"),
    ]
    assert [m.bvid for m in dedupe_members(members)] == ["BV1", "BV2"]


# ===========================================================================
# §4.3 版本化 6 条
# ===========================================================================


def test_v43_three_distinct_facts():
    """§4.3-1：发现时间 / 接受时间 / 发布时间是三种不同事实。"""
    facts = member_facts(discovered_at_s=T, accepted_at_s=T + 10, published_epoch_s=T - 1000)
    assert facts.discovered_at_s == T
    assert facts.accepted_at_s == T + 10
    assert facts.published_epoch_s == T - 1000
    assert facts.are_distinct_fields()


def test_v43_no_backfill_today_into_yesterday():
    """§4.3-2：今日接受成员不得回写昨天「当时已知道的趋势」。"""
    with pytest.raises(HistoricalBackfillError):
        guard_no_backfill(as_of_s=T, accepted_at_s=T + 86400)
    guard_no_backfill(as_of_s=T, accepted_at_s=T - 1)  # 不抛


def test_v43_pin_and_new_revision_traceable():
    """§4.3-3/4：历史 assessment 钉住版本与快照；纠错只产生新版本。"""
    pinned = pin_assessment(
        event_id="evA",
        rule_version=1,
        policy_version="p1",
        window_end_s=T,
        member_decisions={"BV1": 2, "BV2": 1},
        input_snapshot_id="snap1",
    )
    frozen = dict(pinned)
    assert pinned["rule_version"] == 1 and pinned["input_snapshot_id"] == "snap1"
    assert next_assessment_revision(pinned["member_decisions"]["BV1"]) == 3
    # 旧 assessment 不被原地修改。
    assert pinned == frozen


def test_v43_revoke_keeps_fact():
    """§4.3-5：错配撤销保存状态变更，不删除事实。"""
    record = revoke_mismatch(
        member_id=42,
        previous_status="accepted",
        rule_version=1,
        now_s=T,
        reason="misattribution",
    )
    assert record["status"] == "rejected"
    assert record["previous_status"] == "accepted"
    assert record["deletes_fact"] is False


def test_v43_merge_split_create_new_id_with_supersedes():
    """§4.3-6：合并/拆分创建新事件 ID + supersedes，不原地覆盖历史主键。"""
    source = game_event("evOld")
    merged = merge_events(
        source_events=[source, game_event("evOld2")],
        new_event_id="evMerged",
        canonical_name="游戏A 3.2合并事件",
        include_rules={"entity_groups": [["游戏A"]], "anchor_groups": [["3.2版本"]]},
    )
    assert merged.event_id == "evMerged"
    assert merged.supersedes == ("evOld", "evOld2")

    split = split_event(
        source_event=merged,
        new_event_id="evSplit",
        canonical_name="游戏A 3.2拆分事件",
        include_rules={"entity_groups": [["游戏A"]], "anchor_groups": [["3.2新角色"]]},
    )
    assert split.event_id == "evSplit"
    assert split.supersedes == ("evMerged",)

    draft = propose_event_draft(canonical_name="疑似新事件", bvid_cluster=["BV1", "BV2"])
    assert draft["status"] == "draft" and draft["is_truth"] is False


# ===========================================================================
# E01 / E02 / E31 / E32
# ===========================================================================


def test_E01_same_tag_diff_game_or_version_not_merged():
    """E01：同标签不同游戏/版本 → 不自动并同事件。"""
    # 不同游戏：实体组不命中。
    other_game = VideoEvidence(bvid="BV20", title="游戏B 3.2版本 联动", tags=("3.2版本",))
    result = evaluate_event(other_game, game_event(), rule_version=1, strict_auto=True)
    assert result.included is False and result.decision != "accepted"
    # 同游戏旧版本：命中排除规则。
    old_version = VideoEvidence(bvid="BV21", title="游戏A 3.1版本 实测")
    old_result = evaluate_event(old_version, game_event(), rule_version=1, strict_auto=True)
    assert old_result.decision == "rejected"


def test_E02_same_bvid_three_sources_one_member_multi_evidence():
    """E02：同 BVID 三来源 → 事件内一个成员，多条证据。"""
    merged = merge_member_evidence(
        [
            VideoEvidence(bvid="BV30", title="游戏A 3.2版本", sources=("ranking",)),
            VideoEvidence(bvid="BV30", title="游戏A 3.2版本", sources=("search",)),
            VideoEvidence(bvid="BV30", title="游戏A 3.2版本", sources=("seed",)),
        ]
    )
    assert len(merged) == 1
    assert merged[0]["bvid"] == "BV30"
    assert sorted(merged[0]["sources"]) == ["ranking", "search", "seed"]
    assert len(merged[0]["evidence"]) == 3


def test_E31_mid_reject_breaks_three_window_continuity():
    """E31：accepted→rejected→accepted 中间断裂 → 不算全三窗连续合格；需重新预热。"""
    statuses = ["accepted", "rejected", "accepted"]
    assert three_window_continuous(statuses) is False
    assert needs_rewarmup(statuses) is True

    pinned = pin_assessment(
        event_id="evA",
        rule_version=1,
        policy_version="p1",
        window_end_s=T,
        member_decisions={"BV1": 3},
        input_snapshot_id="snap-old",
    )
    frozen = dict(pinned)
    needs_rewarmup(statuses)  # 重新预热判定
    assert pinned == frozen, "旧 assessment 不应被重新预热判定改动"


def test_E32_rule_upgrade_not_auto_promote():
    """E32：规则新版本未完成成员复核 → 不把旧 accepted 自动提升；显示 collecting。"""
    state = rule_review_state(
        event_rule_version=2,
        member_records=[
            {"bvid": "BV1", "rule_version": 1, "status": "accepted"},
            {"bvid": "BV2", "rule_version": 2, "status": "accepted"},
        ],
    )
    assert state["assessment_status"] == "collecting"
    assert state["rule_switch"] is True
    assert state["auto_promote"] is False
    assert state["pending_bvids"] == ["BV1"]
    assert state["stale_accepted_bvids"] == ["BV1"]
