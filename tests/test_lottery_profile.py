"""抽奖公开画像采集的契约级测试。

覆盖 modules/lottery/profile.py 的画像聚合、单项失败隔离与动态活跃度汇总。
B 站客户端使用真实 ``BilibiliAPI`` 实例，仅把四个网络方法替换为契约级响应函数。
"""

import asyncio

import pytest

from bilibili.api import BilibiliAPI

from modules.lottery.profile import ProfileCollector


# ------------------------------------------------------------------ 构造辅助

class _ApiStub:
    """契约级方法集合：每个维度可独立返回数据或抛出异常。"""

    def __init__(self, **behaviour):
        """记录各方法的返回值或待抛异常。"""
        self.behaviour = behaviour
        self.calls = []

    def _resolve(self, name: str, *args, **kwargs):
        """按配置返回数据或抛出预置异常，并记录调用。"""
        self.calls.append({"name": name, "args": args, "kwargs": kwargs})
        outcome = self.behaviour.get(name, {})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_user_info(self, uid):
        """契约级用户资料接口。"""
        return self._resolve("get_user_info", uid)

    async def get_user_relation_stat(self, uid):
        """契约级关系统计接口。"""
        return self._resolve("get_user_relation_stat", uid)

    async def get_user_videos(self, uid, page=1, page_size=10):
        """契约级投稿列表接口。"""
        return self._resolve("get_user_videos", uid, page=page, page_size=page_size)

    async def get(self, url, params=None, **kwargs):
        """契约级通用接口，用于动态 feed。"""
        return self._resolve("get", url, params=params)


def _build_collector(**behaviour) -> tuple[ProfileCollector, BilibiliAPI, _ApiStub]:
    """构造真实 BilibiliAPI + 契约级方法注入的采集器。"""
    api = BilibiliAPI()
    stub = _ApiStub(**behaviour)
    # 实例级注入四个维度方法，避免真实网络请求。
    api.get_user_info = stub.get_user_info
    api.get_user_relation_stat = stub.get_user_relation_stat
    api.get_user_videos = stub.get_user_videos
    api.get = stub.get
    return ProfileCollector(api), api, stub


def _dynamic_item(*, pub_ts: int = 0, text: str = "", dtype: str = "DYNAMIC_TYPE_AV") -> dict:
    """构造一条动态 feed item。"""
    return {
        "type": dtype,
        "modules": {
            "module_author": {"pub_ts": pub_ts},
            "module_dynamic": {"desc": {"text": text}},
        },
    }


# ------------------------------------------------- fetch 成功路径

def test_fetch_aggregates_all_public_dimensions() -> None:
    """四个维度均可用时应输出完整的标准画像。"""
    collector, _, stub = _build_collector(
        get_user_info={"data": {"name": "测试用户", "level": 5}},
        get_user_relation_stat={"data": {"follower": 120, "following": 30}},
        get_user_videos={"data": {"page": {"count": 8}}},
        get={"items": [_dynamic_item(pub_ts=1_700_000_000, text="日常")]},
    )

    profile = asyncio.run(asyncio.wait_for(collector.fetch(42), timeout=5))

    assert profile["uid"] == 42
    assert profile["name"] == "测试用户"
    assert profile["level"] == 5
    assert profile["follower"] == 120
    assert profile["following"] == 30
    assert profile["video_count"] == 8
    assert profile["recent_activity_count"] == 1
    assert profile["data_errors"] == []
    assert "最早可观察动态距今天数" in profile["account_age_note"]
    # 投稿维度必须带固定分页参数，保证复用全站限频策略下的一致性。
    video_call = next(call for call in stub.calls if call["name"] == "get_user_videos")
    assert video_call["kwargs"] == {"page": 1, "page_size": 10}
    dynamic_call = next(call for call in stub.calls if call["name"] == "get")
    assert dynamic_call["kwargs"]["params"] == {"host_mid": 42, "offset": ""}
    assert str(dynamic_call["args"][0]).endswith("/x/polymer/web-dynamic/v1/feed/space")


def test_fetch_coerces_string_numbers_and_defaults_missing_fields() -> None:
    """接口偶发返回字符串数字时应安全转换，缺失字段保持未知（None）。"""
    collector, _, _ = _build_collector(
        get_user_info={"data": {"name": "", "level": "6"}},
        get_user_relation_stat={"data": {"follower": "88"}},
        get_user_videos={"data": {}},
        get={},
    )

    profile = asyncio.run(collector.fetch(7))

    assert profile["name"] == "UID 7"
    assert profile["level"] == 6
    assert profile["follower"] == 88
    # 字段缺失（而非为 0）时保持未知，避免把“无数据”误当成“无粉丝/无投稿”。
    assert profile["following"] is None
    assert profile["video_count"] is None
    assert profile["recent_activity_count"] is None


# ------------------------------------------------- fetch 失败隔离

def test_fetch_isolates_each_dimension_failure() -> None:
    """四个维度全部失败时画像仍可返回，并逐个记录失败原因。"""
    collector, _, _ = _build_collector(
        get_user_info=RuntimeError("-352 风控"),
        get_user_relation_stat=RuntimeError("关系超时"),
        get_user_videos=RuntimeError("投稿不可用"),
        get=RuntimeError("动态不可用"),
    )

    profile = asyncio.run(collector.fetch(99))

    assert profile["name"] == "UID 99"
    # 维度全部失败时关键字段保持未知，而不是伪造为 0。
    assert profile["level"] is None
    assert profile["follower"] is None
    assert len(profile["data_errors"]) == 4
    joined = " | ".join(profile["data_errors"])
    assert "资料不可用" in joined
    assert "关系数据不可用" in joined
    assert "投稿数据不可用" in joined
    assert "动态数据不可用" in joined


def test_fetch_records_single_failure_without_affecting_others() -> None:
    """单项失败不得影响其它维度的正常取值。"""
    collector, _, _ = _build_collector(
        get_user_info={"data": {"name": "半可用用户", "level": 4}},
        get_user_relation_stat=RuntimeError("boom"),
        get_user_videos={"data": {"page": {"count": 3}}},
        get={"items": []},
    )

    profile = asyncio.run(collector.fetch(31))

    assert profile["name"] == "半可用用户"
    assert profile["level"] == 4
    assert profile["video_count"] == 3
    assert profile["follower"] is None
    assert len(profile["data_errors"]) == 1
    assert profile["data_errors"][0].startswith("关系数据不可用: ")


def test_fetch_downgrades_non_dict_response_to_empty() -> None:
    """接口返回非字典（如列表）时降级为空响应并记为格式无效，不抛异常。"""
    collector, _, _ = _build_collector(
        get_user_info=["不是字典"],
        get_user_relation_stat="也不是字典",
        get_user_videos=None,
        get=[{"items": []}],
    )

    profile = asyncio.run(collector.fetch(5))

    assert profile["name"] == "UID 5"
    assert profile["level"] is None
    # 非字典响应按格式无效记入 data_errors，而不是静默吞掉。
    assert len(profile["data_errors"]) == 4
    assert all("响应格式无效" in item for item in profile["data_errors"])


# ------------------------------------------------- _safe_request 直接契约

def test_safe_request_returns_dict_payload() -> None:
    """_safe_request 应返回 (字典响应, "ok") 元组。"""

    async def coro():
        """提供预置字典的协程。"""
        return {"data": {"level": 3}}

    errors: list[str] = []
    result, status = asyncio.run(ProfileCollector._safe_request(coro(), "无错误", errors))

    assert result == {"data": {"level": 3}}
    assert status == "ok"
    assert errors == []


def test_safe_request_appends_label_for_exception() -> None:
    """_safe_request 捕获异常后应写入带类别前缀的错误信息并返回 (空字典, "error")。"""

    async def coro():
        """抛出预置异常的协程。"""
        raise ValueError("接口挂了")

    errors: list[str] = []
    result, status = asyncio.run(ProfileCollector._safe_request(coro(), "资料不可用", errors))

    assert result == {}
    assert status == "error"
    assert errors == ["资料不可用: 接口挂了"]


def test_safe_request_downgrades_non_dict_payload() -> None:
    """非字典响应统一降级为空字典并标记为 invalid。"""

    async def coro():
        """返回列表的协程。"""
        return [1, 2, 3]

    errors: list[str] = []
    result, status = asyncio.run(ProfileCollector._safe_request(coro(), "关系数据不可用", errors))

    assert result == {}
    assert status == "invalid"
    assert errors == ["关系数据不可用: 响应格式无效"]


# ------------------------------------------------- _summarize_dynamics

def test_summarize_dynamics_returns_zeros_for_empty_input() -> None:
    """空动态样本输出零计数，可观察天数为未知（None），且不触发除零。"""
    summary = ProfileCollector._summarize_dynamics([])

    assert summary == {
        "recent_activity_count": 0,
        "lottery_repost_count": 0,
        "lottery_repost_ratio": 0,
        "observable_account_days": None,
    }


def test_summarize_dynamics_limits_sample_to_twelve() -> None:
    """活跃度样本最多取最近 12 条。"""
    items = [_dynamic_item(pub_ts=1_700_000_000) for _ in range(15)]

    summary = ProfileCollector._summarize_dynamics(items)

    assert summary["recent_activity_count"] == 12


@pytest.mark.parametrize("keyword", ["抽奖", "转发", "开奖", "中奖"])
def test_summarize_dynamics_counts_forward_reposts_with_lottery_keywords(keyword: str) -> None:
    """命中任一抽奖关键词的转发动态应计入抽奖转发数。"""
    items = [
        _dynamic_item(dtype="DYNAMIC_TYPE_FORWARD", text=f"求{keyword}机会"),
        _dynamic_item(dtype="DYNAMIC_TYPE_FORWARD", text=f"{keyword}"),
    ]

    summary = ProfileCollector._summarize_dynamics(items)

    assert summary["lottery_repost_count"] == 2
    assert summary["lottery_repost_ratio"] == 1.0


def test_summarize_dynamics_ignores_non_forward_with_keywords() -> None:
    """非转发动态即使提到抽奖也不计入抽奖转发。"""
    items = [_dynamic_item(dtype="DYNAMIC_TYPE_AV", text="抽奖开奖中奖转发")]

    summary = ProfileCollector._summarize_dynamics(items)

    assert summary["lottery_repost_count"] == 0
    assert summary["lottery_repost_ratio"] == 0


def test_summarize_dynamics_ignores_forward_without_keywords() -> None:
    """转发但正文没有抽奖关键词时不计入。"""
    items = [_dynamic_item(dtype="DYNAMIC_TYPE_FORWARD", text="日常分享")]

    assert ProfileCollector._summarize_dynamics(items)["lottery_repost_count"] == 0


def test_summarize_dynamics_rounds_ratio_to_three_decimals() -> None:
    """抽奖转发比例四舍五入到 3 位小数。"""
    items = [
        _dynamic_item(dtype="DYNAMIC_TYPE_FORWARD", text="抽奖"),
        _dynamic_item(dtype="DYNAMIC_TYPE_AV", text="日常"),
        _dynamic_item(dtype="DYNAMIC_TYPE_AV", text="日常"),
    ]

    summary = ProfileCollector._summarize_dynamics(items)

    assert summary["lottery_repost_count"] == 1
    assert summary["lottery_repost_ratio"] == 0.333


def test_summarize_dynamics_computes_observable_days(monkeypatch) -> None:
    """可观察天数取最早一条带时间戳动态距今天数（取整）。"""
    fixed_now = 1_700_000_000 + 86400 * 10
    # 仅替换模块内 time.time 的引用，测试结束由 monkeypatch 自动还原。
    monkeypatch.setattr("modules.lottery.profile.time.time", lambda: fixed_now)
    items = [
        _dynamic_item(pub_ts=1_700_000_000, text="最早"),
        _dynamic_item(pub_ts=1_700_000_000 + 86400 * 3, text="较新"),
    ]

    summary = ProfileCollector._summarize_dynamics(items)

    assert summary["observable_account_days"] == 10


def test_summarize_dynamics_skips_items_without_modules() -> None:
    """缺少 modules 节点的脏数据不得抛异常，但仍计入活跃样本数。"""
    items = [{}, {"modules": None}, {"modules": {"module_author": None}}]

    summary = ProfileCollector._summarize_dynamics(items)

    assert summary["recent_activity_count"] == 3
    assert summary["lottery_repost_count"] == 0
    # 脏数据没有可用时间戳，可观察天数为未知（None）。
    assert summary["observable_account_days"] is None
