"""06 采集广度 · 契约层单测（去重口径 / 质量解析 / 三套分类字段）。"""
from __future__ import annotations

from modules.hotspot.discovery import contracts
from modules.hotspot.discovery.contracts import (
    BroadVideo,
    DISCOVERY_SOURCE_PRIORITY,
    SOURCE_POPULAR,
    SOURCE_RANKING_ALL,
    SOURCE_RANKING_ALL_OTHERS,
    merge_video_candidates,
    parse_heat_score,
    parse_optional_int,
    source_priority,
)


# --------------------------------------------------------------------------
# heat_score 质量口径
# --------------------------------------------------------------------------

def test_heat_score_accepts_non_negative_int_and_digit_string() -> None:
    """非负整数与纯数字字符串均为 ok；真实 0 合法（不因样本全正就拒绝 0）。"""
    assert parse_heat_score(0) == (0, "ok")
    assert parse_heat_score(6915339) == (6915339, "ok")
    assert parse_heat_score("12345") == (12345, "ok")


def test_heat_score_missing_keeps_candidate() -> None:
    """缺失记 missing（保留候选、score 写 NULL）。"""
    assert parse_heat_score(None) == (None, "missing")


def test_heat_score_rejects_invalid_never_zero() -> None:
    """bool / 负数 / 非数值 / 浮点一律 invalid，绝不折算成 0。"""
    for raw in (True, False, -5, "abc", 3.5, "", []):
        value, status = parse_heat_score(raw)
        assert value is None
        assert status == "invalid"


def test_parse_optional_int_missing_vs_zero() -> None:
    """可选整数：缺失 missing、真值 0 为 ok，二者不混淆。"""
    assert parse_optional_int(None) == (None, "missing")
    assert parse_optional_int(0) == (0, "ok")
    assert parse_optional_int(True) == (None, "invalid")
    assert parse_optional_int(-1) == (None, "invalid")


def test_source_priority_is_fixed_and_ordered() -> None:
    """来源优先级预先固定：ranking_all > popular > ranking_all_others。"""
    assert DISCOVERY_SOURCE_PRIORITY == (SOURCE_RANKING_ALL, SOURCE_POPULAR, SOURCE_RANKING_ALL_OTHERS)
    assert source_priority(SOURCE_RANKING_ALL) < source_priority(SOURCE_POPULAR) < source_priority(SOURCE_RANKING_ALL_OTHERS)
    assert source_priority("unknown") == len(DISCOVERY_SOURCE_PRIORITY)


# --------------------------------------------------------------------------
# 合并：按 bvid 去重，来源全留，展示值按优先级（绝不按 max 播放量）
# --------------------------------------------------------------------------

def test_merge_keeps_all_sources_and_never_picks_max_view() -> None:
    """同一 bvid 从 popular 与 ranking 都发现 -> 两条来源全留；展示值取高优先级，不取大。"""
    popular_row = BroadVideo(
        bvid="BV1", source=SOURCE_POPULAR, captured_epoch_s=100,
        view=100000, view_status="ok", legacy_tid=4, legacy_tid_status="ok",
    )
    ranking_row = BroadVideo(
        bvid="BV1", source=SOURCE_RANKING_ALL, captured_epoch_s=200,
        view=1000, view_status="ok", legacy_tid=4, legacy_tid_status="ok",
    )
    merged = merge_video_candidates([popular_row, ranking_row])
    assert len(merged) == 1
    row = merged[0]
    assert row["sources"] == [SOURCE_RANKING_ALL, SOURCE_POPULAR]
    assert row["display_source"] == SOURCE_RANKING_ALL
    # 关键：不是 max(100000, 1000)；而是固定优先级来源的展示值。
    assert row["view"] == 1000
    assert row["captured_epoch_s"] == 200
    assert row["conflict"] is True
    # 原始统计保留来源 + 观测时间。
    assert [detail["captured_epoch_s"] for detail in row["source_details"]] == [200, 100]


def test_merge_same_values_marks_no_conflict() -> None:
    """多来源展示字段一致 -> 不标冲突。"""
    rows = [
        BroadVideo(bvid="BV2", source=SOURCE_POPULAR, captured_epoch_s=100, view=10, view_status="ok"),
        BroadVideo(bvid="BV2", source=SOURCE_RANKING_ALL, captured_epoch_s=100, view=10, view_status="ok"),
    ]
    merged = merge_video_candidates(rows)
    assert merged[0]["conflict"] is False


def test_merge_never_backfills_unknown_from_other_source() -> None:
    """高优先级来源缺字段时保持 None，不从低优先级来源补齐成已验证值。"""
    popular_row = BroadVideo(
        bvid="BV3", source=SOURCE_POPULAR, captured_epoch_s=100,
        view=None, view_status="missing", owner_mid=1, owner_status="ok",
    )
    ranking_row = BroadVideo(
        bvid="BV3", source=SOURCE_RANKING_ALL, captured_epoch_s=100,
        view=555, view_status="ok", owner_mid=None, owner_status="missing",
    )
    row = merge_video_candidates([popular_row, ranking_row])[0]
    assert row["display_source"] == SOURCE_RANKING_ALL
    assert row["view"] == 555
    assert row["owner_mid"] is None
    assert row["owner_status"] == "missing"


def test_merge_keeps_three_taxonomies_separate() -> None:
    """legacy tid / tidv2 / pid_v2 三套分类字段并存，不合并成统一 tid。"""
    row = BroadVideo(
        bvid="BV4", source=SOURCE_POPULAR, captured_epoch_s=100,
        legacy_tid=4, legacy_tid_status="ok",
        tidv2=1004, tidv2_status="ok",
        pid_v2=1000, pid_v2_status="ok",
    )
    merged = merge_video_candidates([row])[0]
    assert merged["legacy_tid"] == 4
    assert merged["tidv2"] == 1004
    assert merged["pid_v2"] == 1000


def test_merge_orders_by_bvid_and_skips_non_dto() -> None:
    """输出按 bvid 稳定排序，非 DTO 噪声条目被忽略。"""
    rows = [
        BroadVideo(bvid="BV9", source=SOURCE_POPULAR, captured_epoch_s=1),
        BroadVideo(bvid="BV1", source=SOURCE_POPULAR, captured_epoch_s=1),
        {"bvid": "BVX"},
        None,
    ]
    merged = merge_video_candidates(rows)
    assert [item["bvid"] for item in merged] == ["BV1", "BV9"]


def test_contracts_exposes_heat_score_label() -> None:
    """heat_score 只叫「平台接口返回热搜分数」。"""
    assert contracts.HEAT_SCORE_LABEL == "platform_search_square_score"
