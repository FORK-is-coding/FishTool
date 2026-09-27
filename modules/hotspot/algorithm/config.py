"""热点算法的领域阈值配置。"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class HeuristicConfig:
    """启发式算法阈值，按领域分桶。"""

    p_candidate: float = 90.0
    p_appear: float = 95.0
    p_decline: float = 50.0
    base_view: int = 10_000
    window: int = 7

    def as_dict(self) -> dict[str, float | int]:
        """返回配置字典，供接口和日志使用。"""
        return asdict(self)


DOMAIN_CONFIGS: dict[str, HeuristicConfig] = {
    "game": HeuristicConfig(base_view=10_000),
    "游戏区": HeuristicConfig(base_view=10_000),
    "default": HeuristicConfig(),
}


def get_config(domain: str | None = None) -> HeuristicConfig:
    """按领域读取配置，未知领域使用默认配置。"""
    return DOMAIN_CONFIGS.get(domain or "default", DOMAIN_CONFIGS["default"])
