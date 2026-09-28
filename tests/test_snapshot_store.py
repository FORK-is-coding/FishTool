"""热点快照写入内核测试（FishTool 03 · 批 2 · 规格 §4.5）。

聚焦 ``modules/hotspot/snapshot_store.persist_snapshot`` 的写入语义，以及
``HotspotCollector._save_snapshot`` 旧 async wrapper 的事务边界：

- 缺失播放量必须落真实 SQL ``NULL``，而不是 ``0``（default=0 不得抢占）；
- 真实 0 才写 0 且 ``view_status='ok'``；
- view 有效、其它指标缺失时 ``stat_status='partial'``，不误杀有效播放；
- 同步内核不提交 caller 事务；
- 包装层在自有事务内提交并返回信号条数。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

import modules.hotspot.collector as collector_module
from core.database import DatabaseManager, VideoStats
from modules.hotspot.collector import HotspotCollector
from modules.hotspot.snapshot_store import persist_snapshot


@pytest.fixture()
def manager(tmp_path):
    """基于临时目录的真实 SQLite 管理器，绝不触碰仓库 data/ 库。"""
    return DatabaseManager(str(tmp_path / "snap.db"))


def _view_data(**overrides):
    """构造一份完整 view 详情；可用 overrides 覆盖任意字段。"""
    base = {
        "bvid": "BV1test",
        "aid": 1001,
        "title": "标题",
        "desc": "简介",
        "duration": 120,
        "tname": "绘画",
        "tid": 1007,
        "pubdate": 1768485600,
        "owner": {"mid": 42, "name": "UP"},
        "stat": {
            "view": 100,
            "danmaku": 1,
            "reply": 2,
            "favorite": 3,
            "coin": 4,
            "share": 5,
            "like": 6,
        },
    }
    base.update(overrides)
    return base


def _write(manager, view_data, **kwargs):
    """写入一次快照并提交，返回 (video_id,)。"""
    session = manager.get_session()
    try:
        params = {
            "source": "ranking",
            "run_id": "r1",
            "captured_epoch_s": 1000,
            "collection_tid": 1007,
        }
        params.update(kwargs)
        row = persist_snapshot(session, view_data, **params)
        session.commit()
        return row.video_id
    finally:
        session.close()


def test_missing_view_written_as_sql_null(manager):
    """stat.view 缺失时 videos.view 与 video_stats.view 必须都是 SQL NULL。"""
    stat = {"danmaku": 1, "reply": 2, "favorite": 3, "coin": 4, "share": 5, "like": 6}
    video_id = _write(manager, _view_data(stat=stat))

    check = manager.get_session()
    try:
        video_view = check.execute(
            text("SELECT view FROM videos WHERE bvid = 'BV1test'")
        ).scalar()
        stat_row = check.execute(
            text("SELECT view, view_status, stat_status FROM video_stats WHERE video_id = :v"),
            {"v": video_id},
        ).one()
    finally:
        check.close()

    assert video_view is None
    assert stat_row[0] is None
    assert stat_row[1] == "missing"
    assert stat_row[2] == "partial"


def test_real_zero_view_stored_as_zero(manager):
    """stat.view=0 是真实值：两处都写 0，view_status='ok'。"""
    stat = {"view": 0, "danmaku": 1, "reply": 2, "favorite": 3, "coin": 4, "share": 5, "like": 6}
    video_id = _write(manager, _view_data(stat=stat), run_id=None, collection_tid=None)

    check = manager.get_session()
    try:
        video_view = check.execute(
            text("SELECT view FROM videos WHERE bvid = 'BV1test'")
        ).scalar()
        stat_row = check.execute(
            text("SELECT view, view_status FROM video_stats WHERE video_id = :v"),
            {"v": video_id},
        ).one()
    finally:
        check.close()

    assert video_view == 0
    assert stat_row[0] == 0
    assert stat_row[1] == "ok"


def test_partial_stats_keep_valid_view(manager):
    """view 合法、like 缺失：view 保留有效值，整条标 partial，不误杀播放。"""
    stat = {"view": 500, "danmaku": 1, "reply": 2, "favorite": 3, "coin": 4, "share": 5}
    video_id = _write(manager, _view_data(stat=stat))

    check = manager.get_session()
    try:
        row = check.query(VideoStats).filter(VideoStats.video_id == video_id).one()
        assert row.view == 500
        assert row.view_status == "ok"
        assert row.stat_status == "partial"
        assert row.like is None
        # metric_status 逐字段三态
        assert row.metric_status["view"] == "ok"
        assert row.metric_status["like"] == "missing"
    finally:
        check.close()


def test_persist_snapshot_does_not_commit_caller_transaction(manager):
    """同步内核不提交：caller rollback 后不应留下任何行。"""
    session = manager.get_session()
    try:
        persist_snapshot(
            session,
            _view_data(),
            source="ranking",
            run_id=None,
            captured_epoch_s=1,
            collection_tid=None,
        )
        session.rollback()
    finally:
        session.close()

    check = manager.get_session()
    try:
        videos = check.execute(text("SELECT COUNT(*) FROM videos")).scalar()
        stats = check.execute(text("SELECT COUNT(*) FROM video_stats")).scalar()
    finally:
        check.close()

    assert videos == 0
    assert stats == 0


def test_persist_snapshot_rejects_invalid_inputs(manager):
    """bvid 缺失、时间戳非法（含 bool）、source 空、run_id 超长都必须抛稳定错误。"""
    session = manager.get_session()
    try:
        for kwargs in (
            {"captured_epoch_s": True},
            {"captured_epoch_s": "100"},
        ):
            with pytest.raises(ValueError):
                persist_snapshot(
                    session, _view_data(), source="ranking", run_id=None,
                    captured_epoch_s=kwargs["captured_epoch_s"], collection_tid=None,
                )
        with pytest.raises(ValueError):
            persist_snapshot(
                session, _view_data(bvid=""), source="ranking", run_id=None,
                captured_epoch_s=1, collection_tid=None,
            )
        with pytest.raises(ValueError):
            persist_snapshot(
                session, _view_data(), source="   ", run_id=None,
                captured_epoch_s=1, collection_tid=None,
            )
        with pytest.raises(ValueError):
            persist_snapshot(
                session, _view_data(), source="ranking", run_id="x" * 65,
                captured_epoch_s=1, collection_tid=None,
            )
    finally:
        session.rollback()
        session.close()


class _StubCollector(HotspotCollector):
    """跳过真实 __init__，只记录信号的采集器替身。"""

    def __init__(self) -> None:
        """仅初始化信号记录列表。"""
        self.signals = []

    def _save_signal(self, source, tid, payload):
        """记录标题信号，返回成功条数。"""
        self.signals.append({"source": source, "tid": tid, "payload": payload})
        return 1


def test_legacy_async_wrapper_commits(manager, monkeypatch):
    """旧 async wrapper 在自有事务内提交，并返回 1 且落标题信号。"""
    monkeypatch.setattr(collector_module, "get_session", manager.get_session)

    collector = _StubCollector()
    saved = asyncio.run(
        collector._save_snapshot(_view_data(), source="ranking", collection_tid=1007, run_id="r1")
    )

    assert saved == 1
    assert len(collector.signals) == 1
    assert collector.signals[0]["source"] == "title"
    assert collector.signals[0]["payload"]["bvid"] == "BV1test"

    check = manager.get_session()
    try:
        stats = check.execute(text("SELECT COUNT(*) FROM video_stats")).scalar()
    finally:
        check.close()
    assert stats == 1
