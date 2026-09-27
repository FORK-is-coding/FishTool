"""热点算法注册表。"""
from __future__ import annotations

from collections.abc import Callable

from .base import LifecycleDetector
from .heuristic_v1 import HeuristicV1

_REGISTRY: dict[str, Callable[..., LifecycleDetector]] = {"heuristic_v1": HeuristicV1}


def register(name: str, factory: Callable[..., LifecycleDetector]) -> None:
    """注册一个算法工厂，重复名称直接覆盖以支持插件热替换。"""
    if not name or not callable(factory):
        raise ValueError("算法名称和工厂函数不能为空")
    _REGISTRY[name] = factory


def create_detector(name: str = "heuristic_v1", **kwargs: object) -> LifecycleDetector:
    """按名称创建算法实例。"""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise KeyError(f"未知热点算法: {name}")
    return factory(**kwargs)


def list_algorithms() -> list[str]:
    """返回已注册算法名称。"""
    return sorted(_REGISTRY)
