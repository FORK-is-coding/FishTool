"""UP 主多源数据采集器的契约级测试。

覆盖 modules/up_analyzer/data_fetcher.py 的多源降级、维度隔离、本地估算与分区榜单。
HTTP 与 B 站接口全部使用契约级假对象（真实实现 ``async with`` 协议），不使用 AsyncMock。
SQLite 一律使用 tmp_path 下的隔离库（monkeypatch 模块级 get_session），绝不触碰 data/*.db。
"""

import asyncio
from pathlib import Path

import aiohttp
import pytest

from core.database import DatabaseManager, UPMaster
from core.exceptions import BilibiliAPIError, ValidationError

from modules.up_analyzer.data_fetcher import UPDataFetcher


# ------------------------------------------------------------------ 契约级假对象

class _ContractResponse:
    """契约级 HTTP 响应：真实实现 async with，并暴露 status/json()。"""

    def __init__(self, status: int = 200, payload=None, raise_on_json=None) -> None:
        """配置状态码、JSON 载荷或 json() 待抛异常。"""
        self.status = status
        self._payload = payload
        self._raise_on_json = raise_on_json

    async def __aenter__(self):
        """进入响应上下文。"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """退出响应上下文，不吞异常。"""
        return False

    async def json(self):
        """返回预置载荷或抛出预置异常。"""
        if self._raise_on_json is not None:
            raise self._raise_on_json
        return self._payload


class _ContractSession:
    """契约级 aiohttp 会话：get() 返回可 async with 的响应。"""

    def __init__(self, response=None, raise_exc=None) -> None:
        """配置响应对象或 get() 待抛异常。"""
        self._response = response
        self._raise_exc = raise_exc
        self.closed = False
        self.timeouts = []

    def get(self, url, timeout=None):
        """记录超时参数并返回预置响应。"""
        self.timeouts.append(timeout)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response

    async def close(self) -> None:
        """记录关闭调用。"""
        self.closed = True


class _ContractClientSession:
    """契约级 aiohttp.ClientSession 工厂：替代真实会话创建。"""

    def __init__(self, timeout=None, **kwargs) -> None:
        """记录构造参数，供上下文管理测试断言。"""
        self.timeout = timeout
        self.closed = False

    async def close(self) -> None:
        """记录关闭调用。"""
        self.closed = True


class _ContractRateLimiter:
    """契约级限频器：记录端点调用，不做真实等待。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls = []

    async def acquire(self, endpoint: str = "unknown") -> None:
        """记录一次限频请求。"""
        self.calls.append(endpoint)


class _ContractBiliAPI:
    """契约级 B 站 API：每个维度可独立返回数据或抛出异常。"""

    def __init__(self, **behaviour) -> None:
        """按维度名配置返回值或待抛异常。"""
        self.behaviour = behaviour
        self.calls = []

    def _resolve(self, name: str, *args, **kwargs):
        """记录调用并返回配置值或抛出配置异常。"""
        self.calls.append({"name": name, "args": args, "kwargs": kwargs})
        outcome = self.behaviour.get(name, {})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_user_info(self, uid):
        """契约级用户资料接口。"""
        return self._resolve("get_user_info", uid)

    async def get_user_relation_stat(self, uid):
        """契约级关系接口。"""
        return self._resolve("get_user_relation_stat", uid)

    async def get_charge_count(self, uid):
        """契约级充电人数接口。"""
        return self._resolve("get_charge_count", uid)

    async def get_room_base_info(self, uids):
        """契约级直播间基础信息接口。"""
        return self._resolve("get_room_base_info", uids)

    async def get_guard_top_list(self, room_id, ruid):
        """契约级大航海榜接口。"""
        return self._resolve("get_guard_top_list", room_id, ruid)

    async def get_user_upstat(self, uid):
        """契约级累计播放接口。"""
        return self._resolve("get_user_upstat", uid)

    async def get_user_videos(self, uid, page=1, page_size=30):
        """契约级投稿列表接口。"""
        return self._resolve("get_user_videos", uid, page=page, page_size=page_size)

    async def get_ranking(self, rid, day=7, original=0, page=1):
        """契约级分区榜单接口。"""
        return self._resolve("get_ranking", rid=rid, day=day, original=original, page=page)


class _ExplodingSession:
    """契约级会话：任何查询都抛错，用于验证回滚与降级。"""

    def __init__(self) -> None:
        """初始化状态标记。"""
        self.rolled_back = False
        self.closed = False

    def query(self, *args, **kwargs):
        """模拟数据库不可用。"""
        raise RuntimeError("数据库不可用")

    def rollback(self) -> None:
        """记录回滚。"""
        self.rolled_back = True

    def close(self) -> None:
        """记录关闭。"""
        self.closed = True


# ------------------------------------------------------------------ 夹具与辅助

@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch):
    """把 data_fetcher 模块的 get_session 指向 tmp_path 隔离库。"""
    manager = DatabaseManager(str(tmp_path / "up_iso.db"))
    monkeypatch.setattr("modules.up_analyzer.data_fetcher.get_session", manager.get_session)
    yield manager
    manager.engine.dispose()


def _build_fetcher(**behaviour) -> tuple:
    """构造真实 UPDataFetcher + 契约级 API 与限频器。"""
    api = _ContractBiliAPI(**behaviour)
    limiter = _ContractRateLimiter()
    return UPDataFetcher(api, limiter), api, limiter


def _full_success_behaviour() -> dict:
    """返回各维度全部成功的契约配置。"""
    return {
        "get_user_info": {
            "data": {"name": "UP主", "mid": 42, "face": "http://face", "level": 6, "official": {"type": 1}}
        },
        "get_user_relation_stat": {"data": {"follower": 1000}},
        "get_charge_count": {"charge_count": 12, "source": "battery_list"},
        "get_room_base_info": {"data": {"42": {"room_id": 999, "live_status": 1}}},
        "get_guard_top_list": {"data": {"info": {"num": 3}}},
        "get_user_upstat": {"data": {"archive": {"view": 55555}}},
        "get_user_videos": {"data": {"list": {"vlist": [{"bvid": "BV1", "play": 100}]}}},
    }


# ================================================================ 上下文管理

def test_aenter_creates_shared_session(monkeypatch) -> None:
    """进入上下文应创建共享 HTTP 会话。"""
    monkeypatch.setattr(
        "modules.up_analyzer.data_fetcher.aiohttp.ClientSession", _ContractClientSession
    )
    fetcher, _, _ = _build_fetcher()

    async def run():
        """进入上下文并返回内部会话对象。"""
        async with fetcher:
            return fetcher.session

    session = asyncio.run(asyncio.wait_for(run(), timeout=5))

    assert isinstance(session, _ContractClientSession)
    assert isinstance(session.timeout, aiohttp.ClientTimeout)
    assert session.timeout.total == 30


def test_aexit_closes_existing_session() -> None:
    """退出上下文应关闭已创建的会话。"""
    fetcher, _, _ = _build_fetcher()
    session = _ContractClientSession()
    fetcher.session = session

    asyncio.run(fetcher.__aexit__(None, None, None))

    assert session.closed is True


def test_aexit_tolerates_missing_session() -> None:
    """未创建会话时退出上下文不得报错。"""
    fetcher, _, _ = _build_fetcher()

    asyncio.run(fetcher.__aexit__(None, None, None))

    assert fetcher.session is None


# ================================================================ extract_uid_from_url

def test_extract_uid_from_space_url() -> None:
    """标准空间主页链接可提取 UID。"""
    fetcher, _, _ = _build_fetcher()

    assert fetcher.extract_uid_from_url("https://space.bilibili.com/123456") == 123456
    assert fetcher.extract_uid_from_url("https://space.bilibili.com/7/video") == 7


def test_extract_uid_from_plain_digits() -> None:
    """纯数字输入直接转为 UID。"""
    fetcher, _, _ = _build_fetcher()

    assert fetcher.extract_uid_from_url("42") == 42


def test_extract_uid_returns_none_for_unrecognised_text() -> None:
    """无法识别时返回 None 并记录告警。"""
    fetcher, _, _ = _build_fetcher()

    assert fetcher.extract_uid_from_url("https://example.com/abc") is None


def test_extract_uid_returns_none_when_regex_input_is_invalid() -> None:
    """非字符串输入触发异常保护分支，返回 None。"""
    fetcher, _, _ = _build_fetcher()

    assert fetcher.extract_uid_from_url(None) is None


# ================================================================ fetch_from_zeroroku

def test_fetch_from_zeroroku_requires_initialised_session() -> None:
    """会话未初始化时直接返回 None。"""
    fetcher, _, _ = _build_fetcher()

    assert asyncio.run(fetcher.fetch_from_zeroroku(42)) is None


def test_fetch_from_zeroroku_returns_none_on_404() -> None:
    """404 表示未收录，降级返回 None。"""
    fetcher, _, _ = _build_fetcher()
    fetcher.session = _ContractSession(_ContractResponse(status=404))

    assert asyncio.run(fetcher.fetch_from_zeroroku(42)) is None


@pytest.mark.parametrize("status", [500, 429, 403])
def test_fetch_from_zeroroku_returns_none_on_other_errors(status: int) -> None:
    """非 200/404 状态码一律降级。"""
    fetcher, _, _ = _build_fetcher()
    fetcher.session = _ContractSession(_ContractResponse(status=status))

    assert asyncio.run(fetcher.fetch_from_zeroroku(42)) is None


def test_fetch_from_zeroroku_parses_success_payload() -> None:
    """200 时应解析出粉丝曲线、投稿频率与互动率。"""
    fetcher, _, _ = _build_fetcher()
    payload = {
        "fans_growth": [{"date": "2024-05-01", "fans": 100}],
        "post_stats": {"avg_per_week": 2.5},
        "engagement": {"avg_rate": 3.2},
        "recent_videos": [{"bvid": "BV1"}],
    }
    session = _ContractSession(_ContractResponse(status=200, payload=payload))
    fetcher.session = session

    result = asyncio.run(asyncio.wait_for(fetcher.fetch_from_zeroroku(42), timeout=5))

    assert result["source"] == "zeroroku"
    assert result["fans_growth"] == [{"date": "2024-05-01", "fans": 100}]
    assert result["post_frequency"] == 2.5
    assert result["engagement_rate"] == 3.2
    assert result["video_data"] == [{"bvid": "BV1"}]
    assert result["raw_data"] == payload
    # 三方请求必须显式携带 15 秒超时。
    assert session.timeouts[0] == aiohttp.ClientTimeout(total=15)


def test_fetch_from_zeroroku_returns_none_on_timeout() -> None:
    """请求超时应降级返回 None。"""
    fetcher, _, _ = _build_fetcher()
    fetcher.session = _ContractSession(raise_exc=asyncio.TimeoutError())

    assert asyncio.run(fetcher.fetch_from_zeroroku(42)) is None


def test_fetch_from_zeroroku_returns_none_on_generic_error() -> None:
    """其它异常（含 json 解析失败）同样降级返回 None。"""
    fetcher, _, _ = _build_fetcher()
    fetcher.session = _ContractSession(_ContractResponse(status=200, raise_on_json=ValueError("坏 JSON")))

    assert asyncio.run(fetcher.fetch_from_zeroroku(42)) is None


# ================================================================ fetch_from_bilibili

def test_fetch_from_bilibili_maps_all_dimensions() -> None:
    """各维度成功时应完整映射基础资料、粉丝、充电、舰长、播放与投稿。"""
    fetcher, api, limiter = _build_fetcher(**_full_success_behaviour())

    result = asyncio.run(asyncio.wait_for(fetcher.fetch_from_bilibili(42), timeout=5))

    assert result["source"] == "bilibili"
    assert result["name"] == "UP主"
    assert result["mid"] == 42
    assert result["face"] == "http://face"
    assert result["fans"] == 1000
    assert result["charge_count"] == 12
    assert result["charge_source"] == "battery_list"
    assert result["guard_count"] == 3
    assert result["guard_source"] == "guard_top_list"
    assert result["live_status"] == 1
    assert result["stats"]["level"] == 6
    assert result["stats"]["official"] == {"type": 1}
    assert result["stats"]["total_play"] == 55555
    assert result["video_list"] == [{"bvid": "BV1", "play": 100}]
    assert result["data_errors"] == {}
    assert result["api_success"] is True
    # 六个维度各限频一次，全部走 normal 队列。
    assert limiter.calls == ["normal"] * 6


def test_fetch_from_bilibili_marks_guard_source_when_no_room() -> None:
    """未开播或无直播间时舰长数记 0 并标注 no_room。"""
    behaviour = _full_success_behaviour()
    behaviour["get_room_base_info"] = {"data": {"42": {}}}
    fetcher, _, _ = _build_fetcher(**behaviour)

    result = asyncio.run(fetcher.fetch_from_bilibili(42))

    assert result["guard_count"] == 0
    assert result["guard_source"] == "no_room"
    assert result["live_status"] == 0
    # 没有直播间就不应请求大航海榜。
    assert "get_guard_top_list" not in [call["name"] for call in fetcher.bili_api.calls]


def test_fetch_from_bilibili_tolerates_non_dict_room_map() -> None:
    """直播间接口返回非字典时不得抛异常。"""
    behaviour = _full_success_behaviour()
    behaviour["get_room_base_info"] = ["不是字典"]
    fetcher, _, _ = _build_fetcher(**behaviour)

    result = asyncio.run(fetcher.fetch_from_bilibili(42))

    assert result["guard_count"] == 0
    assert result["guard_source"] == "no_room"


def test_fetch_from_bilibili_skips_non_dict_charge_payload() -> None:
    """充电接口返回非字典时保留默认值。"""
    behaviour = _full_success_behaviour()
    behaviour["get_charge_count"] = ["坏数据"]
    fetcher, _, _ = _build_fetcher(**behaviour)

    result = asyncio.run(fetcher.fetch_from_bilibili(42))

    assert result["charge_count"] == 0
    assert result["charge_source"] == "unavailable"


def test_fetch_from_bilibili_uses_archive_view_fallback() -> None:
    """upstat 无 archive 节点时回退到 archive_view。"""
    behaviour = _full_success_behaviour()
    behaviour["get_user_upstat"] = {"data": {"archive_view": 777}}
    fetcher, _, _ = _build_fetcher(**behaviour)

    result = asyncio.run(fetcher.fetch_from_bilibili(42))

    assert result["stats"]["total_play"] == 777


def test_fetch_from_bilibili_falls_back_to_fans_key() -> None:
    """关系接口只提供 fans 字段时同样可用。"""
    behaviour = _full_success_behaviour()
    behaviour["get_user_relation_stat"] = {"data": {"fans": 50}}
    fetcher, _, _ = _build_fetcher(**behaviour)

    result = asyncio.run(fetcher.fetch_from_bilibili(42))

    assert result["fans"] == 50


def test_fetch_from_bilibili_records_partial_failures_and_stays_successful() -> None:
    """少量维度失败时记录错误但仍判定为可用数据。"""
    behaviour = _full_success_behaviour()
    behaviour["get_user_info"] = RuntimeError("资料风控")
    behaviour["get_charge_count"] = RuntimeError("充电失败")
    fetcher, _, _ = _build_fetcher(**behaviour)

    result = asyncio.run(fetcher.fetch_from_bilibili(42))

    assert set(result["data_errors"]) == {"user_info", "charge_count"}
    assert result["name"] == ""
    # 仍有粉丝/投稿数据，判定为可用。
    assert result["api_success"] is True


def test_fetch_from_bilibili_marks_failure_when_four_dimensions_lost() -> None:
    """四个以上维度失败时 api_success 必须为 False。"""
    behaviour = _full_success_behaviour()
    for name in (
        "get_user_info",
        "get_user_relation_stat",
        "get_charge_count",
        "get_room_base_info",
    ):
        behaviour[name] = RuntimeError("挂了")
    fetcher, _, _ = _build_fetcher(**behaviour)

    result = asyncio.run(fetcher.fetch_from_bilibili(42))

    assert len(result["data_errors"]) == 4
    assert result["api_success"] is False


def test_fetch_from_bilibili_marks_failure_when_all_dimensions_empty() -> None:
    """所有维度返回空数据时 api_success 应为 False。"""
    fetcher, _, _ = _build_fetcher(
        get_user_info={},
        get_user_relation_stat={},
        get_charge_count={},
        get_room_base_info={},
        get_user_upstat={},
        get_user_videos={},
    )

    result = asyncio.run(fetcher.fetch_from_bilibili(42))

    assert result["data_errors"] == {}
    assert result["api_success"] is False
    assert result["name"] == ""
    assert result["stats"] == {}


# ================================================================ estimate_metrics

def test_estimate_metrics_returns_partial_when_no_videos() -> None:
    """没有投稿数据时无法估算，标记为 partial。"""
    fetcher, _, _ = _build_fetcher()

    metrics = fetcher.estimate_metrics({"video_list": [], "fans": 100})

    assert metrics == {
        "fans_growth": [],
        "post_frequency": 0,
        "engagement_rate": 0,
        "avg_play": 0,
        "avg_comment": 0,
        "data_completeness": "partial",
    }


def test_estimate_metrics_computes_frequency_play_and_engagement() -> None:
    """多投稿时按时间跨度估算投稿频率，并按粉丝数算互动率。"""
    fetcher, _, _ = _build_fetcher()
    day = 86400
    videos = [
        {"created": 0, "play": 100, "comment": 10},
        {"created": 7 * day, "play": 200, "comment": 20},
        {"created": 14 * day, "play": 300, "comment": 30},
    ]

    metrics = fetcher.estimate_metrics({"video_list": videos, "fans": 300})

    # 14 天发 3 条 => 每周 1.5 条
    assert metrics["post_frequency"] == 1.5
    assert metrics["avg_play"] == 200
    assert metrics["avg_comment"] == 20
    assert metrics["engagement_rate"] == round((200 / 300) * 100, 2)


def test_estimate_metrics_keeps_frequency_zero_for_single_video() -> None:
    """只有一条投稿无法计算频率，但仍估算平均值。"""
    fetcher, _, _ = _build_fetcher()

    metrics = fetcher.estimate_metrics(
        {"video_list": [{"created": 0, "play": 60, "comment": 6}], "fans": 0}
    )

    assert metrics["post_frequency"] == 0
    assert metrics["avg_play"] == 60
    # 粉丝为 0 时不做除零，互动率保持 0。
    assert metrics["engagement_rate"] == 0


def test_estimate_metrics_skips_zero_time_span() -> None:
    """所有投稿时间相同（跨度 0）时不计算频率。"""
    fetcher, _, _ = _build_fetcher()

    metrics = fetcher.estimate_metrics(
        {"video_list": [{"created": 5, "play": 10}, {"created": 5, "play": 20}], "fans": 10}
    )

    assert metrics["post_frequency"] == 0
    assert metrics["avg_play"] == 15


def test_estimate_metrics_degrades_on_bad_numeric_field() -> None:
    """字段类型异常时整体降级为 partial，不向上抛异常。"""
    fetcher, _, _ = _build_fetcher()

    metrics = fetcher.estimate_metrics(
        {"video_list": [{"created": 0, "play": "坏值"}, {"created": 0, "play": 5}], "fans": 10}
    )

    assert metrics["data_completeness"] == "partial"
    assert metrics["avg_play"] == 0
    assert metrics["engagement_rate"] == 0


# ================================================================ _normalize_up_data

def test_normalize_up_data_reads_raw_data_aliases() -> None:
    """三方字段别名应在唯一出口被统一兼容。"""
    fetcher, _, _ = _build_fetcher()
    source = {
        "raw_data": {
            "name": "别名昵称",
            "face": "别名头像",
            "follower": 99,
            "archive_view": 888,
            "recent_videos": [{"bvid": "x"}],
            "stats": {"total_play": 777},
        }
    }

    result = fetcher._normalize_up_data(source)

    assert result["name"] == "别名昵称"
    assert result["face"] == "别名头像"
    assert result["fans"] == 99
    assert result["total_play"] == 777
    assert result["video_list"] == [{"bvid": "x"}]
    assert result["charge_count"] == 0
    assert result["charge_source"] == "unavailable"
    assert result["metrics"] == {"fans_growth": [], "post_frequency": 0, "engagement_rate": 0}


def test_normalize_up_data_reads_nested_profile_alias() -> None:
    """user_info/profile 嵌套结构中的昵称与头像同样可识别。"""
    fetcher, _, _ = _build_fetcher()

    result = fetcher._normalize_up_data({"raw_data": {"profile": {"name": "嵌套", "face": "嵌套头像"}}})

    assert result["name"] == "嵌套"
    assert result["face"] == "嵌套头像"


def test_normalize_up_data_prefers_explicit_metrics_and_fields() -> None:
    """显式传入的 metrics 与已有字段优先级最高。"""
    fetcher, _, _ = _build_fetcher()
    metrics = {"post_frequency": 3, "engagement_rate": 4.5, "avg_play": 10}

    result = fetcher._normalize_up_data(
        {"name": "显式", "fans": 11, "total_play": 22, "video_list": [{"bvid": "y"}]}, metrics
    )

    assert result["name"] == "显式"
    assert result["fans"] == 11
    assert result["total_play"] == 22
    assert result["video_list"] == [{"bvid": "y"}]
    assert result["metrics"] is metrics


def test_normalize_up_data_defaults_empty_payload() -> None:
    """空字典输入应产出结构完整的默认结果。"""
    fetcher, _, _ = _build_fetcher()

    result = fetcher._normalize_up_data({})

    assert result["name"] == ""
    assert result["face"] == ""
    assert result["fans"] == 0
    assert result["total_play"] == 0
    assert result["video_list"] == []
    assert result["charge_count"] == 0


# ================================================================ fetch_up_data

def test_fetch_up_data_returns_full_result_from_zeroroku() -> None:
    """三方数据可用时应标记为 full。"""
    fetcher, _, _ = _build_fetcher()
    fetcher.session = _ContractSession(
        _ContractResponse(
            status=200,
            payload={
                "fans_growth": [{"fans": 1}],
                "post_stats": {"avg_per_week": 4},
                "engagement": {"avg_rate": 2},
                "recent_videos": [{"title": "视频", "play": 10}],
            },
        )
    )

    result = asyncio.run(asyncio.wait_for(fetcher.fetch_up_data("42"), timeout=5))

    assert result["uid"] == 42
    assert result["data_source"] == "zeroroku"
    assert result["completeness"] == "full"
    assert result["data"]["metrics"]["post_frequency"] == 4


def test_fetch_up_data_parses_uid_from_space_url() -> None:
    """传入主页链接时应先解析出 UID。"""
    fetcher, _, _ = _build_fetcher()

    result = asyncio.run(fetcher.fetch_up_data("https://space.bilibili.com/123"))

    assert result["uid"] == 123


def test_fetch_up_data_rejects_unparsable_url() -> None:
    """无法解析 UID 时必须显式报错（从外部打异常）。"""
    fetcher, _, _ = _build_fetcher()

    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(fetcher.fetch_up_data("https://example.com/nobody"))

    assert "无法解析UID" in str(excinfo.value)
    assert excinfo.value.code == "VALIDATION_ERROR"


def test_fetch_up_data_falls_back_to_bilibili_plus_local(isolated_db) -> None:
    """三方缺失但 B 站可用时走 partial 并落库快照。"""
    fetcher, _, _ = _build_fetcher(**_full_success_behaviour())

    result = asyncio.run(asyncio.wait_for(fetcher.fetch_up_data("42"), timeout=5))

    assert result["data_source"] == "bilibili+local"
    assert result["completeness"] == "partial"
    assert result["data"]["name"] == "UP主"
    assert result["data"]["fans"] == 1000
    assert result["data"]["api_success"] is True
    # 快照已写入隔离库。
    session = isolated_db.get_session()
    try:
        row = session.query(UPMaster).filter(UPMaster.mid == 42).one()
        assert row.name == "UP主"
        assert row.follower == 1000
        assert row.charge_count == 12
    finally:
        session.close()


def test_fetch_up_data_marks_failed_when_bilibili_unusable(isolated_db) -> None:
    """B 站接口不可用时标记为 failed（本地降级估算）。"""
    fetcher, _, _ = _build_fetcher(
        get_user_info=RuntimeError("挂了"),
        get_user_relation_stat=RuntimeError("挂了"),
        get_charge_count=RuntimeError("挂了"),
        get_room_base_info=RuntimeError("挂了"),
    )

    result = asyncio.run(fetcher.fetch_up_data("42"))

    assert result["data_source"] == "local_fallback"
    assert result["completeness"] == "failed"
    assert result["data"]["api_success"] is False
    # 全维度失败时没有可用 mid，不应写入任何快照。
    session = isolated_db.get_session()
    try:
        assert session.query(UPMaster).count() == 0
    finally:
        session.close()


# ================================================================ fetch_category_top_ups

def _ranking_payload(items) -> dict:
    """构造分区榜单响应。"""
    return {"data": {"list": items}}


def _rank_item(mid, rank=1, view=100) -> dict:
    """构造榜单中的一条视频条目。"""
    return {
        "owner": {"mid": mid, "name": f"UP{mid}", "face": "http://face"},
        "title": f"标题{mid}",
        "bvid": f"BV{mid}",
        "stat": {"view": view},
        "rank": rank,
    }


@pytest.mark.parametrize(
    "category",
    ["游戏", "游戏区", "游戏分区"],
)
def test_fetch_category_top_ups_normalizes_category_names(category: str) -> None:
    """「游戏/游戏区/游戏分区」应归一到同一分区 ID。"""
    fetcher, api, _ = _build_fetcher(get_ranking=_ranking_payload([]))

    asyncio.run(fetcher.fetch_category_top_ups(category, limit=3))

    ranking_call = next(call for call in api.calls if call["name"] == "get_ranking")
    assert ranking_call["kwargs"]["rid"] == 4
    assert ranking_call["kwargs"]["day"] == 7


def test_fetch_category_top_ups_matches_category_by_keyword() -> None:
    """未知展示名可按包含关系模糊匹配到标准分区。"""
    fetcher, api, _ = _build_fetcher(get_ranking=_ranking_payload([]))

    asyncio.run(fetcher.fetch_category_top_ups("美食探店"))

    ranking_call = next(call for call in api.calls if call["name"] == "get_ranking")
    assert ranking_call["kwargs"]["rid"] == 211


def test_fetch_category_top_ups_rejects_unknown_category() -> None:
    """未知分区必须明确报错，不能静默返回空列表（从外部打异常）。"""
    fetcher, _, _ = _build_fetcher()

    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(fetcher.fetch_category_top_ups("不存在的分区"))

    assert "不支持的B站分区" in str(excinfo.value)


def test_fetch_category_top_ups_rejects_empty_ranking() -> None:
    """榜单返回空数据时应报 BilibiliAPIError（从外部打异常）。"""
    fetcher, _, _ = _build_fetcher(get_ranking={"data": None})

    with pytest.raises(BilibiliAPIError) as excinfo:
        asyncio.run(fetcher.fetch_category_top_ups("游戏"))

    assert "分区榜单失败" in str(excinfo.value)


def test_fetch_category_top_ups_wraps_ranking_exception() -> None:
    """榜单接口异常应被包装为 BilibiliAPIError（从外部打异常）。"""
    fetcher, _, _ = _build_fetcher(get_ranking=RuntimeError("风控"))

    with pytest.raises(BilibiliAPIError) as excinfo:
        asyncio.run(fetcher.fetch_category_top_ups("游戏"))

    assert "type 参数只接受 all/origin 字符串" in str(excinfo.value)


def test_fetch_category_top_ups_enriches_owners_and_records_upstat_gap() -> None:
    """应补齐每位 UP 的粉丝数与充电人数；upstat 维度当前存在已知缺陷需记录。"""
    fetcher, api, limiter = _build_fetcher(
        get_ranking=_ranking_payload([_rank_item(7)]),
        get_user_relation_stat={"data": {"follower": 500}},
        get_charge_count={"charge_count": 3, "source": "battery_list"},
        get_user_upstat={"data": {"archive": {"view": 9999}}},
    )

    result = asyncio.run(asyncio.wait_for(fetcher.fetch_category_top_ups("游戏", limit=5), timeout=5))

    assert len(result) == 1
    item = result[0]
    assert item["uid"] == 7
    assert item["name"] == "UP7"
    assert item["video_title"] == "标题7"
    assert item["bvid"] == "BV7"
    assert item["play"] == 100
    assert item["follower_count"] == 500
    assert item["charge_count"] == 3
    assert item["charge_source"] == "battery_list"
    # 已知缺陷：upstat 分支引用了未定义的局部变量，异常被就地捕获。
    assert "upstat" in item.get("data_errors", {})
    assert item["total_play"] == 0
    assert "get_user_upstat" not in [call["name"] for call in api.calls]
    # 1 次榜单 + 每位 UP 3 次（关系/充电/播放）。
    assert len(limiter.calls) == 4


def test_fetch_category_top_ups_applies_limit() -> None:
    """limit 应截断榜单条目数量。"""
    items = [_rank_item(index, rank=index) for index in range(1, 6)]
    fetcher, _, _ = _build_fetcher(
        get_ranking=_ranking_payload(items),
        get_user_relation_stat={"data": {"follower": 1}},
        get_charge_count={"charge_count": 0},
    )

    result = asyncio.run(fetcher.fetch_category_top_ups("游戏", limit=2))

    assert [item["uid"] for item in result] == [1, 2]


def test_fetch_category_top_ups_skips_enrichment_without_owner_uid() -> None:
    """榜单条目缺少 owner.mid 时跳过补取，不影响其它条目。"""
    fetcher, api, _ = _build_fetcher(
        get_ranking=_ranking_payload([{"owner": {}, "title": "无主", "bvid": "BV0"}]),
    )

    result = asyncio.run(fetcher.fetch_category_top_ups("游戏"))

    assert result[0]["uid"] is None
    assert result[0]["follower_count"] == 0
    assert "get_user_relation_stat" not in [call["name"] for call in api.calls]


def test_fetch_category_top_ups_records_dimension_failures() -> None:
    """单个 UP 的维度失败只记录到 data_errors，不中断整体榜单。"""
    fetcher, _, _ = _build_fetcher(
        get_ranking=_ranking_payload([_rank_item(7), _rank_item(8)]),
        get_user_relation_stat=RuntimeError("关系失败"),
        get_charge_count=RuntimeError("充电失败"),
    )

    result = asyncio.run(fetcher.fetch_category_top_ups("游戏"))

    assert [item["uid"] for item in result] == [7, 8]
    assert result[0]["follower_count"] == 0
    assert set(result[0]["data_errors"]) == {"relation_stat", "charge_count", "upstat"}


# ================================================================ _save_account_snapshot

def test_save_account_snapshot_skips_invalid_mid(monkeypatch) -> None:
    """缺少 mid 时直接返回，且不打开数据库会话。"""

    def exploding_get_session():
        """若被调用说明逻辑有误。"""
        raise AssertionError("无效 mid 不应打开数据库会话")

    monkeypatch.setattr("modules.up_analyzer.data_fetcher.get_session", exploding_get_session)

    fetcher, _, _ = _build_fetcher()
    fetcher._save_account_snapshot({"mid": 0, "name": "无主"})


def test_save_account_snapshot_inserts_new_row(isolated_db) -> None:
    """首次采集应新建 UP 主档案。"""
    fetcher, _, _ = _build_fetcher()

    fetcher._save_account_snapshot({"mid": 42, "name": "UP主", "fans": 100, "charge_count": 5})

    session = isolated_db.get_session()
    try:
        row = session.query(UPMaster).filter(UPMaster.mid == 42).one()
        assert row.name == "UP主"
        assert row.follower == 100
        assert row.charge_count == 5
        assert row.updated_at is not None
    finally:
        session.close()


def test_save_account_snapshot_updates_existing_row(isolated_db) -> None:
    """重复采集应更新既有档案而非新增。"""
    session = isolated_db.get_session()
    try:
        session.add(UPMaster(mid=42, name="旧名字", follower=1, charge_count=0))
        session.commit()
    finally:
        session.close()
    fetcher, _, _ = _build_fetcher()

    fetcher._save_account_snapshot({"mid": 42, "name": "新名字", "fans": 200, "charge_count": 9})

    session = isolated_db.get_session()
    try:
        assert session.query(UPMaster).count() == 1
        row = session.query(UPMaster).filter(UPMaster.mid == 42).one()
        assert row.name == "新名字"
        assert row.follower == 200
        assert row.charge_count == 9
    finally:
        session.close()


def test_save_account_snapshot_rolls_back_on_failure(monkeypatch) -> None:
    """落库失败必须回滚并释放会话，不向上抛异常。"""
    session = _ExplodingSession()
    monkeypatch.setattr("modules.up_analyzer.data_fetcher.get_session", lambda: session)
    fetcher, _, _ = _build_fetcher()

    fetcher._save_account_snapshot({"mid": 42, "name": "UP主"})

    assert session.rolled_back is True
    assert session.closed is True
