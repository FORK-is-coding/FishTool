"""04 · 第三批 e · CreatorBrief / 可行性硬门 / 机会规则测试。

覆盖 §16.2 与 §16.2.1 的 E 项：E12 / E13 / E14 / E15 / E23 / E29 / E33 / E34 / E44 / E46，
外加本批钉死项：六 action 互斥顺序、``not_suitable``/``deadline_missed`` 不入排序、
负/超限时长被拒、非法 policy 启动即报错、解释引用不足如实说明、反馈不覆盖原推荐、
同 ``request_fingerprint`` 复用已有 run、反馈 append 的**真并发 CAS**。

口径与红线：
    - daily / early 事实**来自真实 3d 内核**（``aggregate_daily_triplet`` / ``evaluate_early``），
      不把机会内核或仓储打桩；
    - OpportunityRun 用**真临时 SQLite 事务**，并发用 ``threading.Barrier`` 真同时竞争；
    - 只 mock 外部 API/LLM —— 本文件根本不需要它们。

时间锚点：``T0`` = 2026-09-01T00:00:00Z，同时是 86400 与 7200 的公共网格点。
"""
from __future__ import annotations

import copy
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import DatabaseManager
from core.database.hot_event_repository import FeedbackRevisionConflict, HotEventRepository
from modules.hotspot.events.aggregator import PairedMeasure, matched_totals
from modules.hotspot.events.brief import (
    BriefValidationError,
    CreatorBrief,
)
from modules.hotspot.events.daily import aggregate_daily_triplet
from modules.hotspot.events.early import evaluate_early
from modules.hotspot.events.opportunity import (
    ACTION_DEADLINE_MISSED,
    ACTION_DIFFERENTIATE_RESEARCH,
    ACTION_MAKE_CANDIDATE,
    ACTION_NOT_SUITABLE,
    ACTION_PREPARE_OR_PILOT,
    ACTION_WATCH_AND_COLLECT,
    ALL_ACTIONS,
    NON_EXECUTABLE_ACTIONS,
    append_feedback,
    build_candidate,
    build_explanation,
    choose_action,
    compute_publish_eta_s,
    contains_banned_phrase,
    deadline_status,
    evaluate_choice,
    get_or_create_opportunity_run,
    rank_key,
    request_fingerprint,
    sort_candidates,
)
from modules.hotspot.events.policy import (
    EventPolicy,
    InvalidPolicyValue,
    UnsupportedPolicyField,
)
from modules.hotspot.events.windows import MemberRevision, SnapshotPoint

#: 2026-09-01T00:00:00Z —— 86400 与 7200 的公共网格点。
T0: int = 1788220800
#: daily 窗口宽度（秒）。
DAY_W: int = 86400
#: early 快窗宽度（秒）。
EARLY_W: int = 7200
#: daily 执行时钟：向下取整后窗口终点恰为 ``T0``。
DAY_AS_OF: int = T0 + 43200
#: 快采插点步长（秒）：远小于 40 分钟门。
FAST_STEP: int = 1200


# ===========================================================================
# fixtures：真临时 SQLite
# ===========================================================================


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：经 ``DatabaseManager`` 建六表 → 换可跨线程引擎产出会话工厂。"""
    path = tmp_path / "opportunity_policy.db"
    manager = DatabaseManager(str(path))
    manager.engine.dispose()
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def repo(db):
    """注入临时库会话工厂的 3a 仓储，固定时钟指向 ``T0``。"""
    return HotEventRepository(session_factory=db, clock=lambda: T0)


# ===========================================================================
# 构造工具（真内核输入）
# ===========================================================================


def _rev(bvid: str, owner_mid: int | None, decision_at_s: int) -> MemberRevision:
    """构造一条 ``accepted`` 成员版本（revision=1）。"""
    return MemberRevision(
        bvid=bvid,
        owner_mid=owner_mid,
        status="accepted",
        revision=1,
        decision_at_s=decision_at_s,
        first_seen_s=decision_at_s,
    )


def _points(series: list[tuple[int, int]]) -> list[SnapshotPoint]:
    """把 ``(epoch_s, view)`` 序列转成有效快照点。"""
    return [SnapshotPoint(epoch_s=e, view=v, view_ok=True) for e, v in series]


def _render(anchors: list[tuple[int, int]], gap_s: int) -> list[tuple[int, int]]:
    """在锚点之间按 ``gap_s`` 线性插点（保证采样间隔满足质量门）。"""
    out: list[tuple[int, int]] = []
    for i, (epoch_s, view) in enumerate(anchors):
        out.append((epoch_s, int(view)))
        if i + 1 < len(anchors):
            nxt_epoch, nxt_view = anchors[i + 1]
            cur = epoch_s + gap_s
            while cur < nxt_epoch:
                ratio = (cur - epoch_s) / (nxt_epoch - epoch_s)
                out.append((cur, int(round(view + (nxt_view - view) * ratio))))
                cur += gap_s
    return out


def _daily_points(w1: int, w2: int, w3: int, *, base: int = 1000) -> list[tuple[int, int]]:
    """按三个日窗增量生成四个锚点（累计播放单调不减）。"""
    return [
        (T0 - 3 * DAY_W, base),
        (T0 - 2 * DAY_W, base + w1),
        (T0 - DAY_W, base + w1 + w2),
        (T0, base + w1 + w2 + w3),
    ]


def _build_daily(
    triples: list[tuple[int, int, int]],
    owners: list[int | None],
    *,
    prefix: str = "D",
    base: int = 1000,
    as_of: int = DAY_AS_OF,
    request_as_of: int | None = None,
) -> dict:
    """用真实内核算一份日级事实。"""
    revisions: dict = {}
    points: dict = {}
    for index, (triple, owner) in enumerate(zip(triples, owners)):
        bvid = f"{prefix}{index}"
        revisions[bvid] = [_rev(bvid, owner, T0 - 4 * DAY_W)]
        points[bvid] = _points(_daily_points(*triple, base=base))
    return aggregate_daily_triplet(
        revisions,
        points,
        as_of_s=as_of,
        request_as_of_s=request_as_of if request_as_of is not None else as_of,
    )


def _fast_series(end_s: int, d1: int, d2: int, *, base: int = 100) -> list[tuple[int, int]]:
    """按两个 2h 快窗增量生成稠密点（分割点精确落在 ``end-EW``）。"""
    anchors = [
        (end_s - 2 * EARLY_W, base),
        (end_s - EARLY_W, base + d1),
        (end_s, base + d1 + d2),
    ]
    return _render(anchors, FAST_STEP)


def _build_early(
    d1: int,
    d2: int,
    *,
    prefix: str = "F",
    members: int = 3,
    owners: list[int | None] | None = None,
    end_s: int = T0,
    as_of: int | None = None,
    request_as_of: int | None = None,
) -> dict:
    """用真实内核算一份 early 事实。"""
    owner_list = owners if owners is not None else [i + 1 for i in range(members)]
    revisions: dict = {}
    points: dict = {}
    for index in range(members):
        bvid = f"{prefix}{index}"
        revisions[bvid] = [_rev(bvid, owner_list[index], T0 - 4 * EARLY_W)]
        points[bvid] = _points(_fast_series(end_s, d1, d2))
    effective_as_of = end_s if as_of is None else as_of
    effective_request = effective_as_of if request_as_of is None else request_as_of
    return evaluate_early(
        revisions,
        points,
        fast_panel_bvids=[f"{prefix}{i}" for i in range(members)],
        as_of_s=effective_as_of,
        request_as_of_s=effective_request,
        fast_last_observation_s=effective_request,
    )


def _brief(**overrides) -> CreatorBrief:
    """构造一个默认「显式匹配游戏 / 短视频 / 有录屏」的 brief。"""
    payload = {
        "brief_version": "v1",
        "production_hours": 1.0,
        "review_hours": 0.5,
        "publish_buffer_hours": 0.5,
        "max_experiment_hours": 8.0,
        "content_domains": ["游戏"],
        "allowed_entities": ["游戏"],
        "supported_formats": ["短视频"],
        "preferred_angles": ["实测"],
        "available_assets": ["录屏"],
    }
    payload.update(overrides)
    return CreatorBrief.from_dict(payload)


def _facts(**overrides) -> dict:
    """构造默认事实包（显式匹配、无截止、无排除）。"""
    facts = {
        "event_id": "ev1",
        "entities": ["游戏"],
        "domains": ["游戏"],
        "required_formats": ["短视频"],
        "required_assets": [],
        "deadline": {"deadline_known": False},
        "mode": "realtime",
        "evidence_refs": [
            {"bvid": "BV1", "openable": True},
            {"bvid": "BV2", "openable": True},
        ],
    }
    facts.update(overrides)
    return facts


def _strong_daily() -> dict:
    """日级 confirmed rising + 过样本门的真实结果。"""
    return _build_daily([(300, 450, 450)] * 3, [1, 2, 3])


def _stable_daily() -> dict:
    """日级 stable + 过样本门的真实结果。"""
    return _build_daily([(300, 300, 300)] * 3, [1, 2, 3])


def _make_run(repo, *, fingerprint: str = "fp-1", brief: CreatorBrief | None = None):
    """建一个最小 OpportunityRun（真库）。"""
    brief = brief or _brief()
    candidate = build_candidate(_facts(daily=_strong_daily()), brief, T0)
    return get_or_create_opportunity_run(
        repo,
        brief=brief,
        request_fingerprint=fingerprint,
        candidates=[candidate],
        result={"ranked_event_ids": [candidate["event_id"]]},
        assessment_ids=["asmt-1"],
        now_s=T0,
    )


# ===========================================================================
# E12：日级弱但 2h 强 → 只给 prepare_or_pilot，不宣称日级确认
# ===========================================================================


def test_E12_weak_daily_strong_early_gives_pilot_only() -> None:
    """E12：early action = ``prepare_or_pilot``，**不宣称日级确认**。"""
    daily = _stable_daily()
    early = _build_early(10, 30)
    assert daily["topic_phase"] == "stable"
    assert early["status"] == "complete" and early["early_growth_signal"] is True

    facts = _facts(daily=daily, early=early)
    brief = _brief()
    choice = evaluate_choice(facts, brief, T0)
    assert choice["action"] == ACTION_PREPARE_OR_PILOT
    assert choice["matched_order"] == 5
    assert choice["action"] != ACTION_MAKE_CANDIDATE

    explanation = build_explanation(facts, brief, choice["action"], request_as_of_s=T0)
    # 早期信号不得冒充日级确认。
    assert explanation["claimed_daily_confirmation"] is False
    assert early["claimed_daily_confirmation"] is False
    assert contains_banned_phrase(explanation["text"]) is None


# ===========================================================================
# E13：硬门先于排序（高热度不覆盖截止）
# ===========================================================================


def test_E13_deadline_hard_gate_beats_hot_trend() -> None:
    """E13：deadline 剩 4h、制作 5h → ``deadline_missed``，高热度不覆盖硬门。"""
    request_as_of = T0
    daily = _strong_daily()
    brief = _brief(production_hours=5.0, review_hours=0.0, publish_buffer_hours=0.0)
    facts = _facts(
        daily=daily,
        deadline={
            "deadline_known": True,
            "deadline_s": request_as_of + 4 * 3600,
            "deadline_confidence": 0.9,
            "bound_to_activity": True,
        },
    )
    assert choose_action(facts, brief, request_as_of) == ACTION_DEADLINE_MISSED

    # 同一份「很热」的日级事实，只把截止放宽 → 立刻变成可优先制作。
    relaxed = copy.deepcopy(facts)
    relaxed["deadline"]["deadline_s"] = request_as_of + 48 * 3600
    assert choose_action(relaxed, brief, request_as_of) == ACTION_MAKE_CANDIDATE

    # 未绑定该活动的复盘角度不继承此截止。
    unbound = copy.deepcopy(facts)
    unbound["deadline"]["bound_to_activity"] = False
    assert choose_action(unbound, brief, request_as_of) == ACTION_MAKE_CANDIDATE

    # 截止不可信（低于 policy 门槛）→ 视为寿命未知，不误判 deadline_missed。
    untrusted = copy.deepcopy(facts)
    untrusted["deadline"]["deadline_confidence"] = 0.1
    assert choose_action(untrusted, brief, request_as_of) != ACTION_DEADLINE_MISSED


# ===========================================================================
# E14：UGC 无截止 → longevity_unknown，不编 ETA 成功保证
# ===========================================================================


def test_E14_no_deadline_marks_longevity_unknown() -> None:
    """E14：UGC 无截止 → ``longevity_unknown``，不编“还剩 N 天”。"""
    request_as_of = T0
    brief = _brief()
    facts = _facts(daily=_strong_daily(), deadline={"deadline_known": False})

    status = deadline_status(facts, brief, request_as_of)
    assert status["deadline_known"] is False
    assert status["deadline_trusted"] is False
    assert status["longevity_unknown"] is True
    assert status["feasibility_category"] == "no_known_deadline"
    assert status["deadline_feasible"] is True  # 无已知截止不算失败

    action = choose_action(facts, brief, request_as_of)
    explanation = build_explanation(facts, brief, action, request_as_of_s=request_as_of)
    assert explanation["longevity_unknown"] is True
    for phrase in ("还剩", "保证", "肯定来得及", "还能火"):
        assert phrase not in explanation["text"]
    assert contains_banned_phrase(explanation["text"]) is None


# ===========================================================================
# E15：显式排除最强
# ===========================================================================


def test_E15_explicit_exclusion_wins_even_when_trend_is_hot() -> None:
    """E15：用户明确排除该领域 → ``not_suitable``，即便趋势很强。"""
    brief = _brief(excluded_topics=["游戏"])
    facts = _facts(daily=_strong_daily())
    choice = evaluate_choice(facts, brief, T0)
    assert choice["action"] == ACTION_NOT_SUITABLE
    assert choice["matched_order"] == 1
    # 趋势很强也不救：与同一份事实的“未排除”结果对比。
    assert choose_action(facts, _brief(), T0) == ACTION_MAKE_CANDIDATE

    # 不可替代素材缺失 → not_suitable；可改形式 → watch_and_collect + adjust_format。
    heavy = _facts(daily=_strong_daily(), required_assets=["采访"], adjustable_assets=[])
    assert choose_action(heavy, _brief(), T0) == ACTION_NOT_SUITABLE
    adjustable = _facts(daily=_strong_daily(), required_assets=["采访"], adjustable_assets=["采访"])
    ci = evaluate_choice(adjustable, _brief(), T0)
    assert ci["action"] == ACTION_WATCH_AND_COLLECT
    assert "adjust_format" in ci["notes"]


# ===========================================================================
# E29：TTL 从证据窗算，不因新 run 续命
# ===========================================================================


def test_E29_ttl_computed_from_evidence_window_not_run_creation(repo) -> None:
    """E29：06:00 窗结束、10:00 重新请求 → TTL 从 06:00 算，不给实时 ``prepare_or_pilot``。"""
    window_end = T0 + 6 * 3600  # 06:00
    request_as_of = T0 + 10 * 3600  # 10:00
    early = _build_early(10, 30, end_s=window_end, as_of=window_end, request_as_of=request_as_of)

    # TTL 从证据 window_end_s 算（7200s），而不是从重新请求 / 新建 run 的时间算。
    assert early["window_end_s"] == window_end
    assert early["ttl_expires_s"] == window_end + early["fast_ttl_seconds"]
    assert early["ttl_active"] is False

    facts = _facts(daily=_stable_daily(), early=early)
    brief = _brief()
    action = choose_action(facts, brief, request_as_of)
    assert action not in (ACTION_PREPARE_OR_PILOT, ACTION_MAKE_CANDIDATE)

    # 新生成 OpportunityRun 不重置 TTL：建完 run 再判，结论逐字不变。
    run = _make_run(repo, fingerprint="fp-ttl")
    assert run is not None
    assert choose_action(facts, brief, request_as_of) == action

    # 隔离验证：把 TTL 单独拎出来——证据窗旧但 TTL 之内 → pilot；TTL 之外 → 不给 pilot。
    base_early = {
        "status": "complete",
        "signal": "growth",
        "early_growth_signal": True,
        "window_end_s": window_end,
        "fast_ttl_seconds": 7200,
        "ttl_active": True,
    }
    fresh = _facts(daily=_stable_daily(), early=dict(base_early))
    assert choose_action(fresh, brief, window_end + 600) == ACTION_PREPARE_OR_PILOT
    stale = _facts(daily=_stable_daily(), early=dict(base_early))
    assert choose_action(stale, brief, window_end + 7200 + 1) != ACTION_PREPARE_OR_PILOT


# ===========================================================================
# E33：确认度高 ≠ 优先做
# ===========================================================================


def test_E33_declining_confirmed_but_no_new_clue_watches() -> None:
    """E33：日级 declining、样本多 / 覆盖高，无新增线索 → ``watch_and_collect``。"""
    daily = _build_daily([(1000, 700, 400)] * 3, [1, 2, 3])
    assert daily["topic_phase"] == "declining"
    assert daily["sample_gate_passed"] is True
    assert daily["stage_reason"] == "confirmed_cooling"

    action = choose_action(_facts(daily=daily), _brief(), T0)
    assert action == ACTION_WATCH_AND_COLLECT
    assert action != ACTION_MAKE_CANDIDATE

    # 有明确可验证的新材料时，才允许转成差异化研究。
    with_material = _facts(daily=daily, has_new_material=True)
    assert choose_action(with_material, _brief(), T0) == ACTION_DIFFERENTIATE_RESEARCH


# ===========================================================================
# E34：全零不推荐热门制作
# ===========================================================================


def test_E34_all_zero_attention_false_blocks_hot_action() -> None:
    """E34：A=B=C=0、全有效且多作者 → ``attention_present=false``，不推荐热门制作。"""
    daily = _build_daily([(0, 0, 0)] * 3, [1, 2, 3])
    assert daily["topic_phase"] == "stable"
    assert daily["stage_reason"] == "stable_no_attention"
    assert daily["attention_present"] is False
    assert daily["available_windows"] == 3
    assert daily["panel_video_count"] == 3 and daily["panel_author_count"] == 3

    action = choose_action(_facts(daily=daily), _brief(), T0)
    assert action == ACTION_WATCH_AND_COLLECT
    assert action != ACTION_MAKE_CANDIDATE


# ===========================================================================
# E44：转折 undetermined 不给 make_candidate
# ===========================================================================


def test_E44_turning_undetermined_not_stable_nor_confirmed() -> None:
    """E44：n=3、A=0/B=90/C=0 → 转折 ``undetermined``，不给 ``make_candidate``。"""
    daily = _build_daily([(0, 90, 0)] * 3, [1, 2, 3])
    assert daily["a_delta"] == 0 and daily["b_delta"] == 270 and daily["c_delta"] == 0
    assert daily["topic_phase"] == "undetermined"
    assert daily["stage_reason"] == "turning_signal"

    action = choose_action(_facts(daily=daily), _brief(), T0)
    assert action not in (ACTION_MAKE_CANDIDATE, ACTION_PREPARE_OR_PILOT)
    assert action == ACTION_WATCH_AND_COLLECT


# ===========================================================================
# E46：未知作者过多不推荐广泛扩散
# ===========================================================================


def test_E46_unknown_author_coverage_insufficient_blocks_promotion() -> None:
    """E46：已知 2 作者但未知作者贡献 90% → 不推荐广泛扩散 / ``make_candidate``。"""
    daily = _build_daily([(5, 5, 5), (5, 5, 5), (90, 90, 90)], [1, 2, None])
    assert daily["author_coverage_insufficient"] is True
    assert daily["panel_author_count"] == 2
    assert daily["unknown_author_delta_share"] == pytest.approx(0.9)
    assert daily["broad_diffusion"] is False

    action = choose_action(_facts(daily=daily), _brief(), T0)
    assert action not in (ACTION_MAKE_CANDIDATE, ACTION_DIFFERENTIATE_RESEARCH)
    assert action == ACTION_WATCH_AND_COLLECT


# ===========================================================================
# E23：0 分母不算门槛通过，也不当无限增长
# ===========================================================================


def test_E23_zero_denominator_is_null_not_zero_nor_infinity() -> None:
    """E23：0 分母 / 全零增量 → ``relative_change=null`` **不算门槛通过**。"""
    # 底层纯函数口径：A=0 → relative_change=None（不是 0，也不是爆炸百分比）。
    totals = matched_totals(
        [PairedMeasure(bvid="BV1", owner_mid=1, before=0.0, after=90.0, quality_ok=True)]
    )
    assert totals["relative_change"] is None
    assert totals["delta_difference"] == 90.0

    daily = _build_daily([(0, 90, 90)] * 3, [1, 2, 3])
    assert daily["topic_phase"] == "rising"
    assert daily["stage_reason"] == "rising_low_base"
    assert daily["relative_change"] is None

    action = choose_action(_facts(daily=daily), _brief(), T0)
    assert action not in (ACTION_MAKE_CANDIDATE, ACTION_PREPARE_OR_PILOT)

    # 全零增量同样不算通过。
    zero = _build_daily([(0, 0, 0)] * 3, [1, 2, 3])
    assert zero["relative_change"] is None
    assert choose_action(_facts(daily=zero), _brief(), T0) == ACTION_WATCH_AND_COLLECT


# ===========================================================================
# 六 action 互斥顺序（1→7，命中即返回）
# ===========================================================================


def test_six_actions_are_fixed_and_mutually_exclusive_in_order() -> None:
    """六种 action 固定命名；``choose_action`` 严格按 1→7 顺序命中第一条。"""
    assert ALL_ACTIONS == (
        "make_candidate",
        "prepare_or_pilot",
        "differentiate_research",
        "watch_and_collect",
        "not_suitable",
        "deadline_missed",
    )
    brief = _brief()
    strong = _strong_daily()

    # 1：排除 + 截止不可达 + 强趋势 → 仍是 not_suitable。
    both = _facts(
        daily=strong,
        deadline={"deadline_known": True, "deadline_s": T0 + 3600, "deadline_confidence": 0.9},
    )
    assert evaluate_choice(both, _brief(excluded_topics=["游戏"]), T0)["matched_order"] == 1

    # 2：不排除但截止不可达 → deadline_missed（即使趋势强）。
    assert evaluate_choice(both, brief, T0)["matched_order"] == 2

    # 4：日级 rising + early 也强 → make_candidate（顺序在 pilot 之前）。
    both_signal = _facts(daily=strong, early=_build_early(10, 30))
    assert evaluate_choice(both_signal, brief, T0)["matched_order"] == 4

    # 5：日级非 rising，early 强 → prepare_or_pilot。
    assert evaluate_choice(_facts(daily=_stable_daily(), early=_build_early(10, 30)), brief, T0)[
        "matched_order"
    ] == 5

    # 6：stable/declining + 新材料 → differentiate_research。
    assert evaluate_choice(_facts(daily=_stable_daily(), has_new_material=True), brief, T0)[
        "matched_order"
    ] == 6

    # 7：证据不充分 → watch_and_collect。
    assert evaluate_choice(_facts(daily={}), brief, T0)["matched_order"] == 7

    # 历史模式只允许 differentiate_research / watch_and_collect。
    historical = _facts(daily=strong, mode="historical")
    assert choose_action(historical, brief, T0) in (
        ACTION_DIFFERENTIATE_RESEARCH,
        ACTION_WATCH_AND_COLLECT,
    )
    historical_with_material = _facts(daily=strong, mode="historical", has_new_material=True)
    assert choose_action(historical_with_material, brief, T0) == ACTION_DIFFERENTIATE_RESEARCH


# ===========================================================================
# not_suitable / deadline_missed 不入排序
# ===========================================================================


def test_non_executable_actions_stay_out_of_ranking() -> None:
    """``not_suitable`` / ``deadline_missed`` 只在独立不可执行区，不参与“优先做”排序。"""
    brief = _brief()
    candidates = [
        build_candidate(_facts(event_id="ev-hot", daily=_strong_daily()), brief, T0),
        build_candidate(
            _facts(
                event_id="ev-skip",
                daily=_strong_daily(),
                deadline={"deadline_known": True, "deadline_s": T0 + 3600, "deadline_confidence": 0.9},
            ),
            brief,
            T0,
        ),
        build_candidate(_facts(event_id="ev-excluded", daily=_strong_daily()), _brief(excluded_topics=["游戏"]), T0),
    ]
    ranked = sort_candidates(candidates)
    executable_actions = {c["action"] for c in ranked["executable"]}
    non_exec_actions = {c["action"] for c in ranked["non_executable"]}

    assert ACTION_MAKE_CANDIDATE in executable_actions
    assert not (set(NON_EXECUTABLE_ACTIONS) & executable_actions)
    assert non_exec_actions == {ACTION_DEADLINE_MISSED, ACTION_NOT_SUITABLE}

    # rank_key 对不可执行 action 必须拒绝。
    for candidate in ranked["non_executable"]:
        with pytest.raises(ValueError):
            rank_key(candidate)
        assert candidate["rank_key"] is None

    # 可执行项的 rank_key 稳定且可解释。
    keys = [rank_key(c) for c in ranked["executable"]]
    assert keys == sorted(keys)
    components = ranked["executable"][0]["rank_components"]
    assert components["action_rank"] == 0
    assert "account_match_rank" in components and "source_coverage" in components


# ===========================================================================
# 时长校验：负 / 超限被拒
# ===========================================================================


def test_negative_and_over_limit_durations_are_rejected() -> None:
    """负制作时间 / 超限工时不通过校验（**禁止负制作时间绕过截止**）。"""
    with pytest.raises(BriefValidationError):
        _brief(production_hours=-1.0)
    with pytest.raises(BriefValidationError):
        _brief(max_experiment_hours=-0.5)
    with pytest.raises(BriefValidationError):
        _brief(production_hours=1e9)

    policy = EventPolicy()
    with pytest.raises(BriefValidationError):
        _brief(production_hours=policy.duration_max_hours + 1.0)
    with pytest.raises(BriefValidationError):
        _brief(max_experiment_hours=policy.max_experiment_hours_limit + 1.0)
    # 缺省不能静默当 0（否则等于用 0 绕过截止）。
    with pytest.raises(BriefValidationError):
        CreatorBrief.from_dict({"brief_version": "v1"})

    brief = _brief()
    with pytest.raises(ValueError):
        compute_publish_eta_s(T0, CreatorBrief(brief_version="v", production_hours=-1.0))
    # 正常 ETA 口径。
    assert compute_publish_eta_s(
        T0, _brief(production_hours=1.0, review_hours=1.0, publish_buffer_hours=1.0)
    ) == T0 + 3 * 3600

    # 不支持字段 / 风险偏好越权一律报错（不假装做了法律风险评分）。
    with pytest.raises(BriefValidationError):
        CreatorBrief.from_dict(
            {
                "brief_version": "v1",
                "production_hours": 1,
                "review_hours": 0,
                "publish_buffer_hours": 0,
                "max_experiment_hours": 1,
                "smart_inference": {"niche": "猜的"},
            }
        )
    with pytest.raises(BriefValidationError):
        _brief(risk_preferences={"legal_risk_score": 0.1})


# ===========================================================================
# 非法 policy 启动即报错（不静默降级）
# ===========================================================================


class _FakeConfig:
    """最小 ConfigManager 替身：只实现 ``get``。"""

    def __init__(self, value):
        self._value = value

    def get(self, key, default=None):
        return self._value if key == "hotspot.events" else default


def test_invalid_policy_fails_fast_instead_of_silent_fallback() -> None:
    """配置含不支持字段 / 非法阈值 → 显式报错，**不静默换默认值**。"""
    with pytest.raises(UnsupportedPolicyField):
        EventPolicy.from_config(_FakeConfig({"unknown_field": 1}))
    with pytest.raises(InvalidPolicyValue):
        EventPolicy.from_config(_FakeConfig({"safety_margin_s": -1}))
    with pytest.raises(InvalidPolicyValue):
        EventPolicy.from_config(_FakeConfig({"safety_margin_s": 60.5}))
    with pytest.raises(InvalidPolicyValue):
        EventPolicy.from_config(_FakeConfig({"deadline_confidence_min": 2.0}))
    with pytest.raises(InvalidPolicyValue):
        EventPolicy.from_config(_FakeConfig({"duration_max_hours": 0}))
    with pytest.raises(InvalidPolicyValue):
        EventPolicy.from_config(_FakeConfig("not-a-mapping"))
    with pytest.raises(UnsupportedPolicyField):
        EventPolicy.build(nope=1)
    with pytest.raises(InvalidPolicyValue):
        EventPolicy.build(max_experiment_hours_limit=999.0, duration_max_hours=10.0)

    # 合法配置可用，且 policy_version 反映**实际配置**（不是固定默认值）。
    base = EventPolicy.from_config(_FakeConfig({}))
    tuned = EventPolicy.from_config(_FakeConfig({"safety_margin_s": 3600}))
    assert base.policy_version != tuned.policy_version
    assert tuned.safety_margin_s == 3600
    # 更严的 safety_margin 会把原本可行的截止改判为不可达（阈值真的生效）。
    brief = _brief(production_hours=3.0, review_hours=0.0, publish_buffer_hours=0.0)
    facts = _facts(
        daily=_strong_daily(),
        deadline={"deadline_known": True, "deadline_s": T0 + 6 * 3600, "deadline_confidence": 0.9},
    )
    # 默认安全余量（2h）下可行；把余量调大即改判为不可达 —— 说明阈值真的生效。
    assert choose_action(facts, brief, T0, policy=base) == ACTION_MAKE_CANDIDATE
    strict = EventPolicy.build(safety_margin_s=14400)
    assert choose_action(facts, brief, T0, policy=strict) == ACTION_DEADLINE_MISSED


def test_generation_keys_allowed_while_unknown_keys_still_rejected() -> None:
    """§0：生成账本两键与 EventPolicy 同段放行；白名单外多余键仍报配置错误。"""
    # 正例：hotspot.events 段出现生成账本两键不再报错（3f 生成账本 → 3g 接线）。
    policy = EventPolicy.from_config(
        _FakeConfig(
            {
                "generation_lease_seconds": 300,
                "generation_deadline_seconds": 120,
            }
        )
    )
    # 两键只放行 + 形状校验，不并入策略语义、不影响 policy_version。
    assert not hasattr(policy, "generation_lease_seconds")
    assert policy.policy_version == EventPolicy.from_config(_FakeConfig({})).policy_version

    # 形状非法仍报错（不静默放行坏值）。
    with pytest.raises(InvalidPolicyValue):
        EventPolicy.from_config(_FakeConfig({"generation_lease_seconds": 0}))
    with pytest.raises(InvalidPolicyValue):
        EventPolicy.from_config(_FakeConfig({"generation_deadline_seconds": -5}))

    # 反向：白名单之外的未知键，即使与两把合法键同时出现，也必须被拒。
    with pytest.raises(UnsupportedPolicyField):
        EventPolicy.from_config(
            _FakeConfig(
                {
                    "generation_lease_seconds": 300,
                    "generation_deadline_seconds": 120,
                    "totally_unknown_key": 1,
                }
            )
        )


# ===========================================================================
# 解释：引用不足如实说，不伪造
# ===========================================================================


def test_explanation_reports_evidence_shortfall_honestly() -> None:
    """引用少于 2 条时**如实说明实际数量**，不伪造 BVID。"""
    brief = _brief()
    thin = _facts(
        daily=_strong_daily(),
        evidence_refs=[{"bvid": "BV-open", "openable": True}, {"bvid": "BV-closed", "openable": False}],
    )
    explanation = build_explanation(thin, brief, ACTION_MAKE_CANDIDATE, request_as_of_s=T0)
    assert explanation["evidence_ref_count"] == 1
    assert explanation["evidence_ref_shortfall"] is True
    assert explanation["evidence_refs"] == ["BV-open"]
    assert "BV-closed" not in explanation["evidence_refs"]
    assert "1" in explanation["evidence_ref_note"]

    rich = _facts(daily=_strong_daily())
    ok = build_explanation(rich, brief, ACTION_MAKE_CANDIDATE, request_as_of_s=T0)
    assert ok["evidence_ref_count"] == 2
    assert ok["evidence_ref_shortfall"] is False

    # 禁止话术一律不得出现。
    assert contains_banned_phrase("全网热度上涨 300%") is not None
    assert contains_banned_phrase("保证还有两天红利") is not None
    assert contains_banned_phrase("成功率 90%") is not None
    assert contains_banned_phrase(explanation["text"]) is None


# ===========================================================================
# OpportunityRun：同指纹复用 + 反馈 append（不覆盖原推荐）+ 真并发
# ===========================================================================


def test_same_fingerprint_reuses_existing_run(repo) -> None:
    """同一 ``request_fingerprint`` 重复请求返回已有 run，不新建。"""
    brief = _brief()
    candidate = build_candidate(_facts(daily=_strong_daily()), brief, T0)
    fingerprint = request_fingerprint(brief, assessment_ids=["asmt-1"], request_as_of_s=T0)

    first = get_or_create_opportunity_run(
        repo,
        brief=brief,
        request_fingerprint=fingerprint,
        candidates=[candidate],
        result={"ranked_event_ids": ["ev1"]},
        assessment_ids=["asmt-1"],
        now_s=T0,
    )
    second = get_or_create_opportunity_run(
        repo,
        brief=brief,
        request_fingerprint=fingerprint,
        candidates=[candidate],
        result={"ranked_event_ids": ["ev1"]},
        assessment_ids=["asmt-1"],
        now_s=T0 + 100,
    )
    assert first.id == second.id


def test_feedback_append_does_not_overwrite_recommendation(repo) -> None:
    """反馈 append 后 ``creator_brief`` / ``candidates`` / ``rank_key`` **逐字未变**。"""
    run = _make_run(repo, fingerprint="fp-append")
    original_brief = copy.deepcopy(run.creator_brief)
    original_candidates = copy.deepcopy(run.candidates)
    original_rank_key = run.candidates[0]["rank_key"]

    updated = append_feedback(repo, run.id, {"feedback_id": "f1", "kind": "accepted"}, expected_revision=1, now_s=T0 + 10)
    assert updated.revision == 2
    assert len(updated.feedback) == 1
    assert updated.creator_brief == original_brief
    assert updated.candidates == original_candidates
    assert updated.candidates[0]["rank_key"] == original_rank_key

    # 旧 revision 重试必须冲突；新 revision 追加成功且原推荐仍不变。
    with pytest.raises(FeedbackRevisionConflict):
        append_feedback(repo, run.id, {"feedback_id": "f1b"}, expected_revision=1, now_s=T0 + 11)
    final = append_feedback(repo, run.id, {"feedback_id": "f2", "kind": "rejected"}, expected_revision=2, now_s=T0 + 12)
    assert final.revision == 3
    assert [entry["feedback_id"] for entry in final.feedback] == ["f1", "f2"]
    assert final.creator_brief == original_brief
    assert final.candidates == original_candidates
    assert final.candidates[0]["rank_key"] == original_rank_key


def test_feedback_concurrent_append_only_one_wins(repo) -> None:
    """真并发：两线程同 ``expected_revision`` 追加 → 只有一个成功。"""
    run = _make_run(repo, fingerprint="fp-concurrent")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(tag: int) -> None:
        barrier.wait()
        try:
            repo.append_opportunity_feedback(
                run.id,
                {"feedback_id": f"f{tag}"},
                expected_revision=1,
                now_s=T0 + tag,
            )
            outcomes.append("ok")
        except FeedbackRevisionConflict:
            outcomes.append("conflict")
        except Exception as exc:  # noqa: BLE001 - 把非预期异常带出来断言
            outcomes.append(f"error:{type(exc).__name__}")

    threads = [threading.Thread(target=worker, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["conflict", "ok"]
    final = repo.get_opportunity_run(run.id)
    assert final.revision == 2
    assert len(final.feedback) == 1
    # 原推荐条件在并发追加后依旧不可变。
    assert final.creator_brief == run.creator_brief
    assert final.candidates == run.candidates
