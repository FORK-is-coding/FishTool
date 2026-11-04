"""后台任务：去重占位、异常兜底、取消与关闭。"""
from __future__ import annotations

import asyncio
import logging

from fishtool_adapter.errors import CommandError
from fishtool_adapter.jobs import JobRunner

log = logging.getLogger("test")


class Sent:
    def __init__(self, ok=True):
        self.ok = ok
        self.items = []

    async def __call__(self, umo, text):
        self.items.append((umo, text))
        return self.ok


def run(coro):
    return asyncio.run(coro)


async def value(text):
    await asyncio.sleep(0)
    return text


async def fail(exc):
    await asyncio.sleep(0)
    raise exc


def test_claim_is_exclusive_until_release():
    runner = JobRunner(Sent(), log)
    assert runner.claim("monitor:BV1")
    assert not runner.claim("monitor:BV1")
    assert runner.claim("monitor:BV2")
    runner.release("monitor:BV1")
    assert runner.claim("monitor:BV1")


def test_success_pushes_text_and_releases():
    async def main():
        sent = Sent()
        runner = JobRunner(sent, log)
        runner.claim("k")
        await runner.run_text_job("k", "umo1", value("done"))
        return sent, runner

    sent, runner = run(main())
    assert sent.items == [("umo1", "done")]
    assert not runner.is_running("k")


def test_command_error_replied_verbatim():
    async def main():
        sent = Sent()
        runner = JobRunner(sent, log)
        runner.claim("k")
        await runner.run_text_job("k", "u", fail(CommandError("引擎未运行")))
        return sent, runner

    sent, runner = run(main())
    assert sent.items == [("u", "引擎未运行")]
    assert not runner.is_running("k")


def test_unexpected_error_logged_and_reported(caplog):
    async def main():
        sent = Sent()
        runner = JobRunner(sent, log)
        runner.claim("k")
        await runner.run_text_job("k", "u", fail(KeyError("x")))
        return sent, runner

    with caplog.at_level(logging.ERROR):
        sent, runner = run(main())
    assert "内部错误" in sent.items[0][1] and "KeyError" in sent.items[0][1]
    assert "k 执行失败" in caplog.text
    assert any(r.exc_info for r in caplog.records)
    assert not runner.is_running("k")


def test_spawn_tracks_and_close_cancels():
    async def main():
        sent = Sent()
        runner = JobRunner(sent, log)
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(3600)
            return "never"

        runner.claim("k")
        runner.spawn(runner.run_text_job("k", "u", slow()))
        await started.wait()
        assert runner.pending == 1
        await runner.close()
        return sent, runner

    sent, runner = run(main())
    assert sent.items == []  # 取消后不推送
    assert runner.pending == 0
    assert not runner.is_running("k")


def test_closed_runner_rejects_new_work():
    async def main():
        runner = JobRunner(Sent(), log)
        await runner.close()
        coro = value("x")
        assert runner.spawn(coro) is None
        assert not runner.claim("k")
        return runner

    run(main())


def test_no_push_after_close_during_job():
    async def main():
        sent = Sent()
        runner = JobRunner(sent, log)

        async def flip():
            runner.closed = True  # 模拟任务结束前插件被卸载
            return "late"

        runner.claim("k")
        await runner.run_text_job("k", "u", flip())
        return sent

    assert run(main()).items == []


def test_start_text_job_cancelled_before_start_leaves_no_unawaited_coroutine(recwarn):
    async def main():
        sent = Sent()
        runner = JobRunner(sent, log)

        async def never():
            await asyncio.sleep(3600)
            return "x"

        runner.claim("k")
        runner.start_text_job("k", "u", never())
        await runner.close()  # 没有让出事件循环：任务尚未开始就被取消
        return sent, runner

    sent, runner = run(main())
    import gc

    gc.collect()
    assert not [w for w in recwarn if "never awaited" in str(w.message)]
    assert sent.items == [] and runner.pending == 0


def test_start_text_job_runs_and_pushes():
    async def main():
        sent = Sent()
        runner = JobRunner(sent, log)
        runner.claim("k")
        task = runner.start_text_job("k", "u", value("ok"))
        await task
        return sent, runner

    sent, runner = run(main())
    assert sent.items == [("u", "ok")]
    assert not runner._inner


def test_done_callback_logs_escaped_exception(caplog):
    async def main():
        runner = JobRunner(Sent(), log)
        runner.spawn(fail(RuntimeError("leak")), name="raw")
        await asyncio.sleep(0.01)
        return runner

    with caplog.at_level(logging.ERROR):
        runner = run(main())
    assert "raw 异常退出" in caplog.text
    assert runner.pending == 0
