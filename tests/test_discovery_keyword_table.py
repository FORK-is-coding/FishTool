"""06 采集广度 · hot_keyword_signal 建表与注册验收（规格 §5.2）。"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

import core.database as database_pkg
from core.database import DatabaseManager, HotKeywordSignal


@pytest.fixture()
def manager(tmp_path):
    """隔离临时库，绝不触碰仓库 data/。"""
    return DatabaseManager(str(tmp_path / "discovery.db"))


def _table_names(manager: DatabaseManager) -> set:
    """读取库中全部表名。"""
    session = manager.get_session()
    try:
        return {
            row[0]
            for row in session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    finally:
        session.close()


def test_table_created_via_create_all(manager) -> None:
    """显式注册后 create_all 才会建表；重复建表幂等。"""
    manager.create_tables()
    manager.create_tables()
    assert "hot_keyword_signal" in _table_names(manager)


def test_registered_in_package_namespace_and_all() -> None:
    """必须在 core/database/__init__.py 显式导入并列入 __all__。"""
    assert database_pkg.HotKeywordSignal is HotKeywordSignal
    assert "HotKeywordSignal" in database_pkg.__all__


def test_columns_lengths_and_nullability() -> None:
    """heat_score 可空（缺失写 NULL）；keyword / captured_epoch_s / source 非空。"""
    columns = HotKeywordSignal.__table__.c
    assert HotKeywordSignal.__tablename__ == "hot_keyword_signal"
    assert columns.keyword.type.length == 100
    assert columns.keyword.nullable is False
    assert columns.heat_score.nullable is True
    assert columns.heat_status.nullable is False
    assert columns.captured_epoch_s.nullable is False
    assert columns.source.nullable is False


def test_unique_constraint_on_keyword_and_epoch() -> None:
    """唯一约束 (keyword, captured_epoch_s)：同一响应重放幂等。"""
    unique_names = {
        constraint.name
        for constraint in HotKeywordSignal.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert "uq_hot_keyword_signal_keyword_epoch" in unique_names


def test_indexes_created(manager) -> None:
    """keyword / captured_epoch_s 单列索引与 (source, captured_epoch_s) 复合索引真实建立。"""
    manager.create_tables()
    index_names = {index["name"] for index in inspect(manager.engine).get_indexes("hot_keyword_signal")}
    assert "ix_hot_keyword_signal_keyword" in index_names or "ix_hot_keyword_signal_keyword_epoch" in index_names
    assert "ix_hot_keyword_signal_source_time" in index_names


def test_missing_score_stored_as_null(manager) -> None:
    """heat_score 缺失写 SQL NULL，绝不写 0。"""
    session = manager.get_session()
    try:
        session.add(
            HotKeywordSignal(
                keyword="缺失分数的词",
                heat_score=None,
                heat_status="missing",
                rank=1,
                captured_epoch_s=100,
                source="search_square",
                snapshot_id="snap-1",
            )
        )
        session.commit()
        row = session.query(HotKeywordSignal).one()
        assert row.heat_score is None
        assert row.heat_status == "missing"
        assert row.snapshot_id == "snap-1"
    finally:
        session.close()


def test_duplicate_keyword_same_epoch_rejected(manager) -> None:
    """同一 (keyword, captured_epoch_s) 第二次写入被唯一约束拒绝。"""
    session = manager.get_session()
    try:
        session.add(HotKeywordSignal(keyword="重复词", heat_score=1, heat_status="ok", rank=1, captured_epoch_s=7, source="search_square"))
        session.commit()
        session.add(HotKeywordSignal(keyword="重复词", heat_score=2, heat_status="ok", rank=2, captured_epoch_s=7, source="search_square"))
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_same_keyword_new_epoch_allowed(manager) -> None:
    """同一词跨时刻是新观察，允许新增（用于热度曲线）。"""
    session = manager.get_session()
    try:
        session.add(HotKeywordSignal(keyword="持续热点", heat_score=1, heat_status="ok", rank=1, captured_epoch_s=10, source="search_square"))
        session.add(HotKeywordSignal(keyword="持续热点", heat_score=2, heat_status="ok", rank=1, captured_epoch_s=20, source="search_square"))
        session.commit()
        assert session.query(HotKeywordSignal).count() == 2
    finally:
        session.close()
