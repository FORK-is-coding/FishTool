"""数据质量公共契约测试（FishTool 03 · 批 1）。

覆盖 ``core/data_quality.py`` 的严格数值解析、明确时间换算，以及
``bilibili/api/user.py`` wrapper 新增的 ``_meta`` 来源与补默认值前字段状态。

设计原则（对齐 03 规格 §3）：
- 缺失一律返回 ``None`` + ``missing``，绝不用 0 抢占；
- 真实 0 状态为 ``ok``；
- naive 时间在没有显式 ``legacy_timezone`` 时不可换算；
- 夏令时歧义/不存在时刻必须返回不可用。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, Optional

import pytest

from bilibili.api.user import UserAPIMixin
from core.data_quality import (
    epoch_to_utc_dt,
    parse_count,
    parse_ratio,
    to_epoch_s,
    utc_now_epoch_s,
)
from core.exceptions import BilibiliAPIError


# --------------------------------------------------------------------------
# parse_count 契约
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, (None, "missing")),   # 缺失
        (True, (None, "invalid")),   # bool 不得冒充 1
        (False, (None, "invalid")),  # bool 不得冒充 0
        (-1, (None, "invalid")),     # 负数非法
        (0, (0, "ok")),              # 真实 0 有效
        (123, (123, "ok")),
        (" 123 ", (123, "ok")),      # 首尾空白可容忍
        ("0", (0, "ok")),
        ("", (None, "invalid")),     # 空串非法
        ("12a", (None, "invalid")),
        ("1.5", (None, "invalid")),
        (1.5, (None, "invalid")),    # float 非法
    ],
)
def test_parse_count_contract(raw: Any, expected: tuple) -> None:
    """parse_count 对合法/非法/缺失输入的判定必须稳定。"""
    assert parse_count(raw) == expected


# --------------------------------------------------------------------------
# parse_ratio 契约
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, (None, "missing")),
        (True, (None, "invalid")),   # bool 非法
        (0, (0.0, "ok")),
        (1, (1.0, "ok")),
        (0.5, (0.5, "ok")),
        ("0.25", (0.25, "ok")),
        (1.5, (None, "invalid")),    # 越界
        (-0.1, (None, "invalid")),   # 越界
        (float("nan"), (None, "invalid")),
        (float("inf"), (None, "invalid")),
        ("abc", (None, "invalid")),
    ],
)
def test_parse_ratio_contract(raw: Any, expected: tuple) -> None:
    """parse_ratio 仅接受 [0, 1] 内的有限数值。"""
    assert parse_ratio(raw) == expected


# --------------------------------------------------------------------------
# 时间换算契约
# --------------------------------------------------------------------------


def _zone(name: str) -> Optional[tzinfo]:
    """尝试载入 IANA 时区；运行环境缺 zoneinfo 数据时返回 None。"""
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return None
    except ImportError:  # pragma: no cover - 老解释器
        return None


def test_utc_now_epoch_s_returns_int() -> None:
    """当前 UTC 秒级时间戳必须是 int。"""
    assert isinstance(utc_now_epoch_s(), int)


def test_to_epoch_s_none_and_non_datetime() -> None:
    """None 与非 datetime 输入一律返回 None。"""
    assert to_epoch_s(None) is None
    assert to_epoch_s(123) is None
    assert to_epoch_s("2026-01-01") is None


def test_to_epoch_s_naive_without_timezone_is_none() -> None:
    """naive 时间没有显式时区来源时不可换算。"""
    assert to_epoch_s(datetime(2026, 1, 15, 12, 0, 0)) is None


def test_to_epoch_s_aware_datetime() -> None:
    """aware 时间应换算为等价 UTC 时间戳。"""
    aware = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    expected = int(datetime(2026, 1, 15, 4, 0, 0, tzinfo=timezone.utc).timestamp())
    assert to_epoch_s(aware) == expected


def test_to_epoch_s_unique_naive_maps_to_utc() -> None:
    """无歧义的 naive 时刻在明确时区下唯一映射（纽约 1/15 12:00 = UTC 17:00）。"""
    zone = _zone("America/New_York")
    if zone is None:
        pytest.skip("运行环境缺少 America/New_York 的 zoneinfo 数据")

    result = to_epoch_s(datetime(2026, 1, 15, 12, 0, 0), legacy_timezone="America/New_York")
    expected = int(datetime(2026, 1, 15, 17, 0, 0, tzinfo=timezone.utc).timestamp())
    assert result == expected


def test_to_epoch_s_dst_ambiguous_returns_none() -> None:
    """秋季回拨的歧义时刻（11/01 01:30）候选不唯一，必须不可用。"""
    if _zone("America/New_York") is None:
        pytest.skip("运行环境缺少 America/New_York 的 zoneinfo 数据")

    assert to_epoch_s(datetime(2026, 11, 1, 1, 30, 0), legacy_timezone="America/New_York") is None


def test_to_epoch_s_dst_nonexistent_returns_none() -> None:
    """春季跳时的不存在时刻（03/08 02:30）无法往返，必须不可用。"""
    if _zone("America/New_York") is None:
        pytest.skip("运行环境缺少 America/New_York 的 zoneinfo 数据")

    assert to_epoch_s(datetime(2026, 3, 8, 2, 30, 0), legacy_timezone="America/New_York") is None


def test_epoch_to_utc_dt_contract() -> None:
    """时间戳还原契约：None 透传，非 int（含 bool）抛 invalid_epoch。"""
    assert epoch_to_utc_dt(None) is None

    value = int(datetime(2026, 1, 15, 17, 0, 0, tzinfo=timezone.utc).timestamp())
    restored = epoch_to_utc_dt(value)
    assert restored == datetime(2026, 1, 15, 17, 0, 0, tzinfo=timezone.utc)

    for bad in (True, 1.0, "123"):
        with pytest.raises(ValueError):
            epoch_to_utc_dt(bad)


# --------------------------------------------------------------------------
# bilibili/api/user.py wrapper._meta 契约（验收矩阵第 13 条）
# --------------------------------------------------------------------------


class _StubAPI(UserAPIMixin):
    """可注入路由响应的 UserAPIMixin 替身，用于隔离网络。"""

    BASE_URL = "https://api.bilibili.com"

    def __init__(self, routes: Dict[str, Any]) -> None:
        """记录子串 -> 响应（或异常）的路由表。

        Args:
            routes: key 为 URL 子串，value 为响应字典或要抛出的异常实例。
        """
        self._routes = routes
        self.calls: list = []

    async def get(self, url: str, params: Optional[dict] = None, need_sign: bool = False) -> Any:
        """按路由子串返回预置响应或抛异常。"""
        self.calls.append(url)
        for key, value in self._routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"未预置的 URL: {url}")


def _meta_first_read(payload: Dict[str, Any], field: str) -> Any:
    """模拟 01/03 新消费者：meta 优先，status 非 ok 时返回 None 而非兼容 0。"""
    status = (payload.get("_meta") or {}).get("field_status", {}).get(field)
    if status != "ok":
        return None
    return (payload.get("data") or {}).get(field)


def test_user_info_fallback_missing_level_not_pierced_by_zero() -> None:
    """公开名片 fallback 原始缺 level：兼容 data 为 0，但 meta 标 missing 且读端仍 None。"""
    api = _StubAPI(
        {
            "/x/space/wbi/acc/info": BilibiliAPIError("space blocked"),
            "/x/web-interface/card": {"card": {"mid": 42, "name": "测试UP"}, "follower": 100},
        }
    )
    result = asyncio.run(api.get_user_info(42))

    # 兼容层仍给出 0，保证旧消费者不崩。
    assert result["data"]["level"] == 0
    assert result["data"]["name"] == "测试UP"
    # _meta 依据补默认值前的原始字段判定。
    assert result["_meta"]["source"] == "public_card"
    assert result["_meta"]["field_status"]["level"] == "missing"
    assert result["_meta"]["field_status"]["follower"] == "ok"
    assert result["_meta"]["field_status"]["following"] == "missing"
    # 新消费者读 meta 后，缺失的 level 仍是 None，不被兼容 0 穿透。
    assert _meta_first_read(result, "level") is None
    assert _meta_first_read(result, "follower") == 100


def test_user_info_fallback_real_zero_level_is_ok() -> None:
    """公开名片明确返回 level=0 属真实值，状态应为 ok 而非 missing。"""
    api = _StubAPI(
        {
            "/x/space/wbi/acc/info": BilibiliAPIError("space blocked"),
            "/x/web-interface/card": {
                "card": {"mid": 7, "name": "新人UP", "level_info": {"current_level": 0}},
            },
        }
    )
    result = asyncio.run(api.get_user_info(7))

    assert result["_meta"]["source"] == "public_card"
    assert result["_meta"]["field_status"]["level"] == "ok"
    assert _meta_first_read(result, "level") == 0


def test_user_info_primary_source_meta() -> None:
    """主接口成功时 meta 来源为 space_info，并按原始字段给出状态。"""
    api = _StubAPI(
        {
            "/x/space/wbi/acc/info": {"mid": 9, "name": "主接口UP", "level": 6, "follower": 0},
        }
    )
    result = asyncio.run(api.get_user_info(9))

    assert result["data"]["level"] == 6
    assert result["_meta"]["source"] == "space_info"
    assert result["_meta"]["field_status"]["level"] == "ok"
    assert result["_meta"]["field_status"]["follower"] == "ok"   # 真实 0
    assert result["_meta"]["field_status"]["following"] == "missing"
