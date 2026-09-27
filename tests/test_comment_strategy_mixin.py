"""评论分级采集策略 Mixin 契约测试（modules/comment/collector/strategy_mixin.py）。

覆盖：
- _collect_hot_comments：mode=3 热门评论采集与异常降级
- _collect_normal_comments：mode=2 游标翻页、重复页防护、limit 截断与进度回写
- _collect_all_comments：热门+全量普通合并去重、max_count 语义与进度阶段

测试策略：
- 使用真实 CommentCollector，只有 B站客户端与限频器是契约级假对象；
- 翻页循环由假客户端的 is_end/游标控制终止，并配 asyncio.wait_for 双保险；
- 采集进度是模块级内存态，用例开始前显式清理。
"""
import asyncio

import pytest

from core.exceptions import BilibiliAPIError
from modules.comment.collector import CommentCollector

BVID = "BV1strategy"
AID = 101


def run(coro):
    """同步测试内执行单次协程，并加 5 秒超时双保险。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


class FakeRateLimiter:
    """契约级限频器：记录端点且不等待。"""

    def __init__(self):
        self.endpoints = []

    async def acquire(self, endpoint="unknown"):
        """记录一次令牌获取。"""
        self.endpoints.append(endpoint)


class FakeStrategyAPI:
    """契约级假 B站客户端：按 mode 分流返回热门/普通评论。

    Args:
        hot: mode=3 的响应体。
        pages: mode=2 的顺序分页响应体列表。
        view_map: bvid -> 视频详情响应。
        reply_error: 评论接口抛出的异常（详情接口不受影响）。
        normal_error: 仅 mode=2 抛出的异常。
        max_pages: 普通分页预算，超出即抛错，避免测试挂死。
    """

    def __init__(self, hot=None, pages=None, view_map=None, reply_error=None,
                 normal_error=None, max_pages=None):
        self.calls = []
        self._hot = hot if hot is not None else {"replies": []}
        self._pages = list(pages or [])
        self._view_map = view_map if view_map is not None else {BVID: {"aid": AID}}
        self._reply_error = reply_error
        self._normal_error = normal_error
        self._max_pages = max_pages
        self._normal_calls = 0

    async def get(self, url, params=None, need_sign=False):
        """按 URL 与 mode 返回视频详情或评论分页。"""
        params = dict(params or {})
        self.calls.append({"url": url, "params": params, "need_sign": need_sign})
        if "web-interface/view" in url:
            return self._view_map.get(params.get("bvid"))
        if self._reply_error is not None:
            raise self._reply_error
        if params.get("mode") == 3:
            return self._hot
        if self._normal_error is not None:
            raise self._normal_error
        if self._max_pages is not None and self._normal_calls >= self._max_pages:
            raise RuntimeError("page budget exhausted")
        index = self._normal_calls
        self._normal_calls += 1
        if index < len(self._pages):
            return self._pages[index]
        return {"replies": []}


def make_reply(rpid: int) -> dict:
    """构造一条最小可解析的原始评论。"""
    return {
        "rpid": rpid,
        "oid": AID,
        "member": {"mid": 3000 + rpid, "uname": f"用户{rpid}"},
        "content": {"message": f"评论{rpid}"},
        "ctime": 1766400000,
    }


def make_page(rpids, next_offset=None, is_end=False) -> dict:
    """构造一页普通评论响应（含游标结构）。"""
    cursor = {}
    if next_offset is not None:
        cursor["pagination_reply"] = {"next_offset": next_offset}
    if is_end:
        cursor["is_end"] = True
    return {"replies": [make_reply(rpid) for rpid in rpids], "cursor": cursor}


def make_hot(rpids) -> dict:
    """构造热门评论响应。"""
    return {"replies": [make_reply(rpid) for rpid in rpids]}


def build_collector(api) -> CommentCollector:
    """装配真实采集器并替换限频器。"""
    collector = CommentCollector(api=api)
    collector.rate_limiter = FakeRateLimiter()
    return collector


@pytest.fixture(autouse=True)
def clean_progress():
    """每个用例前后都清理采集进度内存态，避免跨用例串味。"""
    CommentCollector.clear_progress(BVID)
    yield
    CommentCollector.clear_progress(BVID)


# ---------------------------------------------------------------------------
# _collect_hot_comments
# ---------------------------------------------------------------------------


def test_collect_hot_comments_uses_hot_mode_and_marks_flag():
    """热门采集走 mode=3、ps=20，且结果 is_hot=True。"""
    api = FakeStrategyAPI(hot=make_hot([1, 2, 3]))
    collector = build_collector(api)

    comments = run(collector._collect_hot_comments(BVID))

    assert [item["rpid"] for item in comments] == [1, 2, 3]
    assert all(item["is_hot"] is True for item in comments)
    reply_call = [call for call in api.calls if "reply/main" in call["url"]][0]
    assert reply_call["params"]["mode"] == 3
    assert reply_call["params"]["ps"] == 20
    assert reply_call["params"]["oid"] == AID
    assert reply_call["need_sign"] is False
    assert collector.rate_limiter.endpoints == ["comment"]


def test_collect_hot_comments_returns_empty_without_oid():
    """拿不到 oid 时直接返回空，不发评论请求。"""
    api = FakeStrategyAPI(view_map={})
    collector = build_collector(api)

    assert run(collector._collect_hot_comments(BVID)) == []
    assert all("reply/main" not in call["url"] for call in api.calls)


def test_collect_hot_comments_returns_empty_on_abnormal_payload():
    """响应缺少 replies 或为空时返回空列表。"""
    assert run(build_collector(FakeStrategyAPI(hot={"code": -412}))._collect_hot_comments(BVID)) == []
    assert run(build_collector(FakeStrategyAPI(hot=None))._collect_hot_comments(BVID)) == []


def test_collect_hot_comments_swallows_api_error_but_not_others():
    """B站 API 异常降级为空列表，其它异常必须冒泡。"""
    assert run(build_collector(FakeStrategyAPI(reply_error=BilibiliAPIError("412")))._collect_hot_comments(BVID)) == []

    with pytest.raises(RuntimeError, match="socket 断开"):
        run(build_collector(FakeStrategyAPI(reply_error=RuntimeError("socket 断开")))._collect_hot_comments(BVID))


# ---------------------------------------------------------------------------
# _collect_normal_comments
# ---------------------------------------------------------------------------


def test_collect_normal_comments_follows_cursor_until_end():
    """跨页采集应把下一页 offset 包进 pagination_str。"""
    api = FakeStrategyAPI(pages=[make_page(range(20), next_offset="A"), make_page([100, 101], is_end=True)])
    collector = build_collector(api)

    comments = run(collector._collect_normal_comments(BVID, limit=100))

    assert len(comments) == 22
    assert comments[0]["rpid"] == 0
    assert comments[-1]["rpid"] == 101
    assert all(item["is_hot"] is False for item in comments)
    normal_calls = [call for call in api.calls if "reply/main" in call["url"]]
    assert "pagination_str" not in normal_calls[0]["params"]
    assert normal_calls[1]["params"]["pagination_str"] == '{"offset":"A"}'
    assert normal_calls[1]["params"]["mode"] == 2


def test_collect_normal_comments_truncates_to_limit():
    """累计超过 limit 时按 limit 截断返回。"""
    api = FakeStrategyAPI(pages=[make_page(range(20), next_offset="A"), make_page(range(20, 40), is_end=True)])
    collector = build_collector(api)

    comments = run(collector._collect_normal_comments(BVID, limit=25))

    assert len(comments) == 25
    # 进度按累计条数回写，可能略大于最终返回条数（固化现状）。
    assert CommentCollector.get_progress(BVID)["collected"] == 40


def test_collect_normal_comments_stops_on_duplicate_page():
    """整页 rpid 全部重复时立即停止，避免虚高统计。"""
    api = FakeStrategyAPI(pages=[make_page([1, 2], next_offset="A"), make_page([1, 2], next_offset="B")])
    collector = build_collector(api)

    comments = run(collector._collect_normal_comments(BVID, limit=100))

    assert [item["rpid"] for item in comments] == [1, 2]


def test_collect_normal_comments_keeps_same_page_duplicates():
    """固化现状缺陷：seen_rpids 在整页解析后才更新，同页重复 rpid 不会被过滤。"""
    api = FakeStrategyAPI(pages=[make_page([1, 1, 2], is_end=True)])
    collector = build_collector(api)

    comments = run(collector._collect_normal_comments(BVID, limit=100))

    # 返回与进度都按 3 条统计，重复项只能靠后续入库阶段兜底。
    assert [item["rpid"] for item in comments] == [1, 1, 2]
    assert CommentCollector.get_progress(BVID)["collected"] == 3


def test_collect_normal_comments_breaks_on_empty_or_missing_payload():
    """空页、空响应与缺 replies 字段都要终止翻页。"""
    assert run(build_collector(FakeStrategyAPI(pages=[{"replies": []}]))._collect_normal_comments(BVID, 100)) == []
    assert run(build_collector(FakeStrategyAPI(pages=[None]))._collect_normal_comments(BVID, 100)) == []
    assert run(build_collector(FakeStrategyAPI(pages=[{"code": 0}]))._collect_normal_comments(BVID, 100)) == []


def test_collect_normal_comments_breaks_without_cursor():
    """无游标信息时只采一页即停。"""
    api = FakeStrategyAPI(pages=[{"replies": [make_reply(1)]}])
    collector = build_collector(api)

    comments = run(collector._collect_normal_comments(BVID, limit=100))

    assert [item["rpid"] for item in comments] == [1]
    assert len([call for call in api.calls if "reply/main" in call["url"]]) == 1


def test_collect_normal_comments_zero_limit_skips_requests():
    """limit=0 时不进入翻页循环，也不发评论请求。"""
    api = FakeStrategyAPI(pages=[make_page([1])])
    collector = build_collector(api)

    assert run(collector._collect_normal_comments(BVID, limit=0)) == []
    assert all("reply/main" not in call["url"] for call in api.calls)


def test_collect_normal_comments_returns_empty_without_oid():
    """无 oid 时短路返回空。"""
    collector = build_collector(FakeStrategyAPI(view_map={}))

    assert run(collector._collect_normal_comments(BVID, limit=100)) == []


def test_collect_normal_comments_swallows_api_error_but_not_others():
    """B站 API 异常降级，非 API 异常冒泡。"""
    assert run(build_collector(FakeStrategyAPI(normal_error=BilibiliAPIError("429")))._collect_normal_comments(BVID, 100)) == []

    with pytest.raises(RuntimeError, match="解析失败"):
        run(build_collector(FakeStrategyAPI(normal_error=RuntimeError("解析失败")))._collect_normal_comments(BVID, 100))


def test_collect_normal_comments_terminates_when_pages_never_end():
    """固化现状：接口永远返回新页且不复用时，循环只受 limit 约束。"""
    api = FakeStrategyAPI(max_pages=5)

    class NeverEndingAPI(FakeStrategyAPI):
        """永远吐出新 rpid 与新游标的分页客户端。"""

        def __init__(self):
            super().__init__(max_pages=5)
            self._counter = 0

        async def get(self, url, params=None, need_sign=False):
            """每次返回一条新评论与递增游标。"""
            if "web-interface/view" in url:
                return {"aid": AID}
            self._counter += 1
            self.calls.append({"url": url, "params": dict(params or {}), "need_sign": need_sign})
            if self._counter > 5:
                raise RuntimeError("page budget exhausted")
            return make_page([self._counter], next_offset=f"cursor-{self._counter}")

    collector = build_collector(NeverEndingAPI())

    with pytest.raises(RuntimeError, match="page budget exhausted"):
        run(collector._collect_normal_comments(BVID, limit=10 ** 9))

    assert api._max_pages == 5


# ---------------------------------------------------------------------------
# _collect_all_comments
# ---------------------------------------------------------------------------


def test_collect_all_comments_merges_hot_first_and_dedups():
    """完整采集 = 热门 + 全量普通，按 rpid 去重且热门在前。"""
    api = FakeStrategyAPI(hot=make_hot([1, 2]), pages=[make_page([2, 3], is_end=True)])
    collector = build_collector(api)

    comments = run(collector._collect_all_comments(BVID, max_count=None))

    assert [item["rpid"] for item in comments] == [1, 2, 3]
    assert comments[0]["is_hot"] is True
    assert comments[-1]["is_hot"] is False


def test_collect_all_comments_records_full_phase_progress():
    """进度应停留在 full 阶段，limit 为 None 表示不限量。"""
    api = FakeStrategyAPI(hot=make_hot([1]), pages=[make_page([2], is_end=True)])
    collector = build_collector(api)

    run(collector._collect_all_comments(BVID, max_count=None))

    progress = CommentCollector.get_progress(BVID)
    assert progress["phase"] == "full"
    assert progress["limit"] is None
    assert progress["collected"] == 2


def test_collect_all_comments_max_count_bounds_normal_page_only():
    """固化现状：max_count 只约束普通评论翻页，合并结果可能超过该上限。"""
    api = FakeStrategyAPI(hot=make_hot([1, 2]), pages=[make_page([2, 3, 4, 5, 6], is_end=True)])
    collector = build_collector(api)

    comments = run(collector._collect_all_comments(BVID, max_count=5))

    assert len(comments) == 6
    assert CommentCollector.get_progress(BVID)["limit"] == 5


def test_collect_all_comments_returns_empty_when_both_sources_fail():
    """热门与普通都拿不到数据时返回空列表。"""
    api = FakeStrategyAPI(reply_error=BilibiliAPIError("全站风控"))
    collector = build_collector(api)

    assert run(collector._collect_all_comments(BVID, max_count=None)) == []


def test_collect_all_comments_falls_back_to_hot_when_normal_fails():
    """普通翻页失败时仍返回热门评论，不全盘丢失。"""
    api = FakeStrategyAPI(hot=make_hot([1, 2]), normal_error=BilibiliAPIError("普通接口 429"))
    collector = build_collector(api)

    comments = run(collector._collect_all_comments(BVID, max_count=None))

    assert [item["rpid"] for item in comments] == [1, 2]


def test_collect_all_comments_returns_normal_when_hot_fails():
    """热门失败时仍返回普通评论。"""
    api = FakeStrategyAPI(hot={"code": -412}, pages=[make_page([7, 8], is_end=True)])
    collector = build_collector(api)

    comments = run(collector._collect_all_comments(BVID, max_count=None))

    assert [item["rpid"] for item in comments] == [7, 8]
