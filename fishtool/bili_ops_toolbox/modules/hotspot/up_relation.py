"""
UP 主关联数据服务。

用于热点详情"查看上涨账号"抽屉：
- 粉丝数 / 关注数：get_user_relation_stat
- 累计投稿播放：get_user_upstat
- 90 日发布视频的平均播放与增长：get_user_videos 按 pubdate 过滤

边界约定：
- 本服务只做只读查询，不写库；
- 任一接口失败时降级返回部分字段，不阻塞抽屉渲染。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bilibili.api import BilibiliAPI
from core.logger import get_logger

logger = get_logger(__name__)


async def get_up_relation(api: BilibiliAPI, mid: int) -> dict[str, Any]:
    """汇总 UP 主关联数据，供前端"查看上涨账号"抽屉展示。

    Args:
        api: 已初始化的 BilibiliAPI 客户端。
        mid: UP 主 UID。

    Returns:
        字典包含：
        - mid / name：账号标识
        - follower / following：粉丝数 / 关注数
        - archive_view：累计投稿播放
        - recent_avg_view：近 90 日发布视频平均播放
        - recent_count：近 90 日发布视频数
        - growth_ratio：近 90 日平均播放 / 全部视频平均播放，衡量近期涨势
        - guard_count：直播间舰长总数（无直播/无大航海时为 None）
        - live_status：直播间开播状态（0=未开播，1=直播中）
    """
    result: dict[str, Any] = {"mid": int(mid)}
    try:
        info = await api.get_user_info(mid)
        data = info.get("data") or {}
        result["name"] = data.get("name") or ""
    except Exception as exc:
        logger.warning("UP 基础信息获取失败 mid=%s: %s", mid, exc)
        result["name"] = ""

    try:
        relation = await api.get_user_relation_stat(mid)
        rel_data = relation.get("data") or {}
        result["follower"] = int(rel_data.get("follower") or 0)
        result["following"] = int(rel_data.get("following") or 0)
    except Exception as exc:
        logger.warning("UP 关系统计获取失败 mid=%s: %s", mid, exc)
        result["follower"] = None
        result["following"] = None

    try:
        upstat = await api.get_user_upstat(mid)
        archive = (upstat.get("data") or {}).get("archive") or {}
        result["archive_view"] = int(archive.get("view") or 0)
    except Exception as exc:
        logger.warning("UP 累计播放获取失败 mid=%s: %s", mid, exc)
        result["archive_view"] = None

    # 90 日增长率：拿第一页投稿列表，按 pubdate 过滤近 90 天作品求平均播放。
    try:
        videos = await api.get_user_videos(mid, page=1, page_size=30)
        vlist = ((videos.get("data") or {}).get("list") or {}).get("vlist") or []
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=90)).timestamp()
        recent = [v for v in vlist if int(v.get("created") or 0) >= cutoff]
        recent_views = [int(v.get("play") or 0) for v in recent]
        all_views = [int(v.get("play") or 0) for v in vlist]
        result["recent_count"] = len(recent)
        result["recent_avg_view"] = round(sum(recent_views) / len(recent_views)) if recent_views else None
        result["all_avg_view"] = round(sum(all_views) / len(all_views)) if all_views else None
        if result.get("all_avg_view"):
            result["growth_ratio"] = round(
                (result["recent_avg_view"] or 0) / result["all_avg_view"], 2
            )
        else:
            result["growth_ratio"] = None
    except Exception as exc:
        logger.warning("UP 近期视频获取失败 mid=%s: %s", mid, exc)
        result["recent_count"] = None
        result["recent_avg_view"] = None
        result["growth_ratio"] = None

    # 舰长转化：先按 UID 查直播间 room_id，再查大航海列表总数。
    # 未开播 / 无大航海 / 接口失败均降级为 None，不影响抽屉其他字段渲染。
    try:
        room_info = await api.get_room_base_info([mid])
        room_map = (room_info.get("data") or {}).get(str(mid)) or {}
        room_id = room_map.get("room_id") or 0
        if room_id:
            guard = await api.get_guard_top_list(int(room_id), int(mid))
            guard_data = guard.get("data") or {}
            result["guard_count"] = int((guard_data.get("info") or {}).get("num") or 0)
            result["live_status"] = int(room_map.get("live_status") or 0)
        else:
            result["guard_count"] = None
            result["live_status"] = 0
    except Exception as exc:
        logger.warning("UP 舰长数据获取失败 mid=%s: %s", mid, exc)
        result["guard_count"] = None
        result["live_status"] = None

    return result
