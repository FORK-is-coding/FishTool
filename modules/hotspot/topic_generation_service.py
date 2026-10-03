"""FishTool 04 · 第三批 f：生成账本与幂等编排（TopicGenerationService / TopicGenerationStore）。

依据：``FishTool_04_话题级热点研判与选题决策_02补充执行案(1).md`` §11.6 / §11.7（L988-1145）
与 3f 执行规格 §3。

钉死口径（逐条实现，不做发挥）：

- **查/领键发生在 TagCloud 与 LLM 调用之前**：``claim_generation`` 先 INSERT 主键/request_hash/
  state=running/随机 lease_token/lease_until_s，并发唯一冲突 rollback 后重读既有行；
  拿到 claim 的一方才可访问 TagCloud / LLM；
- 同 key 同 hash：``completed`` 返存好的完整响应 / ``running`` 返 202 / ``failed|cancelled|interrupted``
  返终态错误；**第一版不自动重启同一终态键**；同 key 不同 hash → 409 ``generation_key_conflict``；
- event 模式（传 ``opportunity_run_id``）**强制要键**；只给 ``selected_event_ids`` 没有 ``run_id`` → 422；
  旧 tag_only 无键路径保留兼容并标 ``idempotency='legacy_unprotected'``；
- 完成事务：条件 UPDATE 的 rowcount 必须=1，否则不插任何 Topic；``_insert_topics`` 必须 flush-only；
  Topic 批次与账本 result **一起提交或一起回滚**；响应构造/序列化失败同样回滚；
- COMMIT 返回异常 → 先独立短只读查已提交状态；查不到返 503 并保留同 key，不猜失败重生；
- lease=300s / 总 deadline=120s；**数据库事务不包网络 await**；超时或 lease 过期标 ``interrupted``
  并清 token，不把同 key 重领给另一 worker；
- ``recover_expired_generation_runs()`` 有界后台恢复；GET **严格只读不落库**；
  ``completed`` 优先于 lease 状态。

本模块**不接任何 HTTP 路由**（归 3g），只做服务端编排与账本。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.database import get_session
from core.database.hot_event_repository import HotEventRepository
from core.database.models_hot_event import OpportunityRun, TopicGenerationRun
from core.logger import get_logger
from modules.hotspot.events.opportunity import EXECUTABLE_ACTIONS
from modules.hotspot.topic_generator import (
    CONTEXT_SCHEMA_VERSION,
    GENERATION_MODE_EVENT,
    GENERATION_MODE_TAG_ONLY,
    TopicGenerator,
)

logger = get_logger(__name__)

#: 账本 schema 版本（进入 payload 的 ``request_schema_version`` 与 ``schema_version`` 列）。
GENERATION_SCHEMA_VERSION: int = 1
#: 规范化请求的 schema 版本（§3.2 固定值）。
GENERATION_REQUEST_SCHEMA_VERSION: int = 1
#: 默认租约（秒）。
DEFAULT_GENERATION_LEASE_SECONDS: int = 300
#: 默认总 deadline（秒）。
DEFAULT_GENERATION_DEADLINE_SECONDS: int = 120
#: 单次后台恢复最多处理的行数（“有界”）。
DEFAULT_RECOVERY_LIMIT: int = 200
#: ``count`` 允许范围。
MIN_GENERATION_COUNT: int = 1
MAX_GENERATION_COUNT: int = 20
#: 合法 ``context_mode``。
CONTEXT_MODES: tuple = ("current", "historical")

#: claim 种类。
CLAIM_ACQUIRED: str = "acquired"
CLAIM_COMPLETED: str = "completed"
CLAIM_RUNNING: str = "running"
CLAIM_TERMINAL: str = "terminal_error"
CLAIM_LEGACY: str = "legacy"

#: BVID 形状（用于从事件证据里提取“可打开引用”）。
_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{8,}$")


# ===========================================================================
# 异常
# ===========================================================================

class GenerationError(Exception):
    """生成编排错误基类（消息/错误码稳定，便于调用方断言与映射 HTTP）。"""

    code: str = "generation_error"

    def __init__(self, message: str = "", *, code: Optional[str] = None) -> None:
        self.code = code or type(self).code
        super().__init__(message or self.code)


class GenerationValidationError(GenerationError):
    """请求校验失败（对应 HTTP 422）。"""

    code = "generation_request_invalid"


class GenerationKeyConflict(GenerationError):
    """同一 generation_request_id 但内容 hash 不同（对应 HTTP 409）。"""

    code = "generation_key_conflict"


class GenerationTerminalError(GenerationError):
    """同 key 已是终态（failed/cancelled/interrupted），不自动重启。"""

    code = "generation_terminal"

    def __init__(self, error_code: Optional[str] = None) -> None:
        self.error_code = error_code or "generation_terminal"
        super().__init__(self.error_code, code=self.error_code)


class GenerationOwnershipLost(GenerationError):
    """账本 ownership 失效（token 不匹配 / 已被 completion 改状态 / lease 过期）。"""

    code = "generation_claim_not_owned"


class GenerationUnavailable(GenerationError):
    """完成状态未知（COMMIT 异常且无法独立查询）——返 503 并保留同 key。"""

    code = "generation_state_unknown"


class GenerationTimeoutError(GenerationError):
    """超过总 deadline（已标 interrupted 并清 token）。"""

    code = "generation_deadline_exceeded"


class GenerationConfigError(GenerationError):
    """生成相关配置非法（启动即报错，不静默换默认值）。"""

    code = "generation_config_invalid"


# ===========================================================================
# 请求 / claim 数据结构
# ===========================================================================

@dataclass(frozen=True)
class GenerationRequest:
    """一次生成请求（web 层 pydantic 模型映射到此，本批不接路由）。

    Attributes:
        direction: 创作方向。
        zone_name: 分区名。
        count: **总题数**（1—20）。
        use_llm: 是否使用 LLM。
        opportunity_run_id: 关联机会运行 ID（event 模式）。
        selected_event_ids: 选中事件 ID（event 模式；tag_only 忽略）。
        generation_request_id: 客户端 UUID 幂等键（event 模式必填）。
        context_mode: ``current`` / ``historical``。
    """

    direction: str
    zone_name: str
    count: int = 10
    use_llm: bool = True
    opportunity_run_id: Optional[str] = None
    selected_event_ids: Optional[Sequence[str]] = None
    generation_request_id: Optional[str] = None
    context_mode: str = "current"


@dataclass
class GenerationClaim:
    """一次 claim 的结果。

    Attributes:
        kind: ``acquired/completed/running/terminal_error/legacy``。
        id: 账本 ID（legacy 为 ``None``）。
        request_hash: 规范化内容 hash。
        lease_token: 本次领取的随机 token。
        request_payload: 规范化请求。
        context_snapshot: 首次领取冻结的 context（claim 时通常为 None）。
        saved_response: 已完成账本里存好的完整响应。
        error_code: 终态错误码。
        effective_state: 计算出的有效状态（``running`` 且 lease 过期 → ``interrupted``）。
        acquired_s / lease_until_s: 领取时刻 / 租约到期时刻。
    """

    kind: str
    id: Optional[str]
    request_hash: Optional[str] = None
    lease_token: Optional[str] = None
    request_payload: Optional[dict] = None
    context_snapshot: Optional[dict] = None
    saved_response: Optional[dict] = None
    error_code: Optional[str] = None
    effective_state: Optional[str] = None
    acquired_s: Optional[int] = None
    lease_until_s: Optional[int] = None


# ===========================================================================
# 小工具
# ===========================================================================

def _sha256_payload(payload: Mapping[str, Any]) -> str:
    """规范化 payload → UTF-8 SHA-256（§3.2 固定算法）。"""
    blob = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _iso_from_epoch(epoch_s: int) -> str:
    """epoch 秒 → 本地 ISO 字符串（仅用于响应展示，不参与判定）。"""
    from datetime import datetime

    return datetime.fromtimestamp(int(epoch_s)).isoformat()


def _safe_error_code(exc: BaseException) -> str:
    """把任意异常映射成**稳定**错误码（长度 <= 64）。"""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code[:64]
    return f"generation_failed:{type(exc).__name__}"[:64]


def _jsonable(value: Any) -> Any:
    """递归转为可 JSON 序列化对象（``datetime`` → ISO 字符串）。"""
    from datetime import datetime

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ===========================================================================
# 配置
# ===========================================================================

def load_generation_config(config_manager: Any = None) -> dict:
    """从 ``hotspot.events`` 段读取生成两配置并校验 ``deadline < lease``。

    Args:
        config_manager: 既有 ``ConfigManager``（缺省则用内置默认值）。

    Returns:
        dict: ``{'generation_lease_seconds': int, 'generation_deadline_seconds': int}``。

    Raises:
        GenerationConfigError: 段不是映射 / 取值非法 / ``deadline >= lease``。
    """
    raw: Any = None
    if config_manager is not None:
        try:
            raw = config_manager.get("hotspot.events", None)
        except Exception:  # noqa: BLE001 - 配置读取失败按缺省处理更稳，但绝不静默改阈值
            raw = None
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise GenerationConfigError("hotspot_events_section_must_be_mapping")

    lease = raw.get("generation_lease_seconds", DEFAULT_GENERATION_LEASE_SECONDS)
    deadline = raw.get("generation_deadline_seconds", DEFAULT_GENERATION_DEADLINE_SECONDS)

    for name, value in (("generation_lease_seconds", lease), ("generation_deadline_seconds", deadline)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise GenerationConfigError(f"invalid_generation_config:{name}")

    if int(deadline) >= int(lease):
        # 违反 deadline < lease 立即报错，不静默调整
        raise GenerationConfigError("generation_deadline_must_be_less_than_generation_lease")

    return {
        "generation_lease_seconds": int(lease),
        "generation_deadline_seconds": int(deadline),
    }


# ===========================================================================
# 规范化
# ===========================================================================

def _normalize_text(value: Any, field_name: str) -> str:
    """``direction`` / ``zone_name``：NFKC + 首尾 strip，且不得为空。"""
    if value is None:
        raise GenerationValidationError(f"missing_field:{field_name}")
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        raise GenerationValidationError(f"empty_field:{field_name}")
    return text


def normalize_generation_request(
    request: GenerationRequest,
    *,
    frozen_event_order: Optional[Sequence[str]] = None,
) -> dict:
    """把请求规范化成固定 payload（§3.2），并校验键/模式约束。

    Args:
        request: 生成请求。
        frozen_event_order: 服务器冻结的事件顺序（event 模式 canonical 化用）。

    Returns:
        dict: 规范化 payload（``request_schema_version/direction/zone_name/count/use_llm/
        opportunity_run_id/selected_event_ids/context_mode``）。

    Raises:
        GenerationValidationError: 字段类型/范围非法，或 event 模式缺键、缺 run_id。
    """
    direction = _normalize_text(request.direction, "direction")
    zone_name = _normalize_text(request.zone_name, "zone_name")

    count = request.count
    if isinstance(count, bool) or not isinstance(count, int) or not (MIN_GENERATION_COUNT <= count <= MAX_GENERATION_COUNT):
        raise GenerationValidationError("invalid_count")

    if not isinstance(request.use_llm, bool):
        raise GenerationValidationError("invalid_use_llm")

    mode = request.context_mode
    if mode not in CONTEXT_MODES:
        raise GenerationValidationError("invalid_context_mode")

    run_id = request.opportunity_run_id
    if run_id is not None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise GenerationValidationError("invalid_opportunity_run_id")
        run_id = run_id.strip()

    selected_raw = request.selected_event_ids
    if selected_raw is None:
        selected: list = []
    else:
        if isinstance(selected_raw, str):
            raise GenerationValidationError("invalid_selected_event_ids")
        selected = []
        for item in selected_raw:
            text = str(item).strip()
            if text and text not in selected:
                selected.append(text)

    if selected and run_id is None:
        # 只给 selected_event_ids 却没有 run_id → 422
        raise GenerationValidationError("selected_event_ids_requires_run_id")

    if run_id is not None:
        # event 模式强制要键
        if not isinstance(request.generation_request_id, str) or not request.generation_request_id.strip():
            raise GenerationValidationError("generation_request_id_required")
        if not selected:
            # 未细分事件 → 用该 run 的冻结事件全集
            selected = [str(item) for item in (frozen_event_order or [])]
        elif frozen_event_order is not None:
            # 按服务器冻结顺序 canonical 化
            order_index = {str(eid): index for index, eid in enumerate(frozen_event_order)}
            unknown = [eid for eid in selected if eid not in order_index]
            if unknown:
                raise GenerationValidationError("selected_event_not_in_run")
            selected = sorted(selected, key=lambda eid: order_index[eid])

    return {
        "request_schema_version": GENERATION_REQUEST_SCHEMA_VERSION,
        "direction": direction,
        "zone_name": zone_name,
        "count": int(count),
        "use_llm": bool(request.use_llm),
        "opportunity_run_id": run_id,
        "selected_event_ids": list(selected),
        "context_mode": mode,
    }


# ===========================================================================
# 账本 store
# ===========================================================================

class TopicGenerationStore:
    """``topic_generation_runs`` 账本读写（claim / freeze / complete / fail / 只读恢复）。

    设计口径：每个公开方法**自持事务**，绝不在数据库事务里 ``await`` 网络。
    """

    def __init__(
        self,
        session_factory: Optional[Callable[[], Session]] = None,
        clock: Optional[Callable[[], int]] = None,
        *,
        lease_seconds: int = DEFAULT_GENERATION_LEASE_SECONDS,
        deadline_seconds: int = DEFAULT_GENERATION_DEADLINE_SECONDS,
        recovery_limit: int = DEFAULT_RECOVERY_LIMIT,
    ) -> None:
        """初始化账本 store。

        Args:
            session_factory: 会话工厂；缺省用 ``core.database.get_session``。
            clock: 秒级时钟；缺省用 ``time.time``。
            lease_seconds: 租约时长（秒）。
            deadline_seconds: 总 deadline（秒），必须 **小于** lease。
            recovery_limit: 后台恢复单次处理上限。

        Raises:
            GenerationConfigError: ``deadline >= lease``（不静默调整）。
        """
        self._session_factory: Callable[[], Session] = session_factory or get_session
        self._clock: Callable[[], int] = clock or (lambda: int(time.time()))
        self.lease_seconds = int(lease_seconds)
        self.deadline_seconds = int(deadline_seconds)
        self.recovery_limit = int(recovery_limit)
        if not (0 < self.deadline_seconds < self.lease_seconds):
            raise GenerationConfigError("generation_deadline_must_be_less_than_generation_lease")

    # ------------------------------------------------------------------ 内部

    def now_s(self) -> int:
        """当前 epoch 秒（走注入时钟）。"""
        return int(self._clock())

    def _open(self) -> Session:
        """打开一个会话（调用方负责 close）。"""
        return self._session_factory()

    # ------------------------------------------------------------------ claim

    def claim_generation(
        self,
        request_id: Optional[str],
        payload: Mapping[str, Any],
        request_hash: Optional[str] = None,
    ) -> GenerationClaim:
        """尝试领取生成任务（**短事务 INSERT 主键**）。

        Args:
            request_id: 客户端 ``generation_request_id``；``None`` 表示旧无键 tag_only 路径。
            payload: 规范化请求。
            request_hash: 内容 hash；缺省按 ``payload`` 现算。

        Returns:
            GenerationClaim: 见 :class:`GenerationClaim`。

        Raises:
            GenerationKeyConflict: 同键但内容 hash 不同。
        """
        if request_id is None:
            return GenerationClaim(
                kind=CLAIM_LEGACY,
                id=None,
                request_hash=request_hash or _sha256_payload(payload),
                request_payload=dict(payload),
            )

        request_hash = request_hash or _sha256_payload(payload)
        now = self.now_s()
        token = uuid.uuid4().hex
        lease_until = now + self.lease_seconds

        session: Optional[Session] = None
        try:
            session = self._open()
            row = TopicGenerationRun(
                id=request_id,
                schema_version=GENERATION_SCHEMA_VERSION,
                request_hash=request_hash,
                request_payload=dict(payload),
                opportunity_run_id=payload.get("opportunity_run_id"),
                context_snapshot=None,
                state="running",
                lease_token=token,
                lease_until_s=lease_until,
                created_s=now,
                started_s=now,
            )
            session.add(row)
            session.commit()
            return GenerationClaim(
                kind=CLAIM_ACQUIRED,
                id=request_id,
                request_hash=request_hash,
                lease_token=token,
                request_payload=dict(payload),
                acquired_s=now,
                lease_until_s=lease_until,
                effective_state="running",
            )
        except IntegrityError:
            # 并发唯一冲突：rollback 后重新读取既有行，再按 §3.2 规则响应
            if session is not None:
                session.rollback()
                existing = session.get(TopicGenerationRun, request_id)
                if existing is not None:
                    return self._claim_from_existing(existing, request_hash, now)
            raise
        except Exception:
            if session is not None:
                session.rollback()
            raise
        finally:
            if session is not None:
                session.close()

    def _claim_from_existing(self, row: TopicGenerationRun, request_hash: str, now: int) -> GenerationClaim:
        """依据既有账本行判定 claim 结果（同 hash 三态 / 不同 hash 409）。"""
        if row.request_hash != request_hash:
            raise GenerationKeyConflict("generation_key_conflict")

        state = str(row.state)
        if state == "completed":
            return GenerationClaim(
                kind=CLAIM_COMPLETED,
                id=row.id,
                request_hash=request_hash,
                saved_response=row.result,
                effective_state="completed",
                lease_until_s=row.lease_until_s,
            )
        if state == "running":
            lease_until = row.lease_until_s
            if lease_until is not None and int(lease_until) <= now:
                # lease 已过期：**不重领给另一个 worker**；按有效状态 interrupted 处理（GET 只读不落库）
                return GenerationClaim(
                    kind=CLAIM_TERMINAL,
                    id=row.id,
                    request_hash=request_hash,
                    error_code="lease_expired",
                    effective_state="interrupted",
                    lease_until_s=lease_until,
                )
            return GenerationClaim(
                kind=CLAIM_RUNNING,
                id=row.id,
                request_hash=request_hash,
                effective_state="running",
                lease_until_s=lease_until,
            )
        # failed / cancelled / interrupted：返回其终态错误，第一版不自动重启
        return GenerationClaim(
            kind=CLAIM_TERMINAL,
            id=row.id,
            request_hash=request_hash,
            error_code=row.error_code or state,
            effective_state=state,
        )

    # ------------------------------------------------------------------ freeze context

    def freeze_context(self, claim: GenerationClaim, context: Mapping[str, Any]) -> GenerationClaim:
        """短事务 token fence：把首次领取冻结的 context 写入 ``context_snapshot``。

        Raises:
            GenerationOwnershipLost: token/状态不匹配，或行不存在。
        """
        if claim.kind != CLAIM_ACQUIRED or claim.id is None:
            return claim

        session: Optional[Session] = None
        try:
            session = self._open()
            outcome = session.execute(
                update(TopicGenerationRun)
                .where(
                    TopicGenerationRun.id == claim.id,
                    TopicGenerationRun.request_hash == claim.request_hash,
                    TopicGenerationRun.lease_token == claim.lease_token,
                    TopicGenerationRun.state == "running",
                )
                .values(context_snapshot=dict(context))
            )
            if outcome.rowcount != 1:
                session.rollback()
                raise GenerationOwnershipLost("generation_claim_not_owned")
            session.commit()
            claim.context_snapshot = dict(context)
            return claim
        except Exception:
            if session is not None:
                session.rollback()
            raise
        finally:
            if session is not None:
                session.close()

    # ------------------------------------------------------------------ complete

    def complete_generation(
        self,
        claim: GenerationClaim,
        draft: Mapping[str, Any],
        insert_topics: Callable[[Session, list], Sequence[int]],
    ) -> dict:
        """**一个原子事务**内：条件 UPDATE(rowcount=1) → 插 Topic → 构造响应 → 写 completed。

        Args:
            claim: 已领取的 claim。
            draft: 生成器草稿（含 topics / used_llm / context_snapshot 等）。
            insert_topics: ``generator._insert_topics``（flush-only 内核）。

        Returns:
            dict: 完整成功响应（HTTP 200 用）。

        Raises:
            GenerationOwnershipLost: 条件 UPDATE rowcount != 1。
            GenerationUnavailable: COMMIT 异常且无法独立短只读查询已提交状态。
        """
        if claim.id is None:
            # 无账本（旧兼容路径）不应走到这里
            raise GenerationUnavailable("generation_state_unknown")

        finish = self.now_s()
        topics = list(draft.get("topics") or [])
        for topic in topics:
            if isinstance(topic, dict):
                # generation_request_id 仅带键路径写入；旧记录不伪回填
                topic["generation_request_id"] = claim.id

        session: Optional[Session] = None
        try:
            session = self._open()

            # 1) 条件 UPDATE 围栏：rowcount 必须 = 1，否则不得插入任何 Topic
            fence = session.execute(
                update(TopicGenerationRun)
                .where(
                    TopicGenerationRun.id == claim.id,
                    TopicGenerationRun.request_hash == claim.request_hash,
                    TopicGenerationRun.lease_token == claim.lease_token,
                    TopicGenerationRun.state == "running",
                    TopicGenerationRun.lease_until_s > finish,
                )
                .values(lease_until_s=TopicGenerationRun.lease_until_s)
            )
            if fence.rowcount != 1:
                session.rollback()
                raise GenerationOwnershipLost("generation_claim_not_owned")

            # 2) flush-only 内核插 Topic（与账本同事务）
            saved_ids = list(insert_topics(session, topics))

            # 3) 构造完整响应（真实 used_llm / context / saved_ids）；序列化失败同样回滚
            response = self._build_response(claim, draft, saved_ids, finish)

            # 4) 更新同一账本为 completed / result / finished_s，清 token/lease
            done = session.execute(
                update(TopicGenerationRun)
                .where(
                    TopicGenerationRun.id == claim.id,
                    TopicGenerationRun.lease_token == claim.lease_token,
                    TopicGenerationRun.state == "running",
                )
                .values(
                    state="completed",
                    result=response,
                    finished_s=finish,
                    lease_token=None,
                    lease_until_s=None,
                    error_code=None,
                )
            )
            if done.rowcount != 1:
                session.rollback()
                raise GenerationOwnershipLost("generation_claim_not_owned")

            # 5) COMMIT（成功后才返回 HTTP 200）
            session.commit()
            return response
        except (GenerationOwnershipLost, GenerationUnavailable):
            if session is not None:
                session.rollback()
            raise
        except Exception as exc:
            if session is not None:
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001 - 回滚失败不覆盖原错误
                    logger.exception("生成账本回滚失败")
            # COMMIT 返回异常 → 先独立短只读查询，排除“COMMIT 成功但响应失败”
            if self._safe_is_completed(claim.id):
                return self.read_completed_response(claim.id)
            raise
        finally:
            if session is not None:
                session.close()

    def _build_response(self, claim: GenerationClaim, draft: Mapping[str, Any], saved_ids: Sequence[int], finish: int) -> dict:
        """构造完整成功响应并**校验 JSON 可序列化**（失败则抛，由上层回滚）。"""
        response = {str(key): value for key, value in dict(draft).items() if key != "saved_ids"}
        response["saved_ids"] = [int(item) for item in saved_ids]
        response["replayed"] = False
        response["generation_request_id"] = claim.id
        response["idempotency"] = "protected"
        if not response.get("generated_at"):
            response["generated_at"] = _iso_from_epoch(finish)
        payload = _jsonable(response)
        # 序列化验证：不可序列化 → 抛异常 → Topic 与账本一起回滚
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        return payload

    # ------------------------------------------------------------------ fail / cancel / interrupt

    def _terminal_if_owned(self, claim: GenerationClaim, state: str, error_code: Optional[str]) -> bool:
        """条件 UPDATE：只把匹配 token 且仍 running 的本键改为终态（已完成不可改）。

        Returns:
            bool: 命中并更新返回 ``True``（rowcount==1）。
        """
        if claim.id is None or claim.lease_token is None:
            return False
        session: Optional[Session] = None
        try:
            session = self._open()
            outcome = session.execute(
                update(TopicGenerationRun)
                .where(
                    TopicGenerationRun.id == claim.id,
                    TopicGenerationRun.lease_token == claim.lease_token,
                    TopicGenerationRun.state == "running",
                )
                .values(
                    state=state,
                    error_code=error_code,
                    finished_s=self.now_s(),
                    lease_token=None,
                    lease_until_s=None,
                )
            )
            session.commit()
            return outcome.rowcount == 1
        except Exception:
            if session is not None:
                session.rollback()
            raise
        finally:
            if session is not None:
                session.close()

    def fail_if_owned(self, claim: GenerationClaim, error_code: Optional[str] = None) -> bool:
        """失败落账（只对本键 running 生效）。"""
        return self._terminal_if_owned(claim, "failed", error_code or "generation_failed")

    def cancel_if_owned(self, claim: GenerationClaim) -> bool:
        """取消落账（``CancelledError`` 时调用后继续抛出）。"""
        return self._terminal_if_owned(claim, "cancelled", "generation_cancelled")

    def interrupt_if_owned(self, claim: GenerationClaim, error_code: Optional[str] = None) -> bool:
        """超时/中断落账（清 token，不把同 key 重领给另一 worker）。"""
        return self._terminal_if_owned(claim, "interrupted", error_code or "generation_interrupted")

    # ------------------------------------------------------------------ 只读读取

    def get_row(self, request_id: str) -> Optional[TopicGenerationRun]:
        """只读读取账本行（读不到返回 ``None``；**不落库**）。"""
        session: Optional[Session] = None
        try:
            session = self._open()
            return session.get(TopicGenerationRun, request_id)
        finally:
            if session is not None:
                session.close()

    def is_completed(self, request_id: str) -> bool:
        """只读判定：账本是否已完成。"""
        row = self.get_row(request_id)
        return row is not None and str(row.state) == "completed"

    def _safe_is_completed(self, request_id: str) -> bool:
        """吞掉查询异常的是否完成判定（用于 COMMIT 异常后的探测）。"""
        try:
            return self.is_completed(request_id)
        except Exception:  # noqa: BLE001 - 探测失败按“未知”处理，由上层返 503
            logger.warning("生成账本完成状态探测失败（查询不可用）")
            return False

    def read_completed_response(self, request_id: str) -> dict:
        """只读取回已完成账本的持久结果。

        Raises:
            GenerationUnavailable: 账本不存在或尚未 completed。
        """
        row = self.get_row(request_id)
        if row is None or str(row.state) != "completed" or row.result is None:
            raise GenerationUnavailable("generation_state_unknown")
        return {str(key): value for key, value in dict(row.result).items()}

    def read_state(self, request_id: str) -> Optional[dict]:
        """**严格只读**的账本状态视图（GET 用）。

        规则：``completed`` 优先于 lease 状态；``running`` 且 lease 已过期 → 返回
        ``interrupted`` + ``lease_expired``（不在 GET 中落库）。

        Returns:
            dict | None: 账本不存在返回 ``None``（对应 404）。
        """
        row = self.get_row(request_id)
        if row is None:
            return None
        state = str(row.state)
        if state == "completed":
            return {
                "generation_request_id": row.id,
                "status": "completed",
                "result": row.result,
                "finished_s": row.finished_s,
            }
        if state == "running":
            lease_until = row.lease_until_s
            if lease_until is not None and int(lease_until) <= self.now_s():
                return {
                    "generation_request_id": row.id,
                    "status": "interrupted",
                    "reason_code": "lease_expired",
                }
            return {
                "generation_request_id": row.id,
                "status": "running",
                "lease_until_s": lease_until,
            }
        return {
            "generation_request_id": row.id,
            "status": state,
            "reason_code": row.error_code,
        }

    # ------------------------------------------------------------------ 恢复

    def recover_expired_generation_runs(self, *, limit: Optional[int] = None) -> int:
        """有界后台恢复：把 lease 已过期的 ``running`` 行标 ``interrupted`` 并清 token。

        Args:
            limit: 单次处理上限；缺省取 ``recovery_limit``。

        Returns:
            int: 实际标记行数。
        """
        cap = int(limit if limit is not None else self.recovery_limit)
        now = self.now_s()
        session: Optional[Session] = None
        try:
            session = self._open()
            expired_ids = [
                row_id
                for (row_id,) in session.query(TopicGenerationRun.id)
                .filter(
                    TopicGenerationRun.state == "running",
                    TopicGenerationRun.lease_until_s.isnot(None),
                    TopicGenerationRun.lease_until_s <= now,
                )
                .limit(cap)
                .all()
            ]
            if not expired_ids:
                return 0
            outcome = session.execute(
                update(TopicGenerationRun)
                .where(
                    TopicGenerationRun.id.in_(expired_ids),
                    TopicGenerationRun.state == "running",
                )
                .values(
                    state="interrupted",
                    error_code="lease_expired",
                    finished_s=now,
                    lease_token=None,
                    lease_until_s=None,
                )
            )
            session.commit()
            return int(outcome.rowcount or 0)
        except Exception:
            if session is not None:
                session.rollback()
            raise
        finally:
            if session is not None:
                session.close()


# ===========================================================================
# 服务编排
# ===========================================================================

class TopicGenerationService:
    """生成编排：normalize → claim → context → generate → complete/read。

    复用现有 :class:`TopicGenerator`，不建第二套模型生成器。
    """

    def __init__(
        self,
        generator: TopicGenerator,
        store: TopicGenerationStore,
        repository: Optional[HotEventRepository] = None,
        *,
        clock: Optional[Callable[[], int]] = None,
    ) -> None:
        """初始化服务。

        Args:
            generator: 现有选题生成器。
            store: 生成账本 store。
            repository: 事件仓储（读取冻结的 OpportunityRun）。
            clock: 秒级时钟（缺省沿用 store 的时钟）。
        """
        self.generator = generator
        self.store = store
        self.repository = repository or HotEventRepository()
        self._clock = clock or store._clock

    async def recover_expired(self) -> int:
        """启动 / 有界后台恢复入口（同步落库，不触网）。"""
        return self.store.recover_expired_generation_runs()

    # ------------------------------------------------------------------ 主控制流

    async def generate(self, request: GenerationRequest) -> dict:
        """按 §11.6 唯一控制流执行一次生成。

        Returns:
            dict: 完成响应 / 202 受理视图 / 旧兼容结果。

        Raises:
            GenerationValidationError: 请求或 run 校验失败（422）。
            GenerationKeyConflict: 同键不同内容（409）。
            GenerationTerminalError: 同键已是终态（不自动重启）。
            GenerationUnavailable: 完成状态未知（503，保留同 key）。
            GenerationTimeoutError: 超过总 deadline（已标 interrupted）。
        """
        payload, run = self._prepare_request(request)
        request_hash = _sha256_payload(payload)

        claim = self.store.claim_generation(request.generation_request_id, payload, request_hash)

        if claim.kind == CLAIM_COMPLETED:
            return self._replayed(claim.saved_response or {})
        if claim.kind == CLAIM_RUNNING:
            return self._accepted(claim)
        if claim.kind == CLAIM_TERMINAL:
            raise GenerationTerminalError(claim.error_code)

        try:
            return await asyncio.wait_for(
                self._execute(claim, payload, run),
                timeout=self.store.deadline_seconds,
            )
        except asyncio.TimeoutError as exc:
            # 超时 → 标 interrupted 并清 token（不重领给另一个 worker）
            self.store.interrupt_if_owned(claim, "deadline_exceeded")
            raise GenerationTimeoutError("generation_deadline_exceeded") from exc
        except asyncio.CancelledError:
            self.store.cancel_if_owned(claim)
            raise
        except GenerationOwnershipLost:
            raise
        except Exception as exc:
            if claim.id is not None:
                # 先读账本排除“COMMIT 成功但响应失败”
                if self.store._safe_is_completed(claim.id):
                    return self._replayed(self.store.read_completed_response(claim.id))
                try:
                    self.store.fail_if_owned(claim, _safe_error_code(exc))
                except Exception as fail_exc:  # noqa: BLE001 - 落失败也失败 → 状态未知
                    raise GenerationUnavailable("generation_state_unknown") from fail_exc
            raise

    # ------------------------------------------------------------------ 内部步骤

    def _prepare_request(self, request: GenerationRequest) -> tuple:
        """加载 run（event 模式）、规范化 payload；返回 ``(payload, run)``。"""
        run: Optional[OpportunityRun] = None
        frozen_order: Optional[list] = None

        if request.opportunity_run_id:
            run = self.repository.get_opportunity_run(request.opportunity_run_id)
            if run is None:
                raise GenerationValidationError("opportunity_run_not_found")
            frozen_order = self._frozen_event_ids(run)

        payload = normalize_generation_request(request, frozen_event_order=frozen_order)
        return payload, run

    @staticmethod
    def _frozen_event_ids(run: OpportunityRun) -> list:
        """从 OpportunityRun 里取**冻结顺序**的事件 ID（candidates 优先，其次 result）。"""
        ordered: list = []
        candidates = run.candidates if isinstance(run.candidates, (list, tuple)) else []
        for candidate in candidates:
            if isinstance(candidate, Mapping) and candidate.get("event_id"):
                event_id = str(candidate["event_id"])
                if event_id not in ordered:
                    ordered.append(event_id)
        if not ordered:
            result = run.result if isinstance(run.result, Mapping) else {}
            for key in ("ranked_event_ids", "not_executable_event_ids"):
                for event_id in result.get(key) or []:
                    text = str(event_id)
                    if text not in ordered:
                        ordered.append(text)
        return ordered

    async def _execute(self, claim: GenerationClaim, payload: Mapping[str, Any], run: Optional[OpportunityRun]) -> dict:
        """context 获取 → freeze → 生成 → complete（或旧兼容保存）。"""
        context = self.prepare_context_or_tag_inputs(payload, run)

        if claim.kind == CLAIM_ACQUIRED:
            if context is not None:
                # 短事务 token fence（同步，不含网络 await）
                self.store.freeze_context(claim, context)
            draft = await self.generator.generate_topics(
                payload["direction"],
                payload["zone_name"],
                payload["count"],
                payload["use_llm"],
                recommendation_context=context,
                persist=False,
            )
            return self.store.complete_generation(claim, draft, self.generator._insert_topics)

        # 旧无键 tag_only 兼容路径：沿用旧保存语义，响应明确 legacy_unprotected
        draft = await self.generator.generate_topics(
            payload["direction"],
            payload["zone_name"],
            payload["count"],
            payload["use_llm"],
            recommendation_context=context,
            persist=True,
        )
        draft["idempotency"] = "legacy_unprotected"
        draft.setdefault("generation_mode", GENERATION_MODE_TAG_ONLY)
        return draft

    def prepare_context_or_tag_inputs(
        self,
        payload: Mapping[str, Any],
        run: Optional[OpportunityRun] = None,
    ) -> Optional[dict]:
        """服务端加载并冻结 context（客户端只能提交 ID）。

        Args:
            payload: 规范化请求。
            run: 已加载的机会运行（tag_only 为 ``None``）。

        Returns:
            dict | None: event 上下文；tag_only 返回 ``None``。
        """
        if payload.get("opportunity_run_id") is None:
            return None
        if run is None:
            run = self.repository.get_opportunity_run(payload["opportunity_run_id"])
            if run is None:
                raise GenerationValidationError("opportunity_run_not_found")
        return self._build_event_context(run, payload)

    def _build_event_context(self, run: OpportunityRun, payload: Mapping[str, Any]) -> dict:
        """把冻结的 OpportunityRun 组装成 ``recommendation_context``（服务器真值）。"""
        as_of_s = int(run.created_s) if run.created_s is not None else self.store.now_s()
        brief = run.creator_brief if isinstance(run.creator_brief, Mapping) else {}
        candidate_by_id = {
            str(candidate["event_id"]): candidate
            for candidate in (run.candidates or [])
            if isinstance(candidate, Mapping) and candidate.get("event_id")
        }
        run_assessment_ids = [str(item) for item in (run.assessment_ids or [])]

        events: list = []
        for event_id in payload.get("selected_event_ids") or []:
            candidate = candidate_by_id.get(str(event_id), {})
            action = candidate.get("action")
            verified_facts, bvid_refs = _collect_event_facts(candidate, as_of_s, str(event_id))
            phase = None
            evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), Mapping) else {}
            daily = evidence.get("daily") if isinstance(evidence.get("daily"), Mapping) else {}
            phase = daily.get("topic_phase")
            deadline = candidate.get("deadline") if isinstance(candidate.get("deadline"), Mapping) else {}
            events.append(
                {
                    "event_id": str(event_id),
                    "rule_version": candidate.get("rule_version"),
                    "assessment_ids": run_assessment_ids,
                    "as_of_s": as_of_s,
                    "input_hash": run.request_fingerprint,
                    "opportunity_action": action,
                    "rank_key": candidate.get("rank_key"),
                    "creator_brief_version": brief.get("brief_version"),
                    "verified_facts": verified_facts,
                    "limitations": list(candidate.get("notes") or []),
                    "activity_deadline": dict(deadline),
                    "allowed_formats": list(brief.get("supported_formats") or []),
                    "available_assets": list(brief.get("available_assets") or []),
                    "entities": [str(event_id)],
                    "anchors": bvid_refs or [str(event_id)],
                    "bvid_refs": bvid_refs,
                    "phase": phase,
                    "research_only": action not in EXECUTABLE_ACTIONS,
                }
            )

        return {
            "context_schema_version": CONTEXT_SCHEMA_VERSION,
            "opportunity_run_id": run.id,
            "policy_version": run.policy_version,
            "creator_brief_version": brief.get("brief_version"),
            "as_of_s": as_of_s,
            "context_mode": payload.get("context_mode", "current"),
            "zone_name": payload.get("zone_name"),
            "direction": payload.get("direction"),
            "events": events,
        }

    # ------------------------------------------------------------------ 响应视图

    @staticmethod
    def _replayed(saved_response: Mapping[str, Any]) -> dict:
        """完成重放：直接读账本，标 ``replayed=true``，不刷新时间、不重调 LLM/TagCloud。"""
        response = {str(key): value for key, value in dict(saved_response).items()}
        response["replayed"] = True
        response["idempotency"] = "protected"
        return response

    def _accepted(self, claim: GenerationClaim) -> dict:
        """运行中受理视图（web 层映射为 HTTP 202；不返回假 topics）。"""
        return {
            "success": True,
            "accepted": True,
            "status": "running",
            "http_status": 202,
            "generation_request_id": claim.id,
            "status_url": f"/api/hotspot/topics/generation-runs/{claim.id}",
            "idempotency": "protected",
            "replayed": False,
        }


def _collect_event_facts(candidate: Mapping[str, Any], as_of_s: int, event_id: str) -> tuple:
    """从候选证据里提取 ``verified_facts`` 与可打开的 BVID 引用。

    Args:
        candidate: 机会候选（含 ``evidence``）。
        as_of_s: 证据基准时刻。
        event_id: 事件 ID。

    Returns:
        tuple: ``(verified_facts, bvid_refs)``；事实 ID 形如 ``metric:{event}:{path}`` / ``bvid:{bvid}``。
    """
    evidence = candidate.get("evidence") if isinstance(candidate, Mapping) else None
    metrics: dict = {}
    bvid_refs: list = []

    def walk(node: Any, path: str) -> None:
        """递归遍历证据树，收集数值叶子与 BVID 字符串。"""
        if isinstance(node, Mapping):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        else:
            if isinstance(node, str) and _BVID_RE.match(node):
                if node not in bvid_refs:
                    bvid_refs.append(node)
            elif isinstance(node, (int, float)) and not isinstance(node, bool):
                metrics[path] = node

    walk(evidence, "")

    facts: list = [
        {"id": f"metric:{event_id}:{path}", "value": value, "unit": None, "source_url": None, "observed_s": as_of_s}
        for path, value in metrics.items()
    ]
    for bvid in bvid_refs:
        facts.append(
            {
                "id": f"bvid:{bvid}",
                "value": bvid,
                "unit": None,
                "source_url": f"https://www.bilibili.com/video/{bvid}",
                "observed_s": as_of_s,
            }
        )
    return facts, bvid_refs


__all__ = [
    "GENERATION_SCHEMA_VERSION",
    "GENERATION_REQUEST_SCHEMA_VERSION",
    "DEFAULT_GENERATION_LEASE_SECONDS",
    "DEFAULT_GENERATION_DEADLINE_SECONDS",
    "MIN_GENERATION_COUNT",
    "MAX_GENERATION_COUNT",
    "CONTEXT_MODES",
    "CLAIM_ACQUIRED",
    "CLAIM_COMPLETED",
    "CLAIM_RUNNING",
    "CLAIM_TERMINAL",
    "CLAIM_LEGACY",
    "GenerationError",
    "GenerationValidationError",
    "GenerationKeyConflict",
    "GenerationTerminalError",
    "GenerationOwnershipLost",
    "GenerationUnavailable",
    "GenerationTimeoutError",
    "GenerationConfigError",
    "GenerationRequest",
    "GenerationClaim",
    "load_generation_config",
    "normalize_generation_request",
    "TopicGenerationStore",
    "TopicGenerationService",
]
