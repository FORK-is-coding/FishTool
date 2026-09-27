"""本轮 B 站接口、一级分区与绘画方案 C 回归测试。"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bilibili.api.user import UserAPIMixin
from modules.hotspot.tag_cloud import TagCloudGenerator


class UserAPIStub(UserAPIMixin):
    """只提供 get 方法的用户接口测试替身。"""

    def __init__(self, responses):
        self.get = AsyncMock(side_effect=responses)


class Limiter:
    """不等待的限频器替身。"""

    def __init__(self):
        self.calls = 0

    async def acquire(self, *_args, **_kwargs):
        """记录一次请求预算消耗。"""
        self.calls += 1


def test_room_base_info_uses_old_single_mid_endpoint_and_normalizes_fields():
    """直播间查询应逐 UID 调旧接口并保持调用方依赖字段。"""
    api = UserAPIStub([
        {"roomid": 101, "liveStatus": 1, "title": "直播A"},
        RuntimeError("single uid failed"),
        {"roomid": 303, "liveStatus": 0},
    ])

    result = asyncio.run(api.get_room_base_info([11, 22, 33, 11]))

    assert result["data"]["11"]["room_id"] == 101
    assert result["data"]["11"]["live_status"] == 1
    assert result["data"]["33"]["room_id"] == 303
    assert "22" not in result["data"]
    assert api.get.await_count == 3
    for call, uid in zip(api.get.await_args_list, (11, 22, 33)):
        assert call.args[0].endswith("/room/v1/Room/getRoomInfoOld")
        assert call.kwargs["params"] == {"mid": uid}
        assert call.kwargs["need_sign"] is False


def test_primary_zone_contract_has_exactly_28_real_zones_without_v_circle():
    """采集下拉数据应仅包含全站 28 个一级分区（新 pid_v2 体系）。"""
    options = TagCloudGenerator.get_zone_options()

    assert len(options) == 28
    assert len({item["tid"] for item in options}) == 28
    assert {"动画", "音乐", "游戏", "影视", "AI", "生活"} <= {item["name"] for item in options}
    assert "V圈" not in {item["name"] for item in options}
    assert TagCloudGenerator.is_collectable_zone("绘画") is True
    assert "绘画" not in TagCloudGenerator.get_supported_zones()


def test_paint_scheme_c_search_constraints_filter_and_account_supplement():
    """绘画方案 C 应约束 TID/7天/播放排序，并合并重点账号投稿补漏。"""
    limiter = Limiter()
    now = int(time.time())
    old = now - 8 * 24 * 60 * 60
    api = SimpleNamespace(
        rate_limiter=limiter,
        get=AsyncMock(return_value={
            "result": [
                {"bvid": "BVA", "title": "板绘过程", "description": "上色", "created": now, "play": 200000},
                {"bvid": "NOISE", "title": "普通日常", "description": "无关", "created": now, "play": 999999},
            ]
        }),
        get_user_videos=AsyncMock(side_effect=[
            {"data": {"list": {"vlist": [
                {"bvid": "BVA", "title": "绘画重复", "created": now, "play": 200000},
                {"bvid": "BVB", "title": "插画教程", "created": now, "play": 100000},
                {"bvid": "OLD", "title": "手绘旧稿", "created": old, "play": 900000},
            ]}}},
            {"data": {"list": {"vlist": [
                {"bvid": "OLD", "title": "手绘旧稿", "created": old},
            ]}}},
        ]),
    )
    generator = TagCloudGenerator(api, limiter)

    result = asyncio.run(generator.get_paint_videos(limit=10))

    assert [item["bvid"] for item in result] == ["BVA", "BVB"]
    params = api.get.await_args.kwargs["params"]
    assert params["tids"] == 27
    assert params["order"] == "click"
    assert params["pubtime_begin"] <= params["pubtime_end"]
    assert api.get_user_videos.await_count == 2


def test_paint_scheme_c_min_view_filters_low_play_videos():
    """min_view 应过滤掉播放量低于下限的搜索主采结果。"""
    limiter = Limiter()
    now = int(time.time())
    api = SimpleNamespace(
        rate_limiter=limiter,
        get=AsyncMock(return_value={
            "result": [
                {"bvid": "BVA", "title": "板绘过程", "description": "上色", "created": now, "play": 120000},
                {"bvid": "LOW", "title": "板绘练习", "description": "素描", "created": now, "play": 30000},
                {"bvid": "NOISE", "title": "普通日常", "description": "无关", "created": now, "play": 999999},
            ]
        }),
        get_user_videos=AsyncMock(return_value={"data": {"list": {"vlist": []}}}),
    )
    generator = TagCloudGenerator(api, limiter)

    result = asyncio.run(generator.get_paint_videos(limit=10, min_view=50000))

    bvids = [item["bvid"] for item in result]
    assert "BVA" in bvids
    assert "LOW" not in bvids
    assert "NOISE" not in bvids


def test_zones_route_exposes_paint_for_frontend_without_breaking_primary_contract():
    """前端下拉应看到绘画区，但一级分区契约保持 28 个不含绘画。"""
    from web.routers.hotspot.routes_zones import get_supported_zones

    payload = asyncio.run(get_supported_zones())

    assert "绘画" in payload["zones"]
    assert payload["count"] == 29
    paint_opt = next(item for item in payload["zone_options"] if item["name"] == "绘画")
    assert paint_opt["tid"] == 27
    assert paint_opt["scheme"] == "paint_c"
    # 一级分区契约仍为 28 个、不含绘画，防止污染榜单走法。
    assert len(TagCloudGenerator.get_zone_options()) == 28
    assert "绘画" not in TagCloudGenerator.get_supported_zones()


def test_collect_routes_paint_tid_to_scheme_c(monkeypatch):
    """采集接口应把绘画 TID(27) 分流到方案 C，而不是榜单分页。"""
    from modules.hotspot.collector import HotspotCollector

    api = SimpleNamespace(rate_limiter=None)
    collector = HotspotCollector(api=api)
    paint_mock = AsyncMock(return_value=[{"bvid": "BVPAINT"}])
    ranking_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(TagCloudGenerator, "get_paint_videos", paint_mock)
    collector._fetch_ranking_paged = ranking_mock
    collector._fetch_view = AsyncMock(return_value={"bvid": "BVPAINT", "stat": {}, "tid": 27})
    collector._save_snapshot = AsyncMock(return_value=1)
    collector._existing_bvids_today = lambda: set()

    result = asyncio.run(collector.collect(tid=27, limit=10, sample_comments=False, sample_danmaku=False))

    assert paint_mock.await_count == 1
    assert ranking_mock.await_count == 0
    assert result["ok"] == 1
    collector._save_snapshot.assert_awaited_once()
    # 落库来源标记为方案 C，便于区分数据血缘。
    assert collector._save_snapshot.await_args.kwargs["source"] == "paint_c"
