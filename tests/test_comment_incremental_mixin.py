"""评论增量采集 Mixin 契约测试（modules/comment/collector/incremental_mixin.py）。

覆盖：
- collect_incremental_comments：游标分页、命中断点提前返回、异常响应终止、重复游标防死循环
- _get_last_rpid：Task.checkpoint 优先、评论表最新 rpid 回退、无数据返回 None

测试策略：
- 采集器使用真实 CommentCollector，只有 B站客户端与限频器是契约级假对象；
- SQLite 走 tmp_path 下真实库；
- 分页循环由假客户端控制终止条件，外加 asyncio.wait_for 双保险。
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from core.database import Comment, DatabaseManager, Task, Video
from modules.comment.collector import CommentCollector
from modules.comment.collector import incremental_mixin as incremental_module
from modules.comment.collector import storage_mixin as storage_module

BVID = "BV1incremental"
AID = 101


def run(coro):
    """同步测试内执行单次协程，并加 5 秒超时双保险。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


# ---------------------------------------------------------------------------
# 契约级替身
# ---------------------------------------------------------------------------


class FakeRateLimiter:
    """契约级限频器：只记录端点。"""

    def __init__(self):
        self.endpoints = []

    async def acquire(self, endpoint="unknown"):
        """记录一次令牌获取。"""
        self.endpoints.append(endpoint)


class FakeIncrementalAPI:
    """契约级假 B站客户端：按 oid 顺序吐出预设分页。"""

    def __init__(self, pages_by_oid=None, view_map=None):
        self.calls = []
        self._pages = pages_by_oid or {}
        self._index = {}
        self._view_map = view_map if view_map is not None else {BVID: {"aid": AID}}

    async def get(self, url, params=None, need_sign=False):
        """返回视频详情或下一页评论响应。"""
        params = dict(params or {})
        self.calls.append({"url": url, "params": params, "need_sign": need_sign})
        if "web-interface/view" in url:
            return self._view_map.get(params.get("bvid"))
        oid = params.get("oid")
        pages = self._pages.get(oid, [])
        index = self._index.get(oid, 0)
        self._index[oid] = index + 1
        if index < len(pages):
            return pages[index]
        return {"replies": []}


class RunawayIncrementalAPI:
    """病态客户端：永远返回新 rpid 与新游标，用预算终止以验证不挂死。"""

    def __init__(self, budget=50):
        self.calls = 0
        self.budget = budget

    async def get(self, url, params=None, need_sign=False):
        """无限翻页，超出预算即抛错，避免测试挂死。"""
        if "web-interface/view" in url:
            return {"aid": AID}
        self.calls += 1
        if self.calls > self.budget:
            raise RuntimeError("page budget exhausted")
        return {
            "replies": [make_reply(1000 + self.calls)],
            "cursor": {"pagination_reply": {"next_offset": f"offset-{self.calls}"}},
        }


def make_reply(rpid: int, content: str = "内容") -> dict:
    """构造一条与 B站评论接口同构的原始评论。"""
    return {
        "rpid": rpid,
        "oid": AID,
        "member": {"mid": 2000 + rpid, "uname": f"用户{rpid}", "level_info": {"current_level": 2}},
        "content": {"message": content},
        "ctime": 1766400000 + rpid,
        "like": 0,
        "rcount": 0,
    }


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """把增量 Mixin 与入库 Mixin 的模块级 get_session 指向 tmp_path 真实库。"""
    manager = DatabaseManager(str(tmp_path / "incremental.db"))
    monkeypatch.setattr(incremental_module, "get_session", manager.get_session)
    monkeypatch.setattr(storage_module, "get_session", manager.get_session)
    return manager


def build_collector(api) -> CommentCollector:
    """装配真实采集器并替换限频器。"""
    collector = CommentCollector(api=api)
    collector.rate_limiter = FakeRateLimiter()
    return collector


def seed_video(manager, bvid=BVID) -> int:
    """写入视频记录并返回主键。"""
    session = manager.get_session()
    try:
        video = Video(bvid=bvid, title="视频")
        session.add(video)
        session.commit()
        return video.id
    finally:
        session.close()


def seed_comment(manager, video_id: int, rpid: int, ctime: datetime) -> None:
    """写入一条历史评论，用于断点回退。"""
    session = manager.get_session()
    try:
        session.add(Comment(rpid=rpid, video_id=video_id, uid=1, uname="u", content="历史", ctime=ctime, sentiment="neutral"))
        session.commit()
    finally:
        session.close()


def seed_task(manager, bvid: str, checkpoint) -> None:
    """写入一条评论采集任务（含断点）。"""
    session = manager.get_session()
    try:
        session.add(Task(task_type="comment_collect", params={"bvid": bvid}, status="completed",
                          checkpoint=checkpoint, started_at=datetime.now(), completed_at=datetime.now()))
        session.commit()
    finally:
        session.close()


def count_comments(manager) -> int:
    """统计临时库评论行数。"""
    session = manager.get_session()
    try:
        return session.query(Comment).count()
    finally:
        session.close()


def rpids(comments) -> list:
    """提取评论结果中的 rpid 列表。"""
    return [item["rpid"] for item in comments]


# ---------------------------------------------------------------------------
# collect_incremental_comments
# ---------------------------------------------------------------------------


def test_incremental_returns_empty_when_video_oid_missing(db):
    """视频详情拿不到 aid 时直接返回空，不发起评论请求。"""
    api = FakeIncrementalAPI(pages_by_oid={AID: [{"replies": [make_reply(1)]}]}, view_map={})
    collector = build_collector(api)

    result = run(collector.collect_incremental_comments(BVID))

    assert result == []
    assert all("reply/main" not in call["url"] for call in api.calls)


def test_incremental_collects_until_first_page_end(db):
    """首轮即 is_end 时收敛，并把新增评论落库。"""
    page = {"replies": [make_reply(1), make_reply(2)], "cursor": {"is_end": True}}
    api = FakeIncrementalAPI(pages_by_oid={AID: [page]})
    collector = build_collector(api)

    result = run(collector.collect_incremental_comments(BVID))

    assert rpids(result) == [1, 2]
    assert count_comments(db) == 2
    # 首轮不带 pagination_str，符合 reply/main 游标分页契约。
    assert "pagination_str" not in api.calls[-1]["params"]


def test_incremental_stops_when_hitting_checkpoint_rpid(db):
    """checkpoint 断点为 int 时能与接口 rpid 命中，命中即返回且不采集更旧评论。"""
    seed_video(db)
    seed_task(db, BVID, {"last_rpid": 5})
    page = {"replies": [make_reply(7), make_reply(5), make_reply(3)], "cursor": {"is_end": True}}
    api = FakeIncrementalAPI(pages_by_oid={AID: [page]})
    collector = build_collector(api)

    result = run(collector.collect_incremental_comments(BVID))

    assert rpids(result) == [7]
    assert count_comments(db) == 1


def test_incremental_returns_empty_when_page_starts_at_checkpoint_rpid(db):
    """断点就是首条时无新增，返回空且不写库。"""
    seed_video(db)
    seed_task(db, BVID, {"last_rpid": 9})
    api = FakeIncrementalAPI(pages_by_oid={AID: [{"replies": [make_reply(9)]}]})
    collector = build_collector(api)

    result = run(collector.collect_incremental_comments(BVID))

    assert result == []
    assert count_comments(db) == 0


def test_incremental_breakpoint_from_comment_table_never_matches(db):
    """固化现状缺陷：评论表回退的断点是字符串，与接口 int rpid 恒不相等，增量退化为全量。"""
    video_id = seed_video(db)
    seed_comment(db, video_id, rpid=5, ctime=datetime.now() - timedelta(hours=1))
    page = {"replies": [make_reply(7), make_reply(5), make_reply(3)], "cursor": {"is_end": True}}
    api = FakeIncrementalAPI(pages_by_oid={AID: [page]})
    collector = build_collector(api)

    result = run(collector.collect_incremental_comments(BVID))

    # 断点 '5'（str）匹配不到 rpid 5（int），因此断点之后的旧评论也被采集。
    assert rpids(result) == [7, 5, 3]
    # 已存在的 rpid=5 在入库阶段被去重，不会产生脏数据。
    assert count_comments(db) == 3


def test_incremental_stops_on_invalid_payload(db):
    """响应缺少 replies 字段时终止翻页，避免死循环。"""
    api = FakeIncrementalAPI(pages_by_oid={AID: [{"code": -412}, {"replies": [make_reply(1)]}]})
    collector = build_collector(api)

    result = run(collector.collect_incremental_comments(BVID))

    assert result == []
    assert count_comments(db) == 0


def test_incremental_stops_on_empty_replies(db):
    """当前页无评论视为翻到末页。"""
    api = FakeIncrementalAPI(pages_by_oid={AID: [{"replies": []}]})
    collector = build_collector(api)

    assert run(collector.collect_incremental_comments(BVID)) == []


def test_incremental_stops_on_repeated_cursor(db):
    """分页返回重复游标时立即停止，避免重复累计。"""
    page1 = {"replies": [make_reply(1)], "cursor": {"pagination_reply": {"next_offset": "same"}}}
    page2 = {"replies": [make_reply(2)], "cursor": {"pagination_reply": {"next_offset": "same"}}}
    api = FakeIncrementalAPI(pages_by_oid={AID: [page1, page2]})
    collector = build_collector(api)

    result = run(collector.collect_incremental_comments(BVID))

    assert rpids(result) == [1, 2]
    assert count_comments(db) == 2


def test_incremental_wraps_cursor_next_into_pagination_str(db):
    """兼容旧结构 cursor.next，并包装成 pagination_str 传给下一页。"""
    page1 = {"replies": [make_reply(1)], "cursor": {"next": 2}}
    page2 = {"replies": [make_reply(2)], "cursor": {"is_end": True}}
    api = FakeIncrementalAPI(pages_by_oid={AID: [page1, page2]})
    collector = build_collector(api)

    result = run(collector.collect_incremental_comments(BVID))

    assert rpids(result) == [1, 2]
    assert api.calls[-1]["params"]["pagination_str"] == '{"offset":"2"}'


def test_incremental_terminates_when_cursor_never_ends(db):
    """固化现状：游标永不结束且持续返回新评论时，循环只能靠外部预算终止。"""
    api = RunawayIncrementalAPI(budget=30)
    collector = build_collector(api)

    with pytest.raises(RuntimeError, match="page budget exhausted"):
        run(collector.collect_incremental_comments(BVID))

    assert api.calls == 31


# ---------------------------------------------------------------------------
# _get_last_rpid
# ---------------------------------------------------------------------------


def test_get_last_rpid_returns_none_without_video(db):
    """视频不在库中说明从未采集，返回 None 走全量。"""
    collector = build_collector(FakeIncrementalAPI())

    assert run(collector._get_last_rpid(BVID)) is None


def test_get_last_rpid_prefers_task_checkpoint(db):
    """Task.checkpoint 中的 last_rpid 优先级最高。"""
    seed_video(db)
    seed_task(db, BVID, {"last_rpid": 42})
    collector = build_collector(FakeIncrementalAPI())

    assert run(collector._get_last_rpid(BVID)) == 42


def test_get_last_rpid_falls_back_to_latest_comment(db):
    """无 checkpoint 时回退到评论表最新（ctime 最大）的 rpid，类型为 str（固化现状）。"""
    video_id = seed_video(db)
    now = datetime.now()
    seed_comment(db, video_id, rpid=1, ctime=now - timedelta(days=2))
    seed_comment(db, video_id, rpid=2, ctime=now)
    seed_comment(db, video_id, rpid=3, ctime=now - timedelta(days=1))
    collector = build_collector(FakeIncrementalAPI())

    # Comment.rpid 是字符串列，回退路径返回 '2' 而不是 int 2。
    assert run(collector._get_last_rpid(BVID)) == "2"


def test_get_last_rpid_ignores_checkpoint_without_last_rpid(db):
    """checkpoint 存在但没有 last_rpid 时继续回退查评论表。"""
    video_id = seed_video(db)
    seed_comment(db, video_id, rpid=8, ctime=datetime.now())
    seed_task(db, BVID, {"phase": "normal"})
    collector = build_collector(FakeIncrementalAPI())

    assert run(collector._get_last_rpid(BVID)) == "8"


def test_get_last_rpid_returns_none_when_no_comment_history(db):
    """有视频但没有任何评论时返回 None。"""
    seed_video(db)
    collector = build_collector(FakeIncrementalAPI())

    assert run(collector._get_last_rpid(BVID)) is None


def test_get_last_rpid_raises_when_session_unavailable(monkeypatch):
    """固化现状缺陷：会话创建失败时 finally 引用了未初始化的 session，抛 NameError。"""
    monkeypatch.setattr(incremental_module, "get_session",
                        lambda: (_ for _ in ()).throw(RuntimeError("库挂了")))
    collector = build_collector(FakeIncrementalAPI())

    with pytest.raises(NameError):
        run(collector._get_last_rpid(BVID))
