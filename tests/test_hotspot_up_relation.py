"""UP 主关联数据服务回归测试。

覆盖本次新增的 P2 账号关联抽屉数据源：
- 正常路径字段汇总与 growth_ratio 计算
- 各接口失败时的降级返回（不抛异常、不阻塞渲染）
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from modules.hotspot.up_relation import get_up_relation


def make_api():
    """构造 UP 关联查询所需的 API 替身，全部接口可用。"""
    now_ts = int(time.time())
    api = MagicMock()
    api.get_user_info = AsyncMock(return_value={"data": {"name": "测试UP"}})
    api.get_user_relation_stat = AsyncMock(return_value={
        "data": {"follower": 12345, "following": 67}
    })
    api.get_user_upstat = AsyncMock(return_value={
        "data": {"archive": {"view": 999999}}
    })
    api.get_user_videos = AsyncMock(return_value={
        "data": {
            "list": {
                "vlist": [
                    {"created": now_ts - 86400, "play": 8000},
                    {"created": now_ts - 200 * 86400, "play": 2000},
                ]
            }
        }
    })
    return api


def test_get_up_relation_returns_full_fields():
    """正常路径应返回完整字段，且 growth_ratio 按近 90 日平均播放计算。"""
    api = make_api()

    result = asyncio.run(get_up_relation(api, mid=1001))

    assert result["mid"] == 1001
    assert result["name"] == "测试UP"
    assert result["follower"] == 12345
    assert result["following"] == 67
    assert result["archive_view"] == 999999
    assert result["recent_count"] == 1
    # 近 90 日平均 8000，全部平均 5000 -> growth_ratio 1.6
    assert result["recent_avg_view"] == 8000
    assert result["growth_ratio"] == 1.6


def test_get_up_relation_degrades_on_failures():
    """接口失败应降级返回部分字段，而不是抛异常。"""
    api = make_api()
    api.get_user_relation_stat = AsyncMock(side_effect=RuntimeError("网络错误"))
    api.get_user_videos = AsyncMock(side_effect=RuntimeError("接口404"))

    result = asyncio.run(get_up_relation(api, mid=1001))

    assert result["mid"] == 1001
    assert result["name"] == "测试UP"
    assert result["follower"] is None
    assert result["archive_view"] == 999999
    assert result["recent_count"] is None
    assert result["growth_ratio"] is None


def test_get_up_relation_handles_no_videos():
    """投稿列表为空时增长率应返回 None，避免除零。"""
    api = make_api()
    api.get_user_videos = AsyncMock(return_value={"data": {"list": {"vlist": []}}})

    result = asyncio.run(get_up_relation(api, mid=1001))

    assert result["recent_count"] == 0
    assert result["recent_avg_view"] is None
    assert result["growth_ratio"] is None
