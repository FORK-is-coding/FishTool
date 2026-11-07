"""告警推送：级别过滤、合并窗口、去重、断线补发、关闭。"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from fishtool_adapter.constants import chat_cfg
from fishtool_adapter.errors import CommandError
from fishtool_adapter.events import EventRelay
from fishtool_adapter.notifier import MAX_BATCH, AlertNotifier, level_allowed

log = logging.getLogger("test")
SESSIONS = [{"umo": "qq:GroupMessage:1", "self_id": "10001"}]


def run(coro):
    return asyncio.run(coro)


class Harness:
    def __init__(self, sessions=SESSIONS, unread=None, fetch_exc=None, send_exc=None, now=1_000_000.0, **raw):
        self.batches = []
        self.unread = unread or []
        self.fetch_exc = fetch_exc
        self.send_exc = send_exc
        self.now = now
        self.sleeps = []
        raw.setdefault("notify_min_level", "low")
        self.notifier = AlertNotifier(
            chat_cfg(raw),
            sessions=lambda: list(sessions),
            send_batch=self._send,
            fetch_unread=self._fetch,
            logger=log,
            clock=lambda: self.now,
            sleep=self._sleep,
        )

    async def _send(self, sessions, alerts, overflow):
        if self.send_exc:
            raise self.send_exc
        self.batches.append((sessions, alerts, overflow))

    async def _fetch(self):
        if self.fetch_exc:
            raise self.fetch_exc
        return self.unread

    async def _sleep(self, seconds):
        self.sleeps.append(seconds)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize("level,minimum,ok", [
    ("high", "high", True), ("medium", "high", False),
    ("medium", "medium", True), ("low", "medium", False),
    ("low", "low", True), (None, "low", True), ("weird", "medium", False),
])
def test_level_allowed(level, minimum, ok):
    assert level_allowed(level, minimum) is ok


def test_alerts_merged_into_one_batch_after_window():
    async def main():
        h = Harness(notify_merge_seconds=120)
        for i in range(3):
            h.notifier.add({"id": i, "level": "high", "message": f"m{i}"})
        await settle()
        return h

    h = run(main())
    assert h.sleeps == [120]
    assert len(h.batches) == 1
    sessions, alerts, overflow = h.batches[0]
    assert sessions == SESSIONS and [a["message"] for a in alerts] == ["m0", "m1", "m2"] and overflow == 0


def test_level_filter_and_dedup():
    async def main():
        h = Harness(notify_min_level="high")
        assert h.notifier.add({"id": 1, "level": "medium"}) is False
        assert h.notifier.add({"id": 2, "level": "high"}) is True
        assert h.notifier.add({"id": 2, "level": "high"}) is False
        await settle()
        return h

    h = run(main())
    assert len(h.batches) == 1 and len(h.batches[0][1]) == 1


def test_disabled_or_no_sessions():
    async def main():
        off = Harness(enable=False)
        assert off.notifier.add({"level": "high"}) is False
        empty = Harness(sessions=[])
        empty.notifier.add({"level": "high"})
        await settle()
        return off, empty

    off, empty = run(main())
    assert off.batches == [] and empty.batches == []


def test_no_sessions_keeps_alerts_and_pushes_after_subscribe():
    """无订阅会话不是“已推送”：缓冲要留住，id 不能进去重表，重新订阅后同一批要发得出去。"""
    async def main():
        sessions = []
        h = Harness(sessions=sessions)
        h.notifier.add({"id": 42, "level": "high", "message": "m"})
        await h.notifier.flush()
        assert h.batches == []
        buffered = h.notifier.buffered
        sessions.extend(SESSIONS)
        await h.notifier.flush()
        return h, buffered

    h, buffered = run(main())
    assert buffered == 1
    assert [[a["id"] for a in batch[1]] for batch in h.batches] == [[42]]
    assert h.notifier.buffered == 0


def test_backfill_does_not_duplicate_still_buffered_alert():
    """补发遇到还躺在缓冲里的同一条，不重复入队；推送成功后正常进去重表。"""
    async def main():
        sessions = []
        h = Harness(sessions=sessions)
        h.notifier.add({"id": 42, "level": "high"})
        await h.notifier.flush()
        sessions.extend(SESSIONS)
        h.unread = [{"id": 42, "level": "high"}]
        added = await h.notifier.backfill(0)
        await h.notifier.flush()
        again = await h.notifier.backfill(0)
        return h, added, again

    h, added, again = run(main())
    assert added == 0
    assert again == 0
    assert len(h.batches) == 1 and [a["id"] for a in h.batches[0][1]] == [42]


def test_send_failure_keeps_alert_recoverable_by_backfill():
    """推送失败的一批不算已推送：重连补发能把同一条重新捞回来并发出。"""
    async def main():
        h = Harness(send_exc=RuntimeError("platform down"))
        h.notifier.add({"id": 5, "level": "high"})
        await h.notifier.flush()
        h.send_exc = None
        h.unread = [{"id": 5, "level": "high"}]
        added = await h.notifier.backfill(0)
        await h.notifier.flush()
        return h, added

    h, added = run(main())
    assert added == 1
    assert [a["id"] for a in h.batches[0][1]] == [5]


def test_overflow_counted():
    async def main():
        h = Harness()
        for i in range(MAX_BATCH + 4):
            h.notifier.add({"id": i, "level": "high"})
        await settle()
        return h

    h = run(main())
    _, alerts, overflow = h.batches[0]
    assert len(alerts) == MAX_BATCH and overflow == 4


def test_send_failure_logged(caplog):
    async def main():
        h = Harness(send_exc=RuntimeError("platform down"))
        h.notifier.add({"level": "high"})
        await settle()
        return h

    with caplog.at_level(logging.WARNING):
        run(main())
    assert "告警推送失败" in caplog.text


def test_first_connection_does_not_backfill():
    async def main():
        h = Harness(unread=[{"id": 1, "level": "high", "created_at": "2026-11-04T10:00:00"}])
        h.notifier.on_connection(True)
        await settle()
        return h

    h = run(main())
    assert h.batches == [] and h.notifier.buffered == 0


def test_alert_arriving_during_send_is_not_stranded():
    """推送进行中到达的告警：本轮定时任务结束前要补排下一轮，否则没有后续告警时会一直卡在缓冲区。"""
    async def main():
        h = Harness()
        gate = asyncio.Event()
        original = h._send

        async def slow_send(sessions, alerts, overflow):
            if not h.batches:
                h.notifier.add({"id": 99, "level": "high", "message": "late"})  # 推送期间到达
                await gate.wait()
            await original(sessions, alerts, overflow)

        h.notifier._send_batch = slow_send
        h.notifier.add({"id": 1, "level": "high", "message": "first"})
        await settle()
        gate.set()
        await settle()
        await settle()
        return h

    h = run(main())
    assert [[a["message"] for a in b[1]] for b in h.batches] == [["first"], ["late"]]
    assert h.notifier.buffered == 0


def test_reconnect_backfills_only_after_disconnect():
    disconnect_at = 1_000_000.0
    before = "1970-01-12T13:45:00"   # 早于 disconnect_at 100 秒，超出 60 秒放宽窗口
    slack = "1970-01-12T13:46:30"    # 早于 disconnect_at 10 秒，落在放宽窗口内
    after = "1970-01-12T13:47:00"    # > disconnect_at

    async def main():
        h = Harness(now=disconnect_at - 10)
        # 用 UTC 字符串避免时区差异
        h.unread = [
            {"id": 3, "level": "high", "created_at": after + "+00:00", "message": "new"},
            {"id": 4, "level": "high", "created_at": slack + "+00:00", "message": "edge"},
            {"id": 2, "level": "high", "created_at": before + "+00:00", "message": "old"},
        ]
        h.notifier.on_connection(True)
        h.now = disconnect_at
        h.notifier.on_connection(False)
        h.notifier.on_connection(True)
        await settle()
        return h

    h = run(main())
    assert len(h.batches) == 1
    assert [a["message"] for a in h.batches[0][1]] == ["edge", "new"]


def test_backfill_skips_already_pushed():
    async def main():
        h = Harness()
        h.notifier.add({"id": 7, "level": "high"})
        await settle()
        h.unread = [{"id": 7, "level": "high"}, {"id": 8, "level": "high"}]
        added = await h.notifier.backfill(0)
        await settle()
        return h, added

    h, added = run(main())
    assert added == 1
    assert [a["id"] for a in h.batches[1][1]] == [8]


def test_backfill_query_failure_is_logged(caplog):
    async def main():
        h = Harness(fetch_exc=CommandError("引擎未运行"))
        return await h.notifier.backfill(0)

    with caplog.at_level(logging.WARNING):
        assert run(main()) == 0
    assert "补发查询失败" in caplog.text


def test_close_drops_buffer_and_cancels(caplog):
    async def main():
        h = Harness()
        h.notifier._sleep = lambda s: asyncio.sleep(3600)
        h.notifier.add({"level": "high"})
        await settle()
        await h.notifier.close()
        assert h.notifier.add({"level": "high"}) is False
        return h

    with caplog.at_level(logging.INFO):
        h = run(main())
    assert h.batches == []
    assert "丢弃 1 条未推送告警" in caplog.text


# ---------------------------------------------------------------- EventRelay wiring


class FakeSup:
    state = "running"
    port = 0


def test_relay_dispatches_alerts_and_tracks_tasks():
    async def main():
        relay = EventRelay(FakeSup(), log)
        got = []

        async def cb(data):
            got.append(data)

        async def bad(data):
            raise RuntimeError("cb failed")

        relay.on_alert.extend([cb, bad])
        relay.handle_message(json.dumps({"type": "alert", "data": {"id": 1}}))
        relay.handle_message(json.dumps({"type": "toast", "data": {}}))
        relay.handle_message("not json")
        relay.handle_message(json.dumps({"type": "alert", "data": "oops"}))
        await settle()
        await relay.stop()
        return got, relay

    got, relay = run(main())
    assert got == [{"id": 1}, {}]
    assert not relay._cb_tasks


def test_relay_connection_callback_only_on_change():
    relay = EventRelay(FakeSup(), log)
    seen = []
    relay.on_connection.append(seen.append)
    relay._set_connected(False)
    relay._set_connected(True)
    relay._set_connected(True)
    relay._set_connected(False)
    assert seen == [True, False]
