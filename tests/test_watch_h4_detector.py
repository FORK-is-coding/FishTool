"""08 案 §H4（R2）：watch 检测器**逐目标**构造的契约测试。

靶子（08 案 §B4 验收 / §M3）：

- 一轮多目标时 ``detector_factory`` 被**逐目标**调用，不再整轮构造一次；
- 每次传入的 ``initial_states`` 只含当前目标，且取值来自 watch 表 ``state_json``
  （不依赖上一目标的 detector 内存对象）—— 所以逐目标构造不丢多目标续算状态；
- 传入的 ``as_of`` 是「该目标采集完成之后」的时刻，不再等于轮开始时刻
  （否则本轮刚采到的点会被当未来信息排掉，最新采样永远晚一轮生效）；
- 07 案预算 / 凭证语义不动：无预算路径（``collect``）与有预算路径（``collect_admitted``）
  的 reserve→redeem、``admitted <= limit`` 均保持原样。

全部采集打桩，绝不真发网络请求；数据库用临时文件 SQLite。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, HotspotWatch, Video, VideoStats
from modules.hotspot.algorithm import LifecycleV2, TrendState
from modules.hotspot.watch_service import (
    WATCH_SOURCE,
    WatchService,
    state_from_json,
    state_to_json,
)
from modules.hotspot.watch_store import upsert_watch

# 统一测试时钟：2026-09-10T00:00:00Z（UTC 日界，秒级 int）。
NOW: int = int(datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp())
HOUR: int = 3600
TID: int = 1008


# --------------------------------------------------------------------------- 打桩


class RecordingCollector:
    """采集端口打桩：记录调用参数，绝不真发网络请求。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def collect(self, bvid, *, collection_tid=None, source=WATCH_SOURCE):
        self.calls.append({"bvid": bvid, "collection_tid": collection_tid, "source": source})
        return 1


class TickingClock:
    """每次读数前进一小时的可变时钟，用来区分「轮开始时刻」与「采集完成时刻」。"""

    def __init__(self, start: int = NOW, step: int = HOUR) -> None:
        self.start = start
        self.step = step
        self.reads: list[int] = []

    def __call__(self) -> int:
        value = self.start + len(self.reads) * self.step
        self.reads.append(value)
        return value


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：多连接各自独立事务。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'h4.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _aid_for(bvid: str) -> int:
    digits = "".join(char for char in bvid if char.isdigit())
    return int(digits) if digits else 1


def seed_watch(db, bvid, *, state_json=None) -> None:
    """插入一行已到点、未到期的 ``hotspot_watch``。"""
    session = db()
    try:
        row = upsert_watch(
            session,
            bvid=bvid,
            now_epoch_s=NOW,
            ttl_end_epoch_s=None,
            next_due_epoch_s=NOW - 1,
            sample_interval_s=3600,
            collection_tid=TID,
        )
        if state_json is not None:
            row.state_json = state_json
        session.commit()
    finally:
        session.close()


def seed_snapshots(db, bvid, points) -> None:
    """写入 ``videos`` / ``video_stats`` 行（质量 ok）。"""
    session = db()
    try:
        video = session.query(Video).filter(Video.bvid == bvid).first()
        if video is None:
            video = Video(
                bvid=bvid, aid=_aid_for(bvid), tid=TID, title="测试视频", mid=9527, author="UP主"
            )
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


def read_row(db, bvid):
    session = db()
    try:
        row = session.query(HotspotWatch).filter_by(bvid=bvid).one()
        session.expunge(row)
        return row
    finally:
        session.close()


def _make_service(db, collector, *, clock=None, factory=None) -> WatchService:
    return WatchService(
        collector_port=collector,
        session_factory=db,
        now_fn=clock or TickingClock(),
        detector_factory=factory,
    )


# ------------------------------------------------------------------- 逐目标构造


def test_detector_factory_called_once_per_target(db) -> None:
    """多目标一轮 -> 逐目标各构造一次，且 initial_states 只含当前目标。"""
    collector = RecordingCollector()
    for bvid in ("BV101", "BV102"):
        seed_watch(db, bvid)
        seed_snapshots(db, bvid, [(NOW - 3 * HOUR, 1000)])

    calls: list[tuple[dict, int]] = []

    def factory(initial_states, as_of):
        calls.append((dict(initial_states), as_of))
        return LifecycleV2(initial_states=initial_states, as_of_epoch_s=as_of)

    service = _make_service(db, collector, factory=factory)
    result = asyncio.run(service.run_tick(limit=10))

    assert result.committed == 2
    assert len(calls) == 2, "detector 必须是逐目标构造，不是整轮一次"
    assert [sorted(states) for states, _ in calls] == [["BV101"], ["BV102"]]


def test_detector_as_of_is_after_that_targets_collection(db) -> None:
    """as_of 取「该目标采集完成之后」的时刻，严格晚于轮开始时刻。"""
    collector = RecordingCollector()
    for bvid in ("BV201", "BV202"):
        seed_watch(db, bvid)
        seed_snapshots(db, bvid, [(NOW - 3 * HOUR, 1000)])

    clock = TickingClock()
    calls: list[int] = []

    def factory(initial_states, as_of):
        calls.append(as_of)
        return LifecycleV2(initial_states=initial_states, as_of_epoch_s=as_of)

    service = _make_service(db, collector, clock=clock, factory=factory)
    asyncio.run(service.run_tick(limit=10))

    tick_start = clock.reads[0]
    assert tick_start == NOW
    assert len(calls) == 2
    # 每个目标的 as_of 都晚于轮开始时刻，且逐目标递增（不是同一个轮开始时刻）。
    assert all(as_of > tick_start for as_of in calls)
    assert calls == sorted(calls) and calls[0] != calls[1]


def test_snapshot_captured_after_tick_start_still_counts(db) -> None:
    """H4 的行为收益：轮开始之后落库的点，本轮就能参与 v2，不必等下一轮。

    构造：轮开始时刻是 ``NOW``，但已有快照的 ``captured_epoch_s`` 是 ``NOW + 1800``
    （模拟采集写回落在轮开始之后）。若 detector 仍在轮开始前构造、``as_of`` 取 ``NOW``，
    这条点会被当未来信息排掉，本轮可见样本数为 0。
    """
    collector = RecordingCollector()
    seed_watch(db, "BV301")
    seed_snapshots(db, "BV301", [(NOW + 1800, 1500)])

    clock = TickingClock()

    def factory(initial_states, as_of):
        return LifecycleV2(initial_states=initial_states, as_of_epoch_s=as_of)

    service = _make_service(db, collector, clock=clock, factory=factory)
    result = asyncio.run(service.run_tick(limit=10))

    assert result.committed == 1
    assert clock.reads[1] > NOW + 1800
    detection = result.detections[0]
    assert detection.metrics["sample_count"] == 1


# --------------------------------------------------------------- 状态续算保住


def test_detector_initial_state_comes_from_watch_row(db) -> None:
    """initial_states 的取值来自 watch 表 state_json，不靠上一目标的 detector 内存。"""
    collector = RecordingCollector()
    seed = TrendState()
    seed_json = state_to_json(seed)
    seed_watch(db, "BV401", state_json=seed_json)
    seed_snapshots(db, "BV401", [(NOW - 3 * HOUR, 1000)])

    calls: list[dict] = []

    def factory(initial_states, as_of):
        calls.append(dict(initial_states))
        return LifecycleV2(initial_states=initial_states, as_of_epoch_s=as_of)

    service = _make_service(db, collector, factory=factory)
    result = asyncio.run(service.run_tick(limit=10))

    assert result.committed == 1
    expected = state_from_json(seed_json)
    assert state_to_json(calls[0]["BV401"]) == state_to_json(expected)


def test_second_target_state_is_not_borrowed_from_first(db) -> None:
    """处理第二个目标时，第一个目标的状态已落表；第二个目标拿到的是自己的表值。"""
    collector = RecordingCollector()
    first_seed = state_to_json(TrendState())
    second_seed = state_to_json(TrendState())
    seed_watch(db, "BV501", state_json=first_seed)
    seed_watch(db, "BV502", state_json=second_seed)
    seed_snapshots(db, "BV501", [(NOW - 3 * HOUR, 1000)])
    seed_snapshots(db, "BV502", [(NOW - 3 * HOUR, 2000)])

    calls: list[dict] = []

    def factory(initial_states, as_of):
        calls.append(dict(initial_states))
        return LifecycleV2(initial_states=initial_states, as_of_epoch_s=as_of)

    service = _make_service(db, collector, factory=factory)
    asyncio.run(service.run_tick(limit=10))

    assert sorted(calls[0]) == ["BV501"]
    assert sorted(calls[1]) == ["BV502"]
    # 两个目标各自续算：第二个目标不会拿到第一个目标的状态对象。
    assert calls[0]["BV501"] is not calls[1]["BV502"]
    # 第一个目标已按自己的历史写回（不是初始空表），证明状态真的在表上流转。
    assert read_row(db, "BV501").state_json != first_seed
