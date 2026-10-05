"""``watch`` 预算集成用例：真实组装链路下的单请求准入全链路（07 执行案 W1）。

W0 时本文件把 07 证据案的三条反例（单目标双扣 / 二次等待 / 预占整分钟）原样固化为
``xfail(strict=True)``。W1 落地「单请求准入协议」后，这三条反例**已 XPASS**——故把断言
改写为**修复后的正确行为**并转为普通断言（不再 strict）。

与 ``test_watch_logical_admission.py``（纯预算器 + 选择器）互补：这里走的是
07 执行案 §11.2 描述的**生产组装集成**路径，除网络与持久化外不再打桩：

- 真 ``RequestBudget``；
- 真 ``WatchService``（选择器 ``_select_targets`` 跑真代码；W1 起只做非消费 ``peek``）；
- 真 ``HotspotCollectorPort``（跑真 ``collect_one`` / ``collect_admitted``）；
- 真 ``HotspotCollector``（跑真 ``collect_one`` / ``_fetch_view``，凭证由 ``redeem`` 兑换）。

只替身两处，且都是 07 案明确许可的「外部副作用」：
- ``BilibiliAPI`` → :class:`_FakeAPI`（不发真请求、不烧配额）；
- ``HotspotCollector._save_snapshot`` → 返回固定条数（不落真库）。

选择器的 DB 读取口（``find_due_for_eval`` / ``load_fast_until_map``）在多数用例里仍用内存
替身；「真实工厂」用例则走真临时 SQLite 与真 DB 读取口。

注：仓库未装 ``pytest-asyncio``，异步场景统一 ``asyncio.run`` 驱动。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, Video, VideoStats
from core.monitor_service import build_watch_service
from modules.hotspot import watch_service as ws
from modules.hotspot.collector import HotspotCollector
from modules.hotspot.risk_control import RequestBudget
from modules.hotspot.watch_service import (
    BUDGET_CATEGORY_FAST,
    BUDGET_CATEGORY_NORMAL,
    WATCH_SOURCE,
    HotspotCollectorPort,
    WatchService,
)
from modules.hotspot.watch_store import upsert_watch

NOW_EPOCH_S: int = 1_787_616_000  # 2026-09-10T00:00:00Z（与 watch 系列用例同口径）

#: 统一的单调时钟读数。
#:
#: 这里刻意取真实 ``time.monotonic()``：``RequestBudget.acquire()`` 内部默认时钟就是
#: ``time.monotonic``，而本文件测的恰恰是「选择器准入」与「采集器 acquire」两处记账是否
#: 落在同一本账上。用同一个真实时钟基准，两次记账才会真正落在同一条账上。
NOW_MONO: float = time.monotonic()
TID: int = 1008

#: 第三道闸门最多等多久：真实现一旦走进「等下一分钟窗口」，睡的是接近 60s；
#: 反例 2/3 只要在 30ms 内没返回就足以证明卡住了。修复后压根不该再排队。
SECOND_GATE_TIMEOUT_S: float = 0.03


class _Row:
    """``hotspot_watch`` 行的最小替身（见前一个文件的同名类）。"""

    def __init__(self, bvid: str, *, collection_tid: int | None = TID) -> None:
        """构造一行替身。

        Args:
            bvid: 视频 BV 号。
            collection_tid: 归属采集分区 ID。
        """
        self.bvid = bvid
        self.collection_tid = collection_tid
        self.sample_interval_s = None
        self.state_json = None


class _TrapSession:
    """会话替身：真被拿去查库就炸，证明 DB 口确实打桩了。"""

    def __getattr__(self, name: str):  # pragma: no cover - 命中即测试假定失效
        """任何属性访问都视为走到了不该走的真实 DB 路径。"""
        raise AssertionError(
            f"测试替身失效：组装链路不应触碰真实 session 的 {name!r}"
        )


class _FakeAPI:
    """``BilibiliAPI`` 替身：只接住 ``_fetch_view`` 那一次详情请求。"""

    BASE_URL: str = "https://example.invalid"

    def __init__(self) -> None:
        """初始化并记录收到的请求。"""
        self.calls: list[tuple[str, str | None]] = []

    async def get(self, url: str, params=None, need_sign: bool = False, **kwargs):
        """返回一份最小可用的详情响应（含 ``stat.view``）。

        Args:
            url: 请求地址；本替身只记录不解析。
            params: 查询参数，须含 ``bvid``。
            need_sign: 真实现会据此签名；替身忽略。
            **kwargs: 其余真实现参数；替身忽略。

        Returns:
            dict: 形如 ``{"bvid": ..., "stat": {"view": 123}}``。
        """
        bvid = (params or {}).get("bvid")
        self.calls.append((url, bvid))
        return {"bvid": bvid, "stat": {"view": 123}}


@pytest.fixture()
def fake_watch_db(monkeypatch):
    """把 ``watch_service`` 的 DB 读取口换成内存替身。

    W2 起选择器改为**按类查询**（``find_due_for_budget_category``），故补一个按类过滤的替身；
    旧的 ``find_due_for_eval`` / ``load_fast_until_map`` 仍打桩（无预算 legacy 路径仍走前者）。

    Returns:
        tuple: ``(rows, fast_until)``，用例先塞数据再跑。
    """
    rows: list[_Row] = []
    fast_until: dict[str, int] = {}

    def _fake_find_due_for_eval(session, now_epoch_s, limit):
        """按 limit 截断返回内存行。"""
        return list(rows[: max(1, int(limit))])

    def _fake_load_fast_until_map(session, bvids):
        """只回映射里有的 bvid。"""
        return {bvid: fast_until[bvid] for bvid in bvids if bvid in fast_until}

    def _fake_find_due_for_budget_category(session, now_epoch_s, *, category, limit=1, cursor=None):
        """W2 新读取口：按 ``fast_until`` 分类 + limit 截断（真实现即「按类 LIMIT」）。"""
        def _category_of(row):
            fast = fast_until.get(row.bvid)
            return (
                BUDGET_CATEGORY_FAST
                if (type(fast) is int and fast > now_epoch_s)
                else BUDGET_CATEGORY_NORMAL
            )

        picked = [row for row in rows if _category_of(row) == category]
        return picked[: max(1, int(limit))]

    monkeypatch.setattr(ws, "find_due_for_eval", _fake_find_due_for_eval)
    monkeypatch.setattr(ws, "load_fast_until_map", _fake_load_fast_until_map)
    monkeypatch.setattr(
        ws, "find_due_for_budget_category", _fake_find_due_for_budget_category
    )
    return rows, fast_until


def _build_stack(per_minute: int, monkeypatch):
    """按 §11.2 组装生产链路（只替身网络与落库）。

    Args:
        per_minute: ``RequestBudget`` 的每分钟上限。
        monkeypatch: pytest 的 monkeypatch，用于替身 ``_save_snapshot``。

    Returns:
        tuple: ``(service, port, collector, budget, api)``。
    """
    budget = RequestBudget(per_minute=per_minute, per_hour=300, per_day=3000)
    api = _FakeAPI()
    collector = HotspotCollector(api=api, budget=budget)

    async def _fake_save_snapshot(*args, **kwargs):
        """替身落库：只回条数，不写任何表。"""
        return 1

    monkeypatch.setattr(collector, "_save_snapshot", _fake_save_snapshot)
    port = HotspotCollectorPort(collector)
    service = WatchService(
        collector_port=port,
        session_factory=_TrapSession,
        now_mono_fn=lambda: NOW_MONO,
        budget=budget,
    )
    return service, port, collector, budget, api


# ===========================================================================
# 三条反例 → 修复后的正确行为（普通断言）
# ===========================================================================


def test_single_target_is_charged_once_not_twice(fake_watch_db, monkeypatch) -> None:
    """反例 1（已修复）：一个目标的成功采集，总消费必须是 **1**，而不是 2。

    链路：选择器 ``peek``（只读，不占）→ ``port.collect`` → ``collect_one`` →
    ``_fetch_view`` 内 ``acquire`` 扣 1。修复后再无「准入 + 请求」两次记账。
    """
    rows, _fast_until = fake_watch_db
    rows.append(_Row("BVSINGLE001"))
    service, port, collector, budget, api = _build_stack(per_minute=20, monkeypatch=monkeypatch)

    selection = service._select_targets(
        session=_TrapSession(),
        now_epoch_s=NOW_EPOCH_S,
        limit=1,
        budget=budget,
        now_mono=NOW_MONO,
    )
    assert len(selection.targets) == 1
    # 修复后：选择器只做非消费视图，准入阶段不占任何预算。
    assert len(budget._requests) == 0, "选择器只 peek 不记账，不应预先扣费"

    asyncio.run(port.collect(selection.targets[0].bvid, collection_tid=TID, source=WATCH_SOURCE))

    # 真发生了 HTTP（只发了一次，说明不是重试造成的重复）。
    assert len(api.calls) == 1
    # 修复后的正确行为：一次请求 = 一份预算，不再出现两次记账。
    assert len(budget._requests) == 1, (
        f"一次成功采集不应记两次账；当前消费明细={dict(budget.category_consumption)}"
    )
    assert collector.budget is budget


def test_selected_target_does_not_wait_at_second_gate(fake_watch_db, monkeypatch) -> None:
    """反例 2（已修复）：``per_minute=1`` 时，已被选中的目标不应卡在第二道闸门。"""
    rows, _ = fake_watch_db
    rows.append(_Row("BVWAIT0001"))
    service, port, _, budget, api = _build_stack(per_minute=1, monkeypatch=monkeypatch)

    selection = service._select_targets(
        session=_TrapSession(),
        now_epoch_s=NOW_EPOCH_S,
        limit=1,
        budget=budget,
        now_mono=NOW_MONO,
    )
    assert [t.bvid for t in selection.targets] == ["BVWAIT0001"]
    assert len(budget._requests) == 0  # 选择器未预占唯一名额

    timed_out = False
    try:
        asyncio.run(
            asyncio.wait_for(
                port.collect("BVWAIT0001", collection_tid=TID, source=WATCH_SOURCE),
                timeout=SECOND_GATE_TIMEOUT_S,
            )
        )
    except asyncio.TimeoutError:
        timed_out = True

    # 修复后：不该超时，且该发出的那一次 HTTP 已经发出。
    assert timed_out is False, "已获准入的目标不应在第二道闸门干等下一分钟窗口"
    assert len(api.calls) == 1
    assert len(budget._requests) == 1


def test_default_minute_capacity_is_not_preconsumed_before_first_http(
    fake_watch_db, monkeypatch
) -> None:
    """反例 3（已修复）：``per_minute=20`` 选 20 条后，第一条仍能立刻发出去。"""
    rows, _ = fake_watch_db
    bvids = [f"BVCAP{i:05d}" for i in range(20)]
    rows.extend([_Row(bvid) for bvid in bvids])
    service, port, _, budget, api = _build_stack(per_minute=20, monkeypatch=monkeypatch)

    selection = service._select_targets(
        session=_TrapSession(),
        now_epoch_s=NOW_EPOCH_S,
        limit=20,
        budget=budget,
        now_mono=NOW_MONO,
    )
    assert len(selection.targets) == 20
    # 修复后：选 20 条不再等于预占整分钟额度。
    assert len(budget._requests) == 0

    timed_out = False
    try:
        asyncio.run(
            asyncio.wait_for(
                port.collect(bvids[0], collection_tid=TID, source=WATCH_SOURCE),
                timeout=SECOND_GATE_TIMEOUT_S,
            )
        )
    except asyncio.TimeoutError:
        timed_out = True

    # 修复后：第一条立刻发得出去，且 HTTP 真的发了。
    assert timed_out is False, "整分钟额度不该在首个 HTTP 之前被预占干净"
    assert len(api.calls) == 1
    assert len(budget._requests) == 1


# ===========================================================================
# 真实工厂 / 原方法集成（§11.2 必须覆盖）
# ===========================================================================


def _aid_for(bvid: str) -> int:
    """由 bvid 数字段派生的稳定 aid（``videos.aid`` 有唯一约束，避免撞号）。"""
    digits = "".join(char for char in bvid if char.isdigit())
    return int(digits) if digits else 1


def _make_real_db(tmp_path):
    """建一个真临时文件 SQLite，含全部 ORM 表。

    Returns:
        tuple: ``(session_factory, engine)``。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'watch_budget.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False), engine


def _seed_watch_row(factory, bvid: str, *, interval_s: int = 3600) -> None:
    """插入一行「已到点、未到期、在池」的 ``hotspot_watch``。"""
    session = factory()
    try:
        upsert_watch(
            session,
            bvid=bvid,
            now_epoch_s=NOW_EPOCH_S,
            ttl_end_epoch_s=NOW_EPOCH_S + 3600,
            next_due_epoch_s=NOW_EPOCH_S - 1,
            sample_interval_s=interval_s,
            collection_tid=TID,
        )
        session.commit()
    finally:
        session.close()


def _seed_snapshots(factory, bvid: str, points) -> None:
    """写入 ``videos`` / ``video_stats`` 行（质量 ok），供算法评估用。"""
    session = factory()
    try:
        video = session.query(Video).filter(Video.bvid == bvid).first()
        if video is None:
            video = Video(bvid=bvid, aid=_aid_for(bvid), tid=TID, title="测试视频", mid=9527, author="UP主")
            session.add(video)
            session.flush()
        for epoch_s, view in points:
            session.add(
                VideoStats(
                    video_id=video.id,
                    view=view,
                    snapshot_time=datetime.fromtimestamp(epoch_s),
                    source=WATCH_SOURCE,
                    captured_epoch_s=epoch_s,
                    collection_tid=TID,
                    raw_tid=TID,
                    view_status="ok",
                    stat_status="ok",
                    metric_status={"view": "ok"},
                )
            )
        session.commit()
    finally:
        session.close()


def test_real_factory_admission_chain_charges_once(tmp_path, monkeypatch) -> None:
    """真实 ``build_watch_service`` → ``collect_admitted`` → ``redeem`` 全链路单扣。

    真工厂 + 真 ``WatchService`` + 真 ``HotspotCollectorPort`` + 真 ``HotspotCollector``；
    只替身网络（:class:`_FakeAPI`）与落库（``_save_snapshot``）。断言：一次成功 tick 的
    逻辑消费恰好一份 committed，legacy ``_requests`` 为空（没走 acquire，故无第二条）。
    """
    factory, engine = _make_real_db(tmp_path)
    try:
        bvid = "BVFACTORY01"
        _seed_watch_row(factory, bvid)
        _seed_snapshots(
            factory,
            bvid,
            [
                (NOW_EPOCH_S - 3 * 86400, 1000),
                (NOW_EPOCH_S - 2 * 86400, 1200),
                (NOW_EPOCH_S - 86400, 1500),
                (NOW_EPOCH_S, 2000),
            ],
        )

        api = _FakeAPI()
        # clock 固定为 NOW_MONO：与 now_mono_fn 同源，保证 peek / reserve / redeem 同一时钟。
        budget = RequestBudget(
            per_minute=20, per_hour=300, per_day=3000, clock=lambda: NOW_MONO
        )
        collector = HotspotCollector(api=api, budget=budget)

        async def _fake_save_snapshot(*args, **kwargs):
            """替身落库：只回条数。"""
            return 1

        monkeypatch.setattr(collector, "_save_snapshot", _fake_save_snapshot)
        port = HotspotCollectorPort(collector)

        service = build_watch_service(
            collector_port=port,
            session_factory=factory,
            now_fn=lambda: NOW_EPOCH_S,
            now_mono_fn=lambda: NOW_MONO,
            budget=budget,
            demand_reconcile_hook=None,
            startup_reconcile=False,
        )

        result = asyncio.run(service.run_tick(limit=1))

        assert result.committed == 1
        assert len(api.calls) == 1
        snapshot = budget.snapshot(NOW_MONO)
        # L 层：恰好一条 committed，没有第二条总量消费。
        assert snapshot["committed"] == 1
        assert snapshot["reserved"] == 0
        assert snapshot["global_used_60s"] == 1
        assert len(budget._requests) == 0, "新准入路径不应再追加 legacy 队列"
    finally:
        engine.dispose()


def test_single_view_two_http_attempts_keep_layers_distinct(fake_watch_db, monkeypatch) -> None:
    """一次 view 的底层两次 HTTP 重试：L 层记 1 次逻辑操作、H 层是 2 次真实尝试。

    层间区别（07 案 §2）：L 是逻辑预算（``RequestBudget``），H 是 HTTP 尝试配额
    （``BilibiliAPICore._before_attempt`` / ``HttpQuotaBucket``）。一次逻辑操作可能对应
    多次真实 HTTP（重试、初始化指纹、签名），所以这里用「逻辑 = 1、真实尝试 = 2」钉住区别。

    真实 H 账本/风控由 ``test_quota_*`` 回归覆盖；本用例只证明修复后的准入票据**不**会
    把一次 view 记成两条逻辑消费，也不免除底层重试的真实计数语义。
    """
    rows, _ = fake_watch_db
    rows.append(_Row("BVRETRY001"))

    budget = RequestBudget(
        per_minute=20, per_hour=300, per_day=3000, clock=lambda: NOW_MONO
    )
    api = _FakeAPI()
    attempts = {"n": 0}

    async def _get_with_retry(url, params=None, need_sign=False, **kwargs):
        """模拟底层一次重试：两次真实 HTTP 尝试后才成功返回。"""
        attempts["n"] += 2
        bvid = (params or {}).get("bvid")
        return {"bvid": bvid, "stat": {"view": 123}}

    api.get = _get_with_retry
    collector = HotspotCollector(api=api, budget=budget)

    async def _fake_save_snapshot(*args, **kwargs):
        """替身落库：只回条数。"""
        return 1

    monkeypatch.setattr(collector, "_save_snapshot", _fake_save_snapshot)
    port = HotspotCollectorPort(collector)

    admission = budget.reserve(
        BUDGET_CATEGORY_NORMAL, NOW_MONO, operation_key="BVRETRY001"
    )
    assert admission.decision.granted is True
    asyncio.run(
        port.collect_admitted(
            "BVRETRY001",
            admission=admission.admission,
            collection_tid=TID,
            source=WATCH_SOURCE,
        )
    )

    assert attempts["n"] == 2, "H 层：一次 view 实际发了 2 次 HTTP 尝试"
    snapshot = budget.snapshot(NOW_MONO)
    assert snapshot["committed"] == 1, "L 层：只记 1 次逻辑操作（不多不少）"
    assert len(budget._requests) == 0


# ===========================================================================
# 对照组（修复前后都成立）
# ===========================================================================


def test_independent_collection_still_charges_exactly_once(fake_watch_db, monkeypatch) -> None:
    """对照组：不经选择器、直接采一条，仍应恰好扣 1 份（独立采集默认扣费保留）。"""
    rows, _ = fake_watch_db
    service, port, _, budget, api = _build_stack(per_minute=20, monkeypatch=monkeypatch)
    assert service.budget is budget

    asyncio.run(port.collect("BVINDEP001", collection_tid=TID, source=WATCH_SOURCE))

    assert len(api.calls) == 1
    assert len(budget._requests) == 1


def test_selector_without_budget_skips_the_gate_entirely(fake_watch_db) -> None:
    """对照组：``budget=None`` 时不做预算门，定额返回（兼容语义不变）。"""
    rows, _ = fake_watch_db
    rows.extend([_Row("BVNOBG0001"), _Row("BVNOBG0002")])

    service = WatchService(session_factory=_TrapSession, now_mono_fn=lambda: NOW_MONO)
    selection = service._select_targets(
        session=_TrapSession(),
        now_epoch_s=NOW_EPOCH_S,
        limit=2,
        budget=None,
    )

    assert [t.bvid for t in selection.targets] == ["BVNOBG0001", "BVNOBG0002"]
    assert selection.budget_skipped == 0


# ===========================================================================
# W3 · 真实工厂接线 / 单实例 / 预算等待移出事务 / 容量延期（07 执行案 §9.3 §10.2 §13-W3）
# ===========================================================================


def test_real_factory_wires_single_budget_instance_and_kwargs_priority(monkeypatch) -> None:
    """§10.2：真实工厂只造**一个**预算实例并沿链路复用；显式注入 budget 时绝不读配置。"""
    captured: list = []
    monkeypatch.setattr(
        ws, "default_collector_port", lambda budget=None: captured.append(budget) or object()
    )

    service = build_watch_service(startup_reconcile=False, demand_reconcile_hook=None)
    budget = service.budget
    assert isinstance(budget, RequestBudget)
    # 策略已接线（config/budget.yaml 的 watch_scheduler 段为 partitioned）。
    assert budget.category_limits is not None

    # 惰性构造采集端口时把**同一**实例传下去：整链路只存在一个预算实例（不按 target 复制）。
    _ = service.collector_port
    assert captured == [budget]

    # 显式注入 budget：kwargs 优先，绝不加载真实配置，也不再造第二个预算。
    from modules.hotspot import risk_control as rc

    def _boom(*_a, **_k):
        raise AssertionError("显式注入 budget 时不应读真实配置")

    monkeypatch.setattr(rc, "load_watch_scheduler_policy", _boom)
    injected = RequestBudget(per_minute=7, per_hour=70, per_day=700)
    service2 = build_watch_service(
        budget=injected, startup_reconcile=False, demand_reconcile_hook=None
    )
    assert service2.budget is injected


def test_real_factory_logical_observation_reports_partitioned() -> None:
    """§10.3：逻辑观测快照如实报告策略版本 / 模式 / 类别隔离。"""
    service = build_watch_service(startup_reconcile=False, demand_reconcile_hook=None)
    snapshot = service.logical_policy_snapshot()
    assert snapshot["logical_mode"] == "partitioned"
    assert snapshot["category_isolation"] is True
    assert snapshot["logical_policy_version"]
    assert snapshot["http_parent_category"] == "watch"


class _TrackingSessionFactory:
    """包真 session 工厂：追踪「当前打开会话数」，用于证明采集不在事务里。"""

    def __init__(self, base) -> None:
        """绑定被包装的真工厂。"""
        self._base = base
        self.open_count = 0

    def __call__(self):
        """开一条真 session，并把 ``close`` 挂钩到计数。"""
        session = self._base()
        self.open_count += 1
        original_close = session.close

        def _tracked_close():
            """关闭时先减计数再走原 close。"""
            self.open_count -= 1
            original_close()

        session.close = _tracked_close
        return session


def test_real_factory_no_db_session_between_reserve_and_redeem(tmp_path, monkeypatch) -> None:
    """§9.3：reserve 与 redeem（采集）之间不夹任何 DB session / 事务。

    用「追踪当前打开会话数」的工厂包住真临时 SQLite：在 ``api.get``（redeem 刚发生后）读一次
    打开计数，必须为 0 —— 证明预算等待与凭证兑换都在事务外完成。
    """
    factory, engine = _make_real_db(tmp_path)
    try:
        bvid = "BVNOSESS001"
        _seed_watch_row(factory, bvid)

        tracking = _TrackingSessionFactory(factory)
        observed = {"open": None}
        api = _FakeAPI()

        async def _get(url, params=None, need_sign=False, **kwargs):
            """替身详情请求：记录此刻仍打开的 DB session 数，并记账调用。"""
            observed["open"] = tracking.open_count
            api.calls.append((url, (params or {}).get("bvid")))
            return {"bvid": (params or {}).get("bvid"), "stat": {"view": 123}}

        api.get = _get
        budget = RequestBudget(per_minute=20, per_hour=300, per_day=3000, clock=lambda: NOW_MONO)
        collector = HotspotCollector(api=api, budget=budget)

        async def _fake_save_snapshot(*_a, **_k):
            """替身落库：只回条数。"""
            return 1

        monkeypatch.setattr(collector, "_save_snapshot", _fake_save_snapshot)
        service = WatchService(
            collector_port=HotspotCollectorPort(collector),
            session_factory=tracking,
            now_fn=lambda: NOW_EPOCH_S,
            now_mono_fn=lambda: NOW_MONO,
            budget=budget,
        )

        result = asyncio.run(service.run_tick(limit=1))

        assert result.committed == 1
        assert len(api.calls) == 1
        assert observed["open"] == 0, "reserve→redeem 之间不得开着 DB session"
        assert tracking.open_count == 0
    finally:
        engine.dispose()


def test_real_factory_twenty_due_first_target_not_blocked(tmp_path, monkeypatch) -> None:
    """§9.3：per_minute=20、20 个 due；逐目标 reserve（非预占整批），首个目标不被阻塞。"""
    factory, engine = _make_real_db(tmp_path)
    try:
        for index in range(20):
            _seed_watch_row(factory, f"BVPERC{index:05d}")

        api = _FakeAPI()
        budget = RequestBudget(per_minute=20, per_hour=300, per_day=3000, clock=lambda: NOW_MONO)
        collector = HotspotCollector(api=api, budget=budget)

        async def _fake_save_snapshot(*_a, **_k):
            """替身落库：只回条数。"""
            return 1

        monkeypatch.setattr(collector, "_save_snapshot", _fake_save_snapshot)
        service = WatchService(
            collector_port=HotspotCollectorPort(collector),
            session_factory=factory,
            now_fn=lambda: NOW_EPOCH_S,
            now_mono_fn=lambda: NOW_MONO,
            budget=budget,
        )

        started = time.perf_counter()
        result = asyncio.run(asyncio.wait_for(service.run_tick(limit=20), timeout=5.0))
        elapsed = time.perf_counter() - started

        assert result.admitted == 20 and result.committed == 20
        assert len(api.calls) == 20
        snapshot = budget.snapshot(NOW_MONO)
        assert snapshot["committed"] == 20 and snapshot["reserved"] == 0
        assert len(budget._requests) == 0
        assert elapsed < 2.0, f"首个目标不应因整批预选而在 HTTP 前等待: {elapsed:.3f}s"
    finally:
        engine.dispose()


def test_independent_collect_one_without_ticket_still_charges_once(
    fake_watch_db, monkeypatch
) -> None:
    """§11.2：独立 ``collect_one`` 未携凭证时仍走 ``acquire`` 扣 1（legacy 默认扣费保留）。"""
    _service, _port, collector, budget, api = _build_stack(per_minute=20, monkeypatch=monkeypatch)

    asyncio.run(collector.collect_one("BVINDP0001", collection_tid=TID, source=WATCH_SOURCE))

    assert len(api.calls) == 1
    assert len(budget._requests) == 1
    # legacy acquire 只记在 _requests，不进准入账本（committed 仍为 0）。
    assert budget.snapshot(NOW_MONO)["committed"] == 0


class _DenyReserveOnceBudget:
    """包一层真 RequestBudget：``peek`` 透传，第一次 ``reserve`` 拒绝（模拟容量瞬时不足）。

    不是拿替身冒充类别隔离：类别窗 / 总窗仍由内层真 RequestBudget 裁决，``peek`` 直通。
    """

    def __init__(self, inner: RequestBudget) -> None:
        """绑定内层真预算。"""
        self._inner = inner
        self.denied_once = False

    def peek(self, kind: str, now_mono: float):
        """透传到内层真预算（类别窗与总窗仍由真实现裁决）。"""
        return self._inner.peek(kind, now_mono)

    def reserve(self, kind: str, now_mono: float, *, operation_key: str):
        """第一次调用返回拒绝，之后透传到内层真预算。"""
        from modules.hotspot.risk_control import AdmissionResult, BudgetDecision

        if not self.denied_once:
            self.denied_once = True
            return AdmissionResult(
                decision=BudgetDecision(
                    granted=False, retry_at_mono=now_mono + 10.0, reason_code="rate_limited"
                )
            )
        return self._inner.reserve(kind, now_mono, operation_key=operation_key)

    def release_unused(self, admission) -> bool:
        """透传到内层真预算。"""
        return self._inner.release_unused(admission)

    def clock(self) -> float:
        """与内层真预算同源时钟。"""
        return self._inner.clock()

    def snapshot(self, now_mono: float) -> dict:
        """透传到内层真预算。"""
        return self._inner.snapshot(now_mono)


def test_capacity_deferral_is_not_platform_failure(tmp_path, monkeypatch) -> None:
    """§9.3 / §7.5：容量不足只记 budget_deferred，**不**增 failure_count、不触发退避、不发 HTTP。"""
    from sqlalchemy import text

    factory, engine = _make_real_db(tmp_path)
    try:
        bvid = "BVDEFER0001"
        _seed_watch_row(factory, bvid)

        api = _FakeAPI()
        base = RequestBudget(per_minute=20, per_hour=300, per_day=3000, clock=lambda: NOW_MONO)
        budget = _DenyReserveOnceBudget(base)
        collector = HotspotCollector(api=api, budget=base)

        async def _fake_save_snapshot(*_a, **_k):
            """替身落库：只回条数。"""
            return 1

        monkeypatch.setattr(collector, "_save_snapshot", _fake_save_snapshot)
        service = WatchService(
            collector_port=HotspotCollectorPort(collector),
            session_factory=factory,
            now_fn=lambda: NOW_EPOCH_S,
            now_mono_fn=lambda: NOW_MONO,
            budget=budget,
        )

        result = asyncio.run(service.run_tick(limit=1))

        assert result.budget_deferred == 1
        assert result.budget_deferred_by_category == {BUDGET_CATEGORY_NORMAL: 1}
        assert result.failed == 0
        assert api.calls == [], "未准入 -> 不得发 HTTP"

        session = factory()
        try:
            row = session.execute(
                text(
                    "SELECT failure_count, next_due_epoch_s FROM hotspot_watch WHERE bvid = :b"
                ),
                {"b": bvid},
            ).first()
        finally:
            session.close()
        assert row[0] == 0, "容量延期不是平台失败，不增 failure_count"
        assert row[1] == NOW_EPOCH_S - 1, "未准入不得推进 next_due（不触发退避）"
    finally:
        engine.dispose()
