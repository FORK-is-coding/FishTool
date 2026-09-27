"""core.database.models_video ORM 模型测试（第4批 · ORM 段）。

覆盖对象：
- Video（videos 表）
- VideoStats（video_stats 表）
- UPMaster（up_masters 表）

验证维度：
字段类型与长度 / 默认值 / 唯一与非空约束 / Account 与评论、统计的级联关系 /
VideoStats 质量列 / UPMaster 分析字段与 onupdate。

测试策略：
- 内存 SQLite 真实建表，不使用 Mock session。
- 级联删除通过真实 delete + commit 后计数断言。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database.base import Base
from core.database.models_account import Account
from core.database.models_comment import Comment
from core.database.models_video import UPMaster, Video, VideoStats


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
# Video 字段契约
# ---------------------------------------------------------------------------


def test_video_table_and_unique_columns(session):
    """videos 表名、bvid/aid 唯一性、bvid 非空。"""
    assert Video.__tablename__ == "videos"
    assert Video.__table__.c.bvid.unique is True
    assert Video.__table__.c.bvid.nullable is False
    assert Video.__table__.c.bvid.type.length == 20
    assert Video.__table__.c.aid.unique is True


def test_video_stat_defaults(session):
    """所有互动统计列默认 0，is_monitoring 默认 False。"""
    columns = Video.__table__.c
    for name in ("view", "danmaku", "reply", "favorite", "coin", "share", "like"):
        assert columns[name].default.arg == 0
    assert columns.is_monitoring.default.arg is False


def test_video_defaults_applied_on_insert(session):
    """插入最小视频后统计与监控标记应取默认值。"""
    video = Video(bvid="BV1VID00001")
    session.add(video)
    session.commit()

    assert video.view == 0
    assert video.danmaku == 0
    assert video.like == 0
    assert video.is_monitoring is False
    assert isinstance(video.created_at, datetime)


def test_video_bvid_unique_constraint(session):
    """重复 bvid 触发唯一约束。"""
    session.add(Video(bvid="BV1VID00002"))
    session.commit()

    session.add(Video(bvid="BV1VID00002"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_video_tags_json_roundtrip_and_account_relation(session):
    """tags JSON 往返，且 Video.account 指向真实账号。"""
    account = Account(uid="9001", username="UP主")
    video = Video(
        bvid="BV1VID00003",
        account=account,
        mid=9001,
        author="UP主",
        tid=4,
        tname="游戏",
        tags=["原神", "攻略"],
        view=1234,
    )
    session.add(video)
    session.commit()
    session.expire_all()

    loaded = session.query(Video).filter(Video.bvid == "BV1VID00003").one()
    assert loaded.tags == ["原神", "攻略"]
    assert loaded.view == 1234
    assert loaded.account is not None
    assert loaded.account.uid == "9001"


def test_video_cascade_delete_removes_comments_and_stats(session):
    """删除 Video 应级联删除其评论与统计历史。"""
    video = Video(bvid="BV1VID00004")
    session.add(video)
    session.commit()

    session.add_all(
        [
            Comment(rpid="rc-1", video_id=video.id, content="a"),
            VideoStats(video_id=video.id, view=1),
            VideoStats(video_id=video.id, view=2),
        ]
    )
    session.commit()
    assert session.query(Comment).count() == 1
    assert session.query(VideoStats).count() == 2

    session.delete(video)
    session.commit()
    assert session.query(Comment).count() == 0
    assert session.query(VideoStats).count() == 0


# ---------------------------------------------------------------------------
# VideoStats
# ---------------------------------------------------------------------------


def test_video_stats_defaults_and_quality_columns(session):
    """VideoStats 统计默认 0，snapshot_time 默认当前时间，质量列可空。"""
    video = Video(bvid="BV1VID00005")
    session.add(video)
    session.commit()

    stat = VideoStats(video_id=video.id)
    session.add(stat)
    session.commit()

    assert stat.view == 0
    assert stat.like == 0
    assert isinstance(stat.snapshot_time, datetime)
    assert stat.source is None
    assert stat.run_id is None
    assert stat.view_status is None
    assert stat.stat_status is None


def test_video_stats_quality_roundtrip_and_backref(session):
    """质量来源列与 source/run_id 应往返，且 stat.video 反向关联。"""
    video = Video(bvid="BV1VID00006")
    session.add(video)
    session.commit()

    stat = VideoStats(
        video_id=video.id,
        view=88,
        source="ranking",
        run_id="run-abc",
        view_status="ok",
        stat_status="partial",
    )
    session.add(stat)
    session.commit()
    session.expire_all()

    loaded = session.query(VideoStats).one()
    assert loaded.source == "ranking"
    assert loaded.run_id == "run-abc"
    assert loaded.view_status == "ok"
    assert loaded.stat_status == "partial"
    assert loaded.video.bvid == "BV1VID00006"


def test_video_stats_video_id_required(session):
    """VideoStats.video_id 为 NOT NULL。"""
    session.add(VideoStats(view=1))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# UPMaster
# ---------------------------------------------------------------------------


def test_up_master_unique_mid_and_defaults(session):
    """up_masters 表 mid 唯一非空，计数默认 0，is_head 默认 False。"""
    assert UPMaster.__tablename__ == "up_masters"
    assert UPMaster.__table__.c.mid.unique is True
    assert UPMaster.__table__.c.mid.nullable is False

    master = UPMaster(mid=555)
    session.add(master)
    session.commit()

    assert master.follower == 0
    assert master.following == 0
    assert master.video_count == 0
    assert master.charge_count == 0
    assert master.is_head is False
    assert isinstance(master.created_at, datetime)


def test_up_master_duplicate_mid_rejected(session):
    """重复 mid 触发唯一约束。"""
    session.add(UPMaster(mid=556))
    session.commit()

    session.add(UPMaster(mid=556))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_up_master_analysis_fields_roundtrip(session):
    """分析字段与 zeroroku JSON 应原样往返，updated_at 可被 onupdate 刷新。"""
    master = UPMaster(
        mid=557,
        name="头部UP",
        category="游戏",
        post_frequency=2.5,
        avg_view=100000,
        avg_interaction=0.12,
        zeroroku_data={"score": 88},
        is_head=True,
    )
    session.add(master)
    session.commit()
    session.expire_all()

    loaded = session.query(UPMaster).filter(UPMaster.mid == 557).one()
    assert loaded.category == "游戏"
    assert loaded.post_frequency == pytest.approx(2.5)
    assert loaded.avg_interaction == pytest.approx(0.12)
    assert loaded.zeroroku_data == {"score": 88}
    assert loaded.is_head is True

    past = datetime.now() - timedelta(days=4)
    loaded.updated_at = past
    session.commit()
    loaded.name = "改名"
    session.commit()
    session.refresh(loaded)
    assert loaded.updated_at > past
