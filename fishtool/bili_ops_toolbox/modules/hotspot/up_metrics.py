"""
UP 主轻量指标批量查询（卡片页专用）。

热点生命周期卡片需要按 UP 展示“每千粉舰长率 / 每千粉充电率”，
但完整账号关联（get_up_relation）太重（约 5-6 个接口/UP），
20 张卡片全跑会打穿单 cookie 请求预算，所以这里只取三个字段：
- follower：relation/stat
- guard_count + live_status：getRoomInfoOld（按 UID 查询）-> guardTab/topList
- charge_count + charge_source：get_charge_count（自带 charge 预算桶）

约定：
- TTL 缓存 30 分钟，减少重复请求；
- 并发上限 3，配合 api 层限频器，避免 429；
- 任一字段失败降级 None / 0，不阻塞卡片渲染。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from bilibili.api import BilibiliAPI
from core.logger import get_logger

logger = get_logger(__name__)

_TTL_SECONDS = 30 * 60
_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_semaphore = asyncio.Semaphore(3)


def _fmt_int(value: Any, default: int | None = 0) -> int | None:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


async def _load_one(api: BilibiliAPI, mid: int, room_map: dict) -> dict[str, Any]:
    result: dict[str, Any] = {"mid": int(mid)}
    try:
        relation = await api.get_user_relation_stat(mid)
        rel = (relation.get("data") or {}) if isinstance(relation, dict) else {}
        result["follower"] = _fmt_int(rel.get("follower"))
    except Exception as exc:
        logger.warning("UP 轻量指标 relation 失败 mid=%s: %s", mid, exc)
        result["follower"] = None

    room = room_map.get(str(mid)) or {}
    room_id = _fmt_int(room.get("room_id"))
    result["live_status"] = _fmt_int(room.get("live_status"))
    if room_id:
        try:
            guard = await api.get_guard_top_list(room_id, int(mid))
            guard_data = guard.get("data") or {}
            result["guard_count"] = _fmt_int(((guard_data.get("info") or {}).get("num")))
        except Exception as exc:
            logger.warning("UP 轻量指标 guard 失败 mid=%s: %s", mid, exc)
            result["guard_count"] = None
    else:
        # 未开播 / 无直播间：不存在大航海，明确置 None 供前端显示“暂无直播数据”。
        result["guard_count"] = None

    try:
        charge_data = await api.get_charge_count(int(mid))
        result["charge_count"] = _fmt_int(charge_data.get("charge_count"), default=0)
        result["charge_source"] = charge_data.get("source", "unavailable")
    except Exception as exc:
        logger.warning("UP 轻量指标 charge 失败 mid=%s: %s", mid, exc)
        result["charge_count"] = None
        result["charge_source"] = "unavailable"
    return result


async def get_up_light_metrics(api: BilibiliAPI, mids: list[int]) -> dict[int, dict[str, Any]]:
    """批量获取 UP 轻量指标，返回 {mid: {...}}，带 TTL 缓存与并发控制。"""
    mids = sorted({int(m) for m in mids if m})
    if not mids:
        return {}

    now = time.time()
    result: dict[int, dict[str, Any]] = {}
    need: list[int] = []
    for mid in mids:
        cached = _cache.get(mid)
        if cached and now - cached[0] < _TTL_SECONDS:
            result[mid] = cached[1]
        else:
            need.append(mid)

    if not need:
        return result

    # 直播间信息支持一次批量查询（上限 100），单独一个大请求搞定。
    room_map: dict = {}
    try:
        room_info = await api.get_room_base_info(need)
        room_map = (room_info.get("data") or {}) if isinstance(room_info, dict) else {}
    except Exception as exc:
        logger.warning("UP 轻量指标 room_base_info 批量失败: %s", exc)

    async def _guarded(mid: int) -> tuple[int, dict[str, Any]]:
        async with _semaphore:
            return mid, await _load_one(api, mid, room_map)

    loaded = await asyncio.gather(*(_guarded(mid) for mid in need), return_exceptions=True)
    for item in loaded:
        if isinstance(item, Exception):
            logger.warning("UP 轻量指标加载异常: %s", item)
            continue
        mid, data = item
        _cache[mid] = (time.time(), data)
        result[mid] = data
    return result
