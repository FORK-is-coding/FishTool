"""评论采集器接口辅助 Mixin 的契约级测试。

覆盖 modules/comment/collector/source_mixin.py：
- _get_video_oid：bvid -> aid 的字段提取与异常降级
- _get_user_videos：空间投稿列表解析、ps 上限夹取、limit 截断、异常降级

网络层使用真实实现 __init__ 的契约级假对象（非 AsyncMock），限频器为不阻塞的记录替身。
"""

from __future__ import annotations

import asyncio

import pytest

from core.exceptions import BilibiliAPIError
from modules.comment.collector.source_mixin import CommentSourceMixin


class FakeLimiter:
    """记录调用参数的假限频器。"""

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


class Host(CommentSourceMixin):
    """最小宿主：把假 api 与假限频器挂到实例上。"""

    def __init__(self, api: FakeAPI, limiter: FakeLimiter | None = None) -> None:
        self.api = api
        self.rate_limiter = limiter or FakeLimiter()


def _host(*responses) -> tuple[Host, FakeAPI, FakeLimiter]:
    """构造宿主并返回 (宿主, api, 限频器)。"""
    api = FakeAPI(responses=list(responses))
    limiter = FakeLimiter()
    return Host(api, limiter), api, limiter


# --------------------------------------------------------------------- _get_video_oid

def test_get_video_oid_returns_aid() -> None:
    """返回体含 aid 时应直接取出。"""
    host, api, _ = _host({"aid": 123})

    assert asyncio.run(host._get_video_oid("BV1")) == 123
    assert api.calls[0]["params"] == {"bvid": "BV1"}
    assert api.calls[0]["need_sign"] is False


@pytest.mark.parametrize("payload", [None, {}, {"view": 1}])
def test_get_video_oid_returns_none_without_aid(payload) -> None:
    """缺少 aid 字段时返回 None。"""
    host, _, _ = _host(payload)

    assert asyncio.run(host._get_video_oid("BV1")) is None


def test_get_video_oid_swallows_api_error() -> None:
    """APIError 应被吞掉并返回 None。"""
    host, _, _ = _host(BilibiliAPIError("boom"))

    assert asyncio.run(host._get_video_oid("BV1")) is None


def test_get_video_oid_does_not_consume_rate_limiter() -> None:
    """视频详情接口不走评论专用限频桶。"""
    host, _, limiter = _host({"aid": 1})

    asyncio.run(host._get_video_oid("BV1"))

    assert limiter.calls == []


# --------------------------------------------------------------------- _get_user_videos

def test_get_user_videos_parses_vlist() -> None:
    """投稿列表应提取 bvid/title/aid 三个字段。"""
    payload = {"list": {"vlist": [
        {"bvid": "BV1", "title": "标题1", "aid": 11},
        {"bvid": "BV2", "title": "标题2", "aid": 22},
    ]}}
    host, api, limiter = _host(payload)

    videos = asyncio.run(host._get_user_videos("42", limit=10))

    assert videos == [
        {"bvid": "BV1", "title": "标题1", "aid": 11},
        {"bvid": "BV2", "title": "标题2", "aid": 22},
    ]
    assert api.calls[0]["params"] == {"mid": "42", "pn": 1, "ps": 10}
    assert api.calls[0]["need_sign"] is True
    assert limiter.calls == [((), {})]


@pytest.mark.parametrize(("limit", "expected_ps"), [(10, 10), (30, 30), (50, 30), (1, 1)])
def test_get_user_videos_caps_ps_at_30(limit, expected_ps) -> None:
    """ps 参数应夹取到不超过 30。"""
    host, api, _ = _host({"list": {"vlist": []}})

    asyncio.run(host._get_user_videos("42", limit=limit))

    assert api.calls[0]["params"]["ps"] == expected_ps


def test_get_user_videos_truncates_to_limit() -> None:
    """返回视频数不应超过 limit。"""
    vlist = [{"bvid": f"BV{index}", "title": "t", "aid": index} for index in range(5)]
    host, _, _ = _host({"list": {"vlist": vlist}})

    videos = asyncio.run(host._get_user_videos("42", limit=2))

    assert [item["bvid"] for item in videos] == ["BV0", "BV1"]


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"list": {}}, {"list": {"vlist": []}}, {"list": {"vlist": None}}],
)
def test_get_user_videos_returns_empty_on_bad_payload(payload) -> None:
    """结构缺失或 vlist 为空时返回空列表。"""
    host, _, _ = _host(payload)

    assert asyncio.run(host._get_user_videos("42", limit=10)) == []


def test_get_user_videos_swallows_api_error() -> None:
    """APIError 应被吞掉并返回空列表。"""
    host, _, _ = _host(BilibiliAPIError("boom"))

    assert asyncio.run(host._get_user_videos("42", limit=10)) == []


def test_get_user_videos_missing_item_fields_default_to_none() -> None:
    """列表项字段缺失时应写 None，而不是抛异常。"""
    host, _, _ = _host({"list": {"vlist": [{}]}})

    videos = asyncio.run(host._get_user_videos("42", limit=10))

    assert videos == [{"bvid": None, "title": None, "aid": None}]
