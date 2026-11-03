"""语义分组与可撤销折叠的持久化层（store）。

把语义任务的运行态、预算台账、已发布分组、视频偏好与人工保护落到 SQLite，并提供
强原子性的 CAS 接口。全部方法同步、短事务、无网络、fail-closed（异常向上抛出）。

关键不变量：
- 组 ID 为服务端稳定标识，模型永远无权创作平台 ID。
- 人工保护键为 ``(video_id=bvid, rpid)``，跨任务生效，代表/组名变化不绕过。
- in-flight 预约带唯一 attempt_id；settle 只结算当前在飞请求，重复 settle 幂等。
- 取消后迟到结果不复活分组；cancelled 仍可"只记账不发布"。
- 语义模型行（组/成员/保护/台账）只存短 ID→rpid 映射与内容哈希，不复制原文；
  原文的权威副本仍是 ``comments`` 表。固定输入快照存于 ``Task.params.snapshot``
  以保证可复现与断点恢复（含原文），由 router 内部持有、不随普通展示外发。

本模块只依赖 core.database 的模型与会话工厂，不 import provider/service，避免循环依赖。
"""
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.exc import IntegrityError

from core.database import (
    Comment,
    Task,
    Video,
    SemanticRun,
    SemanticGroup,
    SemanticGroupMember,
    SemanticPreference,
    SemanticProtection,
    SemanticAudit,
    SemanticDailyUsage,
    get_session,
)

# 共享契约为单一事实源：版本常量、限额常量、类型化异常统一来自 contracts。
# 不重定义同名异常类（重定义会让跨模块 except 失效），仅 re-export 以便外部兼容导入。
from .contracts import (
    CONSENT_VERSION,
    RULE_VERSION,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_ATTEMPTS,
    BATCH_ATTEMPTS,
    BATCH_SIZE,
    INPUT_BYTES,
    OUTPUT_TOKENS,
    TASK_TOKEN_BUDGET,
    DAILY_TOKEN_BUDGET,
    CONTENT_BYTES_CAP,
    MAX_GROUP_SIZE,
    SemanticError,
    SemanticNotFound,
    SemanticConflict,
    SemanticBudgetExceeded,
    SemanticValidationError,
    normalize_bvid,
)

#: 单条内容合格 UTF-8 字节上限（别名，对齐 contracts 命名）。
CONTENT_MAX_BYTES = CONTENT_BYTES_CAP

TASK_TYPE = "comment_semantic"

#: result() 中可展示组内成员的总上限（避免分页旁路出现无可达成员）。
MAX_GROUP_ITEMS_TOTAL = 500

ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled", "interrupted"}
RESUMABLE_STATUSES = {"interrupted", "partial", "failed"}

#: update_task 白名单：**不含 groups**（发布只走 publish_groups，避免覆盖 checkpoint）。
UPDATE_WHITELIST = {"status", "phase", "progress", "checkpoint", "coverage", "error", "result"}

#: update_task 允许 worker 设置的状态值。cancelled/interrupted 必须走 cancel_task/
#: recover_interrupted 专用方法，不能由普通更新绕过（否则跳过幂等/未知结果处理）。
UPDATE_ALLOWED_STATUSES = {"queued", "running", "completed", "partial", "failed"}

# re-export，使 `from modules.comment.semantic.store import SemanticNotFound` 等保持可用。
__all__ = [
    "SemanticStore",
    "SemanticError",
    "SemanticNotFound",
    "SemanticConflict",
    "SemanticBudgetExceeded",
    "SemanticValidationError",
    "content_hash",
    "CONSENT_VERSION",
    "RULE_VERSION",
]


# ============ 纯函数辅助 ============

def content_hash(text: Optional[str]) -> str:
    """对评论原文算稳定内容哈希（用于 stale 检测与重放键）。

    Args:
        text: 评论原文（None 视为空串）。

    Returns:
        str: sha256 十六进制摘要。
    """
    data = (text or "").encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _utf8_len(text: Optional[str]) -> int:
    """原文 UTF-8 字节长度（空值计 0）。"""
    return len((text or "").encode("utf-8"))


def _today() -> str:
    """当前自然日字符串 YYYY-MM-DD（与 SQLite date() 口径一致）。"""
    return datetime.now().strftime("%Y-%m-%d")


def _is_stale(current: Optional[str], published: Optional[str]) -> bool:
    """判断成员是否 stale：原文被删(current 为 None)或内容哈希变化均算 stale。

    删除的评论(current 为 None)必须视为 stale 且不可自动折叠——否则删除对折叠不可见。

    Args:
        current: 当前库内该 rpid 的内容哈希；None 表示该评论已不存在。
        published: 发布/快照时记录的内容哈希。

    Returns:
        bool: True 表示 stale。
    """
    return current is None or current != published


def _stable_group_id(task_id: int, members: List[Dict[str, Any]]) -> str:
    """由 task_id + 排序后的成员 (rpid, content_hash) 派生稳定组 ID。

    同一任务、同一成员集合（含内容版本）重发得到相同 group_id，客户端持有的
    undo_group/allow_group 引用不因重发失效；成员集合变化则自然得到新 ID。

    Args:
        task_id: 所属任务。
        members: 规范化后的成员 [{rpid, content_hash, ...}]。

    Returns:
        str: 形如 g<12hex> 的稳定标识。
    """
    key = [task_id] + sorted((str(m["rpid"]), m["content_hash"]) for m in members)
    raw = json.dumps(key, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "g" + hashlib.sha256(raw).hexdigest()[:12]


def _snapshot_hash(bvid: str, items: List[Dict[str, Any]], model_identity: Dict[str, Any]) -> str:
    """由视频、各成员内容版本、上下文与模型身份派生稳定重放键。

    **不**以模型标签或代表原文哈希单独为键；而是覆盖全部入选成员的
    (rpid, content_hash, context_status) 元组 + 规则/同意版本 + 模型身份。

    Args:
        bvid: 视频 BV 号。
        items: 快照入选成员（含 rpid/content_hash/context_status）。
        model_identity: {api_base, model}（不含密钥）。

    Returns:
        str: sha256 十六进制摘要。
    """
    payload = {
        "bvid": bvid,
        "rule_version": RULE_VERSION,
        "consent_version": CONSENT_VERSION,
        "model": {
            "api_base": (model_identity or {}).get("api_base"),
            "model": (model_identity or {}).get("model"),
        },
        "members": sorted(
            (str(it.get("rpid")), it.get("content_hash"), it.get("context_status"))
            for it in items
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class SemanticStore:
    """语义折叠持久化层。

    所有公共方法同步、短事务、fail-closed。会话工厂默认 ``core.database.get_session``；
    测试可注入指向临时库的工厂。
    """

    def __init__(self, session_factory: Optional[Callable] = None):
        """构造 store。

        Args:
            session_factory: 无参可调用，返回 SQLAlchemy Session；None 用全局 get_session。
        """
        self._session_factory = session_factory or get_session

    # ------------------------------------------------------------------
    # 会话与通用辅助
    # ------------------------------------------------------------------
    def _session(self):
        """打开一个新会话（调用方负责关闭）。"""
        return self._session_factory()

    @staticmethod
    def _require_video(session, bvid: str) -> Video:
        """查 bvid 对应的本地视频；不存在抛 SemanticNotFound（与 snapshot 契约一致）。

        偏好/决策写入前必须调用，避免落下 video_id 为 NULL 的孤儿偏好行（issue #45）。
        """
        video = session.query(Video).filter_by(bvid=bvid).first()
        if video is None:
            raise SemanticNotFound(f"视频不存在: {bvid}")
        return video

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def snapshot(self, bvid: str, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        """构造固定输入快照（仅读原始 comments，绝不读 fuzzy 去重代表）。

        选取规则：按 ``Comment.id`` 降序取最多 ``limit`` 条（确定性 newest），
        再反转为模型输入顺序。合格(eligible)要求：已知顶层上下文且原文 UTF-8
        字节不超过 CONTENT_MAX_BYTES。回复/未知上下文/超长分别计入排除原因，
        仍保留在 items 中可查看，绝不静默丢弃。

        Args:
            bvid: 视频 BV 号。
            limit: 快照上限，钳制到 [1, MAX_LIMIT]。

        Returns:
            snapshot dict（见契约 §Scope）。

        Raises:
            SemanticNotFound: 视频不存在。
            SemanticValidationError: limit 超出 [1, MAX_LIMIT]（直调不静默钳制）。
        """
        bvid = normalize_bvid(bvid, strict=False)
        if limit is None:
            limit = DEFAULT_LIMIT
        limit = int(limit)
        if limit < 1 or limit > MAX_LIMIT:
            raise SemanticValidationError(
                f"limit 超出范围 [1, {MAX_LIMIT}]: {limit}"
            )
        session = self._session()
        try:
            video = self._require_video(session, bvid)

            raw_total = session.query(Comment).filter_by(video_id=video.id).count()
            # 确定性 newest：按 id 降序取 limit 条，再反转成模型输入顺序（老->新）。
            newest = (
                session.query(Comment)
                .filter_by(video_id=video.id)
                .order_by(Comment.id.desc())
                .limit(limit)
                .all()
            )
            ordered = list(reversed(newest))

            items: List[Dict[str, Any]] = []
            eligible_ids: List[str] = []
            excluded_context = 0
            excluded_long = 0
            for idx, c in enumerate(ordered):
                local_id = f"c{idx}"
                status = c.context_status or "unknown"
                chash = content_hash(c.content)
                is_top = status == "top_level"
                too_long = _utf8_len(c.content) > CONTENT_MAX_BYTES
                eligible = is_top and not too_long
                if eligible:
                    eligible_ids.append(local_id)
                elif not is_top:
                    excluded_context += 1
                elif too_long:
                    excluded_long += 1
                items.append({
                    "id": local_id,
                    "rpid": str(c.rpid),
                    "content": c.content or "",
                    "content_hash": chash,
                    "uid": c.uid,
                    "uname": c.uname or "",
                    "ctime": c.ctime.isoformat() if c.ctime else None,
                    "sentiment": c.sentiment,
                    "like": int(c.like or 0),
                    "context_status": status,
                    "eligible": eligible,
                })
            outside_scope = raw_total - len(ordered)
            return {
                "video": {"id": video.id, "bvid": bvid, "title": video.title},
                "items": items,
                "eligible_ids": eligible_ids,
                "counts": {
                    "raw_total": raw_total,
                    "selected": len(ordered),
                    "eligible": len(eligible_ids),
                    "excluded_context": excluded_context,
                    "excluded_long": excluded_long,
                    "outside_scope": max(0, outside_scope),
                },
                "scope": {"limit": limit, "order": "newest"},
            }
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 视频偏好（CAS）
    # ------------------------------------------------------------------
    @staticmethod
    def _pref_dto(pref: Optional[SemanticPreference]) -> Dict[str, Any]:
        """把偏好行（或缺省）映射为公共 DTO。"""
        if pref is None:
            return {
                "auto_fold": True,
                "revision": 0,
                "consent_version": None,
                "model_identity": None,
            }
        return {
            "auto_fold": bool(pref.auto_fold),
            "revision": int(pref.revision),
            "consent_version": pref.consent_version,
            "model_identity": pref.model_identity,
        }

    def preference(self, bvid: str) -> Dict[str, Any]:
        """读取视频偏好；视频存在但无记录时返回 auto_fold=True, revision=0 的缺省。

        Args:
            bvid: 视频 BV 号（首尾空白会被去除）。

        Returns:
            {auto_fold, revision, consent_version, model_identity}

        Raises:
            SemanticNotFound: 视频不存在（与 snapshot 契约一致）。
            SemanticValidationError: bvid 为空白。
        """
        bvid = normalize_bvid(bvid, strict=False)
        session = self._session()
        try:
            self._require_video(session, bvid)
            return self._stored_preference(session, bvid)
        finally:
            session.close()

    def _stored_preference(self, session, bvid: str) -> Dict[str, Any]:
        """内部读偏好：不做视频存在性校验。

        供 result() / decide 幂等重放使用：这些路径的 bvid 来自已持久化的任务/审计，
        即便视频行事后缺失，也应照常返回结果，而不是因公共入口的 404 契约而失败。
        """
        pref = session.query(SemanticPreference).filter_by(bvid=bvid).first()
        return self._pref_dto(pref)

    def set_preference(
        self,
        bvid: str,
        expected_revision: int,
        auto_fold: Optional[bool] = None,
        consent_version: Optional[str] = None,
        model_identity: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """CAS 更新视频偏好；无行时安全创建 revision0，有实际变更则 +1。

        关闭/开启 auto_fold 不触发任何模型调用（纯展示偏好）。模型身份精确匹配闸门
        由 service 负责，这里只持久化。

        Args:
            bvid: 视频 BV 号。
            expected_revision: 期望当前修订号；不匹配抛 SemanticConflict。
            auto_fold: 新的自动折叠偏好（None 表示不改）。
            consent_version: 同意版本（None 不改）。
            model_identity: 绑定模型身份（None 不改）。

        Returns:
            新的偏好 DTO（含新 revision）。

        Raises:
            SemanticConflict: 修订号不匹配。
            SemanticNotFound: 视频不存在（不落 video_id 为 NULL 的孤儿行）。
            SemanticValidationError: bvid 为空白。
        """
        bvid = normalize_bvid(bvid, strict=False)
        session = self._session()
        try:
            video = self._require_video(session, bvid)
            pref = session.query(SemanticPreference).filter_by(bvid=bvid).first()
            if pref is None:
                if int(expected_revision) != 0:
                    raise SemanticConflict("偏好修订冲突：期望 0（首次创建）")
                pref = SemanticPreference(
                    bvid=bvid,
                    video_id=video.id,
                    auto_fold=True if auto_fold is None else bool(auto_fold),
                    revision=0,
                    consent_version=consent_version,
                    model_identity=model_identity,
                )
                session.add(pref)
                session.commit()
                return self._pref_dto(pref)

            if int(pref.revision) != int(expected_revision):
                raise SemanticConflict(
                    f"偏好修订冲突：期望 {expected_revision}，实际 {pref.revision}"
                )
            changed = False
            if auto_fold is not None and bool(pref.auto_fold) != bool(auto_fold):
                pref.auto_fold = bool(auto_fold)
                changed = True
            if consent_version is not None and pref.consent_version != consent_version:
                pref.consent_version = consent_version
                changed = True
            if model_identity is not None and pref.model_identity != model_identity:
                pref.model_identity = model_identity
                changed = True
            if changed:
                pref.revision = int(pref.revision) + 1
            if pref.video_id is None:
                # 修复前写入的行可能 video_id 为 NULL：视频已存在时顺手回填。
                # 只是关联修复，不算用户可见变更，不涨 revision。
                pref.video_id = video.id
            session.commit()
            return self._pref_dto(pref)
        except SemanticError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 任务状态 DTO
    # ------------------------------------------------------------------
    def _state_dto(self, session, task: Task, run: SemanticRun) -> Dict[str, Any]:
        """把 Task + SemanticRun 组装成对外 State DTO。

        snapshot 来自 Task.params，checkpoint 来自 Task.checkpoint（router 内部用）；
        groups 为当前已发布组的规范化列表。budget 暴露预约/结算/in_flight。
        """
        params = task.params or {}
        budget = {
            "reserved_tokens": int(run.reserved_tokens or 0),
            "settled_tokens": int(run.settled_tokens or 0),
            "request_count": int(run.request_count or 0),
            "attempt_count": int(run.attempt_count or 0),
            "max_attempts": int(run.max_attempts or MAX_ATTEMPTS),
            "task_token_budget": int(run.task_token_budget or TASK_TOKEN_BUDGET),
            "daily_token_budget": int(run.daily_token_budget or DAILY_TOKEN_BUDGET),
            "in_flight": run.in_flight,
        }
        day = session.query(SemanticDailyUsage).filter_by(date=_today()).first()
        budget["daily_reserved"] = int(day.reserved_tokens) if day else 0
        budget["daily_settled"] = int(day.settled_tokens) if day else 0
        return {
            "task_id": task.id,
            "bvid": run.bvid,
            "status": task.status,
            "phase": run.phase,
            "revision": int(run.revision),
            "progress": float(task.progress or 0.0),
            "counts": params.get("counts", {}),
            "coverage": run.coverage or {},
            "budget": budget,
            "error": task.error,
            "model_identity": run.model_identity,
            "snapshot": params.get("snapshot"),
            "checkpoint": task.checkpoint,
            "groups": self._groups_dto(session, task.id, run.bvid, params.get("snapshot")),
        }

    def _load_task_run(self, session, task_id: int):
        """按 task_id 取 (Task, SemanticRun)，缺任一抛 SemanticNotFound。"""
        task = session.query(Task).filter_by(id=task_id).first()
        run = session.query(SemanticRun).filter_by(task_id=task_id).first()
        if task is None or run is None:
            raise SemanticNotFound(f"语义任务不存在: {task_id}")
        return task, run

    # ------------------------------------------------------------------
    # 创建 / 读取任务
    # ------------------------------------------------------------------
    def create_task(
        self,
        snapshot: Dict[str, Any],
        model_identity: Dict[str, Any],
        limits: Optional[Dict[str, Any]],
        request_key: str,
    ) -> Dict[str, Any]:
        """持久化创建语义任务（Task + SemanticRun），按 request_key 幂等。

        同一 (bvid, snapshot_hash) 下 request_key 命中既有任务则返回其状态；
        request_key 相同但 snapshot_hash/model_identity 不一致则抛 SemanticConflict。

        Args:
            snapshot: snapshot() 的输出。
            model_identity: {api_base, model}（不含密钥），创建后钉死。
            limits: 可选覆盖默认上限（max_attempts/task_token_budget/daily_token_budget）。
            request_key: 幂等键。

        Returns:
            State DTO（新任务 status=queued, revision=0）。

        Raises:
            SemanticConflict: 幂等键撞上不同快照/模型。
            SemanticValidationError: 快照缺字段。
        """
        if not request_key:
            raise SemanticValidationError("request_key 不能为空")
        video = (snapshot or {}).get("video") or {}
        bvid = video.get("bvid")
        items = (snapshot or {}).get("items") or []
        if not bvid:
            raise SemanticValidationError("快照缺少 video.bvid")
        shash = _snapshot_hash(bvid, items, model_identity)
        limits = limits or {}

        session = self._session()
        try:
            existing = (
                session.query(SemanticRun)
                .filter_by(bvid=bvid, request_key=request_key)
                .first()
            )
            if existing is not None:
                if existing.snapshot_hash != shash or existing.model_identity != model_identity:
                    raise SemanticConflict("幂等键已存在但快照/模型不一致，拒绝复用")
                task = session.query(Task).filter_by(id=existing.task_id).first()
                return self._state_dto(session, task, existing)

            task = Task(
                task_type=TASK_TYPE,
                task_name=f"语义分组 {bvid}",
                params={
                    "bvid": bvid,
                    "snapshot": snapshot,
                    "counts": snapshot.get("counts", {}),
                    "scope": snapshot.get("scope", {}),
                },
                status="queued",
                progress=0.0,
                checkpoint={"phase1": [], "cross": []},
            )
            session.add(task)
            session.flush()  # 取 task.id
            run = SemanticRun(
                task_id=task.id,
                bvid=bvid,
                request_key=request_key,
                snapshot_hash=shash,
                revision=0,
                phase="queued",
                model_identity=model_identity,
                max_attempts=int(limits.get("max_attempts", MAX_ATTEMPTS)),
                task_token_budget=int(limits.get("task_token_budget", TASK_TOKEN_BUDGET)),
                daily_token_budget=int(limits.get("daily_token_budget", DAILY_TOKEN_BUDGET)),
                coverage={},
            )
            session.add(run)
            try:
                session.commit()
            except IntegrityError:
                # 并发竞态：另一事务已用同 (bvid, request_key) 抢先插入。
                # 回滚后按幂等语义复查：同快照/模型则返回既有任务，否则 409。
                session.rollback()
                existing = (
                    session.query(SemanticRun)
                    .filter_by(bvid=bvid, request_key=request_key)
                    .first()
                )
                if existing is None:
                    raise SemanticConflict("创建语义任务并发冲突")
                if existing.snapshot_hash != shash or existing.model_identity != model_identity:
                    raise SemanticConflict("幂等键已存在但快照/模型不一致，拒绝复用")
                etask = session.query(Task).filter_by(id=existing.task_id).first()
                return self._state_dto(session, etask, existing)
            return self._state_dto(session, task, run)
        except SemanticError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_task(self, task_id: int) -> Dict[str, Any]:
        """按 task_id 读取 State DTO。

        Raises:
            SemanticNotFound: 任务不存在。
        """
        session = self._session()
        try:
            task, run = self._load_task_run(session, task_id)
            return self._state_dto(session, task, run)
        finally:
            session.close()

    def latest_task(self, bvid: str) -> Optional[Dict[str, Any]]:
        """取该视频最近创建的语义任务 State DTO（无则 None）。"""
        bvid = normalize_bvid(bvid, strict=False)
        session = self._session()
        try:
            run = (
                session.query(SemanticRun)
                .filter_by(bvid=bvid)
                .order_by(SemanticRun.id.desc())
                .first()
            )
            if run is None:
                return None
            task = session.query(Task).filter_by(id=run.task_id).first()
            if task is None:
                return None
            return self._state_dto(session, task, run)
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 运行态更新（CAS）
    # ------------------------------------------------------------------
    def update_task(self, task_id: int, expected_revision: int, **changes) -> Dict[str, Any]:
        """CAS 更新任务运行态（白名单字段，**不含 groups**）。

        已取消任务不可被 worker 覆盖 status/phase/progress/checkpoint；发布只走
        publish_groups，避免动态 JSON 覆盖 checkpoint 导致丢批次。

        Args:
            task_id: 任务 ID。
            expected_revision: 期望 run.revision。
            **changes: 仅 UPDATE_WHITELIST 内字段生效。

        Returns:
            新 State DTO（revision +1）。

        Raises:
            SemanticConflict: 修订不匹配、终态任务被 worker 改写、非法状态转换。
            SemanticValidationError: 含非白名单字段或非法 status 值。
        """
        bad = set(changes) - UPDATE_WHITELIST
        if bad:
            raise SemanticValidationError(f"不允许更新的字段: {sorted(bad)}")
        # 在创建会话/修改 ORM 前匹配 Float/Text 列；异常消息不回显输入内容。
        if "progress" in changes:
            progress = changes["progress"]
            if (isinstance(progress, bool) or not isinstance(progress, (int, float))
                    or not 0 <= progress <= 1):  # 也拒绝 NaN 和正负无穷。
                raise SemanticValidationError("progress 必须是 [0,1] 内的有限数值")
        if "error" in changes and changes["error"] is not None and not isinstance(changes["error"], str):
            raise SemanticValidationError("error 必须是安全字符串或 None")
        # status 合法值白名单：cancelled/interrupted 只能走专用方法。
        if "status" in changes and changes["status"] not in UPDATE_ALLOWED_STATUSES:
            raise SemanticValidationError(
                f"update_task 不接受 status={changes['status']!r}"
                f"（cancelled/interrupted 走 cancel_task/recover_interrupted）"
            )
        session = self._session()
        try:
            task, run = self._load_task_run(session, task_id)
            if int(run.revision) != int(expected_revision):
                raise SemanticConflict(
                    f"任务修订冲突：期望 {expected_revision}，实际 {run.revision}"
                )
            # 终态任务不可被 worker 覆盖（迟到结果不得复活）：cancelled/completed/failed/
            # partial/interrupted 一律拒绝普通更新；interrupted→queued 的恢复走 resume_task。
            if task.status in TERMINAL_STATUSES:
                raise SemanticConflict(f"任务处于终态 {task.status}，拒绝 worker 覆盖")
            # 只有 queued/running 可变更；queued→running 合法，running 内部推进合法。
            new_status = changes.get("status")
            if new_status == "running" and task.status not in ("queued", "running"):
                raise SemanticConflict(f"非法状态转换: {task.status} -> running")

            for field, value in changes.items():
                if field in ("status", "progress", "error", "result", "checkpoint"):
                    setattr(task, field, value)
                    if field == "status" and value in ("completed", "partial", "failed"):
                        task.completed_at = datetime.now()
                    if field == "status" and value == "running" and task.started_at is None:
                        task.started_at = datetime.now()
                elif field in ("phase", "coverage"):
                    setattr(run, field, value)
            run.revision = int(run.revision) + 1
            session.commit()
            return self._state_dto(session, task, run)
        except SemanticError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 预算：预约 / 结算（单事务原子 CAS）
    # ------------------------------------------------------------------
    def reserve_attempt(
        self, task_id: int, expected_revision: int, estimated_tokens: int
    ) -> Dict[str, Any]:
        """发请求前在一个事务内强制上限并持久化 in-flight 预约。

        强制三道闸门：任务 max_attempts、任务 token 预算、语义日 token 预算。
        通过则写入唯一 attempt_id 的 in_flight、尝试数 +1、请求数 +1、预约入账。

        Args:
            task_id: 任务 ID。
            expected_revision: 期望 run.revision。
            estimated_tokens: 保守估算 token（非负）。

        Returns:
            新 State DTO（含 in_flight，revision +1）。

        Raises:
            SemanticConflict: 修订不匹配 / 已取消或中断 / 已有在飞请求。
            SemanticBudgetExceeded: 任一预算闸门超限。
        """
        est = max(0, int(estimated_tokens or 0))
        session = self._session()
        try:
            task, run = self._load_task_run(session, task_id)
            if int(run.revision) != int(expected_revision):
                raise SemanticConflict(
                    f"任务修订冲突：期望 {expected_revision}，实际 {run.revision}"
                )
            if task.status in ("cancelled", "interrupted"):
                raise SemanticConflict(f"任务 {task.status}，拒绝预约")
            if run.in_flight:
                raise SemanticConflict("已有在飞请求未结算，串行约束拒绝并发预约")
            if int(run.attempt_count) >= int(run.max_attempts):
                raise SemanticBudgetExceeded(
                    f"任务尝试数耗尽: {run.attempt_count}/{run.max_attempts}"
                )
            # reserved 仅为尚未结算的预约；已结算的实际/估算用量同样占用预算。
            if int(run.reserved_tokens) + int(run.settled_tokens) + est > int(run.task_token_budget):
                raise SemanticBudgetExceeded("任务 token 预算耗尽")

            # 一次采样同时用于预算查询和 in_flight 归属，避免午夜读写跨日。
            reserve_date = _today()
            day = session.query(SemanticDailyUsage).filter_by(date=reserve_date).first()
            day_reserved = int(day.reserved_tokens) if day else 0
            day_settled = int(day.settled_tokens) if day else 0
            if day_reserved + day_settled + est > int(run.daily_token_budget):
                raise SemanticBudgetExceeded("语义日 token 预算耗尽")

            attempt_id = uuid.uuid4().hex
            run.in_flight = {
                "attempt_id": attempt_id,
                "estimated_tokens": est,
                "usage_estimated": True,
                # 记预约所属自然日，settle 必须对**该日**回收预约，不能用结算当日，
                # 否则跨午夜结算会从别的日扣减他人实时预约，造成日预算被绕过。
                "reserve_date": reserve_date,
            }
            run.attempt_count = int(run.attempt_count) + 1
            run.request_count = int(run.request_count) + 1
            run.reserved_tokens = int(run.reserved_tokens) + est
            if day is None:
                day = SemanticDailyUsage(
                    date=reserve_date, reserved_tokens=est, settled_tokens=0, request_count=1
                )
                session.add(day)
            else:
                day.reserved_tokens = day_reserved + est
                day.request_count = int(day.request_count) + 1
            run.revision = int(run.revision) + 1
            session.commit()
            return self._state_dto(session, task, run)
        except SemanticError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def settle_attempt(
        self, task_id: int, expected_revision: int, usage_tokens: Optional[int] = None
    ) -> Dict[str, Any]:
        """结算当前在飞请求：实际 usage 或保留估算，清 in_flight，更新台账。

        幂等：无在飞请求时直接返回当前状态（重复 settle 不重复记账，不误结算下一请求，
        也不做修订闸门）。并发取消时仍可结算（只记账不发布）。

        Args:
            task_id: 任务 ID。
            expected_revision: 期望 run.revision（仅当确有在飞请求时校验）。
            usage_tokens: 非负整数为实际用量；None/非法保留预约估算并标 estimated。

        Returns:
            新 State DTO。

        Raises:
            SemanticConflict: 有在飞请求但修订不匹配。
        """
        session = self._session()
        try:
            task, run = self._load_task_run(session, task_id)
            in_flight = run.in_flight
            if not in_flight:
                # 幂等：已结算/从未预约，原样返回，不改台账、不误结算后续。
                return self._state_dto(session, task, run)
            if int(run.revision) != int(expected_revision):
                raise SemanticConflict(
                    f"结算修订冲突：期望 {expected_revision}，实际 {run.revision}"
                )

            est = int(in_flight.get("estimated_tokens", 0))
            # 预约所属自然日：旧记录无该字段时回退到今天（仅影响极老的在飞记录）。
            reserve_date = in_flight.get("reserve_date") or _today()
            valid_actual = (
                usage_tokens is not None
                and isinstance(usage_tokens, int)
                and not isinstance(usage_tokens, bool)
                and usage_tokens >= 0
            )
            actual = int(usage_tokens) if valid_actual else est
            estimated = not valid_actual

            # 任务台账：settled 增加 actual；预约端回收估算 est（任务级与日期无关）。
            run.settled_tokens = int(run.settled_tokens) + actual
            run.reserved_tokens = max(0, int(run.reserved_tokens) - est)

            # 日台账拆成两件事，各归其日：
            #   1) 预约回收：对**预约所属日** reserve_date 扣回 est（不碰其他日的实时预约）。
            #   2) 实际入账：对**预约所属日** settled_tokens 记 actual（归属到发起请求的那天）。
            # 跨午夜场景下，结算当日的 reserved_tokens 代表当天别的任务的活跃预约，绝不被触碰。
            res_day = session.query(SemanticDailyUsage).filter_by(date=reserve_date).first()
            if res_day is None:
                # 预约日记录缺失（异常/极老数据）：只记实际入账，预约端无可回收，不臆造。
                res_day = SemanticDailyUsage(
                    date=reserve_date, reserved_tokens=0, settled_tokens=actual, request_count=0
                )
                session.add(res_day)
            else:
                res_day.settled_tokens = int(res_day.settled_tokens) + actual
                res_day.reserved_tokens = max(0, int(res_day.reserved_tokens) - est)

            run.in_flight = None
            run.revision = int(run.revision) + 1
            # 供恢复/展示标注：最近一次结算是否为估算。
            cov = dict(run.coverage or {})
            cov["last_settlement_estimated"] = estimated
            run.coverage = cov
            session.commit()
            return self._state_dto(session, task, run)
        except SemanticError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 崩溃恢复 / 继续 / 取消
    # ------------------------------------------------------------------
    def recover_interrupted(self) -> int:
        """把 queued/running 的语义任务标为 interrupted；在飞请求记 outcome_unknown。

        绝不自动发任何请求。进程崩溃时在飞请求可能已计费，故标注 outcome_unknown，
        等待用户显式确认后再 resume，不承诺 exactly-once。

        Returns:
            int: 受影响的任务数。
        """
        session = self._session()
        try:
            runs = (
                session.query(SemanticRun)
                .join(Task, Task.id == SemanticRun.task_id)
                .filter(Task.status.in_(list(ACTIVE_STATUSES)))
                .all()
            )
            affected = 0
            for run in runs:
                task = session.query(Task).filter_by(id=run.task_id).first()
                if task is None:
                    continue
                task.status = "interrupted"
                if run.in_flight:
                    in_flight = dict(run.in_flight)
                    in_flight["outcome_unknown"] = True
                    run.in_flight = in_flight
                run.phase = "interrupted"
                run.revision = int(run.revision) + 1
                affected += 1
            session.commit()
            return affected
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def resume_task(
        self, task_id: int, expected_revision: int, acknowledge_unknown: bool = False
    ) -> Dict[str, Any]:
        """把可恢复任务重置为 queued；保留既有计费，不重发已成功批次。

        要求任务处于 interrupted/partial/failed。存在 outcome_unknown 在飞请求时，
        必须 acknowledge_unknown=True 才允许继续（提示可能已计费）。

        Args:
            task_id: 任务 ID。
            expected_revision: 期望 run.revision。
            acknowledge_unknown: 是否确认承担未知计费风险。

        Returns:
            新 State DTO（status=queued）。

        Raises:
            SemanticConflict: 修订不匹配 / 状态不可恢复 / 未确认未知结果。
        """
        session = self._session()
        try:
            task, run = self._load_task_run(session, task_id)
            if int(run.revision) != int(expected_revision):
                raise SemanticConflict(
                    f"恢复修订冲突：期望 {expected_revision}，实际 {run.revision}"
                )
            if task.status not in RESUMABLE_STATUSES:
                raise SemanticConflict(f"任务状态 {task.status} 不可恢复")
            if run.in_flight and run.in_flight.get("outcome_unknown") and not acknowledge_unknown:
                raise SemanticConflict("存在结果未知的在飞请求，需显式确认后恢复")
            # 清掉已确认的未知在飞记录（不重发），保留既有计费台账。
            if run.in_flight and run.in_flight.get("outcome_unknown"):
                run.in_flight = None
            task.status = "queued"
            task.error = None
            run.phase = "queued"
            run.revision = int(run.revision) + 1
            session.commit()
            return self._state_dto(session, task, run)
        except SemanticError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def cancel_task(self, task_id: int, expected_revision: int) -> Dict[str, Any]:
        """取消任务（幂等终态）；修订闸门，不自动退款。

        已是 cancelled 直接返回当前状态（幂等）。取消后迟到结果不能复活分组，
        但仍可 settle 记账（见 settle_attempt）。

        Args:
            task_id: 任务 ID。
            expected_revision: 期望 run.revision（已取消时不校验，幂等返回）。

        Returns:
            State DTO（status=cancelled）。

        Raises:
            SemanticConflict: 修订不匹配（非幂等场景）。
        """
        session = self._session()
        try:
            task, run = self._load_task_run(session, task_id)
            if task.status == "cancelled":
                return self._state_dto(session, task, run)
            if int(run.revision) != int(expected_revision):
                raise SemanticConflict(
                    f"取消修订冲突：期望 {expected_revision}，实际 {run.revision}"
                )
            task.status = "cancelled"
            task.completed_at = datetime.now()
            run.phase = "cancelled"
            run.revision = int(run.revision) + 1
            session.commit()
            return self._state_dto(session, task, run)
        except SemanticError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 分组发布（原子、带不交叉与保护）
    # ------------------------------------------------------------------
    @staticmethod
    def _protected_rpids(session, bvid: str) -> set:
        """取该视频当前所有 keep_separate 的受保护 rpid 集合。"""
        rows = (
            session.query(SemanticProtection)
            .filter_by(bvid=bvid, keep_separate=True)
            .all()
        )
        return {str(r.rpid) for r in rows}

    def publish_groups(
        self,
        task_id: int,
        expected_revision: int,
        groups: List[Dict[str, Any]],
        related_groups: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """原子发布已校验等价组：删旧组、建新组行，发布与读取两处套用人工保护。

        入参 groups 的 member_ids/representative_id 为快照短 ID；store 指派稳定 group_id，
        映射回 rpid，剔除受保护成员；剔除后不足 2 人的组不发布（成员独立）。
        仅 worker-eligible(running) 状态可发布；取消/终态/修订过期一律 CAS 拒绝。

        Args:
            task_id: 任务 ID。
            expected_revision: 期望 run.revision。
            groups: [{representative_id, member_ids, reason, aspect, stance}]。
            related_groups: 相关主题组（只标注不折叠，当前仅接受不建折叠行）。

        Returns:
            新 State DTO（含已发布 groups，revision +1）。

        Raises:
            SemanticConflict: 修订不匹配 / 非 running / 已取消。
            SemanticValidationError: ID 未知/越界/重复/交叉/组内不足 2 人/代表不在组内。
        """
        session = self._session()
        try:
            task, run = self._load_task_run(session, task_id)
            if int(run.revision) != int(expected_revision):
                raise SemanticConflict(
                    f"发布修订冲突：期望 {expected_revision}，实际 {run.revision}"
                )
            if task.status == "cancelled":
                raise SemanticConflict("任务已取消，拒绝发布分组")
            if task.status != "running":
                raise SemanticConflict(f"仅 running 可发布，当前 {task.status}")

            snapshot = (task.params or {}).get("snapshot") or {}
            id_to_item = {it["id"]: it for it in snapshot.get("items", [])}
            protected = self._protected_rpids(session, run.bvid)

            normalized = self._validate_and_normalize_groups(
                task_id, groups or [], id_to_item, protected
            )

            # 原子重写：先删该任务旧组/成员，再插新行。
            session.query(SemanticGroupMember).filter_by(task_id=task_id).delete()
            session.query(SemanticGroup).filter_by(task_id=task_id).delete()
            for g in normalized:
                session.add(SemanticGroup(
                    group_id=g["group_id"],
                    task_id=task_id,
                    bvid=run.bvid,
                    representative_id=g["representative_id"],
                    representative_rpid=g["representative_rpid"],
                    reason=g.get("reason"),
                    aspect=g.get("aspect"),
                    stance=g.get("stance"),
                    member_count=len(g["members"]),
                ))
                for m in g["members"]:
                    session.add(SemanticGroupMember(
                        task_id=task_id,
                        group_id=g["group_id"],
                        bvid=run.bvid,
                        local_id=m["id"],
                        rpid=m["rpid"],
                        content_hash=m["content_hash"],
                        is_representative=(m["id"] == g["representative_id"]),
                    ))
            run.revision = int(run.revision) + 1
            session.commit()
            return self._state_dto(session, task, run)
        except SemanticError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _validate_and_normalize_groups(
        task_id: int,
        groups: List[Dict[str, Any]],
        id_to_item: Dict[str, Any],
        protected: set,
    ) -> List[Dict[str, Any]]:
        """严格校验分组并规范化为可落库结构，套用人工保护。

        校验：类型、已知快照 ID、成员不重复、组间不交叉、代表属于组、组>=2。
        保护：剔除受保护 rpid 成员；剔除后不足 2 人的组丢弃（成员独立保留）。
        模型无权作平台 ID——rpid 一律由 store 从快照映射得到。
        """
        seen_members: set = set()
        result: List[Dict[str, Any]] = []
        for gi, g in enumerate(groups):
            if not isinstance(g, dict):
                raise SemanticValidationError("分组项必须是对象")
            rep = g.get("representative_id")
            member_ids = g.get("member_ids")
            if not isinstance(member_ids, list) or not member_ids:
                raise SemanticValidationError("member_ids 必须是非空列表")
            if len({m for m in member_ids}) != len(member_ids):
                raise SemanticValidationError("组内成员 ID 重复")
            if rep not in member_ids:
                raise SemanticValidationError("代表必须属于本组")
            members = []
            for mid in member_ids:
                if mid not in id_to_item:
                    raise SemanticValidationError(f"未知快照 ID: {mid}")
                if mid in seen_members:
                    raise SemanticValidationError(f"成员跨组交叉: {mid}")
                item = id_to_item[mid]
                members.append({
                    "id": mid,
                    "rpid": str(item["rpid"]),
                    "content_hash": item["content_hash"],
                })
            # 原始组需 >=2（模型输出层面）；MAX_GROUP_SIZE 上界。
            if len(members) < 2:
                raise SemanticValidationError("等价组至少 2 名成员")
            if len(members) > MAX_GROUP_SIZE:
                raise SemanticValidationError(f"组成员超过上限 {MAX_GROUP_SIZE}")

            # 先登记占用（跨组不交叉以原始成员计），再套保护过滤。
            for m in members:
                seen_members.add(m["id"])

            kept = [m for m in members if m["rpid"] not in protected]
            if len(kept) < 2:
                # 受保护后不足 2 人：不发布折叠，成员独立展示。
                continue
            rep_rpid = id_to_item[rep]["rpid"]
            if str(rep_rpid) in protected:
                # 代表被保护：改选第一个保留成员为代表。
                rep = kept[0]["id"]
                rep_rpid = kept[0]["rpid"]
            result.append({
                "group_id": _stable_group_id(task_id, kept),
                "representative_id": rep,
                "representative_rpid": str(rep_rpid),
                "reason": g.get("reason"),
                "aspect": g.get("aspect"),
                "stance": g.get("stance"),
                "members": kept,
            })
        return result

    def _groups_dto(
        self, session, task_id: int, bvid: str, snapshot: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """读取已发布组，套用当前人工保护与 stale 标记后映射为 DTO。

        组内成员的完整原文/情感/用户字段**从发布时的 Task 快照按 local_id 映射回填**，
        不读当前 DB 原文（避免展示被后续改动污染，保持可复现）；stale 由当前内容哈希单独标注。
        """
        groups = (
            session.query(SemanticGroup)
            .filter_by(task_id=task_id)
            .order_by(SemanticGroup.id.asc())
            .all()
        )
        if not groups:
            return []
        protected = self._protected_rpids(session, bvid)
        # 快照 local_id -> 完整成员数据（原文/情感/用户/时间），用于回填组成员展示。
        snap_by_id = {it["id"]: it for it in (snapshot or {}).get("items", [])}
        members_by_group: Dict[str, List[SemanticGroupMember]] = {}
        for m in session.query(SemanticGroupMember).filter_by(task_id=task_id).all():
            members_by_group.setdefault(m.group_id, []).append(m)
        # 预取 rpid->当前内容，判断 stale。
        rpids = [str(m.rpid) for ms in members_by_group.values() for m in ms]
        current_hash: Dict[str, str] = {}
        if rpids:
            for c in session.query(Comment).filter(Comment.rpid.in_(rpids)).all():
                current_hash[str(c.rpid)] = content_hash(c.content)

        out: List[Dict[str, Any]] = []
        for g in groups:
            ms = members_by_group.get(g.group_id, [])
            folded_ids = []
            member_rows = []
            stale = False
            for m in ms:
                is_protected = str(m.rpid) in protected
                is_stale = _is_stale(current_hash.get(str(m.rpid)), m.content_hash)
                if is_stale:
                    stale = True
                # 受保护或 stale(含被删)成员都不自动折叠。
                if not is_protected and not is_stale:
                    folded_ids.append(str(m.rpid))
                snap_it = snap_by_id.get(m.local_id, {})
                member_rows.append({
                    "local_id": m.local_id,
                    "rpid": str(m.rpid),
                    "is_representative": bool(m.is_representative),
                    "keep_separate": is_protected,
                    "stale": is_stale,
                    # 完整原文与成员数据按快照 local_id 回填（不读当前 DB 原文）。
                    "content": snap_it.get("content", ""),
                    "sentiment": snap_it.get("sentiment"),
                    "uid": snap_it.get("uid"),
                    "uname": snap_it.get("uname", ""),
                    "ctime": snap_it.get("ctime"),
                    "like": snap_it.get("like", 0),
                })
            rep_item = snap_by_id.get(g.representative_id, {})
            out.append({
                "group_id": g.group_id,
                "representative_id": g.representative_id,
                "representative_rpid": str(g.representative_rpid),
                "representative_content": rep_item.get("content", ""),
                "reason": g.reason,
                "aspect": g.aspect,
                "stance": g.stance,
                "original_count": int(g.member_count),
                "member_count": len(member_rows),
                "current_folded_rpids": folded_ids,
                "members": member_rows,
                "stale": stale,
            })
        return out

    # ------------------------------------------------------------------
    # 结果读取（分页，无数据丢失）
    # ------------------------------------------------------------------
    def result(self, task_id: int, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
        """读取任务结果：全部入选原文分页 + 组 + 偏好 + 计数，保证无遗漏。

        items 为快照全部入选评论（含被排除/未知/未分组行），stats 以 rpid 去重；
        组内可内联成员行。分页覆盖全部 items，绝不让任何成员无可达路径。

        Args:
            task_id: 任务 ID。
            page: 页码（从 1 起）。
            page_size: 每页条数。

        Returns:
            {task, preference, revision, counts, groups, items, pagination}

        Raises:
            SemanticNotFound: 任务不存在。
        """
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 50), MAX_LIMIT))
        session = self._session()
        try:
            task, run = self._load_task_run(session, task_id)
            state = self._state_dto(session, task, run)
            snapshot = (task.params or {}).get("snapshot") or {}
            all_items = snapshot.get("items", [])
            protected = self._protected_rpids(session, run.bvid)

            # 当前内容哈希用于 stale 标记（原文可能已变）。
            rpids = [str(it["rpid"]) for it in all_items]
            current_hash: Dict[str, str] = {}
            if rpids:
                for c in session.query(Comment).filter(Comment.rpid.in_(rpids)).all():
                    current_hash[str(c.rpid)] = content_hash(c.content)

            total = len(all_items)
            start = (page - 1) * page_size
            page_slice = all_items[start:start + page_size]
            items = []
            for it in page_slice:
                rpid = str(it["rpid"])
                items.append({
                    "id": it["id"],
                    "rpid": rpid,
                    "content": it.get("content", ""),
                    "uid": it.get("uid"),
                    "uname": it.get("uname", ""),
                    "ctime": it.get("ctime"),
                    "sentiment": it.get("sentiment"),
                    "like": it.get("like", 0),
                    "context_status": it.get("context_status"),
                    "eligible": it.get("eligible", False),
                    "keep_separate": rpid in protected,
                    "stale": _is_stale(current_hash.get(rpid), it.get("content_hash")),
                })
            # 独立用户数按 rpid 对应 uid 去重（None 不计）。
            uids = {it.get("uid") for it in all_items if it.get("uid") is not None}
            public_task = {k: state[k] for k in (
                "task_id", "bvid", "status", "phase", "progress", "coverage", "budget",
                "error", "model_identity",
            )}
            return {
                "task": public_task,
                "preference": self._stored_preference(session, run.bvid),
                "revision": state["revision"],
                "counts": {
                    **(snapshot.get("counts", {})),
                    "independent_users": len(uids),
                    "groups": len(state["groups"]),
                },
                "groups": state["groups"],
                "items": items,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    # 统一 total_pages 供前端；保留 pages 作向后兼容别名。
                    "total_pages": (total + page_size - 1) // page_size if total else 0,
                    "pages": (total + page_size - 1) // page_size if total else 0,
                },
            }
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 人工决策（持久保护 + 审计 + 幂等）
    # ------------------------------------------------------------------
    def decide(
        self,
        bvid: str,
        task_id: int,
        action: str,
        expected_revision: int,
        group_id: Optional[str] = None,
        rpid: Optional[str] = None,
        request_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行人工决策，CAS 视频偏好修订，持久化 (bvid,rpid) 保护并记审计。

        actions:
          - undo_group:   给组内所有成员建 keep_separate 保护（恢复独立展示）。
          - split_member: 仅保护该成员 rpid。
          - allow_member: 解除该成员保护（显式）。
          - allow_group:  解除组内所有成员保护（显式）。
        保护键为 (video_id=bvid, rpid)，非 group hash；后续任何任务/代表变化先扣保护成员。
        幂等：相同 request_key 命中既有审计直接返回原结果。

        Args:
            bvid: 视频 BV 号。
            task_id: 关联任务。
            action: 上述四种之一。
            expected_revision: 期望偏好 revision（CAS）。
            group_id: undo_group/allow_group 必填。
            rpid: split_member/allow_member 必填。
            request_key: 幂等键。

        Returns:
            {revision, preference, action, affected_rpids}

        Raises:
            SemanticConflict: 偏好修订不匹配。
            SemanticValidationError: 参数缺失 / ID 越界 / 组不属于该任务。
            SemanticNotFound: 任务/组不存在。
        """
        if action not in ("undo_group", "split_member", "allow_member", "allow_group"):
            raise SemanticValidationError(f"未知操作: {action}")
        if not request_key:
            raise SemanticValidationError("request_key 不能为空")
        bvid = normalize_bvid(bvid, strict=False)
        session = self._session()
        try:
            # 幂等：命中既有审计直接返回。
            prior = (
                session.query(SemanticAudit)
                .filter_by(bvid=bvid, request_key=request_key)
                .first()
            )
            if prior is not None:
                return {
                    "revision": prior.revision_after,
                    "preference": self._stored_preference(session, bvid),
                    "action": prior.action,
                    "affected_rpids": (prior.detail or {}).get("affected_rpids", []),
                    "idempotent": True,
                }

            # 解析受影响 rpid 集合并校验范围/任务归属。
            affected = self._resolve_decision_targets(
                session, bvid, task_id, action, group_id, rpid
            )

            # CAS 偏好修订。
            pref = session.query(SemanticPreference).filter_by(bvid=bvid).first()
            if pref is None:
                if int(expected_revision) != 0:
                    raise SemanticConflict("偏好修订冲突：期望 0（首次创建）")
                pref = SemanticPreference(
                    bvid=bvid,
                    video_id=self._require_video(session, bvid).id,
                    auto_fold=True,
                    revision=0,
                )
                session.add(pref)
                session.flush()
            if int(pref.revision) != int(expected_revision):
                raise SemanticConflict(
                    f"偏好修订冲突：期望 {expected_revision}，实际 {pref.revision}"
                )
            rev_before = int(pref.revision)

            protect = action in ("undo_group", "split_member")
            for target in affected:
                row = (
                    session.query(SemanticProtection)
                    .filter_by(bvid=bvid, rpid=target)
                    .first()
                )
                if protect:
                    if row is None:
                        session.add(SemanticProtection(
                            bvid=bvid, rpid=target, keep_separate=True,
                            source=action, revision=rev_before,
                        ))
                    else:
                        row.keep_separate = True
                        row.source = action
                        row.revision = rev_before
                else:
                    if row is not None:
                        row.keep_separate = False
                        row.revision = rev_before

            pref.revision = rev_before + 1
            session.add(SemanticAudit(
                bvid=bvid, task_id=task_id, action=action, group_id=group_id,
                rpid=rpid, request_key=request_key,
                revision_before=rev_before, revision_after=pref.revision,
                detail={"affected_rpids": affected},
            ))
            session.commit()
            return {
                "revision": pref.revision,
                "preference": self._pref_dto(pref),
                "action": action,
                "affected_rpids": affected,
                "idempotent": False,
            }
        except SemanticError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _resolve_decision_targets(
        session, bvid: str, task_id: int, action: str,
        group_id: Optional[str], rpid: Optional[str],
    ) -> List[str]:
        """把一个决策解析为受影响 rpid 列表，并校验任务/组/成员归属。"""
        if action in ("undo_group", "allow_group"):
            if not group_id:
                raise SemanticValidationError("该操作需要 group_id")
            group = (
                session.query(SemanticGroup)
                .filter_by(task_id=task_id, group_id=group_id, bvid=bvid)
                .first()
            )
            if group is None:
                raise SemanticNotFound(f"组不存在或不属于该任务/视频: {group_id}")
            members = (
                session.query(SemanticGroupMember)
                .filter_by(task_id=task_id, group_id=group_id)
                .all()
            )
            return [str(m.rpid) for m in members]
        # split_member / allow_member
        if not rpid:
            raise SemanticValidationError("该操作需要 rpid")
        # 校验该 rpid 确属于本任务某个已发布组（防止错域引用）。
        member = (
            session.query(SemanticGroupMember)
            .filter_by(task_id=task_id, rpid=str(rpid))
            .first()
        )
        if member is None:
            raise SemanticValidationError(f"rpid 不属于该任务的任何分组: {rpid}")
        return [str(rpid)]
