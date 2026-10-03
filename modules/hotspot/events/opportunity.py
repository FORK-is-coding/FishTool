"""FishTool 04 · 第三批 e：机会可行性硬门 + 六种 action 互斥判定 + 可解释排序 + 推荐解释。

依据：``FishTool_04_话题级热点研判与选题决策_02补充执行案(1).md``
§10.2（L816-832）/ §10.3（L833-858）/ §10.4（L859-869）/ §10.5（L870-884）
与 §5.5（``hotspot_opportunity_runs``），以及 3e 执行规格 §1.2-§1.6。

钉死口径（逐条实现，不做发挥）：

- ``publish_eta_s = request_as_of_s + (production_hours + review_hours + publish_buffer_hours)*3600``；
  ``publish_eta_s + safety_margin_s > deadline_s`` → ``deadline_missed``，
  **高热度绝不覆盖硬门**（硬门排在热度之前）；
- UGC 无已知截止 → ``deadline_known=false``，**绝不编“还剩 N 天”**；
  无已知截止**不算失败**，但必须标 ``longevity_unknown``；
- 六种 action **固定命名、一个不许增改**；``choose_action`` 严格按 1.4 的 1→7 互斥顺序，
  **命中即返回**；``mode=historical`` 只允许 ``differentiate_research`` / ``watch_and_collect``；
- 全零增量、``concentrated``、``author_coverage_insufficient``、``sampling_changed``、``stale``、
  转折 ``undetermined``、``relative_change=null``（0 分母）→ **一律不许**
  ``make_candidate`` / ``prepare_or_pilot``；0 分母**不当 0、也不当无限增长**；
- ``fast_ttl_seconds`` **从证据 ``window_end_s`` 算**，**不因新生成一个 OpportunityRun 而重置**；
- ``rank_key`` 第一项是**固定动作等级**，并返回可解释分项；
  ``not_suitable`` / ``deadline_missed`` 进**独立不可执行区**，不参与“优先做”排序；
- ``CreatorBrief`` 在 run 内**不可变**；反馈 **append 型**，不覆盖原推荐条件；
  同 ``request_fingerprint`` 重复请求返回**已有 run**；
- 解释至少 2 个**可打开** BVID 引用，不够就**如实说数量**，不伪造；
  **禁止**“热度涨 300% / 保证两天红利 / 成功率 90%”这类话术。

本模块只做纯计算与少量既有仓储调用：不触网、不写路由。
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .brief import (
    MATCH_EXPLICIT,
    MATCH_ORDER,
    MATCH_PARTIAL,
    MATCH_UNSPECIFIED,
    BriefValidationError,
    CreatorBrief,
    investment_cap_hours,
    is_excluded,
    match_account,
)
from .policy import EventPolicy

# --------------------------------------------------------------------------- 动作枚举

#: 确认日趋势 + 适配 + 可行，可优先制作。
ACTION_MAKE_CANDIDATE: str = "make_candidate"
#: 早期合格信号，小成本准备 / 试做。
ACTION_PREPARE_OR_PILOT: str = "prepare_or_pilot"
#: 扩散线索有价值但成熟 / 供给密集，研究新角度。
ACTION_DIFFERENTIATE_RESEARCH: str = "differentiate_research"
#: 缺采样 / 低匹配，给明确采集与复查条件。
ACTION_WATCH_AND_COLLECT: str = "watch_and_collect"
#: 用户明确排除或必要能力不满足。
ACTION_NOT_SUITABLE: str = "not_suitable"
#: 所选活动目标截止无法满足。
ACTION_DEADLINE_MISSED: str = "deadline_missed"

#: 六种固定 action（**一个不许增、不许改名**）。
ALL_ACTIONS: tuple[str, ...] = (
    ACTION_MAKE_CANDIDATE,
    ACTION_PREPARE_OR_PILOT,
    ACTION_DIFFERENTIATE_RESEARCH,
    ACTION_WATCH_AND_COLLECT,
    ACTION_NOT_SUITABLE,
    ACTION_DEADLINE_MISSED,
)
#: 可执行（参与“优先做”排序）的四种。
EXECUTABLE_ACTIONS: tuple[str, ...] = (
    ACTION_MAKE_CANDIDATE,
    ACTION_PREPARE_OR_PILOT,
    ACTION_DIFFERENTIATE_RESEARCH,
    ACTION_WATCH_AND_COLLECT,
)
#: 不可执行区（**不参与排序**）。
NON_EXECUTABLE_ACTIONS: tuple[str, ...] = (ACTION_NOT_SUITABLE, ACTION_DEADLINE_MISSED)

#: 固定动作等级（``rank_key`` 第一项）。
ACTION_ORDER: dict[str, int] = {
    ACTION_MAKE_CANDIDATE: 0,
    ACTION_PREPARE_OR_PILOT: 1,
    ACTION_DIFFERENTIATE_RESEARCH: 2,
    ACTION_WATCH_AND_COLLECT: 3,
}

#: 推荐解释要求的最少**可打开**引用数。
MIN_EVIDENCE_REFS: int = 2

#: 出现在输出里即算失败的话术（§15 原文）。
BANNED_PHRASES: tuple[str, ...] = (
    "300%",
    "保证",
    "成功率",
    "红利",
    "已死",
    "肯定来得及",
    "还能火",
    "还剩",
    "包爆",
    "稳赚",
)

#: 可行性分类（§10.3 第 4 条）。
FEASIBLE_DEADLINE_FEASIBLE: str = "deadline_feasible"
FEASIBLE_NO_KNOWN_DEADLINE: str = "no_known_deadline"
FEASIBLE_AT_RISK: str = "at_risk"


# --------------------------------------------------------------------------- 小工具


def _is_real_number(value: Any) -> bool:
    """是否为“真数值”（排除 ``bool`` 与非有限值）。"""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """归一为映射（非映射 → 空映射，不抛异常）。"""
    return value if isinstance(value, Mapping) else {}


def _tokens(value: Any) -> set[str]:
    """把实体 / 领域 token 归一为小写集合。"""
    if value is None:
        return set()
    if isinstance(value, str):
        return {value.strip().lower()}
    try:
        return {str(v).strip().lower() for v in value}
    except TypeError:
        return set()


def _jsonable(value: Any) -> Any:
    """递归转为可 JSON 序列化对象（元组 → 列表；其它不可序列化 → 字符串）。"""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _ensure_brief(brief: Any, policy: EventPolicy) -> CreatorBrief:
    """把入参归一为**已校验**的不可变 brief（映射 → 严格校验构造）。

    Raises:
        BriefValidationError: 入参既不是 ``CreatorBrief`` 也不是映射，或校验不通过。
    """
    if isinstance(brief, CreatorBrief):
        brief.validate(
            duration_max_hours=policy.duration_max_hours,
            max_experiment_hours_limit=policy.max_experiment_hours_limit,
        )
        return brief
    if isinstance(brief, Mapping):
        return CreatorBrief.from_dict(
            brief,
            duration_max_hours=policy.duration_max_hours,
            max_experiment_hours_limit=policy.max_experiment_hours_limit,
        )
    raise BriefValidationError("brief_must_be_creator_brief_or_mapping")


# --------------------------------------------------------------------------- 硬门：截止


def compute_publish_eta_s(request_as_of_s: int, brief: CreatorBrief) -> int:
    """计算制作完成（可发布）的预计时刻。

    ``publish_eta_s = request_as_of_s + (production + review + publish_buffer) * 3600``。

    Args:
        request_as_of_s: 请求时刻（epoch 秒）。
        brief: 创作者简报（时长字段须非负）。

    Returns:
        int: 预计可发布时刻（epoch 秒）。

    Raises:
        ValueError: ``request_as_of_s`` 非法，或任一时长字段为负（**禁止负制作时间绕过截止**）。
    """
    if type(request_as_of_s) is not int or isinstance(request_as_of_s, bool) or request_as_of_s < 0:
        raise ValueError("invalid_request_as_of_s")
    total_hours = 0.0
    for value in (brief.production_hours, brief.review_hours, brief.publish_buffer_hours):
        if not _is_real_number(value) or float(value) < 0:
            raise ValueError("invalid_duration_hours")
        total_hours += float(value)
    return request_as_of_s + int(round(total_hours * 3600.0))


def deadline_status(
    facts: Mapping[str, Any],
    brief: CreatorBrief,
    request_as_of_s: int,
    *,
    policy: EventPolicy | None = None,
) -> dict[str, Any]:
    """判定活动截止的已知 / 可信 / 可行性（无已知截止**不算失败**）。

    Args:
        facts: 事实包；读取 ``facts['deadline']``：
            ``deadline_known`` / ``deadline_s`` / ``deadline_confidence`` / ``bound_to_activity``。
        brief: 创作者简报。
        request_as_of_s: 请求时刻。
        policy: 事件策略（提供 ``safety_margin_s`` 与 ``deadline_confidence_min``）。

    Returns:
        dict: ``deadline_known`` / ``deadline_trusted`` / ``deadline_governing`` / ``deadline_s`` /
        ``deadline_confidence`` / ``bound_to_activity`` / ``publish_eta_s`` /
        ``deadline_feasible`` / ``feasibility_category`` / ``longevity_unknown``。

        其中 ``deadline_governing`` = 「可信 且 绑定所选活动」；只有它为真时截止才真正约束本候选。
        """
    policy = policy or EventPolicy()
    raw = _as_mapping(facts.get("deadline"))
    deadline_s = raw.get("deadline_s")
    confidence = raw.get("deadline_confidence")
    bound = bool(raw.get("bound_to_activity", True))

    declared_known = raw.get("deadline_known")
    if declared_known is None:
        deadline_known = _is_real_number(deadline_s)
    else:
        deadline_known = bool(declared_known) and _is_real_number(deadline_s)

    eta_s = compute_publish_eta_s(request_as_of_s, brief)
    trusted = (
        bool(deadline_known)
        and _is_real_number(deadline_s)
        and _is_real_number(confidence)
        and float(confidence) >= policy.deadline_confidence_min
    )

    # 只有「可信 + 绑定所选活动」的截止才真正约束本候选；其它一律不封禁、不当失败。
    governing = bool(trusted and bound)
    if governing:
        feasible = eta_s + int(policy.safety_margin_s) <= int(deadline_s)
        category = FEASIBLE_DEADLINE_FEASIBLE if feasible else FEASIBLE_AT_RISK
    else:
        # 无已知截止 / 截止不可信 / 未绑定该活动 → 允许推荐研究或制作，但寿命未知。
        feasible = True
        category = FEASIBLE_NO_KNOWN_DEADLINE

    return {
        "deadline_known": bool(deadline_known),
        "deadline_trusted": bool(trusted),
        "deadline_governing": governing,
        "deadline_s": int(deadline_s) if _is_real_number(deadline_s) else None,
        "deadline_confidence": float(confidence) if _is_real_number(confidence) else None,
        "bound_to_activity": bound,
        "publish_eta_s": eta_s,
        "safety_margin_s": int(policy.safety_margin_s),
        "deadline_feasible": bool(feasible),
        "feasibility_category": category,
        # 无已知/可信/绑定的截止 → 寿命未知：既不编“还剩 N 天”，也不说“肯定来得及”。
        "longevity_unknown": not governing,
    }


# --------------------------------------------------------------------------- 证据有效性


def _evidence_invalid(
    facts: Mapping[str, Any],
    daily: Mapping[str, Any],
    early: Mapping[str, Any],
    request_as_of_s: int,
) -> bool:
    """判定证据是否“时间越界 / 引用不存在 / 窗口陈旧或无效”（命中即只能观察）。"""
    if facts.get("references_valid") is False:
        return True
    if facts.get("evidence_missing") is True:
        return True
    for source in (daily, early):
        window_end = source.get("window_end_s")
        if isinstance(window_end, int) and not isinstance(window_end, bool) and window_end > request_as_of_s:
            return True
    if daily.get("stale") is True:
        return True
    if "stale_evidence" in (daily.get("reason_codes") or []):
        return True
    if early.get("status") == "insufficient":
        return True
    return False


def _no_real_increment(
    daily: Mapping[str, Any],
    early: Mapping[str, Any],
    discovery: Mapping[str, Any],
) -> bool:
    """判定是否“全零增量且无任何有效新线索”（0 分母不当 0、也不当无限增长）。"""
    values = [
        float(daily[key])
        for key in ("a_delta", "b_delta", "c_delta")
        if _is_real_number(daily.get(key))
    ]
    daily_zero = bool(values) and all(v == 0.0 for v in values)
    early_growth = early.get("early_growth_signal") is True or early.get("signal") in (
        "growth",
        "emerging_from_zero",
    )
    discovery_delta = discovery.get("discovery_delta")
    new_increment = (
        _is_real_number(discovery_delta) and float(discovery_delta) != 0.0
    ) or bool(discovery.get("new_member_delta_observed"))
    return daily_zero and not early_growth and not new_increment


def _has_differentiation_material(
    facts: Mapping[str, Any],
    daily: Mapping[str, Any],
    discovery: Mapping[str, Any],
) -> bool:
    """是否存在**可用**的差异化材料（未知作者过多 / 采样变化时不采信扩散增量）。"""
    if facts.get("has_new_material") is True:
        return True
    if daily.get("author_coverage_insufficient") is True:
        return False
    if discovery.get("sampling_changed") is True:
        return False
    discovery_delta = discovery.get("discovery_delta")
    if _is_real_number(discovery_delta) and float(discovery_delta) > 0:
        return True
    new_members = discovery.get("new_member_delta_observed")
    if isinstance(new_members, Mapping) and new_members:
        return True
    return facts.get("new_author_increment") is True


def _early_ttl_active(
    early: Mapping[str, Any],
    request_as_of_s: int,
    policy: EventPolicy,
) -> bool:
    """TTL 是否仍有效——**从证据 ``window_end_s`` 算，不认任何刷新时间**。

    这样“10:00 重新请求、06:00 窗结束”的 E29 场景不会被新 run 续命。
    """
    window_end = early.get("window_end_s")
    ttl = early.get("fast_ttl_seconds")
    if not _is_real_number(window_end) or not _is_real_number(ttl):
        return False
    expires_s = int(window_end) + int(ttl)
    if int(ttl) <= 0:
        return False
    if request_as_of_s > expires_s:
        return False
    if request_as_of_s - int(window_end) > int(policy.early_staleness_max_s):
        return False
    # evaluate_early 已给出结论时不得与之相反（取更严者）。
    if early.get("ttl_active") is False:
        return False
    return True


# --------------------------------------------------------------------------- 动作判定（1→7）


def _detail(
    action: str,
    reason_code: str,
    notes: Sequence[str],
    account_match: str,
    deadline: Mapping[str, Any],
    mode: str,
    order: int,
) -> dict[str, Any]:
    """组装 ``evaluate_choice`` 的返回结构（含命中顺序，便于互斥顺序断言）。"""
    return {
        "action": action,
        "reason_code": reason_code,
        "notes": list(notes),
        "account_match": account_match,
        "deadline": dict(deadline),
        "mode": mode,
        "matched_order": order,
        "is_executable": action not in NON_EXECUTABLE_ACTIONS,
    }


def evaluate_choice(
    facts: Mapping[str, Any],
    brief: Any,
    request_as_of_s: int,
    *,
    policy: EventPolicy | None = None,
) -> dict[str, Any]:
    """按 §1.4 的 **1→7 互斥顺序**判定 action（命中即返回）。

    顺序：显式排除/不可替代素材 → 活动截止 → 证据有效性/历史模式 →
    日级 confirmed rising → 未过 TTL 的 early → stable/declining 的差异化 → 观察。

    Args:
        facts: 事实包（见 :func:`build_candidate` 的字段说明）。
        brief: ``CreatorBrief`` 或等价的用户显式输入映射。
        request_as_of_s: 请求时刻。
        policy: 事件策略。

    Returns:
        dict: ``action`` / ``reason_code`` / ``notes`` / ``account_match`` /
        ``deadline`` / ``mode`` / ``matched_order`` / ``is_executable``。
    """
    policy = policy or EventPolicy()
    brief = _ensure_brief(brief, policy)
    daily = _as_mapping(facts.get("daily"))
    early = _as_mapping(facts.get("early"))
    discovery = _as_mapping(facts.get("discovery"))
    mode = str(facts.get("mode") or daily.get("mode") or "realtime")
    entities = _tokens(facts.get("entities"))
    domains = _tokens(facts.get("domains"))
    topics = _tokens(facts.get("topics"))
    account_match = match_account(brief, entities=entities, domains=domains)
    deadline = deadline_status(facts, brief, request_as_of_s, policy=policy)
    notes: list[str] = []

    # ---- 1. 显式领域排除 / 不可替代素材缺失 → not_suitable（可改形式则观察） ----
    if is_excluded(
        brief,
        entities=entities,
        domains=domains,
        topics=topics,
        content_type=facts.get("content_type"),
    ):
        return _detail(ACTION_NOT_SUITABLE, "explicit_exclusion", notes, account_match, deadline, mode, 1)

    if brief.available_assets is not None:
        required_assets = _tokens(facts.get("required_assets"))
        missing = required_assets - brief.available_asset_set
        if missing:
            irreplaceable = missing - _tokens(facts.get("adjustable_assets"))
            if irreplaceable:
                notes.append("irreplaceable_asset_missing:" + ",".join(sorted(irreplaceable)))
                return _detail(
                    ACTION_NOT_SUITABLE, "irreplaceable_asset_missing", notes, account_match, deadline, mode, 1
                )
            notes.append("adjust_format")
            notes.extend("adjust_asset:" + token for token in sorted(missing))
            return _detail(ACTION_WATCH_AND_COLLECT, "asset_adjustable", notes, account_match, deadline, mode, 1)

    if brief.supported_formats is not None:
        required_formats = _tokens(facts.get("required_formats"))
        unsupported = required_formats - brief.supported_format_set
        if unsupported:
            notes.append("adjust_format")
            notes.extend("adjust_format:" + token for token in sorted(unsupported))
            return _detail(ACTION_WATCH_AND_COLLECT, "format_not_supported", notes, account_match, deadline, mode, 1)

    # ---- 2. 绑定活动目标的**可信**截止已无法达到 → deadline_missed（热度不救） ----
    if deadline["bound_to_activity"] and deadline["deadline_trusted"] and not deadline["deadline_feasible"]:
        notes.append(f"eta_s:{deadline['publish_eta_s']}")
        return _detail(ACTION_DEADLINE_MISSED, "deadline_unreachable", notes, account_match, deadline, mode, 2)

    # ---- 3. 证据时间越界 / 引用不存在 / 窗口陈旧无效 → 观察；历史模式只允许研究/观察 ----
    if _evidence_invalid(facts, daily, early, request_as_of_s):
        return _detail(ACTION_WATCH_AND_COLLECT, "evidence_invalid_or_stale", notes, account_match, deadline, mode, 3)
    if mode == "historical":
        if _has_differentiation_material(facts, daily, discovery):
            return _detail(
                ACTION_DIFFERENTIATE_RESEARCH, "historical_replay", notes, account_match, deadline, mode, 3
            )
        return _detail(ACTION_WATCH_AND_COLLECT, "historical_replay", notes, account_match, deadline, mode, 3)

    # ---- 4. 日级 confirmed rising + 适配 + 可行 → make_candidate ----
    if (
        daily.get("topic_phase") == "rising"
        and daily.get("attention_present") is True
        and daily.get("sample_gate_passed") is True
        and daily.get("concentrated") is not True
        and daily.get("author_coverage_insufficient") is not True
        and daily.get("stage_reason") != "rising_low_base"
        and _is_real_number(daily.get("relative_change"))  # 0 分母（null）不算门槛通过
        and discovery.get("sampling_changed") is not True
        and not _no_real_increment(daily, early, discovery)
        and account_match == MATCH_EXPLICIT
        and deadline["deadline_feasible"]
    ):
        return _detail(ACTION_MAKE_CANDIDATE, "confirmed_daily_rising", notes, account_match, deadline, mode, 4)

    # ---- 5. 合格且未过 TTL 的 early 增强 / emerging_from_zero → prepare_or_pilot ----
    early_growth = early.get("early_growth_signal") is True or early.get("signal") in (
        "growth",
        "emerging_from_zero",
    )
    effort_hours = brief.total_effort_hours
    if (
        early.get("status") == "complete"
        and early_growth
        and _early_ttl_active(early, request_as_of_s, policy)
        and account_match in (MATCH_EXPLICIT, MATCH_PARTIAL)
        and effort_hours <= float(brief.max_experiment_hours)
        and effort_hours <= investment_cap_hours(brief)
        and daily.get("concentrated") is not True
        and daily.get("author_coverage_insufficient") is not True
        and discovery.get("sampling_changed") is not True
        and not _no_real_increment(daily, early, discovery)
    ):
        return _detail(ACTION_PREPARE_OR_PILOT, "early_signal_qualified", notes, account_match, deadline, mode, 5)

    # ---- 6. 日级 stable / declining 且有新增线索 → 差异化研究；否则观察 ----
    if daily.get("topic_phase") in ("stable", "declining"):
        if _has_differentiation_material(facts, daily, discovery):
            return _detail(
                ACTION_DIFFERENTIATE_RESEARCH, "new_material_or_new_authors", notes, account_match, deadline, mode, 6
            )
        return _detail(ACTION_WATCH_AND_COLLECT, "no_new_clue", notes, account_match, deadline, mode, 6)

    # ---- 7. discovery_only 或其它不充分情况 → watch_and_collect ----
    return _detail(ACTION_WATCH_AND_COLLECT, "insufficient_evidence", notes, account_match, deadline, mode, 7)


def choose_action(
    facts: Mapping[str, Any],
    brief: Any,
    request_as_of_s: int,
    *,
    policy: EventPolicy | None = None,
) -> str:
    """``choose_action`` 契约入口：只返回六种 action 之一（命名与原文一致）。

    Args:
        facts: 事实包。
        brief: ``CreatorBrief`` 或等价映射。
        request_as_of_s: 请求时刻。
        policy: 事件策略。

    Returns:
        str: 六种 action 之一。
    """
    return str(evaluate_choice(facts, brief, request_as_of_s, policy=policy)["action"])


# --------------------------------------------------------------------------- 可解释排序


def rank_components(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """返回 ``rank_key`` 的**可解释分项**（不把标签转成不透明总分）。"""
    action = candidate.get("action")
    match = candidate.get("account_match", MATCH_UNSPECIFIED)
    return {
        "action": action,
        "action_rank": ACTION_ORDER.get(str(action)),
        "account_match": match,
        "account_match_rank": MATCH_ORDER.get(str(match), MATCH_ORDER[MATCH_UNSPECIFIED]),
        "staleness_s": candidate.get("staleness_s"),
        "source_coverage": candidate.get("source_coverage"),
        "effort_hours": candidate.get("effort_hours"),
        "event_id": candidate.get("event_id"),
        "feasibility_category": _as_mapping(candidate.get("deadline")).get("feasibility_category"),
    }


def _rank_staleness(value: Any) -> float:
    """新鲜度排序分量：越小越新（缺失排最后）。"""
    return float(value) if _is_real_number(value) else float("inf")


def _rank_coverage(value: Any) -> float:
    """来源覆盖排序分量：越大越好（缺失视为最差 0）。"""
    return float(value) if _is_real_number(value) else 0.0


def _rank_effort(value: Any) -> float:
    """制作投入排序分量：越小越优先（缺失排最后）。"""
    return float(value) if _is_real_number(value) else float("inf")


def rank_key(candidate: Mapping[str, Any]) -> tuple:
    """可解释排序键：固定动作等级 → 账号匹配 → 新鲜度 → 来源覆盖 → 投入 →``event_id``。

    Args:
        candidate: 候选字典（须含 ``action``；``not_suitable``/``deadline_missed`` 不可入排序）。

    Returns:
        tuple: 可直接用于 ``sorted`` 的稳定排序键。

    Raises:
        ValueError: 动作缺失 / 未知 / 属于不可执行区。
    """
    action = str(candidate.get("action"))
    if action in NON_EXECUTABLE_ACTIONS:
        raise ValueError("non_executable_action")
    if action not in ACTION_ORDER:
        raise ValueError("unknown_action")
    components = rank_components(candidate)
    return (
        components["action_rank"],
        components["account_match_rank"],
        _rank_staleness(candidate.get("staleness_s")),
        -_rank_coverage(candidate.get("source_coverage")),
        _rank_effort(candidate.get("effort_hours")),
        str(candidate.get("event_id") or ""),
    )


def sort_candidates(candidates: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """把候选分成「可执行（按 ``rank_key`` 排序）」与「独立不可执行区」。

    Args:
        candidates: 候选字典序列。

    Returns:
        dict: ``{'executable': [...], 'non_executable': [...]}``；
        ``not_suitable`` / ``deadline_missed`` **只出现在** ``non_executable``。
    """
    executable: list[dict[str, Any]] = []
    non_executable: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        if item.get("action") in NON_EXECUTABLE_ACTIONS:
            non_executable.append(item)
        else:
            executable.append(item)
    executable.sort(key=rank_key)
    return {"executable": executable, "non_executable": non_executable}


# --------------------------------------------------------------------------- 推荐解释


def contains_banned_phrase(text: str) -> str | None:
    """检查文本是否含禁止话术。

    Args:
        text: 待检查文本。

    Returns:
        str | None: 命中的禁止短语；无命中返回 ``None``。
    """
    if not isinstance(text, str):
        return None
    for phrase in BANNED_PHRASES:
        if phrase in text:
            return phrase
    return None


def _openable_refs(facts: Mapping[str, Any]) -> list[str]:
    """取**可打开**的 BVID / 活动证据引用（不可打开的丢弃，不伪造）。"""
    raw = facts.get("evidence_refs") or []
    refs: list[str] = []
    if not isinstance(raw, (list, tuple)):
        return refs
    for item in raw:
        if isinstance(item, Mapping):
            if item.get("openable") is False:
                continue
            bvid = item.get("bvid") or item.get("ref")
        elif isinstance(item, str):
            bvid = item
        else:
            continue
        if bvid:
            refs.append(str(bvid))
    return refs


def build_explanation(
    facts: Mapping[str, Any],
    brief: Any,
    action: str,
    *,
    policy: EventPolicy | None = None,
    request_as_of_s: int = 0,
) -> dict[str, Any]:
    """组包一条推荐的**完整解释**（缺就如实说，不伪造）。

    Args:
        facts: 事实包。
        brief: ``CreatorBrief`` 或等价映射。
        action: 已判定的 action。
        policy: 事件策略。
        request_as_of_s: 请求时刻。

    Returns:
        dict: 含事件、日级/早期/扩散事实、适配理由、建议形式与制作量、差异化依据、
        可靠截止与 ETA、寿命未知警告、证据引用（数量如实）、下次复查条件，
        以及 ``text`` 人类可读说明（**保证不含禁止话术**）。
    """
    policy = policy or EventPolicy()
    brief = _ensure_brief(brief, policy)
    daily = _as_mapping(facts.get("daily"))
    early = _as_mapping(facts.get("early"))
    discovery = _as_mapping(facts.get("discovery"))
    deadline = deadline_status(facts, brief, request_as_of_s, policy=policy)
    account_match = match_account(
        brief, entities=_tokens(facts.get("entities")), domains=_tokens(facts.get("domains"))
    )

    refs = _openable_refs(facts)
    ref_count = len(refs)
    shortfall = ref_count < MIN_EVIDENCE_REFS
    ref_note = (
        f"仅 {ref_count} 条可打开引用（少于要求的 {MIN_EVIDENCE_REFS} 条），如实说明，不伪造。"
        if shortfall
        else f"{ref_count} 条可打开引用。"
    )

    formats = brief.supported_formats or ()
    angles = brief.preferred_angles or ()
    suggested_format = formats[0] if formats else "未声明"
    suggested_angle = angles[0] if angles else "未声明"
    effort_hours = brief.total_effort_hours

    claimed_daily_confirmation = (
        daily.get("topic_phase") == "rising"
        and daily.get("attention_present") is True
        and daily.get("sample_gate_passed") is True
    )

    if not deadline["deadline_known"]:
        deadline_text = "无已知截止，寿命未知（longevity_unknown），不提供虚假确定性"
    elif not deadline["deadline_trusted"]:
        deadline_text = "截止可信度不足，按寿命未知（longevity_unknown）处理"
    elif deadline["deadline_feasible"]:
        deadline_text = (
            f"活动截止 {deadline['deadline_s']}，制作 ETA {deadline['publish_eta_s']}，在保守余量内可行"
        )
    else:
        deadline_text = (
            f"活动截止 {deadline['deadline_s']}，制作 ETA {deadline['publish_eta_s']}，无法达到"
        )

    text = " ".join(
        [
            f"事件 {facts.get('event_id')}：日级 {daily.get('topic_phase') or '未定'}，"
            f"早期 {early.get('signal') or '无信号'}，扩散线索 {discovery.get('newly_discovered_videos', 0)} 条。",
            f"适配：账号匹配 {account_match}。",
            f"建议形式：{suggested_format}；切入点：{suggested_angle}；预计制作 {effort_hours:.1f} 小时。",
            f"截止：{deadline_text}。",
            f"证据：{ref_note}",
            "复查：2 小时后补采，若仍增强再扩大投入。",
        ]
    )

    return {
        "event_id": facts.get("event_id"),
        "action": action,
        "daily_fact": {
            "topic_phase": daily.get("topic_phase"),
            "stage_reason": daily.get("stage_reason"),
            "attention_present": daily.get("attention_present"),
            "sample_gate_passed": daily.get("sample_gate_passed"),
            "concentrated": daily.get("concentrated"),
            "author_coverage_insufficient": daily.get("author_coverage_insufficient"),
            "relative_change": daily.get("relative_change"),
            "window_end_s": daily.get("window_end_s"),
        },
        "early_fact": {
            "status": early.get("status"),
            "signal": early.get("signal"),
            "early_growth_signal": early.get("early_growth_signal"),
            "ttl_active": early.get("ttl_active"),
            "window_end_s": early.get("window_end_s"),
            "fast_ttl_seconds": early.get("fast_ttl_seconds"),
        },
        "diffusion_fact": dict(discovery),
        "brief_fit": {
            "account_match": account_match,
            "preferred_angles": list(angles),
            "supported_formats": list(formats),
        },
        "suggested": {
            "format": suggested_format,
            "angle": suggested_angle,
            "estimated_effort_hours": effort_hours,
        },
        "deadline": dict(deadline),
        "longevity_unknown": bool(deadline["longevity_unknown"]),
        "evidence_refs": refs,
        "evidence_ref_count": ref_count,
        "evidence_ref_shortfall": bool(shortfall),
        "evidence_ref_note": ref_note,
        "claimed_daily_confirmation": bool(claimed_daily_confirmation),
        "review_condition": "2 小时后补采，若仍增强再扩大投入；否则维持观察。",
        "text": text,
    }


# --------------------------------------------------------------------------- 候选与 run


def build_candidate(
    facts: Mapping[str, Any],
    brief: Any,
    request_as_of_s: int,
    *,
    policy: EventPolicy | None = None,
) -> dict[str, Any]:
    """把一个事件事实包打成候选（含 action、分项证据、``rank_key``、排除原因）。

    Args:
        facts: 事实包；常用键：
            ``event_id`` / ``entities`` / ``domains`` / ``topics`` / ``content_type`` /
            ``required_assets`` / ``adjustable_assets`` / ``required_formats`` /
            ``deadline`` / ``daily`` / ``early`` / ``discovery`` / ``mode`` /
            ``evidence_refs`` / ``has_new_material`` / ``references_valid``。
        brief: ``CreatorBrief`` 或等价映射。
        request_as_of_s: 请求时刻。
        policy: 事件策略。

    Returns:
        dict: 候选（``not_suitable`` / ``deadline_missed`` 的 ``rank_key`` 为 ``None``）。
    """
    policy = policy or EventPolicy()
    brief = _ensure_brief(brief, policy)
    choice = evaluate_choice(facts, brief, request_as_of_s, policy=policy)
    daily = _as_mapping(facts.get("daily"))
    early = _as_mapping(facts.get("early"))
    discovery = _as_mapping(facts.get("discovery"))

    window_end = daily.get("window_end_s")
    staleness = int(request_as_of_s) - int(window_end) if _is_real_number(window_end) else None
    coverage = daily.get("member_coverage")
    if not _is_real_number(coverage):
        coverage = daily.get("paired_member_coverage")

    candidate: dict[str, Any] = {
        "event_id": facts.get("event_id"),
        "action": choice["action"],
        "reason_code": choice["reason_code"],
        "matched_order": choice["matched_order"],
        "account_match": choice["account_match"],
        "mode": choice["mode"],
        "staleness_s": staleness,
        "source_coverage": coverage,
        "effort_hours": brief.total_effort_hours,
        "deadline": choice["deadline"],
        "notes": choice["notes"],
        "exclusion_reason": choice["reason_code"] if choice["action"] in NON_EXECUTABLE_ACTIONS else None,
        "evidence": {"daily": _jsonable(daily), "early": _jsonable(early), "discovery": _jsonable(discovery)},
    }
    if choice["action"] in NON_EXECUTABLE_ACTIONS:
        candidate["rank_key"] = None
        candidate["rank_components"] = None
    else:
        candidate["rank_key"] = list(rank_key(candidate))
        candidate["rank_components"] = rank_components(candidate)
    return candidate


def build_opportunity_result(
    facts_items: Sequence[Mapping[str, Any]],
    brief: Any,
    request_as_of_s: int,
    *,
    policy: EventPolicy | None = None,
    now_s: int | None = None,
) -> dict[str, Any]:
    """把多个事件事实包打成一次机会结果（可执行区排序 + 不可执行区）。

    Returns:
        dict: ``candidates`` / ``executable`` / ``non_executable`` / ``result`` /
        ``policy_version``。
    """
    policy = policy or EventPolicy()
    brief = _ensure_brief(brief, policy)
    candidates = [build_candidate(item, brief, request_as_of_s, policy=policy) for item in facts_items]
    ranked = sort_candidates(candidates)
    result = {
        "policy_version": policy.policy_version,
        "policy": policy.as_dict(),
        "request_as_of_s": int(request_as_of_s),
        "generated_s": int(now_s) if now_s is not None else None,
        "ranked_event_ids": [candidate.get("event_id") for candidate in ranked["executable"]],
        "not_executable_event_ids": [candidate.get("event_id") for candidate in ranked["non_executable"]],
    }
    return {
        "candidates": candidates,
        "executable": ranked["executable"],
        "non_executable": ranked["non_executable"],
        "result": result,
        "policy_version": policy.policy_version,
    }


def request_fingerprint(
    brief: CreatorBrief,
    *,
    assessment_ids: Sequence[str] = (),
    request_as_of_s: int = 0,
    policy_version: str = "",
) -> str:
    """计算请求指纹（同指纹重复请求返回已有 run，不新建）。"""
    payload = {
        "brief": brief.to_dict(),
        "assessment_ids": sorted(str(a) for a in assessment_ids),
        "request_as_of_s": int(request_as_of_s),
        "policy_version": str(policy_version),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get_or_create_opportunity_run(
    repository: Any,
    *,
    brief: CreatorBrief,
    request_fingerprint: str,
    candidates: Sequence[Mapping[str, Any]],
    result: Mapping[str, Any],
    assessment_ids: Sequence[str] = (),
    policy_version: str | None = None,
    run_id: str | None = None,
    now_s: int | None = None,
) -> Any:
    """按指纹取回或新建 OpportunityRun（**同指纹不新建**；brief 作为不可变输入冻结）。

    Args:
        repository: 3a ``HotEventRepository``。
        brief: 已校验的创作者简报。
        request_fingerprint: 请求指纹。
        candidates: 候选列表。
        result: 结果 JSON。
        assessment_ids: 引用的评估 ID。
        policy_version: 政策版本（缺省取默认策略）。
        run_id: 显式 run ID（缺省随机）。
        now_s: 创建时刻。

    Returns:
        OpportunityRun: 已有或新建的 run 行。
    """
    existing = repository.get_opportunity_run_by_fingerprint(request_fingerprint)
    if existing is not None:
        return existing
    return repository.create_opportunity_run(
        run_id=run_id or f"opp-{uuid.uuid4().hex[:16]}",
        policy_version=policy_version or EventPolicy().policy_version,
        request_fingerprint=request_fingerprint,
        now_s=now_s,
        revision=1,
        creator_brief=brief.to_dict(),
        assessment_ids=[str(a) for a in assessment_ids],
        candidates=[_jsonable(candidate) for candidate in candidates],
        result=_jsonable(result),
        feedback=[],
    )


def append_feedback(
    repository: Any,
    run_id: str,
    entry: Mapping[str, Any],
    *,
    expected_revision: int | None = None,
    now_s: int | None = None,
) -> Any:
    """追加一条反馈（**append 型，不覆盖原推荐条件**）。

    Args:
        repository: 3a ``HotEventRepository``。
        run_id: 机会 run ID。
        entry: 反馈内容（建议含 ``feedback_id`` / ``kind``）。
        expected_revision: 期望 revision（CAS）；不匹配则报冲突。
        now_s: 追加时刻。

    Returns:
        OpportunityRun: 更新后的 run（``creator_brief`` / ``candidates`` 逐字未变）。
    """
    return repository.append_opportunity_feedback(
        run_id,
        entry,
        expected_revision=expected_revision,
        now_s=now_s,
    )


__all__ = [
    "ACTION_MAKE_CANDIDATE",
    "ACTION_PREPARE_OR_PILOT",
    "ACTION_DIFFERENTIATE_RESEARCH",
    "ACTION_WATCH_AND_COLLECT",
    "ACTION_NOT_SUITABLE",
    "ACTION_DEADLINE_MISSED",
    "ALL_ACTIONS",
    "EXECUTABLE_ACTIONS",
    "NON_EXECUTABLE_ACTIONS",
    "ACTION_ORDER",
    "MIN_EVIDENCE_REFS",
    "BANNED_PHRASES",
    "FEASIBLE_DEADLINE_FEASIBLE",
    "FEASIBLE_NO_KNOWN_DEADLINE",
    "FEASIBLE_AT_RISK",
    "compute_publish_eta_s",
    "deadline_status",
    "evaluate_choice",
    "choose_action",
    "rank_components",
    "rank_key",
    "sort_candidates",
    "contains_banned_phrase",
    "build_explanation",
    "build_candidate",
    "build_opportunity_result",
    "request_fingerprint",
    "get_or_create_opportunity_run",
    "append_feedback",
]
