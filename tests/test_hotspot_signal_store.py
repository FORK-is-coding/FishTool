"""热点信号存储服务的契约级测试。

覆盖 modules/hotspot/signal_store.py 的 HotspotSignalStore：
- save_many：结构化包装、字段兜底、返回条数、30 天滚动裁剪
- save_many：写库失败回滚并向上抛异常
- list_recent：时间倒序、tid 过滤、limit 上下夹取

数据库一律使用 tmp_path 下的真实 SQLite（SQLAlchemy ORM），
通过 monkeypatch 把模块级 get_session 指向临时库，绝不触碰仓库 data/。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.database import DatabaseManager, HotspotSignal
from modules.hotspot import signal_store as store_module
from modules.hotspot.signal_store import HotspotSignalStore


# --------------------------------------------------------------------- 夹具

@pytest.fixture()
def env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """构造真实临时库 + 信号存储服务。"""
    manager = DatabaseManager(str(tmp_path / "signal.db"))
    monkeypatch.setattr(store_module, "get_session", manager.get_session)
    return HotspotSignalStore(), manager


def _all_signals(manager: DatabaseManager) -> list[HotspotSignal]:
    """读取临时库中的全部信号行。"""
    session = manager.get_session()
    try:
        return session.query(HotspotSignal).all()
    finally:
        session.close()


class _BrokenSession:
    """add 即失败的会话替身，用于验证回滚与异常冒泡。"""

    def __init__(self) -> None:
        self.rolled_back = False
        self.closed = False

    def add(self, obj):
        """模拟写库失败。"""
        raise RuntimeError("db down")

    def query(self, *args, **kwargs):
        """失败路径不应触达查询。"""
        raise AssertionError("失败路径不应执行查询")

    def commit(self):
        """失败路径不应提交。"""
        raise AssertionError("失败路径不应提交")

    def rollback(self) -> None:
        """记录回滚。"""
        self.rolled_back = True

    def close(self) -> None:
        """记录关闭。"""
        self.closed = True


# --------------------------------------------------------------------- save_many

def test_save_many_returns_count_and_wraps_payload(env) -> None:
    """保存后应返回条数，并把 value 包成带 source 元数据的结构。"""
    store, manager = env

    count = store.save_many([
        {"tid": 4, "bvid": "BV1", "source": "title", "value": {"keywords": ["a"]}},
        {"tid": 5, "bvid": "BV2", "source": "comment", "value": {"keywords": []}},
    ])

    assert count == 2
    rows = _all_signals(manager)
    title_row = next(row for row in rows if row.source == "title")
    assert title_row.tid == 4
    assert title_row.bvid == "BV1"
    assert title_row.value == {"source": "title", "payload": {"keywords": ["a"]}}


def test_save_many_applies_field_defaults(env) -> None:
    """缺失 source/tid/bvid/collected_at 时应给出契约默认值。"""
    store, manager = env

    store.save_many([{}])

    row = _all_signals(manager)[0]
    assert row.source == "unknown"
    assert row.tid == 0
    assert row.bvid == ""
    assert isinstance(row.collected_at, datetime)
    assert row.value == {"source": "unknown", "payload": {}}


def test_save_many_keeps_explicit_collected_at(env) -> None:
    """显式传入的 collected_at 应原样入库。"""
    store, manager = env
    moment = datetime(2026, 3, 4, 5, 6, 7)

    store.save_many([{"tid": 1, "bvid": "BVX", "source": "tag", "collected_at": moment}])

    assert _all_signals(manager)[0].collected_at == moment


def test_save_many_empty_list_still_succeeds(env) -> None:
    """空列表返回 0，且不产生任何行。"""
    store, manager = env

    assert store.save_many([]) == 0
    assert _all_signals(manager) == []


def test_save_many_trims_signals_older_than_30_days(env) -> None:
    """超过 30 天的历史信号应被滚动裁剪。"""
    store, manager = env
    # 直接插入一条 40 天前的旧信号。
    session = manager.get_session()
    try:
        session.add(
            HotspotSignal(
                tid=4,
                bvid="BVOLD",
                collected_at=datetime.now() - timedelta(days=40),
                source="title",
                value={},
            )
        )
        session.commit()
    finally:
        session.close()

    store.save_many([{"tid": 4, "bvid": "BVNEW", "source": "title"}])

    rows = _all_signals(manager)
    assert [row.bvid for row in rows] == ["BVNEW"]


def test_save_many_keeps_recent_history_within_30_days(env) -> None:
    """30 天内的历史信号不应被误删。"""
    store, manager = env
    session = manager.get_session()
    try:
        session.add(
            HotspotSignal(
                tid=4,
                bvid="BVRECENT",
                collected_at=datetime.now() - timedelta(days=1),
                source="title",
                value={},
            )
        )
        session.commit()
    finally:
        session.close()

    store.save_many([{"tid": 4, "bvid": "BV2", "source": "title"}])

    assert {row.bvid for row in _all_signals(manager)} == {"BVRECENT", "BV2"}


def test_save_many_rolls_back_and_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """写库失败应回滚、关闭会话并把异常继续抛出。"""
    broken = _BrokenSession()
    monkeypatch.setattr(store_module, "get_session", lambda: broken)

    with pytest.raises(RuntimeError):
        HotspotSignalStore().save_many([{"tid": 1, "bvid": "BV1", "source": "title"}])

    assert broken.rolled_back is True
    assert broken.closed is True


# --------------------------------------------------------------------- list_recent

def test_list_recent_returns_newest_first(env) -> None:
    """list_recent 应按采集时间倒序返回。"""
    store, manager = env
    # 基准时间取当前时间，避免被 save_many 的 30 天裁剪规则删除。
    base = datetime.now()
    for index in range(3):
        store.save_many([{"tid": 4, "bvid": f"BV{index}", "source": "title", "collected_at": base + timedelta(minutes=index)}])

    rows = store.list_recent()

    assert [row.bvid for row in rows] == ["BV2", "BV1", "BV0"]


def test_list_recent_filters_by_tid(env) -> None:
    """传入 tid 时只返回该分区信号。"""
    store, _ = env
    store.save_many([
        {"tid": 4, "bvid": "BV_GAME", "source": "title"},
        {"tid": 1, "bvid": "BV_ANIME", "source": "title"},
    ])

    rows = store.list_recent(tid=1)

    assert [row.bvid for row in rows] == ["BV_ANIME"]


def test_list_recent_limit_is_lower_clamped(env) -> None:
    """limit 小于 1 时至少返回 1 条（max(1, limit)）。"""
    store, _ = env
    store.save_many([{"tid": 4, "bvid": f"BV{index}", "source": "title"} for index in range(3)])

    assert len(store.list_recent(limit=0)) == 1
    assert len(store.list_recent(limit=-5)) == 1


def test_list_recent_applies_limit(env) -> None:
    """limit 正常范围内应限制返回条数。"""
    store, _ = env
    store.save_many([{"tid": 4, "bvid": f"BV{index}", "source": "title"} for index in range(5)])

    assert len(store.list_recent(limit=2)) == 2


def test_list_recent_empty_returns_empty_list(env) -> None:
    """没有信号时返回空列表而不是 None。"""
    store, _ = env

    assert store.list_recent() == []
