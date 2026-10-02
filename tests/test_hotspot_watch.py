"""``hotspot_watch`` 单视频跟踪表：ORM 建表 + 三态 + 两条查询 + 幂等 + fencing（02 · 批 2）。

覆盖点（对应 02 方案 §8 / §0 裁定一·二·三 与前置方案 §3.2）：
- ORM 建表：表名 ``hotspot_watch``、``bvid`` 唯一、两条复合索引、NOT NULL / 默认值、
  且确认不建租约列（``lease_token`` / ``lease_until_epoch``）；
- 唯一约束：裸插两条同 bvid 被数据库拒绝；
- 三态流转：跟踪中 -> 已到期 -> 已释放；
- 调度查询：能捞到「该评估的」，捞不到「不该评估的」（两侧都断言）；
- 清理查询：边界（刚好到期 / 刚过期 / 未到期）；
- 幂等：同一目标重复写两次 -> 行数不变、内容一致（且不重置 next_due）；
- fencing：旧 ``state_revision`` 写回被丢弃，新代际写入被接受；
- 边界：全零 / 空表 / 单条。

测试策略：内存 SQLite 真实建表，不 Mock session；固定时钟；不触网、不读密钥。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database.base import Base
from core.database.models_hotspot_watch import HotspotWatch
from modules.hotspot.watch_store import (
    DEFAULT_SAMPLE_INTERVAL_S,
    DEFAULT_TTL_S,
    WatchState,
    claim_revision,
    classify_state,
    commit_state,
    find_due_for_eval,
    find_expired_for_cleanup,
    release_expired,
    upsert_watch,
)

# 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int）。
E: int = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
HOUR: int = 3600


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


def _count(session) -> int:
    """返回 hotspot_watch 当前行数。"""
    return session.query(HotspotWatch).count()


# --------------------------------------------------------------------------- 建表


def test_table_name_and_columns(session):
    """表名、唯一 bvid、NOT NULL 约束与「不建租约列」均符合 02 方案。"""
    assert HotspotWatch.__tablename__ == "hotspot_watch"
    columns = HotspotWatch.__table__.c

    assert columns.bvid.nullable is False
    assert columns.bvid.unique is True
    assert columns.ttl_end_epoch_s.nullable is False  # 裁定二：NOT NULL，入池必写
    assert columns.first_seen_epoch_s.nullable is False
    assert columns.next_due_epoch_s.nullable is False
    assert columns.sample_interval_s.nullable is False  # 裁定二：独立列，默认 3600
    assert columns.state_revision.nullable is False

    # 裁定一：不发租约
    assert "lease_token" not in columns
    assert "lease_until_epoch" not in columns

    # coverage 两级都在（裁定三）
    assert "coverage_ratio" in columns
    assert "coverage_state" in columns


def test_indexes_declared_and_created(session):
    """两条复合索引 (active, next_due_epoch_s) / (active, ttl_end_epoch_s) 真实建立。"""
    names = {index["name"] for index in inspect(session.get_bind()).get_indexes("hotspot_watch")}
    assert "ix_hotspot_watch_active_due" in names
    assert "ix_hotspot_watch_active_ttl" in names


def test_default_values_applied(session):
    """active 默认在池、failure_count / state_revision 默认 0、sample_interval_s 默认 3600。"""
    row = HotspotWatch(bvid="BV1DEF00001", first_seen_epoch_s=E, ttl_end_epoch_s=E + HOUR, next_due_epoch_s=E)
    session.add(row)
    session.commit()
    session.refresh(row)

    assert row.active is True
    assert row.failure_count == 0
    assert row.state_revision == 0
    assert row.sample_interval_s == 3600
    assert row.released_epoch_s is None


def test_unique_bvid_rejects_raw_duplicate(session):
    """裸插两条同 bvid 必须被唯一约束拒绝（证明约束真实存在，而非只靠应用层）。"""
    session.add(HotspotWatch(bvid="BV1DUP00001", first_seen_epoch_s=E, ttl_end_epoch_s=E + HOUR, next_due_epoch_s=E))
    session.commit()
    session.add(HotspotWatch(bvid="BV1DUP00001", first_seen_epoch_s=E + 1, ttl_end_epoch_s=E + HOUR, next_due_epoch_s=E))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# --------------------------------------------------------------------------- 三态


def test_three_state_transitions(session):
    """跟踪中 -> 已到期 -> 已释放，态名与条件照前置方案 §3.2。"""
    ttl = E + 2 * HOUR
    row = upsert_watch(session, bvid="BV1STATE001", now_epoch_s=E, ttl_end_epoch_s=ttl)
    session.commit()

    # 跟踪中：active=1 且 ttl > now
    assert classify_state(row, E + HOUR) is WatchState.TRACKING
    assert classify_state(row, ttl - 1) is WatchState.TRACKING

    # 已到期：active=1 且 ttl <= now（边界 now==ttl 即到期）
    assert classify_state(row, ttl) is WatchState.EXPIRED
    assert classify_state(row, ttl + 1) is WatchState.EXPIRED

    # 已到期行不参与调度，只能被清理查询捞到
    now = ttl + 1
    assert find_due_for_eval(session, now) == []
    assert [r.bvid for r in find_expired_for_cleanup(session, now)] == ["BV1STATE001"]

    # 已释放：清理动作落盘后 active=0
    released = release_expired(session, now)
    session.commit()
    assert released == 1

    session.refresh(row)
    assert row.active is False
    assert row.released_epoch_s == now
    assert row.stop_reason == "expired"
    assert classify_state(row, now) is WatchState.RELEASED


# --------------------------------------------------------------------------- 调度查询


def test_schedule_query_includes_due_excludes_others(session):
    """调度查询：捞到该评估的；捞不到未到期、已到期、已释放的（两侧断言）。"""
    now = E + 10 * HOUR

    # A：在池、ttl 未到、next_due 已到 -> 该评估
    upsert_watch(session, bvid="BV1DUE0000A", now_epoch_s=now, ttl_end_epoch_s=now + HOUR, next_due_epoch_s=now - 1)
    # B：在池、ttl 未到、next_due 未到 -> 不该评估
    upsert_watch(session, bvid="BV1DUE0000B", now_epoch_s=now, ttl_end_epoch_s=now + HOUR, next_due_epoch_s=now + 1)
    # C：在池但已到期 -> 不该评估（属清理侧）
    upsert_watch(session, bvid="BV1DUE0000C", now_epoch_s=now, ttl_end_epoch_s=now, next_due_epoch_s=now - 1)
    # D：已释放 -> 不该评估
    upsert_watch(session, bvid="BV1DUE0000D", now_epoch_s=now, ttl_end_epoch_s=now + HOUR, next_due_epoch_s=now - 1)
    release_expired(session, now)
    # 说明：D 的 ttl 未到，上面的 release 不会动它；单独把它置为已释放。
    d = session.query(HotspotWatch).filter_by(bvid="BV1DUE0000D").one()
    d.active = False
    session.commit()

    due = [row.bvid for row in find_due_for_eval(session, now)]
    assert due == ["BV1DUE0000A"]
    for excluded in ("BV1DUE0000B", "BV1DUE0000C", "BV1DUE0000D"):
        assert excluded not in due


def test_schedule_query_respects_limit_and_order(session):
    """调度查询按 next_due_epoch_s 升序，并遵守 limit。"""
    now = E + 10 * HOUR
    upsert_watch(session, bvid="BV1ORD00001", now_epoch_s=now, ttl_end_epoch_s=now + HOUR, next_due_epoch_s=now - 30)
    upsert_watch(session, bvid="BV1ORD00002", now_epoch_s=now, ttl_end_epoch_s=now + HOUR, next_due_epoch_s=now - 20)
    upsert_watch(session, bvid="BV1ORD00003", now_epoch_s=now, ttl_end_epoch_s=now + HOUR, next_due_epoch_s=now - 10)

    assert [r.bvid for r in find_due_for_eval(session, now, limit=2)] == ["BV1ORD00001", "BV1ORD00002"]


# --------------------------------------------------------------------------- 清理查询


def test_cleanup_query_boundaries(session):
    """清理查询边界：刚好到期 / 刚过期命中，未到期不命中。"""
    now = E + 5 * HOUR

    upsert_watch(session, bvid="BV1CLN0000X", now_epoch_s=now, ttl_end_epoch_s=now, next_due_epoch_s=now - 1)          # 刚好到期
    upsert_watch(session, bvid="BV1CLN0000Y", now_epoch_s=now, ttl_end_epoch_s=now - 1, next_due_epoch_s=now - 1)      # 刚过期
    upsert_watch(session, bvid="BV1CLN0000Z", now_epoch_s=now, ttl_end_epoch_s=now + 1, next_due_epoch_s=now - 1)      # 未到期

    expired = {row.bvid for row in find_expired_for_cleanup(session, now)}
    assert expired == {"BV1CLN0000X", "BV1CLN0000Y"}

    assert release_expired(session, now) == 2
    session.commit()
    # 已释放行不再被清理查询重复命中
    assert find_expired_for_cleanup(session, now) == []


# --------------------------------------------------------------------------- 幂等


def test_upsert_idempotent_row_count_and_content(session):
    """同一目标重复写两次：行数不变、内容一致，且不重置 next_due / ttl。"""
    kwargs = dict(
        bvid="BV1IDEM0001",
        now_epoch_s=E,
        ttl_end_epoch_s=E + 10 * HOUR,
        next_due_epoch_s=E - 1,
        category_key="游戏",
        discovery_source="ranking",
    )
    first = upsert_watch(session, **kwargs)
    session.commit()
    snapshot_first = (
        first.id, first.active, first.first_seen_epoch_s, first.last_seen_epoch_s,
        first.ttl_end_epoch_s, first.next_due_epoch_s, first.state_revision,
        first.category_key, first.discovery_source,
    )

    upsert_watch(session, **kwargs)
    session.commit()

    assert _count(session) == 1  # 行数不变
    row = session.query(HotspotWatch).filter_by(bvid="BV1IDEM0001").one()
    snapshot_second = (
        row.id, row.active, row.first_seen_epoch_s, row.last_seen_epoch_s,
        row.ttl_end_epoch_s, row.next_due_epoch_s, row.state_revision,
        row.category_key, row.discovery_source,
    )
    assert snapshot_first == snapshot_second  # 内容一致

    # 换一个更晚的时刻再发现：只更新 last_seen，其余调度列不动
    upsert_watch(session, **{**kwargs, "now_epoch_s": E + HOUR, "next_due_epoch_s": E + 999})
    session.commit()
    row = session.query(HotspotWatch).filter_by(bvid="BV1IDEM0001").one()
    assert _count(session) == 1
    assert row.last_seen_epoch_s == E + HOUR          # 元信息：更新
    assert row.next_due_epoch_s == E - 1              # 调度：不重置（02 §7.3 第 3 条）
    assert row.ttl_end_epoch_s == E + 10 * HOUR       # 到期：不挪
    assert row.first_seen_epoch_s == E                # 首见：不覆盖


def test_upsert_does_not_clobber_metadata_with_none(session):
    """重复入库传 None 不应抹掉已有元信息（避免「重复发现丢分类」）。"""
    upsert_watch(session, bvid="BV1META0001", now_epoch_s=E, ttl_end_epoch_s=E + HOUR, category_key="科技")
    session.commit()
    upsert_watch(session, bvid="BV1META0001", now_epoch_s=E + 1)
    session.commit()
    row = session.query(HotspotWatch).filter_by(bvid="BV1META0001").one()
    assert row.category_key == "科技"


def test_upsert_defaults_and_invalid_bvid(session):
    """省略 ttl / next_due 时按默认值；非法 bvid 抛稳定错误码。"""
    row = upsert_watch(session, bvid="BV1DEFT0001", now_epoch_s=E)
    session.commit()
    assert row.ttl_end_epoch_s == E + DEFAULT_TTL_S
    assert row.next_due_epoch_s == E + DEFAULT_SAMPLE_INTERVAL_S
    assert row.sample_interval_s == DEFAULT_SAMPLE_INTERVAL_S

    with pytest.raises(ValueError):
        upsert_watch(session, bvid="   ", now_epoch_s=E)
    with pytest.raises(ValueError):
        upsert_watch(session, bvid="BV1BADT0001", now_epoch_s=-1)


# --------------------------------------------------------------------------- fencing


def test_fencing_stale_revision_dropped(session):
    """旧 state_revision 写回被丢弃；同代际写回成功并把代际 +1。"""
    upsert_watch(session, bvid="BV1FENCE001", now_epoch_s=E, ttl_end_epoch_s=E + HOUR)
    session.commit()
    assert claim_revision(session, "BV1FENCE001") == 0

    # 首次领取 revision=0，提交成功
    assert commit_state(session, "BV1FENCE001", claim_revision=0, last_confirmed_stage="出现期") is True
    session.commit()
    assert claim_revision(session, "BV1FENCE001") == 1
    assert session.query(HotspotWatch).filter_by(bvid="BV1FENCE001").one().last_confirmed_stage == "出现期"

    # 拿着旧代际 0 的迟到写入必须被丢弃，且不改任何列
    assert commit_state(session, "BV1FENCE001", claim_revision=0, last_confirmed_stage="衰退期") is False
    session.commit()
    row = session.query(HotspotWatch).filter_by(bvid="BV1FENCE001").one()
    assert row.last_confirmed_stage == "出现期"   # 未被旧代际覆盖
    assert claim_revision(session, "BV1FENCE001") == 1

    # 用当前代际再提交，接受并再 +1
    assert commit_state(session, "BV1FENCE001", claim_revision=1, last_confirmed_stage="上升期") is True
    session.commit()
    assert claim_revision(session, "BV1FENCE001") == 2


def test_release_expired_fences_inflight_claim(session):
    """释放会推进代际，释放前领取的写回被 fence 掉。"""
    upsert_watch(session, bvid="BV1FENCE002", now_epoch_s=E, ttl_end_epoch_s=E + 1)
    session.commit()
    stale_claim = claim_revision(session, "BV1FENCE002")

    release_expired(session, E + 1)
    session.commit()

    assert commit_state(session, "BV1FENCE002", claim_revision=stale_claim, last_confirmed_stage="上升期") is False


def test_commit_state_validates_coverage_pair(session):
    """coverage 要存就两个都在且取值合法（02 §0 裁定三）。"""
    upsert_watch(session, bvid="BV1COV00001", now_epoch_s=E, ttl_end_epoch_s=E + HOUR)
    session.commit()

    with pytest.raises(ValueError):
        commit_state(session, "BV1COV00001", claim_revision=0, coverage_ratio=0.9)  # 缺 state
    with pytest.raises(ValueError):
        commit_state(session, "BV1COV00001", claim_revision=0, coverage_state="full_support")  # 缺 ratio
    with pytest.raises(ValueError):
        commit_state(session, "BV1COV00001", claim_revision=0, coverage_ratio=1.5, coverage_state="full_support")
    with pytest.raises(ValueError):
        commit_state(session, "BV1COV00001", claim_revision=0, coverage_ratio=0.9, coverage_state="made_up")

    # 合法成对：落盘并可由行读回
    assert commit_state(
        session, "BV1COV00001", claim_revision=0,
        coverage_ratio=0.92, coverage_state="provisional", last_evaluation_epoch_s=E,
    ) is True
    session.commit()
    row = session.query(HotspotWatch).filter_by(bvid="BV1COV00001").one()
    assert row.coverage_ratio == pytest.approx(0.92)
    assert row.coverage_state == "provisional"
    assert row.last_evaluation_epoch_s == E


# --------------------------------------------------------------------------- 边界


def test_empty_table_queries(session):
    """空表：两条查询都返回空，释放返回 0。"""
    assert find_due_for_eval(session, E) == []
    assert find_expired_for_cleanup(session, E) == []
    assert release_expired(session, E) == 0
    assert claim_revision(session, "BV1NONE0001") == 0


def test_zero_epoch_single_row(session):
    """全零 / 单条：ttl=0 且 now=0 按「已到期」处理，不进调度、进清理。"""
    row = upsert_watch(session, bvid="BV1ZERO0001", now_epoch_s=0, ttl_end_epoch_s=0, next_due_epoch_s=0)
    session.commit()

    assert _count(session) == 1
    assert classify_state(row, 0) is WatchState.EXPIRED
    assert find_due_for_eval(session, 0) == []
    assert [r.bvid for r in find_expired_for_cleanup(session, 0)] == ["BV1ZERO0001"]
    assert release_expired(session, 0) == 1
