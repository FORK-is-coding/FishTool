"""AI 伪人巡检 · 举报。

这是 FishTool 中唯一向 B站发起写请求的代码。约束（设计稿 §2、§5.6）：
- 默认“原生举报”：只生成评论链接，由用户在 B站页面自己提交，FishTool 不发写请求。
- “代发举报”需要：配置允许 + 界面开关 + 单独同意 + 非 AstrBot（或 AstrBot 后台已开启）+ 本机回环访问 +
  不在冷却 / 熔断中；每次逐批确认。
- POST 前先落 ``sending`` 台账；``retry_times=1`` 不重试；每条前重新读登录态，换号或退出即停。
- Cookie 只在本模块内使用，不进日志、响应、异常 context。
"""
import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.error_policy import log_low_priority, report_high_priority
from core.exceptions import (
    BilibiliAPIError,
    CircuitBreakerError,
    CookieExpiredError,
    NetworkError,
    RateLimitError,
    Status429Error,
    TimeoutError as BiliTimeoutError,
)
from core.logger import get_logger
from core.request_budget import RequestQuotaExceeded
from core.toast import emit_toast

from . import store
from .contracts import (
    PROXY_CONSENT_VERSION,
    TIER_BOT,
    SOURCE_RULE_LLM,
    PatrolLoginRequired,
    PatrolSettings,
    StopBatch,
    astrbot_allows_proxy_report,
    is_embedded_by_astrbot,
)

logger = get_logger(__name__)

REPORT_URL = "https://api.bilibili.com/x/v2/reply/report"
METADATA_URL = "https://api.bilibili.com/x/v2/reply/report/metadata"
COOKIE_SECRET_KEY = "bilibili.cookie"
DEFAULT_CONTENT = "疑似AI自动生成评论的机器人账号，请核实"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

#: 2026-11-09 网页端评论组件内置的兜底理由表。
FALLBACK_REASONS = [
    {"group": "有害社区环境", "items": [(1, "垃圾广告"), (3, "刷屏"), (4, "引战"), (5, "剧透"), (8, "视频不相关"),
                                  (18, "违规抽奖"), (17, "青少年不良信息")]},
    {"group": "违反法律法规", "items": [(9, "违法违规"), (2, "色情"), (10, "低俗"), (12, "赌博诈骗"), (23, "违法信息外链")]},
    {"group": "谣言类不实信息", "items": [(19, "涉政谣言"), (22, "虚假不实信息"), (20, "涉社会事件谣言")]},
    {"group": "侵犯个人权益", "items": [(7, "人身攻击"), (15, "侵犯隐私")]},
    {"group": "其他", "items": [(0, "其他")]},
]

CONTINUE_CODES = {12008: "duplicate", 12006: "deleted", 12022: "deleted"}
STOP_CODES = {
    12002: "评论区已关闭",
    12077: "说明文字长度不符（需 2–200 字）",
    12009: "举报参数不合法，已停止（请反馈开发者）",
    -111: "csrf 校验失败，请重新扫码登录",
    -102: "账号已被封停",
}
HIGH_CODES = {12009, -102}
COOLDOWN_CODES = {12019, -509, -799, 429}
RISK_CODES = {-352, -412}

_REASON_CACHE: Dict[int, Tuple[float, Dict[str, Any]]] = {}
_REASON_TTL = 24 * 3600


# ============ 账号 ============

@dataclass
class Account:
    cookie: str = field(repr=False)
    bili_jct: str = field(repr=False)
    mid: int = 0
    uname: str = ""
    level: Optional[int] = None

    def __repr__(self) -> str:  # 永不打印 cookie
        return f"Account(mid={self.mid})"


def read_account_cookie() -> Optional[Tuple[str, str, int]]:
    """读取当前登录 Cookie（每次新建 ConfigManager，避免单例不刷新）。

    Returns:
        (cookie, bili_jct, DedeUserID)；未登录或字段不全返回 None。
    """
    from bilibili.auth import CookieLoginHelper
    from core.config import ConfigManager
    try:
        cookie = ConfigManager().get_secret(COOKIE_SECRET_KEY, "") or ""
    except Exception as exc:  # 读 secret 失败按未登录处理
        logger.warning("[巡检] 读取登录态失败: %s", type(exc).__name__)
        return None
    if not cookie:
        return None
    fields = CookieLoginHelper.parse_cookie_to_dict(cookie)
    bili_jct = fields.get("bili_jct") or ""
    try:
        mid = int(fields.get("DedeUserID") or 0)
    except (TypeError, ValueError):
        mid = 0
    if not bili_jct or not mid:
        return None
    return cookie, bili_jct, mid


async def resolve_account() -> Account:
    """确认当前登录账号有效，返回 Account；失败抛 PatrolLoginRequired。"""
    from bilibili.auth import CookieLoginHelper
    found = read_account_cookie()
    if not found:
        raise PatrolLoginRequired()
    cookie, bili_jct, mid = found
    try:
        info = await CookieLoginHelper.get_user_info(cookie)
    except Exception as exc:
        log_low_priority(exc, logger=logger, action="AI伪人巡检·确认登录态")
        raise PatrolLoginRequired("登录状态已失效，请重新扫码登录") from exc
    if int(info.get("uid") or 0) != mid:
        raise PatrolLoginRequired("登录状态异常，请重新扫码登录")
    return Account(cookie=cookie, bili_jct=bili_jct, mid=mid, uname=str(info.get("username") or ""),
                   level=info.get("level"))


# ============ 代发可用性 ============

def _domain_cooling() -> bool:
    try:
        from core import quota_store
        cooldown = quota_store.get_domain_cooldown("cookie")
        return bool(cooldown) and int(cooldown[0]) > int(time.time())
    except Exception as exc:  # 读不到冷却状态按“未冷却”处理，由 request() 内部再拦一次
        logger.warning("[巡检] 读取域冷却状态失败: %s", exc)
        return False


def _ip_circuit_open() -> bool:
    try:
        from core.request_budget import ip_circuit_open
        return bool(ip_circuit_open())
    except Exception as exc:
        logger.warning("[巡检] 读取 IP 熔断状态失败: %s", exc)
        return False


def proxy_report_available(s: PatrolSettings, prefs: Dict[str, Any], client_host: str) -> Tuple[bool, str]:
    """代发举报当前是否可用；不可用时给出中文原因（前端直接展示）。"""
    if not s.report_proxy_allowed:
        return False, "配置中已关闭代发举报（patrol.report.proxy_enabled）"
    if is_embedded_by_astrbot() and not astrbot_allows_proxy_report():
        return False, "AstrBot 插件配置中未开启「允许 AI 伪人巡检代发举报」，请在 AstrBot 后台的插件配置页打开后重启引擎"
    if prefs.get("proxy_consent_version") != PROXY_CONSENT_VERSION:
        return False, "尚未在「巡检设置」中同意代发举报的风险说明"
    if not prefs.get("proxy_enabled"):
        return False, "「巡检设置」中未开启“由 FishTool 代为提交”"
    import os
    listen = os.environ.get("FISHTOOL_LISTEN_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if listen not in LOOPBACK_HOSTS:
        return False, "服务未只监听本机地址，代发举报已禁用"
    if (client_host or "") not in LOOPBACK_HOSTS:
        return False, "只允许在本机发起代发举报"
    cooldown = store.get_cooldown()
    if cooldown is not None:
        minutes = max(1, int((cooldown - datetime.utcnow()).total_seconds() // 60) + 1)
        return False, f"举报冷却中，约 {minutes} 分钟后可再试"
    if _ip_circuit_open():
        return False, "B站风控熔断中，请稍后再试"
    if _domain_cooling():
        return False, "B站请求冷却中（其他功能触发了风控），请稍后再试"
    return True, ""


# ============ 理由表 ============

def _fallback_reasons() -> Dict[str, Any]:
    return {"source": "fallback", "groups": [
        {"group": g["group"], "items": [{"id": i, "name": n, "content_required": i == 0} for i, n in g["items"]]}
        for g in FALLBACK_REASONS]}


def _normalize_remote(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    groups: List[Dict[str, Any]] = []
    for g in data.get("report_groups") or []:
        if not isinstance(g, dict):
            continue
        items = []
        for it in g.get("reason_list") or g.get("reasons") or []:
            if isinstance(it, dict) and isinstance(it.get("tag_id"), int):
                items.append({"id": it["tag_id"], "name": str(it.get("tag_name") or "")[:30],
                              "content_required": bool(it.get("content_required"))})
        if items:
            groups.append({"group": str(g.get("group_name") or g.get("name") or "")[:30], "items": items})
    if not groups:
        items = [{"id": it["tag_id"], "name": str(it.get("tag_name") or "")[:30],
                  "content_required": bool(it.get("content_required"))}
                 for it in data.get("reason_list") or [] if isinstance(it, dict) and isinstance(it.get("tag_id"), int)]
        if items:
            groups.append({"group": "", "items": items})
    return {"source": "remote", "groups": groups} if groups else None


async def fetch_reasons(api: Any, oid: int) -> Dict[str, Any]:
    cached = _REASON_CACHE.get(int(oid))
    if cached and time.time() - cached[0] < _REASON_TTL:
        return cached[1]
    try:
        data = await api.get(METADATA_URL, params={"oid": int(oid), "type": 1})
        result = _normalize_remote(data) or _fallback_reasons()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log_low_priority(exc, logger=logger, action="AI伪人巡检·拉取举报理由表")
        result = _fallback_reasons()
    if result["source"] == "remote":
        _REASON_CACHE[int(oid)] = (time.time(), result)
    return result


def reason_ids(reasons: Dict[str, Any]) -> Dict[int, bool]:
    """{理由 id: 是否必须填写说明}。"""
    out: Dict[int, bool] = {}
    for g in reasons.get("groups") or []:
        for it in g.get("items") or []:
            out[int(it["id"])] = bool(it.get("content_required")) or int(it["id"]) == 0
    return out


# ============ 举报前校验 ============

def _skip(c: Dict[str, Any], code: str, reason: str) -> Dict[str, Any]:
    return {"rpid": c.get("rpid"), "mid": c.get("mid"), "uname": c.get("uname"), "reason_code": code, "reason": reason}


def validate_selection(scan: Dict[str, Any], comments: Sequence[Dict[str, Any]], requested_rpids: Sequence[int], *,
                       mode: str, s: PatrolSettings, prefs: Dict[str, Any], account_mid: Optional[int],
                       confirmed_rpids: Sequence[int] = (), acknowledge_unknown_rpids: Sequence[int] = (),
                       now: Optional[datetime] = None
                       ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """返回 (待举报, 跳过列表)。mode: native / proxy / dry_run。"""
    now = now or datetime.utcnow()
    by_rpid = {int(c["rpid"]): c for c in comments}
    confirmed = {int(r) for r in confirmed_rpids}
    ack_unknown = {int(r) for r in acknowledge_unknown_rpids}
    white = {int(m) for m in prefs.get("whitelist_mids") or [] if isinstance(m, int) or str(m).isdigit()}
    skipped: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    seen_rpids = set()
    for rpid in requested_rpids:
        rpid = int(rpid)
        if rpid in seen_rpids:
            continue
        seen_rpids.add(rpid)
        c = by_rpid.get(rpid)
        if c is None:
            skipped.append({"rpid": rpid, "reason_code": "not_in_scan", "reason": "评论不属于本次扫描"})
            continue
        candidates.append(c)

    active = store.active_report_rpids(account_mid, scan["aid"], [c["rpid"] for c in candidates])
    stage: List[Dict[str, Any]] = []
    for c in candidates:
        mid = c.get("mid")
        if mid in white or (scan.get("owner_mid") and mid == scan.get("owner_mid")) or (account_mid and mid == account_mid):
            skipped.append(_skip(c, "whitelist", "白名单 / UP 主本人 / 你自己的评论"))
            continue
        if c["rpid"] in active or c.get("report_status") in ("reported", "duplicate", "sending"):
            skipped.append(_skip(c, "already", "已举报或正在提交"))
            continue
        if c.get("report_status") == "unknown" and mode != "native" and c["rpid"] not in ack_unknown:
            skipped.append(_skip(c, "unknown_result", "上次提交结果未知，可能已到达 B站；如需重试请单独确认"))
            continue
        if mode != "native":
            needs_confirm = bool(c.get("disagreement")) or c.get("score_source") != SOURCE_RULE_LLM
            if s.report_require_ai and c.get("score_source") != SOURCE_RULE_LLM and c["rpid"] not in confirmed:
                skipped.append(_skip(c, "need_ai", "未经 AI 甄别，需人工核实后单独勾选"))
                continue
            if c.get("tier") != TIER_BOT and c["rpid"] not in confirmed:
                skipped.append(_skip(c, "not_bot", "未达到“高度疑似”档，需人工核实后单独勾选"))
                continue
            if needs_confirm and c["rpid"] not in confirmed:
                skipped.append(_skip(c, "need_confirm", "规则与 AI 结论分歧，需人工核实后单独勾选"))
                continue
        stage.append(c)

    # 同一账号只保留伪人率最高的前 N 条。
    by_mid: Dict[Any, List[Dict[str, Any]]] = {}
    for c in stage:
        by_mid.setdefault(c.get("mid"), []).append(c)
    keep: List[Dict[str, Any]] = []
    for mid, group in by_mid.items():
        group.sort(key=lambda x: (-(x.get("final_score") or 0), x["rpid"]))
        keep.extend(group[: s.report_per_account_limit])
        for extra in group[s.report_per_account_limit:]:
            skipped.append(_skip(extra, "same_account", "同一账号已选代表评论，其余跳过"))
    keep.sort(key=lambda x: -(x.get("final_score") or 0))

    if mode == "native":
        return keep, skipped

    completed_at = scan.get("completed_at_dt")
    if isinstance(completed_at, datetime) and now - completed_at > timedelta(hours=s.report_stale_block_hours):
        for c in keep:
            skipped.append(_skip(c, "stale", f"扫描结果已超过 {s.report_stale_block_hours} 小时，请重新扫描"))
        return [], skipped

    if len(keep) > s.report_max_per_task:
        for c in keep[s.report_max_per_task:]:
            skipped.append(_skip(c, "per_task_limit", f"单次最多提交 {s.report_max_per_task} 条"))
        keep = keep[: s.report_max_per_task]

    if account_mid and mode == "proxy":
        used = store.count_report_attempts(account_mid, now - timedelta(hours=24))
        remaining = max(0, s.report_daily_limit - used)
        if len(keep) > remaining:
            for c in keep[remaining:]:
                skipped.append(_skip(c, "daily_limit", f"24 小时内最多代发 {s.report_daily_limit} 条"))
            keep = keep[:remaining]
    return keep, skipped


def daily_remaining(s: PatrolSettings, account_mid: Optional[int]) -> int:
    if not account_mid:
        return s.report_daily_limit
    used = store.count_report_attempts(account_mid, datetime.utcnow() - timedelta(hours=24))
    return max(0, s.report_daily_limit - used)


# ============ 单条代发 ============

async def report_one(api: Any, account: Account, c: Dict[str, Any], oid: int, reason: int, content: str
                     ) -> Tuple[str, Optional[int], str]:
    """提交一条举报；返回 (状态, bili_code, 说明)。需要整批停止时抛 StopBatch。"""
    data: Dict[str, Any] = {"type": 1, "oid": int(oid), "rpid": int(c["rpid"]), "reason": int(reason),
                            "csrf": account.bili_jct}
    if int(reason) == 0:
        data["content"] = content
    try:
        await api.post(REPORT_URL, data=data, retry_times=1)
        return "reported", 0, ""
    except asyncio.CancelledError:
        raise
    except CookieExpiredError as exc:
        raise StopBatch("登录已失效，请重新扫码登录", bili_code=-101) from exc
    except (Status429Error, RateLimitError) as exc:
        raise StopBatch("举报过于频繁，已暂停", cooldown=True, bili_code=getattr(exc, "bili_code", None)) from exc
    except CircuitBreakerError as exc:
        raise StopBatch("请求熔断中，请稍后再试") from exc
    except RequestQuotaExceeded as exc:
        raise StopBatch("其他功能触发了风控冷却，请稍后再试") from exc
    except (NetworkError, BiliTimeoutError, asyncio.TimeoutError) as exc:
        log_low_priority(exc, logger=logger, action="AI伪人巡检·举报网络超时", context={"rpid": c.get("rpid")})
        return "unknown", None, "网络超时，可能已提交"
    except BilibiliAPIError as exc:
        code = getattr(exc, "bili_code", None)
        msg = str(exc)[:200]
        if code in CONTINUE_CODES:
            return CONTINUE_CODES[code], code, msg
        if code in COOLDOWN_CODES:
            raise StopBatch("举报过于频繁，已暂停", cooldown=True, bili_code=code) from exc
        if code in RISK_CODES:
            raise StopBatch("触发 B站风控，FishTool 的采集功能也会暂停至少 15 分钟", cooldown=True,
                            bili_code=code) from exc
        if code in STOP_CODES:
            raise StopBatch(STOP_CODES[code], high=code in HIGH_CODES, bili_code=code) from exc
        return "failed", code, msg


# ============ 批量代发 ============

ProgressCallback = Optional[Callable[[str, int, str], None]]


async def run_proxy_batch(api: Any, account: Account, scan: Dict[str, Any], items: Sequence[Dict[str, Any]], *,
                          reason: int, content: str, request_key: str, s: PatrolSettings, dry_run: bool,
                          progress: ProgressCallback = None, is_cancelled: Optional[Callable[[], bool]] = None,
                          sleep: Callable[[float], Any] = asyncio.sleep) -> Dict[str, int]:
    """逐条代发（或演练）；返回各状态计数。"""
    counts: Dict[str, int] = {}
    mode = "dry_run" if dry_run else "proxy"

    def bump(status: str) -> None:
        counts[status] = counts.get(status, 0) + 1

    def base_row(c: Dict[str, Any]) -> Dict[str, Any]:
        return {"request_key": request_key, "mode": mode, "reporter_mid": account.mid, "scan_id": scan["scan_id"],
                "oid": scan["aid"], "rpid": c["rpid"], "target_mid": c.get("mid"), "target_uname": c.get("uname"),
                "reason": reason, "content": content if int(reason) == 0 else None,
                "final_score": c.get("final_score"), "tier": c.get("tier")}

    def close_rest(rest: Sequence[Dict[str, Any]], status: str, note: str) -> None:
        for c in rest:
            store.insert_report_simple({**base_row(c), "bili_message": note}, status)
            store.set_comment_report_status(scan["scan_id"], c["rpid"], status if status != "cancelled" else "none")
            bump(status)

    total = len(items)
    stop_error: Optional[StopBatch] = None
    for idx, c in enumerate(items):
        if is_cancelled and is_cancelled():
            close_rest(items[idx:], "cancelled", "用户取消")
            break
        current = read_account_cookie()
        if current is None or current[2] != account.mid:
            stop_error = StopBatch("当前登录账号已变化，已停止举报")
            close_rest(items[idx:], "cancelled", stop_error.message)
            break
        if dry_run:
            store.insert_report_simple(base_row(c), "dry_run")
            store.set_comment_report_status(scan["scan_id"], c["rpid"], "dry_run")
            bump("dry_run")
        else:
            report_id = store.insert_report_sending(base_row(c))
            if not report_id:
                bump("skipped")
                continue
            store.set_comment_report_status(scan["scan_id"], c["rpid"], "sending")
            try:
                status, code, msg = await report_one(api, account, c, scan["aid"], reason, content)
            except StopBatch as exc:
                store.finalize_report(report_id, "failed", exc.bili_code, exc.message)
                store.set_comment_report_status(scan["scan_id"], c["rpid"], "failed")
                bump("failed")
                stop_error = exc
                close_rest(items[idx + 1:], "skipped_rate_limited" if exc.cooldown else "cancelled", exc.message)
                break
            store.finalize_report(report_id, status, code, msg)
            store.set_comment_report_status(scan["scan_id"], c["rpid"], status)
            bump(status)
        if progress:
            progress("reporting", 5 + int(90 * (idx + 1) / max(1, total)), f"已处理 {idx + 1}/{total} 条")
        if idx < total - 1 and not dry_run:
            await sleep(random.uniform(*s.report_interval_seconds))

    if stop_error is not None:
        if stop_error.cooldown:
            store.set_cooldown(datetime.utcnow() + timedelta(minutes=s.report_cooldown_minutes))
        if stop_error.high:
            report_high_priority(stop_error, logger=logger, action="AI伪人巡检·代发举报",
                                 toast_title="举报已停止", toast_message=stop_error.message, source="lottery_patrol")
        else:
            emit_toast("举报已停止", stop_error.message, level="warning", source="lottery_patrol",
                       error_code=stop_error.code)

    store.write_operation_log("report_batch", {
        "scan_id": scan["scan_id"], "mode": mode, "total": total, "reason": reason, **counts,
        "stopped": stop_error.message if stop_error else "",
    }, "stopped" if stop_error else "completed")
    return {"counts": counts, "stopped": stop_error.message if stop_error else "",
            "cooldown": bool(stop_error and stop_error.cooldown)}


# ============ 原生举报 ============

def native_links(scan: Dict[str, Any], items: Sequence[Dict[str, Any]], *, request_key: str,
                 reporter_mid: int) -> List[Dict[str, Any]]:
    """生成评论定位链接并记 native_opened 台账。"""
    links = []
    for c in items:
        report_id = store.insert_report_simple({
            "request_key": request_key, "mode": "native", "reporter_mid": reporter_mid or 0,
            "scan_id": scan["scan_id"], "oid": scan["aid"], "rpid": c["rpid"], "target_mid": c.get("mid"),
            "target_uname": c.get("uname"), "final_score": c.get("final_score"), "tier": c.get("tier"),
        }, "native_opened")
        store.set_comment_report_status(scan["scan_id"], c["rpid"], "native_opened")
        links.append({"rpid": c["rpid"], "report_id": report_id, "uname": c.get("uname") or "",
                      "final_score": c.get("final_score"),
                      "url": f"https://www.bilibili.com/video/{scan['bvid']}#reply{int(c['rpid'])}"})
    return links
