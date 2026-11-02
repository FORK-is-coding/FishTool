"""评论上下文保留/补齐与迁移幂等测试。

覆盖：
- parser classify_comment_context：顶层(root=0,parent=0)、回复(parent 非 0)、
  未知(缺字段/None/bool)——严格不以默认 0 冒充顶层。
- storage 入库写入 root/parent/context_status；重新采集补齐未知上下文、
  绝不用未知覆盖已知。
- 迁移幂等：旧库缺列补齐、重复 create_tables 不报错；legacy 行 context_status 为 NULL
  被语义层视为 unknown。
- root=0,parent=0 严格顶层；缺 root 的 parent=0 不冒充顶层。
"""
import asyncio
from datetime import datetime

import pytest
from sqlalchemy import inspect, text

from core.database import Comment, DatabaseManager, Video
from modules.comment.collector import CommentCollector
from modules.comment.collector import storage_mixin as storage_module
from modules.comment.collector.parse_mixin import classify_comment_context

BVID = "BV1context"


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def test_context_top_level_requires_both_zero():
    out = classify_comment_context({"root": 0, "parent": 0})
    assert out["context_status"] == "top_level"
    assert out["root_rpid"] is None and out["parent_rpid"] is None


def test_context_reply_when_parent_nonzero():
    out = classify_comment_context({"root": 0, "parent": 555})
    assert out["context_status"] == "reply"
    assert out["parent_rpid"] == "555"


def test_context_reply_when_root_nonzero():
    out = classify_comment_context({"root": 999, "parent": 999})
    assert out["context_status"] == "reply"
    assert out["root_rpid"] == "999"


def test_context_unknown_when_missing():
    assert classify_comment_context({})["context_status"] == "unknown"
    assert classify_comment_context({"root": 0})["context_status"] == "unknown"  # 缺 parent


def test_context_unknown_when_none_or_bool():
    assert classify_comment_context({"root": None, "parent": None})["context_status"] == "unknown"
    # bool 不是合法 JSON number：True/False 不能冒充 0/1
    assert classify_comment_context({"root": False, "parent": False})["context_status"] == "unknown"
    assert classify_comment_context({"root": True, "parent": True})["context_status"] == "unknown"


def test_parse_reply_includes_context_fields():
    parser = CommentCollector(api=object())
    raw = {
        "rpid": 1, "oid": 9, "member": {"mid": 1, "uname": "u"},
        "content": {"message": "hi"}, "ctime": 1766400000, "root": 0, "parent": 0,
    }
    parsed = parser._parse_comment_reply(raw)
    assert parsed["context_status"] == "top_level"
    assert parsed["root_rpid"] is None


# ---------------------------------------------------------------------------
# storage: 写入 + 补齐
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    manager = DatabaseManager(str(tmp_path / "ctx.db"))
    monkeypatch.setattr(storage_module, "get_session", manager.get_session)
    return manager


def make_comment(rpid, content="内容", context="top_level", root=None, parent=None):
    return {
        "rpid": rpid, "uid": 1, "uname": "u", "content": content,
        "ctime": datetime(2026, 8, 22, 10, 0), "like": 0, "reply_count": 0,
        "context_status": context, "root_rpid": root, "parent_rpid": parent,
    }


def build_collector():
    return CommentCollector(api=object())


def test_storage_writes_context(db):
    c = build_collector()
    run(c._save_comments_to_db(BVID, [make_comment("1", context="top_level")]))
    session = db.get_session()
    try:
        row = session.query(Comment).filter_by(rpid="1").first()
        assert row.context_status == "top_level"
        assert row.root_rpid is None
    finally:
        session.close()


def test_storage_backfills_unknown_with_known(db):
    c = build_collector()
    # 先入库未知上下文
    run(c._save_comments_to_db(BVID, [make_comment("2", context="unknown")]))
    # 重新采集：已知顶层，应补齐
    run(c._save_comments_to_db(BVID, [make_comment("2", context="top_level")]))
    session = db.get_session()
    try:
        row = session.query(Comment).filter_by(rpid="2").first()
        assert row.context_status == "top_level"
    finally:
        session.close()


def test_storage_known_not_overwritten_by_unknown(db):
    c = build_collector()
    run(c._save_comments_to_db(BVID, [make_comment("3", context="reply", root="7", parent="7")]))
    # 重新采集带未知上下文：不得覆盖已知
    run(c._save_comments_to_db(BVID, [make_comment("3", context="unknown")]))
    session = db.get_session()
    try:
        row = session.query(Comment).filter_by(rpid="3").first()
        assert row.context_status == "reply"
        assert row.root_rpid == "7"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 迁移幂等 + legacy
# ---------------------------------------------------------------------------

def test_migration_idempotent_on_fresh(tmp_path):
    m = DatabaseManager(str(tmp_path / "fresh.db"))
    m.create_tables()  # 再次调用不报错
    cols = {c["name"] for c in inspect(m.engine).get_columns("comments")}
    assert {"root_rpid", "parent_rpid", "context_status"} <= cols


def test_migration_adds_columns_to_legacy(tmp_path):
    """手工建一张缺上下文列的旧 comments 表，迁移应补齐且保留旧行。"""
    path = str(tmp_path / "legacy.db")
    import sqlite3
    conn = sqlite3.connect(path)
    # 近似真实旧库 comments 结构（含 uid/uname/content/like 等基础列），
    # 仅缺本次新增的 root_rpid/parent_rpid/context_status 三列，交由迁移补齐。
    conn.execute(
        "CREATE TABLE comments ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, rpid VARCHAR(50), video_id INTEGER, "
        "uid INTEGER, uname VARCHAR(100), content TEXT, ctime DATETIME, "
        "\"like\" INTEGER, reply_count INTEGER, sentiment VARCHAR(20), "
        "sentiment_score FLOAT, is_spam BOOLEAN, is_duplicate BOOLEAN, "
        "duplicate_group VARCHAR(50), duplicate_count INTEGER, keywords JSON, "
        "created_at DATETIME)"
    )
    conn.execute(
        "INSERT INTO comments (rpid, video_id, uid, uname, content) "
        "VALUES ('old1', 1, 1, 'u', '旧评论')"
    )
    conn.commit()
    conn.close()

    m = DatabaseManager(path)  # 触发迁移
    cols = {c["name"] for c in inspect(m.engine).get_columns("comments")}
    assert {"root_rpid", "parent_rpid", "context_status"} <= cols
    # 旧行保留，context_status 为 NULL（语义层视为 unknown）
    session = m.get_session()
    try:
        row = session.query(Comment).filter_by(rpid="old1").first()
        assert row is not None
        assert row.context_status is None
    finally:
        session.close()
    # 二次迁移幂等
    m.create_tables()


def test_legacy_null_context_treated_unknown_in_snapshot(tmp_path):
    """旧 NULL 上下文行进入 snapshot 时按 unknown 处理，不冒充顶层。"""
    from modules.comment.semantic.store import SemanticStore
    m = DatabaseManager(str(tmp_path / "legacy_snap.db"))
    session = m.get_session()
    try:
        v = Video(bvid=BVID, title="t")
        session.add(v)
        session.commit()
        vid = v.id
        # 直接插入 context_status NULL 的行
        session.execute(text(
            "INSERT INTO comments (rpid, video_id, content, context_status) "
            "VALUES ('leg', :vid, '遗留', NULL)"
        ), {"vid": vid})
        session.commit()
    finally:
        session.close()
    store = SemanticStore(session_factory=m.get_session)
    snap = store.snapshot(BVID)
    assert snap["counts"]["eligible"] == 0
    assert snap["counts"]["excluded_context"] == 1
    assert snap["items"][0]["context_status"] == "unknown"
