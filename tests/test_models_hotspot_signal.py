"""core.database.models_hotspot_signal ORM 模型测试（第4批 · ORM 段）。

覆盖对象：
- HotspotSignal（hotspot_signal 表）

验证维度：
字段类型与长度 / NOT NULL 约束 / value 的 dict 可调用默认 / 索引定义 / JSON 往返。

测试策略：
- 内存 SQLite 真实建表，不使用 Mock session。
- 通过 ``inspect(...).get_indexes`` 从外部验证复合索引与单列索引真实生效。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database.base import Base
from core.database.models_hotspot_signal import HotspotSignal


@pytest.fixture()
def session():
    """内存 SQLite 全量建表，产出独立会话并在结束后释放引擎。"""
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


def test_table_name_and_column_lengths(session):
    """表名、bvid/source 等列长度与可空性符合定义。"""
    assert HotspotSignal.__tablename__ == "hotspot_signal"
    columns = HotspotSignal.__table__.c
    assert columns.bvid.type.length == 20
    assert columns.source.type.length == 30
    assert columns.tid.nullable is False
    assert columns.bvid.nullable is False
    assert columns.source.nullable is False
    assert columns.value.nullable is False


def test_indexes_declared_and_created(session):
    """单列索引与复合索引 (tid, collected_at) 均应真实建立。"""
    index_names = {index["name"] for index in inspect(session.get_bind()).get_indexes("hotspot_signal")}
    assert "ix_hotspot_signal_tid_time" in index_names
    assert "ix_hotspot_signal_tid" in index_names
    assert "ix_hotspot_signal_bvid" in index_names


def test_default_values_applied(session):
    """collected_at 默认当前时间，value 默认独立空字典。"""
    signal = HotspotSignal(tid=4, bvid="BV1SIG00001", source="title")
    session.add(signal)
    session.commit()

    assert isinstance(signal.collected_at, datetime)
    assert signal.value == {}
    assert signal.value is not HotspotSignal.value


def test_value_json_roundtrip(session):
    """带 source 元数据的结构化 value 应原样往返。"""
    payload = {"source": "title", "tokens": ["原神", "攻略"], "score": 0.9}
    session.add(
        HotspotSignal(tid=4, bvid="BV1SIG00002", source="title", value=payload)
    )
    session.commit()
    session.expire_all()

    loaded = session.query(HotspotSignal).filter(HotspotSignal.bvid == "BV1SIG00002").one()
    assert loaded.value == payload
    assert loaded.source == "title"


def test_not_null_constraints_enforced(session):
    """缺失 tid / value 时应被数据库拒绝（value 有默认，故仅测 tid 缺失）。"""
    session.add(HotspotSignal(bvid="BV1SIG00003", source="comment"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_multiple_signals_same_tid_different_time(session):
    """同一 tid 可写入多条不同采集时间信号，验证索引列可查询。"""
    session.add_all(
        [
            HotspotSignal(tid=4, bvid="BV1SIG00004", source="title", value={"n": 1}),
            HotspotSignal(tid=4, bvid="BV1SIG00004", source="comment", value={"n": 2}),
        ]
    )
    session.commit()

    rows = (
        session.query(HotspotSignal)
        .filter(HotspotSignal.tid == 4, HotspotSignal.bvid == "BV1SIG00004")
        .all()
    )
    assert len(rows) == 2
    assert {row.source for row in rows} == {"title", "comment"}
