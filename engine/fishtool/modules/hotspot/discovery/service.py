"""06 采集广度 · 调度、共享轮询缓存、去重落库与快照冻结（规格 §4 / §7 / §8）。

关键约束
--------
- **三源共享轮询缓存**（硬要求）：``DiscoveryPollCache`` 按入口 + 页缓存一轮结果，
  消费者（02 / 04）读服务，**不得**每个事件各自重发 624 次；TTL = 该入口轮询间隔。
- **去重口径**（硬约束 1）：候选身份按 ``bvid`` 去重、发现来源全部保留；展示值按
  预先固定的来源优先级取值，冲突打标记；绝不按 max 播放量挑边。
- **失败状态落点**（硬约束 2）：每轮写一份**全局发现快照**（``DiscoverySnapshotStore``），
  支持区分「真空榜 / 请求失败 / 仅第一页成功 / 显示的其实是上轮缓存」与回放幂等。
- **heat_score** 只叫「平台接口返回热搜分数」，不进播放增量计算（与 01 口径一致）。
- 配额：``search/square`` / ``popular`` 走 ``no_cookie`` + ``discovery``；
  ``ranking/v2?rid=0`` 走 ``ranking``；域归属运行期从 ``config/budget.yaml`` 读取。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.data_quality import utc_now_epoch_s
from core.request_budget import RequestBudgetExceeded, before_http_attempt

from . import sources
from .contracts import (
    BroadKeyword,
    SOURCE_POPULAR,
    SOURCE_RANKING_ALL,
    SOURCE_SEARCH_SQUARE,
    STATE_ERROR,
    STATE_OK,
    STATE_PARTIAL,
    merge_video_candidates,
)
from .snapshot import DiscoverySnapshotStore
from .store import KeywordSignalStore, VideoSignalStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoverRunConfig:
    """发现通道运行参数（频率来自规格 §2.2；**不是配额数字**）。

    Attributes:
        search_limit: ``search/square`` 返回条数。
        popular_pages: ``popular`` 页数（**2 页**，不是 3 页——3 页会超 discovery 额度）。
        popular_ps: ``popular`` 每页条数（上限 20）。
        ranking_rid: 榜单分区，``0`` 为全站榜。
        ranking_day: 榜单周期。
        search_interval_s: ``search/square`` 轮询间隔（10 分钟）。
        popular_interval_s: ``popular`` 轮询间隔（10 分钟）。
        ranking_interval_s: ``ranking`` 轮询间隔（30 分钟）。
    """

    search_limit: int = 10
    popular_pages: int = 2
    popular_ps: int = 20
    ranking_rid: int = 0
    ranking_day: int = 7
    search_interval_s: int = 600
    popular_interval_s: int = 600
    ranking_interval_s: int = 1800

    def to_params(self) -> Dict[str, Any]:
        """返回参与快照参数 hash 的稳定参数集。

        Returns:
            Dict[str, Any]: 影响抓取结果的参数（不含频率）。
        """
        return {
            "search_limit": self.search_limit,
            "popular_pages": self.popular_pages,
            "popular_ps": self.popular_ps,
            "ranking_rid": self.ranking_rid,
            "ranking_day": self.ranking_day,
        }


@dataclass
class SourceRun:
    """单个入口一轮运行的结果（进快照）。

    Attributes:
        source: 来源标识。
        state: ``ok`` / ``partial`` / ``error``。
        items: 解析出的 DTO 列表。
        returned_count: 接口原始返回条数（用于识别真空榜）。
        error_code: 非零业务码；无则 None。
        reason: 失败 / 部分失败原因短码。
        started_epoch_s / finished_epoch_s: 本轮该源的起止时刻。
        poll_slot_epoch_s: 数据实际采样时刻（缓存命中时是缓存采样时刻）。
        from_cache: 是否命中轮询缓存（True 表示本轮未发 HTTP）。
        cache_age_s: 缓存数据相对当前轮的陈旧度（秒）。
        pages: 多页源的逐页明细。
        others_count: ranking ``others`` 原始条数。
    """

    source: str
    state: str
    items: List[Any] = field(default_factory=list)
    returned_count: int = 0
    error_code: Optional[int] = None
    reason: Optional[str] = None
    started_epoch_s: int = 0
    finished_epoch_s: int = 0
    poll_slot_epoch_s: int = 0
    from_cache: bool = False
    cache_age_s: int = 0
    pages: List[Dict[str, Any]] = field(default_factory=list)
    others_count: int = 0

    def to_snapshot(self) -> Dict[str, Any]:
        """转成快照里的来源摘要（含四态判定所需字段）。

        Returns:
            Dict[str, Any]: 精简但足以区分四态的字典。
        """
        return {
            "state": self.state,
            "returned_count": self.returned_count,
            "item_count": len(self.items),
            "error_code": self.error_code,
            "reason": self.reason,
            "started_epoch_s": self.started_epoch_s,
            "finished_epoch_s": self.finished_epoch_s,
            "poll_slot_epoch_s": self.poll_slot_epoch_s,
            "from_cache": self.from_cache,
            "cache_age_s": self.cache_age_s,
            "others_count": self.others_count,
            "pages": self.pages,
        }


@dataclass
class _CacheEntry:
    """轮询缓存条目。"""

    envelope: Dict[str, Any]
    fetched_epoch_s: int
    expires_at: float


class DiscoveryPollCache:
    """三源共享轮询缓存（进程内内存；TTL = 入口轮询间隔）。

    硬要求：三源共享轮询结果，再分发给事件；**不许每个事件各发一遍**。
    进程重启即失效——发现通道可容忍，快照里的 ``from_cache`` 已如实标注当前展示来源。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """初始化缓存。

        Args:
            clock: 单调时钟（秒），便于测试注入。
        """
        self._clock = clock
        self._store: Dict[Tuple[Any, ...], _CacheEntry] = {}

    def get(self, key: Tuple[Any, ...]) -> Optional[_CacheEntry]:
        """读取未过期缓存。

        Args:
            key: 缓存键（来源 + 可选页号）。

        Returns:
            Optional[_CacheEntry]: 命中返回条目，未命中 / 过期返回 None。
        """
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            self._store.pop(key, None)
            return None
        return entry

    def put(self, key: Tuple[Any, ...], envelope: Dict[str, Any], *, fetched_epoch_s: int, ttl_s: int) -> None:
        """写入缓存条目。

        Args:
            key: 缓存键。
            envelope: 统一发现 envelope。
            fetched_epoch_s: 采样时刻（UTC 秒）。
            ttl_s: 存活秒数（<=0 不缓存）。

        Returns:
            无。
        """
        if ttl_s <= 0:
            return
        self._store[key] = _CacheEntry(
            envelope=envelope,
            fetched_epoch_s=int(fetched_epoch_s),
            expires_at=self._clock() + int(ttl_s),
        )

    def clear(self) -> None:
        """清空缓存（测试 / 手工刷新用）。"""
        self._store.clear()


class DiscoveryService:
    """聚合入口发现通道的服务门面（调度 + 缓存 + 去重 + 落库 + 快照）。"""

    def __init__(
        self,
        api: Any = None,
        *,
        cache: Optional[DiscoveryPollCache] = None,
        snapshot_store: Optional[DiscoverySnapshotStore] = None,
        keyword_store: Optional[KeywordSignalStore] = None,
        video_store: Optional[VideoSignalStore] = None,
        config: Optional[DiscoverRunConfig] = None,
        clock: Optional[Callable[[], int]] = None,
        budget_hook: Optional[Callable[..., None]] = None,
    ) -> None:
        """初始化服务。

        Args:
            api: B 站 API 客户端（含 ``get`` / ``get_ranking``）。
            cache: 共享轮询缓存；缺省新建。
            snapshot_store: 全局发现快照存储；缺省 ``data/hotspot/discovery_snapshot.json``。
            keyword_store: 关键词写入器；缺省新建。
            video_store: 视频信号写入器；缺省新建。
            config: 运行参数；缺省 ``DiscoverRunConfig()``。
            clock: 秒级时钟（返回 UTC 秒），便于测试注入。
            budget_hook: 发送前记账钩子；缺省 ``core.request_budget.before_http_attempt``。
        """
        self.api = api
        self.cache = cache or DiscoveryPollCache()
        self.snapshot_store = snapshot_store or DiscoverySnapshotStore()
        self.keyword_store = keyword_store or KeywordSignalStore()
        self.video_store = video_store or VideoSignalStore()
        self.config = config or DiscoverRunConfig()
        self._clock = clock or utc_now_epoch_s
        self._budget_hook = budget_hook or before_http_attempt
        self._last_keywords: List[BroadKeyword] = []
        self._last_videos: List[Dict[str, Any]] = []

    # ------------------------------------------------------------- 主流程
    async def poll_once(
        self, *, captured_epoch_s: Optional[int] = None, use_cache: bool = True
    ) -> Dict[str, Any]:
        """执行一轮发现：三源读取 -> 解析 -> 去重 -> 落库 -> 冻结快照。

        Args:
            captured_epoch_s: 本轮采样时刻（UTC 秒）；缺省取当前时刻。
            use_cache: 是否允许命中共享轮询缓存。

        Returns:
            Dict[str, Any]: 本轮全局发现快照；回放命中的既有快照会带 ``replayed=True``。
        """
        captured = int(captured_epoch_s if captured_epoch_s is not None else self._clock())
        params = self.config.to_params()
        params_hash = self._params_hash(params)
        snapshot_id = self._snapshot_id(params_hash, captured)

        # 回放幂等：同一 (参数, 采样时刻) 已记录 -> 直接短路，不重复写库。
        if self.snapshot_store.has(snapshot_id):
            existing = dict(self.snapshot_store.find(snapshot_id) or {})
            existing["replayed"] = True
            return existing

        runs = [
            await self._run_keywords(captured, use_cache),
            await self._run_popular(captured, use_cache),
            await self._run_ranking(captured, use_cache),
        ]

        keywords: List[BroadKeyword] = []
        video_items: List[Any] = []
        for run in runs:
            if run.source == SOURCE_SEARCH_SQUARE:
                keywords.extend(run.items)
            else:
                video_items.extend(run.items)
        merged = merge_video_candidates(video_items)

        self._last_keywords = keywords
        self._last_videos = merged

        snapshot: Dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "captured_epoch_s": captured,
            "params_hash": params_hash,
            "params": params,
            "served_from_cache": all(run.from_cache for run in runs),
            "sources": {run.source: run.to_snapshot() for run in runs},
            "keyword_count": len(keywords),
            "video_count": len(merged),
            "others_count": sum(run.others_count for run in runs),
            "written_keywords": self.keyword_store.save_observations(keywords, snapshot_id=snapshot_id),
            "written_videos": self.video_store.save_candidates(merged),
        }
        self.snapshot_store.save(snapshot)
        return snapshot

    # ------------------------------------------------------------- 消费接口
    def iter_video_candidates(self) -> List[Dict[str, Any]]:
        """返回最近一轮合并后的视频候选（交 02 / 04 消费）。

        Returns:
            List[Dict[str, Any]]: 每个 bvid 一条，来源全留的合并结果。
        """
        return list(self._last_videos)

    def list_keyword_candidates(self) -> List[BroadKeyword]:
        """返回最近一轮热搜关键词候选（交 04 消费，作 HotEvent 草稿线索）。

        Returns:
            List[BroadKeyword]: 关键词候选列表。
        """
        return list(self._last_keywords)

    def latest_sources_state(self) -> Dict[str, Any]:
        """返回最近一轮各来源运行态（用于区分「新抓 / 上轮缓存」）。

        Returns:
            Dict[str, Any]: 各来源摘要；无快照时为空 dict。
        """
        return self.snapshot_store.latest_sources_state()

    # ------------------------------------------------------------- 单源调度
    async def _obtain(
        self,
        cache_key: Tuple[Any, ...],
        interval_s: int,
        use_cache: bool,
        captured: int,
        fetch_callable: Callable[[], Any],
    ) -> Tuple[Optional[Dict[str, Any]], int, bool, int]:
        """获取某个入口的 envelope（优先缓存，未命中才发 HTTP）。

        Args:
            cache_key: 缓存键。
            interval_s: 缓存 TTL（= 入口轮询间隔）。
            use_cache: 是否允许命中缓存。
            captured: 本轮采样时刻。
            fetch_callable: 无参 async 协程工厂，返回 envelope。

        Returns:
            Tuple[envelope|None, poll_slot, from_cache, cache_age_s]。
            ``envelope`` 为 None 表示配额耗尽（本轮该源记 error）。
        """
        if use_cache:
            entry = self.cache.get(cache_key)
            if entry is not None:
                age = max(0, captured - int(entry.fetched_epoch_s))
                return entry.envelope, int(entry.fetched_epoch_s), True, age
        try:
            envelope = await fetch_callable()
        except RequestBudgetExceeded as exc:
            # 配额耗尽不是业务失败，但也不能伪装成空榜：记 error，不写任何信号。
            logger.warning("发现入口配额耗尽/超时，本轮记 error (key=%s): %r", cache_key, exc)
            return None, captured, False, 0
        self.cache.put(cache_key, envelope, fetched_epoch_s=captured, ttl_s=interval_s)
        return envelope, captured, False, 0

    async def _run_keywords(self, captured: int, use_cache: bool) -> SourceRun:
        """运行 ``search/square`` 单个入口。"""
        started = int(self._clock())
        envelope, poll_slot, from_cache, age = await self._obtain(
            (SOURCE_SEARCH_SQUARE,),
            self.config.search_interval_s,
            use_cache,
            captured,
            lambda: sources.fetch_hot_keywords(
                self.api, limit=self.config.search_limit, budget_hook=self._budget_hook
            ),
        )
        finished = int(self._clock())
        if envelope is None:
            return SourceRun(
                SOURCE_SEARCH_SQUARE, STATE_ERROR, reason="quota_exceeded",
                started_epoch_s=started, finished_epoch_s=finished, poll_slot_epoch_s=captured,
            )
        outcome = sources.parse_hot_keywords(envelope, captured_epoch_s=poll_slot)
        return SourceRun(
            source=SOURCE_SEARCH_SQUARE,
            state=outcome.state,
            items=list(outcome.items),
            returned_count=outcome.returned_count,
            error_code=outcome.error_code,
            reason=outcome.reason,
            started_epoch_s=started,
            finished_epoch_s=finished,
            poll_slot_epoch_s=poll_slot,
            from_cache=from_cache,
            cache_age_s=age,
        )

    async def _run_popular(self, captured: int, use_cache: bool) -> SourceRun:
        """运行 ``popular`` 多页入口（页失败保留已成功页并标 partial）。"""
        started = int(self._clock())
        items: List[Any] = []
        page_details: List[Dict[str, Any]] = []
        states: List[str] = []
        returned_total = 0
        error_code: Optional[int] = None
        reason: Optional[str] = None
        any_cache = False
        max_age = 0

        for page in range(1, max(1, int(self.config.popular_pages)) + 1):
            envelope, poll_slot, from_cache, age = await self._obtain(
                (SOURCE_POPULAR, page),
                self.config.popular_interval_s,
                use_cache,
                captured,
                lambda page=page: sources.fetch_popular_page(
                    self.api,
                    page=page,
                    ps=self.config.popular_ps,
                    budget_hook=self._budget_hook,
                ),
            )
            if envelope is None:
                states.append(STATE_ERROR)
                page_details.append({"page": page, "state": STATE_ERROR, "reason": "quota_exceeded"})
                continue
            outcome = sources.parse_popular(envelope, captured_epoch_s=poll_slot)
            states.append(outcome.state)
            returned_total += outcome.returned_count
            if outcome.state == STATE_OK:
                items.extend(outcome.items)
            if outcome.error_code is not None:
                error_code = outcome.error_code
            if outcome.reason:
                reason = outcome.reason
            any_cache = any_cache or from_cache
            max_age = max(max_age, age)
            page_details.append(
                {
                    "page": page,
                    "state": outcome.state,
                    "returned_count": outcome.returned_count,
                    "error_code": outcome.error_code,
                    "reason": outcome.reason,
                    "from_cache": from_cache,
                }
            )

        finished = int(self._clock())
        if states and all(state == STATE_ERROR for state in states):
            state = STATE_ERROR
        elif any(state == STATE_ERROR for state in states):
            state = STATE_PARTIAL
            reason = reason or "some_pages_failed"
        else:
            state = STATE_OK
        return SourceRun(
            source=SOURCE_POPULAR,
            state=state,
            items=items,
            returned_count=returned_total,
            error_code=error_code,
            reason=reason,
            started_epoch_s=started,
            finished_epoch_s=finished,
            poll_slot_epoch_s=captured,
            from_cache=any_cache,
            cache_age_s=max_age,
            pages=page_details,
        )

    async def _run_ranking(self, captured: int, use_cache: bool) -> SourceRun:
        """运行 ``ranking/v2?rid=0`` 全站榜入口。"""
        started = int(self._clock())
        envelope, poll_slot, from_cache, age = await self._obtain(
            (SOURCE_RANKING_ALL,),
            self.config.ranking_interval_s,
            use_cache,
            captured,
            lambda: sources.fetch_ranking(
                self.api,
                rid=self.config.ranking_rid,
                day=self.config.ranking_day,
                budget_hook=self._budget_hook,
            ),
        )
        finished = int(self._clock())
        if envelope is None:
            return SourceRun(
                SOURCE_RANKING_ALL, STATE_ERROR, reason="quota_exceeded",
                started_epoch_s=started, finished_epoch_s=finished, poll_slot_epoch_s=captured,
            )
        outcome = sources.parse_ranking(envelope, captured_epoch_s=poll_slot)
        return SourceRun(
            source=SOURCE_RANKING_ALL,
            state=outcome.state,
            items=list(outcome.items),
            returned_count=outcome.returned_count,
            error_code=outcome.error_code,
            reason=outcome.reason,
            started_epoch_s=started,
            finished_epoch_s=finished,
            poll_slot_epoch_s=poll_slot,
            from_cache=from_cache,
            cache_age_s=age,
            others_count=outcome.others_count,
        )

    # ------------------------------------------------------------- 工具
    @staticmethod
    def _params_hash(params: Dict[str, Any]) -> str:
        """计算参数的稳定 hash（用于快照身份）。

        Args:
            params: 参数 dict。

        Returns:
            str: sha1 hex。
        """
        blob = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _snapshot_id(params_hash: str, captured_epoch_s: int) -> str:
        """按「参数 hash + 采样时刻」生成稳定快照标识。

        Args:
            params_hash: 参数 hash。
            captured_epoch_s: 采样时刻（UTC 秒）。

        Returns:
            str: sha1 hex（长度 40，满足 snapshot_id 上限 64）。
        """
        return hashlib.sha1(f"{params_hash}|{int(captured_epoch_s)}".encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 调度入口（接调度）
# --------------------------------------------------------------------------

def build_discovery_service(api: Any = None, **kwargs: Any) -> DiscoveryService:
    """构造默认发现服务（共享轮询缓存 + 全局快照 + 真实记账钩子）。

    Args:
        api: B 站 API 客户端。
        **kwargs: 透传给 :class:`DiscoveryService`。

    Returns:
        DiscoveryService: 可直接接入常驻调度的服务实例。
    """
    return DiscoveryService(api, **kwargs)


async def run_discovery_loop(
    service: DiscoveryService,
    *,
    stop_event: Optional[asyncio.Event] = None,
    interval_s: int = 600,
    jitter_ratio: float = 0.1,
    backoff_base_s: int = 60,
    backoff_max_s: int = 1800,
    on_snapshot: Optional[Callable[[DiscoveryService], Any]] = None,
) -> None:
    """发现通道低频调度循环（接调度入口）。

    集成点（与既有 ``core.monitor_service.ResidentCommentMonitor`` 启停约定一致）：

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_discovery_loop(service, stop_event=stop_event, interval_s=600),
            name="bili-discovery",
        )

    行为：
        - ``stop_event`` 置位即退出；``CancelledError`` 原样上抛（不吞取消）；
        - 单轮失败按指数退避重试，封顶 ``backoff_max_s``；
        - 正常运行间隔叠加 ``jitter_ratio`` 抖动，降低被风控识别的概率；
        - 三入口**共享一轮 poll**（``poll_once`` 内部已用共享缓存），不会每轮重复发；
        - ``on_snapshot`` 每轮 poll 成功后调用一次（可等待返回值则 ``await``，sync 回调直接调用）；
          回调自身抛异常走本循环既有的 ``except Exception`` + 指数退避策略，不静默吞掉、
          也不打死常驻循环；**不传时行为与既有完全一致**。

    Args:
        service: 已构造的发现服务。
        stop_event: 外部停止事件；缺省新建（永不外部置位，仅作占位）。
        interval_s: 正常轮询间隔（秒）。
        jitter_ratio: 间隔抖动比例。
        backoff_base_s: 失败退避基数（秒）。
        backoff_max_s: 失败退避上限（秒）。
        on_snapshot: 每轮 poll 成功后的回调，入参为本服务实例（便于回调侧读
            ``iter_video_candidates()`` 等内存结果）；``None``（默认）时不回调。

    Returns:
        无（循环直至停止或取消）。
    """
    stop_event = stop_event or asyncio.Event()
    failures = 0
    while not stop_event.is_set():
        try:
            snapshot = await service.poll_once()
            failures = 0
            logger.info(
                "发现轮询完成 snapshot=%s served_from_cache=%s keyword=%s video=%s",
                snapshot.get("snapshot_id"),
                snapshot.get("served_from_cache"),
                snapshot.get("keyword_count"),
                snapshot.get("video_count"),
            )
            if on_snapshot is not None:
                # 回调放在 poll 成功之后、退避结算之前：回调异常会落到本循环既有的
                # except Exception 分支按指数退避处理（logger.warning 留痕，不静默吞掉）。
                await _dispatch_on_snapshot(on_snapshot, service)
            delay = max(1, int(interval_s)) * (1 + random.uniform(0, max(0.0, jitter_ratio)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单轮失败不退出常驻循环
            failures += 1
            delay = min(int(backoff_max_s), int(backoff_base_s) * (2 ** min(failures, 5)))
            logger.warning("发现轮询失败（第 %s 次），%ss 后退避重试: %r", failures, delay, exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            continue


async def _dispatch_on_snapshot(
    on_snapshot: Callable[[DiscoveryService], Any],
    service: DiscoveryService,
) -> None:
    """调用一轮发现快照回调：sync 回调直接调，async 回调（返回可等待对象）则 await。

    为什么两种都接：``run_discovery_loop`` 是通用调度入口，集成方可能给同步函数
    （如纯内存入池装配），也可能是需要 await 的协程函数（如要开/关会话的入池流程）；
    统一用 ``inspect.isawaitable`` 判定，不强迫调用方包一层 ``create_task``，
    也避免 sync 回调被误 await。

    异常策略：**本函数不吞异常**。回调抛出的异常原样上抛给 ``run_discovery_loop``，
    由该循环既有的 ``except Exception`` 分支按指数退避处理并 ``logger.warning`` 留痕，
    保证回调失败不会打死常驻循环、也不会被静默忽略。

    Args:
        on_snapshot: 回调可调用对象；入参为本轮发现服务实例。
        service: 本轮完成 poll 的发现服务实例。

    Returns:
        无。
    """
    result = on_snapshot(service)
    if inspect.isawaitable(result):
        await result
