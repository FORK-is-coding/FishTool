"""FishTool 04 · 第三批 g：事件 API 路由（§14 清单）。

依据：
- ``FishTool_04_话题级热点研判与选题决策_02补充执行案(1).md`` §14（API 与状态契约）、
  §11.6 反馈段、§11.7 D；
- ``FishTool_04_R5执行规格_第三批g_API路由与工作台.md`` §1 / §2。

钉死口径（逐条实现，不做发挥）：

- **任务 completed ≠ 数据有效**：通用 ``GET /event-tasks/{id}`` 在任务完成但
  ``data_status=insufficient`` 时也返回 HTTP ``completed``，前端轮询能正常结束；
- **发现 failed/cancelled/interrupted 统一映射 HTTP 任务 failed** 并携带原 ``reason_code``，
  **不留未支持状态让通用轮询无限等待**；
- **内存句柄重启不存在 → 404** 并给出持久 run 读取方式，**不假装能恢复**；
- 状态码契约：``expected_revision`` 冲突 409 / 不存在 404 / 非法 422 / 存储不可用 503 /
  **data 不足是正常状态**；
- **``except HTTPException: raise`` 必须排在宽泛异常之前**，422 不许被吞成 500；
- 严格校验：UID/BVID/事件 ID 形状、lists 与 JSON 大小上限、文本最长、候选上限 100、
  事件上限 5（可配置）；**不接受**客户端自填 ``phase`` / 指标 / evidence truth / 已验证 deadline；
  文本 trim、不拼 SQL、不执行 regex 或代码；无自动发帖与开奖。

本模块只做路由编排与校验；窗口内核 / 归属 / 围栏 / 机会排序等全部复用既有模块。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from core.database import Topic, get_session
from core.database.hot_event_repository import HotEventRepository
from core.database.models_hot_event import (
    ASSESSMENT_STATUSES,
    GENERATION_STATES,
    HOT_EVENT_STATUSES,
    MEMBER_STATUSES,
    WINDOW_KINDS,
    EventDiscoveryRun,
    HotEvent,
    HotEventAssessment,
    HotEventMember,
    OpportunityRun,
)
from modules.hotspot.events.brief import BriefValidationError, CreatorBrief
from modules.hotspot.events.opportunity import (
    build_opportunity_result,
    get_or_create_opportunity_run,
    request_fingerprint,
)
from modules.hotspot.events.policy import EventPolicy
from modules.hotspot.event_resolver import (
    EVENT_KINDS,
    EventDefinition,
    VideoEvidence,
    evaluate_events,
)

from . import router
from .schemas import (
    AssessTaskRequest,
    EventCreateRequest,
    EventPatchRequest,
    FeedbackRequest,
    MemberDecisionsRequest,
    OpportunityTaskRequest,
)

# ===========================================================================
# 上限（§1.2）
# ===========================================================================

#: 单个 JSON 字段序列化后的最大字节数。
MAX_JSON_BYTES: int = 20000
#: 列表字段最大条目数。
MAX_LIST_ITEMS: int = 200
#: 发现候选上限（与围栏一致；路由侧只读展示，不改围栏）。
MAX_CANDIDATES: int = 100
#: 一次机会任务可传事件上限（可配置）。
DEFAULT_MAX_EVENT_SELECTION: int = 5
#: 批量成员决定上限。
MAX_MEMBER_DECISIONS: int = 100
#: 文本字段最大长度。
MAX_TEXT_LEN: int = 2000
#: 分页单页上限。
MAX_PAGE_LIMIT: int = 200
#: 单条 BVID 形状（服务端只做形状校验，不执行用户 regex）。
_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{8,12}$")
#: 事件 / 机会 ID 形状（前缀 + 十六进制 / 单词字符）。
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

#: 反馈 kind 受控枚举。
FEEDBACK_KINDS: tuple = ("adopted", "rejected", "published", "outcome")
#: ``outcome_metrics`` 允许保留的有限数值键（其余一律丢弃）。
OUTCOME_METRIC_KEYS: tuple = (
    "view",
    "like",
    "coin",
    "favorite",
    "reply",
    "danmaku",
    "share",
    "followers_delta",
)


# ===========================================================================
# 第三批 i：只读预览请求模型（定义在本模块内，不改动 schemas.py）
# ===========================================================================

class _PreviewSample(BaseModel):
    """单条待预览候选（纯文本锚点；喂已有成员 / 示例文本即可，不落库）。"""

    model_config = ConfigDict(extra="forbid")

    bvid: Optional[str] = Field(default=None, max_length=32)
    title: str = Field(default="", max_length=MAX_TEXT_LEN)
    tags: List[str] = Field(default_factory=list)
    summary: str = Field(default="", max_length=MAX_TEXT_LEN)
    comment_terms: List[str] = Field(default_factory=list)


class MemberPreviewRequest(BaseModel):
    """``POST /events/{id}/members/preview`` 请求体（只读 dry-run）。

    - ``samples``：待预览候选（可选）；缺省时用规则字面量 + ``entity_anchors`` 生成示例兜底；
    - ``entity_anchors``：用户输入的实体 / 事件锚点；
    - ``strict_auto``：与真实归属同一开关，仅影响预览里的 ``decision`` 展示。
    """

    model_config = ConfigDict(extra="forbid")

    samples: List[_PreviewSample] = Field(default_factory=list)
    entity_anchors: List[str] = Field(default_factory=list)
    strict_auto: bool = False


# ===========================================================================
# 错误
# ===========================================================================

class EventApiError(Exception):
    """路由层业务错误基类（携带稳定 ``code``，由 :func:`_fail` 映射 HTTP）。"""

    code: str = "event_api_error"
    status: int = 400

    def __init__(self, message: str = "", *, status: Optional[int] = None) -> None:
        """初始化错误。

        Args:
            message: 稳定错误码 / 人读信息。
            status: 覆盖 HTTP 状态码。
        """
        super().__init__(message or self.code)
        if status is not None:
            self.status = status


def _fail(code: str, status: int, message: str = "") -> HTTPException:
    """构造统一 detail 的 :class:`HTTPException`（结构化 ``error_code``）。"""
    return HTTPException(status_code=status, detail={"error_code": code, "message": message or code})


# ===========================================================================
# 模块状态与依赖提供者（测试可 monkeypatch / configure 注入临时库）
# ===========================================================================

_STATE: Dict[str, Any] = {
    "session_factory": None,  # None → core.database.get_session
    "repository": None,
    "discovery_service": None,
    # §18.2 事件发现开关：生产由 web/main.py 按配置注入；缺省 True 以兼容测试注入。
    "discovery_enabled": True,
    "aggregation_service": None,
    "clock": None,
    "max_event_selection": DEFAULT_MAX_EVENT_SELECTION,
}

#: 内存任务句柄（发现 / 评估）。**故意只活在本进程**：重启即丢，丢失即 404。
_EVENT_TASKS: Dict[str, Dict[str, Any]] = {}


def configure(
    *,
    session_factory: Optional[Callable[[], Session]] = None,
    repository: Optional[HotEventRepository] = None,
    discovery_service: Any = None,
    aggregation_service: Any = None,
    discovery_enabled: Optional[bool] = None,
    clock: Optional[Callable[[], int]] = None,
    max_event_selection: Optional[int] = None,
) -> None:
    """注入依赖（由 ``web/main.py`` lifespan 或测试调用）。

    Args:
        session_factory: 会话工厂；缺省用 ``core.database.get_session``。
        repository: 3a 事件仓储。
        discovery_service: 3c 事件发现服务（``EventDiscoveryService``）。
        aggregation_service: 3d 评估服务（``EventAggregationService``）。
        discovery_enabled: §18.2 事件发现开关；``False`` 时发现端点返回未启用状态
            （``reason_code=discovery_disabled_by_config``），不返回 503。
        clock: 秒级时钟。
        max_event_selection: 一次机会任务事件上限。
    """
    if session_factory is not None:
        _STATE["session_factory"] = session_factory
    if repository is not None:
        _STATE["repository"] = repository
    if discovery_service is not None:
        _STATE["discovery_service"] = discovery_service
    if aggregation_service is not None:
        _STATE["aggregation_service"] = aggregation_service
    if discovery_enabled is not None:
        _STATE["discovery_enabled"] = bool(discovery_enabled)
    if clock is not None:
        _STATE["clock"] = clock
    if max_event_selection is not None:
        _STATE["max_event_selection"] = int(max_event_selection)


def reset_state() -> None:
    """重置注入状态与任务句柄（测试用）。"""
    _STATE.update(
        {
            "session_factory": None,
            "repository": None,
            "discovery_service": None,
            "discovery_enabled": True,
            "aggregation_service": None,
            "clock": None,
            "max_event_selection": DEFAULT_MAX_EVENT_SELECTION,
        }
    )
    _EVENT_TASKS.clear()


def get_session_factory() -> Callable[[], Session]:
    """返回会话工厂（缺省生产库）。"""
    return _STATE["session_factory"] or get_session


def get_repository() -> HotEventRepository:
    """返回 3a 事件仓储（懒构造）。"""
    if _STATE["repository"] is None:
        _STATE["repository"] = HotEventRepository(session_factory=get_session_factory())
    return _STATE["repository"]


def get_clock() -> Callable[[], int]:
    """返回秒级时钟（缺省 ``time.time``）。"""
    return _STATE["clock"] or (lambda: int(time.time()))


def _now() -> int:
    """当前 epoch 秒。"""
    return int(get_clock()())


# ===========================================================================
# 小工具：校验 / 序列化
# ===========================================================================

def _check_json_size(value: Any, field: str) -> None:
    """校验 JSON 字段大小上限（非法 / 超限 → 422）。"""
    if value is None:
        return
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise _fail("invalid_json_field", 422, f"{field}: 无法序列化为 JSON") from exc
    if size > MAX_JSON_BYTES:
        raise _fail("json_field_too_large", 422, f"{field}: 超过 {MAX_JSON_BYTES} 字节")


def _check_list_size(value: Optional[Sequence[Any]], field: str, *, limit: int = MAX_LIST_ITEMS) -> None:
    """校验列表字段条目上限。"""
    if value is not None and len(value) > limit:
        raise _fail("list_field_too_large", 422, f"{field}: 超过 {limit} 条")


def _clean_text(value: str, field: str, *, max_len: int = MAX_TEXT_LEN) -> str:
    """trim 文本并校验非空 / 长度上限（不执行任何用户 regex / 代码）。"""
    text = (value or "").strip()
    if not text:
        raise _fail("empty_text_field", 422, f"{field}: 不能为空")
    if len(text) > max_len:
        raise _fail("text_field_too_long", 422, f"{field}: 超过 {max_len} 字符")
    return text


def _clean_id(value: str, field: str) -> str:
    """校验 ID 形状（仅字符集 + 长度，不做代码 / SQL 拼接）。"""
    text = (value or "").strip()
    if not _ID_RE.match(text):
        raise _fail("invalid_id", 422, f"{field}: ID 形状非法")
    return text


def _iso(epoch_s: Optional[int]) -> Optional[str]:
    """epoch 秒 → 本地 ISO 字符串（仅展示）。"""
    if epoch_s is None:
        return None
    from datetime import datetime

    return datetime.fromtimestamp(int(epoch_s)).isoformat()


def _event_view(row: HotEvent, latest_assessment_id: Optional[str] = None) -> dict:
    """把事件行转成 API 视图。"""
    return {
        "event_id": row.id,
        "name": row.name,
        "status": row.status,
        "revision": int(row.revision),
        "current_rule_version": int(row.current_rule_version),
        "entity_scope": row.entity_scope,
        "source_policy": row.source_policy,
        "source_policy_hash": row.source_policy_hash,
        "links": row.links,
        "created_s": int(row.created_s),
        "updated_s": int(row.updated_s),
        "lease_until_s": row.lease_until_s,
        "latest_assessment_id": latest_assessment_id,
    }


def _assessment_view(row: HotEventAssessment) -> dict:
    """把评估行转成 API 视图（冻结事实，可被推荐引用）。"""
    interpretation = row.interpretation if isinstance(row.interpretation, dict) else {}
    return {
        "assessment_id": row.id,
        "event_id": row.event_id,
        "revision": int(row.revision),
        "as_of_s": int(row.as_of_s),
        "window_end_s": int(row.window_end_s),
        "window_kind": row.window_kind,
        "rule_version": int(row.rule_version),
        "policy_version": row.policy_version,
        "status": row.status,
        "data_status": row.status,
        "reason_codes": list(interpretation.get("reason_codes") or []),
        "interpretation": interpretation,
        "metrics": row.metrics,
        "provenance": row.provenance,
        "input_fingerprint": row.input_fingerprint,
    }


def _run_view(row: EventDiscoveryRun) -> dict:
    """把发现 run 行转成只读视图（进程重启后仍可查）。"""
    counters = row.counters if isinstance(row.counters, dict) else {}
    candidates = row.candidates if isinstance(row.candidates, list) else []
    return {
        "run_id": row.id,
        "event_id": row.event_id,
        "status": row.status,
        "error_code": row.error_code,
        "trigger": row.trigger,
        "started_s": int(row.started_s),
        "finished_s": row.finished_s,
        "candidate_count": len(candidates),
        "candidates": candidates[:MAX_CANDIDATES],
        "newly_discovered_bvids": list(row.newly_discovered_bvids or []),
        "counters": counters,
        "source_attempts": row.source_attempts,
    }


def _load_event(session: Session, event_id: str) -> HotEvent:
    """读取事件；不存在 → 404。"""
    row = session.get(HotEvent, event_id)
    if row is None:
        raise _fail("event_not_found", 404, f"事件不存在: {event_id}")
    return row


# ===========================================================================
# 反馈：规范化 + 固定 SHA-256 幂等
# ===========================================================================

def _normalize_outcome_metrics(raw: Optional[Dict[str, Any]], *, kind: str) -> Optional[dict]:
    """规范化 ``outcome_metrics``：只留允许的有限数值与来源 / 观测时间。

    Args:
        raw: 客户端提交的原始对象。
        kind: 反馈类型；仅 ``outcome`` 强制保留观测字段（缺失为 NULL）。

    Returns:
        dict | None: 规范化对象；``raw`` 为空返回 None。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _fail("invalid_outcome_metrics", 422, "outcome_metrics 必须是对象")
    values: Dict[str, float] = {}
    for key in OUTCOME_METRIC_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _fail("invalid_outcome_metric_value", 422, f"outcome_metrics.{key} 非法")
        if not math.isfinite(float(value)):
            raise _fail("non_finite_outcome_metric", 422, f"outcome_metrics.{key} 非有限数")
        values[key] = float(value)

    metric_source = raw.get("metric_source")
    if metric_source is not None and not isinstance(metric_source, str):
        raise _fail("invalid_metric_source", 422, "outcome_metrics.metric_source 非法")
    observed_s = raw.get("observed_s")
    if observed_s is not None and (isinstance(observed_s, bool) or not isinstance(observed_s, int)):
        raise _fail("invalid_observed_s", 422, "outcome_metrics.observed_s 非法")
    publish_age_s = raw.get("publish_age_s")
    if publish_age_s is not None and (isinstance(publish_age_s, bool) or not isinstance(publish_age_s, int)):
        raise _fail("invalid_publish_age_s", 422, "outcome_metrics.publish_age_s 非法")

    return {
        "metric_source": metric_source.strip() if isinstance(metric_source, str) else None,
        "observed_s": int(observed_s) if observed_s is not None else None,
        "publish_age_s": int(publish_age_s) if publish_age_s is not None else None,
        "values": values,
    }


def _normalize_feedback_payload(request: FeedbackRequest, run: OpportunityRun) -> dict:
    """把请求规范化成反馈 hash 用的固定九字段 payload。

    Args:
        request: 已通过 schema 校验的反馈请求。
        run: 目标机会运行（用于补 ``opportunity_run_id``）。

    Returns:
        dict: 固定九字段；可选缺值统一 ``None``。

    Raises:
        HTTPException: 422 —— kind / reason / 时长 / BVID 非法。
    """
    kind = (request.kind or "").strip()
    if kind not in FEEDBACK_KINDS:
        raise _fail("invalid_feedback_kind", 422, f"kind 非法: {kind}")
    reason = (request.reason or "").strip()
    if not reason:
        raise _fail("empty_feedback_reason", 422, "reason 不能为空")
    if len(reason) > MAX_TEXT_LEN:
        raise _fail("feedback_reason_too_long", 422, "reason 过长")

    seconds: Optional[int] = None
    if request.actual_production_hours is not None:
        hours = request.actual_production_hours
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(float(hours)):
            raise _fail("invalid_production_hours", 422, "actual_production_hours 非法")
        if float(hours) < 0:
            raise _fail("negative_production_hours", 422, "actual_production_hours 不能为负")
        seconds = int(round(float(hours) * 3600))  # 归一到非负整数秒

    bvid: Optional[str] = None
    if request.published_bvid:
        bvid = request.published_bvid.strip()
        if not _BVID_RE.match(bvid):
            raise _fail("invalid_published_bvid", 422, "published_bvid 形状非法")

    metrics = _normalize_outcome_metrics(request.outcome_metrics, kind=kind)

    return {
        "opportunity_run_id": run.id,
        "event_id": (request.event_id or "").strip(),
        "topic_id": (request.topic_id or "").strip(),
        "kind": kind,
        "reason": reason,
        "published_bvid": bvid,
        "actual_production_seconds": seconds,
        "outcome_metrics": metrics,
        # 客户端只能提交 user_reported；platform_verified 必须由独立服务器验证事件产生。
        "verification_status": "user_reported",
    }


def _feedback_payload_hash(payload: dict) -> str:
    """固定 SHA-256：``sort_keys`` + 紧凑分隔符 + 不允许 NaN。"""
    blob = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ===========================================================================
# 端点：事件 CRUD
# ===========================================================================

@router.post("/events")
async def create_event(request: EventCreateRequest):
    """创建草稿 / 激活规则，返回 ``event_id`` / ``revision``。

    服务端只存用户提交的整对象，**不解释**任何客户端自填真值。
    """
    try:
        if request.status not in HOT_EVENT_STATUSES:
            raise _fail("invalid_event_status", 422, f"status 非法: {request.status}")
        _check_json_size(request.entity_scope, "entity_scope")
        _check_json_size(request.source_policy, "source_policy")
        _check_json_size(request.links, "links")
        name = _clean_text(request.name, "name", max_len=200)
        event_id = _clean_id(request.event_id, "event_id") if request.event_id else f"evt_{uuid.uuid4().hex[:16]}"

        session = get_session_factory()()
        try:
            if session.get(HotEvent, event_id) is not None:
                raise _fail("event_already_exists", 409, f"事件已存在: {event_id}")
        finally:
            session.close()

        row = get_repository().create_hot_event(
            event_id=event_id,
            name=name,
            status=request.status,
            entity_scope=request.entity_scope,
            source_policy=request.source_policy,
            links=request.links,
        )
        return {"success": True, "data": {"event_id": row.id, "revision": int(row.revision), "status": row.status}}
    except HTTPException:
        raise


@router.get("/events")
async def list_events(status: Optional[str] = None, limit: int = 50, offset: int = 0):
    """分页列表，**不采集、不写状态**。"""
    try:
        if status is not None and status not in HOT_EVENT_STATUSES:
            raise _fail("invalid_event_status", 422, f"status 非法: {status}")
        if limit < 1 or limit > MAX_PAGE_LIMIT:
            raise _fail("invalid_limit", 422, f"limit 必须在 1..{MAX_PAGE_LIMIT}")
        if offset < 0:
            raise _fail("invalid_offset", 422, "offset 不能为负")
        session = get_session_factory()()
        try:
            query = session.query(HotEvent)
            if status is not None:
                query = query.filter(HotEvent.status == status)
            total = query.count()
            rows = (
                query.order_by(HotEvent.created_s.desc(), HotEvent.id.desc())
                .offset(int(offset))
                .limit(int(limit))
                .all()
            )
            items = [_event_view(row) for row in rows]
        finally:
            session.close()
        return {"success": True, "data": {"items": items, "count": len(items), "total": int(total)}}
    except HTTPException:
        raise


@router.get("/events/{event_id}")
async def get_event(event_id: str):
    """事件定义 + 最新 assessment 引用。"""
    try:
        eid = _clean_id(event_id, "event_id")
        session = get_session_factory()()
        try:
            row = _load_event(session, eid)
            latest = (
                session.query(HotEventAssessment.id)
                .filter(HotEventAssessment.event_id == eid)
                .order_by(HotEventAssessment.as_of_s.desc(), HotEventAssessment.revision.desc())
                .first()
            )
            latest_id = latest[0] if latest is not None else None
            return {"success": True, "data": _event_view(row, latest_assessment_id=latest_id)}
        finally:
            session.close()
    except HTTPException:
        raise


@router.patch("/events/{event_id}")
async def patch_event(event_id: str, request: EventPatchRequest):
    """CAS 更新事件（新规则版本 / 暂停）。

    ``expected_revision`` 不匹配 → **409**；事件不存在 → **404**。
    """
    try:
        eid = _clean_id(event_id, "event_id")
        if request.status is not None and request.status not in HOT_EVENT_STATUSES:
            raise _fail("invalid_event_status", 422, f"status 非法: {request.status}")
        _check_json_size(request.source_policy, "source_policy")
        _check_json_size(request.links, "links")

        session = get_session_factory()()
        try:
            row = _load_event(session, eid)
            if int(row.revision) != int(request.expected_revision):
                raise _fail("revision_conflict", 409, "expected_revision 不匹配")
            changes: Dict[str, Any] = {"revision": int(row.revision) + 1, "updated_s": _now()}
            if request.name is not None:
                changes["name"] = _clean_text(request.name, "name", max_len=200)
            if request.status is not None:
                changes["status"] = request.status
            if request.source_policy is not None:
                changes["source_policy"] = request.source_policy
                changes["current_rule_version"] = int(row.current_rule_version) + 1
            if request.links is not None:
                changes["links"] = request.links
            result = session.execute(
                update(HotEvent)
                .where(HotEvent.id == eid, HotEvent.revision == int(row.revision))
                .values(**changes)
            )
            if result.rowcount != 1:
                session.rollback()
                raise _fail("revision_conflict", 409, "并发更新冲突")
            session.commit()
            session.expire_all()
            fresh = _load_event(session, eid)
            return {"success": True, "data": _event_view(fresh)}
        finally:
            session.close()
    except HTTPException:
        raise
    except Exception:
        raise


# ===========================================================================
# 端点：发现
# ===========================================================================

@router.post("/events/{event_id}/discover/tasks")
async def start_discovery_task(event_id: str):
    """与后台共用 CAS 领取；有效 run 运行中返回 **409 + run_id**。

    §18.2：服务已装配但配置关闭时返回 **409 `discovery_disabled_by_config`**
    （明确「未启用」状态 + reason_code），**不再返回 503**、不编造成功；
    仅在服务确实未装配时才 503。
    """
    try:
        eid = _clean_id(event_id, "event_id")
        # §18.2：配置关闭 → 明确未启用状态，先于「服务未装配」判定。
        if not bool(_STATE.get("discovery_enabled", True)):
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "discovery_disabled_by_config",
                    "reason_code": "discovery_disabled_by_config",
                    "message": "事件发现未启用（EVENT_AUTO_DISCOVERY_ENABLED=false）",
                    "enabled": False,
                    "enable_key": "event_switches.auto_discovery_enabled",
                },
            )
        service = _STATE["discovery_service"]
        if service is None:
            raise _fail("discovery_service_unavailable", 503, "发现服务未装配")

        session = get_session_factory()()
        try:
            row = _load_event(session, eid)
            rule_version = int(row.current_rule_version)
            policy_hash = row.source_policy_hash or ""
        finally:
            session.close()

        from modules.hotspot.event_discovery_fence import (
            DiscoveryBudgetUnavailable,
            DiscoveryCapacityExceeded,
            DiscoveryInProgress,
            DiscoveryNotDue,
            DiscoveryStoreUnavailable,
        )

        try:
            start = await service.discover_event(
                eid, trigger="manual", rule_version=rule_version, source_policy_hash=policy_hash
            )
        except DiscoveryInProgress as exc:
            run_id = exc.extra.get("run_id")
            raise _fail("discovery_in_progress", 409, f"发现进行中: {run_id}") from exc
        except DiscoveryCapacityExceeded as exc:
            raise _fail("active_capacity_exceeded", 409, str(exc)) from exc
        except DiscoveryBudgetUnavailable as exc:
            raise _fail("discovery_budget_unavailable", 429, str(exc)) from exc
        except DiscoveryNotDue as exc:
            raise _fail("discovery_not_due", 409, str(exc)) from exc
        except DiscoveryStoreUnavailable as exc:
            raise _fail("discovery_store_unavailable", 503, str(exc)) from exc

        if not getattr(start, "started", False):
            raise _fail("discovery_in_progress", 409, f"发现进行中: {getattr(start, 'current_run_id', None)}")

        task_id = f"etask_{uuid.uuid4().hex[:16]}"
        _EVENT_TASKS[task_id] = {
            "kind": "discovery",
            "event_id": eid,
            "run_id": start.run_id,
            "started_s": _now(),
        }
        return {
            "success": True,
            "data": {
                "task_id": task_id,
                "status": "running",
                "event_id": eid,
                "run_id": start.run_id,
                "status_url": f"/api/hotspot/event-tasks/{task_id}",
                "persistent_run_url": f"/api/hotspot/event-discovery-runs/{start.run_id}",
            },
        }
    except HTTPException:
        raise


@router.get("/event-discovery-runs/{run_id}")
async def get_discovery_run(run_id: str):
    """只读持久发现结果（进程重启后仍可查看）。"""
    try:
        rid = _clean_id(run_id, "run_id")
        session = get_session_factory()()
        try:
            row = session.get(EventDiscoveryRun, rid)
            if row is None:
                raise _fail("discovery_run_not_found", 404, f"发现 run 不存在: {rid}")
            return {"success": True, "data": _run_view(row)}
        finally:
            session.close()
    except HTTPException:
        raise


@router.get("/event-tasks/{task_id}")
async def get_event_task(task_id: str):
    """通用任务读取：completed ≠ 数据有效；failed/cancelled/interrupted 统一 failed。"""
    try:
        tid = _clean_id(task_id, "task_id")
        task = _EVENT_TASKS.get(tid)
        if task is None:
            # 内存句柄重启不存在 → 404，并给可用的持久读取方式；不假装能恢复。
            raise HTTPException(
                status_code=404,
                detail={
                    "error_code": "task_handle_lost",
                    "message": "任务句柄不存在（可能已重启）；请用持久读取接口。",
                    "persistent_read": {
                        "discovery_run": "/api/hotspot/event-discovery-runs/{run_id}",
                        "assessment": "/api/hotspot/event-assessments/{assessment_id}",
                    },
                },
            )
        if task["kind"] == "assess":
            # 评估任务：completed + data_status（insufficient 是正常结果）。
            return {"success": True, "data": dict(task["result"])}

        run_id = task.get("run_id")
        session = get_session_factory()()
        try:
            row = session.get(EventDiscoveryRun, run_id) if run_id else None
        finally:
            session.close()
        if row is None:
            raise _fail("discovery_run_not_found", 404, f"发现 run 不存在: {run_id}")

        status = str(row.status)
        if status == "running":
            return {
                "success": True,
                "data": {"status": "running", "task_id": tid, "event_id": row.event_id, "run_id": run_id},
            }
        if status in ("completed", "partial"):
            # HTTP 任务 completed：run_status 表达 partial/completed。
            return {
                "success": True,
                "data": {
                    "status": "completed",
                    "task_id": tid,
                    "event_id": row.event_id,
                    "run_id": run_id,
                    "result": {"run_id": run_id, "run_status": status, "candidate_count": len(row.candidates or [])},
                },
            }
        # failed / cancelled / interrupted 统一映射 HTTP 任务 failed 并带原 reason_code。
        reason = row.error_code or status
        return {
            "success": True,
            "data": {
                "status": "failed",
                "task_id": tid,
                "event_id": row.event_id,
                "run_id": run_id,
                "reason_code": reason,
                "run_status": status,
            },
        }
    except HTTPException:
        raise


# ===========================================================================
# 端点：成员
# ===========================================================================

@router.get("/events/{event_id}/members")
async def list_event_members(event_id: str, status: Optional[str] = None):
    """成员列表：按 ``bvid`` 取最新 revision，可按 proposed/accepted/rejected 过滤。"""
    try:
        eid = _clean_id(event_id, "event_id")
        if status is not None and status not in MEMBER_STATUSES:
            raise _fail("invalid_member_status", 422, f"status 非法: {status}")
        session = get_session_factory()()
        try:
            _load_event(session, eid)
            rows = (
                session.query(HotEventMember)
                .filter(HotEventMember.event_id == eid)
                .order_by(HotEventMember.bvid.asc(), HotEventMember.revision.asc())
                .all()
            )
            latest: Dict[str, HotEventMember] = {}
            for row in rows:
                latest[row.bvid] = row  # 升序遍历 → 最后一个即最新 revision
            items = []
            for bvid, row in latest.items():
                if status is not None and row.status != status:
                    continue
                items.append(
                    {
                        "bvid": bvid,
                        "status": row.status,
                        "revision": int(row.revision),
                        "owner_mid": row.owner_mid,
                        "first_seen_s": int(row.first_seen_s),
                        "decision_at_s": int(row.decision_at_s),
                        "decision_source": row.decision_source,
                        "evidence": row.evidence,
                    }
                )
            by_status = {name: 0 for name in MEMBER_STATUSES}
            for row in latest.values():
                by_status[row.status] = by_status.get(row.status, 0) + 1
            return {"success": True, "data": {"items": items, "count": len(items), "by_status": by_status}}
        finally:
            session.close()
    except HTTPException:
        raise


@router.post("/events/{event_id}/members/decisions")
async def append_member_decisions(event_id: str, request: MemberDecisionsRequest):
    """批量 append 成员决定（CAS revision）。

    ``expected_revision`` 冲突 → 409；``bvid`` / ``status`` 非法 → 422。
    """
    try:
        eid = _clean_id(event_id, "event_id")
        if len(request.decisions) > MAX_MEMBER_DECISIONS:
            raise _fail("too_many_decisions", 422, f"decisions 超过 {MAX_MEMBER_DECISIONS}")
        for decision in request.decisions:
            if decision.status not in MEMBER_STATUSES:
                raise _fail("invalid_member_status", 422, f"status 非法: {decision.status}")
            if not _BVID_RE.match((decision.bvid or "").strip()):
                raise _fail("invalid_bvid", 422, f"bvid 形状非法: {decision.bvid}")
            _check_json_size(decision.evidence, "evidence")

        session = get_session_factory()()
        try:
            event = _load_event(session, eid)
            if int(event.revision) != int(request.expected_revision):
                raise _fail("revision_conflict", 409, "expected_revision 不匹配")

            # 每个 bvid 的当前最新 revision。
            existing = (
                session.query(HotEventMember)
                .filter(HotEventMember.event_id == eid)
                .order_by(HotEventMember.bvid.asc(), HotEventMember.revision.asc())
                .all()
            )
            latest: Dict[str, int] = {}
            for row in existing:
                latest[row.bvid] = int(row.revision)

            now = _now()
            new_event_revision = int(event.revision) + 1
            appended: List[dict] = []
            seen: set = set()
            for decision in request.decisions:
                bvid = decision.bvid.strip()
                if bvid in seen:  # 同一批重复 bvid → 去重
                    continue
                seen.add(bvid)
                revision = latest.get(bvid, 0) + 1
                session.add(
                    HotEventMember(
                        event_id=eid,
                        bvid=bvid,
                        revision=revision,
                        status=decision.status,
                        first_seen_s=now,
                        decision_at_s=now,
                        rule_version=int(event.current_rule_version),
                        decision_source="manual",
                        evidence=decision.evidence,
                    )
                )
                latest[bvid] = revision
                appended.append({"bvid": bvid, "status": decision.status, "revision": revision})

            result = session.execute(
                update(HotEvent)
                .where(HotEvent.id == eid, HotEvent.revision == int(event.revision))
                .values(revision=new_event_revision, updated_s=now)
            )
            if result.rowcount != 1:
                session.rollback()
                raise _fail("revision_conflict", 409, "并发更新冲突")
            session.commit()
            return {
                "success": True,
                "data": {"event_id": eid, "revision": new_event_revision, "appended": appended},
            }
        finally:
            session.close()
    except HTTPException:
        raise


# ===========================================================================
# 端点：归属预览（第三批 i · §15.1-②，纯计算只读）
# ===========================================================================

def _policy_dict(event: HotEvent) -> Dict[str, Any]:
    """读取事件 ``source_policy`` 整对象（非 dict 时返回空 dict，不臆造）。"""
    return event.source_policy if isinstance(event.source_policy, dict) else {}


def _event_definition(event: HotEvent, *, entity_anchors: Sequence[str] = ()) -> EventDefinition:
    """由事件行构造归属用 :class:`EventDefinition`（只读，不改库、不发网络）。

    规则字面量直接来自 ``source_policy``；当草稿事件尚未配置 ``include_rules``
    而用户提供了 ``entity_anchors`` 时，用锚点合成最小实体组 / 锚点组，保证
    “示例兜底”能算出来（§15.1-②）。

    Args:
        event: 事件行。
        entity_anchors: 用户输入的实体 / 事件锚点。

    Returns:
        EventDefinition: 归属规则定义。
    """
    policy = _policy_dict(event)
    include = policy.get("include_rules")
    include = dict(include) if isinstance(include, dict) else {}
    anchors = [str(a).strip() for a in entity_anchors if str(a).strip()]
    if not include.get("entity_groups") and anchors:
        include["entity_groups"] = [[anchors[0]]]
        include["anchor_groups"] = [[anchors[1] if len(anchors) > 1 else str(event.name or anchors[0])]]
        if not include.get("aliases"):
            include["aliases"] = anchors
    kind = policy.get("event_kind", "other")
    if kind not in EVENT_KINDS:
        kind = "other"
    return EventDefinition(
        canonical_name=str(event.name or event.id),
        entity_scope=tuple(policy.get("entity_scope") or ()),
        event_kind=kind,
        include_rules=include,
        exclude_rules=policy.get("exclude_rules") or {},
        source_scope=tuple(policy.get("source_scope") or ()),
        event_id=event.id,
    )


def _load_event_definitions(session: Session, *, limit: int = 50) -> List[EventDefinition]:
    """读取全部事件定义（仅用于跨事件冲突判定；只读）。"""
    rows = session.query(HotEvent).order_by(HotEvent.created_s.desc()).limit(int(limit)).all()
    return [_event_definition(row) for row in rows]


def _first_literal(value: Any) -> Optional[str]:
    """从组结构 / 列表里取第一个字面量（用于生成示例文本；不执行用户 regex）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    for item in value:
        if isinstance(item, str) and item:
            return item
        if isinstance(item, (list, tuple)):
            for sub in item:
                if isinstance(sub, str) and sub:
                    return sub
    return None


def _preview_example_samples(
    event_def: EventDefinition, other_defs: Sequence[EventDefinition] = ()
) -> List[Dict[str, Any]]:
    """用规则字面量合成示例三分类的候选文本（无发现结果时的兜底，绝不空着）。

    冲突示例会尝试拼接“另一个同实体事件”的锚点，使它真的同时命中 ≥2 个事件；
    没有可构造的跨事件条件时仍保留该示例并诚实标 ``would_hit=false``。
    """
    include = event_def.include_rules or {}
    exclude = event_def.exclude_rules or {}
    entity = _first_literal(include.get("entity_groups")) or _first_literal(include.get("aliases"))
    anchor = _first_literal(include.get("anchor_groups")) or event_def.canonical_name
    exclude_term = (
        _first_literal(exclude.get("same_name_ambiguity"))
        or _first_literal(exclude.get("old_versions"))
        or _first_literal(exclude.get("unrelated_terms"))
    )
    match_text = " ".join(t for t in (entity, anchor) if t).strip() or event_def.canonical_name
    samples: List[Dict[str, Any]] = [
        {"text": match_text, "example_category": "would_match", "basis": "entity+anchor"},
    ]
    if exclude_term:
        samples.append({"text": f"{match_text} {exclude_term}", "example_category": "would_exclude", "basis": "exclude_literal"})
    else:
        samples.append({"text": match_text, "example_category": "would_exclude", "basis": "no_exclude_rule"})

    conflict_text = match_text
    for other in other_defs:
        other_include = other.include_rules or {}
        other_entity = _first_literal(other_include.get("entity_groups")) or _first_literal(other_include.get("aliases"))
        other_anchor = _first_literal(other_include.get("anchor_groups"))
        if not other_anchor or not other_entity:
            continue
        if entity and other_entity != entity:
            continue
        if other_anchor == anchor:
            continue
        conflict_text = " ".join(t for t in (entity or other_entity, anchor, other_anchor) if t)
        break
    samples.append({"text": conflict_text, "example_category": "would_conflict", "basis": "cross_event"})
    return samples


def _classify_preview(result: Any) -> str:
    """把单事件 :class:`MatchResult` 归到三分类之一（互斥，排除优先）。"""
    if result is None:
        return "ignored"
    if getattr(result, "excluded", False):
        return "would_exclude"
    if getattr(result, "conflict", False):
        return "would_conflict"
    if getattr(result, "included", False):
        return "would_match"
    return "ignored"


def _preview_entry(*, result: Any, sample: Dict[str, Any], example: bool, event_id: str) -> Dict[str, Any]:
    """把一次预览判定转成可展示条目（含命中理由，不写库）。"""
    hit_keywords = [
        hit.get("alias") or hit.get("keyword")
        for bucket in (
            getattr(result, "entity_hits", []) or [],
            getattr(result, "anchor_hits", []) or [],
            getattr(result, "exclude_hits", []) or [],
        )
        for hit in bucket
    ]
    return {
        "event_id": event_id,
        "bvid": sample.get("bvid"),
        "title": str(sample.get("title") or sample.get("text") or ""),
        "example": bool(example),
        "example_category": sample.get("example_category"),
        "basis": sample.get("basis"),
        "category": _classify_preview(result),
        "decision": getattr(result, "decision", "ignored"),
        "would_hit": bool(getattr(result, "included", False)),
        "excluded": bool(getattr(result, "excluded", False)),
        "conflict": bool(getattr(result, "conflict", False)),
        "reason_codes": list(getattr(result, "reason_codes", []) or []),
        "hit_keywords": [k for k in hit_keywords if k],
    }


@router.post("/events/{event_id}/members/preview")
async def preview_event_members(event_id: str, request: MemberPreviewRequest):
    """§15.1-②：只读预览会命中 / 冲突 / 被排除的候选（纯计算）。

    硬口径：
    - **纯计算**：不落库、不写 revision、不发任何网络请求；
    - 预览结果**不可被当作已提交决定**，真正的成员写入仍走 ``members/decisions``
      的 CAS 与围栏；
    - 没有发现结果时，用规则字面量 + 用户输入实体锚点给出**示例三分类**，不返回空。
    """
    try:
        eid = _clean_id(event_id, "event_id")
        if len(request.samples) > MAX_LIST_ITEMS:
            raise _fail("too_many_preview_samples", 422, f"samples 超过 {MAX_LIST_ITEMS}")
        _check_list_size(request.entity_anchors, "entity_anchors")

        session = get_session_factory()()
        try:
            event = _load_event(session, eid)
            event_revision = int(event.revision)
            rule_version = int(event.current_rule_version)
            this_def = _event_definition(event, entity_anchors=request.entity_anchors)
            defs = _load_event_definitions(session)
        finally:
            session.close()

        # 本事件必须参与判定（默认列表按创建时间截断时补上）。
        if all(d.event_id != eid for d in defs):
            defs.append(this_def)

        samples: List[Dict[str, Any]] = []
        for item in request.samples:
            bvid = (item.bvid or "").strip()
            if bvid and not _BVID_RE.match(bvid):
                raise _fail("invalid_bvid", 422, f"bvid 形状非法: {item.bvid}")
            _check_json_size(list(item.tags), "tags")
            samples.append(
                {
                    "bvid": bvid or None,
                    "title": item.title,
                    "tags": list(item.tags),
                    "summary": item.summary,
                    "comment_terms": list(item.comment_terms),
                    "example_category": None,
                    "basis": "user_sample",
                }
            )
        examples_generated = not samples
        if examples_generated:
            samples = _preview_example_samples(this_def, [d for d in defs if d.event_id != eid])

        buckets: Dict[str, List[dict]] = {"would_match": [], "would_conflict": [], "would_exclude": []}
        example_rows: List[dict] = []
        for sample in samples:
            evidence = VideoEvidence(
                bvid=str(sample.get("bvid") or "BV1PREVIEW00"),
                title=str(sample.get("title") or sample.get("text") or ""),
                tags=tuple(sample.get("tags") or ()),
                summary=str(sample.get("summary") or ""),
                comment_terms=tuple(sample.get("comment_terms") or ()),
            )
            resolution = evaluate_events(
                evidence, defs, rule_version=rule_version, strict_auto=bool(request.strict_auto)
            )
            result = resolution.results.get(eid)
            if result is None:
                continue
            entry = _preview_entry(result=result, sample=sample, example=examples_generated, event_id=eid)
            example_rows.append(entry)
            category = entry["category"]
            if category in buckets:
                buckets[category].append(entry)

        return {
            "success": True,
            "data": {
                "event_id": eid,
                "read_only": True,
                "preview_only": True,
                "event_revision": event_revision,
                "rule_literals": {
                    "entity_scope": list(this_def.entity_scope),
                    "include_rules": this_def.include_rules,
                    "exclude_rules": this_def.exclude_rules,
                },
                "would_match": buckets["would_match"],
                "would_conflict": buckets["would_conflict"],
                "would_exclude": buckets["would_exclude"],
                "examples": example_rows,
                "examples_generated": examples_generated,
                "note": "只读预览，不可作为已提交决定；成员写入仍走 members/decisions 的 CAS 与围栏。",
            },
        }
    except HTTPException:
        raise


# ===========================================================================
# 端点：预算与采样名额（第三批 i · §15.1-③，只读、从既有服务状态暴露）
# ===========================================================================

def _quota_limit_view(category: str, source_key: str) -> Dict[str, Any]:
    """从 ``config/budget.yaml`` 读取某类别上限（配额数字的唯一来源）。"""
    try:
        from core.request_budget import load_quota_limits

        limits = load_quota_limits()
    except Exception:  # noqa: BLE001 - 账本不可用按“未提供”处理，不臆造
        return {"available": False, "reason_code": "budget_config_unavailable"}
    if limits is None:
        return {"available": False, "reason_code": "budget_config_unavailable"}
    limit = limits.limit_of(category)
    if limit is None:
        return {"available": False, "reason_code": "budget_category_missing", "category": category}
    return {"available": True, "value": int(limit), "unit": "requests/24h", "source": source_key}


def _quota_remaining_view(category: str, source_key: str, now_s: int) -> Dict[str, Any]:
    """某类别“当前可授予名额” = 上限 − 滚动窗口已用（配置键 + 对账结果，可溯源）。"""
    base = _quota_limit_view(category, source_key)
    if not base.get("available"):
        return base
    try:
        from core import quota_store
        from core.request_budget import load_quota_limits

        limits = load_quota_limits()
        domain = limits.category_domains.get(category) if limits is not None else None
        if not domain:
            return {"available": False, "reason_code": "budget_domain_missing", "category": category}
        used = int(quota_store.used(domain, category, int(now_s)))
    except Exception:  # noqa: BLE001 - 对账状态不可用如实标未提供
        return {"available": False, "reason_code": "quota_store_unavailable"}
    return {
        "available": True,
        "value": max(0, int(base["value"]) - used),
        "used": used,
        "limit": int(base["value"]),
        "unit": "requests/24h",
        "source": source_key + " + core/quota_store.used",
    }


@router.get("/events/{event_id}/budget")
async def get_event_budget(event_id: str):
    """§15.1-③：从**既有服务状态**暴露预算 / 候选 / 待确认 / 采样名额（只读）。

    口径（不新建第二套采集、不改 ``watch_service`` 主循环语义）：
    - 候选 = 本轮发现候选数（读最新 ``EventDiscoveryRun``）；
    - 待确认 = proposed 成员数（读 ``hot_event_members`` 最新 revision）；
    - 名额 = 当前可授予采样名额（``budget.yaml`` 上限 − ``quota_store`` 已用）；
    - 每个数值带 ``source`` 可溯源；拿不到的显式 ``available=false`` + ``reason_code``。
    """
    try:
        eid = _clean_id(event_id, "event_id")
        now_s = _now()
        session = get_session_factory()()
        try:
            _load_event(session, eid)
            latest_run = (
                session.query(EventDiscoveryRun)
                .filter(EventDiscoveryRun.event_id == eid)
                .order_by(EventDiscoveryRun.started_s.desc())
                .first()
            )
            member_rows = (
                session.query(HotEventMember)
                .filter(HotEventMember.event_id == eid)
                .order_by(HotEventMember.bvid.asc(), HotEventMember.revision.asc())
                .all()
            )
            latest_status: Dict[str, str] = {}
            for row in member_rows:
                latest_status[row.bvid] = row.status
            proposed = sum(1 for status in latest_status.values() if status == "proposed")
            run_id = latest_run.id if latest_run is not None else None
            candidate_count = len(latest_run.candidates or []) if latest_run is not None else 0
        finally:
            session.close()

        if run_id is not None:
            candidate_view: Dict[str, Any] = {
                "available": True,
                "value": int(candidate_count),
                "unit": "candidates",
                "source": f"event_discovery_runs:{run_id}",
            }
        else:
            candidate_view = {
                "available": False,
                "reason_code": "no_discovery_run",
                "message": "尚无发现结果（先执行“发现并观察”）",
            }

        return {
            "success": True,
            "data": {
                "event_id": eid,
                "budget": {
                    "discovery_requests_per_24h": _quota_limit_view(
                        "discovery", "config/budget.yaml:quota.categories.discovery.limit"
                    ),
                    "watch_samples_per_24h": _quota_limit_view(
                        "watch", "config/budget.yaml:quota.categories.watch.limit"
                    ),
                },
                "candidate_count": candidate_view,
                "pending_confirm": {
                    "available": True,
                    "value": int(proposed),
                    "unit": "members",
                    "source": "hot_event_members:proposed(latest revision)",
                },
                "sampling_quota": _quota_remaining_view(
                    "watch",
                    "config/budget.yaml:quota.categories.watch.limit",
                    now_s,
                ),
                "note": "数值来自既有配置键与既有服务状态；拿不到的显式标未提供，不臆造。",
            },
        }
    except HTTPException:
        raise


# ===========================================================================
# 端点：评估
# ===========================================================================

@router.post("/events/{event_id}/assess/tasks")
async def start_assessment_task(event_id: str, request: AssessTaskRequest):
    """固定 ``as_of``，后台计算 assessment（completed ≠ 数据有效）。"""
    try:
        eid = _clean_id(event_id, "event_id")
        if request.window_kind not in WINDOW_KINDS:
            raise _fail("invalid_window_kind", 422, f"window_kind 非法: {request.window_kind}")
        service = _STATE["aggregation_service"]
        if service is None:
            raise _fail("aggregation_service_unavailable", 503, "评估服务未装配")

        session = get_session_factory()()
        try:
            event = _load_event(session, eid)
            rule_version = int(event.current_rule_version)
        finally:
            session.close()

        as_of_s = int(request.as_of_s) if request.as_of_s is not None else _now()
        policy = EventPolicy()
        if request.window_kind == "daily24h":
            result = service.run_daily(eid, as_of_s=as_of_s)
        else:
            result = service.run_early(eid, as_of_s=as_of_s, fast_panel_bvids=[])

        assessment = service.persist_assessment(
            eid,
            result,
            window_kind=request.window_kind,
            rule_version=rule_version,
            policy_version=result.get("policy_version"),
        )
        data_status = assessment.status
        reason_codes = list((assessment.interpretation or {}).get("reason_codes") or [])
        next_action = "continue_watch" if data_status in ("collecting", "insufficient", "partial") else "review_opportunity"

        task_id = f"etask_{uuid.uuid4().hex[:16]}"
        payload = {
            "status": "completed",
            "task_id": task_id,
            "result": {
                "assessment_id": assessment.id,
                "data_status": data_status,
                "reason_codes": reason_codes,
                "next_action": next_action,
            },
        }
        _EVENT_TASKS[task_id] = {"kind": "assess", "event_id": eid, "result": payload}
        return {"success": True, "data": payload}
    except HTTPException:
        raise


@router.get("/events/{event_id}/assessments")
async def list_event_assessments(event_id: str, window_kind: Optional[str] = None, limit: int = 50):
    """历史评估快照列表。"""
    try:
        eid = _clean_id(event_id, "event_id")
        if window_kind is not None and window_kind not in WINDOW_KINDS:
            raise _fail("invalid_window_kind", 422, f"window_kind 非法: {window_kind}")
        if limit < 1 or limit > MAX_PAGE_LIMIT:
            raise _fail("invalid_limit", 422, f"limit 必须在 1..{MAX_PAGE_LIMIT}")
        session = get_session_factory()()
        try:
            _load_event(session, eid)
            query = session.query(HotEventAssessment).filter(HotEventAssessment.event_id == eid)
            if window_kind is not None:
                query = query.filter(HotEventAssessment.window_kind == window_kind)
            rows = (
                query.order_by(HotEventAssessment.as_of_s.desc(), HotEventAssessment.revision.desc())
                .limit(int(limit))
                .all()
            )
            items = [_assessment_view(row) for row in rows]
        finally:
            session.close()
        return {"success": True, "data": {"items": items, "count": len(items)}}
    except HTTPException:
        raise


@router.get("/event-assessments/{assessment_id}")
async def get_event_assessment(assessment_id: str):
    """冻结事实（可用于推荐引用）。"""
    try:
        aid = _clean_id(assessment_id, "assessment_id")
        session = get_session_factory()()
        try:
            row = session.get(HotEventAssessment, aid)
            if row is None:
                raise _fail("assessment_not_found", 404, f"评估不存在: {aid}")
            return {"success": True, "data": _assessment_view(row)}
        finally:
            session.close()
    except HTTPException:
        raise


# ===========================================================================
# 端点：机会
# ===========================================================================

def _frozen_event_ids(run: OpportunityRun) -> List[str]:
    """取机会 run 冻结的事件顺序（candidates 优先，其次 result）。"""
    ordered: List[str] = []
    candidates = run.candidates if isinstance(run.candidates, (list, tuple)) else []
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("event_id"):
            eid = str(candidate["event_id"])
            if eid not in ordered:
                ordered.append(eid)
    if not ordered:
        result = run.result if isinstance(run.result, dict) else {}
        for key in ("ranked_event_ids", "not_executable_event_ids"):
            for eid in result.get(key) or []:
                text = str(eid)
                if text not in ordered:
                    ordered.append(text)
    return ordered


@router.post("/opportunities/tasks")
async def create_opportunity_task(request: OpportunityTaskRequest):
    """CreatorBrief + event ids → 生成冻结 OpportunityRun。"""
    try:
        if len(request.event_ids) > int(_STATE["max_event_selection"]):
            raise _fail("too_many_events", 422, f"事件上限 {_STATE['max_event_selection']}")
        _check_list_size(request.event_ids, "event_ids")
        _check_json_size(request.creator_brief, "creator_brief")
        if not request.event_ids:
            raise _fail("empty_event_ids", 422, "event_ids 不能为空")

        try:
            brief = CreatorBrief.from_dict(request.creator_brief)
        except BriefValidationError as exc:
            raise _fail("invalid_creator_brief", 422, str(exc)) from exc

        session = get_session_factory()()
        try:
            for eid in request.event_ids:
                if session.get(HotEvent, _clean_id(str(eid), "event_id")) is None:
                    raise _fail("event_not_found", 404, f"事件不存在: {eid}")
        finally:
            session.close()

        as_of_s = int(request.as_of_s) if request.as_of_s is not None else _now()
        policy = EventPolicy()
        assessment_ids: List[str] = []
        # 收集每个事件的最新评估作为引用（无评估时不伪造）。
        session = get_session_factory()()
        try:
            for eid in request.event_ids:
                row = (
                    session.query(HotEventAssessment.id)
                    .filter(HotEventAssessment.event_id == eid)
                    .order_by(HotEventAssessment.as_of_s.desc())
                    .first()
                )
                if row is not None:
                    assessment_ids.append(row[0])
        finally:
            session.close()

        facts_items = [_event_facts(get_session_factory, eid, as_of_s) for eid in request.event_ids]
        built = build_opportunity_result(facts_items, brief, as_of_s, policy=policy)
        fingerprint = request_fingerprint(
            brief, assessment_ids=assessment_ids, request_as_of_s=as_of_s, policy_version=policy.policy_version
        )
        run = get_or_create_opportunity_run(
            get_repository(),
            brief=brief,
            request_fingerprint=fingerprint,
            candidates=built["candidates"],
            result=built["result"],
            assessment_ids=assessment_ids,
            policy_version=policy.policy_version,
            now_s=as_of_s,
        )
        return {
            "success": True,
            "data": {
                "opportunity_run_id": run.id,
                "revision": int(run.revision),
                "request_fingerprint": run.request_fingerprint,
                "policy_version": run.policy_version,
            },
        }
    except HTTPException:
        raise


def _event_facts(session_factory: Callable[[], Callable[[], Session]], event_id: str, as_of_s: int) -> dict:
    """组装单事件事实包（缺则如实为 None，不伪造指标）。"""
    session = session_factory()()
    try:
        row = session.get(HotEvent, event_id)
        latest = (
            session.query(HotEventAssessment)
            .filter(HotEventAssessment.event_id == event_id)
            .order_by(HotEventAssessment.as_of_s.desc())
            .first()
        )
    finally:
        session.close()
    return {
        "event_id": event_id,
        "entities": [event_id],
        "domains": [],
        "daily": (latest.metrics or {}) if latest is not None else {},
        "early": {},
        "discovery": {},
        "evidence_refs": [],
    }


@router.get("/opportunities/{run_id}")
async def get_opportunity(run_id: str):
    """推荐、``rank_key``、证据、限制。"""
    try:
        rid = _clean_id(run_id, "run_id")
        session = get_session_factory()()
        try:
            row = session.get(OpportunityRun, rid)
            if row is None:
                raise _fail("opportunity_run_not_found", 404, f"机会运行不存在: {rid}")
        finally:
            session.close()
        return {
            "success": True,
            "data": {
                "opportunity_run_id": row.id,
                "revision": int(row.revision),
                "policy_version": row.policy_version,
                "request_fingerprint": row.request_fingerprint,
                "creator_brief": row.creator_brief,
                "assessment_ids": row.assessment_ids,
                "candidates": row.candidates,
                "result": row.result,
                "feedback": row.feedback or [],
            },
        }
    except HTTPException:
        raise


@router.post("/opportunities/{run_id}/feedback")
async def append_feedback(run_id: str, request: FeedbackRequest):
    """采用 / 拒绝 / 发布 / 结果：append 且幂等。

    先查 ``feedback_id``：同内容即便 ``expected_revision`` 已旧也返回已有记录（不 409）；
    同 id 变内容 → 409；仅新 feedback 才校验 ``expected_revision`` 并追加 CAS。
    """
    try:
        rid = _clean_id(run_id, "run_id")
        session = get_session_factory()()
        try:
            run = session.get(OpportunityRun, rid)
            if run is None:
                raise _fail("opportunity_run_not_found", 404, f"机会运行不存在: {rid}")

            # 1) 校验 event_id 确在 run 内、topic_id 属于该 run（有 saved_ids 时）。
            frozen = _frozen_event_ids(run)
            if frozen and request.event_id.strip() not in frozen:
                raise _fail("event_not_in_run", 422, f"event_id 不属于该 run: {request.event_id}")
            saved_ids = []
            if isinstance(run.result, dict):
                saved_ids = [str(item) for item in (run.result.get("saved_ids") or [])]
            if saved_ids and str(request.topic_id).strip() not in saved_ids:
                raise _fail("topic_not_in_run", 422, f"topic_id 不属于该 run: {request.topic_id}")

            payload = _normalize_feedback_payload(request, run)
            payload_hash = _feedback_payload_hash(payload)

            # 2) 先查 feedback_id 幂等：同内容 → 返回已有记录（不 409）。
            existing_feedback = list(run.feedback or [])
            for entry in existing_feedback:
                if isinstance(entry, dict) and str(entry.get("feedback_id")) == request.feedback_id:
                    if entry.get("payload_hash") == payload_hash:
                        return {
                            "success": True,
                            "data": {
                                "opportunity_run_id": rid,
                                "feedback_id": request.feedback_id,
                                "revision": int(run.revision),
                                "idempotent_replay": True,
                                "feedback": entry,
                            },
                        }
                    raise _fail("feedback_id_conflict", 409, "同 feedback_id 内容不一致")

            # 3) 仅新 feedback 才校验 expected_revision 并 CAS 追加。
            if int(run.revision) != int(request.expected_revision):
                raise _fail("feedback_revision_conflict", 409, "expected_revision 不匹配")

            now = _now()
            new_revision = int(run.revision) + 1
            entry = dict(payload)
            entry.update(
                {
                    "feedback_id": request.feedback_id,
                    "payload_hash": payload_hash,
                    "appended_s": now,
                    "revision": new_revision,
                }
            )
            new_feedback = existing_feedback + [entry]

            result = session.execute(
                update(OpportunityRun)
                .where(OpportunityRun.id == rid, OpportunityRun.revision == int(run.revision))
                .values(feedback=new_feedback, revision=new_revision)
            )
            if result.rowcount != 1:
                session.rollback()
                raise _fail("feedback_revision_conflict", 409, "并发反馈冲突")

            # 4) Topic 状态同步（同事务）：原 Topic 只有 pending/adopted/published；
            #    rejected 只写 OpportunityRun.feedback，不给旧 Topic.status 加新值。
            topic_synced = _sync_topic_status(session, request.topic_id, payload["kind"])
            session.commit()
            return {
                "success": True,
                "data": {
                    "opportunity_run_id": rid,
                    "feedback_id": request.feedback_id,
                    "revision": new_revision,
                    "idempotent_replay": False,
                    "topic_status_synced": topic_synced,
                    "verification_status": payload["verification_status"],
                    "feedback": entry,
                },
            }
        finally:
            session.close()
    except HTTPException:
        raise


def _sync_topic_status(session: Session, topic_id: str, kind: str) -> Optional[str]:
    """同事务同步旧 ``Topic.status``（只在 kind 属于原枚举时）。

    Args:
        session: 当前事务会话。
        topic_id: 旧 Topic 主键（数字字符串）。
        kind: 反馈类型。

    Returns:
        str | None: 写入的新状态；未同步返回 None（rejected 只进 feedback）。
    """
    if kind not in ("adopted", "published"):
        return None
    if not str(topic_id).isdigit():
        return None
    row = session.get(Topic, int(topic_id))
    if row is None:
        return None
    row.status = kind
    return kind


__all__ = [
    "configure",
    "reset_state",
    "get_session_factory",
    "get_repository",
    "get_clock",
    "create_event",
    "list_events",
    "get_event",
    "patch_event",
    "start_discovery_task",
    "get_discovery_run",
    "get_event_task",
    "list_event_members",
    "append_member_decisions",
    "preview_event_members",
    "get_event_budget",
    "start_assessment_task",
    "list_event_assessments",
    "get_event_assessment",
    "create_opportunity_task",
    "get_opportunity",
    "append_feedback",
]
