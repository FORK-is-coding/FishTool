"""热点词云采集与统计核心测试。"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.exceptions import BilibiliAPIError, ValidationError
from modules.hotspot.tag_cloud import TagCloudGenerator


class Limiter:
    """不等待的限频器替身。"""

    def __init__(self):
        self.calls = 0

    async def acquire(self, *args, **kwargs):
        """记录令牌请求次数。"""
        self.calls += 1


def build_generator(api_get=None):
    """构造带异步 API 替身的词云生成器。"""
    limiter = Limiter()
    api = SimpleNamespace(rate_limiter=limiter, get=api_get or AsyncMock())
    return TagCloudGenerator(api, limiter), api, limiter


def test_get_zone_ranking_uses_ranking_result_and_limit():
    """普通分区应优先采用 ranking/v2 并按 limit 截断。"""
    get = AsyncMock(return_value={"list": [{"bvid": "BV1"}, {"bvid": "BV2"}]})
    generator, api, limiter = build_generator(get)

    result = asyncio.run(generator.get_zone_ranking(4, limit=1))

    assert result == [{"bvid": "BV1"}]
    assert api.get.await_args.kwargs["params"] == {"rid": 4, "type": "all"}
    assert limiter.calls == 1


def test_get_zone_ranking_falls_back_to_newlist():
    """榜单接口异常后应降级到 newlist。"""
    get = AsyncMock(side_effect=[BilibiliAPIError("blocked"), {"archives": [{"bvid": "BV2"}]}])
    generator, _api, limiter = build_generator(get)

    result = asyncio.run(generator.get_zone_ranking(4, limit=5))

    assert result == [{"bvid": "BV2"}]
    assert limiter.calls == 2


def test_special_zone_uses_newlist_and_combined_failure_is_clear():
    """番剧分区应直接走 newlist，空结果应包装成业务异常。"""
    generator, api, _limiter = build_generator(AsyncMock(return_value={"archives": [{"bvid": "BV13"}]}))
    assert asyncio.run(generator.get_zone_ranking(13, 10))[0]["bvid"] == "BV13"
    assert "newlist" in api.get.await_args.args[0]

    failed, _api, _limiter = build_generator(AsyncMock(side_effect=BilibiliAPIError("down")))
    with pytest.raises(BilibiliAPIError, match="newlist 接口失败"):
        asyncio.run(failed.get_zone_ranking(167, 10))


def test_extract_tags_continues_after_single_video_failure():
    """单视频标签失败不能中断批量采集，且每项都应汇报进度。"""
    get = AsyncMock(
        side_effect=[
            [{"tag_name": "Python"}, {"tag_name": "教程"}, "bad"],
            BilibiliAPIError("missing"),
        ]
    )
    generator, _api, _limiter = build_generator(get)
    progress = []
    videos = [
        {"bvid": "BV1", "tname": "知识"},
        {"bvid": "BV2", "tname": "科技"},
        {"title": "no bvid"},
    ]

    tags = asyncio.run(generator.extract_tags_from_videos(videos, lambda done, total: progress.append((done, total))))

    assert tags == ["Python", "教程", "知识"]
    assert progress == [(1, 3), (2, 3)]


def test_generate_word_frequency_filters_noise_and_orders_top_n():
    """词频统计应过滤单字与无意义标签并返回高频项。"""
    generator, _api, _limiter = build_generator()
    result = generator.generate_word_frequency(["游戏", "游戏", "教程", "视频", "A", "原创"], top_n=2)
    assert result == {"游戏": 2, "教程": 1}


def test_generate_cloud_data_orchestrates_pipeline(monkeypatch):
    """词云主流程应串联榜单、标签、统计、保存和进度回调。"""
    now = int(time.time())
    generator, _api, _limiter = build_generator()
    monkeypatch.setattr(generator, "get_zone_ranking", AsyncMock(return_value=[
        {"bvid": "BV1", "pubdate": now},
        {"bvid": "BV2", "pubdate": now},
    ]))

    async def extract(_videos, callback):
        callback(1, 2)
        callback(2, 2)
        return ["游戏", "游戏", "攻略"]

    monkeypatch.setattr(generator, "extract_tags_from_videos", extract)
    save = AsyncMock()
    monkeypatch.setattr(generator, "_save_to_database", save)
    progress = []

    result = asyncio.run(generator.generate_cloud_data("游戏", limit=2, top_n=2, progress_callback=lambda *item: progress.append(item)))

    assert result["zone_id"] == TagCloudGenerator.ZONE_MAP["游戏"]
    assert result["video_count"] == 2
    assert result["word_frequency"] == {"游戏": 2, "攻略": 1}
    save.assert_awaited_once_with(
        "游戏", TagCloudGenerator.ZONE_MAP["游戏"], {"游戏": 2, "攻略": 1}
    )
    assert ("collecting_tags", 85, "正在采集视频标签 2/2") in progress


def test_generate_cloud_data_validates_zone_and_empty_ranking(monkeypatch):
    """未知分区和空榜单必须以明确异常结束。"""
    generator, _api, _limiter = build_generator()
    with pytest.raises(ValidationError):
        asyncio.run(generator.generate_cloud_data("不存在"))

    monkeypatch.setattr(generator, "get_zone_ranking", AsyncMock(return_value=[]))
    with pytest.raises(BilibiliAPIError, match="空数据"):
        asyncio.run(generator.generate_cloud_data("游戏"))
