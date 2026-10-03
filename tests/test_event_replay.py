"""FishTool 04 · R5 第四批 b：完整历史回放 + E22 + 两处接线缺口收口（测试）。

规格：``FishTool_04_R5执行规格_第四批b_历史回放与接线缺口收口.md``（5421B / 116 行）

覆盖用例（对应规格 §4 表，共 12 条）：

- **E22**（§1.3）：未来快照 / 未来成员决定混入回放 → 被 ``as_of`` 门拒绝，推荐不含未来
- **two-times**（§1.1）：``as_of_s`` 与 ``window_end_s`` 分离；逐项输入越对应截止即被拒
- **mode**（§1.1）：历史重放必须 ``mode=historical``，不进即时机会队列
- **action 限制**（§1.4）：``historical`` 只出 ``differentiate_research`` / ``watch_and_collect``
- **新鲜度门**（§1.5）：daily 越过 36h / early 越过 2h 或快采末次越过 40min
  → 历史数字仍可展示，但 **不得**出 ``make_candidate`` / ``prepare_or_pilot``
- **request_as_of**（§1.2）：回放必须显式传重放时刻，缺失即拒（不许省、不许回退成“现在”）
- **缺口②**（§2.1）：有 counters → 三件套真出数；空 counters → 安全降级
- **缺口③**（§2.2）：真实 config 调 ``EventPolicy.from_config()`` 不抛错，三项可读

纪律：**不触网、不落库、不碰任何红线文件**（``watch_*`` / ``algorithm/`` / 六表 /
``topic_generator*`` / ``topic_generation_service.py`` / ``supply.py`` / ``channel_b.py`` /
``event_watch_demands.py``）。本文件只**调用** ``algorithm/window_metrics.window_end_s``
这一纯函数，绝不修改它。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modules.hotspot.algorithm.window_metrics import window_end_s
from modules.hotspot.events import (
    DiscoveryRunView,
    MemberRevision,
    SnapshotPoint,
    discovery_signals,
)
from modules.hotspot.events.brief import CreatorBrief
from modules.hotspot.events.channel_a import channel_a_trend
from modules.hotspot.events.config import DAY_W, EARLY_W
from modules.hotspot.events.daily import aggregate_daily_triplet
from modules.hotspot.events.early import evaluate_early
from modules.hotspot.events.opportunity import (
    build_candidate,
    build_opportunity_result,
    compute_publish_eta_s,
    evaluate_choice,
)
from modules.hotspot.events.policy import (
    ALLOWED_POLICY_FIELDS,
    EventPolicy,
    InvalidPolicyValue,
    UnsupportedPolicyField,
)
from modules.hotspot.events.service import supply_members_from_counters

#: 缺口③ 涉及的三项 discovery 账本键（config.yaml 真实值 300 / 120 / 60）。
DISCOVERY_POLICY_KEYS: tuple[str, ...] = (
    "discovery_lease_seconds",
    "discovery_deadline_seconds",
    "manual_discovery_cooldown_seconds",
)

#: 固定重放时刻（epoch 秒），避免测试随真实时钟漂移。
REQUEST_AS_OF_S: int = 1_700_000_000

#: 2026-09-01T00:00:00Z —— 既是 UTC 零点，也是 86400 与 7200 的公共网格点。
T0: int = 1_788_220_800
#: 当日 12:00 执行时钟：daily 网格向下取整仍落在 ``T0``（两种时间由此分离）。
AS_OF: int = T0 + 43_200

#: 优先级动作（历史/陈旧证据下**一律不许**出现）。
PRIORITY_ACTIONS: tuple[str, ...] = ("make_candidate", "prepare_or_pilot")


class _FakeConfigManager:
    """最小 ``ConfigManager`` 替身：只回放 ``hotspot.events`` 段（用于 from_config 测试）。"""

    def __init__(self, section: object) -> None:
        self._section = section

    def get(self, key: str, default: object = None) -> object:
        """按 key 取值；只有 ``hotspot.events`` 命中，其余回默认。"""
        if key == "hotspot.events":
            return self._section
        return default


def _real_events_section() -> dict:
    """读取真实 ``config/config.yaml`` 的 ``hotspot.events`` 段（只读，不落库）。

    Returns:
        dict: ``hotspot.events`` 段映射；缺段时返回空 dict。
    """
    import yaml  # 局部导入：仅本用例需要，失败也只影响此用例

    cfg = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    return (data.get("hotspot", {}) or {}).get("events", {}) or {}


def _run_with(supply_members) -> DiscoveryRunView:
    """构造一个最小 ``DiscoveryRunView``（仅用于三件套输入）。"""
    return DiscoveryRunView(
        run_id="run-1",
        window_start_s=0,
        window_end_s=7200,
        plan_hash="plan",
        query_count=1,
        page_count=1,
        query_order=("q",),
        interval_s=7200,
        completed=True,
        supply_members=supply_members,
    )


def _minimal_brief() -> dict:
    """构造一份最小合法 brief（``brief_version`` + 四个必填时长字段）。"""
    return {
        "brief_version": "4b-test",
        "production_hours": 1.0,
        "review_hours": 1.0,
        "publish_buffer_hours": 1.0,
        "max_experiment_hours": 1.0,
    }


# ============================ 夹具：面板与 brief ============================


def _brief() -> CreatorBrief:
    """构造最小合法 ``CreatorBrief``；显式允许实体 = 事件实体，保证 ``explicit_match``。"""
    return CreatorBrief.from_dict(
        {
            "brief_version": "4b-replay",
            "production_hours": 1.0,
            "review_hours": 1.0,
            "publish_buffer_hours": 1.0,
            "max_experiment_hours": 10.0,
            "allowed_entities": ("剑与远征",),
        }
    )


def _rev(bvid: str, owner_mid: int | None, decision_at_s: int, revision: int = 1) -> MemberRevision:
    """构造一条 ``accepted`` 成员版本（默认 revision=1）。"""
    return MemberRevision(
        bvid=bvid,
        owner_mid=owner_mid,
        status="accepted",
        revision=revision,
        decision_at_s=decision_at_s,
        first_seen_s=decision_at_s,
    )


def _points(series: list[tuple[int, int]]) -> list[SnapshotPoint]:
    """把 ``(epoch_s, view)`` 序列转成有效快照点（``view_ok=True``）。"""
    return [SnapshotPoint(epoch_s=e, view=v, view_ok=True) for e, v in series]


def _two_window_series(w1: int, w2: int, *, base: int = 1000) -> list[tuple[int, int]]:
    """按两个日窗增量生成 3 个锚点（累计播放单调不减），供通道 A 双窗配对。"""
    return [
        (T0 - 2 * DAY_W, base),
        (T0 - DAY_W, base + w1),
        (T0, base + w1 + w2),
    ]


def _daily_series(w1: int, w2: int, w3: int, *, base: int = 1000) -> list[tuple[int, int]]:
    """按三个日窗增量生成 4 个锚点（累计播放单调不减），供日级三窗。"""
    return [
        (T0 - 3 * DAY_W, base),
        (T0 - 2 * DAY_W, base + w1),
        (T0 - DAY_W, base + w1 + w2),
        (T0, base + w1 + w2 + w3),
    ]


def _members(count: int, *, prefix: str = "M") -> dict[str, int]:
    """生成 ``{bvid: owner_mid}`` 映射（作者互不相同，便于作者数 / 集中度判定）。"""
    return {f"{prefix}{i}": i + 1 for i in range(1, count + 1)}


def _render(anchors: list[tuple[int, int]], gap_s: int) -> list[tuple[int, int]]:
    """在锚点之间按 ``gap_s`` 步长线性插点，保证采样间隔满足 early 的 40 分钟门。"""
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


def _fast_series(d1: int, d2: int, *, base: int = 100) -> list[tuple[int, int]]:
    """按两个 2h 快窗增量生成稠密点（窗口分割点精确落在 ``T-W``）。"""
    anchors = [
        (T0 - 2 * EARLY_W, base),
        (T0 - EARLY_W, base + d1),
        (T0, base + d1 + d2),
    ]
    return _render(anchors, 1200)


def _rising_daily_facts(*, request_as_of_s: int, prefix: str = "D") -> tuple[dict, dict]:
    """构造「3 成员 / 3 作者 / 三窗确认上升」的日级事实与原始聚合结果。

    Returns:
        tuple[dict, dict]: ``(facts, aggregated)`` —— facts 可直接喂 ``evaluate_choice``。
    """
    members = _members(3, prefix=prefix)
    revisions = {b: [_rev(b, mid, T0 - 4 * DAY_W)] for b, mid in members.items()}
    points = {b: _points(_daily_series(100, 150, 150)) for b in members}

    aggregated = aggregate_daily_triplet(
        revisions, points, as_of_s=AS_OF, request_as_of_s=request_as_of_s
    )
    facts = {
        "event_id": "ev-daily",
        "entities": ["剑与远征"],
        "daily": dict(aggregated),
        "early": {},
        "discovery": {},
    }
    return facts, aggregated


# ============================ 缺口③：EventPolicy 白名单 ============================


def test_policy_whitelist_includes_discovery_keys() -> None:
    """三项 discovery 账本键必须并入 ``ALLOWED_POLICY_FIELDS``。"""
    for key in DISCOVERY_POLICY_KEYS:
        assert key in ALLOWED_POLICY_FIELDS, f"{key} 未并入白名单"


def test_event_policy_from_real_config_does_not_raise() -> None:
    """对真实 config.yaml 的 ``hotspot.events`` 段调 ``from_config`` **不抛错**。"""
    section = _real_events_section()
    assert section, "config.yaml 缺少 hotspot.events 段"
    policy = EventPolicy.from_config(_FakeConfigManager(section))
    assert isinstance(policy, EventPolicy)


def test_discovery_keys_are_read_and_validated() -> None:
    """三项被**真正读取校验**：非法值必须报错（不是静默忽略）。"""
    for key in DISCOVERY_POLICY_KEYS:
        with pytest.raises(InvalidPolicyValue):
            EventPolicy.build(**{key: 0})
        with pytest.raises(InvalidPolicyValue):
            EventPolicy.build(**{key: -1})


def test_unknown_field_still_rejected() -> None:
    """白名单严格性不放松：未知键照旧报 ``UnsupportedPolicyField``。"""
    with pytest.raises(UnsupportedPolicyField):
        EventPolicy.build(not_a_real_key=1)


# ============================ 缺口②：supply_members 映射 ============================


def test_supply_members_mapping_yields_angle_density() -> None:
    """有 counters → 三件套**真出数**（``angle_share`` 不再全 ``None``）。"""
    counters = {
        "supply_members": [
            {
                "bvid": "BV1",
                "format": "long_video",
                "angle": "news",
                "content_depth": "title_description",
                "classification_source": "strict_rule",
            },
            {
                "bvid": "BV2",
                "format": "long_video",
                "angle": "tutorial",
                "content_depth": "provided_transcript",
                "classification_source": "manual",
            },
        ]
    }
    members = supply_members_from_counters(counters)
    assert len(members) == 2
    signals = discovery_signals(_run_with(members))
    angle = signals["angle_density"]
    assert angle["classified_count"] >= 1
    assert any(v is not None for v in angle["angle_share"].values())


def test_supply_members_mapping_empty_degrades_safely() -> None:
    """空 counters → **安全降级**（``angle_share`` 全 ``None``，不报错）。"""
    assert supply_members_from_counters({}) == ()
    signals = discovery_signals(_run_with(()))
    angle = signals["angle_density"]
    assert all(v is None for v in angle["angle_share"].values())


def test_supply_members_mapping_skips_invalid_and_missing_bvid() -> None:
    """非法枚举 / 缺 BVID 的条目被跳过，不拖垮整跑（safe 降级）。"""
    counters = {
        "supply_members": [
            {"bvid": "", "angle": "news"},  # 缺唯一 BVID → 跳过
            {"bvid": "BV3", "angle": "not_a_real_angle"},  # 非法枚举 → 跳过
            {"bvid": "BV4", "angle": "review"},  # 合法 → 保留
            "not-a-mapping",  # 非映射 → 跳过
        ]
    }
    members = supply_members_from_counters(counters)
    assert [m.bvid for m in members] == ["BV4"]


# ============================ action 限制：historical ============================


def test_historical_mode_never_prioritizes_old_opportunity() -> None:
    """历史模式 **不许把旧机会当「现在优先做」** → 绝不返回优先级动作。"""
    for extra in ({}, {"daily": {"mode": "historical"}}, {"early": {"mode": "historical"}}):
        facts = dict(extra)
        facts["mode"] = "historical"
        result = evaluate_choice(facts, _minimal_brief(), REQUEST_AS_OF_S, policy=EventPolicy())
        assert result["mode"] == "historical"
        assert result["action"] not in PRIORITY_ACTIONS, (
            f"历史模式返回了优先级动作：{result['action']}"
        )


# ============================ E22：未来混入回放 → as_of 门拒绝 ============================


def test_e22_future_snapshot_rejected_by_as_of_gate() -> None:
    """E22：未来快照 / 未来成员决定混入回放 → 被 ``as_of`` 门拒绝，推荐不含未来。"""
    # ---- 面板级：panel 有效窗边界（网格 T）永不越过本次执行时钟 as_of_s ----
    members = _members(3, prefix="W")
    points = {b: _points(_two_window_series(100, 100)) for b in members}
    baseline = {b: [_rev(b, mid, T0 - 4 * DAY_W)] for b, mid in members.items()}

    ok = channel_a_trend(baseline, points, as_of_s=AS_OF, replay=True)
    assert ok["mode"] == "historical"
    assert ok["window_end_s"] <= AS_OF

    # ---- 未来成员决定：某成员在 as_of 之后又提交了一版 accepted → 硬门拒绝 ----
    future_decision = AS_OF + 1
    crossed = dict(baseline)
    crossed["W1"] = [
        baseline["W1"][0],  # revision=1：知识截止前已接受，故仍进 U 分母
        _rev("W1", 1, future_decision, revision=2),  # revision=2：晚于执行时钟才提交
    ]
    with pytest.raises(ValueError, match="decision_after_as_of"):
        channel_a_trend(crossed, points, as_of_s=AS_OF, replay=True)

    # ---- 未来快照：证据窗边界越过重放请求时刻 → 推荐层判证据越界，只许观察 ----
    future_window_end = REQUEST_AS_OF_S + 1
    future_facts = {
        "event_id": "ev-22",
        "mode": "historical",
        "has_new_material": True,
        "daily": {
            "window_end_s": future_window_end,
            "topic_phase": "rising",
            "stage_reason": "confirmed_growth",
            "attention_present": True,
            "sample_gate_passed": True,
            "concentrated": False,
            "author_coverage_insufficient": False,
            "relative_change": 0.5,
            "member_coverage": 1.0,
        },
        "early": {"window_end_s": None},
        "discovery": {},
    }
    blocked = evaluate_choice(
        future_facts, _minimal_brief(), REQUEST_AS_OF_S, policy=EventPolicy()
    )
    assert blocked["action"] == "watch_and_collect"
    assert blocked["reason_code"] == "evidence_invalid_or_stale"
    assert blocked["action"] not in PRIORITY_ACTIONS

    # ---- 对照：同一事实把窗口收回请求时刻之内 → 历史模式只出研究/观察，仍不含优先级动作 ----
    in_range_facts = dict(future_facts)
    in_range_facts["daily"] = {**future_facts["daily"], "window_end_s": REQUEST_AS_OF_S}
    in_range = evaluate_choice(
        in_range_facts, _minimal_brief(), REQUEST_AS_OF_S, policy=EventPolicy()
    )
    assert in_range["mode"] == "historical"
    assert in_range["action"] == "differentiate_research"  # 未来快照被剔后仍不利用未来
    assert in_range["action"] not in PRIORITY_ACTIONS


# ============================ 两种时间：as_of_s 与 window_end_s 分离 ============================


def test_two_times_separation_and_cutoff_rejection() -> None:
    """两种时间：``as_of_s``（执行时钟）与 ``window_end_s``（网格边界）分离，越截止即被拒。"""
    as_of = AS_OF
    day_end = window_end_s(as_of, DAY_W)

    # ---- 分离：网格边界由 as_of 向下取整得到，且不越过执行时钟 ----
    assert day_end == (as_of // DAY_W) * DAY_W
    assert day_end <= as_of
    assert day_end != as_of  # 正午执行 → 日网格边界停在当日 00:00
    assert (as_of % DAY_W) == 43_200

    members = _members(3, prefix="T")
    points = {b: _points(_daily_series(100, 150, 150)) for b in members}
    baseline = {b: [_rev(b, mid, T0 - 4 * DAY_W)] for b, mid in members.items()}

    # panel 的「有效窗边界」= 网格边界 T，而不是执行时钟。
    ca = channel_a_trend(
        {b: [_rev(b, mid, T0 - 4 * DAY_W)] for b, mid in members.items()},
        {b: _points(_two_window_series(100, 100)) for b in members},
        as_of_s=as_of,
    )
    assert ca["window_end_s"] == day_end

    # ---- 逐项截止 1：成员决策以 as_of_s 为闭区间上限 ----
    at_bound = dict(baseline)
    at_bound["T1"] = [baseline["T1"][0], _rev("T1", 1, as_of, revision=2)]
    allowed = channel_a_trend(at_bound, points, as_of_s=as_of, replay=True)
    assert allowed["mode"] == "historical"
    assert allowed["window_end_s"] == day_end

    crossed = dict(baseline)
    crossed["T1"] = [baseline["T1"][0], _rev("T1", 1, as_of + 1, revision=2)]
    with pytest.raises(ValueError, match="decision_after_as_of"):
        channel_a_trend(crossed, points, as_of_s=as_of, replay=True)

    # ---- 逐项截止 2：新鲜度只看 request_as_of_s，不改网格边界 ----
    fresh = aggregate_daily_triplet(
        baseline, points, as_of_s=as_of, request_as_of_s=day_end + 36 * 3600
    )
    aged = aggregate_daily_triplet(
        baseline, points, as_of_s=as_of, request_as_of_s=day_end + 36 * 3600 + 1
    )
    assert fresh["window_end_s"] == day_end
    assert aged["window_end_s"] == day_end  # 两个时钟解耦：请求时刻不动网格
    assert fresh["stale"] is False
    assert aged["stale"] is True  # 越 36h 一位即判陈旧

    # ---- 逐项截止 3：证据窗越过 request_as_of_s → 推荐层判无效 ----
    cross_request_facts = {
        "event_id": "ev-times",
        "daily": {
            "window_end_s": day_end,
            "topic_phase": "rising",
            "stage_reason": "confirmed_growth",
            "attention_present": True,
            "sample_gate_passed": True,
            "concentrated": False,
            "author_coverage_insufficient": False,
            "relative_change": 0.5,
            "member_coverage": 1.0,
        },
        "early": {},
        "discovery": {},
    }
    blocked = evaluate_choice(
        cross_request_facts, _minimal_brief(), day_end - 1, policy=EventPolicy()
    )
    assert blocked["action"] == "watch_and_collect"
    assert blocked["reason_code"] == "evidence_invalid_or_stale"


# ============================ request_as_of 必传 ============================


def test_replay_requires_request_as_of() -> None:
    """回放必须显式传重放时刻当 ``request_as_of``；缺失即拒绝，不得回退成“当前时间”。"""
    brief = _brief()
    facts = {
        "event_id": "ev-req",
        "mode": "historical",
        "has_new_material": True,
        "daily": {"window_end_s": REQUEST_AS_OF_S - 3600},
        "early": {},
        "discovery": {},
    }

    # ---- 省略参数 = 拒绝（没有默认时钟） ----
    with pytest.raises(TypeError):
        evaluate_choice(facts, brief)  # type: ignore[call-arg]

    # ---- 显式 None / 非法值 = 拒绝，绝不悄悄用墙钟兜底 ----
    with pytest.raises(ValueError, match="invalid_request_as_of_s"):
        compute_publish_eta_s(None, brief)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_request_as_of_s"):
        evaluate_choice(facts, brief, None, policy=EventPolicy())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_request_as_of_s"):
        build_candidate(facts, brief, None, policy=EventPolicy())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_request_as_of_s"):
        evaluate_choice(facts, brief, -1, policy=EventPolicy())

    # ---- 只有显式传入才放行，且**原样回显**（就是那个重放时刻，不是“现在”） ----
    explicit = evaluate_choice(facts, brief, REQUEST_AS_OF_S, policy=EventPolicy())
    assert explicit["mode"] == "historical"
    assert explicit["action"] in ("differentiate_research", "watch_and_collect")

    payload = build_opportunity_result(
        [facts], brief, REQUEST_AS_OF_S, policy=EventPolicy()
    )
    assert payload["result"]["request_as_of_s"] == REQUEST_AS_OF_S
    assert payload["candidates"][0]["mode"] == "historical"


# ============================ 跨级新鲜度门 ============================


def test_cross_level_freshness_gate() -> None:
    """跨级新鲜度门：daily 越 36h / early 越 2h 或末次越 40min → 不出优先级动作。"""
    brief = _brief()
    policy = EventPolicy()

    # ---------- daily：越 36h 一位即陈旧；数字仍在，但不得再出优先级动作 ----------
    fresh_facts, fresh = _rising_daily_facts(request_as_of_s=AS_OF)
    assert fresh["stale"] is False
    assert fresh["topic_phase"] == "rising"
    assert fresh["sample_gate_passed"] is True
    chosen = evaluate_choice(fresh_facts, brief, AS_OF, policy=policy)
    assert chosen["action"] == "make_candidate"  # 正例：新鲜 + 确认上升

    aged_request = AS_OF + 36 * 3600 + 1
    aged_facts, aged = _rising_daily_facts(request_as_of_s=aged_request)
    assert aged["stale"] is True
    # 历史数字可展示：三窗数值未被抹掉。
    assert (aged["a_delta"], aged["b_delta"], aged["c_delta"]) == (300, 450, 450)
    assert aged["available_windows"] == 3
    gated = evaluate_choice(aged_facts, brief, aged_request, policy=policy)
    assert gated["action"] == "watch_and_collect"
    assert gated["reason_code"] == "evidence_invalid_or_stale"
    assert gated["action"] not in PRIORITY_ACTIONS

    # ---------- early：TTL <=2h 且快采末次 <=40min ----------
    emembers = _members(3, prefix="E")
    erev = {b: [_rev(b, mid, T0 - 4 * EARLY_W)] for b, mid in emembers.items()}
    epoints = {b: _points(_fast_series(10, 30)) for b in emembers}

    e_ok = evaluate_early(
        erev,
        epoints,
        fast_panel_bvids=list(emembers),
        as_of_s=T0,
        request_as_of_s=T0,
        fast_last_observation_s=T0,
    )
    assert e_ok["status"] == "complete"
    assert e_ok["early_growth_signal"] is True
    assert e_ok["ttl_active"] is True

    early_facts = {
        "event_id": "ev-early",
        "entities": ["剑与远征"],
        "daily": {
            "topic_phase": "stable",
            "concentrated": False,
            "author_coverage_insufficient": False,
            "a_delta": 0,
            "b_delta": 0,
            "c_delta": 0,
            "window_end_s": T0,
        },
        "early": dict(e_ok),
        "discovery": {},
    }
    pilot = evaluate_choice(early_facts, brief, T0, policy=policy)
    assert pilot["action"] == "prepare_or_pilot"  # 正例：快采合格 + 未过 TTL

    # 失败模式 A：快采末次观察越过 40min 门 → 快采证据不足。
    e_last_stale = evaluate_early(
        erev,
        epoints,
        fast_panel_bvids=list(emembers),
        as_of_s=T0,
        request_as_of_s=T0,
        fast_last_observation_s=T0 - policy.early_staleness_max_s - 1,
    )
    assert e_last_stale["status"] == "insufficient"
    assert "stale_evidence" in e_last_stale["reason_codes"]
    blocked_last = evaluate_choice(
        {**early_facts, "early": dict(e_last_stale)}, brief, T0, policy=policy
    )
    assert blocked_last["action"] == "watch_and_collect"
    assert blocked_last["action"] not in PRIORITY_ACTIONS

    # 失败模式 B：请求时刻越过 2h TTL → 快窗陈旧。
    ttl_request = T0 + policy.early_staleness_max_s + 1
    e_ttl = evaluate_early(
        erev,
        epoints,
        fast_panel_bvids=list(emembers),
        as_of_s=T0,
        request_as_of_s=ttl_request,
        fast_last_observation_s=T0,
    )
    assert e_ttl["status"] == "insufficient"
    assert e_ttl["ttl_active"] is False
    assert ttl_request - e_ttl["window_end_s"] > 2 * 3600
    blocked_ttl = evaluate_choice(
        {**early_facts, "early": dict(e_ttl)}, brief, ttl_request, policy=policy
    )
    assert blocked_ttl["action"] not in PRIORITY_ACTIONS
