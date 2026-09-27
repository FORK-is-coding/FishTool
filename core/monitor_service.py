"""评论区常驻监控服务：只负责评论增量采集，不调度动态采集。"""
import asyncio
import random
from datetime import datetime
from typing import Callable, Optional

from bilibili.cookie_pool import get_cookie_pool
from core.config import ConfigManager
from core.database import MonitorState, get_session
from core.logger import get_logger

logger = get_logger(__name__)


class ResidentCommentMonitor:
    """管理评论区常驻任务、状态持久化和可控启停。"""

    def __init__(self, monitor_factory: Callable, config: Optional[ConfigManager] = None):
        """初始化服务。

        Args:
            monitor_factory: 返回 CommentMonitor 单例的工厂函数。
            config: 配置管理器；未传入时自动读取默认配置。
        """
        self.monitor_factory = monitor_factory
        self.config = config or ConfigManager()
        self.monitor_task: Optional[asyncio.Task] = None
        self.cookie_task: Optional[asyncio.Task] = None
        self.stop_event = asyncio.Event()

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
        """按配置启动 Cookie 巡检和评论监控任务；动态采集不在此处启动。"""
        self.stop_event.clear()
        state = self.snapshot()
        # Cookie 巡检协程没有存活实例时才新建，避免重复拉起多个巡检任务。
        if self.cookie_task is None or self.cookie_task.done():
            # 将耗时工作交给后台协程，接口立即返回任务 ID 供前端轮询。
            self.cookie_task = asyncio.create_task(self._cookie_loop(), name="bili-cookie-check")
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
        for task in (self.monitor_task, self.cookie_task):
            if task and not task.done():
                task.cancel()
        # 逐个等待协程结束，CancelledError 属于正常取消路径，不当作异常处理。
        for task in (self.monitor_task, self.cookie_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("后台监控任务退出异常")
        self._update(status="stopped")

    async def _cookie_loop(self) -> None:
        """启动已有 auto_check_loop，并使用独立会话避免会话跨轮次泄漏。"""
        try:
            # 拉取全局 Cookie 池单例；巡检循环只在校验时短暂占用数据库会话。
            pool = get_cookie_pool()
            while not self.stop_event.is_set():
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