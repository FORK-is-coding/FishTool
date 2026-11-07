"""core.database 底座测试（第1批补齐 · core 段）。

覆盖范围：
- DatabaseManager.__init__ / create_tables / get_session / drop_all_tables / backup
- DatabaseManager._migrate_comment_member_columns / _migrate_up_master_charge_count
  / _migrate_video_stats_columns / _migrate_cookie_pool_plaintext_credentials
- api.init_database / get_session / get_db
- 包级 __all__ 兼容导出

测试策略：
- 数据库一律使用 tmp_path 下真实 SQLite（SQLAlchemy ORM），不使用 Mock session。
- 迁移分支用真实 sqlite3 造旧库（缺列），再验证 ALTER TABLE 补列，不做桩替身。
- get_db 的"请求结束自动 close"用记录型会话替身校验 finally 分支真实执行。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import core.database as db_package
import core.database.api as db_api
from core.database import Comment, DatabaseManager, LLMUsage, Video


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
        connection.execute(
            "CREATE TABLE video_stats (id INTEGER PRIMARY KEY, video_id INTEGER)"
        )
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
    assert {
        "source", "run_id", "view_status", "stat_status",
        "captured_epoch_s", "collection_tid", "raw_tid", "metric_status",
    } <= stat_columns

    index_names = {index["name"] for index in inspector.get_indexes("video_stats")}
    assert "ix_video_stats_video_captured" in index_names


def sessdata_digest(sessdata: str) -> str:
    """独立计算 cookie_pool.sessdata 列应存的摘要（不复用生产实现）。"""
    return "sha256:" + hashlib.sha256(sessdata.encode("utf-8")).hexdigest()


def _create_legacy_cookie_pool(path):
    """用真实 sqlite3 造旧版 cookie_pool 表，sessdata/bili_jct 列为明文。"""
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE cookie_pool ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, "
            "cookie_data TEXT NOT NULL, sessdata VARCHAR(100), bili_jct VARCHAR(100), "
            "buvid3 VARCHAR(100), is_valid BOOLEAN, fail_count INTEGER, "
            "last_used DATETIME, last_check DATETIME, created_at DATETIME)"
        )
        connection.executemany(
            "INSERT INTO cookie_pool (account_id, cookie_data, sessdata, bili_jct, buvid3) "
            "VALUES (1, ?, ?, ?, ?)",
            [
                ("enc-1", "plain-sess", "plain-jct", "b1"),
                ("enc-2", sessdata_digest("already"), "", "b2"),
                ("enc-3", None, None, "b3"),
                # 不可解码的 BLOB：迁移不能崩溃，也不能原样保留
                ("enc-4", b"\xff\xfe\x00", "jct-4", "b4"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _cookie_pool_rows(path):
    """读取 cookie_pool 的 (id, sessdata, bili_jct, buvid3, cookie_data)，按 id 排序。"""
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT id, sessdata, bili_jct, buvid3, cookie_data FROM cookie_pool ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def test_cookie_pool_migration_hashes_plaintext_and_clears_bili_jct(tmp_path):
    """旧库明文 sessdata 改写为 sha256 摘要、bili_jct 清空，其余列不动。"""
    legacy_path = tmp_path / "legacy_cookie.db"
    _create_legacy_cookie_pool(legacy_path)

    DatabaseManager(str(legacy_path))

    assert _cookie_pool_rows(legacy_path) == [
        (1, sessdata_digest("plain-sess"), "", "b1", "enc-1"),
        (2, sessdata_digest("already"), "", "b2", "enc-2"),
        (3, None, None, "b3", "enc-3"),
        (4, "", "", "b4", "enc-4"),
    ]
    assert sessdata_digest("plain-sess").startswith("sha256:")


def test_cookie_pool_migration_is_idempotent(tmp_path):
    """第二次构造零改动：已是摘要的值不会被二次摘要。"""
    legacy_path = tmp_path / "legacy_cookie.db"
    _create_legacy_cookie_pool(legacy_path)

    DatabaseManager(str(legacy_path))
    first = _cookie_pool_rows(legacy_path)
    DatabaseManager(str(legacy_path))
    second = _cookie_pool_rows(legacy_path)

    assert second == first
    assert first[0][1] == sessdata_digest("plain-sess")
    assert first[0][1] != sessdata_digest(sessdata_digest("plain-sess"))


def test_cookie_pool_migration_skips_missing_table(tmp_path):
    """没有 cookie_pool 表时迁移直接跳过，不报错。"""
    manager = DatabaseManager(str(tmp_path / "ops.db"))
    with manager.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE cookie_pool")

    manager._migrate_cookie_pool_plaintext_credentials()

    assert "cookie_pool" not in set(inspect(manager.engine).get_table_names())


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


def test_task_key_migration_is_idempotent_and_keeps_existing_rows(tmp_path):
    """旧库 tasks 表缺 task_key：迁移补列 + 唯一索引，旧记录保留且可重复迁移。"""
    legacy_path = tmp_path / "legacy_tasks.db"
    connection = sqlite3.connect(legacy_path)
    try:
        connection.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, task_type VARCHAR(50), task_name VARCHAR(200), "
            "params JSON, status VARCHAR(20), progress FLOAT, result JSON, error TEXT, checkpoint JSON, "
            "started_at DATETIME, completed_at DATETIME, created_at DATETIME)"
        )
        connection.execute("INSERT INTO tasks (task_type, status) VALUES ('comment_collect', 'completed')")
        connection.commit()
    finally:
        connection.close()

    manager = DatabaseManager(str(legacy_path))
    manager._migrate_task_key_column()  # 再跑一次仍无异常
    inspector = inspect(manager.engine)
    assert "task_key" in {column["name"] for column in inspector.get_columns("tasks")}
    assert "ux_tasks_task_key" in {index["name"] for index in inspector.get_indexes("tasks")}
    with sqlite3.connect(legacy_path) as raw:
        assert raw.execute("SELECT COUNT(*) FROM tasks WHERE task_key IS NULL").fetchone()[0] == 1
    manager.engine.dispose()


# ---------------------------------------------------------------------------
# SQLite 外键约束（#82）
# ---------------------------------------------------------------------------


def test_foreign_keys_pragma_enabled_on_connection(tmp_path):
    """建连 hook 后每条连接都必须处于 PRAGMA foreign_keys=ON。"""
    manager = DatabaseManager(str(tmp_path / "ops.db"))

    with manager.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

    session = manager.get_session()
    try:
        assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        session.close()


def test_comment_with_missing_parent_is_rejected(tmp_path):
    """video_id 指向不存在的视频时插入必须被拒绝（不会写入孤儿行）。"""
    manager = DatabaseManager(str(tmp_path / "ops.db"))
    session = manager.get_session()
    try:
        session.add(Comment(rpid="r-orphan", video_id=424242, content="孤儿评论"))
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_parent_delete_rejected_while_child_exists(tmp_path):
    """schema 未声明 ON DELETE，裸 SQL 删除仍被引用的父行必须被拒绝。

    走 SQL 而不是 ``session.delete``：ORM 关系上声明了 ``cascade="all, delete-orphan"``，
    会先删子行再删父行，测不到数据库层面的外键约束。
    """
    manager = DatabaseManager(str(tmp_path / "ops.db"))
    session = manager.get_session()
    try:
        video = Video(bvid="BV1test0001", title="父行")
        session.add(video)
        session.commit()
        session.add(Comment(rpid="r-1", video_id=video.id, content="子行"))
        session.commit()

        with pytest.raises(IntegrityError):
            session.execute(text("DELETE FROM videos WHERE id = :id"), {"id": video.id})
            session.commit()
    finally:
        session.rollback()
        session.close()

    session = manager.get_session()
    try:
        assert session.query(Video).filter_by(bvid="BV1test0001").count() == 1
        assert session.query(Comment).filter_by(rpid="r-1").count() == 1
    finally:
        session.close()


def test_legacy_orphan_rows_do_not_break_startup(tmp_path):
    """历史库里的孤儿行不影响启动，也不会被静默删除（避免误删仍可用的凭据/评论）。"""
    db_path = tmp_path / "ops.db"
    DatabaseManager(str(db_path))  # 先建全量 schema

    raw = sqlite3.connect(db_path)
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "INSERT INTO comments (rpid, video_id, content) VALUES ('orphan-1', 987654, '孤儿')"
        )
        raw.commit()
        assert raw.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 1
    finally:
        raw.close()

    DatabaseManager(str(db_path))  # 重新打开：体检告警而不是启动失败

    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 1
    finally:
        raw.close()
