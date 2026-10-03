"""FishTool 04 · R5 第四批 f：缺口② 供给映射的**集成证明**（真临时 SQLite）。

现状已核清（不是没接线，是没集成证明）：
``service.py`` 定义 ``supply_members_from_counters()``，且 ``discovery_run_view()`` 内已在调用
``supply_members=supply_members_from_counters(counters)``。本文件补的是**真库→三件套**的端到端证明。

覆盖两点（对应规格 §三 §3.2）：

1. 插 ``event_discovery_runs`` 行（``counters`` 带 ``supply_members``）→ ``discovery_run_view(row)``
   → ``discovery_signals(view)`` → 三件套**真出数**（``angle_share`` 非 None、
   ``angle_coverage`` 与 ``unknown_angle_count`` 对得上）；
2. 边界：``counters`` 缺 ``supply_members`` 键 / 条目非法 / 缺 BVID → **跳过并安全降级**
   （``angle_share`` 全 None + 原因码，不抛异常、不填 0）。

红线：**不改 ``supply.py`` 公式、不改 ``channel_b.py``** —— 缺什么补测试，不去调公式。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import DatabaseManager
from core.database.models_hot_event import EventDiscoveryRun, HotEvent
from modules.hotspot.events.channel_b import discovery_signals
from modules.hotspot.events.service import discovery_run_view

#: 事件 ID（发现 run 挂在它下面）。
EVENT: str = "ev-supply"


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：经 ``DatabaseManager`` 建全部表 → 换可跨线程引擎产出会话工厂。"""
    path = tmp_path / "supply_integration.db"
    manager = DatabaseManager(str(path))
    manager.engine.dispose()
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _seed_event(db) -> None:
    """建一个 active 事件，供发现 run 的 event_id 引用。"""
    session = db()
    try:
        session.add(
            HotEvent(id=EVENT, name="发现事件", created_s=0, updated_s=0,
                    status="active", revision=0)
        )
        session.commit()
    finally:
        session.close()


def _insert_run(db, run_id: str, counters, *, status: str = "completed") -> None:
    """插入一条 ``event_discovery_runs`` 行（``counters`` 原样落 JSON 列）。"""
    session = db()
    try:
        session.add(
            EventDiscoveryRun(
                id=run_id, event_id=EVENT, rule_version=1, source_policy_hash="h",
                lease_token="tok", trigger="scheduled", started_s=0, finished_s=7200,
                status=status, counters=counters,
            )
        )
        session.commit()
    finally:
        session.close()


def _view(db, run_id: str):
    """在会话内把行归一为 ``DiscoveryRunView``（避免 ORM 行脱管后读属性报错）。"""
    session = db()
    try:
        row = session.get(EventDiscoveryRun, run_id)
        return discovery_run_view(row)
    finally:
        session.close()


def _angle_density(db, run_id: str) -> dict:
    """行 → 视图 → 通道 B 信号 → 三件套字典。"""
    return discovery_signals(_view(db, run_id))["angle_density"]


# ===========================================================================
# 1. 真出数：counters 带 supply_members → 三件套不再全 None
# ===========================================================================


def test_supply_members_mapping_yields_real_angle_density(db) -> None:
    """真库→``discovery_run_view``→``discovery_signals``：三件套**真出数**且自洽。"""
    _seed_event(db)
    counters = {
        "window_start_s": 0,
        "window_end_s": 7200,
        "interval_s": 7200,
        "query_count": 1,
        "page_count": 1,
        "query_order": ["q"],
        "supply_members": [
            {"bvid": "BV1", "format": "long_video", "angle": "news",
             "content_depth": "provided_transcript", "classification_source": "manual"},
            {"bvid": "BV2", "format": "long_video", "angle": "tutorial",
             "content_depth": "provided_transcript", "classification_source": "manual"},
            {"bvid": "BV3", "format": "short_video", "angle": "review",
             "content_depth": "title_description", "classification_source": "strict_rule"},
            {"bvid": "BV4", "format": "long_video", "angle": "unclassified",
             "content_depth": "title_only", "classification_source": "strict_rule"},
        ],
    }
    _insert_run(db, "run-ok", counters)

    view = _view(db, "run-ok")
    assert len(view.supply_members) == 4  # 4 条都映射成功（含 unclassified）

    angle = _angle_density(db, "run-ok")

    # ---- 三件套真出数（不再是全 None）----
    assert angle["member_count"] == 4
    assert angle["classified_count"] == 3  # unclassified 只说明无法分类，不进 |C|
    assert angle["unknown_angle_count"] == 1
    assert angle["angle_coverage"] == pytest.approx(3 / 4)
    assert angle["angle_share"]["news"] == pytest.approx(1 / 3)
    assert angle["angle_share"]["tutorial"] == pytest.approx(1 / 3)
    assert angle["angle_share"]["review"] == pytest.approx(1 / 3)

    # ---- 自洽：coverage 与 unknown_count 对得上 ----
    assert angle["angle_coverage"] == pytest.approx(
        angle["classified_count"] / angle["member_count"]
    )
    assert angle["unknown_angle_count"] == angle["member_count"] - angle["classified_count"]
    assert any(v is not None for v in angle["angle_share"].values())
    assert "no_classifiable_angle" not in angle["reason_codes"]


# ===========================================================================
# 2. 边界：安全降级（全 None + 原因码，不抛异常、不填 0）
# ===========================================================================


def test_missing_supply_members_key_degrades_safely(db) -> None:
    """``counters`` 缺 ``supply_members`` 键 → 全 None + 原因码，不抛异常、不填 0。"""
    _seed_event(db)
    _insert_run(db, "run-missing", {"query_count": 1})

    view = _view(db, "run-missing")
    assert view.supply_members == ()

    angle = _angle_density(db, "run-missing")
    assert angle["member_count"] == 0
    assert angle["classified_count"] == 0
    assert angle["angle_coverage"] is None  # 不填 0
    assert all(v is None for v in angle["angle_share"].values())  # 全 None，不填 0
    assert "no_classifiable_angle" in angle["reason_codes"]


def test_illegal_entries_are_skipped_to_safe_degrade(db) -> None:
    """非法条目 / 缺唯一 BVID → 逐条跳过（不抛异常），整跑安全降级。"""
    _seed_event(db)
    counters = {
        "supply_members": [
            "not-a-mapping",                                # 非映射 → 跳过
            {"bvid": "", "angle": "news"},                  # 缺唯一 BVID → 跳过
            {"bvid": "BVbad", "angle": "not_a_real_angle"},  # 非法枚举 → 跳过
        ]
    }
    _insert_run(db, "run-bad", counters)

    view = _view(db, "run-bad")
    assert view.supply_members == ()  # 全部被跳过，未拖垮整跑

    angle = _angle_density(db, "run-bad")
    assert angle["member_count"] == 0
    assert all(v is None for v in angle["angle_share"].values())  # 全 None，不填 0
    assert "no_classifiable_angle" in angle["reason_codes"]


def test_angle_density_moves_from_all_none_to_real_numbers(db) -> None:
    """前后对比：空 counters → 三件套全 None；补上 ``supply_members`` → 三件套出数。"""
    _seed_event(db)

    # ---- 前：无供给成员 → 全 None ----
    _insert_run(db, "run-empty", {})
    empty = _angle_density(db, "run-empty")
    assert all(v is None for v in empty["angle_share"].values())
    assert empty["angle_coverage"] is None
    assert empty["member_count"] == 0

    # ---- 后：有供给成员 → 出数 ----
    _insert_run(db, "run-full", {
        "supply_members": [
            {"bvid": "BV1", "angle": "news"},
            {"bvid": "BV2", "angle": "tutorial"},
        ]
    })
    full = _angle_density(db, "run-full")
    assert full["member_count"] == 2
    assert full["classified_count"] == 2
    assert full["angle_coverage"] == pytest.approx(1.0)
    assert full["angle_share"]["news"] == pytest.approx(0.5)
    assert full["angle_share"]["tutorial"] == pytest.approx(0.5)
    assert any(v is not None for v in full["angle_share"].values())
