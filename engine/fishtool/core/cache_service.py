"""统一缓存服务：热点模块不得直接操作缓存实现。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _Entry:
    """缓存内部条目。"""

    value: Any
    expires_at: float


class CacheService:
    """进程级线程安全 TTL 缓存单例，统一校验命名空间。"""

    _instance: "CacheService | None" = None
    _instance_lock = threading.Lock()
    _allowed_prefixes = ("bili:comment:", "bili:tag:", "bili:title:", "bili:view:")

    def __new__(cls) -> "CacheService":
        """返回唯一缓存服务实例。"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._items = {}
                    cls._instance._lock = threading.RLock()
        return cls._instance

    def _validate_key(self, key: str) -> None:
        """校验缓存 key 必须属于明确业务命名空间。"""
        if not any(key.startswith(prefix) for prefix in self._allowed_prefixes):
            raise ValueError(f"缓存 key 不在允许的 bili 命名空间: {key}")

    def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        """写入带过期时间的缓存值。"""
        self._validate_key(key)
        with self._lock:
            self._items[key] = _Entry(value=value, expires_at=time.monotonic() + max(1, ttl))

    def get(self, key: str, default: Any = None) -> Any:
        """读取缓存，过期或不存在时返回默认值。"""
        self._validate_key(key)
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return default
            if entry.expires_at <= time.monotonic():
                self._items.pop(key, None)
                return default
            return entry.value

    def delete(self, key: str) -> None:
        """删除缓存值。"""
        self._validate_key(key)
        with self._lock:
            self._items.pop(key, None)


cache_service = CacheService()


def get_cache_service() -> CacheService:
    """返回统一缓存服务单例。"""
    return cache_service
