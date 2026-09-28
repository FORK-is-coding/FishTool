"""抽奖服务重构前的核心行为护栏测试。"""

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pytest

from modules.lottery.service import LotteryService


class FakeAPI:
    """提供可预测用户资料的异步测试替身。"""

    def __init__(self, profiles: Dict[int, Dict[str, Any]]) -> None:
        """初始化测试资料映射。

        Args:
            profiles: UID 到 B 站用户资料 data 字段的映射。

        Returns:
            无。
        """
        self.profiles = profiles
        self.calls: List[int] = []

    async def get_user_info(self, uid: int) -> Dict[str, Any]:
        """返回指定 UID 的固定用户资料。

        Args:
            uid: 用户 ID。

        Returns:
            模拟 B 站接口响应。
        """
        self.calls.append(uid)
        return {"data": self.profiles[uid]}


def test_dynamic_cache_corruption_falls_back_to_empty(tmp_path: Path) -> None:
    """动态缓存损坏时应返回空列表，让调用方回退在线采集。"""
    service = LotteryService(api=object(), cache_dir=tmp_path)
    (tmp_path / "dynamic_123.json").write_text("{broken", encoding="utf-8")

    assert service._load_dynamic_comments("123") == []


def test_profile_cache_round_trip_is_stable(tmp_path: Path) -> None:
    """画像缓存写入后应完整读回，且不遗留临时文件。"""
    service = LotteryService(api=object(), cache_dir=tmp_path)
    profiles = {"42": {"level": 6, "is_vip": True, "vip_type": 2}}

    service._save_profile_cache(profiles)

    assert service._load_profile_cache() == profiles
    assert not (tmp_path / "user_profiles.tmp").exists()


def test_complete_draw_metadata_reuses_cache_and_preserves_values(tmp_path: Path) -> None:
    """补齐候选资料时应复用缓存，并且只填充原本缺失的字段。"""
    api = FakeAPI({})
    service = LotteryService(api=api, cache_dir=tmp_path)
    # 反例修复（规格 §6.5）：draw 资格走 v3 info 组，不再用扁平缓存命中。
    now_s = int(time.time())
    seq = service._cache.begin_attempt(42, namespace="draw", group="info", now_s=now_s)
    service._cache.merge_attempt(
        42, namespace="draw", group="info", attempt=seq,
        success_payload={
            "level": 6,
            "vip": {"vipStatus": 1, "vipType": 2},
            "is_vip": True,
            "vip_type": 2,
            "vip_label": "年度大会员",
        },
        now_s=now_s,
    )
    comments = [{
        "uid": 42,
        "ctime": "2026-08-10T12:00:00",
        "level": None,
        "is_vip": None,
        "vip_type": None,
        "vip_label": "",
        "content": "参与",
    }]

    result = asyncio.run(service._complete_draw_metadata(comments))

    assert api.calls == []
    assert result[0]["level"] == 6
    assert result[0]["vip_label"] == "年度大会员"
    assert comments[0]["level"] is None


def test_complete_draw_metadata_reports_unresolved_fields(tmp_path: Path) -> None:
    """在线资料仍无法补齐必要字段时应给出包含 UID 的明确错误。"""
    api = FakeAPI({42: {"level": 0, "vip": {}}})
    service = LotteryService(api=api, cache_dir=tmp_path)
    comments = [{
        "uid": 42,
        "ctime": None,
        "level": None,
        "is_vip": None,
        "vip_type": None,
        "vip_label": "",
    }]

    with pytest.raises(RuntimeError, match="UID: 42"):
        asyncio.run(service._complete_draw_metadata(comments))


def test_parse_comment_time_accepts_iso_and_rejects_invalid() -> None:
    """评论时间解析应兼容 Z 后缀，并对非法值稳定返回空。"""
    parsed = LotteryService._parse_comment_time("2026-08-10T12:00:00Z")

    assert parsed == datetime(2026, 8, 10, 12, 0)
    assert LotteryService._parse_comment_time("not-a-time") is None
    assert LotteryService._parse_comment_time(None) is None
