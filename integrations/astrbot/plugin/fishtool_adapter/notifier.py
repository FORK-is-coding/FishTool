"""评论舆情告警推送到聊天会话：级别过滤、合并窗口、去重、断线补发。

- 个人 QQ 短时间连发多条消息容易被风控：一个合并窗口内的告警只发一条（多条时由调用方组装成合并转发）。
- 引擎重启或 ws 断开期间推送的告警插件收不到；重连成功后查一次未读告警补发，按 id 去重。
- 首次连接不补发，避免把历史未读告警一次性全推出去。

不 import astrbot：发送函数、订阅读取、补发查询和 logger 都由 main.py 注入，便于单测。
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime

from .constants import ALERT_LEVELS
from .errors import CommandError

#: 最近推送过的告警 id，用于补发去重（插件重载后清空，所以重载前后可能有少量重复）。
SEEN_LIMIT = 500
#: 补发时最多拉取的未读告警数。
BACKFILL_LIMIT = 50
#: 多个订阅会话之间的发送间隔（秒），降低风控风险。
SEND_GAP_S = 1.0
#: 单次推送最多包含的告警条数；超出部分只计数。
MAX_BATCH = 30

#: 推送一批告警：``async (sessions, alerts, overflow) -> None``。
SendBatch = Callable[[list[dict], list[dict], int], Awaitable[None]]
#: 补发查询：``async () -> list[dict]``，返回未读告警（新到旧）。
FetchUnread = Callable[[], Awaitable[list[dict]]]


def level_allowed(level, minimum: str) -> bool:
    """``minimum`` 及以上的级别才推送；未知级别按 low 处理。"""
    order = {name: index for index, name in enumerate(ALERT_LEVELS)}  # high=0 medium=1 low=2
    return order.get(str(level or "low"), len(ALERT_LEVELS) - 1) <= order.get(minimum, 0)


def _parse_time(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class AlertNotifier:
    """每个插件实例一个。``close()`` 后不再接收和推送。"""

    def __init__(
        self,
        chat: dict,
        *,
        sessions: Callable[[], list[dict]],
        send_batch: SendBatch,
        fetch_unread: FetchUnread,
        logger,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable] = asyncio.sleep,
    ):
        self.chat = chat
        self._sessions = sessions
        self._send_batch = send_batch
        self._fetch_unread = fetch_unread
        self.log = logger
        self._clock = clock
        self._sleep = sleep
        self._buffer: list[dict] = []
        self._overflow = 0
        self._seen: OrderedDict = OrderedDict()
        self._flush_task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self._ever_connected = False
        self._disconnected_at: float | None = None
        self.closed = False

    # ------------------------------------------------------------------ intake
    async def on_alert(self, data: dict) -> None:
        """EventRelay 的告警回调。"""
        self.add(data)

    def add(self, data: dict) -> bool:
        """放进缓冲区；被过滤、重复或已关闭时返回 False。"""
        if self.closed or not self.chat.get("enable", True) or not isinstance(data, dict):
            return False
        if not level_allowed(data.get("level"), self.chat.get("notify_min_level", "high")):
            return False
        alert_id = data.get("id")
        if alert_id is not None:
            if alert_id in self._seen:
                return False
            self._remember(alert_id)
        if len(self._buffer) >= MAX_BATCH:
            self._overflow += 1
        else:
            self._buffer.append(dict(data))
        self._ensure_flush_scheduled()
        return True

    def _remember(self, alert_id) -> None:
        self._seen[alert_id] = None
        while len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)

    def _ensure_flush_scheduled(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_later(), name="fishtool-alert-flush")

    async def _flush_later(self) -> None:
        await self._sleep(self.chat.get("notify_merge_seconds", 120))
        await self.flush()

    async def flush(self) -> None:
        """把缓冲区里的告警推送给所有订阅会话。"""
        alerts, overflow = self._buffer, self._overflow
        self._buffer, self._overflow = [], 0
        if self.closed or not alerts:
            return
        sessions = self._sessions()
        if not sessions:
            self.log.debug(f"[fishtool] {len(alerts)} 条告警无订阅会话，跳过推送")
            return
        try:
            await self._send_batch(sessions, alerts, overflow)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 推送失败只记日志，告警仍在引擎里是未读状态
            self.log.warning(f"[fishtool] 告警推送失败: {exc!r}")

    # ------------------------------------------------------------------ backfill
    def on_connection(self, connected: bool) -> None:
        """EventRelay 的连接状态回调：断开时记时间，重连成功时补发断线期间的告警。"""
        if self.closed:
            return
        if not connected:
            if self._ever_connected and self._disconnected_at is None:
                self._disconnected_at = self._clock()
            return
        if not self._ever_connected:
            self._ever_connected = True  # 首次连接不补发
            return
        since = self._disconnected_at
        self._disconnected_at = None
        if since is None or not self.chat.get("enable", True):
            return
        if self._backfill_task is None or self._backfill_task.done():
            self._backfill_task = asyncio.create_task(self.backfill(since), name="fishtool-alert-backfill")

    async def backfill(self, since: float) -> int:
        """补发 ``since`` 之后创建的未读告警，返回补进缓冲区的条数。"""
        try:
            alerts = await self._fetch_unread()
        except asyncio.CancelledError:
            raise
        except CommandError as exc:
            self.log.warning(f"[fishtool] 告警补发查询失败: {exc}")
            return 0
        except Exception as exc:  # noqa: BLE001 - 补发是尽力而为
            self.log.warning(f"[fishtool] 告警补发查询失败: {exc!r}")
            return 0
        added = 0
        for alert in reversed([a for a in alerts if isinstance(a, dict)]):  # 旧的先进
            created = _parse_time(alert.get("created_at"))
            if created is not None and created < since:
                continue
            if self.add(alert):
                added += 1
        if added:
            self.log.info(f"[fishtool] 补发断线期间的告警 {added} 条")
        return added

    # ------------------------------------------------------------------ shutdown
    async def close(self) -> None:
        """取消定时任务；缓冲区里未发出的告警直接丢弃（它们在引擎里仍是未读，可用 /fishtool 告警 查到）。"""
        self.closed = True
        dropped = len(self._buffer) + self._overflow
        self._buffer, self._overflow = [], 0
        tasks = [t for t in (self._flush_task, self._backfill_task) if t and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if dropped:
            self.log.info(f"[fishtool] 插件停用，丢弃 {dropped} 条未推送告警")

    @property
    def buffered(self) -> int:
        return len(self._buffer) + self._overflow
