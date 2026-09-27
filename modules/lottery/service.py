"""抽奖工具核心服务。

合规声明：本模块仅访问 B 站公开接口和用户已授权的登录态数据，严格复用项目限频器，
不绕过访问控制，不进行高并发抓取；用户应遵守平台条款并仅将结果用于合规运营。
"""

import asyncio
import secrets
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from bilibili.api import BilibiliAPI
from core.database import Comment, Video, get_session
from core.logger import get_logger
from modules.comment.collector import CommentCollector

from .analyzer import classify_profiles
# JSON 缓存仓储与候选字段转换器。
from .cache import LotteryCache
from .candidate import (
    build_candidate_pool,
    comment_row_to_dict,
    merge_profile_metadata,
    missing_metadata_uids,
    parse_comment_time,
    parse_reply,
    profile_from_user_info,
    unresolved_metadata_uids,
)
# 目标解析与元数据获取。
from .profile import ProfileCollector
from .target import LotteryTarget, fetch_target_metadata

# 模块级日志实例。
logger = get_logger(__name__)
# 进度回调类型：阶段名 + 百分比 + 提示文案。
ProgressCallback = Optional[Callable[[str, int, str], None]]


# 抽奖服务编排层，负责缓存、采集、判定与抽取全流程。
class LotteryService:
    """编排目标预览、本地复用、评论采集、真人筛选与随机抽取。"""

    def __init__(self, api: BilibiliAPI, cache_dir: Optional[Path] = None):
        """初始化服务并复用现有评论采集器。

        Args:
            api: B 站异步 API 客户端。
            cache_dir: 动态评论本地缓存目录。
        """
        # 复用传入的 API 客户端，共享全站限频配额。
        self.api = api
        # 复用评论采集器，抽奖候选与监控走同一采集链路。
        self.collector = CommentCollector(api)
        # 缓存目录默认放 data/lottery_cache，调用方可按需覆盖。
        self.cache_dir = cache_dir or Path("data") / "lottery_cache"
        self._cache = LotteryCache(self.cache_dir)
        self._profile_collector = ProfileCollector(api)

    async def preview(self, raw_target: str) -> Dict[str, Any]:
        """读取目标元数据，供前端在操作前核对。"""
        try:
            # 拉取目标元数据，供前端操作前预览核对。
            target = await fetch_target_metadata(self.api, raw_target)
            # 转成可序列化字典返回。
            return target.to_dict()
        except Exception:
            # 预览失败记录日志后原样抛出，由路由层转 HTTP 错误。
            logger.exception("抽奖目标预览失败: %s", raw_target)
            raise

    def _load_video_comments(self, bvid: str) -> List[Dict[str, Any]]:
        """从 SQLite 读取指定视频已经采集的评论。"""
        # 先置空会话，便于 finally 统一关闭。
        session = None
        try:
            # 打开独立会话查询，失败不污染其他请求。
            session = get_session()
            # 按 bvid 关联评论表，按时间升序返回稳定顺序。
            rows = (
                session.query(Comment)
                .join(Video, Comment.video_id == Video.id)
                .filter(Video.bvid == bvid)
                .order_by(Comment.ctime.asc())
                .all()
            )
            # 命中本地记录转成候选字段格式。
            return [self._comment_row_to_dict(row) for row in rows]
        except Exception as exc:
            # 本地读取异常降级为空，调用方回退在线采集。
            logger.warning("读取视频本地评论失败，将回退在线采集: %s", exc)
            return []
        finally:
            # 查询结束后关闭会话，避免连接泄漏。
            if session is not None:
                session.close()

    @staticmethod
    def _comment_row_to_dict(row: Comment) -> Dict[str, Any]:
        """将本地评论记录转换为候选字段，保留兼容入口。"""
        return comment_row_to_dict(row)


    def _load_dynamic_comments(self, dynamic_id: str) -> List[Dict[str, Any]]:
        """读取动态评论缓存，损坏时降级为空列表。"""
        return self._cache.load_dynamic_comments(dynamic_id)

    def _save_dynamic_comments(self, dynamic_id: str, comments: List[Dict[str, Any]]) -> None:
        """原子保存动态评论缓存。"""
        self._cache.save_dynamic_comments(dynamic_id, comments)

    @staticmethod
    def _parse_reply(reply: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """清洗单条 B 站评论，保留兼容入口。"""
        return parse_reply(reply)

    async def _collect_dynamic_comments(
        self,
        target: LotteryTarget,
        progress: ProgressCallback = None,
        max_count: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """串行分页采集动态评论并通过项目限频器控制请求速率。"""
        # 收集解析后的评论。
        comments: List[Dict[str, Any]] = []
        # seen 用于按 rpid 去重，防止分页边界重复。
        seen: set[str] = set()
        # 游标从 0 开始，is_end 标记是否还有下一页。
        cursor: Dict[str, Any] = {"next": 0, "is_end": False}
        pagination_str: Optional[str] = None
        previous_offset: Optional[str] = None
        # 页码从 1 起，用于估算进度。
        page = 0
        # 未到末页且未达上限时持续翻页。
        while not cursor.get("is_end") and (max_count is None or len(comments) < max_count):
            # 每翻一页页码加一，驱动游标前进。
            page += 1
            # 动态评论通过统一 API 客户端请求，自动走限频。
            data = await self.api.get(
                f"{self.api.BASE_URL}/x/v2/reply/main",
                params={
                    "oid": target.oid,
                    "type": target.comment_type,
                    "mode": 3,
                    "ps": 20,
                    **({"pagination_str": pagination_str} if pagination_str else {}),
                },
            )
            # 记录当前页游标，供下一轮继续拉取。
            cursor = data.get("cursor") or {"is_end": True}
            next_offset = (cursor.get("pagination_reply") or {}).get("next_offset") or cursor.get("next")
            if next_offset is not None:
                offset_text = str(next_offset)
                if offset_text == previous_offset:
                    logger.warning("抽奖评论分页返回重复游标，停止翻页")
                    break
                previous_offset = offset_text
                pagination_str = json.dumps({"offset": offset_text}, separators=(",", ":"))
            # 逐条清洗评论，缺 UID 的被过滤。
            for raw in data.get("replies") or []:
            # 解析单条评论为最小候选字段。
                item = self._parse_reply(raw)
            # 同一评论只保留一次。
                if item and item["rpid"] not in seen:
                # 记录已见 rpid。
                    seen.add(item["rpid"])
                # 去重后的评论加入结果集。
                    comments.append(item)
            # 进度回调存在时推送进度。
            if progress:
                # 用页数估算进度百分比，上限 52。
                approximate = min(52, 12 + page * 3)
                # 推送当前条数与百分比。
                progress("collecting_comments", approximate, f"已读取动态评论 {len(comments)} 条，正在等待限频后继续翻页")
            # 接口不再返回 replies 说明已到末页。
            if not data.get("replies"):
                break
        # 采集完成落盘缓存，下次直接复用。
        self._save_dynamic_comments(target.target_id, comments)
        return comments

    async def get_comments(
        self,
        target: LotteryTarget,
        progress: ProgressCallback = None,
    ) -> tuple[List[Dict[str, Any]], str]:
        """优先复用本地评论，未命中时调用现有采集能力。"""
        if progress:
            # 通知前端本地检索阶段开始。
            progress("local_lookup", 8, "正在检索工具箱数据库与抽奖缓存，避免重复爬取")
            # 视频查 SQLite，动态查 JSON 缓存。
        local = self._load_video_comments(target.target_id) if target.target_type == "video" else self._load_dynamic_comments(target.target_id)
        if local:
            if progress:
                # 通知前端命中本地缓存，跳过重复爬取。
                progress("local_reuse", 45, f"已复用本地 {len(local)} 条评论，跳过重复采集")
            return local, "local"

        if progress:
            # 本地未命中，通知前端开始在线采集。
            progress("collecting_comments", 12, "本地未找到可复用数据，正在通过现有低频爬虫读取评论区")
            # 视频和动态分别走各自的分页采集逻辑。
        if target.target_type == "video":
            # 视频评论复用统一串行采集器，遵守全站限频。
            comments = await self.collector.collect_video_comments(
                target.target_id,
                strategy=CommentCollector.STRATEGY_FULL,
            )
        else:
            # 动态目标走动态专用采集链路。
            comments = await self._collect_dynamic_comments(target, progress)
            # 在线采集结果标记来源为 crawler。
        return comments, "crawler"

    async def fetch_user_profile(self, uid: int) -> Dict[str, Any]:
        """采集真人判定所需的最小公开画像。

        Args:
            uid: B 站用户 ID。

        Returns:
            标准画像；单项接口失败会在 data_errors 中说明。
        """
        return await self._profile_collector.fetch(uid)

    async def quick_filter(self, uid: int, focus_template: Optional[str]) -> Dict[str, Any]:
        """针对单个 UID 采集画像并执行受约束 AI 判定。"""
        # 单 UID 快速筛选：先采集画像再走同一判定链路。
        profile = await self.fetch_user_profile(uid)
        # LLM 判定结果直接返回给前端。
        result = (await classify_profiles([profile], focus_template))[0]
        return {"profile": profile, "assessment": result}
    async def verify_winners(
        self,
        winners: List[Dict[str, Any]],
        focus_template: Optional[str] = None,
    ) -> Dict[str, Any]:
        """优先复用本地画像，并对中奖用户执行 AI 真人校验。

        Args:
            winners: 当前抽奖产生的中奖用户列表。
            focus_template: 可选的 AI 判定侧重点模板。

        Returns:
            包含本地命中数、在线补取数和逐用户判定的结构化结果。
        """
        if not winners:
            raise ValueError("请先进行抽奖！")

        cache = self._load_profile_cache()
        profiles: List[Dict[str, Any]] = []
        local_count = 0
        fetched_count = 0
        for winner in winners:
            uid = int(winner.get("uid") or 0)
            if uid <= 0:
                raise ValueError("中奖名单包含无效 UID")
            cached = cache.get(str(uid))
            required_fields = {"recent_activity_count", "lottery_repost_ratio", "observable_account_days"}
            if isinstance(cached, dict) and required_fields.issubset(cached):
                profile = dict(cached)
                local_count += 1
            else:
                profile = await self.fetch_user_profile(uid)
                cache[str(uid)] = profile
                fetched_count += 1
            profile["name"] = str(profile.get("name") or winner.get("uname") or f"UID {uid}")
            profiles.append(profile)
            await asyncio.sleep(0)

        if fetched_count:
            self._save_profile_cache(cache)
        assessments = await classify_profiles(profiles, focus_template)
        profile_by_uid = {int(item["uid"]): item for item in profiles}
        winner_by_uid = {int(item.get("uid") or 0): item for item in winners}
        results = []
        for assessment in assessments:
            uid = int(assessment["uid"])
            profile = dict(profile_by_uid.get(uid, {}))
            winner = winner_by_uid.get(uid, {})
            for field in ("uname", "level", "is_vip", "vip_type", "vip_label", "ctime"):
                # 只补空字段，避免覆盖真实值。
                if profile.get(field) is None and winner.get(field) is not None:
                    profile[field] = winner[field]
            results.append({**assessment, "profile": profile})
        return {
            "winner_count": len(winners),
            "local_count": local_count,
            "fetched_count": fetched_count,
            "real_count": sum(item.get("classification") == "real" for item in results),
            "suspicious_count": sum(item.get("classification") == "suspicious" for item in results),
            "indeterminate_count": sum(item.get("classification") == "indeterminate" for item in results),
            "results": results,
        }

    async def filter_real_users(
        self,
        target: LotteryTarget,
        focus_template: Optional[str],
        progress: ProgressCallback = None,
    ) -> Dict[str, Any]:
        """分析评论区全部去重用户并分类真人与疑似抽奖号。"""
        comments, source = await self.get_comments(target, progress)
        users: Dict[int, str] = {}
        for comment in comments:
            uid = int(comment.get("uid") or 0)
            if uid:
                users.setdefault(uid, str(comment.get("uname") or f"UID {uid}"))
        profiles: List[Dict[str, Any]] = []
        total = len(users)
        for index, uid in enumerate(users, start=1):
            profiles.append(await self.fetch_user_profile(uid))
            if progress:
                # 画像采集阶段进度 52-80%。
                percent = 52 + int(index / max(total, 1) * 28)
                # 推送当前进度与提示文案。
                progress("profiling_users", percent, f"正在核验账号等级、活跃轨迹与抽奖转发：{index}/{total}")
                # 让出事件循环避免阻塞。
            await asyncio.sleep(0)

        assessments: List[Dict[str, Any]] = []
        batch_size = 12
        for start in range(0, len(profiles), batch_size):
            assessments.extend(await classify_profiles(profiles[start:start + batch_size], focus_template))
            if progress:
                # 计算本批完成数。
                done = min(start + batch_size, len(profiles))
                # AI 判定阶段进度 80-98%。
                percent = 80 + int(done / max(len(profiles), 1) * 18)
                # 推送判定进度。
                progress("ai_classifying", percent, f"AI 正在受限于已采集数据进行交叉判断：{done}/{len(profiles)}")
        profile_by_uid = {item["uid"]: item for item in profiles}
        results = [
            {**assessment, "profile": profile_by_uid.get(assessment["uid"], {})}
            for assessment in assessments
        ]
        return {
            "target": target.to_dict(),
            "data_source": source,
            "comment_count": len(comments),
            "user_count": len(users),
            "real_users": [item for item in results if item["classification"] == "real"],
            "suspicious_users": [item for item in results if item["classification"] == "suspicious"],
            "indeterminate_users": [item for item in results if item["classification"] == "indeterminate"],
        }


    def _load_profile_cache(self) -> Dict[str, Dict[str, Any]]:
        """读取用户画像缓存，异常时降级为空字典。"""
        return self._cache.load_profiles()

    def _save_profile_cache(self, profiles: Dict[str, Dict[str, Any]]) -> None:
        """原子保存用户画像缓存。"""
        self._cache.save_profiles(profiles)

    def _persist_comment_profiles(self, profiles: Dict[int, Dict[str, Any]]) -> None:
        """把补取的最小用户资料回写到同 UID 的缺字段评论。

        Args:
            profiles: 以 UID 为键的等级和会员资料。

        Returns:
            无；仅更新 ``level_info`` 或 ``vip`` 为空的记录。
        """
        # 会话先置空，finally 统一关闭。
        session = None
        try:
            # 开启数据库会话，批量回写评论字段。
            session = get_session()
            # 统计实际变更条数，无变更不提交。
            changed = 0
            for uid, profile in profiles.items():
                # 按 UID 查询已有评论记录。
                rows = session.query(Comment).filter(Comment.uid == uid).all()
                for row in rows:
                # 逐条补齐缺失的等级字段。
                    if not row.level_info and profile.get("level") is not None:
                    # 写入等级信息。
                        row.level_info = {"current_level": int(profile["level"])}
                        changed += 1
                # 逐条补齐缺失的会员字段。
                    if row.vip is None and profile.get("vip") is not None:
                    # 写入会员信息。
                        row.vip = profile["vip"]
                        changed += 1
            if changed:
                # 有变更才提交事务。
                session.commit()
                # 提交成功后记录变更数量。
                logger.info("已原位补齐评论等级/会员字段 %s 项", changed)
        except Exception:
            # 异常时回滚，保持数据库一致性。
            if session is not None:
                session.rollback()
            # 记录回写失败详情。
            logger.exception("回写评论等级/会员字段失败")
        finally:
            # 无论成败关闭会话。
            if session is not None:
                session.close()

    async def _complete_draw_metadata(
        self,
        comments: List[Dict[str, Any]],
        progress: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """复用缓存并补取候选缺失的等级和会员字段。

        Args:
            comments: 本地或在线取得的候选评论。
            progress: 可选任务进度回调。

        Returns:
            元数据完整的评论副本。
        """
        normalized = [dict(item) for item in comments]
        missing_uids = missing_metadata_uids(normalized)
        if not missing_uids:
            return normalized

        profiles, cache, fetched = await self._resolve_missing_profiles(missing_uids, progress)
        if fetched:
            self._save_profile_cache(cache)

        successful_profiles = {
            uid: profile
            for uid, profile in profiles.items()
            if profile.get("level") is not None and profile.get("vip") is not None
        }
        if successful_profiles:
            self._persist_comment_profiles(successful_profiles)

        normalized = merge_profile_metadata(normalized, profiles)
        unresolved = unresolved_metadata_uids(normalized)
        if unresolved:
            preview = "、".join(str(uid) for uid in unresolved[:5])
            raise RuntimeError(
                f"无法补齐 {len(unresolved)} 位候选用户的等级、会员或评论时间（UID: {preview}），请检查 B 站登录态后重试"
            )
        return normalized

    async def _resolve_missing_profiles(
        self,
        missing_uids: set[int],
        progress: ProgressCallback,
    ) -> tuple[Dict[int, Dict[str, Any]], Dict[str, Dict[str, Any]], bool]:
        """从画像缓存和用户接口解析缺失 UID 的最小资料。

        Args:
            missing_uids: 需要补全的用户 ID。
            progress: 可选任务进度回调。

        Returns:
            画像映射、更新后的缓存和是否执行过在线补取。
        """
        cache = self._load_profile_cache()
        profiles: Dict[int, Dict[str, Any]] = {}
        uncached: List[int] = []
        for uid in sorted(missing_uids):
            cached = cache.get(str(uid))
            if isinstance(cached, dict) and cached.get("level") is not None and cached.get("is_vip") is not None:
                profiles[uid] = cached
            else:
                uncached.append(uid)

        for index, uid in enumerate(uncached, start=1):
            try:
                info = (await self.api.get_user_info(uid)).get("data") or {}
                profile = profile_from_user_info(info)
                profiles[uid] = profile
                cache[str(uid)] = profile
            except Exception as exc:
                logger.warning("候选用户 %s 资料补全失败: %s", uid, exc)
                profiles[uid] = {
                    "level": None,
                    "is_vip": None,
                    "vip_type": None,
                    "vip_label": "会员未知",
                }
            if progress:
                percent = 48 + int(index / max(len(uncached), 1) * 32)
                progress("completing_profiles", percent, f"仅补齐缺失的等级/大会员字段：{index}/{len(uncached)}")
            await asyncio.sleep(0)
        return profiles, cache, bool(uncached)

    @staticmethod
    def _parse_comment_time(value: Any) -> Optional[datetime]:
        """解析评论时间，保留兼容入口。"""
        return parse_comment_time(value)

    async def _apply_real_filter(
        self,
        pool: List[Dict[str, Any]],
        include_indeterminate: bool,
        focus_template: Optional[str],
        progress: ProgressCallback,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """复用真人筛选链判定候选池，并按显式未知策略过滤。

        Args:
            pool: 已完成常规条件过滤的候选评论。
            include_indeterminate: 是否允许未知判定用户保留在候选池。
            focus_template: 可选 AI 判定侧重点。
            progress: 可选任务进度回调。

        Returns:
            过滤后的候选池与按唯一 UID 统计的三态判定详情。
        """
        users: Dict[int, str] = {}
        for item in pool:
            uid = int(item.get("uid") or 0)
            if uid:
                users.setdefault(uid, str(item.get("uname") or f"UID {uid}"))

        profiles: List[Dict[str, Any]] = []
        for index, (uid, uname) in enumerate(users.items(), start=1):
            profile = await self.fetch_user_profile(uid)
            profile["name"] = str(profile.get("name") or uname)
            profiles.append(profile)
            if progress:
                percent = 72 + int(index / max(len(users), 1) * 16)
                progress("real_filter_profiles", percent, f"正在执行真人筛选画像采集：{index}/{len(users)}")
            await asyncio.sleep(0)

        assessments: List[Dict[str, Any]] = []
        for start in range(0, len(profiles), 12):
            assessments.extend(await classify_profiles(profiles[start:start + 12], focus_template))
        assessment_by_uid = {int(item["uid"]): item for item in assessments}
        stats = {
            "real": sum(item.get("classification") == "real" for item in assessments),
            "non_real": sum(item.get("classification") == "suspicious" for item in assessments),
            "indeterminate": sum(item.get("classification") == "indeterminate" for item in assessments),
        }
        allowed_uids = {
            uid
            for uid, assessment in assessment_by_uid.items()
            if assessment.get("classification") == "real"
            or (include_indeterminate and assessment.get("classification") == "indeterminate")
        }
        filtered = [item for item in pool if int(item.get("uid") or 0) in allowed_uids]
        return filtered, {
            "enabled": True,
            "indeterminate_policy": "include" if include_indeterminate else "exclude",
            "stats": stats,
            "excluded_non_real_users": stats["non_real"],
            "excluded_indeterminate_users": 0 if include_indeterminate else stats["indeterminate"],
            "assessments": assessments,
        }

    async def draw(
        self,
        target: LotteryTarget,
        winner_count: int,
        unique_users: bool,
        progress: ProgressCallback = None,
        vip_only: bool = False,
        min_level: Optional[int] = None,
        date_start: Optional[datetime] = None,
        date_end: Optional[datetime] = None,
        real_only: bool = False,
        include_indeterminate: bool = False,
        focus_template: Optional[str] = None,
    ) -> Dict[str, Any]:
        """按显式条件过滤评论，并可选复用真人判定链后安全抽奖。"""
        # 先取评论（本地或在线），再补全等级/会员元数据。
        comments, source = await self.get_comments(target, progress)
        # 补全筛选所需元数据，再由纯函数统一应用候选规则。
        comments = await self._complete_draw_metadata(comments, progress)
        pool, excluded = build_candidate_pool(
            comments=comments,
            unique_users=unique_users,
            vip_only=vip_only,
            min_level=min_level,
            date_start=date_start,
            date_end=date_end,
        )
        real_filter = {
            "enabled": False,
            "indeterminate_policy": "not_applied",
            "stats": {"real": 0, "non_real": 0, "indeterminate": 0},
            "not_evaluated_users": len({int(item.get("uid") or 0) for item in pool if item.get("uid")}),
            "assessments": [],
        }
        if real_only:
            pool, real_filter = await self._apply_real_filter(
                pool,
                include_indeterminate=include_indeterminate,
                focus_template=focus_template,
                progress=progress,
            )
        if winner_count > len(pool):
            raise ValueError(f"筛选后有效候选仅 {len(pool)} 人，无法抽取 {winner_count} 人")
        # 用系统加密随机源抽样，保证公平不可预测。
        winners = secrets.SystemRandom().sample(pool, winner_count)
        # 返回候选池、中奖名单与排除统计。
        return {
            "target": target.to_dict(),
            "data_source": source,
            "candidate_count": len(pool),
            "candidates": pool,
            "excluded": excluded,
            "filters": {
                "unique_users": unique_users,
                "vip_only": vip_only,
                "min_level": min_level,
                "date_start": date_start.date().isoformat() if date_start else None,
                "date_end": date_end.date().isoformat() if date_end else None,
                "real_only": real_only,
                "include_indeterminate": include_indeterminate,
            },
            "real_filter": real_filter,
            "winners": winners,
        }