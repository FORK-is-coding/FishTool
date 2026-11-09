"""AI 伪人巡检编排服务：preflight / 扫描 / AI 甄别 / 举报。

路由只负责参数校验、任务注册与错误码翻译；业务流程都在这里。
每个任务用当前登录账号的固定 Cookie 新建一个 BilibiliAPI（不走 Cookie 池轮换），结束即关闭。
"""
import math
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from core.error_policy import log_low_priority
from core.logger import get_logger

from . import judge as judge_mod
from . import reporter, store
from .collector import PatrolCollector, ScanCancelled
from .contracts import (
    AI_CONSENT_VERSION,
    PROXY_CONSENT_VERSION,
    RISK_NOTICE_VERSION,
    RULES_VERSION,
    SOURCE_WHITELIST,
    TIERS,
    PatrolAccountChanged,
    PatrolConsentRequired,
    PatrolLoginRequired,
    PatrolReportUnavailable,
    PatrolSettings,
    load_settings,
)
from .fusion import rescore
from .rules import apply_cross_features, compile_rules, score_comment

logger = get_logger(__name__)

ProgressCallback = Callable[[str, int, str], None]
CancelCheck = Callable[[], bool]


def _comment_rate() -> float:
    from core.config import ConfigManager
    try:
        return max(0.5, float(ConfigManager().get("bilibili.rate_limit.comment", 4.0)))
    except (TypeError, ValueError):
        return 4.0


def default_api_factory(cookie: str) -> Any:
    """用固定 Cookie + 评论限频构造 API 客户端。"""
    from bilibili.api import BilibiliAPI
    from bilibili.rate_limiter import RateLimiter
    return BilibiliAPI(cookie=cookie, rate_limiter=RateLimiter(rate=_comment_rate()))


def select_for_judge(items: Sequence[Dict[str, Any]], scope: str, s: PatrolSettings,
                     rpids: Optional[Sequence[int]] = None) -> List[Dict[str, Any]]:
    """AI 甄别的选范围（扫描统计与甄别任务共用）。"""
    pool = [c for c in items if c.get("score_source") != SOURCE_WHITELIST]
    if scope == "selected":
        wanted = {int(r) for r in rpids or []}
        chosen = [c for c in pool if int(c["rpid"]) in wanted]
    elif scope == "all":
        chosen = list(pool)
    elif scope == "suspect":
        chosen = [c for c in pool if (c.get("rule_score") or 0) >= s.gray_low]
    else:  # gray
        chosen = [c for c in pool if s.gray_low <= (c.get("rule_score") or 0) <= s.gray_high
                  or ((c.get("rule_score") or 0) > s.gray_high and len(c.get("hard_groups") or []) < 2)]
    chosen.sort(key=lambda c: -(c.get("final_score") or 0))
    return chosen


def _tier_counts(items: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts = {t: 0 for t in TIERS}
    for c in items:
        counts[c.get("tier") or "human"] = counts.get(c.get("tier") or "human", 0) + 1
    return counts


def _top_rules(items: Sequence[Dict[str, Any]], n: int = 5) -> List[Dict[str, Any]]:
    counter: Counter = Counter()
    labels: Dict[str, str] = {}
    for c in items:
        for h in c.get("rule_hits") or c.get("hits") or []:
            if int(h.get("weight") or 0) > 0:
                counter[h.get("rule_id")] += 1
                labels[h.get("rule_id")] = h.get("label") or h.get("rule_id")
    return [{"rule_id": rid, "label": labels[rid], "count": cnt} for rid, cnt in counter.most_common(n)]


def _model_ok(prefs: Dict[str, Any]) -> bool:
    identity = judge_mod.model_identity()
    return prefs.get("ai_consent_version") == AI_CONSENT_VERSION and prefs.get("ai_model_identity") == identity


class PatrolService:
    """巡检业务编排。"""

    def __init__(self, api_factory: Optional[Callable[[str], Any]] = None):
        self._api_factory = api_factory or default_api_factory

    # ---------- preflight ----------

    async def preflight(self, target: Optional[str], client_host: str = "") -> Dict[str, Any]:
        s = load_settings()
        prefs = store.load_preferences()
        login: Dict[str, Any] = {"ok": False, "uname": "", "level": None, "mid": None}
        found = reporter.read_account_cookie()
        video = None
        error = ""
        if found:
            cookie, _jct, mid = found
            login["mid"] = mid
            try:
                from bilibili.auth import CookieLoginHelper
                info = await CookieLoginHelper.get_user_info(cookie)
                login.update(ok=True, uname=str(info.get("username") or ""), level=info.get("level"))
            except Exception as exc:  # 登录态校验失败只影响展示
                log_low_priority(exc, logger=logger, action="AI伪人巡检·preflight 登录态")
            if target and login["ok"]:
                api = self._api_factory(cookie)
                try:
                    meta = await PatrolCollector(api, s).fetch_video_meta(target)
                    video = meta.to_dict()
                except ValueError as exc:
                    error = str(exc)
                except Exception as exc:  # 网络 / 风控错误只给提示
                    log_low_priority(exc, logger=logger, action="AI伪人巡检·preflight 视频信息")
                    error = "获取视频信息失败，请稍后重试"
                finally:
                    await _close(api)
        rate = _comment_rate()
        reply_count = (video or {}).get("reply_count") or s.max_comments_default
        pages = math.ceil(min(reply_count, s.max_comments_default) / 20)
        reply_reqs = min(s.reply_requests_hard, s.reply_floors * s.reply_pages_per_floor)
        ok, why = reporter.proxy_report_available(s, prefs, client_host)
        cooldown = store.get_cooldown()
        identity = judge_mod.model_identity()
        from llm.client import is_llm_available
        return {
            "login": login,
            "video": video,
            "video_error": error,
            "eta": {"without_replies": int(pages * rate), "with_replies": int((pages + reply_reqs) * rate)},
            "llm": {"configured": bool(is_llm_available()), "model": identity["model"],
                    "api_base": identity["api_base"], "consent_ok": _model_ok(prefs)},
            "report": {
                "default_mode": s.report_default_mode, "proxy_available": ok, "proxy_unavailable_reason": why,
                "daily_limit": s.report_daily_limit, "daily_remaining": reporter.daily_remaining(s, login["mid"]),
                "max_per_task": s.report_max_per_task, "per_account_limit": s.report_per_account_limit,
                "cooldown_seconds": int((cooldown - datetime.now()).total_seconds()) if cooldown else 0,
                "proxy_consent_ok": prefs.get("proxy_consent_version") == PROXY_CONSENT_VERSION,
                "proxy_enabled": bool(prefs.get("proxy_enabled")), "dry_run": s.report_dry_run,
                "require_ai": s.report_require_ai, "stale_warn_hours": s.report_stale_warn_hours,
            },
            "prefs": {"include_replies": bool(prefs.get("include_replies", True)),
                      "risk_notice_ok": prefs.get("risk_notice_version") == RISK_NOTICE_VERSION,
                      "default_report_content": prefs.get("default_report_content") or "",
                      "revision": prefs.get("revision", 0)},
            "settings": {"max_comments_default": s.max_comments_default, "max_comments_hard": s.max_comments_hard,
                         "judge_max_items": s.judge_max_items, "judge_batch_size": s.judge_batch_size,
                         "rules_version": RULES_VERSION, "enabled": s.enabled},
        }

    # ---------- 扫描 ----------

    async def run_scan(self, task_id: str, req: Dict[str, Any], *, progress: ProgressCallback,
                       is_cancelled: CancelCheck, on_scan_created: Callable[[int], None],
                       resume_cursor: Optional[str] = None) -> Dict[str, Any]:
        s = load_settings()
        found = reporter.read_account_cookie()
        if not found:
            raise PatrolLoginRequired()
        cookie, _jct, scanner_mid = found
        prefs = store.load_preferences()
        api = self._api_factory(cookie)
        scan_id: Optional[int] = None
        try:
            progress("preparing", 3, "正在读取视频信息")
            collector = PatrolCollector(api, s)
            meta = await collector.fetch_video_meta(req["target"])
            options = {"max_comments": int(req.get("max_comments") or s.max_comments_default),
                       "include_replies": bool(req.get("include_replies", True)), "mode": req.get("mode", "time")}
            scan_id = store.create_scan(task_id, meta.to_dict(), scanner_mid, options, RULES_VERSION)
            on_scan_created(scan_id)
            try:
                store.save_preferences({"include_replies": options["include_replies"]}, None)
            except Exception as exc:  # 记住开关失败不影响扫描
                log_low_priority(exc, logger=logger, action="AI伪人巡检·记住楼中楼开关")

            result = await collector.collect(
                meta, max_comments=options["max_comments"], include_replies=options["include_replies"],
                mode=options["mode"], progress=progress, is_cancelled=is_cancelled,
                checkpoint=lambda cp: store.task_checkpoint(task_id, {**cp, "scan_id": scan_id}),
                resume_cursor=resume_cursor)

            progress("rule_patrol", 88, f"正在规则巡检 {len(result.comments)} 条评论")
            compiled = compile_rules(prefs.get("user_keywords"))
            whitelist = set(int(m) for m in prefs.get("whitelist_mids") or [] if str(m).isdigit())
            items = []
            for c in result.comments:
                scored = score_comment(c, compiled, s, whitelist=whitelist, owner_mid=meta.owner_mid,
                                       self_mid=scanner_mid)
                items.append({**c, **scored})
            apply_cross_features(items, s)
            for it in items:
                it["rule_hits"] = it.pop("hits", [])
                it["ai_score"] = None
                it["ai_reason"] = ""
                it.update(rescore(it, s))
            store.upsert_comments(scan_id, items)

            stats = {
                "top_level": sum(1 for c in items if not c.get("is_reply")),
                "replies": sum(1 for c in items if c.get("is_reply")),
                "tiers": _tier_counts(items), "judged": 0, "reported": 0,
                "top_rules": _top_rules(items),
                "judge_candidates": {k: len(select_for_judge(items, k, s)) for k in ("gray", "suspect", "all")},
                "pages": result.pages, "failed_pages": result.failed_pages, "reply_requests": result.reply_requests,
                "parse_failed": result.parse_failed,
            }
            store.finish_scan(scan_id, "completed", partial=result.partial, partial_reason=result.partial_reason,
                              stats=stats)
            progress("done", 99, "扫描完成")
            return {"scan_id": scan_id, "video": meta.to_dict(), "stats": stats, "partial": result.partial,
                    "partial_reason": result.partial_reason, "rules_version": RULES_VERSION}
        except ScanCancelled:
            if scan_id is not None:
                store.finish_scan(scan_id, "cancelled")
            raise
        except BaseException:
            if scan_id is not None:
                store.finish_scan(scan_id, "failed")
            raise
        finally:
            await _close(api)

    # ---------- AI 甄别 ----------

    async def run_judge(self, task_id: str, scan_id: int, req: Dict[str, Any], *, progress: ProgressCallback,
                        is_cancelled: CancelCheck) -> Dict[str, Any]:
        s = load_settings()
        prefs = store.load_preferences()
        if not _model_ok(prefs):
            raise PatrolConsentRequired()
        scan = store.get_scan(scan_id)
        if scan is None:
            raise ValueError("扫描记录不存在，请重新扫描")
        items = store.all_comments(scan_id)
        by_rpid = {c["rpid"]: c for c in items}
        limit = min(int(req.get("limit") or s.judge_max_items), s.judge_max_items_hard)
        chosen = select_for_judge(items, req.get("scope", "gray"), s, req.get("rpids"))[:limit]
        if not chosen:
            return {"judged": 0, "from_cache": 0, "failed_batches": 0, "partial": False, "partial_reason": "",
                    "selected": 0, "tiers": _tier_counts(items)}

        def apply(batch: Dict[int, Dict[str, Any]]) -> None:
            updates = []
            for rpid, value in batch.items():
                c = by_rpid.get(rpid)
                if c is None:
                    continue
                c["ai_score"] = value.get("ai_score")
                c["ai_reason"] = value.get("reason") or ""
                c.update(rescore(c, s, ai_judged_null=value.get("ai_score") is None))
                updates.append({"rpid": rpid, "ai_score": c["ai_score"], "ai_reason": c["ai_reason"],
                                **{k: c[k] for k in ("final_score", "tier", "score_source", "disagreement")}})
            store.update_scores(scan_id, updates)

        progress("ai_judging", 3, f"准备甄别 {len(chosen)} 条评论")
        result = await judge_mod.judge(chosen, by_rpid, focus=req.get("focus") or "", s=s, progress=progress,
                                       on_batch=apply, is_cancelled=is_cancelled)
        tiers = _tier_counts(by_rpid.values())
        judged_total = sum(1 for c in by_rpid.values() if c.get("ai_score") is not None)
        store.update_scan_stats(scan_id, {"tiers": tiers, "judged": judged_total})
        return {**result.to_dict(), "selected": len(chosen), "tiers": tiers}

    # ---------- 举报 ----------

    async def report_native(self, scan_id: int, req: Dict[str, Any]) -> Dict[str, Any]:
        s = load_settings()
        scan = store.get_scan(scan_id)
        if scan is None:
            raise ValueError("扫描记录不存在，请重新扫描")
        found = reporter.read_account_cookie()
        account_mid = found[2] if found else None
        comments = store.get_comments_by_rpids(scan_id, req["rpids"])
        keep, skipped = reporter.validate_selection(scan, comments, req["rpids"], mode="native", s=s,
                                                    prefs=store.load_preferences(), account_mid=account_mid)
        links = reporter.native_links(scan, keep, request_key=req["request_key"], reporter_mid=account_mid or 0)
        return {"links": links, "skipped": skipped}

    async def run_report(self, task_id: str, scan_id: int, req: Dict[str, Any], *, client_host: str,
                         progress: ProgressCallback, is_cancelled: CancelCheck) -> Dict[str, Any]:
        s = load_settings()
        prefs = store.load_preferences()
        ok, why = reporter.proxy_report_available(s, prefs, client_host)
        if not ok:
            raise PatrolReportUnavailable(why)
        scan = store.get_scan(scan_id)
        if scan is None:
            raise ValueError("扫描记录不存在，请重新扫描")
        account = await reporter.resolve_account()
        if scan.get("scanner_mid") and account.mid != scan["scanner_mid"] and not req.get("accept_account_change"):
            raise PatrolAccountChanged("当前登录账号与扫描时不同", scanner_mid=scan["scanner_mid"],
                                       current_uname=account.uname)
        dry_run = req.get("mode") == "dry_run" or s.report_dry_run
        api = self._api_factory(account.cookie)
        try:
            reasons = await reporter.fetch_reasons(api, scan["aid"])
            allowed = reporter.reason_ids(reasons)
            reason = req.get("reason")
            if reason is None or int(reason) not in allowed:
                raise ValueError("请选择有效的举报理由")
            content = (req.get("content") or "").strip()
            if allowed[int(reason)] and not 2 <= len(content) <= 200:
                raise ValueError("举报说明需为 2–200 字")
            comments = store.get_comments_by_rpids(scan_id, req["rpids"])
            keep, skipped = reporter.validate_selection(
                scan, comments, req["rpids"], mode="dry_run" if dry_run else "proxy", s=s, prefs=prefs,
                account_mid=account.mid, confirmed_rpids=req.get("confirmed_rpids") or [])
            for item in skipped:
                if item.get("reason_code") == "not_in_scan":
                    continue
                store.insert_report_simple({
                    "request_key": req["request_key"], "mode": "dry_run" if dry_run else "proxy",
                    "reporter_mid": account.mid, "scan_id": scan_id, "oid": scan["aid"], "rpid": item["rpid"],
                    "target_mid": item.get("mid"), "target_uname": item.get("uname"),
                    "bili_message": item.get("reason"),
                }, "skipped")
            progress("reporting", 3, f"准备提交 {len(keep)} 条")
            out = await reporter.run_proxy_batch(
                api, account, scan, keep, reason=int(reason), content=content, request_key=req["request_key"], s=s,
                dry_run=dry_run, progress=progress, is_cancelled=is_cancelled)
            reported = sum(1 for c in store.all_comments(scan_id) if c.get("report_status") in ("reported", "duplicate"))
            store.update_scan_stats(scan_id, {"reported": reported})
            return {**out, "skipped": skipped, "dry_run": dry_run, "submitted": len(keep)}
        finally:
            await _close(api)

    # ---------- 启动 ----------

    def startup(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        try:
            s = load_settings()
            out["recover"] = store.recover_on_startup(s.retention_scans_days)
            out["pruned"] = store.prune(s)
        except Exception as exc:  # 启动收尾失败不阻断应用
            log_low_priority(exc, logger=logger, action="AI伪人巡检·启动收尾")
        return out


async def _close(api: Any) -> None:
    close = getattr(api, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception as exc:  # 关闭会话失败不影响结果
        log_low_priority(exc, logger=logger, action="AI伪人巡检·关闭会话")
