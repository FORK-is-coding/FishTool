"""评论采集接口辅助 Mixin 契约测试（modules/comment/collector/source_mixin.py）。

覆盖：
- _get_video_oid：bvid -> aid 转换、异常响应与 API 异常降级
- _get_user_videos：空间投稿列表拉取、ps 上限、结构缺失与 API 异常降级

测试策略：
- 使用真实 CommentCollector，只有 B站客户端与限频器是契约级假对象；
- 只捕获 BilibiliAPIError 是当前契约，其它异常必须原样冒泡（从外部断言）。
"""
import asyncio

import pytest

from core.exceptions import BilibiliAPIError
from modules.comment.collector import CommentCollector


def run(coro):
    """同步测试内执行单次协程，并加 5 秒超时双保险。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


class FakeRateLimiter:
    """契约级限频器：记录调用参数。"""

    def __init__(self):
        self.calls = []

    async def acquire(self, *args, **kwargs):
        """记录一次令牌获取。"""
        self.calls.append({"args": args, "kwargs": kwargs})


class FakeAPI:
    """契约级假 B站客户端：返回固定响应或抛出指定异常。"""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def get(self, url, params=None, need_sign=False):
        """记录请求并按配置返回/抛错。"""
        self.calls.append({"url": url, "params": dict(params or {}), "need_sign": need_sign})
        if self.error is not None:
            raise self.error
        return self.response


def build_collector(api) -> CommentCollector:
    """装配真实采集器并替换限频器。"""
    collector = CommentCollector(api=api)
    collector.rate_limiter = FakeRateLimiter()
    return collector


# ---------------------------------------------------------------------------
# _get_video_oid
# ---------------------------------------------------------------------------


def test_get_video_oid_returns_aid_with_unsigned_detail_request():
    """视频详情请求无需签名，从 data.aid 取 oid。"""
    api = FakeAPI(response={"aid": 98765, "bvid": "BV1x"})
    collector = build_collector(api)

    oid = run(collector._get_video_oid("BV1x"))

    assert oid == 98765
    call = api.calls[0]
    assert "x/web-interface/view" in call["url"]
    assert call["params"] == {"bvid": "BV1x"}
    assert call["need_sign"] is False


def test_get_video_oid_returns_none_when_aid_missing():
    """响应缺少 aid 字段时返回 None，交给上层短路。"""
    collector = build_collector(FakeAPI(response={"code": 0}))

    assert run(collector._get_video_oid("BV1x")) is None


def test_get_video_oid_returns_none_on_empty_response():
    """空响应（None/{}）都返回 None。"""
    assert run(build_collector(FakeAPI(response=None))._get_video_oid("BV1x")) is None
    assert run(build_collector(FakeAPI(response={}))._get_video_oid("BV1x")) is None


def test_get_video_oid_swallows_bilibili_api_error():
    """B站 API 异常降级为 None，不冒泡。"""
    collector = build_collector(FakeAPI(error=BilibiliAPIError("接口 412")))

    assert run(collector._get_video_oid("BV1x")) is None


def test_get_video_oid_propagates_unexpected_error():
    """非 BilibiliAPIError（如网络层异常）必须原样冒泡，不能被静默吞掉。"""
    collector = build_collector(FakeAPI(error=RuntimeError("连接被重置")))

    with pytest.raises(RuntimeError, match="连接被重置"):
        run(collector._get_video_oid("BV1x"))


# ---------------------------------------------------------------------------
# _get_user_videos
# ---------------------------------------------------------------------------


def make_vlist(count: int = 3) -> list:
    """构造空间投稿列表原始数据。"""
    return [
        {"bvid": f"BV{index}", "title": f"视频{index}", "aid": 100 + index}
        for index in range(count)
    ]


def test_get_user_videos_maps_fields_and_signs_request():
    """空间接口需要签名，并映射出 bvid/title/aid 三字段。"""
    api = FakeAPI(response={"list": {"vlist": make_vlist(3)}})
    collector = build_collector(api)

    videos = run(collector._get_user_videos("12345", limit=10))

    assert videos == [
        {"bvid": "BV0", "title": "视频0", "aid": 100},
        {"bvid": "BV1", "title": "视频1", "aid": 101},
        {"bvid": "BV2", "title": "视频2", "aid": 102},
    ]
    call = api.calls[0]
    assert "arc/search" in call["url"]
    assert call["params"] == {"mid": "12345", "pn": 1, "ps": 10}
    assert call["need_sign"] is True
    # 空间接口走默认限频入口，不传 endpoint。
    assert collector.rate_limiter.calls == [{"args": (), "kwargs": {}}]


def test_get_user_videos_caps_page_size_at_thirty():
    """ps 上限为 30，limit 再大也只请求 30 条。"""
    api = FakeAPI(response={"list": {"vlist": make_vlist(1)}})
    collector = build_collector(api)

    run(collector._get_user_videos("12345", limit=100))

    assert api.calls[0]["params"]["ps"] == 30


def test_get_user_videos_truncates_to_limit():
    """返回条数按 limit 截断，不超出调用方预算。"""
    api = FakeAPI(response={"list": {"vlist": make_vlist(5)}})
    collector = build_collector(api)

    videos = run(collector._get_user_videos("12345", limit=2))

    assert [item["bvid"] for item in videos] == ["BV0", "BV1"]


def test_get_user_videos_returns_empty_for_zero_limit():
    """limit=0 时既不返回数据也会退化为 ps=0。"""
    api = FakeAPI(response={"list": {"vlist": make_vlist(3)}})
    collector = build_collector(api)

    videos = run(collector._get_user_videos("12345", limit=0))

    assert videos == []
    assert api.calls[0]["params"]["ps"] == 0


def test_get_user_videos_returns_empty_when_structure_missing():
    """缺少 list / vlist 或空响应时返回空列表。"""
    assert run(build_collector(FakeAPI(response={"code": 0}))._get_user_videos("1", 10)) == []
    assert run(build_collector(FakeAPI(response={"list": {}}))._get_user_videos("1", 10)) == []
    assert run(build_collector(FakeAPI(response={"list": {"vlist": []}}))._get_user_videos("1", 10)) == []
    assert run(build_collector(FakeAPI(response=None))._get_user_videos("1", 10)) == []


def test_get_user_videos_swallows_bilibili_api_error():
    """B站 API 异常降级为空列表。"""
    collector = build_collector(FakeAPI(error=BilibiliAPIError("空间接口风控")))

    assert run(collector._get_user_videos("12345", 10)) == []


def test_get_user_videos_propagates_unexpected_error():
    """非 API 异常必须冒泡，便于上层记录与告警。"""
    collector = build_collector(FakeAPI(error=ValueError("响应解析失败")))

    with pytest.raises(ValueError, match="响应解析失败"):
        run(collector._get_user_videos("12345", 10))


def test_get_user_videos_keeps_missing_fields_as_none():
    """投稿条目缺字段时保留 None，不做臆造填充。"""
    api = FakeAPI(response={"list": {"vlist": [{"bvid": "BV1"}]}})
    collector = build_collector(api)

    videos = run(collector._get_user_videos("12345", 10))

    assert videos == [{"bvid": "BV1", "title": None, "aid": None}]
