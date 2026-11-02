"""引擎 ``/ws`` → 插件内广播 → SSE。

引擎推送两类消息（web/main.py:736-806）：``{"type":"toast",...}`` 与
``{"type":"alert","data":{...}}``。插件只维持**一个**到引擎的 WebSocket 长连接，再扇出给面板
SSE 订阅者与（二期）机器人推送回调。连接不带 Origin，引擎 is_cross_site_origin 判为非跨站。
"""
from __future__ import annotations

import asyncio
import json

MAX_QUEUE = 200
KEEPALIVE_S = 20


class EventRelay:
    def __init__(self, supervisor, logger):
        self.sup = supervisor
        self.log = logger
        self._subs: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.on_alert: list = []  # 二期：机器人推送回调 async def cb(data: dict)
        self.connected = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self._loop(), name="fishtool-event-relay")

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                self.log.debug("[fishtool] event relay cancelled")
        for q in list(self._subs):
            q.put_nowait(None)  # 让 SSE 生成器结束

    def publish(self, msg: dict) -> None:
        """把一条消息广播给所有 SSE 订阅者（慢消费者丢弃，不阻塞）。"""
        for q in list(self._subs):
            if q.qsize() < MAX_QUEUE:
                q.put_nowait(msg)

    async def _loop(self) -> None:
        import websockets

        backoff = 1
        while not self._stopped:
            if self.sup.state != "running" or not self.sup.port:
                self.connected = False
                await asyncio.sleep(1)
                continue
            port = self.sup.port
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/ws",
                    open_timeout=10,
                    ping_interval=30,
                    proxy=None,
                ) as ws:
                    self.connected = True
                    backoff = 1
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            self.log.debug("[fishtool] non-json ws message ignored")
                            continue
                        if not isinstance(msg, dict):
                            continue
                        self.publish(msg)
                        if msg.get("type") == "alert":
                            for cb in list(self.on_alert):
                                asyncio.create_task(cb(msg.get("data") or {}))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 引擎重启/断开时重连
                self.log.debug(f"[fishtool] ws relay retry: {exc!r}")
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def sse(self):
        """SSE 生成器：先发 ready，之后转发事件，空闲时发注释保活。"""
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if msg is None:
                    return
                event = str(msg.get("type") or "message").replace("\n", " ")
                yield f"event: {event}\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
        finally:
            self._subs.discard(q)
