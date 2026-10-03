"""评论区常驻监控服务：只负责评论增量采集，不调度动态采集。"""
import asyncio
import random
from datetime import datetime
from time import time
from typing import Any, Callable, Optional

from bilibili.cookie_pool import get_cookie_pool
from core import quota_store
from core.config import ConfigManager
from core.database import MonitorState, get_session
from core.logger import get_logger
from core.request_budget import log_quota_usage

logger = get_logger(__name__)

#: watch 常驻循环默认轮询间隔（秒）；与 modules/hotspot/watch_service.DEFAULT_LOOP_INTERVAL_S 一致。
DEFAULT_WATCH_INTERVAL_S: int = 60

#: discovery 发现循环默认轮询间隔（秒）；对齐 run_discovery_loop 的 interval_s 默认值。
DEFAULT_DISCOVERY_INTERVAL_S: int = 600


def build_watch_service(**kwargs: Any) -> Any:
    """构造默认 watch 服务（照 modules.hotspot.discovery.service.build_discovery_service 样板）。

    只在真正要跑 watch 循环时才被调用（``ResidentCommentMonitor.watch_service`` 惰性触发），
    **导入期不构造客户端、不发任何网络请求**。采集端口 / 会话工厂 / 时钟三个依赖一律走
    ``WatchService`` 自身的缺省装配（``collector_port`` 惰性构造既有 ``HotspotCollector``，
    ``session_factory`` 用 ``core.database.get_session``，``now_fn`` 用 ``utc_now_epoch_s``），
    本函数**不 new 任何 API 客户端**，避免与既有依赖获取方式分叉。

    Args:
        **kwargs: 透传给 ``modules.hotspot.watch_service.WatchService``（便于单测注入替身）。

    Returns:
        WatchService: 可直接接入常驻调度的编排层实例。

    Note:
        P1 装配默认注入：调用方未显式传 ``budget`` / ``demand_reconcile_hook`` 时，
        本函数补齐一个 ``RequestBudget`` 与 ``EventWatchDemandReconciler().reconcile``，
        使常驻 watch 循环与「缺省采集端口」共用同一预算实例，并在每轮 tick 开头对账事件需求。
    """
    from modules.hotspot.watch_service import WatchService

    # P1 装配默认注入：缺省时补预算门与需求整编 hook。用显式 ``if not in kwargs`` 判断，
    # **不用 setdefault**——setdefault 的第二参数会被先求值，违背「调用方已显式注入时不构造
    # 真实依赖」的意图。budget 先于 hook 构造，二者互不依赖。
    if "budget" not in kwargs:
        from modules.hotspot import risk_control

        kwargs["budget"] = risk_control.RequestBudget()
    if "demand_reconcile_hook" not in kwargs:
        from modules.hotspot.event_watch_demands import EventWatchDemandReconciler

        kwargs["demand_reconcile_hook"] = EventWatchDemandReconciler().reconcile

    return WatchService(**kwargs)


def build_resident_discovery_service(**kwargs: Any) -> Any:
    """构造默认发现服务（照 modules.hotspot.discovery.service.build_discovery_service 样板）。

    只在真正要跑 discovery 发现循环时才被调用（``ResidentCommentMonitor.discovery_service``
    惰性触发），**导入期不构造客户端、不发任何网络请求**。``api`` / ``snapshot_store`` /
    各 ``*_store`` / ``clock`` 一律由 ``DiscoveryService`` 自身的缺省装配与
    ``build_discovery_service`` 透传决定，本函数**不 new 任何 API 客户端**，
    避免与既有依赖获取方式分叉。

    Args:
        **kwargs: 透传给 ``modules.hotspot.discovery.service.build_discovery_service``
            （便于单测注入替身）。

    Returns:
        DiscoveryService: 可直接接入常驻调度的发现服务实例。
    """
    from modules.hotspot.discovery.service import build_discovery_service

    return build_discovery_service(**kwargs)


class ResidentCommentMonitor:
    """管理评论区常驻任务、状态持久化和可控启停。"""

    def __init__(
        self,
        monitor_factory: Callable,
        config: Optional[ConfigManager] = None,
        watch_service_factory: Optional[Callable] = None,
        discovery_service_factory: Optional[Callable] = None,
    ):
        """初始化服务。

        Args:
            monitor_factory: 返回 CommentMonitor 单例的工厂函数。
            config: 配置管理器；未传入时自动读取默认配置。
            watch_service_factory: 返回 watch 服务的工厂函数；缺省用 :func:`build_watch_service`，
                且**惰性调用** —— 只有真要跑 watch 循环时才装配，默认配置下永不调用。
            discovery_service_factory: 返回发现服务的工厂函数；缺省用
                :func:`build_resident_discovery_service`，同样**惰性调用** —— 只有真要跑
                discovery 发现循环时才装配，默认配置下永不调用。
        """
        self.monitor_factory = monitor_factory
        self.config = config or ConfigManager()
        self.monitor_task: Optional[asyncio.Task] = None
        self.cookie_task: Optional[asyncio.Task] = None
        # 第三条常驻 task：watch 采样循环（默认关闭，仅显式配置 monitor.watch_enable=True 才拉起）。
        self.watch_task: Optional[asyncio.Task] = None
        # 第四条常驻 task：discovery 发现循环（默认关闭，仅显式配置 monitor.discovery_enable=True 才拉起）。
        self.discovery_task: Optional[asyncio.Task] = None
        self.stop_event = asyncio.Event()
        # watch 服务的构造入口；缺省走缺省装配（导入期不建客户端、不发请求）。
        self.watch_service_factory = watch_service_factory or build_watch_service
        self._watch_service: Any = None
        # discovery 服务的构造入口；同样惰性，导入期不建客户端、不发请求。
        self.discovery_service_factory = discovery_service_factory or build_resident_discovery_service
        self._discovery_service: Any = None

    def _state(self, session):
        """读取或创建 SQLite 中的单例状态记录。"""
        # 按固定 name 查单例状态记录，保证整个服务只有一条监控状态可持久化。
        state = session.query(MonitorState).filter_by(name="comment_monitor").first()
        # 首次启动时创建空状态记录，目标 BV 列表留空，等待前端或接口填充。
        if state is None:
            state = MonitorState(name="comment_monitor", target_bvids=[])
            session.add(state)
            session.commit()
        return state

    def snapshot(self) -> dict:
        """返回前端状态卡片所需的结构化状态。"""
        session = get_session()
        try:
            state = self._state(session)
            return {
                "enabled": bool(state.enabled),  # 是否启用常驻任务
                
                "paused": bool(state.paused),
                "status": state.status,
                "target_bvids": list(state.target_bvids or []),
                "last_collect_at": state.last_collect_at.isoformat() if state.last_collect_at else None,
                "total_collected": int(state.total_collected or 0),
                "last_error": state.last_error,
                "consecutive_failures": int(state.consecutive_failures or 0),
                "task_running": bool(self.monitor_task and not self.monitor_task.done()),  # 采集协程是否真实存活
                
                "cookie_task_running": bool(self.cookie_task and not self.cookie_task.done()),  # Cookie 巡检协程是否真实存活
                "watch_task_running": bool(self.watch_task and not self.watch_task.done()),  # watch 采样协程是否真实存活
                "discovery_task_running": bool(self.discovery_task and not self.discovery_task.done()),  # discovery 发现协程是否真实存活
                
            }
        except Exception as exc:
            logger.exception("读取常驻监控状态失败: %s", exc)
            return {"enabled": False, "paused": False, "status": "stopped", "error": str(exc)}
        finally:
            session.close()

    def _update(self, **values) -> None:
        """将控制状态或采集统计写入 SQLite。"""
        session = get_session()
        try:
            state = self._state(session)
            for key, value in values.items():
                setattr(state, key, value)
            state.updated_at = datetime.now()
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("写入常驻监控状态失败")
        finally:
            session.close()

    async def start(self) -> None:
        """按配置启动 Cookie 巡检、watch 采样（可选）、discovery 发现（可选）与评论监控任务；动态采集不在此处启动。"""
        self.stop_event.clear()
        state = self.snapshot()
        # Cookie 巡检协程没有存活实例时才新建，避免重复拉起多个巡检任务。
        if self.cookie_task is None or self.cookie_task.done():
            # 将耗时工作交给后台协程，接口立即返回任务 ID 供前端轮询。
            self.cookie_task = asyncio.create_task(self._cookie_loop(), name="bili-cookie-check")
        # watch 采样循环默认关闭：只有显式配置 monitor.watch_enable=True 才拉起（会真采样、真烧配额）。
        if bool(self.config.get("monitor.watch_enable", False)):
            self._ensure_watch_task()
        # discovery 发现循环默认关闭：只有显式配置 monitor.discovery_enable=True 才拉起。
        # 注：入池（watch_ingest）跟着本开关走 —— 入池不发请求、不花钱；真正花钱的采样
        # 仍由 monitor.watch_enable 单独把关，两个开关职责不重叠。
        if bool(self.config.get("monitor.discovery_enable", False)):
            self._ensure_discovery_task()
        # 配置开启且状态未被手动关闭时，跟随配置自动进入运行态。
        if bool(self.config.get("monitor.enable", False)) and state.get("enabled", False) is not False:
            await self.enable()
        # 配置开启但状态为空时同样启动，目标为空则采集循环直接无事可做保持待命。
        elif bool(self.config.get("monitor.enable", False)):
            await self.enable()
        # 配置未开启：明确写回停止态，避免历史状态残留误导前端。
        else:
            self._update(status="stopped", enabled=False, paused=False)

    async def enable(self, bvids: Optional[list[str]] = None) -> dict:
        """启用常驻评论监控，可选更新目标 BV 列表。"""
        # 未传新目标时沿用数据库里已保存的目标，保证重复调用不丢失配置。
        current = self.snapshot().get("target_bvids", [])
        # 统一转字符串并去掉首尾空白，过滤空串后落库。
        targets = [str(item).strip() for item in (bvids if bvids is not None else current) if str(item).strip()]
        # 清空历史错误信息，让前端卡片从"运行中"干净起步。
        self._update(enabled=True, paused=False, status="running", target_bvids=targets, last_error=None)
        # 采集主循环同样只允许单实例；已存活时不重复创建。
        if self.monitor_task is None or self.monitor_task.done():
            # 将耗时工作交给后台协程，接口立即返回任务 ID 供前端轮询。
            self.monitor_task = asyncio.create_task(self._monitor_loop(), name="bili-comment-monitor")
        return self.snapshot()

    async def pause(self) -> dict:
        """暂停采集但保留目标和累计统计。"""
        # 注意 paused=True 但 enabled 保持 True：恢复时无需重新加载目标列表。
        self._update(enabled=True, paused=True, status="paused")
        return self.snapshot()

    async def stop(self) -> dict:
        """停止评论常驻任务并保留历史状态。"""
        self._update(enabled=False, paused=False, status="stopped")
        return self.snapshot()

    async def shutdown(self) -> None:
        """取消后台任务并等待退出，避免服务关闭时遗留任务。"""
        # 先置停止事件让循环自然退出，再取消协程，双保险防止任务悬挂。
        self.stop_event.set()
        for task in (self.monitor_task, self.cookie_task, self.watch_task, self.discovery_task):
            if task and not task.done():
                task.cancel()
        # 逐个等待协程结束，CancelledError 属于正常取消路径，不当作异常处理。
        for task in (self.monitor_task, self.cookie_task, self.watch_task, self.discovery_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("后台监控任务退出异常")
        self._update(status="stopped")

    def _ensure_watch_task(self) -> None:
        """按既有单实例约定拉起 watch 常驻循环：已存活则复用，不重复创建。

        守卫写法与 ``_cookie_loop`` / ``_monitor_loop`` 完全一致（``is None or done()``），
        保证重复 ``start()`` 不会拉起第二条 watch 循环。

        Returns:
            无。
        """
        if self.watch_task is None or self.watch_task.done():
            # 名称固定为 "bili-watch"，与另两条常驻 task 命名风格一致。
            self.watch_task = asyncio.create_task(self._watch_loop(), name="bili-watch")

    def _ensure_discovery_task(self) -> None:
        """按既有单实例约定拉起 discovery 常驻循环：已存活则复用，不重复创建。

        守卫写法与 ``_cookie_loop`` / ``_monitor_loop`` / ``_ensure_watch_task`` 完全一致
        （``is None or done()``），保证重复 ``start()`` 不会拉起第二条 discovery 循环。

        Returns:
            无。
        """
        if self.discovery_task is None or self.discovery_task.done():
            # 名称固定为 "bili-discovery"，与 run_discovery_loop docstring 的集成点示例一致。
            self.discovery_task = asyncio.create_task(self._discovery_loop(), name="bili-discovery")

    @property
    def watch_service(self) -> Any:
        """惰性构造 watch 服务；只在真要跑 watch 循环时才装配（默认配置下永不触发）。

        Returns:
            watch 编排层实例（由 ``watch_service_factory`` 产出）。
        """
        if self._watch_service is None:
            self._watch_service = self.watch_service_factory()
        return self._watch_service

    async def _watch_loop(self) -> None:
        """watch 常驻循环桥接：复用 watch 服务的既有 ``_loop``，共享本服务的 ``stop_event``。

        依赖注入说明：``WatchService`` 的 ``collector_port`` / ``session_factory`` / ``now_fn``
        三个依赖均由 :func:`build_watch_service` 走缺省装配，本桥接层不重复注入。

        ``_loop`` 内部已做到：``stop_event`` 置位即退出、单轮 tick 异常不中断循环、
        ``CancelledError`` 原样上抛（不吞取消），故 cancel 后任务能真正结束。

        Returns:
            无（循环直至停止或被取消）。
        """
        interval = max(1, int(self.config.get("monitor.watch_interval", DEFAULT_WATCH_INTERVAL_S)))
        # watch_service.py 在本批红线冻结内（一行未改），其常驻循环入口即 ``_loop``，故直接复用。
        await self.watch_service._loop(interval_s=interval, stop_event=self.stop_event)

    @property
    def discovery_service(self) -> Any:
        """惰性构造发现服务；只在真要跑 discovery 发现循环时才装配（默认配置下永不触发）。

        与 ``watch_service`` 属性同一路子：**导入期不构造客户端、不发任何网络请求**，
        首次访问才调 ``discovery_service_factory``。

        Returns:
            发现服务实例（由 ``discovery_service_factory`` 产出）。
        """
        if self._discovery_service is None:
            self._discovery_service = self.discovery_service_factory()
        return self._discovery_service

    async def _discovery_loop(self) -> None:
        """discovery 常驻循环桥接：复用发现通道的既有 ``run_discovery_loop``，共享本服务的 ``stop_event``。

        每轮 poll 成功后经 ``on_snapshot`` 回调把候选入 watch 池（装配见 ``_ingest_discovery_snapshot``）。
        ``run_discovery_loop`` 内部已做到：``stop_event`` 置位即退出、单轮失败指数退避、
        ``CancelledError`` 原样上抛（不吞取消），故 cancel 后任务能真正结束。

        Returns:
            无（循环直至停止或被取消）。
        """
        # 局部导入：避免 core.monitor_service 在导入期就拉起整个 discovery 包（含 sources / 快照）。
        from modules.hotspot.discovery.service import run_discovery_loop

        interval = max(1, int(self.config.get("monitor.discovery_interval", DEFAULT_DISCOVERY_INTERVAL_S)))
        await run_discovery_loop(
            self.discovery_service,
            stop_event=self.stop_event,
            interval_s=interval,
            on_snapshot=self._ingest_discovery_snapshot,
        )

    async def _ingest_discovery_snapshot(self, service: Any) -> None:
        """把最近一轮发现候选入 watch 池（只吃内存结果，绝不额外发 HTTP）。

        装配口径：
            - 候选来源 = ``service.iter_video_candidates()``（上一轮 poll 的内存合并结果），
              本回调**不触发**任何入口请求；
            - 入池走 ``watch_ingest.ingest_video_candidates_to_watch``，只吃 ``popular`` /
              ``ranking_all``，``ranking_all_others`` / ``search_square`` 不入池；
            - 桥接函数只 flush，故此处补一次 ``commit``；失败 ``rollback`` 后原样抛出，
              交给 ``run_discovery_loop`` 现有的 try/except + 指数退避处理（不静默吞掉）。

        开关划分：本回调只在 ``monitor.discovery_enable=True`` 时挂上，**入池不需要单独开关**
        （入池不发请求、不花钱，故跟着 discovery 走）；真正花钱的采样仍由
        ``monitor.watch_enable`` 单独把关。

        Args:
            service: 已构造的发现服务（由 ``run_discovery_loop`` 每轮回传）。

        Returns:
            无。
        """
        # 局部导入：把「discovery 候选 → watch 池」的桥接依赖收敛到运行时，
        # 与本模块既有惰性装配风格一致（导入期不碰 watch_store / discovery 包）。
        from modules.hotspot.discovery.watch_ingest import ingest_video_candidates_to_watch

        candidates = service.iter_video_candidates()
        session = get_session()
        try:
            written = ingest_video_candidates_to_watch(
                session,
                candidates,
                now_epoch_s=int(time()),
            )
            session.commit()
            logger.info(
                "发现候选入 watch 池完成：候选 %s 条，入池 %s 条",
                len(candidates or []),
                written,
            )
        except Exception:
            # 入池失败：回滚本轮写入并原样抛出，由 run_discovery_loop 的退避分支处理。
            session.rollback()
            logger.exception("发现候选入 watch 池失败，本轮跳过")
            raise
        finally:
            session.close()

    def _quota_housekeeping(self) -> None:
        """调度循环每轮开头的配额维护，顺序固定：prune 配额桶 → 清 watch 到期行 → 打点。

        为什么必须在「调度之前」：
        - 25 小时前的配额桶不裁掉，表会无限增长且滚动窗口统计难对账；
        - 已到期行/过期桶必须先清再调，否则过期目标会一直占名额（规格 §3.2）。

        Returns:
            无；任何失败只记日志，不阻断本轮调度。
        """
        try:
            now = int(time())
            # 步骤 1：裁掉保留窗口之前的配额桶（保留窗口见 config/budget.yaml）。
            quota_store.prune(now)
            # 步骤 2：清 watch 到期行。
            #   02 单视频跟踪表已落地（批 2：models_hotspot_watch.HotspotWatch +
            #   modules/hotspot/watch_store 的调度/清理两条查询与 release_expired）。
            #   但按 02 方案 §9.9，评论常驻服务保持评论职责、不承载 watch 采样循环，
            #   真正「先清后调」由后续 watch_service._loop 负责；此处因此仍不接 watch
            #   清理，顺序约定（先清后调）不变。
            # 步骤 3：每轮打一行各类已用/上限。
            log_quota_usage(now)
        except Exception:
            # 维护失败不阻断调度：桶裁剪失败最多让表变大，不影响放行判定。
            logger.exception("配额维护失败，本轮跳过")

    async def _cookie_loop(self) -> None:
        """启动已有 auto_check_loop，并使用独立会话避免会话跨轮次泄漏。"""
        try:
            # 拉取全局 Cookie 池单例；巡检循环只在校验时短暂占用数据库会话。
            pool = get_cookie_pool()
            while not self.stop_event.is_set():
                # 每轮开头先做配额维护（prune 配额桶 → 清 watch 到期行），再巡检。
                self._quota_housekeeping()
                session = get_session()
                try:
                    # 批量校验池内 Cookie 有效性，失效项自动剔除或标记。
                    await pool.check_all_cookies(session)
                except Exception:
                    logger.exception("Cookie巡检失败，下一轮继续")
                finally:
                    session.close()
                try:
                    # 等待下一轮巡检；等待期间 stop_event 一旦被置位会立即退出循环。
                    await asyncio.wait_for(self.stop_event.wait(), timeout=max(30, int(pool.check_interval)))
                except asyncio.TimeoutError:
                    # 超时只代表进入下一轮巡检，不递归调用自身。
                    continue
            raise
        except Exception:
            logger.exception("Cookie常驻任务退出")

    async def _monitor_loop(self) -> None:
        """评论增量监控主循环，包含随机抖动与指数退避。"""
        failures = 0
        try:
            while not self.stop_event.is_set():
                # 每轮开头先做配额维护（prune 配额桶 → 清 watch 到期行），再调度采集。
                self._quota_housekeeping()
                state = self.snapshot()
                # 被停止后 enabled=False，循环在此退出，任务自然结束。
                if not state.get("enabled"):
                    break
                # 暂停态跳过采集但保留循环等待，恢复时无需重建任务。
                if not state.get("paused"):
                    try:
                        collected = await self._collect_targets(state.get("target_bvids", []))
                        failures = 0
                        self._update(last_collect_at=datetime.now(), total_collected=state.get("total_collected", 0) + collected, consecutive_failures=0, last_error=None)
                    except Exception as exc:
                        failures += 1
                        # 指数退避：连续失败次数越多等待越久，封顶 1800 秒避免长时间无意义重试。
                        delay = min(1800, int(self.config.get("monitor.check_interval", 300)) * (2 ** min(failures, 4)))
                        self._update(status="running", consecutive_failures=failures, last_error=str(exc))
                        logger.exception("评论常驻采集失败，将在 %s 秒后退避重试", delay)
                        await asyncio.sleep(delay + random.uniform(0, min(30, delay * 0.1)))
                        continue
                # 正常轮询间隔取配置值，并叠加 10% 内的随机抖动，降低被风控识别的概率。
                interval = max(10, int(self.config.get("monitor.check_interval", 300)))
                jitter = random.uniform(0, max(1, interval * 0.1))
                await asyncio.sleep(interval + jitter)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("评论常驻监控任务退出")
            self._update(status="stopped", last_error="监控任务异常退出")

    async def _collect_targets(self, bvids: list[str]) -> int:
        """按 SQLite checkpoint 增量采集目标视频，返回本轮新增入库数。"""
        if not bvids:
            return 0
        monitor = self.monitor_factory()
        total = 0
        # 逐个目标视频走增量采集（依赖 SQLite checkpoint），已有评论不会重复入库。
        for bvid in bvids:
            comments = await monitor.collector.collect_incremental_comments(bvid)
            # 统计本轮实际新增入库条数；采集器未暴露结果时退化为按评论条数估算。
            if comments:
                # 对新增评论执行舆情分析与预警，但不再次全量请求。
                # 新评论进入舆情分析流水线，只处理本轮增量，避免全量重算。
                sentiment_result = monitor.analyzer.analyze_batch(comments)
                monitor._apply_sentiment_results(comments, sentiment_result)
                dedup_result = monitor.deduplicator.deduplicate(comments)
                processed_comments = dedup_result['deduplicated_comments']
                # 命中预警规则时写入告警记录，供前端监控页展示。
                alerts = await monitor._detect_alerts(bvid, comments, processed_comments, sentiment_result)
                await monitor._save_monitoring_record(bvid, comments, alerts)
                total += len(comments)
            return total