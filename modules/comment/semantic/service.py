"""评论语义折叠 · 服务编排层（C 模块，拥有者：service agent）。

职责（参见 `.tmp/implementation-contract.md` 的 Service contract 节）
------------------------------------------------------------------
- 用户触发的语义折叠任务编排：对 store 冻结的**原始快照**里合格、已知顶楼评论
  做分批语义分组（phase1）+ 跨批次候选合并校验（crossphase），最终把经过
  **全量成员校验**的等价组交给 store 原子发布。
- 全局**串行**：每个 service 实例按当前事件循环惰性建 `Semaphore(1)`，同一时刻
  只跑一个语义任务的模型调用（**不**用跨事件循环的单例信号量，测试循环会变）。
- 预算 / 记账 / 落库全部委托 store（store 是真正的强制执行方）：
  每次模型调用前 `reserve_attempt`，调用后 `settle_attempt`（**先 settle 后
  checkpoint**），成功单元 `update_task(checkpoint=...)` 持久化；并发取消用 CAS
  版本栅栏协作式停止，不硬杀协程以免产生悬挂 in_flight；对在飞取消只按最新
  revision 结算记账、绝不复位 cancelled 状态。
- 不在 import 期构造 provider / 发网络请求；默认工厂惰性导入。

边界（本层**不做**的事）
----------------------
- 不做预算 / 每日额度的最终裁决（store 负责，`reserve_attempt` 抛
  `SemanticBudgetExceeded`）。
- 不编造 group_id / 平台 ID（store `publish_groups` 分配；人工保护 store 施加）。
- 不把 uid/uname/rpid 送进模型载荷（provider.sanitize 再兜底，本层也只取 id+content）。
- 不对未知网络结果自动无界重试（仅每批 `BATCH_ATTEMPTS` 次有记账的有界重试）。

本模块**只**拥有 `service.py`；store.py / contracts.py / provider.py / routes
由 A / B / D 拥有。错误与常量从 contracts（单一事实源）导入,不重定义。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from .contracts import (
    BATCH_ATTEMPTS,
    BATCH_SIZE,
    CONSENT_VERSION,
    DEFAULT_LIMIT,
    DEFAULT_LIMITS,
    INPUT_BYTES,
    MAX_GROUP_SIZE,
    MAX_LIMIT,
    MEMBERSHIP_KEY,
    PHASE_BATCH,
    PHASE_CROSS,
    RULE_VERSION,
    ProviderErrorCategory,
    SemanticBudgetExceeded,
    SemanticConflict,
    SemanticError,
    SemanticProviderError,
    SemanticValidationError,
    estimate_request_tokens,
)

logger = logging.getLogger(__name__)

#: crossphase 有界轮数上界(最小实现:1 个基础窗 + 1 次环移轮,给跨窗口代表候选
#: 一次同窗机会;不追求组合全覆盖,未复核对仍如实标 partial)。
MAX_CROSS_ROUNDS = 2

# --------------------------------------------------------------------------
# 纯函数辅助
# --------------------------------------------------------------------------

def _clamp_limit(limit: Optional[int]) -> int:
    """把请求 limit 收敛到 [1, MAX_LIMIT]，缺省回落 DEFAULT_LIMIT。"""
    if limit is None:
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    if value <= 0:
        return DEFAULT_LIMIT
    return min(value, MAX_LIMIT)


def normalize_identity(identity: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """把模型身份规整为 {api_base, model} 两键；api_base 只去尾部 "/"，
    保留 scheme/host/port/path；绝不含任何密钥。

    Args:
        identity: 原始身份 dict（可能含多余键或 None）。

    Returns:
        Optional[Dict[str, str]]: 规整后的两键字典；入参为 None 时返回 None。
    """
    if identity is None:
        return None
    api_base = identity.get("api_base")
    model = identity.get("model")
    base_str = "" if api_base is None else str(api_base).rstrip("/")
    model_str = "" if model is None else str(model)
    return {"api_base": base_str, "model": model_str}


def _identity_equal(left: Optional[Dict[str, Any]],
                    right: Optional[Dict[str, Any]]) -> bool:
    """两个模型身份是否等价（各自先规整为两键再比较）。"""
    return normalize_identity(left) == normalize_identity(right)


def _public_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """公开任务态投影：剔除 snapshot / checkpoint（路由保持内部），其余透传。

    model_identity 只含 {api_base, model}（store 本就不存密钥）；不删 groups——
    groups 是已发布的校验结果，前端需要可见。
    """
    if not isinstance(state, dict):
        return state
    return {k: v for k, v in state.items() if k not in ("snapshot", "checkpoint")}


def _eligible_items_in_order(snapshot: Dict[str, Any]) -> List[Dict[str, str]]:
    """从快照取合格、已知顶楼评论，按快照既定模型顺序脱敏成 {id, content}。

    快照的 items 已是确定性顺序（newest 排序后对选中切片反转得到的模型顺序），
    这里**不重排**，只按 eligible_ids 过滤并剥到 id+content（不带 uid/uname/rpid）。
    """
    eligible = set(snapshot.get("eligible_ids") or [])
    items: List[Dict[str, str]] = []
    for raw in snapshot.get("items") or []:
        cid = raw.get("id")
        if cid in eligible:
            items.append({"id": cid, "content": raw.get("content") or ""})
    return items


def _content_by_id(snapshot: Dict[str, Any]) -> Dict[str, str]:
    """快照内全部 id -> 原文映射（跨批次全量成员校验时取原文用）。"""
    return {raw.get("id"): (raw.get("content") or "")
            for raw in (snapshot.get("items") or []) if raw.get("id")}


def _chunk_items(items: List[Dict[str, Any]], video_title: str,
                 phase: str) -> List[List[Dict[str, Any]]]:
    """按 BATCH_SIZE 条数 + INPUT_BYTES 输入字节双上限切批。

    用 contracts.estimate_request_tokens 的同一套序列化口径度量字节,保证
    「切批依据」与「reserve 估算」「preflight 展示」三处一致,不各算各的。
    单条即超 INPUT_BYTES 时该条独占一批（store eligibility 已按 4000B 过滤内容,
    此处仅作结构兜底,不丢数据）。
    """
    batches: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for item in items:
        tentative = current + [item]
        est = estimate_request_tokens(tentative, video_title, phase)
        over_bytes = est.input_tokens > INPUT_BYTES  # input_tokens == 输入 UTF-8 字节
        if current and (len(tentative) > BATCH_SIZE or over_bytes):
            batches.append(current)
            current = [item]
        else:
            current = tentative
    if current:
        batches.append(current)
    return batches


class SemanticService:
    """语义折叠异步编排服务（运行期由 router 持有一个实例/事件循环）。

    依赖注入：
        store: 同步 SemanticStore（见 store.py）；缺省惰性构造真实实现。
        provider_factory: 零参工厂 -> ChatSemanticProvider（异步上下文管理器，
            import/构造期不建客户端、不触网；仅 __aenter__ 建 LLMClient）。
    """

    def __init__(self, store: Any = None,
                 provider_factory: Optional[Callable[[], Any]] = None) -> None:
        """注入 store 与 provider 工厂；缺省惰性构造真实实现（不在此触网）。"""
        self._store = store
        self._provider_factory = provider_factory
        # 每实例按事件循环惰性建信号量：测试里循环会变，跨循环单例会 "bound to a
        # different loop"。记录信号量所属循环,循环变了就重建。
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._semaphore_loop: Optional[asyncio.AbstractEventLoop] = None
        # 仅本实例的后台任务句柄（shutdown/recover 只管本实例;store 台账才是跨实例约束）。
        self._tasks: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------- 依赖惰性化
    @property
    def store(self) -> Any:
        """暴露底层 store（路由直接走同步 preference/decision 等）。缺省惰性构造。"""
        if self._store is None:
            from .store import SemanticStore  # 惰性：import 期不建 DB/会话
            self._store = SemanticStore()
        return self._store

    def _new_provider(self) -> Any:
        """用工厂建一个 provider 实例（零参、不触网；仅读配置/进 async with 时才建客户端）。"""
        if self._provider_factory is not None:
            return self._provider_factory()
        from .provider import ChatSemanticProvider  # 惰性导入,避免 import 期副作用
        return ChatSemanticProvider()

    def _provider_identity(self) -> Tuple[bool, Optional[Dict[str, str]]]:
        """不进 async with、不建客户端地读取 (configured, identity)。

        provider 契约：`configured` 为同步 bool 属性，`identity` 同步返回
        {api_base, model}；构造 provider 实例本身不触网。
        """
        provider = self._new_provider()
        configured = bool(getattr(provider, "configured", False))
        identity = normalize_identity(getattr(provider, "identity", None)) if configured else None
        return configured, identity

    def _get_semaphore(self) -> asyncio.Semaphore:
        """取当前运行事件循环上的全局串行信号量（循环变化则重建）。"""
        loop = asyncio.get_event_loop()
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(1)
            self._semaphore_loop = loop
        return self._semaphore

    # ------------------------------------------------------------- 公开 API
    async def preflight(self, bvid: str, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        """浏览器预检：返回快照计数/范围 + 模型是否配置/身份/是否需同意/版本/限额。

        **不**返回整个原始快照（只给 counts/scope + 配置状态）。不创建任务、不触网。
        """
        limit = _clamp_limit(limit)
        snapshot = self.store.snapshot(bvid, limit=limit)
        pref = self.store.preference(bvid)
        configured, identity = self._provider_identity()

        stored_identity = normalize_identity(pref.get("model_identity"))
        consent_ok = (
            configured
            and pref.get("consent_version") == CONSENT_VERSION
            and stored_identity is not None
            and _identity_equal(stored_identity, identity)
        )
        return {
            # 回传规范化后的 bvid（snapshot 已去空白），非 HTTP 直调也不回显原始入参。
            "bvid": (snapshot.get("video") or {}).get("bvid", bvid),
            "video": snapshot.get("video"),
            "counts": snapshot.get("counts"),
            "scope": snapshot.get("scope"),
            "model_configured": configured,
            "provider_identity": identity,
            "consent_needed": not consent_ok,
            "consent_version": CONSENT_VERSION,
            "rule_version": RULE_VERSION,
            "limits": dict(DEFAULT_LIMITS),
            "preference": {
                "auto_fold": pref.get("auto_fold"),
                "revision": pref.get("revision"),
                "consent_version": pref.get("consent_version"),
                "model_identity": stored_identity,
            },
        }

    async def start(self, bvid: str, limit: int, consent_version: str,
                    request_key: str) -> Dict[str, Any]:
        """校验「持久化的、与模型身份绑定的同意」后创建任务，快速返回公开态。

        不在 durable create 之前发任何模型请求；创建成功且是新 queued 任务才入队
        后台 worker（全局串行）。幂等 request_key 由 store 保证。
        """
        limit = _clamp_limit(limit)
        # 资源存在性是最基本的前提，必须先于 consent / 模型配置校验（issue #47）：
        # 否则 BV 号打错时用户只会看到 503「模型未配置」或 400「未同意」，排查方向被带偏。
        # store.preference 在视频不存在时抛 SemanticNotFound（-> 404），且只是一次轻量
        # 单行查询；完整 snapshot 仍留到所有门禁通过之后再做。
        pref = self.store.preference(bvid)

        if consent_version != CONSENT_VERSION:
            raise SemanticValidationError(
                "同意版本不匹配，请重新确认", reason="consent_version_mismatch")

        configured, identity = self._provider_identity()
        if not configured or identity is None:
            # 未配置模型：路由应映射 503（not configured）。复用 provider 错误族,
            # 不新造异常类型。
            raise SemanticProviderError(
                "未配置可用的语义折叠模型", category=ProviderErrorCategory.NOT_CONFIGURED)

        if pref.get("consent_version") != CONSENT_VERSION or not _identity_equal(
                pref.get("model_identity"), identity):
            # 同意缺失/与当前模型身份不绑定：要求前端走同意 modal 后再 start。
            raise SemanticValidationError(
                "尚未对当前模型身份授予语义折叠同意", reason="consent_required")

        snapshot = self.store.snapshot(bvid, limit=limit)
        state = self.store.create_task(snapshot, identity, dict(DEFAULT_LIMITS), request_key)
        if state.get("status") == "queued":
            self._schedule(state["task_id"])
        return _public_state(state)

    def status(self, task_id: str) -> Dict[str, Any]:
        """返回任务公开态（同步；store 读取短事务）。"""
        return _public_state(self.store.get_task(task_id))

    def results(self, task_id: str, page: int = 1,
                page_size: int = 50) -> Dict[str, Any]:
        """返回可分页的完整结果（同步委托 store.result；含全量原文/成员）。"""
        return self.store.result(task_id, page=page, page_size=page_size)

    async def cancel(self, task_id: str, expected_revision: int) -> Dict[str, Any]:
        """取消任务：CAS 版本栅栏、幂等终态。协作式——不硬杀在跑的协程，

        worker 下一次 reserve/update 命中 CAS 冲突即自行停止,在飞请求仍会
        settle 记账(不发布)。store 负责终态幂等与不自动退款。
        """
        state = self.store.cancel_task(task_id, expected_revision)
        return _public_state(state)

    async def resume(self, task_id: str, expected_revision: int,
                     acknowledge_unknown: bool = False) -> Dict[str, Any]:
        """续跑：要求 interrupted/partial/failed + 对未知在飞的 ack；模型身份变更则拒。

        不重发已成功的 checkpoint 单元;保留既往计费;在剩余预算内继续。
        """
        state = self.store.get_task(task_id)
        configured, identity = self._provider_identity()
        if not configured or identity is None:
            raise SemanticProviderError(
                "未配置可用的语义折叠模型", category=ProviderErrorCategory.NOT_CONFIGURED)
        if not _identity_equal(state.get("model_identity"), identity):
            # 当前模型身份与任务创建时不一致：拒绝续跑（可执行的报错）。
            raise SemanticConflict(
                "模型身份已变更，拒绝在不同身份下续跑；请新建任务")

        resumed = self.store.resume_task(
            task_id, expected_revision, acknowledge_unknown=acknowledge_unknown)
        if resumed.get("status") == "queued":
            self._schedule(resumed["task_id"])
        return _public_state(resumed)

    async def shutdown(self) -> None:
        """停机：取消本实例所有后台句柄，不发任何自动请求、不改 store 状态。

        未结算的在飞由下次 recover() 统一标 outcome_unknown。
        """
        handles = list(self._tasks.values())
        for handle in handles:
            handle.cancel()
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)
        self._tasks.clear()

    async def recover(self) -> Dict[str, Any]:
        """崩溃恢复：queued/running => interrupted，在飞记 outcome_unknown；无自动请求。

        跨实例的真实约束在 store 台账；这里只委托 store.recover_interrupted()。
        """
        return self.store.recover_interrupted()

    # ------------------------------------------------------------- 调度
    def _schedule(self, task_id: str) -> None:
        """为 task_id 入队一个后台 worker（已在跑则不重复入队）。"""
        existing = self._tasks.get(task_id)
        if existing is not None and not existing.done():
            return
        handle = asyncio.ensure_future(self._run(task_id))
        self._tasks[task_id] = handle

        def _on_done(_fut: asyncio.Future, _tid: str = task_id) -> None:
            # 仅当句柄仍是当前这个才摘除,避免误删续跑换上的新句柄。
            if self._tasks.get(_tid) is handle:
                self._tasks.pop(_tid, None)

        handle.add_done_callback(_on_done)

    async def _run(self, task_id: str) -> None:
        """后台 worker 外壳：全局串行信号量 + 异常归类（不吞取消）。

        关键:任何非取消/非并发处置的终止都必须把任务落到 failed,否则任务会
        永久停在 running(contract: 不得留悬挂 running)。
        """
        async with self._get_semaphore():
            try:
                await self._execute(task_id)
            except asyncio.CancelledError:
                # 停机硬取消:不改 store 状态,留给 recover() 标 interrupted/unknown。
                raise
            except SemanticConflict as exc:
                # 并发取消/版本栅栏/身份变更:任务已被 cancel_task 等旁路处置为终态,
                # 不可再覆写(cancelled 不可被 worker 改写),静默收尾。
                logger.info("语义任务 %s 被并发处置而停止: %s", task_id, exc)
            except SemanticError as exc:
                logger.warning("语义任务 %s 结束于语义错误: %r", task_id, exc)
                self._mark_failed_safe(task_id, "semantic_error", "语义折叠失败")
            except Exception:  # noqa: BLE001 - 后台任务不得把异常泄漏到事件循环
                logger.exception("语义任务 %s 异常终止", task_id)
                self._mark_failed_safe(task_id, "internal_error", "语义折叠内部错误")

    def _mark_failed_safe(self, task_id: str, reason: str, message: str) -> None:
        """把任务安全落到 failed(脱敏原因,绝不回原始模型/服务端报文)。

        先读当前 revision 再 CAS;若任务已终态(cancelled/completed 等)则 CAS 冲突,
        不覆写终态(debug 留痕,不静默吞)。
        """
        try:
            current = self.store.get_task(task_id)
        except SemanticError as exc:
            logger.debug("标记 failed 前读取任务 %s 失败: %r", task_id, exc)
            return
        if current.get("status") in ("cancelled", "completed", "partial", "failed", "interrupted"):
            return  # 已终态,不覆写
        self._fail(task_id, current["revision"], reason, message)

    # ------------------------------------------------------------- 记账原语
    def _reserve(self, task_id: str, revision: int,
                 estimated_tokens: int) -> Tuple[int, Dict[str, Any]]:
        """预约一次 attempt（模型调用前）。返回 (新 revision, state)。

        预算耗尽抛 SemanticBudgetExceeded;取消/中断/版本过期/在飞冲突抛
        SemanticConflict —— 均向上传递由调用方处置。
        """
        state = self.store.reserve_attempt(task_id, revision, estimated_tokens)
        return state["revision"], state

    def _settle(self, task_id: str, revision: int,
                usage_tokens: Optional[int]) -> Dict[str, Any]:
        """结算当前在飞 attempt（模型调用后）。返回结算后的 state。

        若并发取消导致 CAS 冲突:按**最新** revision 再结算一次(仅记账,不复位
        cancelled 状态);store 允许 cancelled 下"只记账不发布"。
        返回 state 让调用方据 status 判断是否应停止(绝不在非 running 下继续发布)。
        """
        try:
            return self.store.settle_attempt(task_id, revision, usage_tokens)
        except SemanticConflict:
            latest = self.store.get_task(task_id)
            return self.store.settle_attempt(task_id, latest["revision"], usage_tokens)

    def _settle_quiet(self, task_id: str, revision: int,
                      usage_tokens: Optional[int]) -> None:
        """尽力结算(取消、契约外异常路径):仅记账,吞异常(debug 留痕),不改变调用方语义。"""
        try:
            self._settle(task_id, revision, usage_tokens)
        except SemanticError as exc:
            logger.debug("尽力结算任务 %s 记账失败(可接受): %r", task_id, exc)

    async def _grouped_call(self, task_id: str, revision: int, provider: Any,
                            items: List[Dict[str, Any]], video_title: str,
                            phase: str) -> Tuple[int, Optional[Dict[str, Any]]]:
        """一次「有界重试」的分组调用：每次尝试都 reserve->call->settle 计账。

        返回 (新 revision, validated_grouping|None)。None 表示本单元在 BATCH_ATTEMPTS
        次内未取得可用响应(降级为各自独立,调用方据此处理)。

        纪律:
        - reserve 在 provider 调用之前;settle 在之后(无论成功/失败/未知都结算,
          不退款)。
        - SemanticBudgetExceeded / SemanticConflict 向上传递(预算耗尽 / 并发取消)。
        - UNSUPPORTED_TEMPERATURE 允许一次 omit_temperature 重试(占一次 attempt)。
        - 未知网络结果不做无界重试:仅在 BATCH_ATTEMPTS 预算内有记账地重试。
        """
        est = estimate_request_tokens(items, video_title, phase)
        omit_temperature = False
        last_exc: Optional[SemanticError] = None

        for _try in range(BATCH_ATTEMPTS):
            revision, _ = self._reserve(task_id, revision, est.total_tokens)
            usage_tokens: Optional[int] = None
            try:
                result = await provider.group(
                    items, video_title, phase=phase, omit_temperature=omit_temperature)
            except asyncio.CancelledError:
                # 硬取消发生在模型调用中:仍结算在飞记账(不退款),再原样上抛;
                # 绝不返回数据 => 绝不发布。
                self._settle_quiet(task_id, revision, None)
                raise
            except SemanticProviderError as exc:
                # 传输/参数层失败:已记账(settle),按分类决定是否有界重试。
                state = self._settle(task_id, revision, None)
                revision = state["revision"]
                self._stop_if_not_running(state)  # 并发取消 => 抛 Conflict,不再续发
                last_exc = exc
                if exc.category == ProviderErrorCategory.UNSUPPORTED_TEMPERATURE and not omit_temperature:
                    omit_temperature = True  # 下一轮省略 temperature 重试
                    continue
                if exc.category == ProviderErrorCategory.NETWORK:
                    continue  # 未知网络:在剩余 BATCH_ATTEMPTS 内有界重试
                break  # auth/model_not_found/token_limit 等:重试无益,停
            except SemanticValidationError as exc:
                # 拿到了响应但不合法(截断/拒答/ID 非法等):已记账,有界重试。
                state = self._settle(task_id, revision, None)
                revision = state["revision"]
                self._stop_if_not_running(state)
                last_exc = exc
                continue
            except Exception:
                # provider 抛了契约外的异常(bug / 未包装的第三方异常):请求可能已发出,
                # 先按估算结算清掉在飞记录再原样上抛,由 _run 标 failed。否则 in_flight
                # 残留,续跑时 reserve 抛"已有在飞请求"被当作并发处置吞掉,任务卡在 running。
                self._settle_quiet(task_id, revision, None)
                raise
            # 成功路径:结算实际/预留用量,再据结算后状态判断是否应停止。
            usage = result.get("usage_tokens")
            usage_estimated = bool(result.get("usage_estimated", False))
            if isinstance(usage, int) and usage >= 0 and not usage_estimated:
                usage_tokens = usage  # 有效真实用量:结算实际
            state = self._settle(task_id, revision, usage_tokens)
            revision = state["revision"]
            # 并发取消已把任务改为非 running:抛 Conflict 让 worker 停,结果绝不发布。
            self._stop_if_not_running(state)
            return revision, result.get("data")
        if last_exc is not None:
            logger.info("分组调用降级为独立(phase=%s): %r", phase, last_exc)
        return revision, None

    @staticmethod
    def _stop_if_not_running(state: Dict[str, Any]) -> None:
        """结算后若任务已非 running(并发取消/中断/终态),抛 Conflict 让 worker 立停。

        这是「取消绝不发布」的硬保证:settle 只负责记账,一旦发现状态被旁路改动,
        worker 不得再继续分组或发布(即使本次已拿到可用 data)。
        """
        if state.get("status") != "running":
            raise SemanticConflict("任务已被并发处置,停止编排(不发布)")

    # ------------------------------------------------------------- 主执行
    async def _execute(self, task_id: str) -> None:
        """跑完一个任务：校验身份 -> 置 running -> phase1 -> crossphase -> 发布。

        预算中途耗尽 => 发布已得组、标 partial;并发取消 => CAS 冲突向上冒泡到
        _run 静默收尾(在飞已 settle 记账、未发布)。
        """
        state = self.store.get_task(task_id)
        if state.get("status") != "queued":
            return  # 已被别的 worker 领取或已终态
        revision = state["revision"]

        configured, identity = self._provider_identity()
        if not configured or identity is None:
            self._fail(task_id, revision, "not_configured", "模型未配置")
            return
        if not _identity_equal(state.get("model_identity"), identity):
            self._fail(task_id, revision, "identity_changed",
                       "模型身份已变更，拒绝续跑")
            return

        snapshot = state.get("snapshot") or {}
        checkpoint = dict(state.get("checkpoint") or {})
        video_title = (snapshot.get("video") or {}).get("title") or ""
        eligible = _eligible_items_in_order(snapshot)

        state = self.store.update_task(
            task_id, revision, status="running", phase=PHASE_BATCH)
        revision = state["revision"]

        # 零合格项：直接发布空结果,标 completed。
        if not eligible:
            self.store.publish_groups(task_id, revision, [], related_groups=[])
            state = self.store.get_task(task_id)
            self.store.update_task(task_id, state["revision"],
                                   status="completed", phase="done",
                                   coverage={"eligible": 0, "partial": False})
            return

        budget_hit = False
        provider = self._new_provider()
        async with provider:
            # ---------- phase1：批内分组 ----------
            try:
                revision, groups, related, batch_count, phase1_partial = \
                    await self._run_phase1(task_id, revision, provider,
                                           eligible, video_title, checkpoint)
            except SemanticBudgetExceeded:
                # 预算耗尽:用已 checkpoint 的 phase1 结果发布 partial。
                revision, groups, related, batch_count, phase1_partial = \
                    self._phase1_from_checkpoint(task_id)
                budget_hit = True

            # ---------- crossphase：跨批候选原成员复核 ----------
            coverage: Dict[str, Any]
            if budget_hit or batch_count <= 1:
                # 预算已耗尽,或只有一批(批内已全覆盖,无跨批可做)。
                coverage = {
                    "batches": batch_count,
                    "cross_windows": 0,
                    "pairs_visited": 0,
                    "pairs_unvisited": 0,
                    "oversize_skipped": 0,
                    "partial": bool(budget_hit or phase1_partial),
                    "reason": "budget" if budget_hit else ("phase1_degraded" if phase1_partial else None),
                }
            else:
                self.store.update_task(task_id, revision, phase=PHASE_CROSS)
                revision = self.store.get_task(task_id)["revision"]
                try:
                    revision, groups, related, coverage = await self._run_cross(
                        task_id, revision, provider, snapshot, eligible,
                        groups, related, batch_count, phase1_partial)
                except SemanticBudgetExceeded:
                    coverage = {
                        "batches": batch_count, "cross_windows": 0,
                        "pairs_visited": 0, "pairs_unvisited": -1,
                        "oversize_skipped": 0, "partial": True, "reason": "budget",
                    }
                    budget_hit = True

        # ---------- 发布 ----------
        # 关键(reviewer fix 2):预算耗尽等路径下本地 revision 可能落后于 store 内部
        # reserve/settle 的自增(异常跨越了返回点)。发布前**重读最新 revision**,避免
        # 用陈旧 revision 触发 publish CAS 冲突 -> 误判"被并发处置" -> 任务永久 running。
        # 并发取消会把状态改为非 running,publish_groups 自身的 running-only 守卫仍会
        # 抛 SemanticConflict 冒泡到 _run 静默收尾(取消绝不发布),不受重读影响。
        partial = bool(coverage.get("partial") or budget_hit)
        publishable = [g for g in groups if len(g.get("member_ids", [])) >= 2]
        latest = self.store.get_task(task_id)
        if latest.get("status") != "running":
            # 已被旁路处置(取消/中断/终态):不发布、交由 _run 依冒泡异常收尾。
            raise SemanticConflict("发布前发现任务已非 running,停止(不发布)")
        state = self.store.publish_groups(
            task_id, latest["revision"], publishable, related_groups=related)
        self.store.update_task(
            task_id, state["revision"],
            status="partial" if partial else "completed",
            phase="done", coverage=coverage)

    def _fail(self, task_id: str, revision: int, reason: str, message: str) -> None:
        """把任务标 failed（安全原因码 + 脱敏消息，绝不回原始模型/服务端报文）。"""
        # Task.error 是 Text 列(非 JSON):必须传安全**字符串**,不能传 dict(否则
        # StatementError/ProgrammingError 落库失败 -> 任务永久 running)。原因码与脱敏
        # 消息编成单行字符串,绝不含原始模型/服务端报文。
        try:
            self.store.update_task(task_id, revision, status="failed",
                                   error=f"{reason}: {message}")
        except SemanticConflict as exc:
            # 已终态/被旁路(如并发 cancel):不可覆写终态,debug 留痕(不静默吞)。
            logger.debug("标记任务 %s failed 时命中 CAS 冲突(已终态): %r", task_id, exc)

    # ------------------------------------------------------------- phase1
    async def _run_phase1(self, task_id: str, revision: int, provider: Any,
                          eligible: List[Dict[str, str]], video_title: str,
                          checkpoint: Dict[str, Any]
                          ) -> Tuple[int, List[Dict[str, Any]],
                                     List[Dict[str, Any]], int, bool]:
        """phase1 顺序批内分组：每批 reserve->call->settle->checkpoint。

        resume 时跳过 checkpoint 里已成功的批(不重发);settle 成功但 checkpoint 前
        崩溃的批会在 resume 时重跑(有记账的受控重复,不承诺绝不重发)。
        返回 (revision, 等价组, related 组, 批数, 是否有批降级)。
        """
        batches = _chunk_items(eligible, video_title, PHASE_BATCH)
        marks: Dict[str, Dict[str, Any]] = dict(
            (checkpoint.get("phase1") or {}).get("batches") or {})

        for idx, batch in enumerate(batches):
            key = str(idx)
            mark = marks.get(key)
            if mark is not None and mark.get("status") == "done":
                continue  # 已成功单元:绝不重发
            # settle 成功但 checkpoint 前崩溃的窗口:先持久化 "pending" 标记,崩溃后
            # resume 看到 pending => 视为 outcome_unknown,须 acknowledge_unknown 才
            # 作有记账的受控重跑(不静默重发、不承诺绝不重发)。
            batch_ids = [it["id"] for it in batch]
            marks[key] = {"ids": batch_ids, "status": "pending"}
            revision = self._persist_phase1(task_id, revision, marks, len(batches))

            revision, data = await self._grouped_call(
                task_id, revision, provider, batch, video_title, PHASE_BATCH)
            if data is None:
                # 降级:本批各条独立(不编造等价),标降级 -> 整体 partial。
                marks[key] = {"ids": batch_ids, "status": "done",
                              "equivalent_groups": [], "related_groups": [],
                              "degraded": True}
            else:
                marks[key] = {
                    "ids": batch_ids, "status": "done",
                    "equivalent_groups": data.get("equivalent_groups") or [],
                    "related_groups": data.get("related_groups") or [],
                    "degraded": False,
                }
            # 先 settle(在 _grouped_call 内)后 checkpoint:持久化已完成单元。
            revision = self._persist_phase1(task_id, revision, marks, len(batches))

        groups, related = self._collect_phase1(marks)
        # 降级标志从**全部** marks 计算(含 resume 跳过的既有 done 批),不可因 resume
        # 把之前批的 degraded 清零(reviewer fix 4)。
        degraded = self._marks_degraded(marks, len(batches))
        return revision, groups, related, len(batches), degraded

    def _persist_phase1(self, task_id: str, revision: int,
                        marks: Dict[str, Dict[str, Any]], total: int) -> int:
        """持久化 phase1 checkpoint + progress,返回新 revision。

        关键:读当前 checkpoint 再合并,**不得整体覆写**(否则会抹掉已写的 cross 段;
        resume 时 phase1 先于 cross 执行,整体覆写会丢失跨批进度导致重复收费)。
        """
        cp = self._task_checkpoint(task_id)
        cp["phase1"] = {"batches": marks, "total": total}
        # progress 是 store 的 Float 列(0-1),必须传浮点分数,不能传 dict(会在
        # _state_dto 的 float() 处抛 TypeError;批次明细已在 checkpoint.phase1 里)。
        done = self._done_count(marks)
        fraction = (done / total) if total else 1.0
        state = self.store.update_task(
            task_id, revision, checkpoint=cp, progress=float(fraction))
        return state["revision"]

    @staticmethod
    def _marks_degraded(marks: Dict[str, Dict[str, Any]], total: int) -> bool:
        """phase1 是否降级:任一 done 批标降级,或仍有未完成(pending/缺失)批。"""
        any_degraded = any(b.get("degraded") for b in marks.values()
                           if b.get("status") == "done")
        done_n = sum(1 for m in marks.values() if m.get("status") == "done")
        return bool(any_degraded or done_n < total)

    @staticmethod
    def _done_count(marks: Dict[str, Dict[str, Any]]) -> int:
        """已成功(status==done)批数(用于 progress 展示)。"""
        return sum(1 for m in marks.values() if m.get("status") == "done")

    def _phase1_from_checkpoint(self, task_id: str
                                ) -> Tuple[int, List[Dict[str, Any]],
                                           List[Dict[str, Any]], int, bool]:
        """预算耗尽时,从已持久化 checkpoint 还原 phase1 已得结果。"""
        state = self.store.get_task(task_id)
        revision = state["revision"]
        phase1 = (state.get("checkpoint") or {}).get("phase1") or {}
        marks = phase1.get("batches") or {}
        total = phase1.get("total") or len(marks)
        groups, related = self._collect_phase1(marks)
        # 还有未完成(pending/未处理)批次或任一降级 => 视为降级(partial)。
        degraded = self._marks_degraded(marks, int(total))
        return revision, groups, related, int(total), degraded

    @staticmethod
    def _collect_phase1(marks: Dict[str, Dict[str, Any]]
                        ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """把各批**已成功**结果汇总成全局等价组 + related 组列表。"""
        groups: List[Dict[str, Any]] = []
        related: List[Dict[str, Any]] = []
        for _key, b in sorted(marks.items(), key=lambda kv: int(kv[0])):
            if b.get("status") != "done":
                continue  # pending/未完成单元不计入结果(避免把未知当成功)
            groups.extend(b.get("equivalent_groups") or [])
            related.extend(b.get("related_groups") or [])
        return groups, related

    # ------------------------------------------------------------- crossphase
    async def _run_cross(self, task_id: str, revision: int, provider: Any,
                         snapshot: Dict[str, Any], eligible: List[Dict[str, str]],
                         groups: List[Dict[str, Any]], related: List[Dict[str, Any]],
                         batch_count: int, phase1_partial: bool
                         ) -> Tuple[int, List[Dict[str, Any]],
                                    List[Dict[str, Any]], Dict[str, Any]]:
        """跨批候选比较 + 原成员复核。

        流程(严格无「A~B~C 盲传递合并」):
        1. 候选集 = 当前各组代表 + 尚未入组的单条(singleton),每个候选携带其
           **原始成员** id 列表(members 元数据)。
        2. 按 BATCH_SIZE/INPUT_BYTES 把候选切「窗口」。窗口内让模型在**代表之间**
           提等价候选;跨窗口的对视为**未访问**(coverage 标 partial),不臆断等价。
        3. 对每个被提议合并的候选集,展开到**全部原始成员**做一次独立复核
           (<=MAX_GROUP_SIZE 且不超 INPUT_BYTES);模型须把全部成员判为单一等价组
           才接受合并,否则保持原状(related/uncertain 绝不自动折叠)。
        4. 接受的合并**原子替换**被并入的旧组;未并入的原组保留。
        返回 (revision, 新组集, related, coverage)。
        """
        content = _content_by_id(snapshot)
        grouped_ids = {m for g in groups for m in g.get("member_ids", [])}

        # 候选:组代表(携带该组全部成员) + 未入组单条(成员即自身)。
        candidates: List[Dict[str, Any]] = []
        rep_to_group: Dict[str, Dict[str, Any]] = {}
        for g in groups:
            rep = g.get("representative_id")
            members = list(g.get("member_ids", []))
            if not rep or not members:
                continue
            candidates.append({"id": rep, "content": content.get(rep, ""),
                               MEMBERSHIP_KEY: members})
            rep_to_group[rep] = g
        singletons: Dict[str, Dict[str, Any]] = {}
        for it in eligible:
            if it["id"] not in grouped_ids:
                candidates.append({"id": it["id"], "content": it["content"],
                                   MEMBERSHIP_KEY: [it["id"]]})
                singletons[it["id"]] = it

        video_title = (snapshot.get("video") or {}).get("title") or ""
        cand_ids = [c["id"] for c in candidates]
        total_pairs = len(cand_ids) * (len(cand_ids) - 1) // 2

        # 可变组表:rep -> group;合并时在此原子替换。
        working: Dict[str, Dict[str, Any]] = dict(rep_to_group)

        # 已**成功复核**过的候选对(无序对,sorted tuple);只在窗口拿到可用响应后计入,
        # 绝不预计算(reviewer: 不得在模型跑之前虚报 visited)。
        reviewed_pairs: set = set()
        coverage: Dict[str, Any] = {
            "batches": batch_count,
            "candidates": len(cand_ids),
            "total_pairs": total_pairs,
            "rounds_done": 0,
            "windows_attempted": 0,
            "windows_reviewed": 0,
            "pairs_reviewed": 0,
            "pairs_unvisited": total_pairs,
            "oversize_skipped": 0,
            "partial": bool(phase1_partial),
            "reason": "phase1_degraded" if phase1_partial else None,
        }

        # crossphase checkpoint(reviewer fix 1):按**轮**记录进度 + working/coverage/
        # reviewed_pairs;resume 跳过已完成轮,**不重复收费**跨批比较/复核调用。
        cross_cp = (self._task_checkpoint(task_id).get("cross") or {})
        round_start = int(cross_cp.get("rounds_done") or 0)
        if cross_cp.get("working"):
            working = {g["representative_id"]: g for g in cross_cp["working"]}
        if cross_cp.get("coverage"):
            coverage = dict(cross_cp["coverage"])
        reviewed_pairs = {tuple(p) for p in (cross_cp.get("reviewed_pairs") or [])}
        if "singletons" in cross_cp:
            # 已在前一轮被合并进组的单条不能在续跑时复活成单条(否则会被重复比较、计费)。
            keep = set(cross_cp.get("singletons") or [])
            singletons = {cid: it for cid, it in singletons.items() if cid in keep}
        # 轮内进度:中断前这一轮的窗口划分与已完成窗口序号。只对 round_start 这一轮有效。
        saved_plan: Optional[List[List[str]]] = None
        saved_done: List[int] = []
        # 中断时正在合并复核的窗口:已拿到的分组提议与下一个待复核提议的序号。
        saved_in_progress: Optional[Dict[str, Any]] = None
        if int(cross_cp.get("plan_round", -1)) == round_start and cross_cp.get("plan"):
            saved_plan = [list(w) for w in cross_cp["plan"]]
            saved_done = [int(i) for i in (cross_cp.get("windows_done") or [])]
            if isinstance(cross_cp.get("window_in_progress"), dict):
                saved_in_progress = dict(cross_cp["window_in_progress"])

        # 轮转:单窗口时 1 轮即全覆盖;多窗口时做有界多轮(每轮换偏移,让跨窗口代表
        # 候选至少有一次同窗机会),轮数上界受预算与 MAX_CROSS_ROUNDS 双重限制,
        # 不追求组合全覆盖(明确标 partial),但不因固定顺序永久漏掉最后窗口。
        first_windows = _chunk_items(candidates, video_title, PHASE_CROSS)
        max_rounds = 1 if len(first_windows) <= 1 else min(
            MAX_CROSS_ROUNDS, len(first_windows))

        # 预算异常分支需要知道停在哪一轮、哪些窗口已完成。
        rnd = round_start
        plan: Optional[List[List[str]]] = None
        done_windows: set = set()
        current: Optional[Dict[str, Any]] = None  # 正在合并复核的窗口进度
        try:
            for rnd in range(round_start, max_rounds):
                if rnd == round_start and saved_plan is not None:
                    # 续跑同一轮:用中断前持久化的窗口划分,跳过已完成窗口,不重复收费。
                    windows = self._windows_from_plan(saved_plan, working, singletons, content)
                    done_windows = set(saved_done)
                else:
                    windows = self._cross_windows(
                        list(working.values()), singletons, content, video_title, rnd)
                    done_windows = set()
                plan = [[it["id"] for it in window] for window in windows]
                for w_idx, window in enumerate(windows):
                    if w_idx in done_windows:
                        continue
                    if len(window) < 2:
                        done_windows.add(w_idx)
                        continue
                    resumed = None
                    if saved_in_progress is not None and int(saved_in_progress.get("index", -1)) == w_idx:
                        resumed = saved_in_progress
                    saved_in_progress = None  # 只对续跑后遇到的第一个未完成窗口有效
                    if resumed is not None:
                        # 续跑:本窗口的分组调用在中断前已成功并计入 reviewed_pairs,
                        # 只把剩下的合并提议复核完,不重做分组调用、不重复计数。
                        proposals = [list(p) for p in (resumed.get("proposals") or [])]
                        next_k = int(resumed.get("next") or 0)
                    else:
                        coverage["windows_attempted"] += 1
                        revision, data = await self._grouped_call(
                            task_id, revision, provider, window, video_title, PHASE_CROSS)
                        if data is None:
                            # 本窗口未取得可用响应:不把其对计入 reviewed(不虚报),标 partial。
                            coverage["partial"] = True
                            coverage["reason"] = coverage["reason"] or "cross_degraded"
                            done_windows.add(w_idx)
                            revision = self._persist_cross(
                                task_id, revision, working, coverage, reviewed_pairs,
                                singletons, rnd, plan, done_windows)
                            continue
                        # 成功复核:本窗口内所有候选对计入 reviewed_pairs(真实覆盖)。
                        coverage["windows_reviewed"] += 1
                        wids = [it["id"] for it in window]
                        for i in range(len(wids)):
                            for j in range(i + 1, len(wids)):
                                reviewed_pairs.add(tuple(sorted((wids[i], wids[j]))))
                        proposals = [list(p.get("member_ids") or [])
                                     for p in (data.get("equivalent_groups") or [])]
                        proposals = [p for p in proposals if len(p) >= 2]
                        next_k = 0
                        if proposals:
                            # 分组结果先落盘:之后的合并复核若中途停下,续跑从下一个提议继续,
                            # 不再为本窗口重新付一次分组调用。
                            current = {"index": w_idx, "proposals": proposals, "next": 0}
                            revision = self._persist_cross(
                                task_id, revision, working, coverage, reviewed_pairs,
                                singletons, rnd, plan, done_windows, current)
                    current = {"index": w_idx, "proposals": proposals, "next": next_k}
                    # 同一窗口的提议成员互不相交(validate_grouping 保证),前面提议的合并
                    # 不会影响后面提议引用的候选,可以逐个提议续跑。
                    for k in range(next_k, len(proposals)):
                        rep_ids = proposals[k]
                        revision, merged, outcome = await self._verify_merge(
                            task_id, revision, provider, content, video_title,
                            rep_ids, working, singletons)
                        if outcome == "merged" and merged is not None:
                            self._apply_merge(working, singletons, rep_ids, merged)
                        elif outcome == "oversize":
                            coverage["oversize_skipped"] += 1
                            coverage["partial"] = True
                            coverage["reason"] = coverage["reason"] or "oversize"
                        elif outcome == "unverified":
                            coverage["partial"] = True
                            coverage["reason"] = coverage["reason"] or "verify_unverified"
                        # "rejected":模型明确不合并,完整判定,不降级。
                        current = {"index": w_idx, "proposals": proposals, "next": k + 1}
                        if k + 1 < len(proposals):
                            # 每复核完一个提议就落盘,续跑从下一个提议继续。
                            revision = self._persist_cross(
                                task_id, revision, working, coverage, reviewed_pairs,
                                singletons, rnd, plan, done_windows, current)
                    # 窗口的分组调用与全部合并复核都完成才记为 done。
                    done_windows.add(w_idx)
                    current = None
                    # 不在这里重算覆盖率:_recompute_cross_coverage 对 reason 先写先得,逐窗口重算会在
                    # 预算停下前就写成 cross_pairs_unvisited,盖掉 budget。轮结束和函数末尾都会重算。
                    revision = self._persist_cross(
                        task_id, revision, working, coverage, reviewed_pairs,
                        singletons, rnd, plan, done_windows)
                # 先 settle(在 _grouped_call 内)后 checkpoint:持久化本轮进度。
                coverage["rounds_done"] = rnd + 1
                self._recompute_cross_coverage(coverage, reviewed_pairs)
                revision = self._persist_cross(
                    task_id, revision, working, coverage, reviewed_pairs, singletons)
                plan, done_windows = None, set()
        except SemanticBudgetExceeded:
            # 跨批途中预算耗尽:标 partial、据**已真实复核**的对结算覆盖,不虚报。
            coverage["partial"] = True
            coverage["reason"] = coverage["reason"] or "budget"
            # 异常跨越了 _grouped_call 的返回点:同一单元里先前尝试的 reserve/settle
            # 已推进 store 的 revision,本地值是旧的。必须重读,否则写 checkpoint 时
            # CAS 冲突抛 SemanticConflict,被 _run 当作"并发处置"吞掉,任务永久 running。
            revision = self.store.get_task(task_id)["revision"]
            self._recompute_cross_coverage(coverage, reviewed_pairs)
            # 连同本轮窗口计划、已完成窗口与正在复核的窗口进度一起保存:
            # 续跑同一轮时只做剩下的窗口和提议。
            revision = self._persist_cross(
                task_id, revision, working, coverage, reviewed_pairs,
                singletons, rnd, plan, done_windows, current)

        self._recompute_cross_coverage(coverage, reviewed_pairs)
        new_groups = list(working.values())
        return revision, new_groups, related, coverage

    @staticmethod
    def _recompute_cross_coverage(coverage: Dict[str, Any], reviewed_pairs: set) -> None:
        """据**已真实复核**的对数更新 coverage;仍有未复核对即 partial(不谎称全量)。"""
        reviewed = len(reviewed_pairs)
        total = int(coverage.get("total_pairs") or 0)
        coverage["pairs_reviewed"] = reviewed
        coverage["pairs_unvisited"] = max(0, total - reviewed)
        if coverage["pairs_unvisited"] > 0:
            coverage["partial"] = True
            coverage["reason"] = coverage["reason"] or "cross_pairs_unvisited"

    def _cross_windows(self, groups: List[Dict[str, Any]],
                       singletons: Dict[str, Dict[str, Any]],
                       content: Dict[str, str], video_title: str,
                       rnd: int) -> List[List[Dict[str, Any]]]:
        """构造本轮跨批候选窗口:组代表 + 未入组单条,按轮次做环移后再切窗。

        环移让跨窗口的代表候选在不同轮里有同窗机会(有界,不追求组合全覆盖)。
        """
        candidates: List[Dict[str, Any]] = []
        for g in groups:
            rep = g.get("representative_id")
            members = list(g.get("member_ids", []))
            if rep and members:
                candidates.append({"id": rep, "content": content.get(rep, ""),
                                   MEMBERSHIP_KEY: members})
        for cid, it in singletons.items():
            candidates.append({"id": cid, "content": it["content"],
                               MEMBERSHIP_KEY: [cid]})
        if rnd and len(candidates) > 1:
            off = rnd % len(candidates)
            candidates = candidates[off:] + candidates[:off]
        return _chunk_items(candidates, video_title, PHASE_CROSS)

    def _task_checkpoint(self, task_id: str) -> Dict[str, Any]:
        """读取任务当前 checkpoint(内部字段,仅本层编排用)。"""
        return dict((self.store.get_task(task_id).get("checkpoint") or {}))

    def _persist_cross(self, task_id: str, revision: int,
                       working: Dict[str, Dict[str, Any]],
                       coverage: Dict[str, Any], reviewed_pairs: set,
                       singletons: Optional[Dict[str, Dict[str, Any]]] = None,
                       plan_round: Optional[int] = None,
                       plan: Optional[List[List[str]]] = None,
                       windows_done: Optional[set] = None,
                       window_in_progress: Optional[Dict[str, Any]] = None) -> int:
        """持久化 crossphase checkpoint(保留 phase1 段),返回新 revision。

        resume 据 rounds_done 跳过已完成轮;同一轮内据 plan/windows_done 跳过已完成窗口,
        不重复收费。working / reviewed_pairs 以列表存(JSON 友好),覆盖率据已复核对真实计量。
        """
        cp = self._task_checkpoint(task_id)
        cross: Dict[str, Any] = {
            "rounds_done": int(coverage.get("rounds_done") or 0),
            "working": list(working.values()),
            "coverage": coverage,
            "reviewed_pairs": [list(p) for p in reviewed_pairs],
        }
        if singletons is not None:
            # 当前仍是单条的 id:已在本任务里被合并进组的单条续跑时不能复活。
            cross["singletons"] = list(singletons.keys())
        if plan is not None and plan_round is not None:
            # 轮内进度:本轮窗口划分(每个窗口的候选 id)与已完成窗口序号。
            cross["plan_round"] = int(plan_round)
            cross["plan"] = [list(w) for w in plan]
            cross["windows_done"] = sorted(int(i) for i in (windows_done or ()))
            if window_in_progress is not None:
                # 正在合并复核的窗口:分组提议与下一个待复核提议的序号。
                cross["window_in_progress"] = {
                    "index": int(window_in_progress["index"]),
                    "proposals": [list(p) for p in window_in_progress.get("proposals") or []],
                    "next": int(window_in_progress.get("next") or 0),
                }
        cp["cross"] = cross
        state = self.store.update_task(task_id, revision, checkpoint=cp)
        return state["revision"]

    @staticmethod
    def _windows_from_plan(plan: List[List[str]],
                           working: Dict[str, Dict[str, Any]],
                           singletons: Dict[str, Dict[str, Any]],
                           content: Dict[str, str]) -> List[List[Dict[str, Any]]]:
        """按持久化的窗口计划(候选 id 列表)重建本轮窗口。

        成员信息取当前 working/singletons:中断前已在本轮合并掉的 id 不再是候选,直接跳过;
        合并后的新组以其代表为键仍在 working 里,照常带全部成员。
        """
        windows: List[List[Dict[str, Any]]] = []
        for ids in plan:
            window: List[Dict[str, Any]] = []
            for cid in ids:
                if cid in working:
                    members = list(working[cid].get("member_ids", []))
                    window.append({"id": cid, "content": content.get(cid, ""),
                                   MEMBERSHIP_KEY: members})
                elif cid in singletons:
                    window.append({"id": cid, "content": singletons[cid]["content"],
                                   MEMBERSHIP_KEY: [cid]})
            windows.append(window)
        return windows

    async def _verify_merge(self, task_id: str, revision: int, provider: Any,
                            content: Dict[str, str], video_title: str,
                            rep_ids: List[str], working: Dict[str, Dict[str, Any]],
                            singletons: Dict[str, Dict[str, Any]]
                            ) -> Tuple[int, Optional[Dict[str, Any]], str]:
        """对被提议并的候选代表,展开到全部原始成员做独立复核。

        返回 (revision, 合并后组|None, outcome)。outcome 取值(reviewer fix 3):
        - "merged":    复核把全部原始成员覆盖为单一等价组 => 接受合并(完整判定)。
        - "rejected":  拿到了合法复核响应但未覆盖为单一组 => 保守不折叠(**完整**判定,
                       不降级 coverage:这是模型明确说"不该合并")。
        - "oversize":  并集 >MAX_GROUP_SIZE 或超 INPUT_BYTES => 无法复核 => 降级 partial。
        - "unverified":复核未取得可用响应(降级/截断/网络耗尽) => 无法判定 => 降级 partial。
        """
        all_members: List[str] = []
        seen = set()
        for rep in rep_ids:
            src = (working.get(rep, {}).get("member_ids")
                   if rep in working else [rep])
            for m in (src or []):
                if m not in seen:
                    seen.add(m)
                    all_members.append(m)
        if len(all_members) > MAX_GROUP_SIZE:
            return revision, None, "oversize"

        verify_items = [{"id": m, "content": content.get(m, "")} for m in all_members]
        est = estimate_request_tokens(verify_items, video_title, PHASE_CROSS)
        if est.input_tokens > INPUT_BYTES:
            return revision, None, "oversize"

        revision, data = await self._grouped_call(
            task_id, revision, provider, verify_items, video_title, PHASE_CROSS)
        if data is None:
            # 复核未取得可用响应:**无法判定** => 不合并且 coverage 降级(不得谎称完整)。
            return revision, None, "unverified"

        equivalents = data.get("equivalent_groups") or []
        member_set = set(all_members)
        for eg in equivalents:
            eg_members = set(eg.get("member_ids") or [])
            rep = eg.get("representative_id")
            # 必须把全部原始成员判为单一等价组,且代表确为成员之一,才接受。
            if eg_members == member_set and rep in member_set:
                return revision, {
                    "representative_id": rep,
                    "member_ids": list(all_members),
                    "reason": eg.get("reason") or "cross-batch verified",
                    "aspect": eg.get("aspect") or "",
                    "stance": eg.get("stance") or "",
                }, "merged"
        return revision, None, "rejected"  # 模型明确不覆盖为单一组:保守不折叠(完整判定)

    @staticmethod
    def _apply_merge(working: Dict[str, Dict[str, Any]],
                     singletons: Dict[str, Dict[str, Any]],
                     rep_ids: List[str], merged: Dict[str, Any]) -> None:
        """原子替换:移除被并入的旧组/单条,挂上复核通过的新组(以其代表为键)。"""
        for rep in rep_ids:
            working.pop(rep, None)
            singletons.pop(rep, None)
        working[merged["representative_id"]] = merged
