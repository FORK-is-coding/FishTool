"""评论监控动作 Mixin 契约测试（modules/comment/_monitor.py）。

覆盖 MonitorActionsMixin 的三个监控入口：
- add_custom_keywords：自定义关键词累加
- monitor_video：采集 -> 情感 -> 去重 -> 预警 -> 落库的完整编排
- monitor_multiple_videos：批量聚合并统计成功数
- monitor_user_account：账号级监控（昵称容错 + 视频列表 -> 批量监控）

测试策略：
- 监控器/采集器/去重器/情感分析器全部使用真实实现，只有 B站客户端与限频器是契约级假对象；
- SQLite 走 tmp_path 下真实库（打桩 storage_mixin / _alerts 的 get_session）；
- 采集协程由假客户端在首轮返回 is_end，配合 asyncio.wait_for 双保险，不会挂死。
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from core.database import Comment, CommentAlert, CommentAlert as _CommentAlert, DatabaseManager
from modules.comment import _alerts as alerts_module
from modules.comment.collector import CommentCollector
from modules.comment.collector import storage_mixin as storage_module
from modules.comment.monitor import CommentMonitor

BVID = "BV1monitor"
BVID2 = "BV2monitor"


def run(coro):
    """同步测试内执行单次协程，并加 5 秒超时双保险。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


# ---------------------------------------------------------------------------
# 契约级替身
# ---------------------------------------------------------------------------


class FakeRateLimiter:
    """契约级限频器：记录端点，不真正等待。"""

    def __init__(self):
        self.endpoints = []

    async def acquire(self, endpoint="unknown"):
        """记录一次令牌获取调用。"""
        self.endpoints.append(endpoint)


class FakeCommentAPI:
    """契约级假 B站客户端：按 aid 返回热门/普通评论分页。

    Args:
        aid_replies: {aid: 热门评论原始列表}
        aid_normal_pages: {aid: [分页原始响应, ...]}，用尽后返回空页
        view_map: {bvid: 视频详情响应}
        user_info: get_user_info 返回值
        user_info_error: get_user_info 抛出的异常
        user_videos: 空间投稿接口的 vlist
    """

    def __init__(self, aid_replies=None, aid_normal_pages=None, view_map=None,
                 user_info=None, user_info_error=None, user_videos=None):
        self.calls = []
        self._replies = {aid: list(items) for aid, items in (aid_replies or {}).items()}
        self._pages = {aid: list(pages) for aid, pages in (aid_normal_pages or {}).items()}
        self._page_cursor = {}
        self._view_map = view_map or {"BV1monitor": {"aid": 101}, "BV2monitor": {"aid": 102}}
        self._user_info = user_info if user_info is not None else {"data": {"name": "测试UP"}}
        self._user_info_error = user_info_error
        self._user_videos = user_videos or []
        self.rate_limiter = None

    async def get(self, url, params=None, need_sign=False):
        """返回与真实接口同构的响应体。"""
        params = dict(params or {})
        self.calls.append({"url": url, "params": params, "need_sign": need_sign})
        if "web-interface/view" in url:
            return self._view_map.get(params.get("bvid"))
        if "arc/search" in url:
            return {"list": {"vlist": list(self._user_videos)}}
        aid = params.get("oid")
        if params.get("mode") == 3:
            return {"replies": self._replies.get(aid, [])}
        pages = self._pages.get(aid, [])
        index = self._page_cursor.get(aid, 0)
        if index < len(pages):
            self._page_cursor[aid] = index + 1
            return pages[index]
        return {"replies": []}

    async def get_user_info(self, uid):
        """可选地抛错，用于验证昵称获取失败不阻塞主流程。"""
        if self._user_info_error:
            raise self._user_info_error
        return self._user_info


def make_reply(rpid: int, content: str, hours_ago: int = 1, aid: int = 101) -> dict:
    """构造一条与 B站评论接口同构的原始评论。"""
    return {
        "rpid": rpid,
        "oid": aid,
        "member": {"mid": 1000 + rpid, "uname": f"用户{rpid}", "avatar": "", "level_info": {"current_level": 3}},
        "content": {"message": content},
        "ctime": int((datetime.now() - timedelta(hours=hours_ago)).timestamp()),
        "like": rpid,
        "rcount": 0,
    }


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """把监控链路涉及的两个模块级 get_session 都指向 tmp_path 下的真实库。"""
    manager = DatabaseManager(str(tmp_path / "comment_monitor.db"))
    monkeypatch.setattr(alerts_module, "get_session", manager.get_session)
    monkeypatch.setattr(storage_module, "get_session", manager.get_session)
    return manager


def build_monitor(api=None, db=None, alert_callback=None, hot_replies=None):
    """装配真实监控器，并把限频器换成不等待的契约级替身。

    Args:
        api: 可选的假客户端；缺省时为一轮即结束的默认响应。
        db: 临时数据库管理器（用于断言落库结果）。
        alert_callback: 可选的预警回调。
        hot_replies: 热门评论原始列表，仅在构造默认假客户端时生效。

    Returns:
        (monitor, api, limiter) 三元组，便于断言调用轨迹。
    """
    api = api or FakeCommentAPI(aid_replies={101: hot_replies or []})
    monitor = CommentMonitor(api=api, alert_callback=alert_callback)
    limiter = FakeRateLimiter()
    monitor.collector.rate_limiter = limiter
    assert isinstance(monitor.collector, CommentCollector)
    return monitor, api, limiter


def count_rows(manager, model) -> int:
    """统计临时库中某张表的行数。"""
    session = manager.get_session()
    try:
        return session.query(model).count()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# add_custom_keywords
# ---------------------------------------------------------------------------


def test_add_custom_keywords_accumulates_and_deduplicates():
    """多次添加应累加去重，空列表不改变状态。"""
    monitor, _, _ = build_monitor()

    monitor.add_custom_keywords(["抄袭", "举报"])
    monitor.add_custom_keywords(["举报"])
    monitor.add_custom_keywords([])

    assert monitor.custom_keywords == {"抄袭", "举报"}


# ---------------------------------------------------------------------------
# monitor_video
# ---------------------------------------------------------------------------


def test_monitor_video_returns_failure_when_no_comments(db):
    """未采集到评论时返回失败结果与空大屏结构。"""
    monitor, _, _ = build_monitor(hot_replies=[])

    result = run(monitor.monitor_video(BVID))

    assert result["success"] is False
    assert result["collected_count"] == 0
    assert result["processed_count"] == 0
    assert result["warning"] is None
    assert "未采集到评论" in result["error"]
    assert result["visualization"]["top10_voice_comments"] == []
    assert "monitored_at" not in result


def test_monitor_video_surfaces_save_warning_when_database_unavailable(monkeypatch, db):
    """落库失败时即使没有评论，也必须把采集器的 last_save_result.warning 透出。"""
    monitor, _, _ = build_monitor(hot_replies=[])

    def broken_session():
        raise RuntimeError("锁表")

    monkeypatch.setattr(storage_module, "get_session", broken_session)

    result = run(monitor.monitor_video(BVID))

    assert result["success"] is False
    assert result["collected_count"] == 0
    assert "落库失败" in result["warning"]
    assert "锁表" in result["warning"]


def test_monitor_video_runs_full_pipeline_and_persists(db):
    """完整链路：热门+普通合并、情感回写、去重统计、评论落库。"""
    hot = [make_reply(1, "这个视频真棒", hours_ago=2), make_reply(2, "普通评论", hours_ago=4)]
    normal_page = {"replies": [make_reply(3, "第三条评论", hours_ago=6)], "cursor": {"is_end": True}}
    api = FakeCommentAPI(aid_replies={101: hot}, aid_normal_pages={101: [normal_page]})
    monitor, _, limiter = build_monitor(api=api)

    result = run(monitor.monitor_video(BVID))

    assert result["success"] is True
    assert result["collected_count"] == 3
    assert result["processed_count"] == 3
    assert result["dedup_result"]["original_count"] == 3
    assert result["sentiment_result"]["total_count"] == 3
    # 情感标签已回写到原评论，大屏按真实标签统计。
    assert sum(result["visualization"]["sentiment_distribution"].values()) == 3
    assert len(result["visualization"]["top10_voice_comments"]) == 3
    assert result["visualization"]["dedup_statistics"]["before_count"] == 3
    assert count_rows(db, Comment) == 3
    assert limiter.endpoints == ["comment", "comment"]


def test_monitor_video_can_skip_sentiment_and_dedup(db):
    """关闭情感与去重时两者结果均为 None，处理数等于采集数。"""
    monitor, _, _ = build_monitor(hot_replies=[make_reply(1, "a"), make_reply(2, "b")])

    result = run(monitor.monitor_video(BVID, enable_dedup=False, enable_sentiment=False))

    assert result["dedup_result"] is None
    assert result["sentiment_result"] is None
    assert result["processed_count"] == result["collected_count"] == 2
    assert result["visualization"]["dedup_statistics"]["removed_count"] == 0


def test_monitor_video_fast_strategy_only_hits_hot_endpoint(db):
    """fast 策略只走热门接口，请求参数中的 mode=3 且不出现普通分页。"""
    monitor, api, _ = build_monitor(hot_replies=[make_reply(1, "热门")])

    result = run(monitor.monitor_video(BVID, strategy=CommentCollector.STRATEGY_FAST))

    modes = [call["params"].get("mode") for call in api.calls if "reply/main" in call["url"]]
    assert modes == [3]
    assert result["collected_count"] == 1
    assert CommentCollector.get_progress(BVID)["phase"] == "done"
    assert CommentCollector.get_progress(BVID)["finished"] is True


def test_monitor_video_raises_alerts_on_custom_keyword(db):
    """自定义关键词命中时，预警应进入结果并写入 comment_alerts 表。"""
    monitor, _, _ = build_monitor(hot_replies=[make_reply(1, "我要投诉这个活动"), make_reply(2, "无关内容")])
    monitor.add_custom_keywords(["投诉"])

    result = run(monitor.monitor_video(BVID))

    assert [alert["type"] for alert in result["alerts"]] == ["custom_keywords"]
    assert count_rows(db, CommentAlert) == 1


def test_monitor_video_keeps_warning_when_persist_fails(tmp_path, monkeypatch, db):
    """落库失败不改变采集结果，但必须在响应里显式返回 warning。"""
    monitor, _, _ = build_monitor(hot_replies=[make_reply(1, "内容")])

    def broken_session():
        raise RuntimeError("磁盘写满")

    monkeypatch.setattr(storage_module, "get_session", broken_session)

    result = run(monitor.monitor_video(BVID))

    assert result["success"] is True
    assert result["collected_count"] == 1
    assert "落库失败" in result["warning"]
    assert count_rows(db, Comment) == 0


# ---------------------------------------------------------------------------
# monitor_multiple_videos
# ---------------------------------------------------------------------------


def test_monitor_multiple_videos_aggregates_alerts_and_success_count(db):
    """批量监控应聚合每个视频的预警，并跳过无评论的视频。"""
    api = FakeCommentAPI(aid_replies={101: [make_reply(1, "活动征稿来了")]})
    monitor, _, _ = build_monitor(api=api)
    monitor.add_custom_keywords(["征稿"])

    summary = run(monitor.monitor_multiple_videos([BVID, BVID2]))

    assert summary["total_videos"] == 2
    assert summary["successful_count"] == 1
    assert summary["total_alerts"] == 1
    assert len(summary["results"]) == 2
    assert summary["results"][1]["success"] is False
    assert summary["monitored_at"]


def test_monitor_multiple_videos_handles_empty_input(db):
    """空 BV 列表返回全 0 汇总，不发起任何请求。"""
    monitor, api, _ = build_monitor()

    summary = run(monitor.monitor_multiple_videos([]))

    assert summary["total_videos"] == 0
    assert summary["successful_count"] == 0
    assert summary["total_alerts"] == 0
    assert api.calls == []


# ---------------------------------------------------------------------------
# monitor_user_account
# ---------------------------------------------------------------------------


def test_monitor_user_account_reports_missing_videos(db):
    """账号无投稿时返回失败结果，但保留昵称字段。"""
    api = FakeCommentAPI(user_info={"data": {"name": "空账号UP"}}, user_videos=[])
    monitor, _, _ = build_monitor(api=api)

    result = run(monitor.monitor_user_account("12345"))

    assert result["success"] is False
    assert result["error"] == "未找到视频"
    assert result["uid"] == "12345"
    assert result["nickname"] == "空账号UP"


def test_monitor_user_account_monitors_all_videos(db):
    """账号监控应透传视频列表并批量监控其评论。"""
    api = FakeCommentAPI(
        aid_replies={101: [make_reply(1, "第一条")], 102: [make_reply(2, "第二条")]},
        user_videos=[{"bvid": BVID, "title": "视频1", "aid": 101}, {"bvid": BVID2, "title": "视频2", "aid": 102}],
        user_info={"data": {"name": "UP主昵称"}},
    )
    monitor, _, _ = build_monitor(api=api)

    result = run(monitor.monitor_user_account("12345", video_limit=5))

    assert result["nickname"] == "UP主昵称"
    assert result["username"] == "UP主昵称"
    assert len(result["videos"]) == 2
    assert result["total_videos"] == 2
    assert result["successful_count"] == 2
    space_call = [call for call in api.calls if "arc/search" in call["url"]][0]
    assert space_call["params"]["ps"] == 5
    assert space_call["need_sign"] is True


def test_monitor_user_account_survives_nickname_lookup_failure(db):
    """昵称接口失败只降级为空昵称，不影响视频采集。"""
    api = FakeCommentAPI(
        aid_replies={101: [make_reply(1, "内容")]},
        user_videos=[{"bvid": BVID, "title": "视频1", "aid": 101}],
        user_info_error=RuntimeError("412 风控"),
    )
    monitor, _, _ = build_monitor(api=api)

    result = run(monitor.monitor_user_account("12345"))

    assert result["nickname"] == ""
    assert result["successful_count"] == 1


def test_monitor_user_account_rejects_non_numeric_uid_but_keeps_collecting(db):
    """UID 非数字时昵称退化为空，仍继续按原 UID 采集视频。"""
    api = FakeCommentAPI(
        aid_replies={101: [make_reply(1, "内容")]},
        user_videos=[{"bvid": BVID, "title": "视频1", "aid": 101}],
    )
    monitor, _, _ = build_monitor(api=api)

    result = run(monitor.monitor_user_account("non-numeric"))

    assert result["nickname"] == ""
    assert result["uid"] == "non-numeric"
    assert result["successful_count"] == 1
