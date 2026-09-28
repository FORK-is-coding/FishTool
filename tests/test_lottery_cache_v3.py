"""抽奖画像缓存 v3 契约测试（FishTool 03 · 批 4 · 规格 §6.2 / §6.3 / §6.4）。

覆盖：
- 旧扁平缓存 -> 全量 legacy_unverified（含非 None 的 0）
- begin_attempt / merge_attempt 的 seq 定序与命中判定
- TTL 过期 / 缺 group -> stale
- 最新失败不使用旧成功；迟到旧 attempt 不覆盖新结果（同秒也成立）
- full / draw 空间互不覆盖；未来/pending 时间不命中
- namespace=None 的旧扁平兼容读写仍可用
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules.lottery.cache import LotteryCache, group_is_fresh


@pytest.fixture()
def cache(tmp_path: Path) -> LotteryCache:
    """指向 tmp 目录的缓存仓储。"""
    return LotteryCache(tmp_path)


def test_legacy_flat_file_is_migrated_as_unverified(cache: LotteryCache) -> None:
    """旧裸 UID 字典整份标 legacy_unverified，旧非 None 的 0 也不可信。"""
    cache.profile_path().write_text(
        json.dumps({"42": {"level": 0, "is_vip": False}}), encoding="utf-8"
    )
    lookup = cache.get_profile(42, namespace="full", now_s=1000, required_groups=["info"])
    assert lookup.cache_state == "legacy"
    assert "legacy_unverified" in lookup.reason_codes
    assert lookup.profile is None


def test_begin_merge_success_hits_when_fresh(cache: LotteryCache) -> None:
    """成功 attempt 在 TTL 内可命中，profile 合并 group values。"""
    seq = cache.begin_attempt(7, namespace="full", group="info", now_s=100)
    assert seq == 1
    cache.merge_attempt(
        7, namespace="full", group="info", attempt=seq,
        success_payload={"level": 5, "recent_activity_count": 3,
                         "lottery_repost_ratio": 0.1, "video_count": 9},
        now_s=101,
    )
    lookup = cache.get_profile(7, namespace="full", now_s=102, required_groups=["info"])
    assert lookup.cache_state == "hit"
    assert lookup.profile["level"] == 5


def test_missing_group_is_stale(cache: LotteryCache) -> None:
    """缺某个必需 group 时判 stale 并列出需刷新 group。"""
    seq = cache.begin_attempt(7, namespace="full", group="info", now_s=100)
    cache.merge_attempt(7, namespace="full", group="info", attempt=seq,
                        success_payload={"level": 5}, now_s=101)
    lookup = cache.get_profile(7, namespace="full", now_s=102, required_groups=["info", "videos"])
    assert lookup.cache_state == "stale"
    assert "videos" in lookup.refresh_groups


def test_ttl_expiry_is_stale(cache: LotteryCache) -> None:
    """draw 资格 1h：边界内命中，超出变 stale。"""
    seq = cache.begin_attempt(7, namespace="full", group="draw", now_s=100)
    cache.merge_attempt(7, namespace="full", group="draw", attempt=seq,
                        success_payload={"level": 5}, now_s=100)
    assert cache.get_profile(7, namespace="full", now_s=100 + 3600, required_groups=["draw"]).cache_state == "hit"
    assert cache.get_profile(7, namespace="full", now_s=100 + 3601, required_groups=["draw"]).cache_state == "stale"


def test_latest_failure_does_not_use_old_success(cache: LotteryCache) -> None:
    """最新尝试失败时不把历史成功当本次新鲜证据，也不刷新成功时间戳。"""
    first = cache.begin_attempt(7, namespace="full", group="info", now_s=100)
    cache.merge_attempt(7, namespace="full", group="info", attempt=first,
                        success_payload={"level": 5}, now_s=101)
    second = cache.begin_attempt(7, namespace="full", group="info", now_s=102)
    cache.merge_attempt(7, namespace="full", group="info", attempt=second,
                        success_payload=None, now_s=103)
    lookup = cache.get_profile(7, namespace="full", now_s=104, required_groups=["info"])
    assert lookup.cache_state == "stale"


def test_late_old_attempt_does_not_override_newer(cache: LotteryCache) -> None:
    """A 先发起、B 后发起先成功、A 迟到：A 的旧 seq 不覆盖 B（同秒也成立）。"""
    a = cache.begin_attempt(7, namespace="full", group="info", now_s=100)
    b = cache.begin_attempt(7, namespace="full", group="info", now_s=101)
    cache.merge_attempt(7, namespace="full", group="info", attempt=b,
                        success_payload={"level": 9}, now_s=102)
    cache.merge_attempt(7, namespace="full", group="info", attempt=a,
                        success_payload={"level": 1}, now_s=102)
    lookup = cache.get_profile(7, namespace="full", now_s=103, required_groups=["info"])
    assert lookup.cache_state == "hit"
    assert lookup.profile["level"] == 9


def test_namespaces_do_not_overwrite_each_other(cache: LotteryCache) -> None:
    """full 与 draw 分空间，互不覆盖。"""
    cache.merge_attempt(
        7, namespace="full", group="info",
        attempt=cache.begin_attempt(7, namespace="full", group="info", now_s=100),
        success_payload={"level": 5}, now_s=101,
    )
    cache.merge_attempt(
        7, namespace="draw", group="draw",
        attempt=cache.begin_attempt(7, namespace="draw", group="draw", now_s=100),
        success_payload={"level": 5}, now_s=101,
    )
    assert cache.get_profile(7, namespace="full", now_s=102, required_groups=["info"]).cache_state == "hit"
    assert cache.get_profile(7, namespace="draw", now_s=102, required_groups=["draw"]).cache_state == "hit"


def test_group_is_fresh_rejects_future_and_pending() -> None:
    """未来时间戳与 pending attempt 都不算新鲜。"""
    assert group_is_fresh(
        {"latest_started_seq": 1, "last_success": {"fetched_s": 200},
         "last_attempt": {"seq": 1, "at_s": 200, "status": "ok"}},
        100, 3600,
    ) is False
    assert group_is_fresh(
        {"latest_started_seq": 1, "last_success": {"fetched_s": 50},
         "last_attempt": {"seq": 1, "at_s": None, "status": "pending"}},
        100, 3600,
    ) is False


def test_flat_compat_round_trip_still_supported(cache: LotteryCache) -> None:
    """namespace=None 保持旧扁平兼容读写（不回归旧调用）。"""
    cache.save_profiles({"42": {"level": 6}})
    assert cache.load_profiles() == {"42": {"level": 6}}
