"""core.database 底座测试（第1批补齐 · core 段）。

覆盖范围：
- DatabaseManager.__init__ / create_tables / get_session / drop_all_tables / backup
- DatabaseManager._migrate_comment_member_columns / _migrate_up_master_charge_count
  / _migrate_video_stats_columns
- api.init_database / get_session / get_db
- 包级 __all__ 兼容导出

测试策略：
- 数据库一律使用 tmp_path 下真实 SQLite（SQLAlchemy ORM），不使用 Mock session。
- 迁移分支用真实 sqlite3 造旧库（缺列），再验证 ALTER TABLE 补列，不做桩替身。
- get_db 的"请求结束自动 close"用记录型会话替身校验 finally 分支真实执行。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

import core.database as db_package
import core.database.api as db_api
from core.database import DatabaseManager, LLMUsage


# ---------------------------------------------------------------------------
# DatabaseManager 基础能力
# ---------------------------------------------------------------------------


def test_manager_creates_parent_dir_and_tables(tmp_path):
    """初始化应自动建父目录、建库文件与所有业务表。"""
    db_path = tmp_path / "nested" / "deep" / "ops.db"
    manager = DatabaseManager(str(db_path))
    assert db_path.exists()
    table_names = set(inspect(manager.engine).get_table_names())
    assert {"llm_usage", "comments", "monitor_state"} <= table_names


def test_session_roundtrip_persists_llm_usage(tmp_path):
    """通过真实会话写入并读回 LLMUsage 记录。"""
    manager = DatabaseManager(str(tmp_path / "ops.db"))
    session = manager.get_session()
    try:
        session.add(
            LLMUsage(
                date="2030-01-01",
                model="unit-model",
                prompt_tokens=3,
                completion_tokens=4,
                total_tokens=7,
                request_count=1,
                module="unit",
            )
        )
        session.commit()
        rows = session.query(LLMUsage).all()
        assert len(rows) == 1
        assert rows[0].total_tokens == 7
        assert rows[0].model == "unit-model"
    finally:
        session.close()


def test_create_tables_is_idempotent(tmp_path):
    """重复建表不抛异常（checkfirst 语义）。"""
    manager = DatabaseManager(str(tmp_path / "ops.db"))
    manager.create_tables()
    manager.create_tables()


def test_drop_all_tables_removes_schema(tmp_path):
    """drop_all_tables 后业务表应消失。"""
    manager = DatabaseManager(str(tmp_path / "ops.db"))
    manager.drop_all_tables()
    assert "llm_usage" not in set(inspect(manager.engine).get_table_names())


def test_backup_auto_and_explicit_path(tmp_path):
    """backup 支持自动时间戳命名与显式路径两种模式。"""
    manager = DatabaseManager(str(tmp_path / "ops.db"))
    auto_path = manager.backup()
    assert auto_path.exists()

    explicit = tmp_path / "manual_backup.db"
    returned = manager.backup(str(explicit))
    assert Path(returned) == explicit
    assert explicit.exists()


# ---------------------------------------------------------------------------
# 旧库迁移（真实缺列 SQLite → ALTER TABLE 补列）
# ---------------------------------------------------------------------------


def test_migrations_add_missing_columns_to_legacy_db(tmp_path):
    """对缺少新列的旧库，三个迁移方法应幂等补列。"""
    legacy_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(legacy_path)
    try:
        connection.execute("CREATE TABLE comments (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE up_masters (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE video_stats (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    manager = DatabaseManager(str(legacy_path))
    inspector = inspect(manager.engine)

    comment_columns = {column["name"] for column in inspector.get_columns("comments")}
    assert {"level_info", "vip"} <= comment_columns

    up_columns = {column["name"] for column in inspector.get_columns("up_masters")}
    assert "charge_count" in up_columns

    stat_columns = {column["name"] for column in inspector.get_columns("video_stats")}
    assert {"source", "run_id", "view_status", "stat_status"} <= stat_columns


# ---------------------------------------------------------------------------
# 模块级工厂函数
# ---------------------------------------------------------------------------


class _RecordingSession:
    """记录 close 次数的会话替身，用于验证 get_db 的 finally 分支。"""

    def __init__(self) -> None:
        """初始化关闭计数。"""
        self.closed = 0

    def close(self) -> None:
        """累加关闭次数。"""
        self.closed += 1


class _ManagerStub:
    """只暴露 get_session 的管理器替身。"""

    def __init__(self) -> None:
        """持有一个记录型会话。"""
        self.session = _RecordingSession()

    def get_session(self) -> _RecordingSession:
        """返回同一会话实例。"""
        return self.session


def test_init_database_sets_module_singleton(tmp_path, monkeypatch):
    """init_database 应返回管理器并写入模块级单例。"""
    monkeypatch.setattr(db_api, "db_manager", None)
    manager = db_api.init_database(str(tmp_path / "init.db"))
    assert isinstance(manager, DatabaseManager)
    assert db_api.db_manager is manager


def test_get_session_auto_initialises_manager(tmp_path, monkeypatch):
    """db_manager 为空时 get_session 应自动初始化。"""
    manager = DatabaseManager(str(tmp_path / "auto.db"))
    monkeypatch.setattr(db_api, "db_manager", None)

    def fake_init(db_path: str = "data/bili_ops.db"):
        """替身初始化：直接挂载 tmp 管理器，避免触碰仓库数据库。"""
        db_api.db_manager = manager
        return manager

    monkeypatch.setattr(db_api, "init_database", fake_init)
    session = db_api.get_session()
    try:
        assert isinstance(session, Session)
    finally:
        session.close()
    assert db_api.db_manager is manager


def test_get_db_yields_session_and_closes(monkeypatch):
    """get_db 生成器结束后必须关闭会话。"""
    stub = _ManagerStub()
    monkeypatch.setattr(db_api, "db_manager", stub)

    generator = db_api.get_db()
    session = next(generator)
    assert session is stub.session
    with pytest.raises(StopIteration):
        next(generator)
    assert stub.session.closed == 1


def test_get_db_auto_initialises_manager(monkeypatch):
    """db_manager 为空时 get_db 也应先初始化再产出会话。"""
    stub = _ManagerStub()
    monkeypatch.setattr(db_api, "db_manager", None)

    def fake_init(db_path: str = "data/bili_ops.db"):
        """替身初始化：挂载记录型管理器。"""
        db_api.db_manager = stub
        return stub

    monkeypatch.setattr(db_api, "init_database", fake_init)
    generator = db_api.get_db()
    session = next(generator)
    assert session is stub.session
    with pytest.raises(StopIteration):
        next(generator)
    assert stub.session.closed == 1


def test_package_exports_are_available():
    """core.database 对外导出符号必须齐全（兼容旧 import 路径）。"""
    expected = [
        "Base", "Account", "CookiePool", "Video", "VideoStats", "UPMaster",
        "Comment", "CommentAlert", "Hotspot", "Topic", "Activity", "HotspotSignal",
        "Task", "OperationLog", "LLMUsage", "MonitorState",
        "DatabaseManager", "init_database", "get_session", "get_db", "db_manager",
    ]
    assert set(expected) <= set(db_package.__all__)
    for name in expected:
        assert hasattr(db_package, name)
