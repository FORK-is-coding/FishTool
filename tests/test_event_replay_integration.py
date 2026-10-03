"""FishTool 04 · R5 第四批 f：历史回放补完的集成测试（真临时 SQLite）。

规格：``FishTool_04_R5执行规格_第四批f_三点收口.md`` §二「回放补完」。

覆盖三点（对应 §2.2）：

1. **DB 层 as_of 门**：插 ``as_of`` 之前 + 之后两组行（decision / capture / run /
   panel effective 四个时间列各一组）→ 断言**只有之前那组进得来**，之后的行全部取不到；
2. **编排入口** ``run_historical_replay``：统一 ``mode='historical'``、``action`` 只落在
   允许集合（``differentiate_research`` / ``watch_and_collect``）、**不写机会 run 表**；
3. **``request_as_of`` 必传**：缺参数由签名直接 ``TypeError``；显式非法值 ``ValueError``。

纪律：不触网、不碰任何红线文件；只**调用**既有纯内核与本次新增的 as_of 加载器。

验证边界：本文件只证明 **DB 层 + 内核编排** 正确；**未经真服务端**（无 HTTP / 无真实
生产库）—— 即「回放入口可用」的边界到此为止。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import DatabaseManager
from core.database.models_hot_event import (
    EventDiscoveryRun,
    HotEvent,
    HotEventMember,
    OpportunityRun,
)
from core.database.models_video import Video, VideoStats
from modules.hotspot.events.brief import CreatorBrief
from modules.hotspot.events.config import DAY_W
from modules.hotspot.events.service import (
    _load_as_of_discovery_runs,
    _load_as_of_member_revisions,
    _load_as_of_panel_effective,
    _load_as_of_snapshots,
    run_historical_replay,
)

#: 统一测试时钟：2026-09-01T00:00:00Z（UTC 零点，也是 86400 / 7200 的公共网格点）。
T0: int = 1_788_220_800
#: 回放请求时刻：当日 12:00（as_of 与日网格边界分离；网格边界停在 T0）。
AS_OF: int = T0 + 43_200
#: 事件 ID。
EVENT: str = "ev-replay"


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：经 ``DatabaseManager`` 建全部表 → 换可跨线程引擎产出会话工厂。"""
    path = tmp_path / "replay_integration.db"
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


def _brief() -> CreatorBrief:
    """最小合法 ``CreatorBrief``（实体与事件对齐，保证 ``explicit_match``）。"""
    return CreatorBrief.from_dict(
        {
            "brief_version": "4f-replay",
            "production_hours": 1.0,
            "review_hours": 1.0,
            "publish_buffer_hours": 1.0,
            "max_experiment_hours": 10.0,
            "allowed_entities": ("剑与远征",),
        }
    )


def _seed(db) -> None:
    """插入 as_of 之前 + 之后两组行（成员 / 快照 / 发现 run / panel effective）。"""
    session = db()
    try:
        # ---- 事件（panel effective 门：past / future / 未激活草稿 三条历史）----
        session.add(
            HotEvent(
                id=EVENT,
                name="回放事件",
                created_s=T0 - 10 * DAY_W,
                updated_s=T0,
                status="active",
                revision=0,
                fast_panel_history=[
                    {"panel_id": "p-past", "effective_s": T0, "expires_s": T0 + DAY_W,
                     "bvids": ["BVpanelPast"]},
                    {"panel_id": "p-future", "effective_s": AS_OF + 3600,
                     "expires_s": AS_OF + DAY_W + 3600, "bvids": ["BVpanelFuture"]},
                    {"panel_id": "p-draft", "effective_s": None, "bvids": ["BVpanelDraft"]},
                ],
            )
        )
        # ---- decision 门：past 在 as_of 之前、future 在 as_of 之后 ----
        session.add_all(
            [
                HotEventMember(
                    event_id=EVENT, bvid="BVpast1", revision=1, status="accepted",
                    first_seen_s=T0 - 4 * DAY_W, decision_at_s=T0 - 4 * DAY_W,
                    rule_version=1, decision_source="auto", owner_mid=101,
                ),
                HotEventMember(
                    event_id=EVENT, bvid="BVpast2", revision=1, status="accepted",
                    first_seen_s=T0 - 4 * DAY_W, decision_at_s=T0 - 4 * DAY_W,
                    rule_version=1, decision_source="auto", owner_mid=102,
                ),
                HotEventMember(
                    event_id=EVENT, bvid="BVfuture", revision=1, status="accepted",
                    first_seen_s=AS_OF + 1, decision_at_s=AS_OF + 1,
                    rule_version=1, decision_source="auto", owner_mid=103,
                ),
            ]
        )
        # ---- capture 门：past 锚点 <= as_of、future 采集点 > as_of ----
        session.add_all([Video(bvid="BVpast1"), Video(bvid="BVpast2")])
        session.flush()
        v1 = session.query(Video).filter(Video.bvid == "BVpast1").one()
        v2 = session.query(Video).filter(Video.bvid == "BVpast2").one()
        session.add_all(
            [
                VideoStats(video_id=v1.id, view=1000, view_status="ok",
                           captured_epoch_s=T0 - 3 * DAY_W),
                VideoStats(video_id=v1.id, view=1100, view_status="ok",
                           captured_epoch_s=T0 - 2 * DAY_W),
                VideoStats(video_id=v1.id, view=1250, view_status="ok",
                           captured_epoch_s=T0 - DAY_W),
                VideoStats(video_id=v1.id, view=1400, view_status="ok",
                           captured_epoch_s=T0),
                # 未来采集点：必须被 capture 门挡在 as_of 之外
                VideoStats(video_id=v1.id, view=9999, view_status="ok",
                           captured_epoch_s=AS_OF + DAY_W),
                VideoStats(video_id=v2.id, view=5000, view_status="ok",
                           captured_epoch_s=T0 - 2 * DAY_W),
            ]
        )
        # ---- run 门：past <= as_of、future > as_of ----
        session.add_all(
            [
                EventDiscoveryRun(
                    id="run-past", event_id=EVENT, rule_version=1, source_policy_hash="h",
                    lease_token="t", trigger="scheduled", started_s=T0, finished_s=T0 + 60,
                    status="completed", counters={"query_count": 1},
                ),
                EventDiscoveryRun(
                    id="run-future", event_id=EVENT, rule_version=1, source_policy_hash="h",
                    lease_token="t", trigger="scheduled", started_s=AS_OF + 1,
                    finished_s=AS_OF + 61, status="completed", counters={"query_count": 1},
                ),
            ]
        )
        session.commit()
    finally:
        session.close()


# ===========================================================================
# 1. DB 层 as_of 门：只有之前那组进得来
# ===========================================================================


def test_as_of_loaders_exclude_all_future_rows(db) -> None:
    """四个时间列各自过闸：as_of 之后的行（含未来采集点）一律取不到。"""
    _seed(db)

    # ---- decision 门：未来才 accepted 的 BVfuture 取不到 ----
    revisions = _load_as_of_member_revisions(db, EVENT, as_of_s=AS_OF)
    assert set(revisions) == {"BVpast1", "BVpast2"}
    assert "BVfuture" not in revisions

    # ---- capture 门：AS_OF + DAY_W 的未来采集点取不到（哪怕窗口右端天然越界）----
    pts = _load_as_of_snapshots(
        db, ["BVpast1"], as_of_s=AS_OF, start_s=T0 - 4 * DAY_W, end_s=AS_OF + 10 * DAY_W
    )
    assert [p.epoch_s for p in pts["BVpast1"]] == [
        T0 - 3 * DAY_W, T0 - 2 * DAY_W, T0 - DAY_W, T0
    ]
    assert all(p.epoch_s <= AS_OF for p in pts["BVpast1"])

    # ---- run 门：AS_OF 之后开始的 run-future 取不到 ----
    runs = _load_as_of_discovery_runs(db, EVENT, as_of_s=AS_OF)
    assert [r.run_id for r in runs] == ["run-past"]

    # ---- panel effective 门：未来生效 panel / 未激活草稿都取不到 ----
    panel = _load_as_of_panel_effective(db, EVENT, as_of_s=AS_OF)
    assert panel == ("BVpanelPast",)


# ===========================================================================
# 2. 编排入口：统一 historical + 不进即时机会队列
# ===========================================================================


def test_run_historical_replay_is_historical_and_never_enters_queue(db) -> None:
    """``run_historical_replay``：mode 恒 historical、action 在允许集合、不写机会 run。"""
    _seed(db)

    out = run_historical_replay(
        EVENT, request_as_of_s=AS_OF, brief=_brief(), session_factory=db
    )

    assert out["mode"] == "historical"
    assert out["request_as_of_s"] == AS_OF
    assert out["entered_realtime_queue"] is False

    # 三个内核统一 historical；通道 A 的窗口边界不越过执行时钟
    for key in ("channel_a", "daily", "early"):
        assert out[key]["mode"] == "historical", f"{key} 未标 historical"
    assert out["channel_a"]["window_end_s"] <= AS_OF

    # 历史模式 action 只允许 differentiate_research / watch_and_collect
    assert out["action"] in ("differentiate_research", "watch_and_collect")
    assert out["opportunity"]["mode"] == "historical"

    # 不得进入即时机会队列：hotspot_opportunity_runs 不新增任何行
    session = db()
    try:
        assert session.query(OpportunityRun).count() == 0
    finally:
        session.close()


# ===========================================================================
# 3. request_as_of 必传（不许有默认值、不许省）
# ===========================================================================


def test_run_historical_replay_requires_request_as_of() -> None:
    """回放必须显式传重放时刻：缺参数 TypeError，非法值 ValueError，绝不回退墙钟。"""
    # ---- 省略参数 = 签名直接拒绝（没有默认时钟）----
    with pytest.raises(TypeError):
        run_historical_replay(EVENT, brief=_brief())  # type: ignore[call-arg]

    # ---- 显式 None / 负数 = 拒绝 ----
    with pytest.raises(ValueError, match="invalid_request_as_of_s"):
        run_historical_replay(EVENT, request_as_of_s=None, brief=_brief())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_request_as_of_s"):
        run_historical_replay(EVENT, request_as_of_s=-1, brief=_brief())
