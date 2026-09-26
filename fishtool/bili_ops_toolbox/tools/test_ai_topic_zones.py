"""AI 选题分区拉取与 Cookie 池回归测试。"""
import asyncio
from datetime import datetime

import pytest

from bilibili.cookie_pool import CookiePool
from core.exceptions import BilibiliAPIError
from modules.hotspot.tag_cloud import TagCloudGenerator


class NoopLimiter:
    """测试用无限频器。"""

    async def acquire(self, endpoint: str = "unknown") -> None:
        """立即放行请求。"""


class FakeAPI:
    """按接口返回预设数据的测试 API。"""

    def __init__(self, ranking_error: Exception | None = None) -> None:
        """初始化模拟响应和调用记录。"""
        self.rate_limiter = None
        self.ranking_error = ranking_error
        self.calls = []

    async def get(self, url: str, params=None, need_sign: bool = False):
        """模拟 B 站公开分区接口。"""
        self.calls.append((url, params))
        if url.endswith("ranking/v2"):
            if self.ranking_error:
                raise self.ranking_error
            return {"list": [{"bvid": f"BV{params['rid']}"}]}
        return {"archives": [{"bvid": f"BV{params['rid']}"}]}


@pytest.mark.parametrize("zone_name,zone_id", TagCloudGenerator.ZONE_MAP.items())
def test_all_zones_select_supported_public_endpoint(zone_name: str, zone_id: int) -> None:
    """全部 17 个分区均应通过受支持的公开接口返回视频。"""
    api = FakeAPI()
    generator = TagCloudGenerator(api, NoopLimiter())

    videos = asyncio.run(generator.get_zone_ranking(zone_id, limit=3))

    assert videos and videos[0]["bvid"] == f"BV{zone_id}"
    first_url = api.calls[0][0]
    if zone_id in {13, 167}:
        assert first_url.endswith("newlist")
    else:
        assert first_url.endswith("ranking/v2")


def test_normal_zone_falls_back_to_newlist() -> None:
    """普通分区 ranking/v2 被风控时应降级 newlist。"""
    api = FakeAPI(BilibiliAPIError("请求被风控"))
    generator = TagCloudGenerator(api, NoopLimiter())

    videos = asyncio.run(generator.get_zone_ranking(188, limit=3))

    assert videos == [{"bvid": "BV188"}]
    assert [call[0].rsplit("/", 1)[-1] for call in api.calls] == ["v2", "newlist"]


class FakeCipher:
    """测试用解密器。"""

    def decrypt(self, value: bytes) -> bytes:
        """返回固定明文 Cookie。"""
        return b"SESSDATA=test; bili_jct=test"


class FakeQuery:
    """仅支持全量查询，用于防止重新引入有效状态过滤。"""

    def __init__(self, rows) -> None:
        """保存模拟数据库行。"""
        self.rows = rows

    def all(self):
        """返回全部 Cookie 行。"""
        return self.rows


class FakeSession:
    """Cookie 加载测试数据库会话。"""

    def __init__(self, rows) -> None:
        """保存模拟数据库行。"""
        self.rows = rows

    def query(self, model):
        """返回不带 filter 方法的查询对象。"""
        return FakeQuery(self.rows)


def test_invalid_cookie_is_loaded_for_recovery_check() -> None:
    """失效 Cookie 也必须载入内存，以便后台巡检恢复。"""
    row = type("CookieRow", (), {
        "id": 7,
        "account_id": 1,
        "cookie_data": "encrypted",
        "sessdata": "test",
        "bili_jct": "test",
        "buvid3": "",
        "is_valid": False,
        "fail_count": 2,
        "last_used": datetime(2026, 1, 1),
    })()
    pool = object.__new__(CookiePool)
    pool.cookies = []
    pool._cipher = FakeCipher()

    pool.load_from_db(FakeSession([row]))

    assert len(pool.cookies) == 1
    assert pool.cookies[0].is_valid is False
    assert pool.cookies[0].fail_count == 2
