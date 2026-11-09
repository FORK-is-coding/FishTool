"""抽奖工具核心服务。

合规声明：本模块仅访问 B 站公开接口和用户已授权的登录态数据，严格复用项目限频器，
不绕过访问控制，不进行高并发抓取；用户应遵守平台条款并仅将结果用于合规运营。
"""

import asyncio
import hashlib
import secrets
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from bilibili.api import BilibiliAPI
from bilibili.rate_limiter import RateLimiter
from core.database import Comment, Video, get_session
from modules.comment.collector import CommentCollector
from core.logger import get_logger

from .analyzer import classify_profiles
# JSON 缓存仓储与候选字段转换器。
from .cache import LotteryCache, snapshot_is_reusable
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
#: 动态评论快照默认复用窗口（秒），可由 lottery.comment_snapshot_ttl_seconds 覆盖。
_DEFAULT_SNAPSHOT_TTL_S = 600

#: full 命中所需的来源组：等级/动态/投稿数分别来自 info / dynamics / videos（规格 §6.4）。
_FULL_REQUIRED_GROUPS = ("info", "dynamics", "videos")
#: full 命中必须四字段真实可用，不是旧版“三键存在”。
_FULL_REQUIRED_FIELDS = ("level", "recent_activity_count", "lottery_repost_ratio", "video_count")
#: 真人筛选画像按批读写缓存的批大小：中断后最多重采这一批。
_RESUME_BATCH_SIZE = 20
#: 真人筛选复用画像时要求齐全的来源组：在 full 命中所需三组之外加上 relation，
#: 因为粉丝 / 关注数也会交给 AI 判定，缺了它续跑时的判定证据会比首次少。
_RESUME_PROFILE_GROUPS = ("info", "relation", "dynamics", "videos")
#: AI 判定每批用户数（沿用原 12 人一批）。
_CLASSIFY_BATCH_SIZE = 12
#: AI 判定缓存键依据的画像字段；采集时间、昵称等不影响判定的字段不参与，保证重采同值时可复用。
_ASSESSMENT_EVIDENCE_FIELDS = (
    "level", "is_vip", "vip_type", "follower", "following", "video_count",
    "recent_activity_count", "lottery_repost_count", "lottery_repost_ratio", "observable_account_days",
)
#: draw 资格只需来源可确认的等级/会员字段，对应 info 组。
_DRAW_REQUIRED_GROUPS = ("info",)
#: source-group 到画像字段的映射，用于按组读写 v3 缓存。
_GROUP_FIELDS: Dict[str, tuple] = {
    "info": ("name", "level", "is_vip", "vip_type", "vip_label", "vip"),
    "relation": ("follower", "following"),
    "videos": ("video_count",),
    "dynamics": (
        "recent_activity_count",
        "lottery_repost_count",
        "lottery_repost_ratio",
        "observable_account_days",
    ),
}


# 抽奖服务编排层，负责缓存、采集、判定与抽取全流程。
class LotteryService:
    """编排目标预览、本地复用、评论采集、真人筛选与随机抽取。"""

    def __init__(
        self,
        api: BilibiliAPI,
        cache_dir: Optional[Path] = None,
        snapshot_ttl_s: Optional[int] = None,
    ):
        """初始化服务，复用 API 限频与画像缓存。

        Args:
            api: B 站异步 API 客户端。
            cache_dir: 动态评论本地缓存目录。
            snapshot_ttl_s: 动态评论快照复用窗口秒数；``None`` 时读取配置。
        """
        # 显式传入时覆盖配置，便于测试与调用方定制。
        self._snapshot_ttl_override = snapshot_ttl_s
        # 复用传入的 API 客户端，共享全站限频配额。
        self.api = api
        self.collector = CommentCollector(api)
        self._comment_rate_limiter = RateLimiter(rate=4.0)
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

    def _snapshot_ttl_s(self) -> int:
        """返回动态评论快照复用窗口秒数。

        优先使用构造参数，其次读取 ``lottery.comment_snapshot_ttl_seconds``，
        配置缺失或非法（非正整数）时回退默认 600 秒。
        """
        raw: Any = self._snapshot_ttl_override
        if raw is None:
            try:
                # 延迟导入，每次读取以便配置热重载后生效。
                from core.config import config as _config

                raw = _config.get("lottery.comment_snapshot_ttl_seconds")
            except Exception as exc:  # noqa: BLE001 - 配置不可用时回退默认值
                logger.warning("读取抽奖评论快照 TTL 配置失败，使用默认值: %s", exc)
                raw = None
        if isinstance(raw, bool):
            raw = None
        try:
            ttl_s = int(raw) if raw is not None else _DEFAULT_SNAPSHOT_TTL_S
        except (TypeError, ValueError):
            ttl_s = _DEFAULT_SNAPSHOT_TTL_S
        return ttl_s if ttl_s > 0 else _DEFAULT_SNAPSHOT_TTL_S

    def _load_dynamic_comments(self, dynamic_id: str) -> List[Dict[str, Any]]:
        """读取动态评论缓存，损坏时降级为空列表。"""
        return self._cache.load_dynamic_comments(dynamic_id)

    def _save_dynamic_comments(
        self,
        dynamic_id: str,
        comments: List[Dict[str, Any]],
        complete: bool = False,
    ) -> None:
        """原子保存动态评论缓存，并记录本次采集是否完整。"""
        self._cache.save_dynamic_comments(dynamic_id, comments, complete=complete)

    @staticmethod
    def _parse_reply(reply: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """清洗单条 B 站评论，保留兼容入口。"""
        return parse_reply(reply)

    async def _collect_video_comments(
        self, target: LotteryTarget, progress: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """按视频读取当前账号的评论管理列表，只有完整翻页才返回候选。

        固定本轮 Cookie，避免 Cookie 池轮换账号造成空页或名单混用。
        每页 50 条，保留楼中楼；按 rpid 去重，UID 去重仍由候选池统一处理。
        """
        cookie = self.api.cookie
        if self.api.cookie_pool is not None:
            account = await self.api.cookie_pool.get_cookie()
            cookie = account.cookie_data if account else ""
        if not cookie:
            raise ValueError("请先在 FishTool 中登录 B站，再采集本人视频的抽奖评论")

        async with BilibiliAPI(cookie=cookie, rate_limiter=self.api.rate_limiter) as api:
            login = await api.get(f"{api.BASE_URL}/x/web-interface/nav")
            if not login.get("isLogin") or not login.get("mid"):
                raise ValueError("B站登录态已失效，请重新扫码登录")
            video = await api.get(
                f"{api.BASE_URL}/x/web-interface/view", params={"aid": target.oid},
            )
            if str((video.get("owner") or {}).get("mid")) != str(login["mid"]):
                raise ValueError("视频抽奖仅支持当前登录账号发布的视频，请切换到发布者账号")

            comments: Dict[str, Dict[str, Any]] = {}
            total = None
            page_number = 1
            while True:
                await self._comment_rate_limiter.acquire(endpoint="comment")
                data = await api.get(
                    f"{api.BASE_URL}/x/v2/reply/up/fulllist",
                    params={"oid": target.oid, "type": 1, "order": 1,
                            "filter": -1, "pn": page_number, "ps": 50},
                )
                page = data.get("page") or {}
                rows = data.get("list")
                count = page.get("total")
                if rows is None and count == 0:
                    rows = []
                if (type(count) is not int or count < 0 or not isinstance(rows, list)
                        or page.get("num") != page_number):
                    raise ValueError("评论管理接口返回的分页信息不完整，未生成抽奖名单")
                if total is not None and count != total:
                    raise ValueError("采集期间评论总数发生变化，请等待评论稳定后重新采集")
                total = count
                before = len(comments)
                for raw in rows:
                    if (not isinstance(raw, dict) or str(raw.get("oid")) != str(target.oid)
                            or raw.get("type") != 1):
                        raise ValueError("评论管理接口返回了其他内容的评论，未生成抽奖名单")
                    item = self._parse_reply(raw)
                    if not item or not item["rpid"] or item["uid"] <= 0:
                        raise ValueError("评论缺少参与者或评论 ID，未生成抽奖名单")
                    comments[item["rpid"]] = item
                if progress:
                    progress("collecting_comments", 12 + int(40 * min(len(comments), total) / max(total, 1)),
                             f"已读取本人视频评论 {len(comments)}/{total} 条（含楼中楼）")
                if len(comments) == total:
                    return list(comments.values())
                if len(comments) > total or len(comments) == before:
                    raise ValueError("评论分页未覆盖完整名单，请重新采集后再抽奖")
                page_number += 1

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
        # 是否确认翻到评论区末页，决定缓存能否被后续抽奖直接复用。
        complete = False
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
            # 只有接口明确声明 is_end 才算翻到末页；缺游标、空页提前结束或触达上限都不算完整。
            complete = bool((data.get("cursor") or {}).get("is_end"))
            next_offset = (cursor.get("pagination_reply") or {}).get("next_offset") or cursor.get("next")
            if next_offset is not None:
                offset_text = str(next_offset)
                if offset_text == previous_offset:
                    logger.warning("抽奖评论分页返回重复游标，停止翻页")
                    # 重复游标说明分页异常，本次结果不能当作完整候选集复用。
                    complete = False
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
        # 采集完成落盘缓存，并记录完整性；只有完整快照才会在 TTL 内被复用。
        self._save_dynamic_comments(target.target_id, comments, complete=complete)
        return comments

    async def get_comments(
        self,
        target: LotteryTarget,
        progress: ProgressCallback = None,
        comment_mode: str = "up_fulllist",
    ) -> tuple[List[Dict[str, Any]], str]:
        """??? comment_mode ??????????????????

        comment_mode:
        - "standard"??????????????????????
        - "up_fulllist"?UP ?????????????????????
        """
        if target.target_type == "video":
            if comment_mode == "up_fulllist":
                return await self._collect_video_comments(target, progress), "crawler"
            # standard????????????
            if progress:
                progress("local_lookup", 8, "??????????????????????")
            ttl_s = self._snapshot_ttl_s()
            now_s = int(time.time())
            local: List[Dict[str, Any]] = []
            snapshot = self._cache.load_video_snapshot(target.target_id)
            if snapshot_is_reusable(snapshot, now_s, ttl_s):
                local = self._load_video_comments(target.target_id)
            if local:
                if progress:
                    progress("local_reuse", 45, f"????? {len(local)} ??????????")
                return local, "local"
            if progress:
                progress("collecting_comments", 12, "?????????????????????????????????")
            comments = await self.collector.collect_video_comments(
                target.target_id,
                strategy=CommentCollector.STRATEGY_FULL,
            )
            save_result = getattr(self.collector, "last_save_result", None) or {}
            if comments and save_result.get("success", True):
                self._cache.mark_video_snapshot(target.target_id, len(comments), complete=True)
            return comments, "crawler"
        if progress:
            progress("local_lookup", 8, "?????????????????")
        # ???? TTL ??????????????????????
        ttl_s = self._snapshot_ttl_s()
        now_s = int(time.time())
        local: List[Dict[str, Any]] = []
        snapshot = self._cache.load_dynamic_snapshot(target.target_id)
        if snapshot_is_reusable(snapshot, now_s, ttl_s):
            local = list(snapshot["comments"])
        if local:
            if progress:
                progress("local_reuse", 45, f"????? {len(local)} ??????????")
            return local, "local"
        if progress:
            progress("collecting_comments", 12, "?????????????????????????????????")
        comments = await self._collect_dynamic_comments(target, progress)
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

        # v3：full 空间按来源组命中，缺字段或旧缓存一律重采（规格 §6.5）。
        profiles: List[Dict[str, Any]] = []
        local_count = 0
        fetched_count = 0
        partial_count = 0
        for winner in winners:
            uid = int(winner.get("uid") or 0)
            if uid <= 0:
                raise ValueError("中奖名单包含无效 UID")
            lookup = self._cache.get_profile(
                uid,
                namespace="full",
                now_s=int(time.time()),
                required_groups=list(_FULL_REQUIRED_GROUPS),
            )
            hit = (
                lookup.cache_state == "hit"
                and isinstance(lookup.profile, dict)
                and all(lookup.profile.get(field) is not None for field in _FULL_REQUIRED_FIELDS)
            )
            if hit:
                profile = dict(lookup.profile)
                local_count += 1
            else:
                refresh_groups = list(lookup.refresh_groups) or list(_FULL_REQUIRED_GROUPS)
                if lookup.cache_state in ("legacy", "error", "miss"):
                    # 旧扁平/损坏缓存不可信，必要组全部重采。
                    refresh_groups = list(_FULL_REQUIRED_GROUPS)
                seqs = {
                    group: self._cache.begin_attempt(
                        uid, namespace="full", group=group, now_s=int(time.time())
                    )
                    for group in refresh_groups
                }
                fetched = None
                try:
                    fetched = await self.fetch_user_profile(uid)
                except Exception as exc:
                    logger.warning("中奖用户 %s 画像采集失败: %s", uid, exc)
                if fetched is not None:
                    plan = self._split_profile_groups(fetched)
                    for group, seq in seqs.items():
                        self._cache.merge_attempt(
                            uid,
                            namespace="full",
                            group=group,
                            attempt=seq,
                            success_payload=plan.get(group, {}),
                            now_s=int(time.time()),
                        )
                    profile = dict(fetched)
                    fetched_count += 1
                else:
                    # 采集失败：登记失败 attempt（不替换历史成功），本次按旧值构造并标 partial。
                    for group, seq in seqs.items():
                        self._cache.merge_attempt(
                            uid,
                            namespace="full",
                            group=group,
                            attempt=seq,
                            success_payload=None,
                            now_s=int(time.time()),
                        )
                    profile = dict(lookup.profile) if isinstance(lookup.profile, dict) else {"uid": uid}
                    partial_count += 1
            profile["uid"] = uid
            profile["name"] = str(profile.get("name") or winner.get("uname") or f"UID {uid}")
            profiles.append(profile)
            await asyncio.sleep(0)

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
            "partial_count": partial_count,
            "real_count": sum(item.get("classification") == "real" for item in results),
            "suspicious_count": sum(item.get("classification") == "suspicious" for item in results),
            "indeterminate_count": sum(item.get("classification") == "indeterminate" for item in results),
            "results": results,
        }

    async def _profile_and_classify(
        self,
        users: Dict[int, str],
        focus_template: Optional[str],
        progress: ProgressCallback,
        *,
        profile_stage: tuple,
        classify_stage: Optional[tuple],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """采集画像并做 AI 判定；两步都按批读写缓存，中断后再次执行只补缺失部分。

        画像复用 ``full`` 空间、与中奖校验同一套 v3 命中规则：四个判定字段齐全且各来源组新鲜才算命中，
        否则重新采集并按批回写。AI 判定只缓存 LLM 实际返回的结果（``source == "llm"``），
        键绑定 UID、判定侧重点与本次画像的判定字段，画像变化后旧判定自动失效。

        Args:
            users: ``{uid: 评论昵称}``，按评论出现顺序。
            focus_template: AI 判定侧重点。
            progress: 进度回调。
            profile_stage: ``(阶段名, 起始百分比, 跨度, 文案前缀)``。
            classify_stage: 同上；``None`` 表示判定阶段不推送进度。

        Returns:
            按 ``users`` 顺序的画像列表与判定列表。
        """
        uids = list(users)
        total = len(uids)
        profiles: List[Dict[str, Any]] = []
        stage, base, span, label = profile_stage
        for start in range(0, total, _RESUME_BATCH_SIZE):
            batch = uids[start:start + _RESUME_BATCH_SIZE]
            lookups = self._cache.get_profiles_batch(
                batch, namespace="full", now_s=int(time.time()), required_groups=list(_RESUME_PROFILE_GROUPS)
            )
            fetched_plans: Dict[int, Optional[Dict[str, Dict[str, Any]]]] = {}
            for offset, uid in enumerate(batch, start=start + 1):
                lookup = lookups[uid]
                if (
                    lookup.cache_state == "hit"
                    and isinstance(lookup.profile, dict)
                    and all(lookup.profile.get(field) is not None for field in _FULL_REQUIRED_FIELDS)
                ):
                    # 缓存只保存分组值，不含字段级状态；命中即四个判定字段都来自成功采集。
                    profile = {**lookup.profile, "uid": uid, "field_status": {field: "ok" for field in _FULL_REQUIRED_FIELDS}}
                else:
                    profile = await self.fetch_user_profile(uid)
                    fetched_plans[uid] = self._split_profile_groups(profile)
                profile["name"] = str(profile.get("name") or users[uid])
                profiles.append(profile)
                if progress:
                    percent = base + int(offset / max(total, 1) * span)
                    progress(stage, percent, f"{label}：{offset}/{total}")
                await asyncio.sleep(0)
            # 每批落盘一次：中断时最多重采这一批。
            self._cache.merge_profiles_batch(
                fetched_plans, namespace="full", groups=list(_RESUME_PROFILE_GROUPS), now_s=int(time.time())
            )

        assessments: List[Dict[str, Any]] = []
        for start in range(0, len(profiles), _CLASSIFY_BATCH_SIZE):
            batch_profiles = profiles[start:start + _CLASSIFY_BATCH_SIZE]
            keys = [self._assessment_key(profile, focus_template) for profile in batch_profiles]
            cached = self._cache.load_assessments(keys)
            pending = [profile for profile, key in zip(batch_profiles, keys) if key not in cached]
            fresh = await classify_profiles(pending, focus_template) if pending else []
            fresh_by_uid = {int(item["uid"]): item for item in fresh}
            to_save: Dict[str, Dict[str, Any]] = {}
            for profile, key in zip(batch_profiles, keys):
                if key in cached:
                    assessments.append(cached[key])
                    continue
                item = fresh_by_uid.get(int(profile["uid"]))
                if item is None:
                    # 判定函数未返回该用户（契约外情况）：与原逻辑一致，不凭空补结果。
                    continue
                assessments.append(item)
                # LLM 不可用时的规则兜底不缓存，避免一次短暂故障把这批用户永久判成资料不足。
                if item.get("source") == "llm":
                    to_save[key] = item
            self._cache.save_assessments(to_save)
            if progress and classify_stage:
                stage, base, span, label = classify_stage
                done = min(start + _CLASSIFY_BATCH_SIZE, len(profiles))
                percent = base + int(done / max(len(profiles), 1) * span)
                progress(stage, percent, f"{label}：{done}/{len(profiles)}")
        return profiles, assessments

    @staticmethod
    def _assessment_key(profile: Dict[str, Any], focus_template: Optional[str]) -> str:
        """生成 AI 判定缓存键：UID + 侧重点 + 判定所依据的画像字段，任何一项变化都不复用。"""
        evidence = {field: profile.get(field) for field in _ASSESSMENT_EVIDENCE_FIELDS}
        payload = json.dumps(
            {"uid": int(profile["uid"]), "focus": (focus_template or "").strip(), "evidence": evidence},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def filter_real_users(
        self,
        target: LotteryTarget,
        focus_template: Optional[str],
        progress: ProgressCallback = None,
        comment_mode: str = "up_fulllist",
    ) -> Dict[str, Any]:
        """分析评论区全部去重用户并分类真人与疑似抽奖号。"""
        comments, source = await self.get_comments(target, progress, comment_mode=comment_mode)
        users: Dict[int, str] = {}
        for comment in comments:
            uid = int(comment.get("uid") or 0)
            if uid:
                users.setdefault(uid, str(comment.get("uname") or f"UID {uid}"))
        # 画像与 AI 判定都走缓存：任务中断（取消 / 服务重启）后再次执行只补未完成的部分。
        profiles, assessments = await self._profile_and_classify(
            users,
            focus_template,
            progress,
            profile_stage=("profiling_users", 52, 28, "正在核验账号等级、活跃轨迹与抽奖转发"),
            classify_stage=("ai_classifying", 80, 18, "AI 正在受限于已采集数据进行交叉判断"),
        )
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


    @staticmethod
    def _split_profile_groups(profile: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """把整份画像按 source-group 拆分，只保留非 None 字段（规格 §6.4）。

        Args:
            profile: 采集得到的最小画像。

        Returns:
            ``{group: {field: value}}``，缺少有效字段的组不出现在结果中。
        """
        plan: Dict[str, Dict[str, Any]] = {}
        for group, fields in _GROUP_FIELDS.items():
            payload = {field: profile.get(field) for field in fields if profile.get(field) is not None}
            if payload:
                plan[group] = payload
        return plan

    def _load_profile_cache(self, namespace: str = "full") -> Dict[str, Dict[str, Any]]:
        """读取画像缓存（默认 full v3 空间；保留 namespace 参数供旧调用）。

        Args:
            namespace: 缓存空间，``full`` 或 ``draw``。

        Returns:
            以字符串 UID 为键的扁平兼容快照。
        """
        return self._cache.load_profiles(namespace)

    def _save_profile_cache(
        self, profiles: Dict[str, Dict[str, Any]], namespace: str = "full"
    ) -> None:
        """兼容入口：把整份映射写入指定 v3 空间。新业务应走 begin/merge 分组合并。

        Args:
            profiles: UID 到画像的映射。
            namespace: 目标空间。

        Returns:
            无。
        """
        self._cache.save_profiles(profiles, namespace)

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

        profiles, _fetched = await self._resolve_missing_profiles(missing_uids, progress)

        # 只回写有来源证据的有效字段，缺失/旧缓存值不得污染 Comment（规格 §6.5）。
        successful_profiles = {
            uid: {key: value for key, value in profile.items() if value is not None}
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
        profiles: Dict[int, Dict[str, Any]] = {}
        uncached: List[int] = []
        for uid in sorted(missing_uids):
            lookup = self._cache.get_profile(
                uid,
                namespace="draw",
                now_s=int(time.time()),
                required_groups=list(_DRAW_REQUIRED_GROUPS),
            )
            cached = lookup.profile if lookup.cache_state == "hit" else None
            if (
                isinstance(cached, dict)
                and cached.get("level") is not None
                and cached.get("is_vip") is not None
            ):
                profiles[uid] = cached
            else:
                uncached.append(uid)

        for index, uid in enumerate(uncached, start=1):
            seq = self._cache.begin_attempt(uid, namespace="draw", group="info", now_s=int(time.time()))
            try:
                response = await self.api.get_user_info(uid)
                info = (response or {}).get("data") or {}
                meta = (response or {}).get("_meta")
                profile = profile_from_user_info(info, meta=meta)
                profiles[uid] = profile
                payload = {
                    field: profile.get(field)
                    for field in ("level", "is_vip", "vip_type", "vip_label", "vip")
                    if profile.get(field) is not None
                }
                # 成功结构无任何有效字段时记录失败，绝不缓存伪 0/false（§6.5）。
                self._cache.merge_attempt(
                    uid,
                    namespace="draw",
                    group="info",
                    attempt=seq,
                    success_payload=payload if payload else None,
                    now_s=int(time.time()),
                )
            except Exception as exc:
                logger.warning("候选用户 %s 资料补全失败: %s", uid, exc)
                self._cache.merge_attempt(
                    uid,
                    namespace="draw",
                    group="info",
                    attempt=seq,
                    success_payload=None,
                    now_s=int(time.time()),
                )
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
        return profiles, bool(uncached)

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

        # 与真人筛选共用可续跑的画像 + 判定流程；判定阶段沿用原逻辑不推送进度。
        profiles, assessments = await self._profile_and_classify(
            users,
            focus_template,
            progress,
            profile_stage=("real_filter_profiles", 72, 16, "正在执行真人筛选画像采集"),
            classify_stage=None,
        )
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
        comment_mode: str = "up_fulllist",
    ) -> Dict[str, Any]:
        """按显式条件过滤评论，并可选复用真人判定链后安全抽奖。"""
        # 先取评论（本地或在线），再补全等级/会员元数据。
        comments, source = await self.get_comments(target, progress, comment_mode=comment_mode)
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
