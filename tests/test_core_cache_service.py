"""core.cache_service 底座测试（第1批补齐 · core 段）。

覆盖范围：
- CacheService.__new__（进程级单例）
- CacheService._validate_key（命名空间校验）
- CacheService.set / get / delete
- 模块级 get_cache_service 单例

测试策略：
- 使用真实 CacheService 单例，键名带独立前缀，避免跨用例污染。
- TTL 过期不靠真实 sleep：把模块内的 ``time`` 换成可控时钟替身，
  同时驱动 set/get 两侧的时间判断，验证"到点即过期"的真实逻辑。
"""
from __future__ import annotations

import importlib

import pytest


cache_module = importlib.import_module("core.cache_service")
CacheService = cache_module.CacheService
get_cache_service = cache_module.get_cache_service


class _FakeClock:
    """可控单调时钟替身：monotonic() 返回可手动推进的浮点数。"""

    def __init__(self, now: float = 1000.0) -> None:
        """记录起始时间并初始化推进接口。"""
        self.now = now

    def monotonic(self) -> float:
        """返回当前时间，不真正走时钟。"""
        return self.now


# ---------------------------------------------------------------------------
# 单例与命名空间校验
# ---------------------------------------------------------------------------


def test_cache_service_is_process_singleton():
    """CacheService() 与 get_cache_service() 必须返回同一实例。"""
    assert CacheService() is CacheService()
    assert get_cache_service() is CacheService()
    assert get_cache_service() is cache_module.cache_service


def test_validate_key_rejects_foreign_namespace():
    """非 bili 命名空间的 key 必须抛 ValueError。"""
    service = CacheService()
    with pytest.raises(ValueError) as excinfo:
        service.set("other:key", 1)
    assert "命名空间" in str(excinfo.value)

    with pytest.raises(ValueError):
        service.get("other:key")
    with pytest.raises(ValueError):
        service.delete("other:key")


def test_all_allowed_prefixes_are_accepted():
    """四个业务前缀都应被识别为合法命名空间。"""
    service = CacheService()
    for prefix in ("bili:comment:", "bili:tag:", "bili:title:", "bili:view:"):
        key = f"{prefix}prefix-probe"
        service.set(key, prefix)
        assert service.get(key) == prefix
        service.delete(key)


# ---------------------------------------------------------------------------
# set / get / delete 基本行为
# ---------------------------------------------------------------------------


def test_set_get_roundtrip_and_default():
    """写入后可读回原对象；缺失键返回自定义默认值。"""
    service = CacheService()
    payload = {"nested": [1, 2, 3]}
    service.set("bili:comment:roundtrip", payload)
    assert service.get("bili:comment:roundtrip") is payload
    assert service.get("bili:comment:never-set") is None
    assert service.get("bili:comment:never-set", "fallback") == "fallback"


def test_delete_removes_value_and_is_idempotent():
    """delete 后读取回默认值，删除不存在的键不报错。"""
    service = CacheService()
    service.set("bili:title:to-delete", "v")
    service.delete("bili:title:to-delete")
    assert service.get("bili:title:to-delete") is None
    # 再删一次不应抛异常（pop 带默认值）
    service.delete("bili:title:to-delete")


# ---------------------------------------------------------------------------
# TTL 过期
# ---------------------------------------------------------------------------


def test_ttl_expires_and_pops_entry(monkeypatch):
    """TTL 到期后读取应返回默认值，并顺带清理条目。"""
    clock = _FakeClock(1000.0)
    monkeypatch.setattr(cache_module, "time", clock)
    service = CacheService()

    service.set("bili:view:ttl", "v", ttl=10)  # 过期时间 = 1010
    assert service.get("bili:view:ttl") == "v"

    clock.now = 1009.0  # 未到期
    assert service.get("bili:view:ttl") == "v"

    clock.now = 1011.0  # 已到期
    assert service.get("bili:view:ttl") is None
    # 到期条目已被弹出，内部表不再持有该 key
    assert "bili:view:ttl" not in service._items


def test_ttl_lower_bound_is_one_second(monkeypatch):
    """ttl<=0 时按 1 秒下限处理，避免写入即过期。"""
    clock = _FakeClock(2000.0)
    monkeypatch.setattr(cache_module, "time", clock)
    service = CacheService()

    service.set("bili:tag:min-ttl", "v", ttl=0)  # 过期时间 = 2001
    clock.now = 2000.5
    assert service.get("bili:tag:min-ttl") == "v"
    clock.now = 2001.0
    assert service.get("bili:tag:min-ttl") is None
