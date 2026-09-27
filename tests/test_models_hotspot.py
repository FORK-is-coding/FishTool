"""core.database.models_hotspot ORM 模型测试（第4批 · ORM 段）。

覆盖对象：
- Hotspot（hotspots 表）
- Topic（topics 表）
- Activity（activities 表）

验证维度：
字段类型 / 默认值 / 唯一与非空约束 / Hotspot→Topic 外键关系 / JSON 往返 / onupdate 行为。

测试策略：
- 内存 SQLite 真实建表，不使用 Mock session。
- onupdate 用「先把 updated_at 改成过去时间再更新」的方式稳定断言。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database.base import Base
from core.database.models_hotspot import Activity, Hotspot, Topic


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


# ---------------------------------------------------------------------------
# Hotspot
# ---------------------------------------------------------------------------


def test_hotspot_contract_and_json_roundtrip(session):
    """hotspots 表名、类型长度与 JSON 列往返。"""
    assert Hotspot.__tablename__ == "hotspots"
    assert Hotspot.__table__.c.source.type.length == 50
    assert Hotspot.__table__.c.title.type.length == 500
    assert Hotspot.__table__.c.trend.type.length == 20

    session.add(
        Hotspot(
            source="tag_cloud",
            category="游戏",
            title="原神新版本",
            tags=["原神", "攻略"],
            keywords={"原神": 3},
            heat_score=92.5,
            trend="rising",
        )
    )
    session.commit()
    session.expire_all()

    loaded = session.query(Hotspot).one()
    assert loaded.tags == ["原神", "攻略"]
    assert loaded.keywords == {"原神": 3}
    assert loaded.heat_score == pytest.approx(92.5)
    assert isinstance(loaded.created_at, datetime)


def test_hotspot_allows_nullable_optional_columns(session):
    """热点除主键外均可为空，仅写 source 也允许落库。"""
    session.add(Hotspot(source="trending"))
    session.commit()
    assert session.query(Hotspot).count() == 1


# ---------------------------------------------------------------------------
# Topic
# ---------------------------------------------------------------------------


def test_topic_defaults_applied(session):
    """Topic 状态默认 pending、优先级默认 0、时间戳自动填充。"""
    topic = Topic(title="选题A")
    session.add(topic)
    session.commit()

    assert topic.status == "pending"
    assert topic.priority == 0
    assert isinstance(topic.created_at, datetime)
    assert isinstance(topic.updated_at, datetime)


def test_topic_declared_default_arguments(session):
    """列级默认值与模型声明保持一致。"""
    assert Topic.__table__.c.status.default.arg == "pending"
    assert Topic.__table__.c.priority.default.arg == 0
    assert Topic.__table__.c.title.type.length == 500
    assert Topic.__table__.c.title.nullable is False


def test_topic_title_not_null(session):
    """Topic.title 为 NOT NULL。"""
    session.add(Topic(description="缺标题"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_topic_foreign_key_to_hotspot(session):
    """Topic.hotspot_id 可引用真实 Hotspot 行。"""
    hotspot = Hotspot(source="tag_cloud", title="父热点")
    session.add(hotspot)
    session.commit()

    topic = Topic(title="子选题", hotspot_id=hotspot.id, ai_suggestions={"angle": "复盘"})
    session.add(topic)
    session.commit()
    session.expire_all()

    loaded = session.query(Topic).filter(Topic.title == "子选题").one()
    assert loaded.hotspot_id == hotspot.id
    assert loaded.ai_suggestions == {"angle": "复盘"}


def test_topic_updated_at_onupdate_refreshes(session):
    """更新 Topic 时 updated_at 应被 onupdate 刷新到更近时间。"""
    topic = Topic(title="待更新")
    session.add(topic)
    session.commit()

    past = datetime.now() - timedelta(days=3)
    topic.updated_at = past
    session.commit()

    topic.title = "已更新"
    session.commit()
    session.refresh(topic)
    assert topic.updated_at > past


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


def test_activity_unique_activity_id(session):
    """activities 表 activity_id 唯一。"""
    assert Activity.__tablename__ == "activities"
    assert Activity.__table__.c.activity_id.unique is True

    session.add(Activity(activity_id="act-1", title="活动一"))
    session.commit()

    session.add(Activity(activity_id="act-1", title="重复活动"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_activity_json_and_status_roundtrip(session):
    """reward_info/tags JSON 与状态、时间窗口应原样往返。"""
    start = datetime(2030, 1, 1, 9, 0, 0)
    end = datetime(2030, 2, 1, 9, 0, 0)
    session.add(
        Activity(
            activity_id="act-2",
            title="拜年祭",
            desc="年度活动",
            tags=["官方", "联动"],
            reward_info={"money": 1000},
            requirement="投稿",
            status="ongoing",
            start_time=start,
            end_time=end,
        )
    )
    session.commit()
    session.expire_all()

    loaded = session.query(Activity).filter(Activity.activity_id == "act-2").one()
    assert loaded.tags == ["官方", "联动"]
    assert loaded.reward_info == {"money": 1000}
    assert loaded.status == "ongoing"
    assert loaded.start_time == start
    assert loaded.end_time == end


def test_activity_updated_at_onupdate_refreshes(session):
    """更新 Activity 时 updated_at 应被 onupdate 刷新。"""
    activity = Activity(activity_id="act-3", title="待改")
    session.add(activity)
    session.commit()

    past = datetime.now() - timedelta(days=5)
    activity.updated_at = past
    session.commit()

    activity.status = "ended"
    session.commit()
    session.refresh(activity)
    assert activity.updated_at > past
