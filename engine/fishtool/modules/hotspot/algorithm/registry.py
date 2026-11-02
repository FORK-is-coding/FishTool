"""热点算法注册表。"""
from __future__ import annotations

from collections.abc import Callable

from .base import LifecycleDetector
from .heuristic_v1 import HeuristicV1
from .lifecycle_v2 import LifecycleV2

#: 已注册算法：``heuristic_v1``（对照回放）与 ``lifecycle_v2``（接口 / 门面默认）。
#: 注意 ``create_detector`` 的**函数默认值仍为 heuristic_v1**：本字典只负责「名字 -> 工厂」
#: 登记，不承担默认口径；默认口径由路由 Query 与 :class:`HotspotService` 显式给出。
_REGISTRY: dict[str, Callable[..., LifecycleDetector]] = {
    "heuristic_v1": HeuristicV1,
    "lifecycle_v2": LifecycleV2,
}


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
