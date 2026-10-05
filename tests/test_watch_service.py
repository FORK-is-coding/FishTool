"""``watch_service`` 编排层用例：捞 → 采 → 评 → 写 → 排（FishTool 02 · 批 3）。

覆盖点（对应批 3 规格 §二「一轮 tick 流程」/ §三「必须做的三条」/ §四「测试要求」）：

- 完整一轮：捞到 → 采（**打桩**）→ 评 → fenced 写回 → ``next_due_epoch_s`` 前进；
- 空转：未到期 / 已到期 / 已释放 / 空表 —— 一轮不报错、不发起任何采集；
- 先清后调：过期行先被 ``release_expired``，随后「捞」不再出现；
- **fencing 真用上**：领取后别的代际把 ``state_revision`` 推进 → 本次写回被丢弃、
  任何列都没落盘（用真实独立事务模拟并发写入者）；
- **失败隔离**：第 1 个目标采集抛异常，第 2 个照常处理完；
- **不重复采样**：同一 tick 跑两遍，第二遍 ``next_due`` 已推进 → 不再采；
- 边界：空表 / 单条 / ``limit`` 截断；
- 时间字段一律 ``*_epoch_s`` 秒级 int。

测试策略：**采集层全部打桩**（:class:`RecordingCollector`），绝不真发网络请求（不烧配额）；
数据库用**临时文件 SQLite**（多连接各自独立事务），才能真实模拟「另一个写回代际抢先」；
算法层跑批 1 的真实 ``LifecycleV2``（纯函数，不触网、不落库）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, HotspotWatch, Video, VideoStats
from modules.hotspot.algorithm import Stage, TrendState
from modules.hotspot.watch_service import (
    DEFAULT_SAMPLE_INTERVAL_S,
    DEFAULT_TICK_LIMIT,
    MAX_BACKOFF_S,
    WATCH_SOURCE,
    HotspotCollectorPort,
    WatchService,
    advance_next_due,
    error_code_of,
    load_bvid_snapshots,
    record_failure,
    state_from_json,
    state_to_json,
)
from modules.hotspot.risk_control import BudgetWiringError, RequestBudget
from modules.hotspot.watch_store import commit_state, upsert_watch

# 统一测试时钟：2026-09-10T00:00:00Z（UTC 日界，秒级 int，便于固定网格对齐）。
NOW: int = int(datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp())
DAY: int = 86400
HOUR: int = 3600
TID: int = 1008


# --------------------------------------------------------------------------- 打桩与夹具


class RecordingCollector:
    """采集端口打桩：记录调用参数、可指定某 bvid 抛异常，绝不真发网络请求。

    只在编排层边界打桩（采集层），不改 ``collector.py`` 一行。
    """

    def __init__(self, *, fail_for=(), on_collect=None) -> None:
        """构造打桩采集端口。

        Args:
            fail_for: 需要抛异常的 bvid 集合（模拟采集失败）。
            on_collect: 每次采集时的回调 ``(bvid) -> None``（用于模拟并发写入者 / 停循环）。
        """
        self.calls: list[dict] = []
        self._fail_for = set(fail_for)
        self._on_collect = on_collect

    async def collect(self, bvid, *, collection_tid=None, source=WATCH_SOURCE):
        """记录调用参数；命中 ``fail_for`` 时抛带稳定错误码的异常。"""
        self.calls.append({"bvid": bvid, "collection_tid": collection_tid, "source": source})
        if self._on_collect is not None:
            self._on_collect(bvid)
        if bvid in self._fail_for:
            raise FakeApiError("采集失败")
        return 1


class FakeApiError(RuntimeError):
    """带稳定错误码的采集异常替身（对应 ``BiliOpsException.code`` 口径）。"""

    code = "collection_failed"


def _aid_for(bvid: str) -> int:
    """由 bvid 数字段派生的稳定 aid（``videos.aid`` 有唯一约束，避免撞号）。"""
    digits = "".join(char for char in bvid if char.isdigit())
    return int(digits) if digits else 1


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：多连接（各自独立事务），可真实模拟并发写入者。

    Yields:
        sessionmaker: 绑定临时库的会话工厂。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'watch.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def seed_watch(
    db,
    bvid,
    *,
    now=NOW,
    ttl_end_epoch_s=None,
    next_due_epoch_s=NOW - 1,
    sample_interval_s=DEFAULT_SAMPLE_INTERVAL_S,
    collection_tid=TID,
    state_json=None,
):
    """插入一行 ``hotspot_watch``（默认「已到点、未到期、在池」）。"""
    session = db()
    try:
        row = upsert_watch(
            session,
            bvid=bvid,
            now_epoch_s=now,
            ttl_end_epoch_s=ttl_end_epoch_s,
            next_due_epoch_s=next_due_epoch_s,
            sample_interval_s=sample_interval_s,
            collection_tid=collection_tid,
        )
        if state_json is not None:
            row.state_json = state_json
        session.commit()
    finally:
        session.close()


def seed_snapshots(db, bvid, points, *, tid=TID, title="测试视频", mid=9527, author="UP主"):
    """写入 ``videos`` / ``video_stats`` 行（质量 ok），供算法评估用。

    Args:
        db: 会话工厂。
        bvid: 视频 BV 号。
        points: ``[(epoch_s, view), ...]`` 快照点。
    """
    session = db()
    try:
        video = session.query(Video).filter(Video.bvid == bvid).first()
        if video is None:
            video = Video(bvid=bvid, aid=_aid_for(bvid), tid=tid, title=title, mid=mid, author=author)
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
                    collection_tid=tid,
                    raw_tid=tid,
                    view_status="ok",
                    stat_status="ok",
                    metric_status={"view": "ok"},
                )
            )
        session.commit()
    finally:
        session.close()


def read_row(db, bvid):
    """读回一行 ``HotspotWatch``（会话内用完即关，返回的是已加载的普通对象）。"""
    session = db()
    try:
        row = session.query(HotspotWatch).filter_by(bvid=bvid).one()
        session.expunge(row)
        return row
    finally:
        session.close()


def make_service(db, collector, *, now=NOW):
    """构造注入固定时钟与打桩采集端口的编排层。"""
    return WatchService(collector_port=collector, session_factory=db, now_fn=lambda: now)


def bump_revision(db, bvid, *, claim, stage="抢跑"):
    """模拟「领取后被别的代际抢先」：另开一个会话把代际推进并**提交**（真实独立事务）。"""
    session = db()
    try:
        assert commit_state(session, bvid, claim_revision=claim, last_confirmed_stage=stage) is True
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------------- 1. 完整一轮


def test_tick_full_flow_collect_eval_commit_advance(db):
    """一轮完整流程：捞到 → 采（打桩）→ 评 → 写回 → next_due 前进。"""
    bvid = "BV1FLOW0001"
    seed_watch(db, bvid, collection_tid=TID, sample_interval_s=HOUR)
    # 四个日界观测点：增量 200 / 300 / 500（播放/天），足以推进到「上升期」。
    seed_snapshots(
        db,
        bvid,
        [(NOW - 3 * DAY, 1000), (NOW - 2 * DAY, 1200), (NOW - 1 * DAY, 1500), (NOW, 2000)],
    )
    collector = RecordingCollector()

    result = asyncio.run(make_service(db, collector).run_tick())

    # ---- 采：只在编排边界打桩，断言调用参数（bvid / 归属分区 / 来源）----
    assert collector.calls == [{"bvid": bvid, "collection_tid": TID, "source": WATCH_SOURCE}]

    # ---- 轮级计数 ----
    assert result.now_epoch_s == NOW
    assert result.released == 0
    assert result.due_count == 1
    assert result.committed == 1
    assert result.dropped == 0
    assert result.failed == 0
    assert [item.bvid for item in result.detections] == [bvid]

    # ---- 写：算法层结果真的落到了 evaluations 列 ----
    row = read_row(db, bvid)
    assert row.last_confirmed_stage == Stage.RISING  # 上升期
    assert row.last_evaluation_epoch_s == NOW
    assert row.coverage_ratio == pytest.approx(1.0)
    assert row.coverage_state == "full_support"
    assert row.state_json["stage"] == Stage.RISING
    assert row.state_json["prev_rate"] == pytest.approx(500.0)
    assert row.state_json["last_evaluation_epoch_s"] == NOW

    # ---- 排：next_due 按该行 sample_interval_s 前进；成功计数复位 ----
    assert row.next_due_epoch_s == NOW + HOUR
    assert row.last_success_epoch_s == NOW
    assert row.failure_count == 0
    assert row.last_error_code is None

    # ---- 代际（批 3.5 · 改动一）：一轮 tick = 一个事务 = 代际净 +1 ----
    # 步骤 5（commit_state）与步骤 6（advance_next_due）同事务，两次状态写入只 +1 代际；
    # 上面已证明调度列（next_due / last_success）与状态列（state_json / stage）都落了，
    # 说明这不是「漏写」（漏写会某一族列为空），而是两次自增合并成一次。
    assert row.state_revision == 1
    assert row.next_due_epoch_s == NOW + HOUR and row.last_success_epoch_s == NOW
    assert row.state_json is not None and row.last_confirmed_stage == Stage.RISING


# --------------------------------------------------------------------------- 2. 空转


def test_tick_advances_generation_by_one_merging_two_writes(db):
    """改动一：一轮 tick 跑完代际只前进 1（同事务里两次状态写入合并为一次自增）。"""
    bvid = "BV1GEN00001"
    seed_watch(db, bvid, next_due_epoch_s=NOW - 1, sample_interval_s=HOUR)
    seed_snapshots(db, bvid, [(NOW - DAY, 100), (NOW, 900)])
    assert read_row(db, bvid).state_revision == 0

    result = asyncio.run(make_service(db, RecordingCollector()).run_tick())
    assert result.committed == 1

    row = read_row(db, bvid)
    # 调度列与状态列都落（不是漏写），代际仍只 +1。
    assert row.next_due_epoch_s == NOW + HOUR
    assert row.last_success_epoch_s == NOW
    assert row.state_json is not None
    assert row.coverage_state is not None
    assert row.state_revision == 1


def test_tick_empty_table_is_noop(db):
    """空表：这轮空转、不报错、不发起任何采集。"""
    collector = RecordingCollector()

    result = asyncio.run(make_service(db, collector).run_tick())

    assert (result.released, result.due_count, result.committed, result.failed) == (0, 0, 0, 0)
    assert collector.calls == []
    assert result.detections == []


def test_tick_skips_not_due_expired_and_released(db):
    """未到期 / 已到期 / 已释放都不该进本轮：空转且不采集。"""
    seed_watch(db, "BV1SKIP0001", ttl_end_epoch_s=NOW + HOUR, next_due_epoch_s=NOW + 1)  # 未到期
    seed_watch(db, "BV1SKIP0002", ttl_end_epoch_s=NOW, next_due_epoch_s=NOW - 1)  # 已到期
    seed_watch(db, "BV1SKIP0003", ttl_end_epoch_s=NOW + HOUR, next_due_epoch_s=NOW - 1)
    # 已释放：在池谓词之外（active=0）。
    session = db()
    try:
        released = session.query(HotspotWatch).filter_by(bvid="BV1SKIP0003").one()
        released.active = False
        session.commit()
    finally:
        session.close()

    collector = RecordingCollector()
    result = asyncio.run(make_service(db, collector).run_tick())

    assert result.due_count == 0
    assert result.committed == 0
    assert collector.calls == []
    # 已到期那一行在本轮「先清」里被归档，让出名额。
    assert result.released == 1


# --------------------------------------------------------------------------- 3. 先清后调


def test_tick_releases_expired_before_scheduling(db):
    """到期先归档：过期行先被 release，再捞时不出现，也不会被采集。"""
    expired, due = "BV1CLN00001", "BV1CLN00002"
    seed_watch(db, expired, ttl_end_epoch_s=NOW, next_due_epoch_s=NOW - 5)
    seed_watch(db, due, ttl_end_epoch_s=NOW + HOUR, next_due_epoch_s=NOW - 1)
    collector = RecordingCollector()

    result = asyncio.run(make_service(db, collector).run_tick())

    assert result.released == 1
    assert result.due_count == 1
    assert result.committed == 1
    assert [call["bvid"] for call in collector.calls] == [due]

    released_row = read_row(db, expired)
    assert released_row.active is False
    assert released_row.stop_reason == "expired"
    assert released_row.released_epoch_s == NOW
    assert released_row.state_json is None  # 归档不写入 evaluate 结果


# --------------------------------------------------------------------------- 4. fencing


def test_fencing_stale_claim_discards_write(db):
    """领取后代际被别的写入者推进 -> 第 5 步写回被丢弃，任何列都没落盘。"""
    bvid = "BV1FENCE001"
    seed_watch(db, bvid, next_due_epoch_s=NOW - 1)
    seed_snapshots(db, bvid, [(NOW - DAY, 100), (NOW, 900)])
    # 采集期间「别的代际」把 state_revision 0 -> 1 并提交（真实独立事务）。
    collector = RecordingCollector(on_collect=lambda target: bump_revision(db, target, claim=0))

    result = asyncio.run(make_service(db, collector).run_tick())

    # ---- 本轮结果被丢弃，不算失败 ----
    assert result.committed == 0
    assert result.dropped == 1
    assert result.failed == 0

    row = read_row(db, bvid)
    # 抢跑者写的那一列在（证明它确实先落盘），服务这次写回一列都没落。
    assert row.last_confirmed_stage == "抢跑"
    assert row.state_revision == 1
    assert row.state_json is None
    assert row.coverage_ratio is None
    assert row.coverage_state is None
    assert row.last_evaluation_epoch_s is None
    # 调度列也没被本轮动过：next_due 未前进、成功列未写。
    assert row.next_due_epoch_s == NOW - 1
    assert row.last_success_epoch_s is None


def test_fencing_current_claim_is_accepted(db):
    """没被抢先的普通一轮：同代际提交成功并把代际 +1（对照组，证明 fencing 不是恒 False）。"""
    bvid = "BV1FENCE002"
    seed_watch(db, bvid, next_due_epoch_s=NOW - 1)
    collector = RecordingCollector()

    result = asyncio.run(make_service(db, collector).run_tick())

    assert result.committed == 1
    assert result.dropped == 0
    assert read_row(db, bvid).state_revision == 1  # 代际净 +1（批 3.5 合并自增）


# --------------------------------------------------------------------------- 5. 失败隔离


def test_failure_isolation_one_target_raises_other_completes(db):
    """第 1 个目标采集抛异常：记失败但仍继续，第 2 个照常处理完。"""
    bad, good = "BV1FAILA001", "BV1FAILB001"
    seed_watch(db, bad, next_due_epoch_s=NOW - 100)  # 先被处理
    seed_watch(db, good, next_due_epoch_s=NOW - 50)
    collector = RecordingCollector(fail_for={bad})

    result = asyncio.run(make_service(db, collector).run_tick(limit=DEFAULT_TICK_LIMIT))

    assert [call["bvid"] for call in collector.calls] == [bad, good]  # 第二轮照常发出
    assert result.due_count == 2
    assert result.failed == 1
    assert result.committed == 1
    assert result.dropped == 0

    # 失败目标：failure_count + 1、稳定错误码，并按指数退避推进 next_due（退避 >= 正常间隔）。
    failed_row = read_row(db, bad)
    assert failed_row.failure_count == 1
    assert failed_row.last_error_code == "collection_failed"
    assert failed_row.last_attempt_epoch_s == NOW
    # 首败：退避 = min(3600 * 2 ** 1, 21600) = 7200，next_due = NOW + max(3600, 7200) = NOW + 7200。
    assert failed_row.next_due_epoch_s == NOW + 7200
    assert failed_row.state_json is None
    assert failed_row.last_success_epoch_s is None

    # 成功目标：完全不受影响。
    good_row = read_row(db, good)
    assert good_row.state_json is not None
    assert good_row.next_due_epoch_s == NOW + DEFAULT_SAMPLE_INTERVAL_S
    assert good_row.failure_count == 0
    assert good_row.last_success_epoch_s == NOW


# --------------------------------------------------------------------------- 6. 不重复采样


def test_no_double_sample_when_tick_runs_twice(db):
    """同一 tick 跑两遍：第二遍 next_due 已推进 -> 不再采集、不再写回。"""
    bvid = "BV1ONCE0001"
    seed_watch(db, bvid, next_due_epoch_s=NOW - 1)
    seed_snapshots(db, bvid, [(NOW - DAY, 100), (NOW, 900)])
    collector = RecordingCollector()
    service = make_service(db, collector)

    first = asyncio.run(service.run_tick())
    second = asyncio.run(service.run_tick())

    assert first.committed == 1
    assert second.due_count == 0
    assert second.committed == 0
    assert len(collector.calls) == 1  # 只采了一次
    assert read_row(db, bvid).next_due_epoch_s == NOW + DEFAULT_SAMPLE_INTERVAL_S


# --------------------------------------------------------------------------- 7. 边界


def test_limit_truncates_targets(db):
    """limit 截断：只处理最靠前的 N 个，其余一行不动。"""
    for index, offset in enumerate((30, 20, 10), start=1):
        seed_watch(db, f"BV1LIMIT0{index:02d}", next_due_epoch_s=NOW - offset)
    collector = RecordingCollector()

    result = asyncio.run(make_service(db, collector).run_tick(limit=2))

    assert result.due_count == 2
    assert result.committed == 2
    assert [call["bvid"] for call in collector.calls] == ["BV1LIMIT001", "BV1LIMIT002"]
    untouched = read_row(db, "BV1LIMIT003")
    assert untouched.state_json is None
    assert untouched.next_due_epoch_s == NOW - 10


def test_single_row_without_snapshots_still_commits(db):
    """单条且无历史快照（采集打桩不落库）：数据不足也走完流程，不报错。"""
    bvid = "BV1ONLY0001"
    seed_watch(db, bvid, next_due_epoch_s=NOW - 1)
    collector = RecordingCollector()

    result = asyncio.run(make_service(db, collector).run_tick())

    assert result.committed == 1
    assert result.failed == 0
    row = read_row(db, bvid)
    assert row.state_json is not None
    # 状态机自身未起步（无有效窗 -> 没被推进），仍是默认的观察期；
    # 而算法给出的 Analysis.stage 是「数据不足」，那不是「已确认阶段」，故不落历史列。
    assert row.state_json["stage"] == Stage.OBSERVING
    assert row.last_confirmed_stage is None
    # coverage 两级仍然成对落库（0.0 / insufficient）。
    assert row.coverage_ratio == pytest.approx(0.0)
    assert row.coverage_state == "insufficient"
    assert row.next_due_epoch_s == NOW + DEFAULT_SAMPLE_INTERVAL_S


# --------------------------------------------------------------------------- 8. 时间字段口径


def test_time_columns_are_epoch_s_only(db):
    """``hotspot_watch`` 的时间列一律 ``*_epoch_s`` 后缀，没有 ``_ts`` / ``_at``。"""
    names = {column.name for column in HotspotWatch.__table__.c}

    epoch_names = {name for name in names if "epoch" in name}
    assert epoch_names == {
        "first_seen_epoch_s",
        "last_seen_epoch_s",
        "ttl_end_epoch_s",
        "released_epoch_s",
        "next_due_epoch_s",
        "last_attempt_epoch_s",
        "last_success_epoch_s",
        "last_evaluation_epoch_s",
    }
    assert all(name.endswith("_epoch_s") for name in epoch_names)
    assert not [name for name in names if name.endswith(("_ts", "_at"))]


def test_tick_writes_epoch_s_int_values(db):
    """本轮写下去的时间值都是秒级 int，``state_json`` 里的时间也是 ``*_epoch_s``。"""
    bvid = "BV1TIME0001"
    seed_watch(db, bvid, next_due_epoch_s=NOW - 1, sample_interval_s=HOUR)
    seed_snapshots(db, bvid, [(NOW - DAY, 100), (NOW, 900)])

    result = asyncio.run(make_service(db, RecordingCollector()).run_tick())

    row = read_row(db, bvid)
    assert type(result.now_epoch_s) is int
    assert type(row.next_due_epoch_s) is int
    assert type(row.last_success_epoch_s) is int
    assert type(row.last_evaluation_epoch_s) is int
    assert row.next_due_epoch_s == NOW + HOUR
    assert type(row.state_json["last_evaluation_epoch_s"]) is int


# --------------------------------------------------------------------------- 9. 采集端口


def test_default_port_reuses_public_single_video_entry():
    """缺省端口只走 collector 的**公开单视频入口** ``collect_one``，不再触碰私有方法、不触网。"""
    collector = MagicMock()
    collector.collect_one = AsyncMock(return_value=1)
    port = HotspotCollectorPort(collector)

    saved = asyncio.run(port.collect("BV1PORT0001", collection_tid=TID, source=WATCH_SOURCE))

    assert saved == 1
    collector.collect_one.assert_awaited_once_with(
        "BV1PORT0001",
        collection_tid=TID,
        source=WATCH_SOURCE,
    )
    # 红线：端口层不再调用任何私有方法。
    assert collector._fetch_view.call_count == 0
    assert collector._save_snapshot.call_count == 0


# --------------------------------------------------------------------------- 10. 工具函数


def test_state_json_roundtrip_and_invalid_payload():
    """``state_json`` 往返只带续算字段（不含算法内存代际），非法载荷回退全新状态。"""
    state = TrendState(
        last_evaluation_epoch_s=NOW,
        prev_rate=12.5,
        candidate="up",
        baseline=10.0,
        count=1,
        stable_count=0,
        stage=Stage.RISING,
        state_revision=9,
    )

    payload = state_to_json(state)
    assert "state_revision" not in payload  # 算法内存代际不落库
    restored = state_from_json(payload)
    assert restored.last_evaluation_epoch_s == NOW
    assert restored.prev_rate == pytest.approx(12.5)
    assert restored.candidate == "up"
    assert restored.baseline == pytest.approx(10.0)
    assert restored.count == 1
    assert restored.stable_count == 0
    assert restored.stage == Stage.RISING

    assert state_from_json(None) == TrendState()
    assert state_from_json("not-a-dict") == TrendState()
    assert state_from_json({"count": "x", "prev_rate": True, "stage": ""}) == TrendState()


def test_load_bvid_snapshots_reads_watch_first_seen(db):
    """B6b：load_bvid_snapshots 按 bvid 从 HotspotWatch 读同一首次发现时刻（非 created_at）。"""
    bvid = "BV1FS00001"
    seed_watch(db, bvid, now=NOW - 5 * DAY, next_due_epoch_s=NOW - 1)
    seed_snapshots(db, bvid, [(NOW - DAY, 100), (NOW, 900)])

    session = db()
    try:
        rows = load_bvid_snapshots(session, bvid)
    finally:
        session.close()

    assert rows
    assert all(row.first_seen_epoch_s == NOW - 5 * DAY for row in rows)


def test_load_bvid_snapshots_first_seen_none_without_watch(db):
    """B6b：无 watch 的 bvid，first_seen_epoch_s 保持 None，且不为读取新建 watch。"""
    bvid = "BV1FS00002"
    seed_snapshots(db, bvid, [(NOW - DAY, 100), (NOW, 900)])

    session = db()
    try:
        rows = load_bvid_snapshots(session, bvid)
        watch_count = session.query(HotspotWatch).filter_by(bvid=bvid).count()
    finally:
        session.close()

    assert rows
    assert all(row.first_seen_epoch_s is None for row in rows)
    assert watch_count == 0  # 读取不为所有 GET 创建 watch


def test_error_code_of_prefers_exception_code():
    """错误码提取：优先 ``exc.code``，否则类名；截断 64，永不带正文。"""
    assert error_code_of(FakeApiError("正文不该进库")) == "collection_failed"
    assert error_code_of(RuntimeError("boom")) == "RuntimeError"

    class LongCode(Exception):
        code = "x" * 100

    assert len(error_code_of(LongCode("boom"))) == 64


def test_advance_next_due_advances_and_falls_back(db):
    """步骤 6：``next_due = now + sample_interval_s``；非法间隔回退默认 3600。"""
    seed_watch(db, "BV1ADV00001", next_due_epoch_s=NOW - 1, sample_interval_s=7200)
    seed_watch(db, "BV1ADV00002", next_due_epoch_s=NOW - 1, sample_interval_s=0)

    session = db()
    try:
        advance_next_due(session, "BV1ADV00001", now_epoch_s=NOW, sample_interval_s=7200)
        advance_next_due(session, "BV1ADV00002", now_epoch_s=NOW, sample_interval_s=0)
        session.commit()
    finally:
        session.close()

    row = read_row(db, "BV1ADV00001")
    assert row.next_due_epoch_s == NOW + 7200
    assert row.last_success_epoch_s == NOW
    assert row.state_revision == 1  # 调度列变更 -> 代际 +1
    assert read_row(db, "BV1ADV00002").next_due_epoch_s == NOW + DEFAULT_SAMPLE_INTERVAL_S


def test_record_failure_increments_backs_off_and_records_code(db):
    """失败落点：``failure_count`` 累加并封顶、错误码截断、``last_attempt_epoch_s`` 记时刻，
    ``next_due`` 按指数退避推进（改动二）。"""
    seed_watch(db, "BV1REC00001", next_due_epoch_s=NOW - 1, sample_interval_s=DEFAULT_SAMPLE_INTERVAL_S)

    session = db()
    try:
        record_failure(session, "BV1REC00001", error_code="x" * 100, now_epoch_s=NOW)
        record_failure(session, "BV1REC00001", error_code="second_error", now_epoch_s=NOW + 1)
        session.commit()
    finally:
        session.close()

    row = read_row(db, "BV1REC00001")
    assert row.failure_count == 2
    assert row.last_error_code == "second_error"   # 稳定错误码，非正文
    assert row.last_attempt_epoch_s == NOW + 1
    assert row.state_revision == 2                 # 两次失败各走独立事务，各 +1
    # 第 1 次：退避 = min(3600 * 2**1, 21600) = 7200 -> next_due = NOW + 7200；
    # 第 2 次：退避 = min(3600 * 2**2, 21600) = 14400 -> next_due = (NOW+1) + 14400。
    assert row.next_due_epoch_s == (NOW + 1) + 14400


def test_record_failure_backoff_never_below_normal_interval_when_interval_exceeds_cap(db):
    """边界（改动二）：``sample_interval_s`` 本身 > 6h 时，退避不得把 next_due 压到正常间隔以下。

    专盯旧公式 ``min(sample_interval_s * 2**n, 6h)`` 的洞：若直接把该 min 当推进量，
    ``sample_interval_s = 8h`` 会被压到 6h，失败反而加速。新公式
    ``next_due = now + max(sample_interval_s, min(...))`` 必须让推进量 >= sample_interval_s。
    """
    interval = 8 * HOUR  # 28800s，> MAX_BACKOFF_S(21600s)
    assert MAX_BACKOFF_S < interval
    seed_watch(db, "BV1REC00002", next_due_epoch_s=NOW - 1, sample_interval_s=interval)

    session = db()
    try:
        record_failure(
            session, "BV1REC00002", error_code="boom", now_epoch_s=NOW, sample_interval_s=interval
        )
        session.commit()
    finally:
        session.close()

    row = read_row(db, "BV1REC00002")
    assert row.failure_count == 1
    # 退避被 6h 封顶 = 21600，但 max(正常间隔, 退避) 兜住：推进量 = 28800 = 正常间隔。
    assert row.next_due_epoch_s - NOW == interval
    assert row.next_due_epoch_s - NOW >= interval   # 关键断言：不被压到 6h 以下


def test_advance_next_due_can_defer_revision_bump_to_same_transaction(db):
    """改动一配套：同一事务内已有状态写入时，``advance_next_due(bump_revision=False)`` 不重复 +1。"""
    bvid = "BV1ADV00003"
    seed_watch(db, bvid, next_due_epoch_s=NOW - 1, sample_interval_s=7200)

    session = db()
    try:
        # 模拟 tick：先 commit_state（代际 0 -> 1），再 advance_next_due 不再 +1。
        assert commit_state(session, bvid, claim_revision=0, last_confirmed_stage=Stage.RISING) is True
        advance_next_due(session, bvid, now_epoch_s=NOW, sample_interval_s=7200, bump_revision=False)
        session.commit()
    finally:
        session.close()

    row = read_row(db, bvid)
    assert row.next_due_epoch_s == NOW + 7200          # 调度列照落
    assert row.last_success_epoch_s == NOW
    assert row.state_revision == 1                     # 代际净 +1


# --------------------------------------------------------------------------- 11. 常驻循环


def test_loop_runs_one_tick_then_stops(db):
    """``_loop`` 跑满一轮 tick 后按停止信号退出（不空转、不无限循环）。"""
    seed_watch(db, "BV1LOOP0001", next_due_epoch_s=NOW - 1)
    stop_event = asyncio.Event()
    collector = RecordingCollector(on_collect=lambda _bvid: stop_event.set())
    service = make_service(db, collector)

    asyncio.run(service._loop(interval_s=1, stop_event=stop_event, limit=DEFAULT_TICK_LIMIT))

    assert len(collector.calls) == 1
    assert read_row(db, "BV1LOOP0001").next_due_epoch_s == NOW + DEFAULT_SAMPLE_INTERVAL_S


# --------------------------------------------------------------------------- 12. W3 接线 / 单实例 / 取消（07 执行案 §6 §8.2 §9.4）

#: W3 用例统一单调时钟读数（只喂真 RequestBudget，绝不落库）。
W3_MONO: float = 777.0


class AdmittedRecordingCollector:
    """采集端口替身：实现单请求准入协议；``collect_admitted`` **真兑换**票据（用真预算）。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls: list = []

    async def collect(self, bvid, *, collection_tid=None, source=WATCH_SOURCE):
        """无票据路径：只记录，不触网。"""
        self.calls.append(("collect", bvid))
        return 1

    async def collect_admitted(self, bvid, *, admission, collection_tid=None, source=WATCH_SOURCE):
        """带票据路径：用票据发行方真兑换一次，再记录（绝不真发请求）。"""
        admission.issuer.redeem(admission, operation_key=bvid, now_mono=admission.issuer.clock())
        self.calls.append(("admitted", bvid))
        return 1


def test_run_tick_budget_override_uses_override_only(db):
    """§8.2：``run_tick(budget=override)`` 该次操作**只用** override；默认 ``self._budget`` 纹丝不动。"""
    bvid = "BVOVER0001"
    seed_watch(db, bvid, next_due_epoch_s=NOW - 1)
    seed_snapshots(db, bvid, [(NOW - DAY, 100), (NOW, 900)])
    default_budget = RequestBudget(per_minute=5, per_hour=100, per_day=1000, clock=lambda: W3_MONO)
    override = RequestBudget(per_minute=5, per_hour=100, per_day=1000, clock=lambda: W3_MONO)
    collector = AdmittedRecordingCollector()
    service = WatchService(
        collector_port=collector, session_factory=db, now_fn=lambda: NOW, budget=default_budget
    )

    result = asyncio.run(service.run_tick(limit=1, budget=override))

    assert result.committed == 1
    assert collector.calls == [("admitted", bvid)]
    # override 被扣 1；默认预算未同时被扣（一次操作不同时扣两个对象）。
    assert override.snapshot(W3_MONO)["committed"] == 1
    assert default_budget.snapshot(W3_MONO)["committed"] == 0
    assert default_budget.snapshot(W3_MONO)["reserved"] == 0


def test_run_tick_with_budget_requires_admission_capable_port(db):
    """§8.2：有预算的 watch 路径要求端口实现 ``collect_admitted``，否则显式报错、不静默绕过。"""
    seed_watch(db, "BVWIRE0001", next_due_epoch_s=NOW - 1)
    budget = RequestBudget(per_minute=5, per_hour=100, per_day=1000, clock=lambda: W3_MONO)
    collector = RecordingCollector()  # 只有 collect，无 collect_admitted
    service = WatchService(
        collector_port=collector, session_factory=db, now_fn=lambda: NOW, budget=budget
    )

    with pytest.raises(BudgetWiringError):
        asyncio.run(service.run_tick(limit=1))

    assert collector.calls == []  # 未静默绕过预算 -> 一次采集都没发


def test_run_tick_serialized_by_instance_lock(db):
    """§6：同一实例的两个 ``run_tick`` 并发时被实例级锁串行化，不进/退出交错。"""
    seed_watch(db, "BVLCK00001", next_due_epoch_s=NOW - 2)
    seed_watch(db, "BVLCK00002", next_due_epoch_s=NOW - 1)
    order: list = []

    class _SlowCollector:
        """慢采集替身：用「进入→退出」标记暴露是否并发交错。"""

        async def collect(self, bvid, *, collection_tid=None, source=WATCH_SOURCE):
            """进入 -> 睡一小会 -> 退出。"""
            order.append(("enter", bvid))
            await asyncio.sleep(0.05)
            order.append(("exit", bvid))
            return 1

    service = WatchService(collector_port=_SlowCollector(), session_factory=db, now_fn=lambda: NOW)

    async def scenario():
        """并发跑两轮 tick。"""
        return await asyncio.gather(service.run_tick(limit=1), service.run_tick(limit=1))

    results = asyncio.run(scenario())

    # 串行：每轮「进入→退出」成对出现，绝不 enter/enter/exit/exit。
    assert [step[0] for step in order] == ["enter", "exit", "enter", "exit"]
    assert sum(item.committed for item in results) == 2


def test_cancel_during_collect_releases_reservation(db):
    """§9.4：reserve 后采集期间被取消 -> finally 释放未用票据，不留悬挂 reservation。"""
    seed_watch(db, "BVCAN00001", next_due_epoch_s=NOW - 1)
    budget = RequestBudget(per_minute=5, per_hour=100, per_day=1000, clock=lambda: W3_MONO)
    started = asyncio.Event()

    class _BlockingAdmittedCollector:
        """带票据采集替身：阻塞在采集里，等待被取消。"""

        async def collect(self, bvid, *, collection_tid=None, source=WATCH_SOURCE):
            """无票据路径（本轮不会走到）。"""
            return 1

        async def collect_admitted(self, bvid, *, admission, collection_tid=None, source=WATCH_SOURCE):
            """置位 started 后长睡，等外部取消。"""
            started.set()
            await asyncio.sleep(3600)
            return 1

    service = WatchService(
        collector_port=_BlockingAdmittedCollector(),
        session_factory=db,
        now_fn=lambda: NOW,
        budget=budget,
    )

    async def scenario():
        """起 run_tick，等它进入采集后取消。"""
        task = asyncio.create_task(service.run_tick(limit=1))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    snapshot = budget.snapshot(W3_MONO)
    assert snapshot["reserved"] == 0, "取消后不得留下悬挂 reservation"
    assert snapshot["cancelled"] >= 1
