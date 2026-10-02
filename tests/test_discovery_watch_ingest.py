"""06 批 1：discovery 候选 → watch 池桥接 · run_discovery_loop 回调 · monitor_service 第二条常驻 task。

全程离线（不真发网络、不烧配额）：
- 桥接用例用**内存 SQLite 真实建表**（含 ``hotspot_watch``），不 Mock session；
- 零 HTTP 用例注入「被调用即抛 AssertionError」的 fetch / API 替身，跑完断言零调用；
- 长循环一律 ``asyncio.run`` + ``wait_for`` 双保险，回调/替身首轮即置位 ``stop_event`` 保证单轮退出。

覆盖点（对齐 06 批 1 交付要求 1~7）：
1. 筛来源：popular 入池、ranking_all 入池、ranking_all_others 不入、search_square 不入、混合来源按口径；
2. 幂等：连调两次行数不变，且 next_due / first_seen / state_revision 未被重置；
3. 零 HTTP：入池只吃内存候选，不触发任何抓取闸门与 API 调用；
4. 空候选 → 返回 0、不炸；
5. run_discovery_loop 的 on_snapshot 每轮调一次；不传时行为不变（对照用例）；
6. monitor_service 集成：开 discovery_enable → 拉起 task → 回调真入池 → shutdown 真结束；
7. 配置默认关时 start() 不拉起 discovery task，且不装配发现服务。
"""
from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import DatabaseManager, HotspotWatch
from core.database.base import Base
from modules.hotspot.discovery.contracts import (
    BroadVideo,
    SOURCE_POPULAR,
    SOURCE_RANKING_ALL,
    SOURCE_RANKING_ALL_OTHERS,
    SOURCE_SEARCH_SQUARE,
    merge_video_candidates,
)
from modules.hotspot.discovery.service import (
    DiscoverRunConfig,
    DiscoveryPollCache,
    DiscoveryService,
    run_discovery_loop,
)
from modules.hotspot.discovery.snapshot import DiscoverySnapshotStore
from modules.hotspot.discovery.store import KeywordSignalStore, VideoSignalStore
from modules.hotspot.discovery.watch_ingest import (
    WATCH_INGEST_SOURCES,
    ingest_video_candidates_to_watch,
    is_watch_candidate,
)

# 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int）。
E: int = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())

monitor_module = importlib.import_module("core.monitor_service")
ResidentCommentMonitor = monitor_module.ResidentCommentMonitor


# ---------------------------------------------------------------------------
# 夹具与构造助手
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    """内存 SQLite 全量建表，产出独立会话（含 hotspot_watch）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _cf(bvid: str, source: str, position: int | None = 1) -> BroadVideo:
    """构造一条单来源 BroadVideo（供契约层真实合并用）。"""
    return BroadVideo(bvid=bvid, source=source, captured_epoch_s=E, position=position)


def _merged(*rows: BroadVideo):
    """复用契约层真实合并，产出 discovery 候选 dict 列表。"""
    return merge_video_candidates(rows)


def _watch_rows(session) -> list:
    """返回 hotspot_watch 全部行（按 bvid 排序，便于确定性断言）。"""
    return session.query(HotspotWatch).order_by(HotspotWatch.bvid).all()


# ---------------------------------------------------------------------------
# 1. 筛来源口径
# ---------------------------------------------------------------------------


def test_watch_ingest_sources_is_exactly_popular_and_ranking_all() -> None:
    """允许入池的来源集合恰好是 {popular, ranking_all}。"""
    assert WATCH_INGEST_SOURCES == frozenset({SOURCE_POPULAR, SOURCE_RANKING_ALL})


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ([SOURCE_POPULAR], True),
        ([SOURCE_RANKING_ALL], True),
        ([SOURCE_RANKING_ALL_OTHERS], False),
        ([SOURCE_SEARCH_SQUARE], False),
        ([SOURCE_RANKING_ALL, SOURCE_RANKING_ALL_OTHERS], True),
        ([SOURCE_POPULAR, SOURCE_SEARCH_SQUARE], True),
        ([], False),
        (None, False),
        ("popular", False),
        ({}, False),
    ],
)
def test_is_watch_candidate_matches_declared_scope(sources, expected: bool) -> None:
    """入池口径：sources 含 popular / ranking_all 才入池；others / search_square 不入池。"""
    assert is_watch_candidate({"bvid": "BV1", "sources": sources}) is expected


@pytest.mark.parametrize("candidate", [None, "BV1", 42, ["popular"]])
def test_is_watch_candidate_rejects_non_dict(candidate) -> None:
    """非候选结构（None / 字符串 / 数字 / 列表）一律判 False，不抛异常。"""
    assert is_watch_candidate(candidate) is False


def test_merged_candidates_ingest_only_popular_and_ranking_all(session) -> None:
    """混合来源按口径入池：popular / ranking_all 入池，ranking_all_others 不入池。"""
    candidates = _merged(
        _cf("BV1", SOURCE_POPULAR),
        _cf("BV2", SOURCE_RANKING_ALL),
        _cf("BV3", SOURCE_RANKING_ALL_OTHERS),
    )

    written = ingest_video_candidates_to_watch(session, candidates, now_epoch_s=E)

    assert written == 2
    rows = _watch_rows(session)
    assert [row.bvid for row in rows] == ["BV1", "BV2"]
    # discovery_source 填 display_source（合并层已按固定优先级取好展示来源）。
    assert {row.bvid: row.discovery_source for row in rows} == {
        "BV1": SOURCE_POPULAR,
        "BV2": SOURCE_RANKING_ALL,
    }


def test_multi_source_candidate_ingested_with_priority_display_source(session) -> None:
    """同一 bvid 多来源（ranking_all + others）-> 入池，discovery_source 取优先级最高的 ranking_all。"""
    candidates = _merged(
        _cf("BV4", SOURCE_RANKING_ALL_OTHERS, position=None),
        _cf("BV4", SOURCE_RANKING_ALL, position=1),
    )
    assert candidates[0]["sources"] == [SOURCE_RANKING_ALL, SOURCE_RANKING_ALL_OTHERS]

    assert ingest_video_candidates_to_watch(session, candidates, now_epoch_s=E) == 1

    row = _watch_rows(session)[0]
    assert row.bvid == "BV4"
    assert row.discovery_source == SOURCE_RANKING_ALL


def test_search_square_only_candidate_not_ingested(session) -> None:
    """只有 search_square 来源的候选（热搜词）一条都不入池。"""
    candidates = [
        {
            "bvid": "BV9",
            "sources": [SOURCE_SEARCH_SQUARE],
            "display_source": SOURCE_SEARCH_SQUARE,
        }
    ]

    assert ingest_video_candidates_to_watch(session, candidates, now_epoch_s=E) == 0
    assert _watch_rows(session) == []


# ---------------------------------------------------------------------------
# 4. 空候选 / 全不入池候选
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("empty", [None, [], ()])
def test_empty_candidates_return_zero(session, empty) -> None:
    """空候选 -> 返回 0、不抛异常。"""
    assert ingest_video_candidates_to_watch(session, empty, now_epoch_s=E) == 0
    assert _watch_rows(session) == []


def test_all_non_ingestable_candidates_return_zero(session) -> None:
    """全是 others / search_square 的候选 -> 返回 0、不抛异常。"""
    candidates = [
        {
            "bvid": "BV1",
            "sources": [SOURCE_RANKING_ALL_OTHERS],
            "display_source": SOURCE_RANKING_ALL_OTHERS,
        },
        {
            "bvid": "BV2",
            "sources": [SOURCE_SEARCH_SQUARE],
            "display_source": SOURCE_SEARCH_SQUARE,
        },
    ]

    assert ingest_video_candidates_to_watch(session, candidates, now_epoch_s=E) == 0
    assert _watch_rows(session) == []


def test_candidate_without_bvid_is_skipped_not_fatal(session) -> None:
    """缺 bvid 的脏候选被跳过并留痕，不炸整批；同批合法候选照常入池。"""
    candidates = [
        {"sources": [SOURCE_POPULAR], "display_source": SOURCE_POPULAR},
        {"bvid": "  ", "sources": [SOURCE_RANKING_ALL], "display_source": SOURCE_RANKING_ALL},
        {"bvid": "BV5", "sources": [SOURCE_POPULAR], "display_source": SOURCE_POPULAR},
    ]

    assert ingest_video_candidates_to_watch(session, candidates, now_epoch_s=E) == 1
    assert [row.bvid for row in _watch_rows(session)] == ["BV5"]


# ---------------------------------------------------------------------------
# 2. 幂等：不重置调度 / 首见 / 代际
# ---------------------------------------------------------------------------


def test_second_ingest_is_idempotent_and_keeps_schedule(session) -> None:
    """同一批候选连调两次：watch 表还是那些行，且 next_due / first_seen / state_revision 未被重置。"""
    candidates = _merged(_cf("BV1", SOURCE_POPULAR), _cf("BV2", SOURCE_RANKING_ALL))

    assert ingest_video_candidates_to_watch(session, candidates, now_epoch_s=E) == 2
    first_pass = {
        row.bvid: (
            row.first_seen_epoch_s,
            row.next_due_epoch_s,
            row.state_revision,
            row.sample_interval_s,
            row.ttl_end_epoch_s,
        )
        for row in _watch_rows(session)
    }
    # next_due 用 upsert_watch 的默认值 now + sample_interval_s（桥接层不自己算偏移）。
    assert first_pass["BV1"][1] == E + first_pass["BV1"][3]

    # 第二次入池时刻更晚（模拟下一轮发现）。
    assert ingest_video_candidates_to_watch(session, candidates, now_epoch_s=E + 100) == 2

    rows = _watch_rows(session)
    assert len(rows) == 2  # 没有新增行
    second_pass = {
        row.bvid: (
            row.first_seen_epoch_s,
            row.next_due_epoch_s,
            row.state_revision,
            row.sample_interval_s,
            row.ttl_end_epoch_s,
        )
        for row in rows
    }
    assert second_pass == first_pass  # 调度列 / 首见 / 代际 / ttl 一律未动
    assert all(row.last_seen_epoch_s == E + 100 for row in rows)  # 只更新 last_seen


def test_invalid_clock_raises_instead_of_silent_zero(session) -> None:
    """非法时钟显式报错，不被逐条容错吞成「0 条入池」。"""
    candidates = _merged(_cf("BV1", SOURCE_POPULAR))
    for bad in (None, -1, True, "123"):
        with pytest.raises(ValueError):
            ingest_video_candidates_to_watch(session, candidates, now_epoch_s=bad)
    assert _watch_rows(session) == []


# ---------------------------------------------------------------------------
# 3. 零 HTTP
# ---------------------------------------------------------------------------


class _ExplodingAPI:
    """任何 HTTP 调用即断言失败的 API 替身（用于钉死「入池零请求」）。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls = []

    async def get(self, *args, **kwargs):
        """被调用即记录并断言失败。"""
        self.calls.append("get")
        raise AssertionError("入池不得发任何 HTTP")

    async def get_ranking(self, *args, **kwargs):
        """被调用即记录并断言失败。"""
        self.calls.append("get_ranking")
        raise AssertionError("入池不得发任何 HTTP")


class _ExplodingFetch:
    """被调用即抛 AssertionError 的抓取闸门替身（记录调用次数）。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls = []

    def __call__(self, *args, **kwargs):
        """被调用即记录并断言失败。"""
        self.calls.append((args, kwargs))
        raise AssertionError("入池不得触发任何抓取闸门")


def test_ingest_sends_zero_http(session, tmp_path, monkeypatch) -> None:
    """入池只吃内存候选：抓取闸门替身与发现服务的 API 替身均零调用。"""
    import core.request_budget as request_budget_module
    import modules.hotspot.discovery.service as discovery_service_module

    gate = _ExplodingFetch()
    monkeypatch.setattr(request_budget_module, "before_http_attempt", gate)
    monkeypatch.setattr(discovery_service_module, "before_http_attempt", gate)

    manager = DatabaseManager(str(tmp_path / "discovery.db"))
    api = _ExplodingAPI()
    service = DiscoveryService(
        api,
        cache=DiscoveryPollCache(),
        snapshot_store=DiscoverySnapshotStore(tmp_path / "snapshot.json"),
        keyword_store=KeywordSignalStore(manager.get_session),
        video_store=VideoSignalStore(manager.get_session),
        config=DiscoverRunConfig(),
        clock=lambda: E,
    )

    async def _boom(*args, **kwargs):
        """poll 被调用即断言失败（入池不该触发轮询）。"""
        raise AssertionError("入池不得触发 poll_once")

    monkeypatch.setattr(service, "poll_once", _boom)
    # 把「上一轮 poll 的内存结果」直接注入：入池只该读内存，不再打入口。
    service._last_videos = _merged(
        _cf("BV1", SOURCE_POPULAR),
        _cf("BV2", SOURCE_RANKING_ALL),
    )

    written = ingest_video_candidates_to_watch(
        session, service.iter_video_candidates(), now_epoch_s=E
    )

    assert written == 2
    assert gate.calls == []  # 抓取闸门零调用
    assert api.calls == []  # API 零调用
    assert [row.bvid for row in _watch_rows(session)] == ["BV1", "BV2"]


# ---------------------------------------------------------------------------
# 5. run_discovery_loop 的 on_snapshot 回调
# ---------------------------------------------------------------------------


class _FakeLoopService:
    """run_discovery_loop 的离线服务替身：poll 立即返回固定快照。"""

    def __init__(self) -> None:
        """初始化轮次计数与固定快照。"""
        self.poll_calls = 0
        self.snapshot = {
            "snapshot_id": "s1",
            "served_from_cache": False,
            "keyword_count": 0,
            "video_count": 0,
        }

    async def poll_once(self):
        """记录并返回固定快照（不发网络）。"""
        self.poll_calls += 1
        return dict(self.snapshot)


def test_on_snapshot_is_awaited_and_called_once_per_round() -> None:
    """async 回调：每轮 poll 成功后调一次，并把服务实例传进去。"""
    service = _FakeLoopService()
    stop_event = asyncio.Event()
    seen = []

    async def _on_snapshot(passed):
        """记录入参并置位停止事件（保证单轮退出）。"""
        seen.append(passed)
        stop_event.set()

    asyncio.run(
        asyncio.wait_for(
            run_discovery_loop(
                service, stop_event=stop_event, interval_s=1, on_snapshot=_on_snapshot
            ),
            timeout=5,
        )
    )

    assert service.poll_calls == 1
    assert seen == [service]


def test_sync_on_snapshot_is_called_directly() -> None:
    """sync 回调：直接调用（不被强行 await）。"""
    service = _FakeLoopService()
    stop_event = asyncio.Event()
    seen = []

    def _on_snapshot(passed):
        """记录入参并置位停止事件。"""
        seen.append(passed)
        stop_event.set()

    asyncio.run(
        asyncio.wait_for(
            run_discovery_loop(
                service, stop_event=stop_event, interval_s=1, on_snapshot=_on_snapshot
            ),
            timeout=5,
        )
    )

    assert seen == [service]


def test_without_on_snapshot_behavior_unchanged() -> None:
    """不传 on_snapshot（对照用例）：行为与既有完全一致 —— 单轮 poll 后按期退出。"""

    async def _scenario() -> int:
        """跑一轮无回调循环，返回 poll 次数。"""
        service = _FakeLoopService()
        stop_event = asyncio.Event()

        async def _watcher():
            """等首轮 poll 完成再置位停止事件，保证只跑一轮。"""
            while service.poll_calls == 0:
                await asyncio.sleep(0)
            stop_event.set()

        watching = asyncio.create_task(_watcher())
        await asyncio.wait_for(
            run_discovery_loop(service, stop_event=stop_event, interval_s=1), timeout=5
        )
        await watching
        return service.poll_calls

    assert asyncio.run(_scenario()) == 1


def test_on_snapshot_error_does_not_kill_loop() -> None:
    """回调抛异常走既有退避策略：循环不退出，下一轮 poll 继续（不静默吞、也不打死循环）。"""
    service = _FakeLoopService()
    stop_event = asyncio.Event()
    calls = []

    async def _on_snapshot(passed):
        """首轮抛异常，次轮置位停止事件。"""
        calls.append(passed)
        if len(calls) == 1:
            raise RuntimeError("ingest boom")
        stop_event.set()

    asyncio.run(
        asyncio.wait_for(
            run_discovery_loop(
                service,
                stop_event=stop_event,
                interval_s=1,
                backoff_base_s=1,
                backoff_max_s=1,
                on_snapshot=_on_snapshot,
            ),
            timeout=10,
        )
    )

    assert service.poll_calls == 2
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 6 / 7. monitor_service 集成
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


class _OfflineDiscoveryService:
    """离线发现服务替身：poll 立即返回；候选来自内存（不发网络）。"""

    def __init__(self, candidates) -> None:
        """保存候选并初始化轮次计数。"""
        self._candidates = list(candidates)
        self.poll_calls = 0

    async def poll_once(self):
        """记录一轮并返回固定快照。"""
        self.poll_calls += 1
        return {
            "snapshot_id": "s1",
            "served_from_cache": False,
            "keyword_count": 0,
            "video_count": len(self._candidates),
        }

    def iter_video_candidates(self):
        """返回内存候选（入池唯一数据源）。"""
        return list(self._candidates)


@pytest.fixture(autouse=True)
def quiet_logger(monkeypatch):
    """把 monitor 模块 logger 换成记录型替身（与本项目其余 monitor 用例同款）。"""
    fake = _RecordingLogger()
    monkeypatch.setattr(monitor_module, "logger", fake)
    return fake


@pytest.fixture()
def monitor_db(tmp_path, monkeypatch):
    """把 monitor 模块 get_session 指向 tmp_path 下的真实 SQLite（全量建表）。"""
    manager = DatabaseManager(str(tmp_path / "monitor.db"))
    monkeypatch.setattr(monitor_module, "get_session", manager.get_session)
    return manager


def _db_watch_rows(manager) -> list:
    """从真实临时库读取 hotspot_watch 全部行（按 bvid 排序）。"""
    session = manager.get_session()
    try:
        return session.query(HotspotWatch).order_by(HotspotWatch.bvid).all()
    finally:
        session.close()


def test_discovery_enable_ingests_candidates_into_watch_pool(monitor_db, monkeypatch) -> None:
    """开 discovery_enable → 拉起 task → on_snapshot 真把候选喂进 watch 表 → shutdown 后 task 真结束。"""
    candidates = _merged(
        _cf("BV1", SOURCE_POPULAR),
        _cf("BV2", SOURCE_RANKING_ALL),
        _cf("BV3", SOURCE_RANKING_ALL_OTHERS),
    )
    fake = _OfflineDiscoveryService(candidates)
    observed: dict = {}

    async def _scenario():
        """拉起 discovery task、等入池落地、再 shutdown。"""
        monitor = ResidentCommentMonitor(
            lambda: None,
            config=_FakeConfig(
                {"monitor.discovery_enable": True, "monitor.discovery_interval": 1}
            ),
            discovery_service_factory=lambda: fake,
        )

        async def _noop():
            """立即返回的占位协程（隔离 cookie 循环，避免真巡检）。"""
            return None

        monkeypatch.setattr(monitor, "_cookie_loop", _noop)
        await monitor.start()

        # 轮询等待入池落地（回调不发网络，落地极快）。
        for _ in range(500):
            if _db_watch_rows(monitor_db):
                break
            await asyncio.sleep(0.01)

        observed["running"] = monitor.snapshot()["discovery_task_running"]
        observed["task"] = monitor.discovery_task
        await monitor.shutdown()

    asyncio.run(asyncio.wait_for(_scenario(), timeout=10))

    assert observed["running"] is True
    assert observed["task"].get_name() == "bili-discovery"
    assert observed["task"].done() is True  # shutdown 后真结束

    assert fake.poll_calls >= 1
    rows = _db_watch_rows(monitor_db)
    assert [row.bvid for row in rows] == ["BV1", "BV2"]  # others 不入池
    assert {row.discovery_source for row in rows} == {SOURCE_POPULAR, SOURCE_RANKING_ALL}


def test_shutdown_cancels_discovery_task(monitor_db, monkeypatch) -> None:
    """shutdown 将 discovery task 纳入统一 cancel + await：置 stop_event 且任务以取消收尾。"""

    async def _scenario():
        """拉起阻塞 discovery task 后 shutdown。"""
        monitor = ResidentCommentMonitor(
            lambda: None, config=_FakeConfig({"monitor.discovery_enable": True})
        )
        entered = asyncio.Event()

        async def _blocking_discovery():
            """阻塞等待取消。"""
            entered.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(monitor, "_discovery_loop", _blocking_discovery)
        monitor._ensure_discovery_task()  # 不经 start，隔离 discovery 启停本身
        await entered.wait()
        await monitor.shutdown()
        return monitor.stop_event.is_set(), monitor.discovery_task

    is_set, task = asyncio.run(asyncio.wait_for(_scenario(), timeout=10))
    assert is_set is True
    assert task.done() is True
    assert task.cancelled() is True  # CancelledError 未被吞


def test_start_reuses_alive_discovery_task(monitor_db, monkeypatch) -> None:
    """重复 start 两次只保留一条 discovery task（单实例守卫）。"""

    async def _scenario():
        """连续两次 start，比较 discovery task 是否同一对象。"""
        monitor = ResidentCommentMonitor(
            lambda: None, config=_FakeConfig({"monitor.discovery_enable": True})
        )
        entered = asyncio.Event()

        async def _blocking_discovery():
            """阻塞等待取消。"""
            entered.set()
            await asyncio.sleep(3600)

        async def _noop():
            """立即返回的占位协程。"""
            return None

        monkeypatch.setattr(monitor, "_discovery_loop", _blocking_discovery)
        monkeypatch.setattr(monitor, "_cookie_loop", _noop)
        await monitor.start()
        await entered.wait()
        first = monitor.discovery_task
        await monitor.start()  # 已有存活 discovery task → 应复用
        second = monitor.discovery_task
        await monitor.shutdown()
        return first is second, first

    same, task = asyncio.run(asyncio.wait_for(_scenario(), timeout=10))
    assert same is True
    assert task.get_name() == "bili-discovery"
    assert task.cancelled() is True


def test_start_does_not_create_discovery_task_by_default(monitor_db, monkeypatch) -> None:
    """默认配置（未配 monitor.discovery_enable）时 start 不拉起 discovery 常驻循环。"""
    observed: dict = {}

    async def _scenario():
        """执行 start 并等待 Cookie 替身。"""
        monitor = ResidentCommentMonitor(lambda: None, config=_FakeConfig())

        async def _noop():
            """立即返回的占位协程。"""
            return None

        monkeypatch.setattr(monitor, "_cookie_loop", _noop)
        await monitor.start()
        if monitor.cookie_task:
            await monitor.cookie_task
        observed["monitor"] = monitor

    asyncio.run(asyncio.wait_for(_scenario(), timeout=10))

    monitor = observed["monitor"]
    assert monitor.discovery_task is None
    assert monitor.snapshot()["discovery_task_running"] is False


def test_default_config_never_builds_discovery_service(monitor_db, monkeypatch) -> None:
    """默认配置下 start()/shutdown() 都不装配发现服务：无任何路径拉起 discovery 循环。"""
    calls = []

    def _factory():
        """若被调用即记录并失败（惰性装配的反向断言）。"""
        calls.append(1)
        raise AssertionError("默认配置下不应构造发现服务")

    async def _scenario():
        """走完 start + shutdown 全流程。"""
        monitor = ResidentCommentMonitor(
            lambda: None, config=_FakeConfig(), discovery_service_factory=_factory
        )

        async def _noop():
            """立即返回的占位协程。"""
            return None

        monkeypatch.setattr(monitor, "_cookie_loop", _noop)
        monkeypatch.setattr(monitor, "_monitor_loop", _noop)
        await monitor.start()
        if monitor.cookie_task:
            await monitor.cookie_task
        await monitor.shutdown()
        return monitor

    monitor = asyncio.run(asyncio.wait_for(_scenario(), timeout=10))
    assert calls == []  # 工厂从未被调用 → 未构造发现服务
    assert monitor.discovery_task is None
    assert monitor.snapshot()["discovery_task_running"] is False
