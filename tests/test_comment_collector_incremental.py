"""评论采集器增量采集 Mixin 的契约级测试。

覆盖 modules/comment/collector/incremental_mixin.py：
- collect_incremental_comments：断点命中即停、游标翻页、重复游标停止、空页/无数据停止、自动落库
- _get_last_rpid：Task.checkpoint 优先、评论表回退、无数据/异常降级

长循环通过「单页脚本 + 明确终止条件」驱动，外层再用 asyncio.wait_for 兜底，
避免任何情形下裸挂。数据库使用 tmp_path 下的真实 SQLite。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from core.database import Comment, DatabaseManager, Task, Video
from modules.comment.collector import CommentCollector
from modules.comment.collector import incremental_mixin as incremental_module

WAIT_SECONDS = 5.0


class FakeLimiter:
    """记录调用参数的假限频器，永不阻塞。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def acquire(self, *args, **kwargs):
        """记录一次限频调用。"""
        self.calls.append((args, kwargs))


class FakeAPI:
    """脚本化返回的契约级假 API。"""

    def __init__(self, responses=None) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict] = []

    async def get(self, url, params=None, need_sign=False):
        """记录请求并弹出下一条脚本响应。"""
        self.calls.append({"url": url, "params": params, "need_sign": need_sign})
        if not self._responses:
            raise RuntimeError("没有更多脚本化响应")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _reply(rpid) -> dict:
    """构造一条最小可解析的原始评论。"""
    return {
        "rpid": rpid,
        "member": {"mid": rpid, "uname": f"u{rpid}"},
        "content": {"message": f"内容{rpid}"},
        "ctime": 1700000000,
    }


def _collector(responses, *, oid=999, last_rpid=None, save_recorder=None) -> CommentCollector:
    """构造注入假 api/限频器并固定 oid 与断点的采集器。"""
    api = FakeAPI(responses=list(responses))
    collector = CommentCollector(api=api, rate_limiter=FakeLimiter())

    async def _const_oid(bvid):
        """固定返回预置 oid。"""
        return oid

    async def _const_last(bvid):
        """固定返回预置断点。"""
        return last_rpid

    collector._get_video_oid = _const_oid
    collector._get_last_rpid = _const_last

    if save_recorder is not None:
        async def _record(bvid, comments):
            """记录落库调用并把评论保留为普通列表。"""
            save_recorder.append((bvid, list(comments)))
            return {"success": True, "saved_count": len(comments), "warning": None}

        collector._save_comments_to_db = _record
    return collector


def _run(coro):
    """带外层超时兜底的协程运行器。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=WAIT_SECONDS))


# --------------------------------------------------------------------- 增量采集

def test_incremental_stops_at_last_rpid() -> None:
    """遇到上次断点评论时应立即结束并落库本批新增。"""
    saved: list = []
    collector = _collector(
        [{"replies": [_reply(30), _reply(20), _reply(10)]}],
        last_rpid=10,
        save_recorder=saved,
    )

    result = _run(collector.collect_incremental_comments("BV1"))

    assert [item["rpid"] for item in result] == [30, 20]
    assert saved and saved[0][0] == "BV1"
    assert [item["rpid"] for item in saved[0][1]] == [30, 20]


def test_incremental_no_matching_rpid_returns_empty_without_save() -> None:
    """当前页无评论时返回空且不落库。"""
    saved: list = []
    collector = _collector([{"replies": []}], last_rpid=10, save_recorder=saved)

    result = _run(collector.collect_incremental_comments("BV1"))

    assert result == []
    assert saved == []


def test_incremental_returns_empty_without_oid() -> None:
    """oid 缺失时直接返回空且不发请求。"""
    saved: list = []
    collector = _collector([], oid=None, save_recorder=saved)

    assert _run(collector.collect_incremental_comments("BV1")) == []
    assert collector.api.calls == []
    assert saved == []


def test_incremental_stops_on_bad_payload() -> None:
    """返回体缺 replies 时应终止翻页并落库已采集部分。"""
    saved: list = []
    collector = _collector([
        {"replies": [_reply(1)], "cursor": {"pagination_reply": {"next_offset": "n1"}}},
        {"bad": "payload"},
    ], save_recorder=saved)

    result = _run(collector.collect_incremental_comments("BV1"))

    assert [item["rpid"] for item in result] == [1]
    assert saved and len(saved[0][1]) == 1


def test_incremental_follows_cursor_and_wraps_offset() -> None:
    """第二页应携带包装后的 pagination_str。"""
    collector = _collector([
        {"replies": [_reply(2)], "cursor": {"pagination_reply": {"next_offset": "abc"}}},
        {"replies": [_reply(1)], "cursor": {"is_end": True}},
    ])

    result = _run(collector.collect_incremental_comments("BV1"))

    assert [item["rpid"] for item in result] == [2, 1]
    assert "pagination_str" not in collector.api.calls[0]["params"]
    assert collector.api.calls[1]["params"]["pagination_str"] == '{"offset":"abc"}'


def test_incremental_stops_on_is_end() -> None:
    """cursor.is_end 为真时应停止翻页。"""
    collector = _collector([
        {"replies": [_reply(1)], "cursor": {"is_end": True}},
        {"replies": [_reply(99)]},
    ])

    result = _run(collector.collect_incremental_comments("BV1"))

    assert [item["rpid"] for item in result] == [1]
    assert len(collector.api.calls) == 1


def test_incremental_stops_on_missing_cursor() -> None:
    """缺少下一页游标时应停止。"""
    collector = _collector([{"replies": [_reply(1)]}])

    result = _run(collector.collect_incremental_comments("BV1"))

    assert [item["rpid"] for item in result] == [1]
    assert len(collector.api.calls) == 1


def test_incremental_stops_on_repeated_cursor() -> None:
    """重复游标应触发保护性停止，防止死循环。"""
    collector = _collector([
        {"replies": [_reply(2)], "cursor": {"pagination_reply": {"next_offset": "same"}}},
        {"replies": [_reply(1)], "cursor": {"pagination_reply": {"next_offset": "same"}}},
    ])

    result = _run(collector.collect_incremental_comments("BV1"))

    assert [item["rpid"] for item in result] == [2, 1]
    assert len(collector.api.calls) == 2


def test_incremental_uses_comment_endpoint_bucket() -> None:
    """增量采集应走评论专用限频桶。"""
    collector = _collector([{"replies": []}])

    _run(collector.collect_incremental_comments("BV1"))

    assert collector.rate_limiter.calls[0] == ((), {"endpoint": "comment"})


# --------------------------------------------------------------------- 断点查询

@pytest.fixture()
def db_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """构造真实临时库并接管模块级 get_session。"""
    manager = DatabaseManager(str(tmp_path / "incremental.db"))
    monkeypatch.setattr(incremental_module, "get_session", manager.get_session)
    return manager


def _collector_for_db() -> CommentCollector:
    """构造只用于断点查询的采集器。"""
    return CommentCollector(api=object(), rate_limiter=FakeLimiter())


def _seed_video(manager: DatabaseManager, bvid: str = "BV1") -> int:
    """插入一条视频记录并返回其主键。"""
    session = manager.get_session()
    try:
        video = Video(bvid=bvid, title="标题")
        session.add(video)
        session.commit()
        return video.id
    finally:
        session.close()


def test_get_last_rpid_returns_none_without_video(db_env) -> None:
    """视频不存在时断点为 None。"""
    assert _run(_collector_for_db()._get_last_rpid("BV1")) is None


def test_get_last_rpid_prefers_task_checkpoint(db_env) -> None:
    """存在 Task.checkpoint 时应优先采用 last_rpid。"""
    _seed_video(db_env)
    session = db_env.get_session()
    try:
        session.add(Task(
            task_type="comment_collect",
            params={"bvid": "BV1"},
            status="completed",
            checkpoint={"last_rpid": 888},
            created_at=datetime.now(),
        ))
        session.commit()
    finally:
        session.close()

    assert _run(_collector_for_db()._get_last_rpid("BV1")) == 888


def test_get_last_rpid_falls_back_to_latest_comment(db_env) -> None:
    """无断点时回退到评论表最新 ctime 的 rpid。"""
    video_id = _seed_video(db_env)
    session = db_env.get_session()
    try:
        session.add(Comment(rpid="1", video_id=video_id, content="old", ctime=datetime(2026, 1, 1)))
        session.add(Comment(rpid="2", video_id=video_id, content="new", ctime=datetime(2026, 2, 1)))
        session.commit()
    finally:
        session.close()

    assert _run(_collector_for_db()._get_last_rpid("BV1")) == "2"


def test_get_last_rpid_ignores_checkpoint_without_last_rpid(db_env) -> None:
    """checkpoint 不含 last_rpid 时应继续回退评论表。"""
    video_id = _seed_video(db_env)
    session = db_env.get_session()
    try:
        session.add(Task(
            task_type="comment_collect",
            params={"bvid": "BV1"},
            status="completed",
            checkpoint={"other": 1},
            created_at=datetime.now(),
        ))
        session.add(Comment(rpid="7", video_id=video_id, content="c", ctime=datetime(2026, 1, 1)))
        session.commit()
    finally:
        session.close()

    assert _run(_collector_for_db()._get_last_rpid("BV1")) == "7"


def test_get_last_rpid_returns_none_without_comments(db_env) -> None:
    """视频存在但没有任何评论与断点时返回 None。"""
    _seed_video(db_env)

    assert _run(_collector_for_db()._get_last_rpid("BV1")) is None


def test_get_last_rpid_get_session_error_raises_unbound_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """【缺陷固化】get_session 抛异常时 finally 引用未绑定 session，抛出 UnboundLocalError。

    期望行为本应是降级返回 None（同项目 storage/_alerts 等模块均有 ``if session is not None`` 保护），
    但本方法缺失该保护。此处仅固化当前现状，未修改生产代码。
    """
    def _boom():
        """模拟数据库不可用。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(incremental_module, "get_session", _boom)

    with pytest.raises(UnboundLocalError):
        _run(_collector_for_db()._get_last_rpid("BV1"))
