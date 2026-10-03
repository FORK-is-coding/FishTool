"""04 · 第三批 d · 事件聚合内核测试（通道 A/B、日级三窗、多事件重叠）。

覆盖 §16.2 / R5 规格表的 E 项：E04 / E40 / E05 / E45 / E06 / E07 / E08 /
E09 / E10 / E23 / E34 / E39 / E44 / E46。

口径与红线：
    - 只**调用** ``modules/hotspot/events/`` 与 ``algorithm/window_metrics.py``；
    - **不修改** ``modules/hotspot/algorithm/`` 任何文件；
    - 全部为纯计算断言：不触库、不触网、不看时钟（as_of 由用例显式给定）。

时间锚点：``T0`` 既是 UTC 零点、也是 ``86400`` 与 ``7200`` 的公共网格点，
所有 fixture 都围绕它构造，避免把窗口判成 stale。
"""
from __future__ import annotations

import pytest

from modules.hotspot.algorithm.window_metrics import window_end_s
from modules.hotspot.events.aggregator import PairedMeasure, matched_totals
from modules.hotspot.events.channel_a import channel_a_trend
from modules.hotspot.events.channel_b import DiscoveryRunView, discovery_signals, sampling_comparable
from modules.hotspot.events.config import DAY_W, EARLY_W, DailyPolicy
from modules.hotspot.events.daily import aggregate_daily_triplet
from modules.hotspot.events.early import evaluate_early
from modules.hotspot.events.overlap import cross_event_union, event_internal_measure
from modules.hotspot.events.windows import MemberRevision, SnapshotPoint

#: 2026-09-01T00:00:00Z —— 同时是 86400 与 7200 的整数倍（两条通道网格的公共不动点）。
T0 = 1788220800
#: 当日 12:00 执行时钟；对 daily 网格向下取整后仍落在 ``T0``。
AS_OF = T0 + 43200


# --------------------------------------------------------------------------- 构造工具


def _rev(bvid: str, owner_mid: int | None, decision_at_s: int) -> MemberRevision:
    """构造一条 ``accepted`` 成员版本（revision=1）。

    Args:
        bvid: 视频 BV 号。
        owner_mid: UP 主 mid；``None`` 表示作者不可核实。
        decision_at_s: 该版本提交时刻。

    Returns:
        MemberRevision: 只读成员版本。
    """
    return MemberRevision(
        bvid=bvid,
        owner_mid=owner_mid,
        status="accepted",
        revision=1,
        decision_at_s=decision_at_s,
        first_seen_s=decision_at_s,
    )


def _points(series: list[tuple[int, int]]) -> list[SnapshotPoint]:
    """把 ``(epoch_s, view)`` 序列转成有效快照点（``view_ok=True``）。"""
    return [SnapshotPoint(epoch_s=e, view=v, view_ok=True) for e, v in series]


def _two_window_series(w1: int, w2: int, *, base: int = 1000) -> list[tuple[int, int]]:
    """按两个日窗增量生成 3 个锚点（累计播放单调不减）。

    通道 A 只需 ``(T-2W, T-W]`` 与 ``(T-W, T]`` 两窗。

    Args:
        w1: 前窗增量。
        w2: 本窗增量。
        base: 最早锚点累计播放。

    Returns:
        list[tuple[int, int]]: 锚点序列。
    """
    return [
        (T0 - 2 * DAY_W, base),
        (T0 - DAY_W, base + w1),
        (T0, base + w1 + w2),
    ]


def _daily_series(w1: int, w2: int, w3: int, *, base: int = 1000) -> list[tuple[int, int]]:
    """按三个日窗增量生成 4 个锚点（累计播放单调不减）。

    Args:
        w1 / w2 / w3: 最早 / 中间 / 最近窗增量。
        base: 最早锚点累计播放。

    Returns:
        list[tuple[int, int]]: 锚点序列。
    """
    return [
        (T0 - 3 * DAY_W, base),
        (T0 - 2 * DAY_W, base + w1),
        (T0 - DAY_W, base + w1 + w2),
        (T0, base + w1 + w2 + w3),
    ]


def _members(count: int, *, prefix: str = "M") -> dict[str, int]:
    """生成 ``{bvid: owner_mid}`` 映射（作者互不相同，便于作者数/集中度判定）。"""
    return {f"{prefix}{i}": i + 1 for i in range(count)}


# --------------------------------------------------------------------------- 通道 A


def test_E04_new_member_million_only_enters_discovery() -> None:
    """E04：老成员前后均 [100,100,100] + 新 D 累计百万无前窗 → 不允许虚涨。"""
    members = ["BV1", "BV2", "BV3"]
    revisions = {b: [_rev(b, i + 1, T0 - 4 * DAY_W)] for i, b in enumerate(members)}
    # BV9 在知识截止 S = T-2W 之后才被接受 → 只能进发现通道，不进 U 分母。
    revisions["BV9"] = [_rev("BV9", 9, T0 - DAY_W)]

    points = {b: _points(_two_window_series(100, 100)) for b in members}
    points["BV9"] = _points([(T0, 1_000_000)])

    ca = channel_a_trend(revisions, points, as_of_s=AS_OF)

    assert ca["before_delta"] == 300
    assert ca["after_delta"] == 300
    assert ca["matched_growth"] == 0.0
    assert ca["paired_member_count"] == 3
    assert ca["eligible_member_count"] == 3
    assert "BV9" in ca["newly_discovered_bvids"]

    # 假增长反例（真伪可查）：朴素「今天全成员累计 − 昨天全成员累计」会把新入池
    # 百万播放旧视频算进来而虚涨；通道 A 必须完全剔除这部分。
    naive_growth = (3 * 1200 + 1_000_000) - (3 * 1000)
    assert ca["after_delta"] - ca["before_delta"] != naive_growth
    assert ca["after_delta"] < 1_000_000


def test_E40_new_member_excluded_from_denominator() -> None:
    """E40：E04 精确 fixture —— 新 D 只入 discovery，无百万增量。"""
    members = ["BV1", "BV2", "BV3"]
    revisions = {b: [_rev(b, i + 1, T0 - 4 * DAY_W)] for i, b in enumerate(members)}
    revisions["BV9"] = [_rev("BV9", 9, T0 - DAY_W)]

    points = {b: _points(_two_window_series(100, 100)) for b in members}
    points["BV9"] = _points([(T0, 1_000_000)])

    ca = channel_a_trend(revisions, points, as_of_s=AS_OF)

    assert ca["before_delta"] == 300
    assert ca["after_delta"] == 300
    assert ca["matched_growth"] == 0.0

    # 百万累计只出现在「被发现」，绝不进入配对增量。
    assert 1_000_000 not in (ca["before_delta"], ca["after_delta"])

    bv9 = [ex for ex in ca["exclusions"] if ex["bvid"] == "BV9"]
    assert bv9, "BV9 必须出现在排除清单里"
    assert bv9[0]["reason"] == "discovered_after_cutoff"
    assert bv9[0]["in_denominator"] is False


def test_E05_missing_member_dropped_from_both_windows() -> None:
    """E05：一成员本窗缺失 → 两窗同时剔该成员，质量计数降低，不假衰退。"""
    members = ["M1", "M2", "M3"]
    revisions = {b: [_rev(b, i + 1, T0 - 4 * DAY_W)] for i, b in enumerate(members)}
    points = {b: _points(_two_window_series(100, 100)) for b in members}
    # M3 本窗缺失（最后一个观测点停在 T-W）。
    points["M3"] = _points([(T0 - 2 * DAY_W, 1000), (T0 - DAY_W, 1100)])

    ca = channel_a_trend(revisions, points, as_of_s=AS_OF)

    assert ca["before_delta"] == 200
    assert ca["after_delta"] == 200
    assert ca["matched_growth"] == 0.0
    assert ca["paired_member_count"] == 2
    assert ca["eligible_member_count"] == 3
    assert ca["member_coverage"] == pytest.approx(2 / 3)
    # 若把缺失成员本窗按 0 计，before 会虚高为 300 而伪造衰退；这里必须是 200。
    assert ca["before_delta"] != 300


def test_E45_two_members_one_missing_no_fake_decline() -> None:
    """E45：两成员原 100+100，本窗只剩一人 100 → before=after=100、覆盖 1/2。"""
    members = ["V1", "V2"]
    revisions = {b: [_rev(b, i + 1, T0 - 4 * DAY_W)] for i, b in enumerate(members)}
    points = {b: _points(_two_window_series(100, 100)) for b in members}
    points["V2"] = _points([(T0 - 2 * DAY_W, 1000), (T0 - DAY_W, 1100)])

    ca = channel_a_trend(revisions, points, as_of_s=AS_OF)

    assert ca["before_delta"] == 100
    assert ca["after_delta"] == 100
    assert ca["matched_growth"] == 0.0
    assert ca["member_coverage"] == pytest.approx(0.5)
    assert ca["paired_member_count"] == 1
    assert ca["eligible_member_count"] == 2
    # 明确不是「旧总 200 -> 100」的衰退。
    assert not (ca["before_delta"] == 200 and ca["after_delta"] == 100)


# --------------------------------------------------------------------------- 日级三窗


def test_E06_daily_triplet_confirmed_growth_and_recomputable() -> None:
    """E06：A/B/C 同组 300→450→450 → 日级增强确认，policy 与 panel 可复算。"""
    members = _members(3)
    revisions = {b: [_rev(b, mid, T0 - 4 * DAY_W)] for b, mid in members.items()}
    points = {b: _points(_daily_series(100, 150, 150)) for b in members}

    res = aggregate_daily_triplet(revisions, points, as_of_s=AS_OF)

    assert (res["a_delta"], res["b_delta"], res["c_delta"]) == (300, 450, 450)
    assert res["available_windows"] == 3
    assert res["topic_phase"] == "rising"
    assert res["stage_reason"] == "confirmed_growth"
    assert res["confirmed_multi_author"] is True
    assert res["policy_version"] == DailyPolicy().policy_version

    # 可复算：同一输入 + 同一显式 policy 重跑，三窗数值与 canonical 指纹一致。
    again = aggregate_daily_triplet(revisions, points, as_of_s=AS_OF, policy=DailyPolicy())
    assert (again["a_delta"], again["b_delta"], again["c_delta"]) == (300, 450, 450)
    assert again["fingerprint"] == res["fingerprint"]


def test_E23_zero_denominator_keeps_real_zero() -> None:
    """E23：0 分母 / 全零增量 → relative_change=null，真实零保留。"""
    # 纯计算契约：前窗为 0 时不得用 epsilon 造爆炸百分比。
    totals = matched_totals([PairedMeasure("X", 1, 0.0, 0.0, True)])
    assert totals["before_delta"] == 0
    assert totals["after_delta"] == 0
    assert totals["relative_change"] is None
    assert totals["mean_difference"] == 0

    # 通道 A：A=0 → matched_growth=None（不除 epsilon）。
    members = _members(3, prefix="Z")
    revisions = {b: [_rev(b, mid, T0 - 4 * DAY_W)] for b, mid in members.items()}
    flat2 = {b: _points(_two_window_series(0, 0)) for b in members}
    ca = channel_a_trend(revisions, flat2, as_of_s=AS_OF)
    assert ca["before_delta"] == 0
    assert ca["after_delta"] == 0
    assert ca["matched_growth"] is None

    # 日级全零 → relative_change=None，但 a/b/c 保留真实 0（不是 None）。
    flat3 = {b: _points(_daily_series(0, 0, 0)) for b in members}
    res = aggregate_daily_triplet(revisions, flat3, as_of_s=AS_OF)
    assert (res["a_delta"], res["b_delta"], res["c_delta"]) == (0, 0, 0)
    assert res["relative_change"] is None


def test_E34_all_zero_multi_author_no_attention() -> None:
    """E34：A=B=C=0、全有效且多作者 → attention_present=false，不推荐热门制作。"""
    members = _members(3, prefix="A")
    revisions = {b: [_rev(b, mid, T0 - 4 * DAY_W)] for b, mid in members.items()}
    points = {b: _points(_daily_series(0, 0, 0, base=700)) for b in members}

    res = aggregate_daily_triplet(revisions, points, as_of_s=AS_OF)

    assert (res["a_delta"], res["b_delta"], res["c_delta"]) == (0, 0, 0)
    assert res["available_windows"] == 3
    # 全有效 + 多作者：结论不是「样本不足」，而是真实的无新增注意力。
    assert res["sample_gate_passed"] is True
    assert res["panel_author_count"] >= 2
    assert res["topic_phase"] == "stable"
    assert res["stage_reason"] == "stable_no_attention"
    assert res["attention_present"] is False


def test_E44_turning_signal_not_stable_nor_rising() -> None:
    """E44：n=3，A=0/B=90/C=0 → 转折 undetermined，不因相对分母 0 落 stable/rising。"""
    members = _members(3, prefix="N")
    revisions = {b: [_rev(b, mid, T0 - 4 * DAY_W)] for b, mid in members.items()}
    points = {b: _points(_daily_series(0, 30, 0)) for b in members}

    res = aggregate_daily_triplet(revisions, points, as_of_s=AS_OF)

    assert (res["a_delta"], res["b_delta"], res["c_delta"]) == (0, 90, 0)
    assert res["topic_phase"] == "undetermined"
    assert res["stage_reason"] == "turning_signal"
    assert res["topic_phase"] not in ("stable", "rising")
    assert res["relative_change"] is None

    # 「不复用 02 常量」真伪可查：同一 fixture 换一份显式 policy，结论必须随 policy 变，
    # 证明阈值来自 DailyPolicy（各算法各用各的），而非写死在某个 02 全局常量里。
    sentinel = DailyPolicy(absolute_per_video=1_000_000.0, relative_min=0.99)
    relaxed = aggregate_daily_triplet(revisions, points, as_of_s=AS_OF, policy=sentinel)
    assert (relaxed["a_delta"], relaxed["b_delta"], relaxed["c_delta"]) == (0, 90, 0)
    assert relaxed["topic_phase"] == "stable"
    assert relaxed["stage_reason"] == "stable_observed"


def test_E46_unknown_author_dominance_blocks_broad_diffusion() -> None:
    """E46：已知 2 作者但未知作者贡献 90% → author_coverage_insufficient，非广泛扩散。"""
    revisions = {
        "V1": [_rev("V1", 1, T0 - 4 * DAY_W)],
        "V2": [_rev("V2", 2, T0 - 4 * DAY_W)],
        "V3": [_rev("V3", None, T0 - 4 * DAY_W)],  # 作者不可核实
    }
    points = {
        "V1": _points(_daily_series(100, 150, 50)),
        "V2": _points(_daily_series(100, 150, 50)),
        "V3": _points(_daily_series(100, 150, 900)),
    }

    res = aggregate_daily_triplet(revisions, points, as_of_s=AS_OF)

    assert res["panel_author_count"] == 2
    assert res["unknown_author_delta_share"] == pytest.approx(0.9)
    assert res["author_coverage_insufficient"] is True
    assert "author_coverage_insufficient" in res["reason_codes"]
    assert res["broad_diffusion"] is False


# --------------------------------------------------------------------------- 通道 B


def test_E07_dual_channel_old_member_decline_and_new_authors() -> None:
    """E07：老成员下降、新作者发现增加 → 双通道并列，不塌成单一「话题衰退」。"""
    # 通道 A：老成员增量下降（累计播放仍单调不减，只是窗口增量变小）。
    members = _members(3, prefix="O")
    revisions = {b: [_rev(b, mid, T0 - 4 * DAY_W)] for b, mid in members.items()}
    points = {b: _points([(T0 - 2 * DAY_W, 1000), (T0 - DAY_W, 1200), (T0, 1300)]) for b in members}
    ca = channel_a_trend(revisions, points, as_of_s=AS_OF)

    assert ca["before_delta"] == 600
    assert ca["after_delta"] == 300
    assert ca["matched_growth"] == pytest.approx(-0.5)

    # 通道 B：同周期新作者发现增加（两次 run 采样可比）。
    prev = DiscoveryRunView(
        "r1", 0, 1, "planH", 1, 1, ("kw",), 600, True,
        newly_discovered_bvids=("D1",), newly_discovered_authors=(101,),
    )
    cur = DiscoveryRunView(
        "r2", 1, 2, "planH", 1, 1, ("kw",), 600, True,
        newly_discovered_bvids=("D1", "D2", "D3"), newly_discovered_authors=(101, 102, 103),
    )
    cb = discovery_signals(cur, prev_run=prev)

    assert cb["sampling_changed"] is False
    assert cb["discovery_delta"] == 2
    assert cb["newly_discovered_authors"] == 3

    # 两条通道并列可读：老成员减速与新作者增加同时成立。
    combined = {"matched_growth": ca["matched_growth"], "discovery_delta": cb["discovery_delta"]}
    assert combined["matched_growth"] < 0
    assert combined["discovery_delta"] > 0


def test_E08_page_expansion_flags_sampling_changed() -> None:
    """E08：查 1 页改查 5 页 → sampling_changed，不给发现增速。"""
    prev = DiscoveryRunView(
        "r1", 0, 1, "planH", 1, 1, ("kw",), 600, True,
        newly_discovered_bvids=tuple(f"B{i}" for i in range(20)),
    )
    cur = DiscoveryRunView(
        "r2", 1, 2, "planH", 1, 5, ("kw",), 600, True,
        newly_discovered_bvids=tuple(f"C{i}" for i in range(80)),
    )

    assert sampling_comparable(prev, cur) is False

    sig = discovery_signals(cur, prev_run=prev)
    assert sig["sampling_changed"] is True
    assert sig["discovery_delta"] is None
    assert "sampling_changed" in sig["reason_codes"]
    # 发现事实本身仍保留，只是不给「发现线索增加」的结论。
    assert sig["newly_discovered_videos"] == 80


def test_E10_concentrated_single_author_not_broad_diffusion() -> None:
    """E10：大作者贡献 90% 增量 → concentrated，非广泛扩散。"""
    members = {"V1": 1, "V2": 2, "V3": 3}
    revisions = {b: [_rev(b, mid, T0 - 4 * DAY_W)] for b, mid in members.items()}
    points = {
        "V1": _points(_daily_series(100, 150, 900)),
        "V2": _points(_daily_series(100, 150, 100)),
        "V3": _points(_daily_series(100, 150, 100)),
    }

    res = aggregate_daily_triplet(revisions, points, as_of_s=AS_OF)

    assert res["c_delta"] == 1100
    assert res["top_author_share"] == pytest.approx(900 / 1100)
    assert res["concentrated"] is True
    assert "concentrated" in res["reason_codes"]
    assert res["broad_diffusion"] is False

    # 通道 B 的 dominance 同口径：top1 作者占本次已观测增量的 90%。
    run = DiscoveryRunView(
        "r1", 0, 1, "planH", 1, 1, ("kw",), 600, True,
        observed_delta_by_author={1: 900.0, 2: 50.0, 3: 50.0},
    )
    assert discovery_signals(run)["dominance"] == pytest.approx(0.9)


# --------------------------------------------------------------------------- 多事件重叠


def test_E09_cross_event_union_counts_each_bvid_once() -> None:
    """E09：同视频属两事件 → 每事件全额；跨事件合计并集去重。"""
    ev1 = event_internal_measure([("BVX", 100.0), ("BVY", 50.0)])
    ev2 = event_internal_measure([("BVX", 100.0), ("BVZ", 70.0)])

    assert ev1["total_delta"] == 150.0
    assert ev2["total_delta"] == 170.0

    uni = cross_event_union({"e1": ev1["members"], "e2": ev2["members"]})
    # 事件内部：BVX 在各自事件里都拿全额。
    assert uni["per_event"]["e1"]["BVX"] == 100.0
    assert uni["per_event"]["e2"]["BVX"] == 100.0
    # 跨事件合计：BVX 只计一次。
    assert uni["union_member_count"] == 3
    assert uni["union_total_delta"] == 220.0
    assert uni["union_total_delta"] != 320.0  # 不许把重叠成员重复相加
    assert uni["overlap_notice"]

    # 事件内部重复 BVID 必须显式拒绝，避免静默双计。
    with pytest.raises(ValueError):
        event_internal_measure([("BVX", 100.0), ("BVX", 100.0)])


# --------------------------------------------------------------------------- 通道网格隔离


def test_E39_daily_and_fast_grids_do_not_cross_talk() -> None:
    """E39：02 日窗与 04 快窗各用自己的 UTC 网格/W；快采插点不改日级数值。"""
    # 刻意挑一个不落在日窗边界上的执行时钟。
    as_of = T0 + 50000
    day_end = window_end_s(as_of, DAY_W)
    fast_end = window_end_s(as_of, EARLY_W)
    assert day_end == T0
    assert fast_end == T0 + 6 * EARLY_W
    assert day_end != fast_end  # 两条通道各锚各的网格

    members = _members(3, prefix="L")
    revisions = {b: [_rev(b, mid, T0 - 4 * DAY_W)] for b, mid in members.items()}

    # 同一条线性轨迹：每 1200 秒多 4 播放（步长必须 <= early 的 40 分钟门，
    # 否则快采点自身会被判成断段，early 永远进不了 complete）。
    def _line(epoch_s: int) -> int:
        return 1000 + ((epoch_s - (T0 - 3 * DAY_W)) // 1200) * 4

    # 稀疏：只放日窗锚点。稠密：再插入 20 分钟粒度的快采点（同一条直线上）。
    sparse = {
        b: _points([(e, _line(e)) for e in (T0 - 3 * DAY_W, T0 - 2 * DAY_W, T0 - DAY_W, T0)])
        for b in members
    }
    dense = {
        b: _points([(e, _line(e)) for e in range(T0 - 3 * DAY_W, T0 + 43200 + 1, 1200)])
        for b in members
    }

    daily_sparse = aggregate_daily_triplet(revisions, sparse, as_of_s=as_of)
    daily_dense = aggregate_daily_triplet(revisions, dense, as_of_s=as_of)

    # 快采插点不得改变日级三窗数值与指纹。
    assert (
        daily_sparse["a_delta"],
        daily_sparse["b_delta"],
        daily_sparse["c_delta"],
    ) == (daily_dense["a_delta"], daily_dense["b_delta"], daily_dense["c_delta"])
    assert daily_sparse["fingerprint"] == daily_dense["fingerprint"]
    assert daily_sparse["window_end_s"] == day_end

    # early 走自己的 7200 网格，右边界与日窗不同。
    early = evaluate_early(
        revisions,
        dense,
        fast_panel_bvids=list(members),
        as_of_s=as_of,
        request_as_of_s=fast_end,
        fast_last_observation_s=fast_end,
    )
    assert early["window_end_s"] == fast_end
    assert early["window_end_s"] != daily_dense["window_end_s"]
    assert early["status"] == "complete"
