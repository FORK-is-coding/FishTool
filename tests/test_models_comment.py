"""core.database.models_comment ORM 模型测试（第4批 · ORM 段）。

覆盖对象：
- Comment（comments 表）
- CommentAlert（comment_alerts 表）

验证维度：
字段类型 / 默认值（含 JSON 列）/ 唯一与外键约束 / Video 关系往返 / 约束失败分支。

测试策略：
- 内存 SQLite 真实建表，不使用 Mock session。
- 外键需要真实父行（Video）才能落库，因此统一先建父视频。
- 约束失败分支从外部以 ``pytest.raises(IntegrityError)`` 断言。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database.base import Base
from core.database.models_comment import Comment, CommentAlert
from core.database.models_video import Video


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


@pytest.fixture()
def video(session):
    """创建一条真实 Video 父行，供评论外键引用。"""
    parent = Video(bvid="BV1TEST00001", title="测试视频")
    session.add(parent)
    session.commit()
    return parent


# ---------------------------------------------------------------------------
# Comment 字段契约
# ---------------------------------------------------------------------------


def test_comment_table_and_unique_rpid(session):
    """comments 表名与 rpid 唯一非空契约。"""
    assert Comment.__tablename__ == "comments"
    rpid = Comment.__table__.c.rpid
    assert rpid.type.length == 50
    assert rpid.unique is True
    assert rpid.nullable is False


def test_comment_numeric_defaults(session):
    """互动与去重相关列默认值应与模型一致。"""
    columns = Comment.__table__.c
    assert columns.like.default.arg == 0
    assert columns.reply_count.default.arg == 0
    assert columns.duplicate_count.default.arg == 1
    assert columns.is_spam.default.arg is False
    assert columns.is_duplicate.default.arg is False


def test_comment_defaults_applied_on_insert(session, video):
    """插入最小评论后各默认值应自动填充，时间戳非空。"""
    comment = Comment(rpid="r-1", video_id=video.id, content="你好")
    session.add(comment)
    session.commit()

    assert comment.like == 0
    assert comment.reply_count == 0
    assert comment.duplicate_count == 1
    assert comment.is_spam is False
    assert comment.is_duplicate is False
    assert isinstance(comment.created_at, datetime)


def test_comment_json_columns_roundtrip(session, video):
    """level_info / vip / keywords 三个 JSON 列应原样往返。"""
    session.add(
        Comment(
            rpid="r-json",
            video_id=video.id,
            uname="观众甲",
            level_info={"current_level": 3},
            vip={"status": 1, "type": 2},
            keywords=["抽奖", "点赞"],
            sentiment="positive",
            sentiment_score=0.87,
        )
    )
    session.commit()
    session.expire_all()

    loaded = session.query(Comment).filter(Comment.rpid == "r-json").one()
    assert loaded.level_info == {"current_level": 3}
    assert loaded.vip == {"status": 1, "type": 2}
    assert loaded.keywords == ["抽奖", "点赞"]
    assert loaded.sentiment_score == pytest.approx(0.87)


def test_comment_relationship_back_to_video(session, video):
    """Comment.video 与 Video.comments 应双向关联。"""
    comment = Comment(rpid="r-rel", video_id=video.id, content="关联测试")
    session.add(comment)
    session.commit()
    session.refresh(video)

    assert comment.video is video
    assert comment in video.comments


def test_comment_video_id_required(session):
    """video_id 为 NOT NULL，缺失应被数据库拒绝。"""
    session.add(Comment(rpid="r-orphan", content="无父视频"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_comment_rpid_unique_constraint(session, video):
    """重复 rpid 触发唯一约束。"""
    session.add(Comment(rpid="r-dup", video_id=video.id))
    session.commit()

    session.add(Comment(rpid="r-dup", video_id=video.id))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# CommentAlert 字段契约
# ---------------------------------------------------------------------------


def test_comment_alert_defaults(session, video):
    """CommentAlert 处理状态默认均为 False。"""
    alert = CommentAlert(
        video_id=video.id,
        alert_type="negative_surge",
        alert_level="high",
        trigger_value=8.5,
        threshold=5.0,
        message="负面激增",
        details={"window": "10m"},
    )
    session.add(alert)
    session.commit()

    assert alert.is_read is False
    assert alert.is_handled is False
    assert isinstance(alert.created_at, datetime)


def test_comment_alert_json_and_float_roundtrip(session, video):
    """details JSON 与数值列应原样往返。"""
    session.add(
        CommentAlert(
            video_id=video.id,
            alert_type="keyword_match",
            alert_level="medium",
            trigger_value=1.0,
            threshold=1.0,
            details={"keywords": ["骗子"]},
        )
    )
    session.commit()
    session.expire_all()

    loaded = session.query(CommentAlert).one()
    assert loaded.details == {"keywords": ["骗子"]}
    assert loaded.alert_type == "keyword_match"
    assert loaded.trigger_value == pytest.approx(1.0)


def test_comment_alert_video_id_required(session):
    """CommentAlert.video_id 为 NOT NULL。"""
    session.add(CommentAlert(alert_type="volume_surge", alert_level="low"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
