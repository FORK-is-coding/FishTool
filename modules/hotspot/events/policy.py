"""FishTool 04 · 第三批 e：事件策略（``EventPolicy``）载入 + ``policy_version`` 规范化 hash。

依据：``FishTool_04_话题级热点研判与选题决策_02补充执行案(1).md`` §10.2 / §10.5 末段
（阈值一律经 ``EventPolicy`` 载入；``safety_margin_s``、``max_experiment_hours`` 上限、
死线可信度门槛必须配置化并输出 ``policy_version``）与 3e 执行规格 §1.7。

红线遵守：

- **``events/config.py`` 一行不动**（3d 已落地的 ``DailyPolicy`` / ``EarlyPolicy`` 保持原样）；
- 本批策略默认值集中放本文件，读取现有 ``ConfigManager`` 的 ``hotspot.events`` 段；
- ``policy_version`` 对**规范化实际配置**求 hash；
- **存在不支持字段 / 非法阈值 → 启动时显式报配置错误**，
  **绝不在运行中默默改用另一组默认值**。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .brief import DEFAULT_DURATION_MAX_HOURS, DEFAULT_MAX_EXPERIMENT_HOURS_LIMIT

#: 策略 schema 版本；进入 ``policy_version`` hash，schema 变更即版本变更。
POLICY_SCHEMA_VERSION: str = "events_policy_v1"

#: 默认安全余量（秒）——加到制作 ETA 上再与活动截止比较。
DEFAULT_SAFETY_MARGIN_S: int = 7200
#: 默认死线可信度门槛（低于此值的截止不当作“已验证截止”）。
DEFAULT_DEADLINE_CONFIDENCE_MIN: float = 0.6
#: daily 新鲜度门（秒），与 3d 口径一致（36h）。
DEFAULT_DAILY_STALENESS_MAX_S: int = 36 * 3600
#: early 新鲜度门（秒），与 3d 口径一致（2h）。
DEFAULT_EARLY_STALENESS_MAX_S: int = 2 * 3600

#: 真正构成 :class:`EventPolicy` 的策略字段（进入构造与 ``policy_version``）。
_POLICY_FIELD_NAMES: tuple[str, ...] = (
    "safety_margin_s",
    "deadline_confidence_min",
    "max_experiment_hours_limit",
    "duration_max_hours",
    "daily_staleness_max_s",
    "early_staleness_max_s",
)

#: ``hotspot.events`` 段允许出现的键集合——**白名单之外一律报配置错误**。
#:
#: 第三批 g §0：生成账本（第三批 f）把 ``generation_lease_seconds`` /
#: ``generation_deadline_seconds`` 放进了同一 ``hotspot.events`` 段。为让本类能读取
#: 该段而不炸，把这两键**扩进白名单**；但它们只做放行 + 形状校验，
#: **不并入策略语义、不进入 ``policy_version``**（由 ``topic_generation_service`` 消费）。
#: 白名单之外的未知键仍然照旧报错，严格性不放松。
ALLOWED_POLICY_FIELDS: frozenset[str] = frozenset(_POLICY_FIELD_NAMES) | frozenset(
    {
        "generation_lease_seconds",
        "generation_deadline_seconds",
        # 第四批 b 缺口③：discovery 账本三项。真实 ``config.yaml`` 的 ``hotspot.events``
        # 段同时含此三键；同样只放行 + 形状校验，**不并入策略语义、不进入 policy_version**
        # （由 discovery 子系统消费）。不加白名单则对真实 config 调 ``from_config`` 会抛
        # ``UnsupportedPolicyField``。
        "discovery_lease_seconds",
        "discovery_deadline_seconds",
        "manual_discovery_cooldown_seconds",
    }
)

#: 各类阈值的取值上界（秒 / 小时），防止把“配置错误”当合法值。
_MAX_SECONDS: int = 30 * 24 * 3600
_MAX_HOURS: float = 24 * 366.0


class PolicyConfigError(ValueError):
    """策略配置错误基类（消息即稳定错误码）。"""


class UnsupportedPolicyField(PolicyConfigError):
    """配置出现不支持的字段（不允许静默忽略）。"""


class InvalidPolicyValue(PolicyConfigError):
    """配置阈值非法（类型 / 范围），不允许运行中静默降级。"""


def _is_real_number(value: Any) -> bool:
    """是否为“真数值”（排除 ``bool``）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_field(name: str, value: Any) -> Any:
    """校验单个策略字段的类型与范围。

    Args:
        name: 字段名。
        value: 原始取值。

    Returns:
        Any: 归一化后的合法取值。

    Raises:
        InvalidPolicyValue: 字段类型或范围非法。
    """
    if name in ("safety_margin_s", "daily_staleness_max_s", "early_staleness_max_s"):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > _MAX_SECONDS:
            raise InvalidPolicyValue(f"invalid_policy_value:{name}")
        return int(value)
    if name == "deadline_confidence_min":
        if not _is_real_number(value) or not (0.0 < float(value) <= 1.0):
            raise InvalidPolicyValue(f"invalid_policy_value:{name}")
        return float(value)
    if name in ("max_experiment_hours_limit", "duration_max_hours"):
        if not _is_real_number(value) or float(value) <= 0 or float(value) > _MAX_HOURS:
            raise InvalidPolicyValue(f"invalid_policy_value:{name}")
        return float(value)
    if name in (
        "generation_lease_seconds",
        "generation_deadline_seconds",
        "discovery_lease_seconds",
        "discovery_deadline_seconds",
        "manual_discovery_cooldown_seconds",
    ):
        # 生成账本 / 发现账本键与策略同段：只放行 + 形状校验（正整数秒），
        # **不并入策略对象、不进入 policy_version**（由各自子系统消费）。
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > _MAX_SECONDS:
            raise InvalidPolicyValue(f"invalid_policy_value:{name}")
        return int(value)
    raise UnsupportedPolicyField(f"unsupported_policy_field:{name}")


@dataclass(frozen=True)
class EventPolicy:
    """事件机会策略（阈值集中配置化，输出可复算的 ``policy_version``）。

    Attributes:
        safety_margin_s: 制作 ETA 之外的安全余量（秒）。
        deadline_confidence_min: 死线可信度门槛；低于此值不当作“已验证截止”。
        max_experiment_hours_limit: ``max_experiment_hours`` 字段的合理上限（小时）。
        duration_max_hours: 单个时长字段的合理上限（小时）。
        daily_staleness_max_s: daily 证据新鲜度门（秒）。
        early_staleness_max_s: early 证据新鲜度门（秒）。
    """

    safety_margin_s: int = DEFAULT_SAFETY_MARGIN_S
    deadline_confidence_min: float = DEFAULT_DEADLINE_CONFIDENCE_MIN
    max_experiment_hours_limit: float = DEFAULT_MAX_EXPERIMENT_HOURS_LIMIT
    duration_max_hours: float = DEFAULT_DURATION_MAX_HOURS
    daily_staleness_max_s: int = DEFAULT_DAILY_STALENESS_MAX_S
    early_staleness_max_s: int = DEFAULT_EARLY_STALENESS_MAX_S

    # ------------------------------------------------------------------ 派生量

    def as_dict(self) -> dict[str, Any]:
        """导出为可序列化字典（供 hash / 日志 / 回放消费）。"""
        return asdict(self)

    @property
    def policy_version(self) -> str:
        """对规范化实际配置求 hash（``evp1-`` + 24 位 hex，长度 29 < 列宽 32）。"""
        payload = {"schema": POLICY_SCHEMA_VERSION, **self.as_dict()}
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return "evp1-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]

    # ------------------------------------------------------------------ 构造

    @classmethod
    def build(cls, **overrides: Any) -> "EventPolicy":
        """由显式覆盖项构造策略；**未知字段 / 非法阈值直接报错**。

        Args:
            **overrides: 字段覆盖（键必须落在 :data:`ALLOWED_POLICY_FIELDS`）。

        Returns:
            EventPolicy: 校验通过的不可变策略。

        Raises:
            UnsupportedPolicyField: 出现不支持字段。
            InvalidPolicyValue: 阈值非法或字段间关系非法。
        """
        unknown = set(overrides) - ALLOWED_POLICY_FIELDS
        if unknown:
            raise UnsupportedPolicyField("unsupported_policy_field:" + ",".join(sorted(unknown)))
        defaults = asdict(cls())
        merged = {**defaults, **overrides}
        # 白名单内的键都要过形状校验；但只有真正的策略字段进入 EventPolicy 对象，
        # 同段承载的其他子系统键（生成账本两键）校验后即丢弃，绝不并入策略 / policy_version。
        validated = {
            name: _validate_field(name, merged[name])
            for name in ALLOWED_POLICY_FIELDS
            if name in merged
        }
        policy = cls(**{name: validated[name] for name in _POLICY_FIELD_NAMES})
        if policy.max_experiment_hours_limit > policy.duration_max_hours:
            raise InvalidPolicyValue("invalid_policy_relation:max_experiment_hours_limit>duration_max_hours")
        return policy

    @classmethod
    def from_config(
        cls,
        config_manager: Any = None,
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> "EventPolicy":
        """从现有 ``ConfigManager`` 的 ``hotspot.events`` 段载入策略。

        Args:
            config_manager: 既有配置管理器（缺省则只用默认值 + 覆盖项）。
            overrides: 显式覆盖（测试 / 调用方传入），优先级高于配置文件。

        Returns:
            EventPolicy: 校验通过的策略。

        Raises:
            InvalidPolicyValue: ``hotspot.events`` 段不是映射。
            UnsupportedPolicyField: 配置或覆盖含不支持字段。
            InvalidPolicyValue: 阈值非法。
        """
        raw: Any = None
        if config_manager is not None:
            raw = config_manager.get("hotspot.events", None)
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise InvalidPolicyValue("hotspot_events_section_must_be_mapping")
        merged: dict[str, Any] = {str(k): v for k, v in raw.items()}
        if overrides:
            for key, value in overrides.items():
                merged[str(key)] = value
        return cls.build(**merged)


__all__ = [
    "POLICY_SCHEMA_VERSION",
    "DEFAULT_SAFETY_MARGIN_S",
    "DEFAULT_DEADLINE_CONFIDENCE_MIN",
    "DEFAULT_DAILY_STALENESS_MAX_S",
    "DEFAULT_EARLY_STALENESS_MAX_S",
    "ALLOWED_POLICY_FIELDS",
    "PolicyConfigError",
    "UnsupportedPolicyField",
    "InvalidPolicyValue",
    "EventPolicy",
]
