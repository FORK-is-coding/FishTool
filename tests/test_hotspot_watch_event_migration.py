"""P4 · hotspot_watch 04 事件列迁移幂等（FishTool 04 · R5 前置）。

被测：``core/database/manager.py::DatabaseManager._migrate_hotspot_watch_event_columns``。

三个独立库各跑一遍（参数化 ``lib_a`` / ``lib_b`` / ``lib_c``），每库都断言：
1. 全新空库：建库后四列都在；
2. 旧库（02 模型定义、已有 ``sample_interval_s``）：跑完不报错、``sample_interval_s`` 不被重复
   ALTER（``PRAGMA table_info`` 列名不重复；再跑一次仍无异常）；
3. 重复启动：同一库连跑两次迁移，第二次零改动、无异常；
4. 旧记录默认值：``manual_pinned`` 落 0、``fast_until_s`` / ``source_demands`` 为 NULL，
   且不臆造 ``source_demands`` 内容；
5. 表不存在时不盲 ALTER（先建再接）。

只碰临时库，不触网、不读密钥。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from core.database import DatabaseManager
from core.database.models_hotspot_watch import HotspotWatch

#: 04 事件列（四列齐备即为迁移目标达成）。
FOUR_COLUMNS = ("sample_interval_s", "fast_until_s", "manual_pinned", "source_demands")


def _columns(engine) -> list:
    """读取 ``hotspot_watch`` 的列名列表（PRAGMA table_info，按表定义顺序）。"""
    with engine.connect() as conn:
        rows = conn.execute(text('PRAGMA table_info("hotspot_watch")')).all()
    return [str(row[1]) for row in rows]


@pytest.fixture(params=["lib_a", "lib_b", "lib_c"])
def db_path(request, tmp_path):
    """三个互相独立的新库路径：lib_a / lib_b / lib_c 各跑一遍全套断言。"""
    return tmp_path / f"{request.param}.db"


def _build_legacy_db(db_path) -> None:
    """构造 02 口径旧库：表已存在（含 ``sample_interval_s``），缺 04 三列，并插一条旧记录。

    关键：这里只用 02 的 ``HotspotWatch.__table__`` 建表 —— 它**不含**
    ``fast_until_s`` / ``manual_pinned`` / ``source_demands``，因此能把「迁移补列」这条
    路径真正跑起来（而不是让 create_all 新表天然全列、迁移退化成空操作）。
    """
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        HotspotWatch.__table__.create(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO hotspot_watch "
                    "(bvid, first_seen_epoch_s, ttl_end_epoch_s, next_due_epoch_s) "
                    "VALUES ('BVlegacy', 1000, 9999999999, 1000)"
                )
            )
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- 1. 全新空库


def test_fresh_db_builds_all_four_columns(db_path) -> None:
    """全新空库：建库后四列齐备、``sample_interval_s`` 不重复。"""
    mgr = DatabaseManager(str(db_path))
    try:
        assert inspect(mgr.engine).has_table("hotspot_watch")
        cols = _columns(mgr.engine)
        missing = [name for name in FOUR_COLUMNS if name not in cols]
        assert missing == [], f"缺列: {missing}"
        assert cols.count("sample_interval_s") == 1
    finally:
        mgr.engine.dispose()


# --------------------------------------------------------------------------- 2. 旧库补列


def test_legacy_db_sample_interval_s_not_realtered(db_path) -> None:
    """旧库（已有 sample_interval_s）：补 04 三列、不重复 ALTER、再跑一次仍无异常。"""
    _build_legacy_db(db_path)
    mgr = DatabaseManager(str(db_path))
    try:
        cols = _columns(mgr.engine)
        missing = [name for name in FOUR_COLUMNS if name not in cols]
        assert missing == [], f"缺列: {missing}"
        # 若对已有列再次 ALTER，SQLite 会抛 duplicate column name；这里必须只有一列。
        assert cols.count("sample_interval_s") == 1

        # 4. 旧记录默认值：manual_pinned=0、fast_until_s / source_demands 为 NULL。
        with mgr.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT manual_pinned, fast_until_s, source_demands "
                    "FROM hotspot_watch WHERE bvid='BVlegacy'"
                )
            ).one()
        assert row[0] == 0
        assert row[1] is None
        assert row[2] is None

        # 再跑一次迁移：仍无异常、列集不变。
        mgr._migrate_hotspot_watch_event_columns()
        assert _columns(mgr.engine) == cols
    finally:
        mgr.engine.dispose()


# --------------------------------------------------------------------------- 3. 重复启动


def test_repeat_startup_is_idempotent(db_path) -> None:
    """同一库连跑两次迁移：第二次零改动、无异常。"""
    mgr1 = DatabaseManager(str(db_path))
    first = _columns(mgr1.engine)
    mgr1.engine.dispose()

    mgr2 = DatabaseManager(str(db_path))
    try:
        second = _columns(mgr2.engine)
        assert second == first, "重复启动第二次迁移产生了列改动"
        # 显式再跑一次：仍无异常、列不变。
        mgr2._migrate_hotspot_watch_event_columns()
        assert _columns(mgr2.engine) == first
    finally:
        mgr2.engine.dispose()


# --------------------------------------------------------------------------- 5. 表不存在


def test_missing_table_is_created_not_blind_altered(db_path) -> None:
    """表不存在：迁移必须先建再接，绝不盲 ALTER 不存在的表（盲 ALTER 会直接抛错）。"""
    mgr = DatabaseManager(str(db_path))
    try:
        HotspotWatch.__table__.drop(mgr.engine)
        assert inspect(mgr.engine).has_table("hotspot_watch") is False

        mgr._migrate_hotspot_watch_event_columns()

        assert inspect(mgr.engine).has_table("hotspot_watch") is True
        cols = _columns(mgr.engine)
        missing = [name for name in FOUR_COLUMNS if name not in cols]
        assert missing == [], f"缺列: {missing}"
    finally:
        mgr.engine.dispose()
