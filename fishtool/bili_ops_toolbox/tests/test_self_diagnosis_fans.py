"""账号自诊粉丝数回退与不可用状态回归测试。"""

import asyncio
from typing import Any

from modules.self_diagnosis.report_generator import ReportGenerator
from modules.self_diagnosis.self_analyzer import SelfAnalyzer


class FakeRateLimiter:
    """记录限频调用，避免单元测试访问真实限频器。"""

    def __init__(self) -> None:
        """初始化调用计数。"""
        self.calls = 0

    async def acquire(self, level: str) -> None:
        """模拟获取限频令牌。

        Args:
            level: 限频等级。

        Returns:
            无返回值。
        """
        assert level == "normal"
        self.calls += 1


class FakeBilibiliAPI:
    """按测试场景返回 relation 统计或抛出异常。"""

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        """保存预设响应。

        Args:
            response: relation/stat 的模拟响应。
            error: 调用 relation/stat 时抛出的模拟异常。
        """
        self.response = response
        self.error = error

    async def get_user_relation_stat(self, uid: int) -> dict[str, Any]:
        """返回模拟关系数据。

        Args:
            uid: B站用户 UID。

        Returns:
            预设 relation/stat 响应。
        """
        assert uid == 42
        if self.error is not None:
            raise self.error
        return self.response or {}


def run_fan_stats(
    response: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> tuple[dict[str, int | None], bool, str | None]:
    """执行单次粉丝统计采集。

    Args:
        response: relation/stat 的模拟响应。
        error: relation/stat 的模拟异常。

    Returns:
        自诊分析器返回的粉丝统计、可用性和错误原因。
    """
    analyzer = SelfAnalyzer(FakeBilibiliAPI(response, error), FakeRateLimiter())
    return asyncio.run(analyzer._fetch_fan_stats(42, {"follower": 16}))


def test_relation_zero_uses_card_follower() -> None:
    """relation 返回零时应使用 card follower，而不是展示伪零值。"""
    stats, available, error = run_fan_stats(
        {"data": {"follower": 0, "following": 3}},
    )

    assert stats == {"follower": 16, "following": 3}
    assert available is True
    assert error is None


def test_relation_exception_is_marked_unavailable() -> None:
    """relation 异常时应标记暂无数据，且不能把异常伪装成零。"""
    stats, available, error = run_fan_stats(error=RuntimeError("relation blocked"))

    assert stats == {"follower": None, "following": None}
    assert available is False
    assert error == "relation blocked"


def test_relation_normal_value_has_priority() -> None:
    """relation 返回正常粉丝数时应优先使用该值。"""
    stats, available, error = run_fan_stats(
        {"data": {"follower": 25, "following": 3}},
    )

    assert stats == {"follower": 25, "following": 3}
    assert available is True
    assert error is None


def test_relation_missing_follower_uses_card_follower() -> None:
    """relation 缺少 follower 字段时应使用 card follower 兜底。"""
    stats, available, error = run_fan_stats({"data": {"following": 3}})

    assert stats == {"follower": 16, "following": 3}
    assert available is True
    assert error is None


def test_card_explicit_zero_is_kept_as_real_zero() -> None:
    """relation 与 card 都明确返回零时应识别为真实零粉丝。"""
    analyzer = SelfAnalyzer(
        FakeBilibiliAPI({"data": {"follower": 0, "following": 0}}),
        FakeRateLimiter(),
    )
    stats, available, error = asyncio.run(
        analyzer._fetch_fan_stats(42, {"follower": 0})
    )

    assert stats == {"follower": 0, "following": 0}
    assert available is True
    assert error is None


def test_relation_exception_does_not_abort_other_dimensions() -> None:
    """relation 异常只能降级粉丝维度，投稿等后续采集必须继续完成。"""

    class FullFakeAPI(FakeBilibiliAPI):
        """补充自诊主流程所需基础资料接口。"""

        BASE_URL = "https://api.bilibili.com"

        async def get_user_info(self, uid: int) -> dict[str, Any]:
            """返回带 card follower 的基础资料。

            Args:
                uid: B站用户 UID。

            Returns:
                标准化基础资料响应。
            """
            assert uid == 42
            return {"data": {"name": "测试账号", "follower": 16}}

    class EmptyThirdPartyFetcher:
        """模拟无可用三方数据的异步上下文管理器。"""

        async def __aenter__(self) -> "EmptyThirdPartyFetcher":
            """进入异步上下文并返回自身。"""
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            """退出异步上下文，无需释放测试资源。"""

        async def fetch_from_zeroroku(self, uid: int) -> None:
            """模拟三方数据不可用。

            Args:
                uid: B站用户 UID。

            Returns:
                固定返回 ``None``。
            """
            assert uid == 42
            return None

    async def fake_fetch_all_videos(uid: int) -> list[dict[str, Any]]:
        """模拟投稿列表为空。

        Args:
            uid: B站用户 UID。

        Returns:
            空投稿列表。
        """
        assert uid == 42
        return []

    analyzer = SelfAnalyzer(
        FullFakeAPI(error=RuntimeError("relation blocked")),
        FakeRateLimiter(),
    )
    analyzer.data_fetcher = EmptyThirdPartyFetcher()
    analyzer._fetch_all_videos = fake_fetch_all_videos

    result = asyncio.run(analyzer.fetch_self_data(42))

    assert result["fan_stats"]["follower"] is None
    assert result["data_availability"]["fan_stats"] is False
    assert result["data_errors"]["relation_stat"] == "relation blocked"
    assert result["data_availability"]["video_stats"] is True
    assert result["video_stats"]["total_count"] == 0
    assert result["data_availability"]["post_rhythm"] is True


def test_report_renders_unavailable_follower_without_format_error() -> None:
    """粉丝数不可用时 Markdown 报告应显示暂无数据。"""
    report = ReportGenerator().generate_markdown_report(
        {
            "uid": 42,
            "basic_info": {},
            "fan_stats": {"follower": None, "following": None},
            "video_stats": {},
            "engagement_metrics": {},
            "post_rhythm": {},
            "data_availability": {},
        }
    )

    assert "**粉丝数**: 暂无数据" in report
    assert "**关注数**: 暂无数据" in report
