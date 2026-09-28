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
        info_response, info_state = await self._safe_request(
            self.api.get_user_info(uid), "资料不可用", errors
        )
        relation_response, relation_state = await self._safe_request(
            self.api.get_user_relation_stat(uid), "关系数据不可用", errors
        )
        video_response, video_state = await self._safe_request(
            self.api.get_user_videos(uid, page=1, page_size=10),
            "投稿数据不可用",
            errors,
        )
        dynamic_response, dynamic_state = await self._safe_request(
            self.api.get(
                f"{self.api.BASE_URL}/x/polymer/web-dynamic/v1/feed/space",
                params={"host_mid": uid, "offset": ""},
            ),
            "动态数据不可用",
            errors,
        )

        info_value = info_response.get("data")
        relation_value = relation_response.get("data")
        video_value = video_response.get("data")
        info = info_value if isinstance(info_value, dict) else {}
        relation = relation_value if isinstance(relation_value, dict) else {}
        videos = video_value.get("page") if isinstance(video_value, dict) else None
        videos = videos if isinstance(videos, dict) else {}
        dynamics_value = dynamic_response.get("items")
        dynamics = dynamics_value if isinstance(dynamics_value, list) else []
        if info_state == "ok" and not isinstance(info_value, dict):
            info_state = "invalid"
        if relation_state == "ok" and not isinstance(relation_value, dict):
            relation_state = "invalid"
        if video_state == "ok" and not isinstance(video_value, dict):
            video_state = "invalid"
        if dynamic_state == "ok" and not isinstance(dynamics_value, list):
            dynamic_state = "invalid"
        activity = self._summarize_dynamics(dynamics) if dynamic_state == "ok" else {
            "recent_activity_count": None,
            "lottery_repost_count": None,
            "lottery_repost_ratio": None,
            "observable_account_days": None,
        }
        # 消费上游 wrapper 的 _meta.field_status：缺 meta 时退化为“字段是否存在”。
        def _field_state(meta_status: Any, key: str, present: bool) -> str:
            """返回字段级状态 ok/missing/invalid，优先使用上游 _meta。"""
            if isinstance(meta_status, dict) and key in meta_status:
                return str(meta_status.get(key))
            return "ok" if present else "missing"

        info_meta = info_response.get("_meta") if isinstance(info_response.get("_meta"), dict) else {}
        relation_meta = relation_response.get("_meta") if isinstance(relation_response.get("_meta"), dict) else {}
        video_meta = video_response.get("_meta") if isinstance(video_response.get("_meta"), dict) else {}
        info_status = info_meta.get("field_status") if isinstance(info_meta.get("field_status"), dict) else {}
        relation_status = relation_meta.get("field_status") if isinstance(relation_meta.get("field_status"), dict) else {}
        video_field_status = video_meta.get("field_status") if isinstance(video_meta.get("field_status"), dict) else {}

        # 兼容 data.level=0 不能覆盖 _meta 的 missing/invalid（规格 §3.2）。
        level_state = _field_state(info_status, "level", "level" in info)
        level = self._optional_int(info.get("level")) if level_state == "ok" else None
        vip = info.get("vip")
        vip_fields_present = isinstance(vip, dict) and any(
            key in vip for key in ("status", "vipStatus", "type", "vipType")
        )
        vip_status = self._optional_int((vip or {}).get("status", (vip or {}).get("vipStatus"))) if vip_fields_present else None
        vip_type = self._optional_int((vip or {}).get("type", (vip or {}).get("vipType"))) if vip_fields_present else None
        is_vip = bool((vip_status or 0) or (vip_type or 0)) if vip_fields_present else None
        # 来源接口失败/字段缺失时数值必须为 None，不允许补 0（规格 §6.5）。
        follower_state = _field_state(relation_status, "follower", "follower" in relation)
        following_state = _field_state(relation_status, "following", "following" in relation)
        follower = self._optional_int(relation.get("follower")) if follower_state == "ok" else None
        following = self._optional_int(relation.get("following")) if following_state == "ok" else None
        video_count_state = _field_state(video_field_status, "count", "count" in videos)
        video_count = self._optional_int(videos.get("count")) if video_count_state == "ok" else None
        return {
            "uid": int(uid),
            "name": str(info.get("name") or f"UID {uid}"),
            "level": level,
            "is_vip": is_vip,
            "vip_type": vip_type,
            "vip_label": (
                "年度大会员" if vip_type == 2 else "大会员" if is_vip else "非会员"
            ) if is_vip is not None else "会员未知",
            "follower": follower,
            "following": following,
            "video_count": video_count,
            **activity,
            # 采集时间与字段级状态，供 v3 缓存命中和证据门禁使用（规格 §6.5）。
            "collected_at": int(time.time()),
            "field_status": {
                "level": level_state,
                "is_vip": "ok" if vip_fields_present else "missing",
                "follower": follower_state,
                "following": following_state,
                "video_count": video_count_state,
                "recent_activity_count": "ok" if dynamic_state == "ok" else dynamic_state,
                "lottery_repost_ratio": "ok" if dynamic_state == "ok" else dynamic_state,
                "observable_account_days": (
                    "ok" if dynamic_state == "ok" and activity.get("observable_account_days") is not None
                    else ("missing" if dynamic_state == "ok" else dynamic_state)
                ),
            },
            "account_age_note": "公开接口无精确注册时间，此值为近期样本中最早可观察动态距今天数",
            "source_status": {
                "info": {"state": info_state},
                "relation": {"state": relation_state},
                "videos": {"state": video_state},
                "dynamics": {"state": dynamic_state},
            },
            "data_errors": errors,
        }

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        """将明确存在的数值转为整数，缺失或非法值保持未知。

        Args:
            value: 外部接口返回的原始字段。

        Returns:
            合法整数；无法确认时返回 ``None``。
        """
        try:
            return int(value) if value is not None and not isinstance(value, bool) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    async def _safe_request(
        request: Awaitable[dict[str, Any]],
        error_label: str,
        errors: list[str],
    ) -> tuple[dict[str, Any], str]:
        """执行单项外部请求并保留成功或失败状态。

        Args:
            request: 已构造的异步 API 请求。
            error_label: 面向结果的错误类别。
            errors: 收集错误信息的列表。

        Returns:
            接口字典响应和 ``ok/error/invalid`` 状态。
        """
        try:
            response = await request
            if not isinstance(response, dict):
                errors.append(f"{error_label}: 响应格式无效")
                return {}, "invalid"
            return response, "ok"
        except Exception as exc:
            errors.append(f"{error_label}: {exc}")
            return {}, "error"

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
        observed_days = int((time.time() - min(timestamps)) / 86400) if timestamps else None
        return {
            "recent_activity_count": activity_count,
            "lottery_repost_count": lottery_count,
            "lottery_repost_ratio": round(lottery_count / activity_count, 3)
            if activity_count
            else 0,
            "observable_account_days": observed_days,
        }
