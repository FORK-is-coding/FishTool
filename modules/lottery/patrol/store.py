"""AI 伪人巡检存储层（SQLite）。

会话写法与 web/routers/lottery_store.py 一致：每个函数自己开关会话。
- 台账写入失败必须向上抛（举报前必须先落 ``sending``，否则不能发请求）。
- 其他读写失败记 warning 后按“不存在 / 空”降级，不让巡检页面整体报错。
"""
import contextlib
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from core.database import (
    OperationLog,
    PatrolComment,
    PatrolLlmCache,
    PatrolPreference,
    PatrolReport,
    PatrolScan,
    Task,
    get_session,
)
from core.logger import get_logger

from .contracts import (
    REPORT_ACTIVE_STATUSES,
    REPORT_NOT_ATTEMPTED,
    PatrolSettings,
    PreferenceConflict,
)

logger = get_logger(__name__)

#: 巡检任务在 ``tasks.task_type`` 中的取值，与抽奖、评论采集等任务隔离。
TASK_TYPES = {"scan": "lottery_patrol_scan", "judge": "lottery_patrol_judge", "report": "lottery_patrol_report"}
_TASK_NAMES = {"scan": "AI伪人巡检·扫描", "judge": "AI伪人巡检·AI甄别", "report": "AI伪人巡检·举报"}
_COMMENT_SCORE_FIELDS = ("rule_score", "rule_hits", "hard_groups", "has_focus_hit", "ai_score", "ai_reason",
                         "final_score", "tier", "score_source", "disagreement")
_COMMENT_FIELDS = ("rpid", "root_rpid", "parent_rpid", "is_reply", "mid", "uname", "sign", "level", "is_vip",
                   "official_type", "content", "ctime", "like", "rcount", "at_users") + _COMMENT_SCORE_FIELDS
_PREF_ID = 1


@contextlib.contextmanager
def _session() -> Iterator[Any]:
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


# ============ 扫描批次 ============

def create_scan(scan_key: str, meta: Dict[str, Any], scanner_mid: Optional[int], options: Dict[str, Any],
                rules_version: str) -> int:
    with _session() as s:
        row = PatrolScan(
            scan_key=scan_key, bvid=meta["bvid"], aid=int(meta["aid"]), comment_type=1,
            title=meta.get("title"), owner_mid=meta.get("owner_mid") or None, owner_name=meta.get("owner_name"),
            scanner_mid=scanner_mid, options=dict(options), stats={}, rules_version=rules_version,
            status="running", partial=False,
        )
        s.add(row)
        s.flush()
        return int(row.id)


def finish_scan(scan_id: int, status: str, *, partial: bool = False, partial_reason: str = "",
                stats: Optional[Dict[str, Any]] = None) -> None:
    try:
        with _session() as s:
            row = s.get(PatrolScan, scan_id)
            if row is None:
                return
            row.status = status
            row.partial = bool(partial)
            row.partial_reason = (partial_reason or "")[:200] or None
            if stats is not None:
                row.stats = dict(stats)
            row.completed_at = datetime.utcnow()
    except Exception as exc:  # noqa: BLE001 - 终态写失败只降级
        logger.warning("[巡检] 扫描终态写入失败: %s %s", scan_id, exc)


def update_scan_stats(scan_id: int, patch: Dict[str, Any]) -> None:
    try:
        with _session() as s:
            row = s.get(PatrolScan, scan_id)
            if row is not None:
                row.stats = {**(row.stats or {}), **patch}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[巡检] 扫描统计更新失败: %s %s", scan_id, exc)


def _scan_dict(row: PatrolScan) -> Dict[str, Any]:
    return {
        "scan_id": row.id, "scan_key": row.scan_key, "bvid": row.bvid, "aid": row.aid, "title": row.title,
        "owner_mid": row.owner_mid, "owner_name": row.owner_name, "scanner_mid": row.scanner_mid,
        "options": row.options or {}, "stats": row.stats or {}, "rules_version": row.rules_version,
        "status": row.status, "partial": bool(row.partial), "partial_reason": row.partial_reason or "",
        "created_at": _iso(row.created_at), "completed_at": _iso(row.completed_at),
        "completed_at_dt": row.completed_at,
    }


def get_scan(scan_id: int) -> Optional[Dict[str, Any]]:
    try:
        with _session() as s:
            row = s.get(PatrolScan, scan_id)
            return _scan_dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[巡检] 读取扫描失败: %s %s", scan_id, exc)
        return None


def get_scan_by_key(scan_key: str) -> Optional[Dict[str, Any]]:
    try:
        with _session() as s:
            row = s.query(PatrolScan).filter(PatrolScan.scan_key == scan_key).first()
            return _scan_dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[巡检] 读取扫描失败: %s %s", scan_key, exc)
        return None


# ============ 评论结果 ============

def upsert_comments(scan_id: int, items: Sequence[Dict[str, Any]], batch: int = 200) -> int:
    """按 (scan_id, rpid) 写入；已存在的只更新评分字段。"""
    written = 0
    items = list(items)
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        with _session() as s:
            rpids = [int(it["rpid"]) for it in chunk]
            existing = {r.rpid: r for r in s.query(PatrolComment).filter(
                PatrolComment.scan_id == scan_id, PatrolComment.rpid.in_(rpids)).all()}
            for it in chunk:
                row = existing.get(int(it["rpid"]))
                if row is None:
                    data = {k: it.get(k) for k in _COMMENT_FIELDS if k in it}
                    data["rule_hits"] = list(it.get("rule_hits") or it.get("hits") or [])
                    s.add(PatrolComment(scan_id=scan_id, report_status="none", **data))
                else:
                    for key in _COMMENT_SCORE_FIELDS:
                        if key in it:
                            setattr(row, key, it[key])
                written += 1
    return written


def update_scores(scan_id: int, updates: Sequence[Dict[str, Any]]) -> None:
    """只更新评分字段；updates 每项需含 rpid。"""
    if not updates:
        return
    with _session() as s:
        by_rpid = {int(u["rpid"]): u for u in updates}
        rows = s.query(PatrolComment).filter(PatrolComment.scan_id == scan_id,
                                             PatrolComment.rpid.in_(list(by_rpid))).all()
        for row in rows:
            for key, value in by_rpid[row.rpid].items():
                if key in _COMMENT_SCORE_FIELDS:
                    setattr(row, key, value)


def _comment_dict(row: PatrolComment) -> Dict[str, Any]:
    return {
        "rpid": row.rpid, "root_rpid": row.root_rpid, "parent_rpid": row.parent_rpid, "is_reply": bool(row.is_reply),
        "mid": row.mid, "uname": row.uname or "", "sign": row.sign or "", "level": row.level,
        "is_vip": row.is_vip, "official_type": row.official_type, "content": row.content or "",
        "ctime": _iso(row.ctime), "like": row.like or 0, "rcount": row.rcount or 0, "at_users": row.at_users or [],
        "rule_score": row.rule_score, "rule_hits": row.rule_hits or [], "hard_groups": row.hard_groups or [],
        "has_focus_hit": bool(row.has_focus_hit), "ai_score": row.ai_score, "ai_reason": row.ai_reason or "",
        "final_score": row.final_score, "tier": row.tier, "score_source": row.score_source,
        "disagreement": bool(row.disagreement), "report_status": row.report_status,
    }


def all_comments(scan_id: int) -> List[Dict[str, Any]]:
    with _session() as s:
        rows = s.query(PatrolComment).filter(PatrolComment.scan_id == scan_id).all()
        return [_comment_dict(r) for r in rows]


def get_comments_by_rpids(scan_id: int, rpids: Iterable[int]) -> List[Dict[str, Any]]:
    rpids = [int(r) for r in rpids]
    if not rpids:
        return []
    with _session() as s:
        rows = s.query(PatrolComment).filter(PatrolComment.scan_id == scan_id, PatrolComment.rpid.in_(rpids)).all()
        return [_comment_dict(r) for r in rows]


_SORTS = {
    "score_desc": (PatrolComment.final_score.desc(), PatrolComment.rpid.desc()),
    "time_desc": (PatrolComment.ctime.desc(), PatrolComment.rpid.desc()),
    "like_desc": (PatrolComment.like.desc(), PatrolComment.rpid.desc()),
}


def list_comments(scan_id: int, *, page: int = 1, page_size: int = 50, tier: str = "", min_score: Optional[int] = None,
                  max_score: Optional[int] = None, focus_only: bool = False, disagreement_only: bool = False,
                  report_status: str = "", keyword: str = "", sort: str = "score_desc") -> Dict[str, Any]:
    """一级评论分页；楼中楼挂在其一级评论下（每楼最多 20 条）。筛选同时作用于一级评论与楼中楼。"""
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    with _session() as s:
        q = s.query(PatrolComment).filter(PatrolComment.scan_id == scan_id)
        if tier:
            q = q.filter(PatrolComment.tier == tier)
        if min_score is not None:
            q = q.filter(PatrolComment.final_score >= int(min_score))
        if max_score is not None:
            q = q.filter(PatrolComment.final_score <= int(max_score))
        if focus_only:
            q = q.filter(PatrolComment.has_focus_hit.is_(True))
        if disagreement_only:
            q = q.filter(PatrolComment.disagreement.is_(True))
        if report_status:
            q = q.filter(PatrolComment.report_status == report_status)
        if keyword:
            like = f"%{keyword[:50]}%"
            q = q.filter(or_(PatrolComment.content.like(like), PatrolComment.uname.like(like)))
        filtering = any([tier, min_score is not None, max_score is not None, focus_only, disagreement_only,
                         report_status, keyword])
        # 未筛选时只列一级评论（楼中楼折叠在下方）；筛选时一级 / 楼中楼都作为独立行，避免命中的楼中楼被藏起来。
        if not filtering:
            q = q.filter(PatrolComment.is_reply.is_(False))
        total = q.count()
        rows = q.order_by(*_SORTS.get(sort, _SORTS["score_desc"])).offset((page - 1) * page_size).limit(page_size).all()
        items = [_comment_dict(r) for r in rows]
        if not filtering and items:
            roots = [it["rpid"] for it in items]
            subs = s.query(PatrolComment).filter(
                PatrolComment.scan_id == scan_id, PatrolComment.is_reply.is_(True),
                PatrolComment.root_rpid.in_(roots)).order_by(PatrolComment.final_score.desc()).all()
            grouped: Dict[int, List[Dict[str, Any]]] = {}
            for r in subs:
                bucket = grouped.setdefault(r.root_rpid, [])
                if len(bucket) < 20:
                    bucket.append(_comment_dict(r))
            for it in items:
                it["replies"] = grouped.get(it["rpid"], [])
        else:
            for it in items:
                it["replies"] = []
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size)}


def set_comment_report_status(scan_id: Optional[int], rpid: int, status: str) -> None:
    if scan_id is None:
        return
    try:
        with _session() as s:
            s.query(PatrolComment).filter(PatrolComment.scan_id == scan_id, PatrolComment.rpid == int(rpid)).update(
                {"report_status": status}, synchronize_session=False)
    except Exception as exc:  # noqa: BLE001 - 评论行状态只是展示，失败不影响台账
        logger.warning("[巡检] 评论举报状态更新失败: %s %s %s", scan_id, rpid, exc)


# ============ 举报台账 ============

def _report_dict(row: PatrolReport) -> Dict[str, Any]:
    return {
        "report_id": row.id, "request_key": row.request_key, "mode": row.mode, "reporter_mid": row.reporter_mid,
        "scan_id": row.scan_id, "oid": row.oid, "rpid": row.rpid, "target_mid": row.target_mid,
        "target_uname": row.target_uname or "", "reason": row.reason, "content": row.content or "",
        "final_score": row.final_score, "tier": row.tier, "bili_code": row.bili_code,
        "bili_message": row.bili_message or "", "status": row.status,
        "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
    }


def count_report_attempts(reporter_mid: int, since: datetime) -> int:
    """滚动窗口内该账号的代发尝试次数（成功、失败、结果未知都算；未真正发请求的不算）。"""
    with _session() as s:
        return s.query(PatrolReport).filter(
            PatrolReport.reporter_mid == int(reporter_mid), PatrolReport.mode == "proxy",
            PatrolReport.created_at >= since, PatrolReport.status.notin_(list(REPORT_NOT_ATTEMPTED)),
        ).count()


def has_active_report(reporter_mid: int, oid: int, rpid: int) -> bool:
    with _session() as s:
        return s.query(PatrolReport).filter(
            PatrolReport.reporter_mid == int(reporter_mid), PatrolReport.oid == int(oid),
            PatrolReport.rpid == int(rpid), PatrolReport.status.in_(list(REPORT_ACTIVE_STATUSES)),
        ).first() is not None


def active_report_rpids(reporter_mid: Optional[int], oid: int, rpids: Iterable[int]) -> set:
    """一次查出已被占用（发送中 / 已举报 / 已举报过）的 rpid。reporter_mid 为空时按任意账号。"""
    rpids = [int(r) for r in rpids]
    if not rpids:
        return set()
    with _session() as s:
        q = s.query(PatrolReport.rpid).filter(PatrolReport.oid == int(oid), PatrolReport.rpid.in_(rpids),
                                              PatrolReport.status.in_(list(REPORT_ACTIVE_STATUSES)))
        if reporter_mid:
            q = q.filter(PatrolReport.reporter_mid == int(reporter_mid))
        return {r[0] for r in q.all()}


def _report_row(row: Dict[str, Any], status: str) -> PatrolReport:
    return PatrolReport(
        request_key=row["request_key"], mode=row["mode"], reporter_mid=int(row["reporter_mid"]),
        scan_id=row.get("scan_id"), oid=int(row["oid"]), type=1, rpid=int(row["rpid"]),
        target_mid=row.get("target_mid"), target_uname=(row.get("target_uname") or "")[:100] or None,
        reason=row.get("reason"), content=(row.get("content") or "")[:200] or None,
        final_score=row.get("final_score"), tier=row.get("tier"), status=status,
        bili_code=row.get("bili_code"), bili_message=(row.get("bili_message") or "")[:200] or None,
    )


def insert_report_sending(row: Dict[str, Any]) -> int:
    """POST 前插入 ``sending`` 行并立即提交。

    Returns:
        新行 id；被并发占用（部分唯一索引 / request_key 冲突）时返回 0。
        其他数据库错误向上抛：落不了台账就不能发请求。
    """
    session = get_session()
    try:
        record = _report_row(row, "sending")
        session.add(record)
        session.commit()
        return int(record.id)
    except IntegrityError:
        session.rollback()
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def finalize_report(report_id: int, status: str, bili_code: Optional[int], bili_message: str) -> bool:
    """只允许从 ``sending`` 更新一次终态；返回是否真的更新了。"""
    with _session() as s:
        n = s.query(PatrolReport).filter(PatrolReport.id == int(report_id), PatrolReport.status == "sending").update(
            {"status": status, "bili_code": bili_code, "bili_message": (bili_message or "")[:200] or None,
             "updated_at": datetime.utcnow()}, synchronize_session=False)
        return n == 1


def insert_report_simple(row: Dict[str, Any], status: str) -> int:
    """原生 / 演练 / 跳过等一次性记录；同 request_key + rpid 已有记录时返回 0。"""
    session = get_session()
    try:
        record = _report_row(row, status)
        session.add(record)
        session.commit()
        return int(record.id)
    except IntegrityError:
        session.rollback()
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def mark_native(report_id: int, done: bool) -> Optional[Dict[str, Any]]:
    with _session() as s:
        row = s.get(PatrolReport, int(report_id))
        if row is None or row.mode != "native" or row.status not in ("native_opened", "native_done"):
            return None
        row.status = "native_done" if done else "native_opened"
        row.updated_at = datetime.utcnow()
        s.flush()
        result = _report_dict(row)
        scan_id, rpid = row.scan_id, row.rpid
    set_comment_report_status(scan_id, rpid, result["status"])
    return result


def list_reports(days: int = 30, limit: int = 500) -> List[Dict[str, Any]]:
    since = datetime.utcnow() - timedelta(days=max(1, int(days)))
    with _session() as s:
        rows = s.query(PatrolReport).filter(PatrolReport.created_at >= since).order_by(
            PatrolReport.id.desc()).limit(limit).all()
        return [_report_dict(r) for r in rows]


# ============ 偏好 ============

def _pref_dict(row: PatrolPreference) -> Dict[str, Any]:
    return {
        "revision": row.revision, "ai_consent_version": row.ai_consent_version,
        "ai_model_identity": row.ai_model_identity, "risk_notice_version": row.risk_notice_version,
        "proxy_consent_version": row.proxy_consent_version, "proxy_enabled": bool(row.proxy_enabled),
        "include_replies": bool(row.include_replies), "whitelist_mids": list(row.whitelist_mids or []),
        "user_keywords": list(row.user_keywords or []), "default_report_content": row.default_report_content or "",
        "report_cooldown_until": row.report_cooldown_until,
    }


def _ensure_pref(s: Any) -> PatrolPreference:
    row = s.get(PatrolPreference, _PREF_ID)
    if row is None:
        row = PatrolPreference(id=_PREF_ID, revision=0, proxy_enabled=False, include_replies=True,
                               whitelist_mids=[], user_keywords=[])
        s.add(row)
        s.flush()
    return row


def load_preferences() -> Dict[str, Any]:
    try:
        with _session() as s:
            return _pref_dict(_ensure_pref(s))
    except Exception as exc:  # noqa: BLE001 - 读失败给默认值
        logger.warning("[巡检] 读取偏好失败，使用默认值: %s", exc)
        return {"revision": 0, "ai_consent_version": None, "ai_model_identity": None, "risk_notice_version": None,
                "proxy_consent_version": None, "proxy_enabled": False, "include_replies": True,
                "whitelist_mids": [], "user_keywords": [], "default_report_content": "",
                "report_cooldown_until": None}


_PREF_FIELDS = {"ai_consent_version", "ai_model_identity", "risk_notice_version", "risk_notice_at",
                "proxy_consent_version", "proxy_consent_at", "proxy_enabled", "include_replies", "whitelist_mids",
                "user_keywords", "default_report_content", "report_cooldown_until"}


def save_preferences(patch: Dict[str, Any], expected_revision: Optional[int]) -> Dict[str, Any]:
    """更新偏好；expected_revision 不为 None 时做 CAS，不一致抛 PreferenceConflict。"""
    with _session() as s:
        row = _ensure_pref(s)
        if expected_revision is not None and int(expected_revision) != int(row.revision):
            raise PreferenceConflict()
        for key, value in patch.items():
            if key in _PREF_FIELDS:
                setattr(row, key, value)
        row.revision = int(row.revision) + 1
        row.updated_at = datetime.utcnow()
        s.flush()
        return _pref_dict(row)


def set_cooldown(until: datetime) -> None:
    save_preferences({"report_cooldown_until": until}, None)


def get_cooldown() -> Optional[datetime]:
    until = load_preferences().get("report_cooldown_until")
    return until if isinstance(until, datetime) and until > datetime.utcnow() else None


# ============ LLM 缓存 ============

def get_llm_cache(keys: Iterable[str], cache_days: int) -> Dict[str, Dict[str, Any]]:
    keys = list(keys)
    if not keys or cache_days <= 0:
        return {}
    since = datetime.utcnow() - timedelta(days=cache_days)
    try:
        with _session() as s:
            rows = s.query(PatrolLlmCache).filter(PatrolLlmCache.cache_key.in_(keys),
                                                  PatrolLlmCache.created_at >= since).all()
            return {r.cache_key: {"ai_score": r.ai_score, "reason": r.reason or ""} for r in rows}
    except Exception as exc:  # noqa: BLE001 - 缓存不可用当作未命中
        logger.warning("[巡检] 读取 AI 缓存失败: %s", exc)
        return {}


def put_llm_cache(entries: Dict[str, Dict[str, Any]], model: str) -> None:
    if not entries:
        return
    try:
        with _session() as s:
            for key, value in entries.items():
                row = s.get(PatrolLlmCache, key)
                if row is None:
                    row = PatrolLlmCache(cache_key=key)
                    s.add(row)
                row.ai_score = value.get("ai_score")
                row.reason = (value.get("reason") or "")[:200]
                row.model = (model or "")[:100]
                row.created_at = datetime.utcnow()
    except Exception as exc:  # noqa: BLE001 - 缓存写失败不影响结果
        logger.warning("[巡检] 写入 AI 缓存失败: %s", exc)


# ============ 审计 ============

_AUDIT_FORBIDDEN = {"cookie", "csrf", "bili_jct", "sessdata", "content", "text"}


def write_operation_log(operation: str, details: Dict[str, Any], status: str, error: Optional[str] = None) -> None:
    safe = {k: v for k, v in (details or {}).items() if k.lower() not in _AUDIT_FORBIDDEN}
    try:
        with _session() as s:
            s.add(OperationLog(operation=operation, module="lottery_patrol", details=safe, status=status,
                               error=(error or None)))
    except Exception as exc:  # noqa: BLE001 - 审计失败只记日志
        logger.warning("[巡检] 写入操作审计失败: %s %s", operation, exc)


# ============ 任务持久化（不注册进 lottery_store） ============

def _kind_of(task_type: Optional[str]) -> Optional[str]:
    for kind, value in TASK_TYPES.items():
        if value == task_type:
            return kind
    return None


def task_create(task_id: str, kind: str, request: Dict[str, Any], scan_id: Optional[int] = None) -> None:
    try:
        with _session() as s:
            s.add(Task(task_key=task_id, task_type=TASK_TYPES[kind], task_name=_TASK_NAMES[kind],
                       params={"request": request, "scan_id": scan_id}, status="running", progress=0.0,
                       started_at=datetime.utcnow()))
    except Exception as exc:  # noqa: BLE001 - 持久化失败只降级
        logger.warning("[巡检] 任务持久化失败（任务仍在内存中运行）: %s %s", task_id, exc)


def task_set_scan(task_id: str, scan_id: int) -> None:
    try:
        with _session() as s:
            row = s.query(Task).filter(Task.task_key == task_id).first()
            if row is not None:
                row.params = {**(row.params or {}), "scan_id": scan_id}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[巡检] 任务 scan_id 写入失败: %s %s", task_id, exc)


def task_finish(task_id: str, status: str, message: str, result: Optional[Dict[str, Any]] = None) -> None:
    try:
        with _session() as s:
            row = s.query(Task).filter(Task.task_key == task_id).first()
            if row is None:
                return
            row.status = status
            row.error = message
            if status == "completed":
                row.result = result
                row.progress = 1.0
            if status != "running":
                row.completed_at = datetime.utcnow()
            else:
                row.completed_at = None
                row.started_at = datetime.utcnow()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[巡检] 任务状态持久化失败: %s %s %s", task_id, status, exc)


def task_checkpoint(task_id: str, checkpoint: Dict[str, Any]) -> None:
    try:
        with _session() as s:
            row = s.query(Task).filter(Task.task_key == task_id).first()
            if row is not None:
                row.checkpoint = dict(checkpoint)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[巡检] 任务断点写入失败: %s %s", task_id, exc)


def task_load(task_id: str) -> Optional[Dict[str, Any]]:
    try:
        with _session() as s:
            row = s.query(Task).filter(Task.task_key == task_id,
                                       Task.task_type.in_(list(TASK_TYPES.values()))).first()
            if row is None:
                return None
            params = row.params if isinstance(row.params, dict) else {}
            status = row.status or "failed"
            return {
                "task_id": row.task_key, "kind": _kind_of(row.task_type), "status": status, "stage": status,
                "progress": 100 if status == "completed" else int(round((row.progress or 0) * 100)),
                "message": row.error or ("任务已完成" if status == "completed" else ""),
                "estimated_seconds": 0 if status != "running" else None,
                "result": row.result if status == "completed" else None,
                "request": params.get("request"), "scan_id": params.get("scan_id"),
                "checkpoint": row.checkpoint if isinstance(row.checkpoint, dict) else None,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[巡检] 读取任务失败: %s %s", task_id, exc)
        return None


def recover_on_startup(retention_days: int) -> Dict[str, Any]:
    """启动收尾：任务与扫描标记中断、台账 sending→unknown、评论 queued→none、过期清理任务行。"""
    outcome = {"interrupted": 0, "reports_unknown": 0, "pruned_tasks": 0}
    try:
        with _session() as s:
            types = list(TASK_TYPES.values())
            stale = s.query(Task).filter(Task.task_type.in_(types), Task.status == "running").all()
            for row in stale:
                row.status = "interrupted"
                row.error = "服务重启，任务已中断"
                row.completed_at = datetime.utcnow()
            outcome["interrupted"] = len(stale)
            s.query(PatrolScan).filter(PatrolScan.status == "running").update(
                {"status": "interrupted", "completed_at": datetime.utcnow()}, synchronize_session=False)
            outcome["reports_unknown"] = s.query(PatrolReport).filter(PatrolReport.status == "sending").update(
                {"status": "unknown", "bili_message": "服务重启时仍在提交，结果未知", "updated_at": datetime.utcnow()},
                synchronize_session=False)
            s.query(PatrolComment).filter(PatrolComment.report_status == "queued").update(
                {"report_status": "none"}, synchronize_session=False)
            s.query(PatrolComment).filter(PatrolComment.report_status == "sending").update(
                {"report_status": "unknown"}, synchronize_session=False)
            cutoff = datetime.utcnow() - timedelta(days=max(1, int(retention_days)))
            outcome["pruned_tasks"] = s.query(Task).filter(
                Task.task_type.in_(types), Task.status.in_(["completed", "failed", "cancelled", "interrupted"]),
                Task.completed_at.isnot(None), Task.completed_at < cutoff).delete(synchronize_session=False)
    except Exception as exc:  # noqa: BLE001 - 启动收尾失败不阻断应用
        logger.warning("[巡检] 启动收尾失败: %s", exc)
    return outcome


def prune(settings: PatrolSettings) -> Dict[str, int]:
    """按保留天数清理扫描（级联评论）、台账、AI 缓存。"""
    now = datetime.utcnow()
    out = {"scans": 0, "reports": 0, "llm_cache": 0}
    try:
        with _session() as s:
            out["scans"] = s.query(PatrolScan).filter(
                PatrolScan.created_at < now - timedelta(days=settings.retention_scans_days),
                PatrolScan.status != "running").delete(synchronize_session=False)
            out["reports"] = s.query(PatrolReport).filter(
                PatrolReport.created_at < now - timedelta(days=settings.retention_reports_days),
                PatrolReport.status != "sending").delete(synchronize_session=False)
            out["llm_cache"] = s.query(PatrolLlmCache).filter(
                PatrolLlmCache.created_at < now - timedelta(days=settings.retention_llm_cache_days)).delete(
                synchronize_session=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[巡检] 过期数据清理失败: %s", exc)
    return out


def clear_all(keep_reports: bool = True) -> Dict[str, int]:
    """用户点击「清除巡检数据」：删除全部扫描与评论（级联），可选保留台账。"""
    with _session() as s:
        scans = s.query(PatrolScan).filter(PatrolScan.status != "running").delete(synchronize_session=False)
        cache = s.query(PatrolLlmCache).delete(synchronize_session=False)
        reports = 0
        if not keep_reports:
            reports = s.query(PatrolReport).filter(PatrolReport.status != "sending").delete(synchronize_session=False)
    return {"scans": scans, "llm_cache": cache, "reports": reports}
