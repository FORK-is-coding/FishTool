"""B站活动情报追踪器的契约级测试。

覆盖 modules/hotspot/activity_tracker.py 的 ActivityTracker：
- __init__：限频器三级兜底
- get_official_activities：字段标准化、链接回退、异常降级
- get_ugc_account_dynamics：rich_text/topic/major/图片解析、offset 规则、逐条容错
- fetch_all_activities：官方+UGC 合并、分区标记、汇总计数
- _get_zone_accounts：内置默认与 config.yaml 覆盖两种格式
- _is_activity_related / _dynamic_to_activity / _extract_title_from_text
- _parse_timestamp：秒/毫秒/非法值
- _get_activity_status：upcoming/ongoing/ended/unknown
- _save_to_database：新增、upsert、异常回滚

网络层使用真实实现 __init__ 的契约级假对象（非 AsyncMock），
数据库使用 tmp_path 下的真实 SQLite。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from bilibili.rate_limiter import RateLimiter
from core.database import Activity, DatabaseManager
from core.exceptions import BilibiliAPIError
from modules.hotspot import activity_tracker as tracker_module
from modules.hotspot.activity_tracker import ActivityTracker


# --------------------------------------------------------------------- 假对象

class FakeLimiter:
    """记录调用参数的假限频器，永不阻塞。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def acquire(self, *args, **kwargs):
        """记录一次限频调用。"""
        self.calls.append((args, kwargs))


class FakeAPI:
    """按脚本顺序返回响应或异常的契约级假 API。"""

    def __init__(self, responses=None, rate_limiter=None) -> None:
        self._responses = list(responses or [])
        self.rate_limiter = rate_limiter
        self.calls: list[dict] = []

    async def get(self, url, params=None, need_sign=False):
        """记录请求并按脚本弹出响应。"""
        self.calls.append({"url": url, "params": params, "need_sign": need_sign})
        if not self._responses:
            raise RuntimeError("没有更多脚本化响应")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeQuery:
    """只支持 filter_by(...).first() 的查询替身。"""

    def filter_by(self, **kwargs):
        """忽略过滤条件，返回自身。"""
        return self

    def first(self):
        """始终返回不存在。"""
        return None


class _CommitFailSession:
    """提交即失败的会话替身，用于验证回滚分支。"""

    def __init__(self) -> None:
        self.rolled_back = False
        self.closed = False

    def query(self, model):
        """返回空查询替身。"""
        return _FakeQuery()

    def add(self, obj) -> None:
        """接收但不真正持久化。"""

    def commit(self) -> None:
        """模拟提交失败。"""
        raise RuntimeError("commit boom")

    def rollback(self) -> None:
        """记录回滚。"""
        self.rolled_back = True

    def close(self) -> None:
        """记录关闭。"""
        self.closed = True


def _make_tracker(limiter=None, api=None) -> ActivityTracker:
    """构造注入了假限频器的追踪器。"""
    api = api or FakeAPI(rate_limiter=limiter)
    return ActivityTracker(api=api, rate_limiter=limiter)


# --------------------------------------------------------------------- 初始化

def test_init_keeps_injected_rate_limiter() -> None:
    """显式传入的限频器应被原样保留。"""
    limiter = FakeLimiter()
    tracker = ActivityTracker(api=FakeAPI(), rate_limiter=limiter)

    assert tracker.rate_limiter is limiter


def test_init_reuses_api_rate_limiter() -> None:
    """未传入时应复用 api 自带的限频器。"""
    shared = RateLimiter(rate=0.0)
    tracker = ActivityTracker(api=FakeAPI(rate_limiter=shared), rate_limiter=None)

    assert tracker.rate_limiter is shared


def test_init_creates_fallback_rate_limiter() -> None:
    """api 也没有限频器时应自建兜底实例。"""
    tracker = ActivityTracker(api=FakeAPI(), rate_limiter=None)

    assert isinstance(tracker.rate_limiter, RateLimiter)


# --------------------------------------------------------------------- 官方活动

def test_get_official_activities_parses_fields() -> None:
    """官方活动应标准化字段并推断进行中状态。"""
    now = datetime.now()
    api = FakeAPI(responses=[{
        "list": [{
            "id": 1,
            "name": "  活动A  ",
            "pc_url": "https://pc",
            "cover": "cover.jpg",
            "desc": "  简介  ",
            "stime": int((now - timedelta(days=1)).timestamp()),
            "etime": int((now + timedelta(days=1)).timestamp()),
            "tags": ["tag1"],
        }]
    }])
    tracker = _make_tracker(api=api)

    activities = asyncio.run(tracker.get_official_activities())

    activity = activities[0]
    assert activity["id"] == 1
    assert activity["title"] == "活动A"
    assert activity["link"] == "https://pc"
    assert activity["desc"] == "简介"
    assert activity["status"] == "ongoing"
    assert activity["tags"] == ["tag1"]
    assert activity["source"] == "official"
    assert isinstance(activity["fetched_at"], datetime)


def test_get_official_activities_link_fallback_chain() -> None:
    """链接应依次回退 pc_url -> h5_url -> url。"""
    api = FakeAPI(responses=[{"list": [
        {"id": 1, "name": "A", "h5_url": "https://h5"},
        {"id": 2, "name": "B", "url": "https://raw"},
        {"id": 3, "name": "C"},
    ]}])
    tracker = _make_tracker(api=api)

    activities = asyncio.run(tracker.get_official_activities())

    assert [item["link"] for item in activities] == ["https://h5", "https://raw", ""]


def test_get_official_activities_passes_expected_params() -> None:
    """请求参数应携带 pn/ps/type/plat 且无需签名。"""
    limiter = FakeLimiter()
    api = FakeAPI(responses=[{"list": []}], rate_limiter=limiter)
    tracker = _make_tracker(limiter=limiter, api=api)

    asyncio.run(tracker.get_official_activities(page=2, page_size=15))

    call = api.calls[0]
    assert call["params"] == {"pn": 2, "ps": 15, "type": 0, "plat": 1}
    assert call["need_sign"] is False
    assert limiter.calls and limiter.calls[0] == ((), {})


@pytest.mark.parametrize("payload", [None, {}, {"foo": "bar"}])
def test_get_official_activities_returns_empty_on_bad_payload(payload) -> None:
    """缺少 list 字段时应返回空列表。"""
    tracker = _make_tracker(api=FakeAPI(responses=[payload]))

    assert asyncio.run(tracker.get_official_activities()) == []


def test_get_official_activities_swallows_api_error() -> None:
    """BilibiliAPIError 应被吞掉并返回空列表。"""
    tracker = _make_tracker(api=FakeAPI(responses=[BilibiliAPIError("boom")]))

    assert asyncio.run(tracker.get_official_activities()) == []


def test_get_official_activities_swallows_unexpected_error() -> None:
    """非 APIError 的异常同样不应冒泡。"""
    tracker = _make_tracker(api=FakeAPI(responses=[RuntimeError("network")]))

    assert asyncio.run(tracker.get_official_activities()) == []


def test_get_official_activities_name_none_aborts_batch() -> None:
    """name 为 None 时当前实现整批丢弃（固化现状）。"""
    api = FakeAPI(responses=[{"list": [{"id": 1, "name": None}]}])
    tracker = _make_tracker(api=api)

    assert asyncio.run(tracker.get_official_activities()) == []


# --------------------------------------------------------------------- UGC 动态

def _ugc_payload() -> dict:
    """构造一条包含 rich_text/topic/视频标题/图片的完整动态。"""
    return {
        "items": [{
            "id_str": "dyn1",
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {
                "module_author": {"pub_ts": 1700000000},
                "module_dynamic": {
                    "desc": {"text": None, "rich_text_nodes": [{"text": "征稿"}, {"text": "活动开启"}]},
                    "topic": {"name": "画师同人展"},
                    "major": {"type": "MAJOR_TYPE_DRAW", "draw": {"items": [{"src": "img1"}, {"src": "img2"}]}},
                },
            },
        }]
    }


def test_get_ugc_parses_rich_text_topic_and_images() -> None:
    """新版动态应拼接 rich_text、取话题名与图片 URL。"""
    api = FakeAPI(responses=[_ugc_payload()])
    tracker = _make_tracker(api=api)

    dynamics = asyncio.run(tracker.get_ugc_account_dynamics("26366366"))

    dynamic = dynamics[0]
    assert dynamic["id"] == "dyn1"
    assert dynamic["uid"] == "26366366"
    assert dynamic["username"] == "哔哩哔哩活动"
    assert dynamic["text"] == "征稿活动开启"
    assert dynamic["topic"] == "画师同人展"
    assert dynamic["images"] == ["img1", "img2"]
    assert dynamic["pub_time"] == datetime.fromtimestamp(1700000000)
    assert dynamic["source"] == "ugc_dynamic"


def test_get_ugc_prefers_desc_text_when_present() -> None:
    """desc.text 存在时应直接使用，不再拼节点。"""
    payload = {
        "items": [{
            "id_str": "dyn2",
            "modules": {
                "module_author": {"pub_ts": 1700000000},
                "module_dynamic": {"desc": {"text": "参加活动"}},
            },
        }]
    }
    tracker = _make_tracker(api=FakeAPI(responses=[payload]))

    dynamics = asyncio.run(tracker.get_ugc_account_dynamics("1"))

    assert dynamics[0]["text"] == "参加活动"


def test_get_ugc_extracts_major_title() -> None:
    """视频/专栏标题应参与关键词过滤。"""
    payload = {
        "items": [{
            "id_str": "dyn3",
            "modules": {
                "module_author": {"pub_ts": 1700000000},
                "module_dynamic": {"major": {"archive": {"title": "创作激励计划"}}},
            },
        }]
    }
    tracker = _make_tracker(api=FakeAPI(responses=[payload]))

    dynamics = asyncio.run(tracker.get_ugc_account_dynamics("1"))

    assert dynamics[0]["title"] == "创作激励计划"


def test_get_ugc_omits_offset_when_zero() -> None:
    """offset=0 时不应携带该参数（B站会报参数异常）。"""
    api = FakeAPI(responses=[{"items": []}])
    tracker = _make_tracker(api=api)

    asyncio.run(tracker.get_ugc_account_dynamics("1", offset=0))

    assert "offset" not in api.calls[0]["params"]


def test_get_ugc_passes_offset_when_positive() -> None:
    """offset>0 时应透传该参数。"""
    api = FakeAPI(responses=[{"items": []}])
    tracker = _make_tracker(api=api)

    asyncio.run(tracker.get_ugc_account_dynamics("1", offset=20))

    assert api.calls[0]["params"]["offset"] == 20
    assert api.calls[0]["params"]["host_mid"] == "1"


def test_get_ugc_filters_unrelated_dynamics() -> None:
    """不含活动关键词的动态应被过滤。"""
    payload = {"items": [{"id_str": "x", "modules": {"module_dynamic": {"desc": {"text": "今天天气不错"}}}}]}
    tracker = _make_tracker(api=FakeAPI(responses=[payload]))

    assert asyncio.run(tracker.get_ugc_account_dynamics("1")) == []


def test_get_ugc_respects_limit() -> None:
    """limit 应限制解析的动态条数。"""
    base = _ugc_payload()["items"][0]
    payload = {"items": [dict(base, id_str=f"d{index}") for index in range(5)]}
    tracker = _make_tracker(api=FakeAPI(responses=[payload]))

    dynamics = asyncio.run(tracker.get_ugc_account_dynamics("1", limit=2))

    assert len(dynamics) == 2


def test_get_ugc_skips_broken_item() -> None:
    """单条动态解析失败应被跳过，不影响整批。"""
    payload = {"items": [123, _ugc_payload()["items"][0]]}
    tracker = _make_tracker(api=FakeAPI(responses=[payload]))

    dynamics = asyncio.run(tracker.get_ugc_account_dynamics("1"))

    assert len(dynamics) == 1


def test_get_ugc_invalid_pub_ts_yields_none_time() -> None:
    """pub_ts 非法时应把发布时间置为 None。"""
    payload = {"items": [{"id_str": "d", "modules": {"module_author": {"pub_ts": "abc"}, "module_dynamic": {"desc": {"text": "活动"}}}}]}
    tracker = _make_tracker(api=FakeAPI(responses=[payload]))

    assert asyncio.run(tracker.get_ugc_account_dynamics("1"))[0]["pub_time"] is None


@pytest.mark.parametrize("payload", [None, {}, {"other": 1}])
def test_get_ugc_returns_empty_on_bad_payload(payload) -> None:
    """缺少 items 字段时返回空列表。"""
    tracker = _make_tracker(api=FakeAPI(responses=[payload]))

    assert asyncio.run(tracker.get_ugc_account_dynamics("1")) == []


def test_get_ugc_swallows_api_and_unexpected_errors() -> None:
    """APIError 与一般异常都应被吞掉。"""
    assert asyncio.run(_make_tracker(api=FakeAPI(responses=[BilibiliAPIError("x")])).get_ugc_account_dynamics("1")) == []
    assert asyncio.run(_make_tracker(api=FakeAPI(responses=[RuntimeError("x")])).get_ugc_account_dynamics("1")) == []


def test_get_ugc_uses_dynamic_endpoint_bucket() -> None:
    """UGC 动态应走 dynamic 专用限频桶。"""
    limiter = FakeLimiter()
    tracker = _make_tracker(limiter=limiter)
    tracker.api._responses = [{"items": []}]

    asyncio.run(tracker.get_ugc_account_dynamics("1"))

    assert limiter.calls == [((), {"endpoint": "dynamic"})]


# --------------------------------------------------------------------- 关键词过滤

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", False),
        (None, False),
        ("今天天气不错", False),
        ("快来参加活动", True),
        ("创作激励计划上线", True),
        ("赛", True),
    ],
)
def test_is_activity_related(text, expected) -> None:
    """活动关键词匹配应覆盖空值与正负样本。"""
    tracker = _make_tracker()

    assert tracker._is_activity_related(text) is expected


# --------------------------------------------------------------------- 转换与工具

def test_dynamic_to_activity_prefers_topic_title() -> None:
    """标题优先级为 topic > title > 正文首行。"""
    tracker = _make_tracker()
    dynamic = {
        "id": "dyn1",
        "topic": "话题名",
        "title": "视频标题",
        "text": "正文内容",
        "images": ["cover.jpg"],
        "pub_time": datetime(2026, 1, 1),
        "uid": "42",
        "username": "UP",
        "fetched_at": datetime(2026, 1, 2),
    }

    activity = tracker._dynamic_to_activity(dynamic)

    assert activity["id"] == "ugc_dyn1"
    assert activity["title"] == "话题名"
    assert activity["link"] == "https://t.bilibili.com/dyn1"
    assert activity["cover"] == "cover.jpg"
    assert activity["start_time"] == datetime(2026, 1, 1)
    assert activity["end_time"] is None
    assert activity["status"] == "unknown"
    assert activity["source"] == "ugc"
    assert activity["source_uid"] == "42"
    assert activity["source_username"] == "UP"


def test_dynamic_to_activity_falls_back_to_body_title() -> None:
    """无 topic/title 时从正文首行提取标题，无图时封面为空。"""
    tracker = _make_tracker()
    dynamic = {
        "id": "d",
        "topic": "",
        "title": "",
        "text": "第一行\n第二行",
        "images": [],
        "pub_time": datetime(2026, 1, 1),
        "uid": "42",
        "username": "UP",
        "fetched_at": datetime(2026, 1, 2),
    }

    activity = tracker._dynamic_to_activity(dynamic)

    assert activity["title"] == "第一行"
    assert activity["cover"] == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "无标题"),
        (None, "无标题"),
        ("   ", "无标题"),
        ("第一行\n第二行", "第一行"),
        ("短标题", "短标题"),
    ],
)
def test_extract_title_from_text(text, expected) -> None:
    """标题提取应覆盖空值、多行与短文本。"""
    tracker = _make_tracker()

    assert tracker._extract_title_from_text(text) == expected


def test_extract_title_from_text_truncates_long_line() -> None:
    """超过 30 字的第一行应截断并加省略号。"""
    tracker = _make_tracker()
    text = "字" * 40

    title = tracker._extract_title_from_text(text)

    assert title == "字" * 30 + "..."


@pytest.mark.parametrize("ts", [None, 0, "abc", ""])
def test_parse_timestamp_invalid_returns_none(ts) -> None:
    """空值与非法时间戳返回 None。"""
    tracker = _make_tracker()

    assert tracker._parse_timestamp(ts) is None


def test_parse_timestamp_seconds_and_milliseconds() -> None:
    """秒与毫秒时间戳都应解析为同一时刻附近。"""
    tracker = _make_tracker()

    seconds = tracker._parse_timestamp(1_700_000_000)
    milliseconds = tracker._parse_timestamp(1_700_000_000_000)

    assert seconds == datetime.fromtimestamp(1_700_000_000)
    assert milliseconds == seconds


def test_get_activity_status_variants() -> None:
    """四种状态判定应覆盖未来/过去/进行中/信息不全。"""
    tracker = _make_tracker()
    now = datetime.now()

    assert tracker._get_activity_status(now + timedelta(days=1), None) == "upcoming"
    assert tracker._get_activity_status(now - timedelta(days=2), now - timedelta(days=1)) == "ended"
    assert tracker._get_activity_status(now - timedelta(days=1), now + timedelta(days=1)) == "ongoing"
    assert tracker._get_activity_status(None, None) == "unknown"
    assert tracker._get_activity_status(now - timedelta(days=1), None) == "unknown"
    assert tracker._get_activity_status(None, now + timedelta(days=1)) == "unknown"


# --------------------------------------------------------------------- 分区账号

class _FakeConfigManager:
    """返回预置 activity.accounts 的配置管理器替身。"""

    accounts: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get(self, key, default=None):
        """仅对 activity 键返回预置结构。"""
        if key == "activity":
            return {"accounts": dict(type(self).accounts)}
        return default


class _BoomConfigManager:
    """构造即失败的配置管理器替身，用于验证回退分支。"""

    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError("config broken")


def _install_config(monkeypatch: pytest.MonkeyPatch, accounts: dict | None = None, *, boom: bool = False) -> None:
    """把 core.config.ConfigManager 换成可控假类，避免读取真实 config.yaml。"""
    import importlib

    # 注意：core.config 属性被 core.config 单例实例遮蔽，必须用 importlib 取子模块。
    config_module = importlib.import_module("core.config")

    if boom:
        monkeypatch.setattr(config_module, "ConfigManager", _BoomConfigManager)
    else:
        scoped = type("_ScopedConfigManager", (_FakeConfigManager,), {"accounts": accounts or {}})
        monkeypatch.setattr(config_module, "ConfigManager", scoped)


def test_get_zone_accounts_defaults_per_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置时应返回内置分区账号表。"""
    _install_config(monkeypatch, {})
    tracker = _make_tracker()

    assert tracker._get_zone_accounts("game") == ActivityTracker.ZONE_ACCOUNTS["game"]
    assert tracker._get_zone_accounts("unknown-zone") == ActivityTracker.ZONE_ACCOUNTS["all"]


def test_get_zone_accounts_from_string_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """config.yaml 的 "uid:名称" 列表格式应被解析。"""
    _install_config(monkeypatch, {"game": ["111:甲", "222:乙", "333"]})
    tracker = _make_tracker()

    assert tracker._get_zone_accounts("game") == [("111", "甲"), ("222", "乙"), ("333", "333")]


def test_get_zone_accounts_from_dict_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """config.yaml 的字典列表格式应被解析。"""
    _install_config(monkeypatch, {"game": [{"uid": 111, "name": "甲"}, {"uid": 222}]})
    tracker = _make_tracker()

    assert tracker._get_zone_accounts("game") == [("111", "甲"), ("222", "222")]


def test_get_zone_accounts_falls_back_when_config_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置读取失败时应回退内置默认，不阻塞主流程。"""
    _install_config(monkeypatch, boom=True)
    tracker = _make_tracker()

    assert tracker._get_zone_accounts("game") == ActivityTracker.ZONE_ACCOUNTS["game"]


def test_get_zone_accounts_ignores_unknown_zone_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置只覆盖了其它分区时，当前分区仍用内置默认。"""
    _install_config(monkeypatch, {"anime": ["999:番剧"]})
    tracker = _make_tracker()

    assert tracker._get_zone_accounts("paint") == ActivityTracker.ZONE_ACCOUNTS["paint"]


# --------------------------------------------------------------------- 汇总入口

def test_fetch_all_activities_without_ugc(monkeypatch: pytest.MonkeyPatch) -> None:
    """include_ugc=False 时只汇总官方活动。"""
    tracker = _make_tracker()
    saved: list = []

    async def _fake_official(page=1, page_size=20):
        """返回单条官方活动。"""
        return [{"id": 1, "title": "官A", "source": "official", "status": "ongoing"}]

    async def _recorder(activities, zone="all"):
        """记录落库调用。"""
        saved.append((activities, zone))

    monkeypatch.setattr(tracker, "get_official_activities", _fake_official)
    monkeypatch.setattr(tracker, "_save_to_database", _recorder)
    monkeypatch.setattr(tracker, "_get_zone_accounts", lambda zone: [("1", "甲")])

    result = asyncio.run(tracker.fetch_all_activities(include_ugc=False, zone="game"))

    assert result["official_count"] == 1
    assert result["ugc_count"] == 0
    assert result["total_count"] == 1
    assert result["zone"] == "game"
    assert result["zone_accounts"] == [{"uid": "1", "name": "甲"}]
    assert result["activities"][0]["zone"] == "all"
    assert saved and saved[0][1] == "game"


def test_fetch_all_activities_merges_ugc(monkeypatch: pytest.MonkeyPatch) -> None:
    """include_ugc=True 时应合并动态并补充分区/账号名。"""
    tracker = _make_tracker()

    async def _official(page=1, page_size=20):
        """返回空官方列表。"""
        return []

    async def _ugc(uid, limit=10):
        """返回一条可转换的动态。"""
        return [{
            "id": "d1",
            "topic": "活动话题",
            "title": "",
            "text": "参加活动",
            "images": [],
            "pub_time": datetime(2026, 1, 1),
            "uid": uid,
            "username": "甲",
            "fetched_at": datetime(2026, 1, 2),
        }]

    async def _recorder(activities, zone="all"):
        """吞掉落库。"""
        return None

    monkeypatch.setattr(tracker, "get_official_activities", _official)
    monkeypatch.setattr(tracker, "get_ugc_account_dynamics", _ugc)
    monkeypatch.setattr(tracker, "_save_to_database", _recorder)
    monkeypatch.setattr(tracker, "_get_zone_accounts", lambda zone: [("1", "甲")])

    result = asyncio.run(tracker.fetch_all_activities(include_ugc=True, zone="paint"))

    assert result["ugc_count"] == 1
    assert result["total_count"] == 1
    activity = result["activities"][0]
    assert activity["source"] == "ugc"
    assert activity["zone"] == "paint"
    assert activity["source_username"] == "甲"
    assert result["fetched_at"]


# --------------------------------------------------------------------- 落库

@pytest.fixture()
def db_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """构造真实临时库并接管模块级 get_session。"""
    manager = DatabaseManager(str(tmp_path / "activity.db"))
    monkeypatch.setattr(tracker_module, "get_session", manager.get_session)
    return manager


def _activity(**overrides) -> dict:
    """构造一条可落库的活动字典。"""
    base = {
        "id": "1001",
        "title": "活动A",
        "link": "https://a",
        "cover": "c.jpg",
        "desc": "d",
        "zone": "game",
        "tags": ["t"],
        "start_time": None,
        "end_time": None,
        "status": "ongoing",
    }
    base.update(overrides)
    return base


def test_save_to_database_inserts_new_activity(db_env) -> None:
    """新活动应插入并带上 reward_info 附加信息。"""
    tracker = _make_tracker()

    asyncio.run(tracker._save_to_database([
        _activity(source_uid="9", source_username="UP", images=["i1"]),
    ], zone="game"))

    session = db_env.get_session()
    try:
        row = session.query(Activity).filter_by(activity_id="1001").one()
        assert row.title == "活动A"
        assert row.url == "https://a"
        assert row.category == "game"
        assert row.tags == ["t"]
        assert row.reward_info == {"images": ["i1"], "source_uid": "9", "source_username": "UP"}
    finally:
        session.close()


def test_save_to_database_upserts_existing_activity(db_env) -> None:
    """已存在活动应更新标题/状态/分区而不是重复插入。"""
    tracker = _make_tracker()
    asyncio.run(tracker._save_to_database([_activity()], zone="game"))

    asyncio.run(tracker._save_to_database([_activity(title="活动A改", status="ended", zone="anime")], zone="game"))

    session = db_env.get_session()
    try:
        rows = session.query(Activity).filter_by(activity_id="1001").all()
        assert len(rows) == 1
        assert rows[0].title == "活动A改"
        assert rows[0].status == "ended"
        assert rows[0].category == "anime"
    finally:
        session.close()


def test_save_to_database_uses_zone_when_activity_has_none(db_env) -> None:
    """活动缺少 zone 时回落到入参 zone。"""
    tracker = _make_tracker()

    asyncio.run(tracker._save_to_database([_activity(zone=None)], zone="paint"))

    session = db_env.get_session()
    try:
        assert session.query(Activity).filter_by(activity_id="1001").one().category == "paint"
    finally:
        session.close()


def test_save_to_database_survives_session_creation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_session 抛异常时不应冒泡（session 为 None 已加保护）。"""
    def _boom():
        """模拟取会话失败。"""
        raise RuntimeError("no db")

    monkeypatch.setattr(tracker_module, "get_session", _boom)
    tracker = _make_tracker()

    asyncio.run(tracker._save_to_database([_activity()]))  # 不应抛异常


def test_save_to_database_rolls_back_on_commit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """提交失败时应回滚并关闭会话，不向上抛异常。"""
    session = _CommitFailSession()
    monkeypatch.setattr(tracker_module, "get_session", lambda: session)
    tracker = _make_tracker()

    asyncio.run(tracker._save_to_database([_activity()]))

    assert session.rolled_back is True
    assert session.closed is True
