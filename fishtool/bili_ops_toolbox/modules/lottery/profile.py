"""抽奖真人判定所需的最小公开用户画像采集。"""

import time
from collections.abc import Awaitable
from typing import Any

from bilibili.api import BilibiliAPI


class ProfileCollector:
    """串行采集公开资料，并隔离单个接口失败。"""

    def __init__(self, api: BilibiliAPI) -> None:
        """初始化画像采集器。

        Args:
            api: 复用全站限频策略的 B 站 API 客户端。

        Returns:
            无。
        """
        self.api = api

    async def fetch(self, uid: int) -> dict[str, Any]:
        """采集真人判定所需的最小公开画像。

        Args:
            uid: B 站用户 ID。

        Returns:
            标准画像；单项接口失败会记录在 data_errors。
        """
        errors: list[str] = []
        info_response = await self._safe_request(
            self.api.get_user_info(uid), "资料不可用", errors
        )
        relation_response = await self._safe_request(
            self.api.get_user_relation_stat(uid), "关系数据不可用", errors
        )
        video_response = await self._safe_request(
            self.api.get_user_videos(uid, page=1, page_size=10),
            "投稿数据不可用",
            errors,
        )
        dynamic_response = await self._safe_request(
            self.api.get(
                f"{self.api.BASE_URL}/x/polymer/web-dynamic/v1/feed/space",
                params={"host_mid": uid, "offset": ""},
            ),
            "动态数据不可用",
            errors,
        )

        info = info_response.get("data") or {}
        relation = relation_response.get("data") or {}
        videos = (video_response.get("data") or {}).get("page") or {}
        dynamics = dynamic_response.get("items") or []
        activity = self._summarize_dynamics(dynamics)
        return {
            "uid": int(uid),
            "name": str(info.get("name") or f"UID {uid}"),
            "level": int(info.get("level") or 0),
            "follower": int(relation.get("follower") or 0),
            "following": int(relation.get("following") or 0),
            "video_count": int(videos.get("count") or 0),
            **activity,
            "account_age_note": "公开接口无精确注册时间，此值为近期样本中最早可观察动态距今天数",
            "data_errors": errors,
        }

    @staticmethod
    async def _safe_request(
        request: Awaitable[dict[str, Any]],
        error_label: str,
        errors: list[str],
    ) -> dict[str, Any]:
        """执行单项外部请求并将失败降级为空响应。

        Args:
            request: 已构造的异步 API 请求。
            error_label: 面向结果的错误类别。
            errors: 收集错误信息的列表。

        Returns:
            接口字典响应；失败时返回空字典。
        """
        try:
            response = await request
            return response if isinstance(response, dict) else {}
        except Exception as exc:
            errors.append(f"{error_label}: {exc}")
            return {}

    @staticmethod
    def _summarize_dynamics(dynamics: list[dict[str, Any]]) -> dict[str, Any]:
        """汇总近期动态活跃度与抽奖转发特征。

        Args:
            dynamics: 用户动态接口 items 列表。

        Returns:
            活跃数、抽奖转发比例和可观察账号天数。
        """
        sample = dynamics[:12]
        lottery_count = 0
        timestamps: list[int] = []
        for item in sample:
            modules = item.get("modules") or {}
            author = modules.get("module_author") or {}
            timestamp = int(author.get("pub_ts") or 0)
            if timestamp:
                timestamps.append(timestamp)

            dynamic = modules.get("module_dynamic") or {}
            text = str((dynamic.get("desc") or {}).get("text") or "")
            if item.get("type") == "DYNAMIC_TYPE_FORWARD" and any(
                word in text for word in ("抽奖", "转发", "开奖", "中奖")
            ):
                lottery_count += 1

        activity_count = len(sample)
        observed_days = int((time.time() - min(timestamps)) / 86400) if timestamps else 0
        return {
            "recent_activity_count": activity_count,
            "lottery_repost_count": lottery_count,
            "lottery_repost_ratio": round(lottery_count / activity_count, 3)
            if activity_count
            else 0,
            "observable_account_days": observed_days,
        }
