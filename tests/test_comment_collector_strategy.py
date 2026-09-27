"""评论采集器分级策略 Mixin 的契约级测试。

覆盖 modules/comment/collector/strategy_mixin.py：
- _collect_hot_comments：热门评论解析、oid 缺失短路、结构异常与 APIError 降级
- _collect_normal_comments：游标翻页、重复页去重停止、limit 截断、异常降级、进度回写
- _collect_all_comments：热门+普通合并去重、max_count 语义、进度回写

使用真实 CommentCollector 实例（真解析/真去重/真进度），
只把 api 与限频器换成契约级假对象；oid 解析以实例方法注入固定值。
"""

from __future__ import annotations

import asyncio

import pytest

from core.exceptions import BilibiliAPIError
from modules.comment.collector import CommentCollector


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


def _reply(rpid: int) -> dict:
    """构造一条最小可解析的原始评论。"""
    return {
        "rpid": rpid,
        "member": {"mid": rpid, "uname": f"u{rpid}"},
        "content": {"message": f"内容{rpid}"},
        "ctime": 1700000000,
    }


@pytest.fixture(autouse=True)
def _clean_progress():
    """每个用例前后清空采集进度，避免跨用例串扰。"""
    CommentCollector.clear_progress("BV1")
    yield
    CommentCollector.clear_progress("BV1")


def _collector(responses, oid: int | None = 999) -> tuple[CommentCollector, FakeAPI, FakeLimiter]:
    """构造注入假 api/限频器、且 oid 固定的采集器。"""
    api = FakeAPI(responses=list(responses))
    limiter = FakeLimiter()
    collector = CommentCollector(api=api, rate_limiter=limiter)

    async def _const_oid(bvid):
        """固定返回预置 oid，隔离视频详情接口。"""
        return oid

    collector._get_video_oid = _const_oid
    return collector, api, limiter


# --------------------------------------------------------------------- 热门评论

def test_collect_hot_comments_parses_and_uses_mode3() -> None:
    """热门评论应走 mode=3 且解析为标准化列表。"""
    collector, api, limiter = _collector([{"replies": [_reply(1), _reply(2)]}])

    comments = asyncio.run(collector._collect_hot_comments("BV1"))

    assert [item["rpid"] for item in comments] == [1, 2]
    assert all(item["is_hot"] is True for item in comments)
    assert api.calls[0]["params"] == {"oid": 999, "type": 1, "mode": 3, "ps": 20}
    assert api.calls[0]["need_sign"] is False
    assert limiter.calls[0] == ((), {"endpoint": "comment"})


def test_collect_hot_comments_short_circuits_without_oid() -> None:
    """拿不到 oid 时应直接返回空列表且不发请求。"""
    collector, api, _ = _collector([], oid=None)

    assert asyncio.run(collector._collect_hot_comments("BV1")) == []
    assert api.calls == []


@pytest.mark.parametrize("payload", [None, {}, {"other": 1}])
def test_collect_hot_comments_returns_empty_on_bad_payload(payload) -> None:
    """缺少 replies 字段时返回空列表。"""
    collector, _, _ = _collector([payload])

    assert asyncio.run(collector._collect_hot_comments("BV1")) == []


def test_collect_hot_comments_swallows_api_error() -> None:
    """APIError 应被吞掉并返回空列表。"""
    collector, _, _ = _collector([BilibiliAPIError("boom")])

    assert asyncio.run(collector._collect_hot_comments("BV1")) == []


def test_collect_hot_comments_empty_replies() -> None:
    """replies 为空列表时返回空列表。"""
    collector, _, _ = _collector([{"replies": []}])

    assert asyncio.run(collector._collect_hot_comments("BV1")) == []


# --------------------------------------------------------------------- 普通评论

def test_collect_normal_comments_single_page() -> None:
    """单页结束时直接返回解析结果并回写进度。"""
    collector, api, limiter = _collector([
        {"replies": [_reply(1), _reply(2)], "cursor": {"is_end": True}},
    ])

    comments = asyncio.run(collector._collect_normal_comments("BV1", limit=100))

    assert [item["rpid"] for item in comments] == [1, 2]
    assert api.calls[0]["params"]["mode"] == 2
    assert limiter.calls[0] == ((), {"endpoint": "comment"})
    progress = CommentCollector.get_progress("BV1")
    assert progress["phase"] == "normal"
    assert progress["collected"] == 2


def test_collect_normal_comments_follows_cursor_pagination() -> None:
    """应把 next_offset 包装进 pagination_str 并翻到下一页。"""
    collector, api, _ = _collector([
        {"replies": [_reply(1)], "cursor": {"pagination_reply": {"next_offset": "abc"}}},
        {"replies": [_reply(2)], "cursor": {"is_end": True}},
    ])

    comments = asyncio.run(collector._collect_normal_comments("BV1", limit=100))

    assert [item["rpid"] for item in comments] == [1, 2]
    assert "pagination_str" not in api.calls[0]["params"]
    assert api.calls[1]["params"]["pagination_str"] == '{"offset":"abc"}'


def test_collect_normal_comments_uses_cursor_next_alias() -> None:
    """缺少 pagination_reply 时应回退 cursor.next。"""
    collector, api, _ = _collector([
        {"replies": [_reply(1)], "cursor": {"next": "7"}},
        {"replies": [_reply(2)], "cursor": {"is_end": True}},
    ])

    asyncio.run(collector._collect_normal_comments("BV1", limit=100))

    assert api.calls[1]["params"]["pagination_str"] == '{"offset":"7"}'


def test_collect_normal_comments_stops_on_duplicate_page() -> None:
    """分页返回重复 rpid 时应停止，避免无限重复采集。"""
    collector, api, _ = _collector([
        {"replies": [_reply(1)], "cursor": {"pagination_reply": {"next_offset": "a"}}},
        {"replies": [_reply(1)], "cursor": {"pagination_reply": {"next_offset": "b"}}},
    ])

    comments = asyncio.run(collector._collect_normal_comments("BV1", limit=100))

    assert [item["rpid"] for item in comments] == [1]
    assert len(api.calls) == 2


def test_collect_normal_comments_stops_on_empty_page() -> None:
    """当前页无评论时应停止翻页。"""
    collector, api, _ = _collector([{"replies": []}])

    assert asyncio.run(collector._collect_normal_comments("BV1", limit=100)) == []
    assert len(api.calls) == 1


def test_collect_normal_comments_stops_when_cursor_missing() -> None:
    """没有下一页游标时应停止。"""
    collector, api, _ = _collector([{"replies": [_reply(1)]}])

    comments = asyncio.run(collector._collect_normal_comments("BV1", limit=100))

    assert [item["rpid"] for item in comments] == [1]
    assert len(api.calls) == 1


def test_collect_normal_comments_truncates_to_limit() -> None:
    """累计条数达到 limit 后应截断返回。"""
    collector, _, _ = _collector([
        {"replies": [_reply(index) for index in range(25)], "cursor": {"is_end": True}},
    ])

    comments = asyncio.run(collector._collect_normal_comments("BV1", limit=20))

    assert len(comments) == 20


def test_collect_normal_comments_short_circuits_without_oid() -> None:
    """oid 缺失时返回空列表且不发请求。"""
    collector, api, _ = _collector([], oid=None)

    assert asyncio.run(collector._collect_normal_comments("BV1", limit=100)) == []
    assert api.calls == []


def test_collect_normal_comments_swallows_api_error() -> None:
    """APIError 应被吞掉并返回空列表。"""
    collector, _, _ = _collector([BilibiliAPIError("boom")])

    assert asyncio.run(collector._collect_normal_comments("BV1", limit=100)) == []


# --------------------------------------------------------------------- 完整采集

def test_collect_all_comments_merges_and_dedups() -> None:
    """热门与普通评论按 rpid 去重合并，保留首次出现。"""
    collector, _, _ = _collector([])

    async def _hot(bvid):
        """返回含重复 rpid 的热门评论。"""
        return [{"rpid": 1, "is_hot": True}, {"rpid": 2, "is_hot": True}]

    calls = {}

    async def _normal(bvid, limit=100):
        """记录 limit 并返回与热门重叠的普通评论。"""
        calls["limit"] = limit
        return [{"rpid": 2}, {"rpid": 3}]

    collector._collect_hot_comments = _hot
    collector._collect_normal_comments = _normal

    comments = asyncio.run(collector._collect_all_comments("BV1", max_count=50))

    assert [item["rpid"] for item in comments] == [1, 2, 3]
    assert comments[0]["is_hot"] is True
    assert calls["limit"] == 50
    assert CommentCollector.get_progress("BV1")["collected"] == 3


def test_collect_all_comments_uses_default_limit_when_max_none() -> None:
    """max_count 为 None 时普通评论按 10000 拉取。"""
    collector, _, _ = _collector([])

    async def _hot(bvid):
        """返回空热门评论。"""
        return []

    calls = {}

    async def _normal(bvid, limit=100):
        """记录传入的 limit。"""
        calls["limit"] = limit
        return []

    collector._collect_hot_comments = _hot
    collector._collect_normal_comments = _normal

    asyncio.run(collector._collect_all_comments("BV1", max_count=None))

    assert calls["limit"] == 10000


def test_collect_all_comments_only_hot() -> None:
    """普通评论为空时只返回热门评论。"""
    collector, _, _ = _collector([])

    async def _hot(bvid):
        """返回两条热门评论。"""
        return [{"rpid": 1}, {"rpid": 2}]

    async def _normal(bvid, limit=100):
        """返回空普通评论。"""
        return []

    collector._collect_hot_comments = _hot
    collector._collect_normal_comments = _normal

    comments = asyncio.run(collector._collect_all_comments("BV1", max_count=10))

    assert [item["rpid"] for item in comments] == [1, 2]
