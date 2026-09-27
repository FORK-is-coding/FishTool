"""评论入库 Mixin 契约测试（modules/comment/collector/storage_mixin.py）。

覆盖 _save_comments_to_db：
- 视频占位记录的创建与复用
- 评论按 rpid 批内去重 + 库内去重，已存在记录只补齐等级/会员字段
- 增量 checkpoint（Task.checkpoint.last_rpid）写入
- 入库失败回滚并把原因写入 last_save_result.warning

测试策略：
- 使用真实 CommentCollector 与 tmp_path 下的真实 SQLite（打桩 storage_mixin.get_session）；
- 失败分支通过真实坏数据（缺字段、混用 rpid 类型）驱动，从外部断言结果。
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from core.database import Comment, DatabaseManager, Task, Video
from modules.comment.collector import CommentCollector
from modules.comment.collector import storage_mixin as storage_module

BVID = "BV1storage"


def run(coro):
    """同步测试内执行单次协程，并加 5 秒超时双保险。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


def make_comment(rpid, uid=1, uname="用户", content="内容", ctime=None, **extra) -> dict:
    """构造一条待入库的标准评论字典。"""
    data = {
        "rpid": rpid,
        "uid": uid,
        "uname": uname,
        "content": content,
        "ctime": ctime or datetime(2026, 8, 22, 10, 0),
        "like": 0,
        "reply_count": 0,
        "level_info": {"current_level": 3},
        "vip": {"vipType": 2},
    }
    data.update(extra)
    return data


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """把入库 Mixin 的模块级 get_session 指向 tmp_path 下的真实库。"""
    manager = DatabaseManager(str(tmp_path / "storage.db"))
    monkeypatch.setattr(storage_module, "get_session", manager.get_session)
    return manager


def build_collector() -> CommentCollector:
    """只用于入库测试的真实采集器实例（不触网）。"""
    return CommentCollector(api=SimpleNamespace())


def query(manager, model) -> list:
    """读取临时库中某张表的全部行。"""
    session = manager.get_session()
    try:
        return session.query(model).all()
    finally:
        session.close()


def seed_video(manager, bvid=BVID, title="真实标题") -> int:
    """预置视频记录并返回主键。"""
    session = manager.get_session()
    try:
        video = Video(bvid=bvid, title=title)
        session.add(video)
        session.commit()
        return video.id
    finally:
        session.close()


def seed_comment(manager, video_id: int, rpid: int, level_info=None, vip=None) -> None:
    """预置一条已存在评论，用于验证“不重复插入”。"""
    session = manager.get_session()
    try:
        session.add(Comment(rpid=rpid, video_id=video_id, uid=1, uname="u", content="历史",
                            ctime=datetime(2026, 8, 20), sentiment="neutral",
                            level_info=level_info, vip=vip))
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_save_creates_video_comments_and_checkpoint(db):
    """首次入库应创建占位视频、写入评论并记录增量断点。"""
    collector = build_collector()

    result = run(collector._save_comments_to_db(BVID, [make_comment(1), make_comment(2), make_comment(3)]))

    assert result == {"success": True, "saved_count": 3, "warning": None}
    assert collector.last_save_result == result

    videos = query(db, Video)
    assert len(videos) == 1
    assert videos[0].title == f"视频_{BVID}"

    comments = query(db, Comment)
    assert sorted(item.rpid for item in comments) == ["1", "2", "3"]
    assert all(item.video_id == videos[0].id for item in comments)
    # 默认情感标签留给后续分析阶段。
    assert all(item.sentiment == "neutral" for item in comments)

    tasks = query(db, Task)
    assert len(tasks) == 1
    assert tasks[0].task_type == "comment_collect"
    assert tasks[0].params == {"bvid": BVID}
    assert tasks[0].checkpoint == {"last_rpid": 3}
    assert tasks[0].result == {"saved_count": 3}


def test_save_reuses_existing_video_record(db):
    """视频已存在时不再新建占位记录，评论挂在原视频下。"""
    video_id = seed_video(db)

    run(build_collector()._save_comments_to_db(BVID, [make_comment(9)]))

    assert len(query(db, Video)) == 1
    assert query(db, Comment)[0].video_id == video_id


def test_save_deduplicates_rpids_within_same_batch(db):
    """同一批次内的重复 rpid 必须在内存里先过滤，避免唯一键冲突。"""
    result = run(build_collector()._save_comments_to_db(BVID, [make_comment(1), make_comment(1), make_comment(2)]))

    assert result["saved_count"] == 2
    assert sorted(item.rpid for item in query(db, Comment)) == ["1", "2"]


def test_save_backfills_member_fields_on_existing_comment(db):
    """已存在评论只补齐缺失的 level_info/vip，并计入 saved_count。"""
    video_id = seed_video(db)
    seed_comment(db, video_id, rpid=1, level_info=None, vip=None)

    result = run(build_collector()._save_comments_to_db(BVID, [make_comment(1)]))

    assert result["saved_count"] == 1
    rows = query(db, Comment)
    assert len(rows) == 1
    assert rows[0].level_info == {"current_level": 3}
    assert rows[0].vip == {"vipType": 2}


def test_save_does_not_count_unchanged_existing_comment(db):
    """字段齐全的已存在评论既不重复插入也不计数。"""
    video_id = seed_video(db)
    seed_comment(db, video_id, rpid=1, level_info={"current_level": 2}, vip={"vipType": 1})

    result = run(build_collector()._save_comments_to_db(BVID, [make_comment(1)]))

    assert result == {"success": True, "saved_count": 0, "warning": None}
    assert len(query(db, Comment)) == 1


def test_save_keeps_legacy_string_rpids_and_max_value(db):
    """rpid 全为字符串时按字符串比较取断点（固化现状）。"""
    result = run(build_collector()._save_comments_to_db(BVID, [make_comment("1"), make_comment("2")]))

    assert result["saved_count"] == 2
    assert query(db, Task)[0].checkpoint == {"last_rpid": "2"}


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_save_empty_list_still_creates_video_without_checkpoint(db):
    """空评论列表会创建占位视频，但不写 checkpoint。"""
    result = run(build_collector()._save_comments_to_db(BVID, []))

    assert result == {"success": True, "saved_count": 0, "warning": None}
    assert len(query(db, Video)) == 1
    assert query(db, Task) == []
    assert query(db, Comment) == []


def test_save_skips_comments_without_rpid(db):
    """缺少 rpid 的记录无法安全入库，直接跳过。"""
    result = run(build_collector()._save_comments_to_db(BVID, [make_comment(None), make_comment(7)]))

    assert result["saved_count"] == 1
    assert [item.rpid for item in query(db, Comment)] == ["7"]
    assert query(db, Task)[0].checkpoint == {"last_rpid": 7}


# ---------------------------------------------------------------------------
# 异常分支
# ---------------------------------------------------------------------------


def test_save_returns_failure_when_session_unavailable(monkeypatch):
    """会话创建失败时返回降级结果而不是抛异常，warning 说明原因。"""
    monkeypatch.setattr(storage_module, "get_session",
                        lambda: (_ for _ in ()).throw(RuntimeError("数据库被占用")))
    collector = build_collector()

    result = run(collector._save_comments_to_db(BVID, [make_comment(1)]))

    assert result["success"] is False
    assert result["saved_count"] == 0
    assert "评论已采集但落库失败" in result["warning"]
    assert "数据库被占用" in result["warning"]
    assert collector.last_save_result == result


def test_save_rolls_back_on_incomplete_comment(db):
    """字段缺失导致 KeyError 时整体回滚，不留半截数据。"""
    broken = make_comment(1)
    broken.pop("uid")
    collector = build_collector()

    result = run(collector._save_comments_to_db(BVID, [broken]))

    assert result["success"] is False
    assert "uid" in result["warning"]
    assert query(db, Comment) == []
    assert query(db, Video) == []
    assert query(db, Task) == []


def test_save_fails_on_mixed_rpid_types(db):
    """固化现状：int 与 str 混用会在断点比较时抛 TypeError，整批落库失败。"""
    collector = build_collector()

    result = run(collector._save_comments_to_db(BVID, [make_comment(1), make_comment("2")]))

    assert result["success"] is False
    assert "not supported between instances of 'str' and 'int'" in result["warning"]
    assert query(db, Comment) == []
