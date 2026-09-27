"""core.monitor_service 底座测试（第1批补齐 · core 段）。

覆盖范围：
- ResidentCommentMonitor.__init__ / _state / snapshot / _update
- ResidentCommentMonitor.start / enable / pause / stop / shutdown
- ResidentCommentMonitor._cookie_loop / _monitor_loop / _collect_targets

测试策略：
- 状态持久化使用 tmp_path 下真实 SQLite（MonitorState 表），不 Mock session。
- ``_monitor_loop`` / ``_cookie_loop`` 都是 ``while not stop_event`` 长循环：
  测试必须"同时替换时钟/睡眠 + 主动置位 stop_event"，否则协程不退出就是死循环。
  这里用 shim 把 ``asyncio.sleep`` 换成"记录延迟并置位 stop_event"，并叠加
  外层 ``asyncio.wait_for`` 双保险。
- 触发 logger 的分支用记录型 logger 替身，避免真实文件日志初始化。
"""
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

from core.database import DatabaseManager, MonitorState


monitor_module = importlib.import_module("core.monitor_service")
ResidentCommentMonitor = monitor_module.ResidentCommentMonitor


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _RecordingLogger:
    """记录日志调用的替身，避免测试触发真实文件日志初始化。"""

    def __init__(self) -> None:
        """初始化调用记录容器。"""
        self.calls = []

    def _record(self, level: str, *args, **kwargs) -> None:
        """保存级别与参数。"""
        self.calls.append((level, args, kwargs))

    def debug(self, *args, **kwargs):
        """记录 debug。"""
        self._record("debug", *args, **kwargs)

    def info(self, *args, **kwargs):
        """记录 info。"""
        self._record("info", *args, **kwargs)

    def warning(self, *args, **kwargs):
        """记录 warning。"""
        self._record("warning", *args, **kwargs)

    def error(self, *args, **kwargs):
        """记录 error。"""
        self._record("error", *args, **kwargs)

    def exception(self, *args, **kwargs):
        """记录 exception。"""
        self._record("exception", *args, **kwargs)


class _FakeConfig:
    """只暴露 get 的配置替身。"""

    def __init__(self, values: dict | None = None) -> None:
        """保存预置配置项。"""
        self._values = values or {}

    def get(self, key, default=None):
        """返回预置值或默认值。"""
        return self._values.get(key, default)


@pytest.fixture(autouse=True)
def quiet_logger(monkeypatch):
    """把 monitor 模块 logger 换成记录型替身。"""
    fake = _RecordingLogger()
    monkeypatch.setattr(monitor_module, "logger", fake)
    return fake


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """把 monitor 模块 get_session 指向 tmp_path 下的真实 SQLite。"""
    manager = DatabaseManager(str(tmp_path / "monitor.db"))
    monkeypatch.setattr(monitor_module, "get_session", manager.get_session)
    return manager


def _read_state(manager):
    """从真实库读取单例监控状态行。"""
    session = manager.get_session()
    try:
        return session.query(MonitorState).filter_by(name="comment_monitor").first()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 初始化 / 状态读写
# ---------------------------------------------------------------------------


def test_init_sets_factory_and_empty_tasks():
    """构造函数记录工厂、空任务与未置位的停止事件。"""
    monitor = ResidentCommentMonitor(monitor_factory=lambda: "M", config=_FakeConfig())
    assert monitor.monitor_factory() == "M"
    assert monitor.monitor_task is None
    assert monitor.cookie_task is None
    assert isinstance(monitor.stop_event, asyncio.Event)
    assert monitor.stop_event.is_set() is False


def test_snapshot_creates_singleton_state(db):
    """首次 snapshot 建单例状态行，字段为默认停止态。"""
    monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())
    snap = monitor.snapshot()
    assert snap["enabled"] is False
    assert snap["paused"] is False
    assert snap["status"] == "stopped"
    assert snap["target_bvids"] == []
    assert snap["last_collect_at"] is None
    assert snap["total_collected"] == 0
    assert snap["last_error"] is None
    assert snap["consecutive_failures"] == 0
    assert snap["task_running"] is False
    assert snap["cookie_task_running"] is False

    monitor.snapshot()  # 幂等：不应产生第二行
    session = db.get_session()
    try:
        assert session.query(MonitorState).count() == 1
    finally:
        session.close()


def test_snapshot_returns_error_dict_when_state_read_fails(db, monkeypatch):
    """状态读取异常时 snapshot 返回带 error 的降级字典，不抛出。"""
    monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())

    def _boom(session):
        """模拟状态读取失败。"""
        raise RuntimeError("boom")

    monkeypatch.setattr(monitor, "_state", _boom)
    result = monitor.snapshot()
    assert result["enabled"] is False
    assert result["status"] == "stopped"
    assert "boom" in result["error"]


def test_update_persists_values(db):
    """_update 写入的字段可被 snapshot 读回。"""
    monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())
    monitor._update(
        enabled=True,
        paused=True,
        status="paused",
        target_bvids=["BV1"],
        total_collected=5,
    )
    snap = monitor.snapshot()
    assert snap["enabled"] is True
    assert snap["paused"] is True
    assert snap["status"] == "paused"
    assert snap["target_bvids"] == ["BV1"]
    assert snap["total_collected"] == 5


def test_update_rolls_back_on_commit_failure(monkeypatch):
    """提交失败时回滚并关闭会话，不向调用方抛出。"""

    class _FailingSession:
        """commit 必然失败、记录 rollback/close 的会话替身。"""

        def __init__(self) -> None:
            """初始化计数。"""
            self.rollback_calls = 0
            self.close_calls = 0

        def commit(self):
            """模拟提交失败。"""
            raise RuntimeError("commit boom")

        def rollback(self):
            """累加回滚次数。"""
            self.rollback_calls += 1

        def close(self):
            """累加关闭次数。"""
            self.close_calls += 1

    monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())
    failing = _FailingSession()
    monkeypatch.setattr(monitor_module, "get_session", lambda: failing)
    monkeypatch.setattr(monitor, "_state", lambda session: SimpleNamespace())

    monitor._update(status="x")  # 不应抛出
    assert failing.rollback_calls == 1
    assert failing.close_calls == 1


# ---------------------------------------------------------------------------
# 启停控制
# ---------------------------------------------------------------------------


def test_enable_pause_stop_transitions(db, monkeypatch):
    """enable 规范化目标并建任务，pause/stop 保留可读状态。"""
    monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())
    loop_calls = []

    async def _fake_loop():
        """记录主循环被拉起。"""
        loop_calls.append(1)

    monkeypatch.setattr(monitor, "_monitor_loop", _fake_loop)

    async def _scenario():
        """顺序执行启用/暂停/停止。"""
        enabled = await monitor.enable(bvids=[" BV1 ", "", "BV2"])
        if monitor.monitor_task is not None:
            await monitor.monitor_task
        paused = await monitor.pause()
        stopped = await monitor.stop()
        return enabled, paused, stopped

    enabled, paused, stopped = asyncio.run(_scenario())
    assert enabled["enabled"] is True
    assert enabled["target_bvids"] == ["BV1", "BV2"]  # 去空白、过滤空串
    assert enabled["status"] == "running"
    assert loop_calls == [1]
    assert paused["paused"] is True
    assert paused["enabled"] is True  # 暂停仍保留 enabled
    assert paused["status"] == "paused"
    assert stopped["enabled"] is False
    assert stopped["status"] == "stopped"


def test_enable_reuses_alive_task(db, monkeypatch):
    """已有存活任务时 enable 不重复创建主循环任务。"""

    async def _scenario():
        """构造阻塞主循环并二次 enable。"""
        monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())
        started = asyncio.Event()

        async def _blocking_loop():
            """阻塞等待取消，模拟存活中的长任务。"""
            started.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(monitor, "_monitor_loop", _blocking_loop)
        await monitor.enable()
        first = monitor.monitor_task
        await started.wait()
        await monitor.enable()  # 任务存活 → 应复用
        second = monitor.monitor_task
        second.cancel()
        try:
            await second
        except asyncio.CancelledError:
            pass
        return first is second

    assert asyncio.run(_scenario()) is True


def test_start_follows_enabled_config(db, monkeypatch):
    """配置开启时 start 同时拉起 Cookie 巡检与评论采集。"""

    async def _scenario():
        """执行 start 并等待子任务。"""
        monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig({"monitor.enable": True}))

        async def _noop():
            """立即返回的占位协程。"""
            return None

        monkeypatch.setattr(monitor, "_cookie_loop", _noop)
        monkeypatch.setattr(monitor, "_monitor_loop", _noop)
        await monitor.start()
        if monitor.monitor_task:
            await monitor.monitor_task
        if monitor.cookie_task:
            await monitor.cookie_task
        return monitor.snapshot(), monitor

    snap, monitor = asyncio.run(_scenario())
    assert snap["enabled"] is True
    assert snap["status"] == "running"
    assert monitor.monitor_task is not None
    assert monitor.cookie_task is not None


def test_start_writes_stopped_when_config_disabled(db, monkeypatch):
    """配置关闭时 start 只拉 Cookie 巡检并写回停止态。"""

    async def _scenario():
        """执行 start 并等待子任务。"""
        monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig({"monitor.enable": False}))

        async def _noop():
            """立即返回的占位协程。"""
            return None

        monkeypatch.setattr(monitor, "_cookie_loop", _noop)
        monkeypatch.setattr(monitor, "_monitor_loop", _noop)
        await monitor.start()
        if monitor.cookie_task:
            await monitor.cookie_task
        return monitor.snapshot(), monitor

    snap, monitor = asyncio.run(_scenario())
    assert snap["enabled"] is False
    assert snap["status"] == "stopped"
    assert monitor.monitor_task is None
    assert monitor.cookie_task is not None  # Cookie 巡检始终拉起


def test_shutdown_cancels_and_marks_stopped(db):
    """shutdown 置位停止事件、取消两个后台任务并写停止态。"""

    async def _scenario():
        """构造两个阻塞任务并关闭。"""
        monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())

        async def _blocking():
            """阻塞等待取消。"""
            await asyncio.sleep(3600)

        monitor.monitor_task = asyncio.create_task(_blocking())
        monitor.cookie_task = asyncio.create_task(_blocking())
        await asyncio.sleep(0)  # 让两个任务真正启动
        await monitor.shutdown()
        return (
            monitor.stop_event.is_set(),
            monitor.monitor_task.cancelled(),
            monitor.cookie_task.cancelled(),
            monitor.snapshot()["status"],
        )

    is_set, monitor_cancelled, cookie_cancelled, status = asyncio.run(_scenario())
    assert is_set is True
    assert monitor_cancelled is True
    assert cookie_cancelled is True
    assert status == "stopped"


def test_shutdown_without_tasks_is_safe(db):
    """无任务时 shutdown 仍能安全写停止态。"""

    async def _scenario():
        """直接关闭空监控器。"""
        monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())
        await monitor.shutdown()
        return monitor.snapshot()["status"]

    assert asyncio.run(_scenario()) == "stopped"


# ---------------------------------------------------------------------------
# 增量采集流水线
# ---------------------------------------------------------------------------


def test_collect_targets_empty_returns_zero():
    """空目标列表直接返回 0，不访问采集器。"""
    monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())
    assert asyncio.run(monitor._collect_targets([])) == 0


class _Collector:
    """记录 bvid 并返回预置评论的采集器替身。"""

    def __init__(self, comments):
        """保存预置评论与调用记录。"""
        self.comments = comments
        self.calls = []

    async def collect_incremental_comments(self, bvid):
        """记录目标并返回增量评论。"""
        self.calls.append(bvid)
        return self.comments


class _Analyzer:
    """记录批次的舆情分析替身。"""

    def __init__(self):
        """初始化批次记录。"""
        self.batches = []

    def analyze_batch(self, comments):
        """记录输入并返回固定结果。"""
        self.batches.append(comments)
        return {"positive": 1}


class _Deduplicator:
    """记录输入的去重器替身。"""

    def __init__(self):
        """初始化调用记录。"""
        self.calls = []

    def deduplicate(self, comments):
        """返回去重结果（保留全部条目）。"""
        self.calls.append(comments)
        return {"deduplicated_comments": list(comments)}


class _FakeMonitor:
    """承载采集/分析/去重/预警/落库全链路的监控器替身。"""

    def __init__(self, comments):
        """初始化各子替身与调用记录。"""
        self.collector = _Collector(comments)
        self.analyzer = _Analyzer()
        self.deduplicator = _Deduplicator()
        self.sentiment_applied = []
        self.alert_calls = []
        self.saved_records = []

    def _apply_sentiment_results(self, comments, result):
        """记录舆情结果回写。"""
        self.sentiment_applied.append((comments, result))

    async def _detect_alerts(self, bvid, comments, processed, sentiment):
        """记录预警检测入参并返回告警。"""
        self.alert_calls.append((bvid, processed))
        return ["alert-1"]

    async def _save_monitoring_record(self, bvid, comments, alerts):
        """记录落库调用。"""
        self.saved_records.append((bvid, len(comments), alerts))


def test_collect_targets_runs_full_pipeline():
    """单个目标走完采集→分析→去重→预警→落库全链路。"""
    comments = [{"rpid": 1}, {"rpid": 2}]
    fake = _FakeMonitor(comments)
    monitor = ResidentCommentMonitor(lambda: fake, config=_FakeConfig())

    total = asyncio.run(monitor._collect_targets(["BV1"]))
    assert total == 2
    assert fake.collector.calls == ["BV1"]
    assert fake.analyzer.batches == [comments]
    assert fake.sentiment_applied == [(comments, {"positive": 1})]
    assert fake.deduplicator.calls == [comments]
    assert fake.alert_calls == [("BV1", comments)]
    assert fake.saved_records == [("BV1", 2, ["alert-1"])]


# ---------------------------------------------------------------------------
# 长循环（必须先解除"不退出"风险）
# ---------------------------------------------------------------------------


def test_monitor_loop_breaks_when_disabled(monkeypatch):
    """enabled=False 时主循环立即退出。"""
    monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())
    monkeypatch.setattr(monitor, "snapshot", lambda: {"enabled": False})
    asyncio.run(asyncio.wait_for(monitor._monitor_loop(), timeout=2))


def test_monitor_loop_paused_sleeps_interval_then_exits(db, monkeypatch):
    """暂停态跳过采集，按配置间隔睡眠；用 shim 记录延迟并置位 stop_event 退出。"""
    monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig({"monitor.check_interval": 300}))
    sleeps = []

    class _AsyncioShim:
        """代理真实 asyncio，仅拦截 sleep。"""

        def __init__(self) -> None:
            """持有真实模块。"""
            self._real = asyncio

        def __getattr__(self, name):
            """未拦截属性转发真实 asyncio。"""
            return getattr(self._real, name)

        async def sleep(self, delay, *args, **kwargs):
            """记录延迟并置位停止事件，避免死循环。"""
            sleeps.append(delay)
            monitor.stop_event.set()

    monkeypatch.setattr(monitor_module, "asyncio", _AsyncioShim())
    monkeypatch.setattr(monitor_module.random, "uniform", lambda low, high: 0.0)
    monkeypatch.setattr(
        monitor,
        "snapshot",
        lambda: {"enabled": True, "paused": True, "target_bvids": [], "total_collected": 0},
    )

    asyncio.run(asyncio.wait_for(monitor._monitor_loop(), timeout=2))
    assert sleeps == [300]


def test_monitor_loop_backoff_on_collect_failure(db, monkeypatch):
    """采集失败时按指数退避睡眠，并持久化失败次数与错误。"""
    monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig({"monitor.check_interval": 300}))

    async def _boom(bvids):
        """模拟采集失败。"""
        raise RuntimeError("collect boom")

    monkeypatch.setattr(monitor, "_collect_targets", _boom)

    sleeps = []

    class _AsyncioShim:
        """代理真实 asyncio，仅拦截 sleep。"""

        def __init__(self) -> None:
            """持有真实模块。"""
            self._real = asyncio

        def __getattr__(self, name):
            """未拦截属性转发真实 asyncio。"""
            return getattr(self._real, name)

        async def sleep(self, delay, *args, **kwargs):
            """记录退避延迟并置位停止事件。"""
            sleeps.append(delay)
            monitor.stop_event.set()

    monkeypatch.setattr(monitor_module, "asyncio", _AsyncioShim())
    monkeypatch.setattr(monitor_module.random, "uniform", lambda low, high: 0.0)
    monkeypatch.setattr(
        monitor,
        "snapshot",
        lambda: {"enabled": True, "paused": False, "target_bvids": ["BV1"], "total_collected": 0},
    )

    asyncio.run(asyncio.wait_for(monitor._monitor_loop(), timeout=2))
    assert sleeps == [600]  # min(1800, 300 * 2**1)

    row = _read_state(db)
    assert row.consecutive_failures == 1
    assert row.last_error == "collect boom"
    assert row.status == "running"


def test_cookie_loop_breaks_on_stop_event(db, monkeypatch):
    """Cookie 巡检：替身校验后置位 stop_event，循环单轮退出（不挂死）。"""
    monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())

    class _Pool:
        """记录巡检次数并在首轮置位停止事件的 Cookie 池替身。"""

        def __init__(self) -> None:
            """初始化间隔与计数。"""
            self.check_interval = 30
            self.checks = 0

        async def check_all_cookies(self, session):
            """记录一次巡检并置位停止事件。"""
            self.checks += 1
            monitor.stop_event.set()

    pool = _Pool()
    monkeypatch.setattr(monitor_module, "get_cookie_pool", lambda: pool)

    asyncio.run(asyncio.wait_for(monitor._cookie_loop(), timeout=2))
    assert pool.checks == 1
    assert monitor.stop_event.is_set()
