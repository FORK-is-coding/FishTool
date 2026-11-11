"""AI 伪人巡检评论采集器（只读）。

- 只支持视频：BV 号或视频链接，动态链接直接拒绝。
- 一级评论走 ``/x/v2/reply/main`` 游标分页（与 lottery/service.py 的写法一致）；
  ``comment_endpoint=wbi_main`` 时改走 ``/x/v2/reply/wbi/main`` 并 WBI 签名。
- 楼中楼：先收一级评论自带的 ``replies`` 预览（不额外请求），再对热门楼层按页抓取，
  总请求数受 ``reply_requests_hard`` 限制。
- 不解析 IP 属地、头像（数据最小化）。
- 限频由传入 api 的 rate_limiter 负责，本模块不自建限频器。
"""
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core.error_policy import log_low_priority
from core.exceptions import (
    BilibiliAPIError,
    CircuitBreakerError,
    CookieExpiredError,
    NetworkError,
    RateLimitError,
)
from core.logger import get_logger
from core.request_budget import RequestBudgetExceeded
from modules.lottery.target import parse_target_input

from .contracts import HARD_REPLY_REQUESTS, PatrolSettings

logger = get_logger(__name__)

ProgressCallback = Optional[Callable[[str, int, str], None]]
CancelCheck = Optional[Callable[[], bool]]
CheckpointCallback = Optional[Callable[[Dict[str, Any]], None]]

#: 遇到这些风控 / 登录类错误必须立即终止整个扫描，交给上层处理。
_FATAL_CODES = (-352, -412, -101, -111, -102)
_WBI_WEB_LOCATION = 1315875


class ScanCancelled(Exception):
    """用户取消扫描（在页与页之间检查）。"""


@dataclass
class VideoMeta:
    bvid: str
    aid: int
    title: str
    owner_mid: int
    owner_name: str
    reply_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {"bvid": self.bvid, "aid": self.aid, "title": self.title, "owner_mid": self.owner_mid,
                "owner_name": self.owner_name, "reply_count": self.reply_count}


@dataclass
class CollectResult:
    comments: List[Dict[str, Any]] = field(default_factory=list)
    partial: bool = False
    partial_reason: str = ""
    pages: int = 0
    failed_pages: int = 0
    reply_requests: int = 0
    parse_failed: int = 0
    cursor: Optional[str] = None


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_fatal(exc: BaseException) -> bool:
    if isinstance(exc, (CookieExpiredError, RateLimitError, CircuitBreakerError, RequestBudgetExceeded)):
        return True
    return isinstance(exc, BilibiliAPIError) and getattr(exc, "bili_code", None) in _FATAL_CODES


def parse_reply(raw: Dict[str, Any], *, is_reply: bool) -> Optional[Dict[str, Any]]:
    """把 B站评论对象清洗成巡检字段；必填字段缺失返回 None。"""
    if not isinstance(raw, dict):
        return None
    member = raw.get("member") if isinstance(raw.get("member"), dict) else {}
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    rpid = _as_int(raw.get("rpid"))
    mid = _as_int(member.get("mid"))
    if not rpid or not mid:
        return None
    level_info = member.get("level_info") if isinstance(member.get("level_info"), dict) else {}
    vip = member.get("vip") if isinstance(member.get("vip"), dict) else {}
    official = member.get("official_verify") if isinstance(member.get("official_verify"), dict) else {}
    ctime = _as_int(raw.get("ctime"))
    members = content.get("members") if isinstance(content.get("members"), list) else []
    at_users = []
    for m in members:
        if isinstance(m, dict) and _as_int(m.get("mid")):
            at_users.append({"mid": _as_int(m.get("mid")), "uname": str(m.get("uname") or "")[:100]})
    root = _as_int(raw.get("root"), 0) or None
    parent = _as_int(raw.get("parent"), 0) or None
    return {
        "rpid": rpid,
        "root_rpid": root,
        "parent_rpid": parent,
        "is_reply": bool(is_reply or root),
        "mid": mid,
        "uname": str(member.get("uname") or "")[:100],
        "sign": str(member.get("sign") or "")[:200],
        "level": _as_int(level_info.get("current_level")),
        "is_vip": (_as_int(vip.get("vipStatus") if "vipStatus" in vip else vip.get("status"), 0) or 0) == 1 if vip else None,
        "official_type": _as_int(official.get("type"), -1),
        "content": str(content.get("message") or ""),
        "ctime": datetime.fromtimestamp(ctime) if ctime else None,
        "like": _as_int(raw.get("like"), 0),
        "rcount": _as_int(raw.get("rcount"), 0),
        "at_users": at_users,
    }


class PatrolCollector:
    """按视频采集一级评论与楼中楼。"""

    def __init__(self, api: Any, settings: PatrolSettings):
        self.api = api
        self.settings = settings

    async def fetch_video_meta(self, raw: str) -> VideoMeta:
        kind, bvid = parse_target_input(raw)
        if kind != "video":
            raise ValueError("伪人巡检暂只支持视频 BV 号")
        data = await self.api.get(f"{self.api.BASE_URL}/x/web-interface/view", params={"bvid": bvid})
        data = data if isinstance(data, dict) else {}
        aid = _as_int(data.get("aid"), 0)
        if not aid:
            raise ValueError("未获取到视频信息，请检查 BV 号")
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
        return VideoMeta(
            bvid=str(data.get("bvid") or bvid),
            aid=aid,
            title=str(data.get("title") or "未获取到视频标题")[:200],
            owner_mid=_as_int(owner.get("mid"), 0) or 0,
            owner_name=str(owner.get("name") or "")[:100],
            reply_count=_as_int(stat.get("reply"), 0) or 0,
        )

    async def _page(self, meta: VideoMeta, mode: str, pagination_str: Optional[str]) -> Dict[str, Any]:
        params: Dict[str, Any] = {"oid": meta.aid, "type": 1, "mode": 3 if mode == "hot" else 2, "ps": 20}
        if pagination_str:
            params["pagination_str"] = pagination_str
        if self.settings.comment_endpoint == "wbi_main":
            params["web_location"] = _WBI_WEB_LOCATION
            return await self.api.get(f"{self.api.BASE_URL}/x/v2/reply/wbi/main", params=params, need_sign=True)
        return await self.api.get(f"{self.api.BASE_URL}/x/v2/reply/main", params=params)

    async def collect(self, meta: VideoMeta, *, max_comments: int, include_replies: bool, mode: str = "time",
                      progress: ProgressCallback = None, is_cancelled: CancelCheck = None,
                      checkpoint: CheckpointCallback = None, resume_cursor: Optional[str] = None) -> CollectResult:
        result = CollectResult()
        seen: Dict[int, Dict[str, Any]] = {}
        top_level = 0
        pagination_str = resume_cursor
        previous_offset: Optional[str] = None
        consecutive_failures = 0
        is_end = False
        max_comments = max(1, min(int(max_comments), self.settings.max_comments_hard))

        def add(item: Optional[Dict[str, Any]]) -> None:
            if item is None:
                result.parse_failed += 1
                return
            if item["rpid"] not in seen:
                seen[item["rpid"]] = item

        while top_level < max_comments:
            if is_cancelled and is_cancelled():
                raise ScanCancelled()
            try:
                data = await self._page(meta, mode, pagination_str)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _is_fatal(exc):
                    raise
                if not isinstance(exc, (BilibiliAPIError, NetworkError)):
                    raise
                if resume_cursor and result.pages == 0:
                    raise ValueError("断点已失效，请重新扫描") from exc
                consecutive_failures += 1
                result.failed_pages += 1
                log_low_priority(exc, logger=logger, action="AI伪人巡检·评论分页失败",
                                 context={"bvid": meta.bvid, "page": result.pages + 1})
                if consecutive_failures >= 2:
                    result.partial = True
                    result.partial_reason = "评论分页连续失败"
                    break
                continue
            consecutive_failures = 0
            result.pages += 1
            data = data if isinstance(data, dict) else {}
            replies = data.get("replies") or []
            if resume_cursor and result.pages == 1 and not replies:
                raise ValueError("断点已失效，请重新扫描")
            for raw in replies:
                item = parse_reply(raw, is_reply=False)
                if item is not None and item["rpid"] not in seen:
                    top_level += 1
                add(item)
                for sub in (raw.get("replies") or []) if isinstance(raw, dict) else []:
                    add(parse_reply(sub, is_reply=True))
            cursor = data.get("cursor") if isinstance(data.get("cursor"), dict) else {}
            is_end = bool(cursor.get("is_end"))
            next_offset = (cursor.get("pagination_reply") or {}).get("next_offset") if isinstance(
                cursor.get("pagination_reply"), dict) else None
            next_offset = next_offset or cursor.get("next")
            if progress:
                pct = 10 + int(50 * min(top_level, max_comments) / max_comments)
                progress("collecting_comments", pct, f"已读取评论 {top_level} 条，正在等待限频后继续翻页")
            if is_end or not replies:
                break
            if next_offset is None:
                result.partial = True
                result.partial_reason = "评论分页缺少游标"
                break
            offset_text = str(next_offset)
            if offset_text == previous_offset:
                logger.warning("[巡检] 评论分页返回重复游标，停止翻页: %s", meta.bvid)
                result.partial = True
                result.partial_reason = "评论分页返回重复游标"
                break
            previous_offset = offset_text
            pagination_str = json.dumps({"offset": offset_text}, separators=(",", ":"))
            result.cursor = pagination_str
            if checkpoint:
                checkpoint({"pagination_str": pagination_str, "pages": result.pages})

        if top_level >= max_comments and not is_end:
            result.partial = True
            result.partial_reason = result.partial_reason or "已达采集上限"

        if include_replies:
            await self._collect_replies(meta, seen, result, progress=progress, is_cancelled=is_cancelled)

        result.comments = list(seen.values())
        return result

    async def _collect_replies(self, meta: VideoMeta, seen: Dict[int, Dict[str, Any]], result: CollectResult, *,
                               progress: ProgressCallback, is_cancelled: CancelCheck) -> None:
        budget = min(self.settings.reply_requests_hard, HARD_REPLY_REQUESTS)
        floors = sorted((c for c in seen.values() if not c["is_reply"] and (c.get("rcount") or 0) > 3),
                        key=lambda c: -(c.get("rcount") or 0))[: self.settings.reply_floors]
        total = max(1, min(budget, len(floors) * self.settings.reply_pages_per_floor))
        for floor in floors:
            for pn in range(1, self.settings.reply_pages_per_floor + 1):
                if result.reply_requests >= budget:
                    result.partial = True
                    reason = "楼中楼已达请求上限"
                    if reason not in result.partial_reason:
                        result.partial_reason = "；".join(x for x in (result.partial_reason, reason) if x)
                    return
                if is_cancelled and is_cancelled():
                    raise ScanCancelled()
                result.reply_requests += 1
                try:
                    data = await self.api.get(
                        f"{self.api.BASE_URL}/x/v2/reply/reply",
                        params={"oid": meta.aid, "type": 1, "root": floor["rpid"], "ps": 20, "pn": pn},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if _is_fatal(exc):
                        raise
                    if not isinstance(exc, (BilibiliAPIError, NetworkError)):
                        raise
                    log_low_priority(exc, logger=logger, action="AI伪人巡检·楼中楼失败",
                                     context={"bvid": meta.bvid, "root": floor["rpid"], "pn": pn})
                    break
                data = data if isinstance(data, dict) else {}
                replies = data.get("replies") or []
                for raw in replies:
                    item = parse_reply(raw, is_reply=True)
                    if item is None:
                        result.parse_failed += 1
                    elif item["rpid"] not in seen:
                        seen[item["rpid"]] = item
                if progress:
                    pct = 60 + int(25 * min(result.reply_requests, total) / total)
                    progress("collecting_replies", pct, f"已读取楼中楼 {result.reply_requests} 页")
                page = data.get("page") if isinstance(data.get("page"), dict) else {}
                count = _as_int(page.get("count"), 0) or 0
                if not replies or pn * 20 >= count:
                    break
        # 预算恰好在最后一个请求用完、但仍有楼层没抓完时，同样标记不完整。
        if result.reply_requests >= budget and len(floors) * self.settings.reply_pages_per_floor > budget:
            result.partial = True
            reason = "楼中楼已达请求上限"
            if reason not in result.partial_reason:
                result.partial_reason = "；".join(x for x in (result.partial_reason, reason) if x)
