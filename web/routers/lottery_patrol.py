"""抽奖工具 · AI 伪人巡检路由（prefix=/lottery/patrol，挂在 /api 下）。

- 写接口全部 require_local_write（Host 回环 + 同源 + X-Local-Token），读接口 require_local_read。
  token 复用 /api/comment/semantic/local-token（进程内同一个 token，AstrBot 代理已处理）。
- patrol.enabled=false 时全部返回 404。
- 路由只做参数校验、任务注册与错误码翻译，业务流程在 modules/lottery/patrol/service.py。
"""
import asyncio
import csv
import hashlib
import io
import json
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from core.error_policy import report_exception
from core.exceptions import (
    BilibiliAPIError,
    BiliOpsException,
    CircuitBreakerError,
    CookieExpiredError,
    LLMNotConfiguredError,
)
from core.logger import get_logger
from core.request_budget import RequestBudgetExceeded
from modules.lottery.patrol import judge as judge_mod
from modules.lottery.patrol import reporter, store
from modules.lottery.patrol.collector import ScanCancelled
from modules.lottery.patrol.contracts import (
    AI_CONSENT_VERSION,
    PROXY_CONSENT_VERSION,
    RISK_NOTICE_VERSION,
    PatrolAccountChanged,
    PatrolBusy,
    PatrolConsentRequired,
    PatrolLoginRequired,
    PatrolReportUnavailable,
    PreferenceConflict,
    load_settings,
)
from modules.lottery.patrol.service import PatrolService
from web.local_guard import require_local_read, require_local_write

from .lottery_patrol_tasks import registry

logger = get_logger(__name__)


async def require_patrol_enabled() -> None:
    if not load_settings().enabled:
        raise HTTPException(status_code=404, detail="not_found")


router = APIRouter(prefix="/lottery/patrol", tags=["lottery-patrol"], dependencies=[Depends(require_patrol_enabled)])
_READ = [Depends(require_local_read)]
_WRITE = [Depends(require_local_write)]

_service: Optional[PatrolService] = None


def get_service() -> PatrolService:
    global _service
    if _service is None:
        _service = PatrolService()
    return _service


def set_service(svc: Optional[PatrolService]) -> None:
    global _service
    _service = svc


# ============ 请求模型 ============

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScanRequest(_Strict):
    target: str = Field(min_length=3, max_length=500)
    max_comments: StrictInt = Field(default=500, ge=1, le=2000)
    include_replies: StrictBool = True
    mode: Literal["time", "hot"] = "time"


class JudgeRequest(_Strict):
    scope: Literal["gray", "suspect", "all", "selected"] = "gray"
    rpids: List[StrictInt] = Field(default_factory=list, max_length=500)
    limit: Optional[StrictInt] = Field(default=None, ge=1, le=500)
    focus: str = Field(default="", max_length=600)
    request_key: str = Field(min_length=8, max_length=64)


class ReportRequest(_Strict):
    rpids: List[StrictInt] = Field(min_length=1, max_length=50)
    mode: Literal["native", "proxy", "dry_run"] = "native"
    reason: Optional[StrictInt] = Field(default=None, ge=0, le=99)
    content: Optional[str] = Field(default=None, max_length=200)
    confirmed_rpids: List[StrictInt] = Field(default_factory=list, max_length=50)
    acknowledge_unknown_rpids: List[StrictInt] = Field(default_factory=list, max_length=50)
    accept_account_change: StrictBool = False
    request_key: str = Field(min_length=8, max_length=64)


class MarkRequest(_Strict):
    done: StrictBool


class UserKeyword(_Strict):
    word: str = Field(min_length=1, max_length=20)
    field: Literal["name", "sign", "text"]
    weight: StrictInt = Field(ge=-30, le=30)


class PreferencesPatch(_Strict):
    expected_revision: StrictInt
    ai_consent: Optional[Literal["agree"]] = None
    risk_notice: Optional[Literal["agree"]] = None
    proxy_consent: Optional[Literal["agree"]] = None
    proxy_enabled: Optional[StrictBool] = None
    include_replies: Optional[StrictBool] = None
    whitelist_mids: Optional[List[StrictInt]] = Field(default=None, max_length=200)
    user_keywords: Optional[List[UserKeyword]] = Field(default=None, max_length=100)
    default_report_content: Optional[str] = Field(default=None, max_length=200)


# ============ 错误翻译 ============

def _http(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"error_code": code, "message": message, **extra})


def _translate(exc: BaseException) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, ValueError):
        return _http(400, "bad_request", str(exc))
    if isinstance(exc, (PatrolLoginRequired, CookieExpiredError)):
        return _http(409, "login_required", getattr(exc, "message", "") or "请先扫码登录 B站")
    if isinstance(exc, PatrolConsentRequired):
        return _http(409, "consent_required", exc.message)
    if isinstance(exc, LLMNotConfiguredError):
        return _http(409, "llm_not_configured", "请先在配置页填写 AI 的 API Key")
    if isinstance(exc, PatrolAccountChanged):
        return _http(409, "account_changed", exc.message, current_uname=exc.current_uname)
    if isinstance(exc, PatrolReportUnavailable):
        return _http(403, "proxy_unavailable", exc.message)
    if isinstance(exc, PreferenceConflict):
        return _http(409, "revision_conflict", exc.message)
    if isinstance(exc, PatrolBusy):
        return _http(409, exc.code, exc.message)
    if isinstance(exc, (RequestBudgetExceeded, CircuitBreakerError)):
        return _http(503, "risk_cooling", "B站请求冷却中，请稍后再试")
    if isinstance(exc, BilibiliAPIError):
        return _http(502, "bilibili_error", "B站接口请求失败，请稍后重试")
    return _http(500, "internal_error", "服务内部错误，请查看日志")


def _task_message(exc: BaseException) -> str:
    err = _translate(exc)
    detail = err.detail if isinstance(err.detail, dict) else {}
    return str(detail.get("message") or "任务执行失败，请查看日志")


async def _guard(coro: Any) -> Any:
    try:
        return await coro
    except HTTPException:
        raise
    except Exception as exc:
        if isinstance(exc, BiliOpsException) and not isinstance(exc, ValueError):
            logger.warning("[巡检] 请求失败: %s", type(exc).__name__)
        elif not isinstance(exc, ValueError):
            logger.exception("[巡检] 请求异常")
        raise _translate(exc) from exc


# ============ 后台任务 ============

async def _run_task(task_id: str, action: str, factory: Any) -> None:
    try:
        result = await factory()
        registry.complete(task_id, result)
    except asyncio.CancelledError:
        registry.mark_cancelled(task_id)
        raise
    except ScanCancelled:
        registry.mark_cancelled(task_id)
    except BiliOpsException as exc:
        report_exception(exc, logger=logger, action=f"AI伪人巡检·{action}", source="lottery_patrol",
                         toast_message=_task_message(exc))
        registry.fail(task_id, _task_message(exc))
    except ValueError as exc:
        registry.fail(task_id, str(exc))
    except Exception as exc:
        logger.exception("[巡检] %s 任务异常: %s", action, task_id)
        registry.fail(task_id, "任务执行失败，请查看日志")


def _progress_cb(task_id: str):
    return lambda stage, pct, msg: registry.progress(task_id, stage, pct, msg)


def _cancel_cb(task_id: str):
    return lambda: registry.is_cancelled(task_id)


def _fingerprint(kind: str, payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([kind, payload], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _start_scan(req: Dict[str, Any], *, task_id: Optional[str] = None, resume_cursor: Optional[str] = None,
                resume_scan_id: Optional[int] = None) -> str:
    tid = registry.create("scan", req, fingerprint=_fingerprint("scan", req), task_id=task_id)
    svc = get_service()
    registry.spawn(tid, _run_task(tid, "扫描", lambda: svc.run_scan(
        tid, req, progress=_progress_cb(tid), is_cancelled=_cancel_cb(tid),
        on_scan_created=lambda sid: registry.set_scan(tid, sid), resume_cursor=resume_cursor,
        resume_scan_id=resume_scan_id)))
    return tid


def _start_judge(scan_id: int, req: Dict[str, Any], *, task_id: Optional[str] = None) -> str:
    tid = registry.create("judge", req, fingerprint=_fingerprint("judge", {**req, "scan_id": scan_id}),
                          scan_id=scan_id, task_id=task_id)
    svc = get_service()
    registry.spawn(tid, _run_task(tid, "AI甄别", lambda: svc.run_judge(
        tid, scan_id, req, progress=_progress_cb(tid), is_cancelled=_cancel_cb(tid))))
    return tid


def _require_scan(scan_id: int) -> Dict[str, Any]:
    scan = store.get_scan(scan_id)
    if scan is None:
        raise _http(404, "scan_not_found", "扫描记录不存在，请重新扫描")
    return scan


def _scanning(scan_id: int) -> bool:
    scan_task = registry.running_of("scan")
    return bool(scan_task) and scan_task in registry.running_for_scan(scan_id)


def _client_host(request: Request) -> str:
    return request.client.host if request.client else ""


# ============ 路由 ============

@router.get("/preflight", dependencies=_READ)
async def preflight(request: Request, target: str = Query(default="", max_length=500)) -> Dict[str, Any]:
    data = await _guard(get_service().preflight(target.strip() or None, client_host=_client_host(request)))
    data["active_tasks"] = {k: registry.running_of(k) for k in ("scan", "judge", "report")}
    return {"success": True, "data": data}


@router.post("/scans", status_code=202, dependencies=_WRITE)
async def start_scan(req: ScanRequest) -> Dict[str, Any]:
    payload = req.model_dump(mode="json")
    existing = registry.find_by_fingerprint(_fingerprint("scan", payload))
    if existing:
        return {"success": True, "task_id": existing, "reused": True}
    if registry.running_of("scan"):
        raise _http(409, "scan_running", "已有扫描任务在运行，请等待完成或取消后再试")
    return {"success": True, "task_id": _start_scan(payload), "reused": False}


@router.get("/scans/{scan_id}", dependencies=_READ)
async def get_scan(scan_id: int) -> Dict[str, Any]:
    scan = _require_scan(scan_id)
    scan.pop("completed_at_dt", None)
    return {"success": True, "data": scan}


@router.get("/scans/{scan_id}/comments", dependencies=_READ)
async def list_comments(
    scan_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    tier: Literal["", "bot", "suspect", "human", "unknown", "whitelist"] = "",
    min_score: Optional[int] = Query(default=None, ge=0, le=100),
    max_score: Optional[int] = Query(default=None, ge=0, le=100),
    focus_only: bool = False,
    disagreement_only: bool = False,
    report_status: str = Query(default="", max_length=24),
    keyword: str = Query(default="", max_length=50),
    sort: Literal["score_desc", "time_desc", "like_desc"] = "score_desc",
) -> Dict[str, Any]:
    _require_scan(scan_id)
    data = store.list_comments(scan_id, page=page, page_size=page_size, tier=tier, min_score=min_score,
                               max_score=max_score, focus_only=focus_only, disagreement_only=disagreement_only,
                               report_status=report_status, keyword=keyword.strip(), sort=sort)
    return {"success": True, "data": data}


@router.post("/scans/{scan_id}/judge", status_code=202, dependencies=_WRITE)
async def start_judge(scan_id: int, req: JudgeRequest) -> Dict[str, Any]:
    _require_scan(scan_id)
    if _scanning(scan_id):
        raise _http(409, "scan_running", "该视频仍在扫描中，请完成后再甄别")
    payload = req.model_dump(mode="json")
    existing = registry.find_by_fingerprint(_fingerprint("judge", {**payload, "scan_id": scan_id}))
    if existing:
        return {"success": True, "task_id": existing, "reused": True}
    if registry.running_of("judge"):
        raise _http(409, "judge_running", "已有 AI 甄别任务在运行")
    from llm.client import is_llm_available
    if not is_llm_available():
        raise _http(409, "llm_not_configured", "请先在配置页填写 AI 的 API Key")
    prefs = store.load_preferences()
    if prefs.get("ai_consent_version") != AI_CONSENT_VERSION or prefs.get("ai_model_identity") != judge_mod.model_identity():
        raise _http(409, "consent_required", "请先阅读并同意 AI 甄别的外发说明")
    return {"success": True, "task_id": _start_judge(scan_id, payload), "reused": False}


@router.get("/report/reasons", dependencies=_READ)
async def report_reasons(scan_id: int = Query(ge=1)) -> Dict[str, Any]:
    scan = _require_scan(scan_id)
    found = reporter.read_account_cookie()
    if not found:
        return {"success": True, "data": reporter._fallback_reasons()}
    api = get_service()._api_factory(found[0])
    try:
        data = await reporter.fetch_reasons(api, scan["aid"])
    finally:
        close = getattr(api, "close", None)
        if close is not None:
            await close()
    return {"success": True, "data": data}


@router.post("/scans/{scan_id}/report", dependencies=_WRITE)
async def start_report(scan_id: int, req: ReportRequest, request: Request):
    _require_scan(scan_id)
    if _scanning(scan_id):
        raise _http(409, "scan_running", "该视频仍在扫描中")
    payload = req.model_dump(mode="json")
    if req.mode == "native":
        data = await _guard(get_service().report_native(scan_id, payload))
        return {"success": True, "data": data}
    if registry.running_of("report"):
        raise _http(409, "report_running", "已有举报任务在运行")
    svc = get_service()
    # 可用性先同步检查一次，不可用时直接 403，避免建出必然失败的任务。
    ok, why = reporter.proxy_report_available(load_settings(), store.load_preferences(), _client_host(request))
    if not ok:
        raise _http(403, "proxy_unavailable", why)
    scan = store.get_scan(scan_id) or {}
    found = reporter.read_account_cookie()
    if found and scan.get("scanner_mid") and found[2] != scan["scanner_mid"] and not req.accept_account_change:
        raise _http(409, "account_changed", "当前登录账号与扫描时不同")
    client_host = _client_host(request)
    tid = registry.create("report", payload, scan_id=scan_id)
    registry.spawn(tid, _run_task(tid, "举报", lambda: svc.run_report(
        tid, scan_id, payload, client_host=client_host, progress=_progress_cb(tid), is_cancelled=_cancel_cb(tid))))
    return JSONResponse(status_code=202, content={"success": True, "task_id": tid, "reused": False})


@router.post("/reports/{report_id}/mark", dependencies=_WRITE)
async def mark_report(report_id: int, req: MarkRequest) -> Dict[str, Any]:
    row = store.mark_native(report_id, req.done)
    if row is None:
        raise _http(404, "report_not_found", "台账记录不存在或不是原生举报")
    return {"success": True, "data": row}


@router.get("/reports", dependencies=_READ)
async def list_reports(days: int = Query(default=30, ge=1, le=180)) -> Dict[str, Any]:
    return {"success": True, "data": store.list_reports(days)}


@router.get("/tasks/{task_id}", dependencies=_READ)
async def get_task(task_id: str) -> Dict[str, Any]:
    task = registry.public(task_id)
    if task is None:
        raise _http(404, "task_not_found", "任务不存在或服务已重启")
    return {"success": True, "data": task}


@router.post("/tasks/{task_id}/cancel", dependencies=_WRITE)
async def cancel_task(task_id: str) -> Dict[str, Any]:
    try:
        status = registry.request_cancel(task_id)
    except KeyError:
        raise _http(404, "task_not_found", "任务不存在或服务已重启")
    except ValueError as exc:
        raise _http(409, "task_finished", str(exc))
    return {"success": True, "task_id": task_id, "status": status}


@router.post("/tasks/{task_id}/resume", status_code=202, dependencies=_WRITE)
async def resume_task(task_id: str) -> Dict[str, Any]:
    if registry.get(task_id) is not None:
        raise _http(409, "task_not_interrupted", "只有中断的任务可以继续")
    stored = store.task_load(task_id)
    if stored is None:
        raise _http(404, "task_not_found", "任务不存在")
    if stored["status"] != "interrupted":
        raise _http(409, "task_not_interrupted", "只有中断的任务可以继续")
    request_body = stored.get("request") or {}
    if stored["kind"] == "report":
        raise _http(409, "report_not_resumable", "举报任务不支持续报，请重新确认后提交")
    if stored["kind"] == "scan":
        if registry.running_of("scan"):
            raise _http(409, "scan_running", "已有扫描任务在运行")
        try:
            req = ScanRequest(**request_body).model_dump(mode="json")
        except Exception as exc:
            raise _http(409, "resume_invalid", "原任务参数无效，请重新扫描") from exc
        # 断点续采：恢复原批次，从 checkpoint 的游标继续，合并中断前已采集的评论。
        checkpoint = stored.get("checkpoint") or {}
        resume_cursor = checkpoint.get("pagination_str")
        resume_scan_id = stored.get("scan_id") or checkpoint.get("scan_id")
        _start_scan(req, task_id=task_id, resume_cursor=resume_cursor,
                    resume_scan_id=int(resume_scan_id) if resume_scan_id else None)
        return {"success": True, "task_id": task_id, "resumed": True}
    if registry.running_of("judge"):
        raise _http(409, "judge_running", "已有 AI 甄别任务在运行")
    scan_id = stored.get("scan_id")
    if not scan_id or store.get_scan(int(scan_id)) is None:
        raise _http(409, "resume_invalid", "原扫描记录已不存在")
    try:
        req = JudgeRequest(**request_body).model_dump(mode="json")
    except Exception as exc:
        raise _http(409, "resume_invalid", "原任务参数无效，请重新发起") from exc
    _start_judge(int(scan_id), req, task_id=task_id)
    return {"success": True, "task_id": task_id, "resumed": True}


@router.get("/preferences", dependencies=_READ)
async def get_preferences() -> Dict[str, Any]:
    pref = store.load_preferences()
    pref.pop("report_cooldown_until", None)
    pref["ai_consent_ok"] = (pref.get("ai_consent_version") == AI_CONSENT_VERSION
                             and pref.get("ai_model_identity") == judge_mod.model_identity())
    pref["proxy_consent_ok"] = pref.get("proxy_consent_version") == PROXY_CONSENT_VERSION
    pref["risk_notice_ok"] = pref.get("risk_notice_version") == RISK_NOTICE_VERSION
    return {"success": True, "data": pref}


@router.put("/preferences", dependencies=_WRITE)
async def put_preferences(req: PreferencesPatch) -> Dict[str, Any]:
    patch: Dict[str, Any] = {}
    now = datetime.now()
    current = store.load_preferences()
    if req.ai_consent == "agree":
        patch.update(ai_consent_version=AI_CONSENT_VERSION, ai_model_identity=judge_mod.model_identity())
    if req.risk_notice == "agree":
        patch.update(risk_notice_version=RISK_NOTICE_VERSION, risk_notice_at=now)
    if req.proxy_consent == "agree":
        patch.update(proxy_consent_version=PROXY_CONSENT_VERSION, proxy_consent_at=now)
    if req.proxy_enabled is not None:
        consent = patch.get("proxy_consent_version") or current.get("proxy_consent_version")
        if req.proxy_enabled and consent != PROXY_CONSENT_VERSION:
            raise _http(400, "proxy_consent_required", "请先同意代发举报的风险说明")
        patch["proxy_enabled"] = req.proxy_enabled
    if req.include_replies is not None:
        patch["include_replies"] = req.include_replies
    if req.whitelist_mids is not None:
        patch["whitelist_mids"] = sorted({int(m) for m in req.whitelist_mids if m > 0})
    if req.user_keywords is not None:
        patch["user_keywords"] = [k.model_dump() for k in req.user_keywords if k.word.strip()]
    if req.default_report_content is not None:
        text = req.default_report_content.strip()
        if text and not 2 <= len(text) <= 200:
            raise _http(400, "bad_request", "默认举报说明需为 2–200 字")
        patch["default_report_content"] = text
    try:
        pref = store.save_preferences(patch, req.expected_revision)
    except PreferenceConflict as exc:
        raise _translate(exc)
    pref.pop("report_cooldown_until", None)
    return {"success": True, "data": pref}


_CSV_RISKY = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> Any:
    """防 CSV 公式注入：首字符及每个换行后的行首是危险字符时前置单引号。"""
    if not isinstance(value, str) or not value:
        return value
    # 逐行处理，覆盖多行文本单元格中换行后的新行首。
    lines = value.split("\n")
    safe_lines = [("'" + line if line.startswith(_CSV_RISKY) else line) for line in lines]
    return "\n".join(safe_lines)


_TIER_LABEL = {"bot": "高度疑似", "suspect": "疑似", "human": "较像真人", "unknown": "证据不足", "whitelist": "白名单"}
_SOURCE_LABEL = {"rule": "规则估计", "hard_rule": "规则估计", "rule+llm": "规则+AI", "whitelist": "白名单"}


@router.get("/scans/{scan_id}/export", dependencies=_READ)
async def export_scan(scan_id: int, include_ids: bool = False) -> StreamingResponse:
    scan = _require_scan(scan_id)
    rows = store.all_comments(scan_id)
    rows.sort(key=lambda c: -(c.get("final_score") or 0))
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM：Excel 识别 UTF-8
    writer = csv.writer(buf)
    header = ["rpid", "是否楼中楼", "昵称", "等级", "评论内容", "发布时间", "点赞", "规则伪人率", "AI伪人率",
              "最终伪人率", "档位", "来源", "是否分歧", "命中规则", "AI理由", "举报状态"]
    if include_ids:
        header += ["mid", "签名"]
    writer.writerow(header)
    for c in rows:
        line = [c["rpid"], "是" if c["is_reply"] else "否", c["uname"], c["level"], c["content"], c["ctime"] or "",
                c["like"], c["rule_score"], "" if c["ai_score"] is None else c["ai_score"], c["final_score"],
                _TIER_LABEL.get(c["tier"], c["tier"]), _SOURCE_LABEL.get(c["score_source"], c["score_source"]),
                "是" if c["disagreement"] else "否",
                "；".join(h.get("label", "") for h in c["rule_hits"] if int(h.get("weight") or 0) > 0),
                c["ai_reason"], c["report_status"]]
        if include_ids:
            line += [c["mid"], c["sign"]]
        writer.writerow([_csv_safe(v) for v in line])
    name = f"patrol_{scan['bvid']}_{datetime.now():%Y%m%d%H%M}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.delete("/data", dependencies=_WRITE)
async def clear_data(keep_reports: bool = True) -> Dict[str, Any]:
    if registry.any_running():
        raise _http(409, "task_running", "有巡检任务在运行，请先等待完成或取消")
    return {"success": True, "data": store.clear_all(keep_reports=keep_reports)}


# ============ 生命周期钩子 ============

def recover_tasks_on_startup() -> Dict[str, Any]:
    """lifespan 调用：标记中断、恢复台账状态、过期清理；失败只记日志。"""
    return get_service().startup()


def cancel_all_tasks(reason: str) -> int:
    """logout 调用：取消全部运行中的巡检任务。"""
    return registry.cancel_all(reason)