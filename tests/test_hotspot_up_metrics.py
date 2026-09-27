"""UP 主轻量指标批量查询的契约级测试。

覆盖 modules/hotspot/up_metrics.py：
- _fmt_int：None/字符串/浮点/非法值的兜底
- _load_one：relation / guard / charge 三个字段的独立降级
- get_up_light_metrics：去重排序、TTL 缓存命中与过期、并发加载异常隔离

网络层使用真实实现 __init__ 的契约级假对象（非 AsyncMock），
缓存与时间通过 monkeypatch 注入，绝不访问真实 data/ 库。
"""

from __future__ import annotations

import asyncio

import pytest

from modules.hotspot import up_metrics


# --------------------------------------------------------------------- 假对象

class FakeUpAPI:
    """逐接口可注入返回或异常的 UP 指标假 API。"""

    def __init__(self, *, relation=None, guard=None, charge=None, room=None) -> None:
        self._relation = relation
        self._guard = guard
        self._charge = charge
        self._room = room
        self.calls = {"relation": [], "guard": [], "charge": [], "room": []}

    @staticmethod
    def _resolve(value, *args):
        """把异常实例、可调用对象或普通值统一解析为返回值。"""
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(*args)
        return value

    async def get_user_relation_stat(self, mid):
        """返回粉丝数接口的假响应。"""
        self.calls["relation"].append(mid)
        return self._resolve(self._relation, mid)

    async def get_guard_top_list(self, room_id, mid):
        """返回大航海上榜接口的假响应。"""
        self.calls["guard"].append((room_id, mid))
        return self._resolve(self._guard, room_id, mid)

    async def get_charge_count(self, mid):
        """返回充电人数接口的假响应。"""
        self.calls["charge"].append(mid)
        return self._resolve(self._charge, mid)

    async def get_room_base_info(self, mids):
        """返回直播间基础信息的批量假响应。"""
        self.calls["room"].append(list(mids))
        return self._resolve(self._room, mids)


class FakeClock:
    """可控时钟，用于驱动 TTL 过期分支。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def time(self) -> float:
        """返回当前受控时间戳。"""
        return self.now


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch: pytest.MonkeyPatch):
    """每个用例使用独立的模块级缓存，避免相互污染。"""
    monkeypatch.setattr(up_metrics, "_cache", {}, raising=True)


def _happy_api() -> FakeUpAPI:
    """构造四个接口全部成功的假 API。"""
    return FakeUpAPI(
        relation={"data": {"follower": 1234}},
        room={"data": {"7": {"room_id": 555, "live_status": 1}}},
        guard={"data": {"info": {"num": 8}}},
        charge={"charge_count": 9, "source": "api"},
    )


# --------------------------------------------------------------------- _fmt_int

@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, 0, 0),
        (None, None, None),
        ("12", 0, 12),
        ("abc", 0, 0),
        ("abc", None, None),
        (3.9, 0, 3),
        (True, 0, 1),
        (0, 5, 0),
    ],
)
def test_fmt_int_boundaries(value, default, expected) -> None:
    """_fmt_int 应覆盖 None、合法、非法与浮点截断。"""
    assert up_metrics._fmt_int(value, default) == expected


def test_fmt_int_uses_default_when_argument_omitted() -> None:
    """未显式传 default 时非法值回落 0。"""
    assert up_metrics._fmt_int(None) == 0
    assert up_metrics._fmt_int("nope") == 0


# --------------------------------------------------------------------- _load_one

def test_load_one_maps_all_fields() -> None:
    """三个接口正常时字段应逐项落到结果字典。"""
    api = _happy_api()
    room_map = {"7": {"room_id": 555, "live_status": 1}}

    result = asyncio.run(up_metrics._load_one(api, 7, room_map))

    assert result == {
        "mid": 7,
        "follower": 1234,
        "live_status": 1,
        "guard_count": 8,
        "charge_count": 9,
        "charge_source": "api",
    }


def test_load_one_without_room_marks_guard_unavailable() -> None:
    """无直播间时明确置 guard_count 为 None。"""
    api = FakeUpAPI(relation={"data": {"follower": 5}}, charge={"charge_count": 0})

    result = asyncio.run(up_metrics._load_one(api, 7, {}))

    assert result["guard_count"] is None
    assert result["live_status"] == 0
    assert api.calls["guard"] == []


def test_load_one_with_zero_room_id_skips_guard_call() -> None:
    """room_id 为 0 时不应请求大航海接口。"""
    api = FakeUpAPI(relation={"data": {"follower": 5}}, room={"data": {}}, charge={})

    result = asyncio.run(up_metrics._load_one(api, 7, {"7": {"room_id": 0}}))

    assert result["guard_count"] is None
    assert api.calls["guard"] == []


def test_load_one_degrades_relation_failure() -> None:
    """粉丝接口失败只让 follower 为 None，不阻塞其它字段。"""
    api = _happy_api()
    api._relation = RuntimeError("relation down")

    result = asyncio.run(up_metrics._load_one(api, 7, {"7": {"room_id": 555, "live_status": 2}}))

    assert result["follower"] is None
    assert result["guard_count"] == 8
    assert result["charge_count"] == 9


def test_load_one_degrades_guard_failure() -> None:
    """大航海接口失败只让 guard_count 为 None。"""
    api = _happy_api()
    api._guard = RuntimeError("guard down")

    result = asyncio.run(up_metrics._load_one(api, 7, {"7": {"room_id": 555}}))

    assert result["guard_count"] is None
    assert result["follower"] == 1234


def test_load_one_degrades_charge_failure() -> None:
    """充电接口失败时数量为 None 且来源标记 unavailable。"""
    api = _happy_api()
    api._charge = RuntimeError("charge down")

    result = asyncio.run(up_metrics._load_one(api, 7, {"7": {"room_id": 555}}))

    assert result["charge_count"] is None
    assert result["charge_source"] == "unavailable"


def test_load_one_missing_metric_fields_default_to_zero() -> None:
    """接口返回结构存在但字段缺失时按 0 兜底。"""
    api = FakeUpAPI(
        relation={"data": {}},
        room={"data": {"7": {"room_id": 555}}},
        guard={"data": {"info": {}}},
        charge={},
    )

    result = asyncio.run(up_metrics._load_one(api, 7, {"7": {"room_id": 555}}))

    assert result["follower"] == 0
    assert result["guard_count"] == 0
    assert result["charge_count"] == 0
    assert result["charge_source"] == "unavailable"


# --------------------------------------------------------------------- 批量入口

def test_get_up_light_metrics_empty_returns_empty() -> None:
    """空 mid 列表应直接返回空字典。"""
    api = _happy_api()

    assert asyncio.run(up_metrics.get_up_light_metrics(api, [])) == {}
    assert asyncio.run(up_metrics.get_up_light_metrics(api, [0, None])) == {}


def test_get_up_light_metrics_dedups_and_drops_falsy() -> None:
    """重复与假值 mid 应先去重排序再请求。"""
    api = _happy_api()

    result = asyncio.run(up_metrics.get_up_light_metrics(api, [7, 7, 3, 0]))

    assert set(result) == {3, 7}
    assert api.calls["room"][0] == [3, 7]


def test_get_up_light_metrics_happy_path() -> None:
    """批量正常路径应返回逐 mid 字典。"""
    api = _happy_api()

    result = asyncio.run(up_metrics.get_up_light_metrics(api, [7]))

    assert result[7]["follower"] == 1234
    assert result[7]["guard_count"] == 8
    assert result[7]["charge_count"] == 9


def test_get_up_light_metrics_survives_room_batch_failure() -> None:
    """直播间批量接口失败应降级为空映射，其它字段仍可用。"""
    api = _happy_api()
    api._room = RuntimeError("room down")

    result = asyncio.run(up_metrics.get_up_light_metrics(api, [7]))

    assert result[7]["guard_count"] is None
    assert result[7]["live_status"] == 0
    assert result[7]["follower"] == 1234


def test_get_up_light_metrics_uses_cache_on_second_call() -> None:
    """TTL 内二次调用应命中缓存，不再发起任何接口请求。"""
    api = _happy_api()

    first = asyncio.run(up_metrics.get_up_light_metrics(api, [7]))
    calls_after_first = {key: list(value) for key, value in api.calls.items()}
    second = asyncio.run(up_metrics.get_up_light_metrics(api, [7]))

    assert second == first
    assert api.calls == calls_after_first


def test_get_up_light_metrics_refreshes_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存过期后应重新加载并覆盖旧值。"""
    clock = FakeClock()
    monkeypatch.setattr(up_metrics, "time", clock)
    api = _happy_api()

    asyncio.run(up_metrics.get_up_light_metrics(api, [7]))
    clock.now += up_metrics._TTL_SECONDS + 1
    api._charge = {"charge_count": 42, "source": "api"}
    result = asyncio.run(up_metrics.get_up_light_metrics(api, [7]))

    assert result[7]["charge_count"] == 42
    assert len(api.calls["charge"]) == 2


def test_get_up_light_metrics_serves_fresh_and_cached_mix() -> None:
    """部分命中缓存、部分需加载时应合并返回。"""
    api = _happy_api()
    asyncio.run(up_metrics.get_up_light_metrics(api, [3]))

    result = asyncio.run(up_metrics.get_up_light_metrics(api, [3, 7]))

    assert set(result) == {3, 7}
    assert api.calls["room"][-1] == [7]


def test_get_up_light_metrics_skips_task_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """单个加载任务抛异常时应被隔离，不影响其它 mid。"""
    api = _happy_api()

    async def _boom(api_arg, mid, room_map):
        """模拟加载协程整体失败。"""
        raise RuntimeError("load boom")

    monkeypatch.setattr(up_metrics, "_load_one", _boom)

    result = asyncio.run(up_metrics.get_up_light_metrics(api, [7, 3]))

    assert result == {}
