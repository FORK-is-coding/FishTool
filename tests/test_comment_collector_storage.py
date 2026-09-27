"""评论采集器去重入库 Mixin 的契约级测试。

覆盖 modules/comment/collector/storage_mixin.py 的 _save_comments_to_db：
- 新建视频记录 + 逐条入库 + 返回成功结果
- 批次内重复 rpid 过滤、缺失 rpid 跳过
- 已存在评论只补齐等级/会员字段，不重复插入
- Task.checkpoint 写入 last_rpid（取本批最大）
- 空列表短路
- 入库异常 -> 回滚 + last_save_result 标记失败且带 warning

数据库一律使用 tmp_path 下的真实 SQLite，通过 monkeypatch 接管模块级 get_session。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from core.database import Comment, DatabaseManager, Task, Video
from modules.comment.collector import CommentCollector
from modules.comment.collector import storage_mixin as storage_module


def _comment(rpid: int, **overrides) -> dict:
    """构造一条可入库的标准化评论。"""
    base = {
        "rpid": rpid,
        "uid": 1,
        "uname": "用户",
        "content": f"内容{rpid}",
        "ctime": datetime(2026, 1, 1),
        "like": 0,
        "reply_count": 0,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def collector(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """构造真实 SQLite + 采集器，返回 (采集器, 管理器)。"""
    manager = DatabaseManager(str(tmp_path / "comment.db"))
    monkeypatch.setattr(storage_module, "get_session", manager.get_session)
    return CommentCollector(api=object()), manager


def _count(manager: DatabaseManager, model) -> int:
    """统计临时库中某模型的记录数。"""
    session = manager.get_session()
    try:
        return session.query(model).count()
    finally:
        session.close()


# --------------------------------------------------------------------- 成功路径

def test_save_comments_inserts_video_and_comments(collector) -> None:
    """首次入库应创建视频记录并插入全部新评论。"""
    eng, manager = collector
    comments = [_comment(1), _comment(2)]

    result = asyncio.run(eng._save_comments_to_db("BV1", comments))

    assert result == {"success": True, "saved_count": 2, "warning": None}
    assert eng.last_save_result == result
    assert _count(manager, Video) == 1
    assert _count(manager, Comment) == 2


def test_save_comments_reuses_existing_video(collector) -> None:
    """同一 bvid 二次入库不应重复创建视频记录。"""
    eng, manager = collector

    asyncio.run(eng._save_comments_to_db("BV1", [_comment(1)]))
    asyncio.run(eng._save_comments_to_db("BV1", [_comment(2)]))

    assert _count(manager, Video) == 1
    assert _count(manager, Comment) == 2


def test_save_comments_links_comments_to_video(collector) -> None:
    """评论应关联到新建视频记录的主键。"""
    eng, manager = collector

    asyncio.run(eng._save_comments_to_db("BV9", [_comment(7)]))

    session = manager.get_session()
    try:
        video = session.query(Video).filter_by(bvid="BV9").one()
        comment = session.query(Comment).filter_by(rpid=7).one()
        assert comment.video_id == video.id
        assert comment.sentiment == "neutral"
    finally:
        session.close()


def test_save_comments_filters_batch_duplicates(collector) -> None:
    """同一批次内重复 rpid 只插入一次。"""
    eng, manager = collector

    result = asyncio.run(eng._save_comments_to_db("BV1", [_comment(1), _comment(1)]))

    assert result["saved_count"] == 1
    assert _count(manager, Comment) == 1


def test_save_comments_skips_missing_rpid(collector) -> None:
    """缺少 rpid 的记录应被跳过。"""
    eng, manager = collector
    no_rpid = _comment(1)
    no_rpid["rpid"] = None

    result = asyncio.run(eng._save_comments_to_db("BV1", [no_rpid, _comment(2)]))

    assert result["saved_count"] == 1
    assert _count(manager, Comment) == 1


def test_save_comments_writes_task_checkpoint_with_max_rpid(collector) -> None:
    """Task.checkpoint 应记录本批最大 rpid 供增量续采。"""
    eng, manager = collector

    asyncio.run(eng._save_comments_to_db("BV1", [_comment(5), _comment(9), _comment(7)]))

    session = manager.get_session()
    try:
        task = session.query(Task).filter_by(task_type="comment_collect").one()
        assert task.checkpoint == {"last_rpid": 9}
        assert task.params == {"bvid": "BV1"}
        assert task.status == "completed"
        assert task.result == {"saved_count": 3}
    finally:
        session.close()


def test_save_comments_empty_list_short_circuits(collector) -> None:
    """空列表应返回 0 且不写 checkpoint。"""
    eng, manager = collector

    result = asyncio.run(eng._save_comments_to_db("BV1", []))

    assert result == {"success": True, "saved_count": 0, "warning": None}
    assert _count(manager, Task) == 0
    # 视频记录仍会被创建（用于后续评论挂载）。
    assert _count(manager, Video) == 1


# --------------------------------------------------------------------- 已存在补齐

def test_save_comments_backfills_member_fields_on_existing(collector) -> None:
    """已存在评论缺等级/会员字段时应原位补齐并计数。"""
    eng, manager = collector
    asyncio.run(eng._save_comments_to_db("BV1", [_comment(1)]))

    result = asyncio.run(eng._save_comments_to_db("BV1", [
        _comment(1, level_info={"current_level": 3}, vip={"vipStatus": 1}),
    ]))

    assert result["saved_count"] == 1
    assert _count(manager, Comment) == 1
    session = manager.get_session()
    try:
        comment = session.query(Comment).filter_by(rpid=1).one()
        assert comment.level_info == {"current_level": 3}
        assert comment.vip == {"vipStatus": 1}
    finally:
        session.close()


def test_save_comments_does_not_overwrite_existing_member_fields(collector) -> None:
    """已有等级字段的评论不应被覆盖，也不计入 saved_count。"""
    eng, manager = collector
    asyncio.run(eng._save_comments_to_db("BV1", [_comment(1, level_info={"current_level": 6}, vip={"vipStatus": 1})]))

    result = asyncio.run(eng._save_comments_to_db("BV1", [
        _comment(1, level_info={"current_level": 1}, vip={"vipStatus": 0}),
    ]))

    assert result["saved_count"] == 0
    session = manager.get_session()
    try:
        comment = session.query(Comment).filter_by(rpid=1).one()
        assert comment.level_info == {"current_level": 6}
        assert comment.vip == {"vipStatus": 1}
    finally:
        session.close()


def test_save_comments_existing_without_changes_not_counted(collector) -> None:
    """已存在且无需补齐的评论不计入 saved_count。"""
    eng, manager = collector
    asyncio.run(eng._save_comments_to_db("BV1", [_comment(1, level_info={"current_level": 2}, vip={"vipStatus": 1})]))

    result = asyncio.run(eng._save_comments_to_db("BV1", [_comment(1)]))

    assert result["saved_count"] == 0


# --------------------------------------------------------------------- 失败路径

def test_save_comments_rolls_back_on_error(collector) -> None:
    """构造评论缺失必填字段时应回滚并返回失败结果。"""
    eng, manager = collector
    broken = {"rpid": 1, "uid": 1, "uname": "u"}  # 缺少 content / ctime

    result = asyncio.run(eng._save_comments_to_db("BV1", [broken]))

    assert result["success"] is False
    assert result["saved_count"] == 0
    assert "落库失败" in result["warning"]
    assert eng.last_save_result == result
    # 回滚后不应残留视频或评论。
    assert _count(manager, Video) == 0
    assert _count(manager, Comment) == 0


def test_save_comments_rollback_keeps_previous_batch(collector) -> None:
    """失败批次回滚不应影响此前已成功提交的数据。"""
    eng, manager = collector
    asyncio.run(eng._save_comments_to_db("BV1", [_comment(1)]))

    asyncio.run(eng._save_comments_to_db("BV1", [{"rpid": 2, "uid": 1, "uname": "u"}]))

    assert _count(manager, Comment) == 1
    assert _count(manager, Video) == 1
