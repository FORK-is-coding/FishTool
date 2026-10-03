"""FishTool 04 · 第三批 c：有限外部发现接入（全局共享轮询缓存 → 事件分发）。

依据：
- ``FishTool_04_话题级热点研判与选题决策_02补充执行案(1).md`` **§3.2（L32-38）**
  「EventDiscoveryRun / 发现状态与来源证据」；
- 同文件 **§17 第 4 步（L1503）**「先实现发现 claim/finish/invalidate 围栏及 D01—D07，
  再接有限外部发现和共享 watch 需求」；
- ``FishTool_04_R5执行规格_第三批c_有限外部发现与watch需求映射.md`` §1.1 / §5。

钉死口径（逐条对齐 §3.2 L34-38 原文）：
- **聚合轮询是跨事件的全局数据**：三个入口一轮只读一次，``SharedDiscoveryCache`` 全局共享后
  再按事件分发；**不许每个 HotEvent 各读一次同榜单**；
- **共享入口一次只读，也不消除迟到问题**：共享缓存结果分发给事件后，仍必须走 3b 围栏的
  rule/hash 校验（``commit_discovery`` 谓词含冻结 ``rule_version`` / ``source_policy_hash``），
  **不因“就一次”绕过**；
- 发现状态落库：全局批次记录含 **批次 ID / 开始结束 / 各来源成功失败 / 策略 hash / 候选身份 /
  保存时刻 / 保留期限 / 并发更新方式**（:class:`EventDiscoveryBatchStore`，单文件 + ``os.replace``
  原子替换 + 有界 history = 保留期限）；事件定向发现只在需要时经 3b 围栏写**小批次记录**
  （``event_discovery_runs``）；
- 四因不混：合法空 ``legitimate_empty`` / 接口失败 ``interface_failure`` / 页重复 ``page_duplicate`` /
  达上限 ``cap_reached``（见 :func:`classify_empty_reason`）；
- 限额沿用已有共享 ``RequestBudget`` 与 ``get_rate_limiter``：本模块**不新建第二个 limiter**，
  只经 :class:`SharedDiscoveryCache` 的可注入 ``budget_acquire`` 消费既有预算；真正发 HTTP 的
  可注入源（生产侧）复用 ``bilibili.rate_limiter.get_rate_limiter()`` 单例。

边界（本批明确不做）：
- 不接真·B 站接口契约（只写可注入源，测试只 mock 该源）；
- 不做 fast panel 冻结 / ``activate_fast_panel`` / 容量 12 / ``queued_capacity`` / E41 / E42（第四批）；
- 不改 3a 六表列定义、不改 3b 围栏语义、不改 02 ``watch_service.reconcile_demands``。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from sqlalchemy.orm import Session

from core.database.models_hot_event import (
    DISCOVERY_EMPTY_CAP_REACHED,
    DISCOVERY_EMPTY_INTERFACE_FAILURE,
    DISCOVERY_EMPTY_LEGITIMATE,
    DISCOVERY_EMPTY_PAGE_DUPLICATE,
    HotEvent,
)
from modules.hotspot.event_discovery_fence import (
    DiscoveryFetchResult,
    EventDiscoveryFence,
)
from modules.hotspot.event_resolver import EventDefinition, VideoEvidence, evaluate_event

logger = logging.getLogger(__name__)

__all__ = [
    "SOURCE_STATE_OK",
    "SOURCE_STATE_ERROR",
    "SOURCE_STATE_PARTIAL",
    "SOURCE_STATES",
    "DEFAULT_BATCH_PATH",
    "SourceOutcome",
    "DiscoveryBatch",
    "EventDiscoveryBatchStore",
    "SharedDiscoveryCache",
    "EventDiscoveryService",
    "classify_empty_reason",
]

# ===========================================================================
# 受控枚举（与 models_hot_event 的口径一致，禁止别处随手拼）
# ===========================================================================

#: 来源运行态：本轮该入口成功。
SOURCE_STATE_OK: str = "ok"
#: 来源运行态：本轮该入口失败（应有 ``error_code``）。
SOURCE_STATE_ERROR: str = "error"
#: 来源运行态：本轮该入口部分成功（多页源只拿到部分页）。
SOURCE_STATE_PARTIAL: str = "partial"

#: 来源运行态全集。
SOURCE_STATES: tuple = (SOURCE_STATE_OK, SOURCE_STATE_ERROR, SOURCE_STATE_PARTIAL)

#: 全局发现批次文件的缺省位置（运行态文件，与 06 快照同域，不进业务库）。
DEFAULT_BATCH_PATH = Path("data") / "hotspot" / "event_discovery_batches.json"

#: 批次文件结构版本。
BATCH_SCHEMA_VERSION: int = 1

#: 全局发现批次的并发更新方式（写进批次记录，供读端遵守）。
BATCH_CONCURRENCY: str = "single_writer_asyncio_lock_atomic_replace"


# ===========================================================================
# DTO
# ===========================================================================

@dataclass
class SourceOutcome:
    """单个聚合入口一轮的读取结果（来自可注入的外部源）。

    Attributes:
        source: 来源标识（如 ``popular`` / ``ranking`` / ``search_square``）。
        state: :data:`SOURCE_STATES` 之一。
        candidates: 该源解析出的候选列表；每条含 ``bvid`` 与归属所需证据字段。
        returned_count: 接口原始返回条数（用于区分「真空榜」与「解析异常」）。
        error_code: 失败时的稳定错误码；成功为 None。
        reason: 失败 / 部分失败的短原因码（``page_duplicate`` 表示翻页重复）。
        cap_reached: 该源是否因候选上限截断。
        started_s / finished_s: 该源本轮起止时刻（epoch 秒）。
    """

    source: str
    state: str = SOURCE_STATE_OK
    candidates: list = field(default_factory=list)
    returned_count: int = 0
    error_code: Optional[str] = None
    reason: Optional[str] = None
    cap_reached: bool = False
    started_s: int = 0
    finished_s: int = 0

    def to_snapshot(self) -> dict:
        """转成批次记录里的「来源成功/失败」摘要。

        Returns:
            dict: 精简但足以区分四态的字典。
        """
        return {
            "source": self.source,
            "state": self.state,
            "returned_count": int(self.returned_count),
            "candidate_count": len(self.candidates or []),
            "error_code": self.error_code,
            "reason": self.reason,
            "cap_reached": bool(self.cap_reached),
            "started_s": int(self.started_s),
            "finished_s": int(self.finished_s),
        }


@dataclass
class DiscoveryBatch:
    """一轮**全局**聚合轮询的批次记录（跨事件共享，不是每事件一份）。

    字段对齐 §3.2 L36：批次 ID、开始/结束、各来源成功/失败、策略 hash、候选身份、保存时刻、
    保留期限、并发更新方式。
    """

    batch_id: str
    started_s: int
    finished_s: int
    saved_s: int
    policy_hash: str
    sources: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    cap_reached: bool = False
    page_duplicate: bool = False
    retention: dict = field(default_factory=dict)
    concurrency: str = BATCH_CONCURRENCY

    # ------------------------------------------------------------------ 派生
    @property
    def candidate_count(self) -> int:
        """本批次去重后的候选数。"""
        return len(self.candidates or [])

    @property
    def failed_sources(self) -> list[str]:
        """本轮读取失败的来源名列表。"""
        return [s.get("source") for s in (self.sources or []) if s.get("state") == SOURCE_STATE_ERROR]

    def to_dict(self) -> dict:
        """序列化为批次记录 dict（落盘 / 断言用）。

        Returns:
            dict: 含全部必需字段的批次记录。
        """
        return {
            "batch_id": str(self.batch_id),
            "started_s": int(self.started_s),
            "finished_s": int(self.finished_s),
            "saved_s": int(self.saved_s),
            "policy_hash": str(self.policy_hash),
            "sources": list(self.sources or []),
            "candidates": list(self.candidates or []),
            "candidate_count": int(self.candidate_count),
            "cap_reached": bool(self.cap_reached),
            "page_duplicate": bool(self.page_duplicate),
            "retention": dict(self.retention or {}),
            "concurrency": str(self.concurrency),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DiscoveryBatch":
        """从批次记录 dict 还原对象（缺字段容错）。

        Args:
            payload: 批次记录 dict。

        Returns:
            DiscoveryBatch: 还原后的批次对象。
        """
        return cls(
            batch_id=str(payload.get("batch_id", "")),
            started_s=int(payload.get("started_s", 0) or 0),
            finished_s=int(payload.get("finished_s", 0) or 0),
            saved_s=int(payload.get("saved_s", 0) or 0),
            policy_hash=str(payload.get("policy_hash", "") or ""),
            sources=list(payload.get("sources") or []),
            candidates=list(payload.get("candidates") or []),
            cap_reached=bool(payload.get("cap_reached", False)),
            page_duplicate=bool(payload.get("page_duplicate", False)),
            retention=dict(payload.get("retention") or {}),
            concurrency=str(payload.get("concurrency", BATCH_CONCURRENCY)),
        )


# ===========================================================================
# 四因判定（纯函数：合法空 / 接口失败 / 页重复 / 达上限，四者不混）
# ===========================================================================

def classify_empty_reason(
    *,
    matched_count: int,
    candidate_count: int,
    failed_sources: Sequence[str] = (),
    cap_reached: bool = False,
    page_duplicate: bool = False,
) -> Optional[str]:
    """判定一次（事件定向）发现**无候选**时的唯一原因码。

    四因口径见 §3.2 / 3a 口径 10；四者不混：命中候选时返回 None（非空）。

    Args:
        matched_count: 本轮命中该事件的候选数（> 0 即非空）。
        candidate_count: 本批次可供匹配的去重候选数。
        failed_sources: 本轮读取失败的来源名序列。
        cap_reached: 是否因候选上限截断。
        page_duplicate: 是否命中翻页重复。

    Returns:
        Optional[str]: 四因之一；命中候选时为 None。

    """
    if matched_count > 0:
        return None
    if failed_sources and candidate_count == 0:
        # 接口失败：本批次一个可用候选都没有，且确有来源读取失败。
        return DISCOVERY_EMPTY_INTERFACE_FAILURE
    if cap_reached:
        return DISCOVERY_EMPTY_CAP_REACHED
    if page_duplicate:
        return DISCOVERY_EMPTY_PAGE_DUPLICATE
    return DISCOVERY_EMPTY_LEGITIMATE


# ===========================================================================
# 全局发现批次存储（单文件 + 原子替换 + 有界 history = 保留期限）
# ===========================================================================

class EventDiscoveryBatchStore:
    """全局发现批次记录的读写（单文件 + 原子替换 + 有界 history）。

    - ``latest``：最近一批（整体覆盖）；
    - ``history``：每批摘要 append，只保留最近 ``retention_batches`` 批（即保留期限）；
    - 写入走「同目录临时文件 + ``os.replace``」原子替换，读端永远看到完整 JSON；
    - 文件缺失 / 损坏：``load`` 返回空结构并记日志，**不抛异常**。
    """

    def __init__(self, path: Any = DEFAULT_BATCH_PATH, *, retention_batches: int = 50) -> None:
        """初始化批次存储。

        Args:
            path: 批次文件路径（可为 str / Path）。
            retention_batches: history 保留批数（至少 1），即保留期限。
        """
        self.path = Path(path)
        self.retention_batches = max(1, int(retention_batches))

    # ------------------------------------------------------------------ 读
    def load(self) -> dict:
        """读取批次文件；缺失 / 损坏时返回空结构（不抛异常）。

        Returns:
            dict: ``{"schema_version", "latest", "history"}``。
        """
        empty = {"schema_version": BATCH_SCHEMA_VERSION, "latest": None, "history": []}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError("batch file root is not a dict")
            payload.setdefault("schema_version", BATCH_SCHEMA_VERSION)
            payload.setdefault("latest", None)
            if not isinstance(payload.get("history"), list):
                payload["history"] = []
            return payload
        except FileNotFoundError:
            return empty
        except Exception as exc:  # noqa: BLE001 - 读失败不阻断发现通道
            logger.warning("事件发现批次读取失败，按空结构处理 (%s): %r", self.path, exc)
            return empty

    def latest(self) -> Optional[dict]:
        """返回最近一批批次记录；从未写入时返回 None。"""
        return self.load().get("latest")

    def history(self) -> list:
        """返回 history 列表（每批摘要，最新在尾部）。"""
        return list(self.load().get("history") or [])

    # ------------------------------------------------------------------ 写
    def save(self, batch: Mapping[str, Any]) -> dict:
        """写入一批：整体覆盖 ``latest``，并向 ``history`` 追加摘要（有界）。

        Args:
            batch: :meth:`DiscoveryBatch.to_dict` 产出的批次记录。

        Returns:
            dict: 落盘后的完整文件结构。

        Raises:
            OSError: 目录不可写 / 原子替换失败时抛出（调用方决定是否上抛）。
        """
        payload = self.load()
        record = dict(batch)
        payload["schema_version"] = BATCH_SCHEMA_VERSION
        payload["latest"] = record
        summary = {key: record.get(key) for key in (
            "batch_id", "started_s", "finished_s", "saved_s", "policy_hash",
            "sources", "candidate_count", "cap_reached", "page_duplicate", "retention", "concurrency",
        ) if key in record}
        history = list(payload.get("history") or [])
        history.append(summary)
        payload["history"] = history[-self.retention_batches:]
        self._write_atomic(payload)
        return payload

    def _write_atomic(self, payload: Mapping[str, Any]) -> None:
        """原子写出批次文件（同目录临时文件 + ``os.replace``）。

        Args:
            payload: 待写入结构。

        Raises:
            OSError: 写入或替换失败时抛出。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, temp_path = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except Exception:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            finally:
                raise


# ===========================================================================
# 全局共享轮询缓存（一次只读，结果分发给事件；仍走围栏 rule/hash 校验）
# ===========================================================================

#: 可注入聚合源签名：``now_s -> Sequence[SourceOutcome]``（可为 async）。
AggregateFetcher = Callable[[int], Any]
#: 预算授予回调签名：``(kind, now_mono) -> bool``（沿用既有共享 RequestBudget）。
BudgetAcquire = Callable[[str, float], bool]

#: 预算类别（沿用既有共享 RequestBudget 的 ``discovery`` 额度）。
BUDGET_KIND_DISCOVERY: str = "discovery"
#: 无法识别来源时的兜底来源名。
_UNKNOWN_SOURCE: str = "aggregate"


class SharedDiscoveryCache:
    """跨事件的**全局**聚合轮询缓存：一轮只读一次，再分发给事件。

    关键约束：
    - 同一轮（``now_s`` 与上次保存时刻之差 < ``ttl_s``）内，外部源**只被读一次**；
      两个事件同轮各自要数据，第二个直接命中缓存，**不各读一次榜单**；
    - 刷新在 ``asyncio.Lock`` 内串行，并发事件不会重复触发外部读取；
    - 缓存**只负责“少读一次”**，并不消除迟到问题：分发给事件的结果仍要经
      3b 围栏 ``commit_discovery`` 的 ``rule_version`` / ``source_policy_hash`` 谓词校验；
    - 预算不可授予时**不读榜单**，落一条 ``quota_exceeded`` 失败批次。
    """

    def __init__(
        self,
        fetcher: AggregateFetcher,
        *,
        batch_store: EventDiscoveryBatchStore,
        ttl_s: int = 600,
        max_candidates: int = 100,
        policy_plan: Optional[Sequence[str]] = None,
        clock: Optional[Callable[[], int]] = None,
        mono: Optional[Callable[[], float]] = None,
        budget_acquire: Optional[BudgetAcquire] = None,
    ) -> None:
        """初始化共享缓存。

        Args:
            fetcher: 可注入聚合源（测试只 mock 它；生产侧复用既有 limiter）。
            batch_store: 批次存储（落全局发现状态）。
            ttl_s: 缓存存活秒数（<=0 表示每轮都刷新）。
            max_candidates: 本批次候选上限（达上限即计入 ``cap_reached``）。
            policy_plan: 本轮来源计划（参与 ``policy_hash``）；缺省从实际来源推导。
            clock: 秒级时钟（返回 UTC 秒），缺省 ``time.time`` 取整。
            mono: 单调时钟（只喂预算），缺省 ``time.monotonic``。
            budget_acquire: 既有共享 ``RequestBudget`` 的授予回调；缺省不设预算门。
        """
        self._fetcher = fetcher
        self._batch_store = batch_store
        self._ttl_s = int(ttl_s)
        self._max_candidates = max(1, int(max_candidates))
        self._policy_plan = list(policy_plan) if policy_plan is not None else None
        self._clock: Callable[[], int] = clock or (lambda: int(time.time()))
        self._mono: Callable[[], float] = mono or time.monotonic
        self._budget_acquire = budget_acquire
        # 串行刷新：并发事件只触发一次外部读取（Python 3.12 可在无运行 loop 时构造）。
        self._lock = asyncio.Lock()

    def _now(self, now_s: Optional[int]) -> int:
        """归一当前时刻为 epoch 秒（显式优先，其次注入时钟）。"""
        if now_s is None:
            return int(self._clock())
        if type(now_s) is bool or not isinstance(now_s, int):
            raise ValueError("now_s_must_be_int")
        return int(now_s)

    async def snapshot(self, now_s: Optional[int] = None, *, force: bool = False) -> DiscoveryBatch:
        """取本轮全局共享快照：新鲜则命中缓存，过期才刷新（并发只刷一次）。

        Args:
            now_s: 当前时刻（UTC 秒）；缺省取注入时钟。
            force: 强制刷新（忽略 TTL）。

        Returns:
            DiscoveryBatch: 本轮（共享的）批次记录。
        """
        now = self._now(now_s)
        if not force:
            cached = self._fresh_batch(now)
            if cached is not None:
                return cached
        async with self._lock:
            if not force:
                cached = self._fresh_batch(now)
                if cached is not None:
                    return cached
            return await self._refresh(now)

    def _fresh_batch(self, now_s: int) -> Optional[DiscoveryBatch]:
        """返回未过期的缓存批次；无 / 过期返回 None（不触发外部读取）。"""
        latest = self._batch_store.latest()
        if not isinstance(latest, dict):
            return None
        saved_s = int(latest.get("saved_s", 0) or 0)
        age = now_s - saved_s
        if 0 <= age < self._ttl_s:
            return DiscoveryBatch.from_dict(latest)
        return None

    async def _refresh(self, now_s: int) -> DiscoveryBatch:
        """真正刷新一轮：读取可注入源一次 → 去重有界 → 落批次记录。

        Args:
            now_s: 本轮采样时刻（UTC 秒）。

        Returns:
            DiscoveryBatch: 写库后的批次记录。
        """
        started = int(now_s)
        outcomes: list[SourceOutcome] = []
        if self._budget_acquire is not None and not bool(
            self._budget_acquire(BUDGET_KIND_DISCOVERY, self._mono())
        ):
            # 不绕过预算：本轮不读榜单，落一条 quota_exceeded 失败批次。
            outcomes = [
                SourceOutcome(
                    source=_UNKNOWN_SOURCE,
                    state=SOURCE_STATE_ERROR,
                    error_code="quota_exceeded",
                    started_s=started,
                    finished_s=started,
                )
            ]
        else:
            outcomes = await self._read_sources(started)

        batch = self._build_batch(outcomes, started)
        try:
            self._batch_store.save(batch.to_dict())
        except OSError as exc:  # noqa: BLE001 - 落盘失败不阻断本轮分发
            logger.warning("事件发现批次落盘失败（不影响本轮分发）: %r", exc)
        return batch

    async def _read_sources(self, started: int) -> list[SourceOutcome]:
        """调用可注入源一次，并把返回归一为 :class:`SourceOutcome` 列表。

        Args:
            started: 本轮开始时刻（UTC 秒）。

        Returns:
            list[SourceOutcome]: 归一后的来源结果；异常时返回一条失败来源。
        """
        try:
            raw = self._fetcher(started)
            if inspect.isawaitable(raw):
                raw = await raw
            return [self._normalize_outcome(item, started) for item in (raw or [])]
        except Exception as exc:  # noqa: BLE001 - 外部源失败：记失败来源，不炸
            logger.warning("聚合源读取失败: %r", exc)
            return [
                SourceOutcome(
                    source=_UNKNOWN_SOURCE,
                    state=SOURCE_STATE_ERROR,
                    error_code=f"fetch_error:{type(exc).__name__}",
                    started_s=started,
                    finished_s=started,
                )
            ]

    @staticmethod
    def _normalize_outcome(item: Any, started: int) -> SourceOutcome:
        """把一条外部源返回归一为 :class:`SourceOutcome`。

        Args:
            item: 外部源返回项（``SourceOutcome`` 或 mapping）。
            started: 本轮开始时刻（兜底 start/finish）。

        Returns:
            SourceOutcome: 归一结果。
        """
        if isinstance(item, SourceOutcome):
            if not item.started_s:
                item.started_s = started
            if not item.finished_s:
                item.finished_s = started
            return item
        if isinstance(item, Mapping):
            state = str(item.get("state", SOURCE_STATE_OK))
            return SourceOutcome(
                source=str(item.get("source", _UNKNOWN_SOURCE)),
                state=state if state in SOURCE_STATES else SOURCE_STATE_OK,
                candidates=list(item.get("candidates") or []),
                returned_count=int(item.get("returned_count", 0) or 0),
                error_code=item.get("error_code"),
                reason=item.get("reason"),
                cap_reached=bool(item.get("cap_reached", False)),
                started_s=int(item.get("started_s", started) or started),
                finished_s=int(item.get("finished_s", started) or started),
            )
        return SourceOutcome(source=_UNKNOWN_SOURCE, started_s=started, finished_s=started)

    def _build_batch(self, outcomes: Sequence[SourceOutcome], started: int) -> DiscoveryBatch:
        """把来源结果去重、有界化，组装成批次记录。

        Args:
            outcomes: 归一后的来源结果。
            started: 本轮开始时刻（UTC 秒）。

        Returns:
            DiscoveryBatch: 批次记录（含四态标志与保留期限）。
        """
        merged: dict[str, dict] = {}
        total_raw = 0
        for outcome in outcomes:
            for candidate in outcome.candidates or []:
                if not isinstance(candidate, Mapping):
                    continue
                total_raw += 1
                bvid = str(candidate.get("bvid") or "").strip()
                if not bvid:
                    continue
                existing = merged.get(bvid)
                if existing is None:
                    merged[bvid] = dict(candidate, bvid=bvid)
                else:
                    # 同 bvid 多来源：来源取并集，绝不按播放量挑边。
                    sources = list(existing.get("sources") or [])
                    for src in candidate.get("sources") or [outcome.source]:
                        if src not in sources:
                            sources.append(src)
                    existing["sources"] = sources
        unique = list(merged.values())
        candidates = unique[: self._max_candidates]
        cap_reached = (
            any(o.cap_reached for o in outcomes)
            or len(unique) > self._max_candidates
            or total_raw > self._max_candidates
        )
        page_duplicate = any(o.reason == "page_duplicate" for o in outcomes)
        finished = int(self._clock())
        plan = self._policy_plan if self._policy_plan is not None else sorted(
            {str(o.source) for o in outcomes}
        )
        return DiscoveryBatch(
            batch_id=f"batch_{hashlib.sha1(f'{started}:{finished}:{len(outcomes)}'.encode()).hexdigest()[:16]}",
            started_s=int(started),
            finished_s=finished,
            saved_s=finished,
            policy_hash=self._policy_hash(plan),
            sources=[o.to_snapshot() for o in outcomes],
            candidates=candidates,
            cap_reached=bool(cap_reached),
            page_duplicate=bool(page_duplicate),
            retention={"max_batches": self._batch_store.retention_batches, "mode": "bounded_history"},
            concurrency=BATCH_CONCURRENCY,
        )

    def _policy_hash(self, plan: Sequence[str]) -> str:
        """算本轮发现策略 hash（来源计划 + 候选上限的稳定指纹）。

        Args:
            plan: 本轮来源计划。

        Returns:
            str: 64 位十六进制策略 hash。
        """
        payload = json.dumps(
            {"sources": sorted(str(s) for s in plan), "max_candidates": self._max_candidates},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ===========================================================================
# 事件定向发现服务：把 3b 围栏接到共享缓存（仍在围栏内做 rule/hash 校验）
# ===========================================================================

#: 事件规则提供者：``(session, event_id) -> EventDefinition | None``。
RuleProvider = Callable[[Session, str], Optional[EventDefinition]]
#: 会话工厂签名：无参调用返回一个 ``Session``。
SessionFactory = Callable[[], Session]


class EventDiscoveryService:
    """04 侧有限外部发现的编排门面：共享缓存 + 3b 围栏（claim/finish/commit）。

    用法（生产由上层装配；测试把 ``fetcher`` 换成计数桩）::

        cache = SharedDiscoveryCache(fetcher, batch_store=store)
        service = EventDiscoveryService(fence=fence, shared_cache=cache, session_factory=factory)
        await service.discover_event("ev1", trigger="scheduled",
                                     rule_version=1, source_policy_hash="h1")
    """

    def __init__(
        self,
        *,
        fence: EventDiscoveryFence,
        shared_cache: SharedDiscoveryCache,
        session_factory: SessionFactory,
        clock: Optional[Callable[[], int]] = None,
        rule_provider: Optional[RuleProvider] = None,
        strict_auto: bool = False,
        max_candidates: int = 100,
    ) -> None:
        """初始化事件发现服务。

        Args:
            fence: 3b 发现围栏（共用同一条领取入口，未被打桩）。
            shared_cache: 全局共享轮询缓存（一轮只读一次）。
            session_factory: 会话工厂（读事件规则用）。
            clock: 秒级时钟（返回 UTC 秒），缺省 ``time.time`` 取整。
            rule_provider: 事件规则提供者；缺省从 ``HotEvent.source_policy`` 读取。
            strict_auto: 是否启用「已审核规则的 strict-auto」（规则 5）。
            max_candidates: 单个 run 落库候选上限。
        """
        self._fence = fence
        self._cache = shared_cache
        self._session_factory = session_factory
        self._clock: Callable[[], int] = clock or (lambda: int(time.time()))
        self._rule_provider = rule_provider
        self._strict_auto = bool(strict_auto)
        self._max_candidates = max(1, int(max_candidates))

    # ------------------------------------------------------------ 对外入口
    async def discover_event(
        self,
        event_id: str,
        *,
        trigger: str,
        rule_version: int,
        source_policy_hash: str,
        now_s: Optional[int] = None,
    ):
        """对单个事件发起一次发现（走 3b 围栏的 claim + 后台执行 + commit）。

        Args:
            event_id: 事件 ID。
            trigger: ``scheduled`` / ``manual``。
            rule_version: 冻结规则版本。
            source_policy_hash: 冻结策略 hash。
            now_s: 当前时刻；缺省取注入时钟。

        Returns:
            ``DiscoveryStartResult``（见 3b 围栏）。
        """
        return await self._fence.start_discovery(
            event_id,
            trigger=trigger,
            rule_version=rule_version,
            source_policy_hash=source_policy_hash,
            now_s=now_s,
        )

    def make_fetch_fn(self):
        """返回可注入 3b 围栏的 ``fetch_fn``（读共享缓存 → 按事件分发）。

        Returns:
            Callable[[str, int, str], Awaitable[DiscoveryFetchResult]]: 围栏用外部源。
        """
        async def fetch_fn(event_id: str, rule_version: int, source_policy_hash: str) -> DiscoveryFetchResult:
            """围栏回调：读共享缓存（同轮只读一次）并按事件归属分发。"""
            return await self._fetch_for_event(event_id, rule_version, source_policy_hash)

        return fetch_fn

    # ------------------------------------------------------------ 分发逻辑
    async def _fetch_for_event(
        self, event_id: str, rule_version: int, source_policy_hash: str
    ) -> DiscoveryFetchResult:
        """读共享缓存并按事件规则分发，产出一轮围栏结果。

        Args:
            event_id: 事件 ID。
            rule_version: 冻结规则版本。
            source_policy_hash: 冻结策略 hash。

        Returns:
            DiscoveryFetchResult: 供 3b 围栏 commit（仍在围栏内校验 rule/hash）。
        """
        now_s = int(self._clock())
        batch = await self._cache.snapshot(now_s)
        event_def = self._event_definition(event_id)
        member_decisions: list[dict] = []
        matched_candidates: list[dict] = []
        excluded = 0
        ignored = 0
        if event_def is not None:
            for candidate in batch.candidates:
                evidence = self._to_evidence(candidate)
                result = evaluate_event(
                    evidence,
                    event_def,
                    rule_version=int(rule_version),
                    strict_auto=self._strict_auto,
                    decision_at_s=now_s,
                )
                if result.decision in ("accepted", "proposed"):
                    matched_candidates.append(candidate)
                    member_decisions.append(
                        {
                            "bvid": evidence.bvid,
                            "status": result.decision,
                            "first_seen_s": now_s,
                            "rule_version": int(rule_version),
                            "decision_source": result.decision_source,
                            "evidence": {
                                "match_spans": result.match_spans,
                                "reason_codes": result.reason_codes,
                                "sources": result.evidence_sources,
                            },
                            "raw_tid": candidate.get("raw_tid"),
                            "published_epoch_s": candidate.get("published_epoch_s"),
                            "owner_mid": candidate.get("owner_mid"),
                        }
                    )
                elif result.decision == "rejected":
                    excluded += 1
                else:
                    ignored += 1

        empty_reason = classify_empty_reason(
            matched_count=len(member_decisions),
            candidate_count=batch.candidate_count,
            failed_sources=batch.failed_sources,
            cap_reached=batch.cap_reached,
            page_duplicate=batch.page_duplicate,
        )
        final_status, error_code = self._final_status(empty_reason, batch)
        counters: dict = {
            "matched": len(member_decisions),
            "candidate_count": batch.candidate_count,
            "excluded": excluded,
            "ignored": ignored,
            "cap_reached": batch.cap_reached,
            "page_duplicate": batch.page_duplicate,
            "source_failures": len(batch.failed_sources),
        }
        if empty_reason is not None:
            counters["empty_reason"] = empty_reason
        return DiscoveryFetchResult(
            candidates=matched_candidates[: self._max_candidates],
            counters=counters,
            new_bvids=[d["bvid"] for d in member_decisions],
            member_decisions=member_decisions,
            source_attempts=list(batch.sources),
            final_status=final_status,
            error_code=error_code,
        )

    @staticmethod
    def _final_status(empty_reason: Optional[str], batch: DiscoveryBatch) -> tuple[str, Optional[str]]:
        """由空因 / 来源状态推出 run 终态与错误码。

        Args:
            empty_reason: 四因之一（或 None）。
            batch: 本轮全局批次。

        Returns:
            tuple[str, Optional[str]]: ``(final_status, error_code)``。
        """
        if empty_reason == DISCOVERY_EMPTY_INTERFACE_FAILURE:
            failed = batch.failed_sources
            return "failed", "source_failure" if failed else "unknown_failure"
        if batch.failed_sources or any(s.get("state") == SOURCE_STATE_PARTIAL for s in batch.sources):
            return "partial", None
        return "completed", None

    def _event_definition(self, event_id: str) -> Optional[EventDefinition]:
        """读取事件定义（缺省从 ``HotEvent.source_policy`` 构造）。

        Args:
            event_id: 事件 ID。

        Returns:
            EventDefinition | None: 事件规则定义；事件不存在返回 None。
        """
        session = self._session_factory()
        try:
            return self._event_definition_in(session, event_id)
        finally:
            session.close()

    def _event_definition_in(self, session: Session, event_id: str) -> Optional[EventDefinition]:
        """在给定会话内构造事件定义（可注入 ``rule_provider`` 覆盖）。

        Args:
            session: 会话。
            event_id: 事件 ID。

        Returns:
            EventDefinition | None: 事件规则定义。
        """
        if self._rule_provider is not None:
            return self._rule_provider(session, event_id)
        event = session.get(HotEvent, event_id)
        if event is None:
            return None
        policy = event.source_policy if isinstance(event.source_policy, dict) else {}
        return EventDefinition(
            canonical_name=str(event.name or event_id),
            entity_scope=tuple(policy.get("entity_scope") or ()),
            event_kind=policy.get("event_kind", "other"),
            include_rules=policy.get("include_rules") or {},
            exclude_rules=policy.get("exclude_rules") or {},
            source_scope=tuple(policy.get("source_scope") or ()),
            event_id=event_id,
        )

    @staticmethod
    def _to_evidence(candidate: Mapping[str, Any]) -> VideoEvidence:
        """把一条候选 dict 转成归属用 :class:`VideoEvidence`。

        Args:
            candidate: 候选 dict。

        Returns:
            VideoEvidence: 归属证据。
        """
        return VideoEvidence(
            bvid=str(candidate.get("bvid") or ""),
            title=str(candidate.get("title") or ""),
            tags=tuple(str(t) for t in (candidate.get("tags") or ())),
            summary=str(candidate.get("summary") or ""),
            comment_terms=tuple(str(t) for t in (candidate.get("comment_terms") or ())),
            sources=tuple(str(s) for s in (candidate.get("sources") or ())),
            published_epoch_s=candidate.get("published_epoch_s"),
            owner_mid=candidate.get("owner_mid"),
            raw_tid=candidate.get("raw_tid"),
            discovered_at_s=candidate.get("discovered_at_s"),
        )
