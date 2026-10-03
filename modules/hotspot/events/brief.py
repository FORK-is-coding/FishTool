"""FishTool 04 · 第三批 e：CreatorBrief 契约、校验与显式条件匹配。

依据：``FishTool_04_话题级热点研判与选题决策_02补充执行案(1).md`` §10.1（L891-913）
与 3e 执行规格 §1.1。

核心口径（逐条钉死）：

- **以用户显式输入为主**：本模块只做「如实记录 + 校验 + 显式条件匹配」，
  **绝不依据几条热门内容猜用户定位**（不解析、不写入任何‘智能推断’字段）；
- 所有时长字段 **非负且带合理上限**，**禁止负制作时间绕过截止**；
- 账号匹配只说明 **显式条件匹配**（``explicit_match > partial_match > unspecified``），
  **不推断算法会推荐给谁**；
- ``brief_version`` 必填；OpportunityRun 内保存的 brief 是**不可变输入**
  （本模块对象为 ``frozen``，构造后不可原地改写）；
- 空值语义分开：**字段缺省（``None``） ≠ 用户明确“不限”（显式空元组）**；
- ``risk_preferences`` 第一版 **只使用** 显式排除清单 / 禁止类型与投入上限，
  **不假装已实现法律风险评分或事实审查**。

本模块是纯数据 + 纯函数：不触库、不触网、不看时钟。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: 单个时长字段的默认合理上限（小时）；超过即视为异常输入，**拒绝而非截断**。
DEFAULT_DURATION_MAX_HOURS: float = 720.0
#: ``max_experiment_hours``（试做投入上限）的默认上限（小时）。
DEFAULT_MAX_EXPERIMENT_HOURS_LIMIT: float = 168.0

#: 时长字段（全部非负且带合理上限）。
DURATION_FIELDS: tuple[str, ...] = (
    "production_hours",
    "review_hours",
    "publish_buffer_hours",
    "max_experiment_hours",
)

#: 列表型字段（``None`` = 缺省未声明；空元组 = 用户明确“不限”）。
LIST_FIELDS: tuple[str, ...] = (
    "primary_entities",
    "allowed_entities",
    "content_domains",
    "supported_formats",
    "preferred_angles",
    "excluded_entities",
    "excluded_topics",
    "available_assets",
)

#: 允许出现的全部字段——**不允许自行新增“智能推断”字段**。
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {"creator_uid", "brief_version", "risk_preferences", *DURATION_FIELDS, *LIST_FIELDS}
)

#: ``risk_preferences`` 第一版只认「显式排除类型 / 投入上限」，其余键一律报错。
ALLOWED_RISK_KEYS: frozenset[str] = frozenset({"excluded_types", "max_spend", "max_hours"})

#: 账号匹配三档（`explicit_match > partial_match > unspecified`）。
MATCH_EXPLICIT: str = "explicit_match"
MATCH_PARTIAL: str = "partial_match"
MATCH_UNSPECIFIED: str = "unspecified"

#: 匹配档位 -> 排序权重（越小越优先）。
MATCH_ORDER: dict[str, int] = {MATCH_EXPLICIT: 0, MATCH_PARTIAL: 1, MATCH_UNSPECIFIED: 2}
_MATCH_BY_RANK: dict[int, str] = {0: MATCH_EXPLICIT, 1: MATCH_PARTIAL, 2: MATCH_UNSPECIFIED}


class BriefValidationError(ValueError):
    """CreatorBrief 校验失败。

    消息即稳定错误码（形如 ``negative_duration:production_hours``），便于调用方断言，
    也便于日志检索。**绝不静默纠正为默认值**。
    """


def _norm_token(value: Any) -> str:
    """把一个实体 / 领域 / 话题 token 归一化（小写 + 去首尾空白）。

    Args:
        value: 原始 token（任何类型，统一转字符串）。

    Returns:
        str: 归一化后的 token。
    """
    return str(value).strip().lower()


def _norm_set(values: Any) -> set[str]:
    """把可迭代 token 归一为集合（``None`` / 不可迭代 → 空集合）。

    Args:
        values: 可迭代 token；也接受单个字符串。

    Returns:
        set[str]: 归一化 token 集合。
    """
    if values is None:
        return set()
    if isinstance(values, str):
        return {_norm_token(values)}
    try:
        return {_norm_token(v) for v in values}
    except TypeError:
        return set()


def _norm_sequence(value: Any, *, field_name: str) -> tuple[str, ...] | None:
    """把列表型字段归一为去重有序元组；``None`` 原样保留（表示“缺省”）。

    Args:
        value: 原始取值（``None`` / 字符串 / 可迭代）。
        field_name: 字段名（用于错误码）。

    Returns:
        tuple[str, ...] | None: 归一化元组；输入为 ``None`` 时返回 ``None``。

    Raises:
        BriefValidationError: 取值类型非法。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return (_norm_token(value),)
    if isinstance(value, (list, tuple, set, frozenset)):
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            token = _norm_token(item)
            if token and token not in seen:
                seen.add(token)
                out.append(token)
        return tuple(out)
    raise BriefValidationError(f"invalid_list_field:{field_name}")


def _validate_hours(value: Any, *, name: str, limit: float) -> float:
    """校验单个时长字段：非负、有限、不超过合理上限。

    Args:
        value: 原始取值。
        name: 字段名（用于错误码）。
        limit: 合理上限（小时）。

    Returns:
        float: 合法时长（小时）。

    Raises:
        BriefValidationError: 类型非法 / 负值 / 超限 / 非有限。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BriefValidationError(f"invalid_duration:{name}")
    number = float(value)
    if not math.isfinite(number):
        raise BriefValidationError(f"invalid_duration:{name}")
    if number < 0:
        raise BriefValidationError(f"negative_duration:{name}")
    if number > float(limit):
        raise BriefValidationError(f"duration_above_limit:{name}")
    return number


def _normalize_risk_preferences(value: Any) -> dict[str, Any]:
    """校验并归一 ``risk_preferences``（只认显式排除类型与投入上限）。

    Args:
        value: 原始取值（``None`` / 映射）。

    Returns:
        dict: 归一化后的风险偏好（可能为空）。

    Raises:
        BriefValidationError: 非映射 / 含不支持键 / 数值非法。
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise BriefValidationError("risk_preferences_must_be_mapping")
    unknown = {str(k) for k in value} - ALLOWED_RISK_KEYS
    if unknown:
        raise BriefValidationError("unsupported_risk_preference:" + ",".join(sorted(unknown)))
    normalized: dict[str, Any] = {}
    if "excluded_types" in value:
        normalized["excluded_types"] = list(_norm_sequence(value["excluded_types"], field_name="excluded_types") or ())
    for cap_key in ("max_spend", "max_hours"):
        if cap_key in value and value[cap_key] is not None:
            raw = value[cap_key]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)) or float(raw) < 0:
                raise BriefValidationError(f"invalid_risk_cap:{cap_key}")
            normalized[cap_key] = float(raw)
    return normalized


@dataclass(frozen=True)
class CreatorBrief:
    """创作者简报（**不可变输入**）。

    Attributes:
        brief_version: 版本号（必填，非空字符串）。
        production_hours / review_hours / publish_buffer_hours: 制作 / 审核 / 发布缓冲（小时）。
        max_experiment_hours: 允许的试做投入上限（小时）。
        creator_uid: 创作者账号（可选）。
        primary_entities / allowed_entities: 主攻 / 允许实体（``None`` = 未声明）。
        content_domains: 内容领域（``None`` = 未声明）。
        supported_formats: 用户声明的可做内容形式。
        preferred_angles: 用户声明的偏好角度。
        excluded_entities / excluded_topics: 显式排除实体 / 话题。
        available_assets: 已有账号、录屏、素材或采访能力。
        risk_preferences: 第一版只含显式排除类型与投入上限。
    """

    brief_version: str
    production_hours: float = 0.0
    review_hours: float = 0.0
    publish_buffer_hours: float = 0.0
    max_experiment_hours: float = 0.0
    creator_uid: str | None = None
    primary_entities: tuple[str, ...] | None = None
    allowed_entities: tuple[str, ...] | None = None
    content_domains: tuple[str, ...] | None = None
    supported_formats: tuple[str, ...] | None = None
    preferred_angles: tuple[str, ...] | None = None
    excluded_entities: tuple[str, ...] | None = None
    excluded_topics: tuple[str, ...] | None = None
    available_assets: tuple[str, ...] | None = None
    risk_preferences: Mapping[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 派生量

    @property
    def total_effort_hours(self) -> float:
        """制作 + 审核 + 发布缓冲的总投入（小时）。"""
        return float(self.production_hours) + float(self.review_hours) + float(self.publish_buffer_hours)

    @property
    def available_asset_set(self) -> set[str]:
        """``available_assets`` 的归一集合（缺省 → 空集合）。"""
        return set(self.available_assets or ())

    @property
    def supported_format_set(self) -> set[str]:
        """``supported_formats`` 的归一集合（缺省 → 空集合）。"""
        return set(self.supported_formats or ())

    @property
    def excluded_set(self) -> set[str]:
        """显式排除实体 + 话题的归一集合。"""
        return set(self.excluded_entities or ()) | set(self.excluded_topics or ())

    @property
    def allowed_entity_set(self) -> set[str]:
        """``allowed_entities`` + ``primary_entities`` 的归一集合。"""
        return set(self.allowed_entities or ()) | set(self.primary_entities or ())

    @property
    def excluded_content_types(self) -> set[str]:
        """``risk_preferences.excluded_types`` 的归一集合。"""
        raw = self.risk_preferences.get("excluded_types") if isinstance(self.risk_preferences, Mapping) else None
        return _norm_set(raw)

    # ------------------------------------------------------------------ 序列化

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化字典（``None`` 保留为“缺省”，不伪造空列表）。"""
        return {
            "brief_version": self.brief_version,
            "creator_uid": self.creator_uid,
            "production_hours": float(self.production_hours),
            "review_hours": float(self.review_hours),
            "publish_buffer_hours": float(self.publish_buffer_hours),
            "max_experiment_hours": float(self.max_experiment_hours),
            "primary_entities": list(self.primary_entities) if self.primary_entities is not None else None,
            "allowed_entities": list(self.allowed_entities) if self.allowed_entities is not None else None,
            "content_domains": list(self.content_domains) if self.content_domains is not None else None,
            "supported_formats": list(self.supported_formats) if self.supported_formats is not None else None,
            "preferred_angles": list(self.preferred_angles) if self.preferred_angles is not None else None,
            "excluded_entities": list(self.excluded_entities) if self.excluded_entities is not None else None,
            "excluded_topics": list(self.excluded_topics) if self.excluded_topics is not None else None,
            "available_assets": list(self.available_assets) if self.available_assets is not None else None,
            "risk_preferences": dict(self.risk_preferences or {}),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        duration_max_hours: float = DEFAULT_DURATION_MAX_HOURS,
        max_experiment_hours_limit: float = DEFAULT_MAX_EXPERIMENT_HOURS_LIMIT,
    ) -> "CreatorBrief":
        """从用户显式输入构造并**严格校验**一个 brief。

        Args:
            payload: 用户提交的字段映射（**只认** :data:`ALLOWED_FIELDS` 内的键）。
            duration_max_hours: 单时长字段上限（小时）。
            max_experiment_hours_limit: 试做投入上限字段的上限（小时）。

        Returns:
            CreatorBrief: 校验通过的不可变 brief。

        Raises:
            BriefValidationError: 含不支持字段 / 必填缺失 / 时长非法 / 列表字段非法 /
                风险偏好含不支持键。
        """
        if not isinstance(payload, Mapping):
            raise BriefValidationError("brief_payload_must_be_mapping")
        unknown = {str(k) for k in payload} - ALLOWED_FIELDS
        if unknown:
            raise BriefValidationError("unsupported_brief_field:" + ",".join(sorted(unknown)))

        raw_version = payload.get("brief_version")
        if not isinstance(raw_version, str) or not raw_version.strip():
            raise BriefValidationError("brief_version_required")

        creator_uid = payload.get("creator_uid")
        if creator_uid is not None and not isinstance(creator_uid, str):
            raise BriefValidationError("invalid_creator_uid")

        # 四个时长字段必须显式给出：缺省不能静默当 0（否则等于用 0 绕过截止）。
        hours: dict[str, float] = {}
        for name in DURATION_FIELDS:
            if name not in payload:
                raise BriefValidationError(f"missing_duration:{name}")
            limit = max_experiment_hours_limit if name == "max_experiment_hours" else duration_max_hours
            hours[name] = _validate_hours(payload[name], name=name, limit=limit)

        list_values: dict[str, tuple[str, ...] | None] = {}
        for name in LIST_FIELDS:
            list_values[name] = _norm_sequence(payload.get(name), field_name=name)

        return cls(
            brief_version=raw_version.strip(),
            creator_uid=creator_uid,
            production_hours=hours["production_hours"],
            review_hours=hours["review_hours"],
            publish_buffer_hours=hours["publish_buffer_hours"],
            max_experiment_hours=hours["max_experiment_hours"],
            primary_entities=list_values["primary_entities"],
            allowed_entities=list_values["allowed_entities"],
            content_domains=list_values["content_domains"],
            supported_formats=list_values["supported_formats"],
            preferred_angles=list_values["preferred_angles"],
            excluded_entities=list_values["excluded_entities"],
            excluded_topics=list_values["excluded_topics"],
            available_assets=list_values["available_assets"],
            risk_preferences=_normalize_risk_preferences(payload.get("risk_preferences")),
        )

    def validate(
        self,
        *,
        duration_max_hours: float = DEFAULT_DURATION_MAX_HOURS,
        max_experiment_hours_limit: float = DEFAULT_MAX_EXPERIMENT_HOURS_LIMIT,
    ) -> None:
        """对一个已构造实例做同样的时长/版本校验（防止绕过 ``from_dict`` 直接构造）。

        Raises:
            BriefValidationError: 版本空 / 时长非法。
        """
        if not isinstance(self.brief_version, str) or not self.brief_version.strip():
            raise BriefValidationError("brief_version_required")
        for name in ("production_hours", "review_hours", "publish_buffer_hours"):
            _validate_hours(getattr(self, name), name=name, limit=duration_max_hours)
        _validate_hours(self.max_experiment_hours, name="max_experiment_hours", limit=max_experiment_hours_limit)


def match_account(
    brief: CreatorBrief,
    *,
    entities: Any = (),
    domains: Any = (),
) -> str:
    """显式账号匹配（**只依据用户声明条件**，不推断算法会推荐给谁）。

    维度规则（每个维度独立判定，整体取最优）:

    - 实体维度：``allowed_entities``/``primary_entities`` 均为 ``None`` → 未声明；
      显式空集合 → 用户明确“不限” → ``explicit_match``；事件实体完全落在集合内 →
      ``explicit_match``；有交集 → ``partial_match``；否则未声明。
    - 领域维度：``content_domains`` 同上。

    Args:
        brief: 创作者简报。
        entities: 事件实体 token 集合。
        domains: 事件领域 token 集合。

    Returns:
        str: ``explicit_match`` / ``partial_match`` / ``unspecified``。
    """
    event_entities = _norm_set(entities)
    event_domains = _norm_set(domains)
    ranks: list[int] = []

    # ---- 实体维度 ----
    if brief.allowed_entities is not None or brief.primary_entities is not None:
        allowed = brief.allowed_entity_set
        if not allowed:
            ranks.append(0)  # 用户明确“不限”
        elif event_entities and event_entities <= allowed:
            ranks.append(0)
        elif event_entities and (event_entities & allowed):
            ranks.append(1)
        else:
            ranks.append(2)

    # ---- 领域维度 ----
    if brief.content_domains is not None:
        domains_set = set(brief.content_domains)
        if not domains_set:
            ranks.append(0)
        elif event_domains and event_domains <= domains_set:
            ranks.append(0)
        elif event_domains and (event_domains & domains_set):
            ranks.append(1)
        else:
            ranks.append(2)

    if not ranks:
        return MATCH_UNSPECIFIED
    return _MATCH_BY_RANK[min(ranks)]


def is_excluded(
    brief: CreatorBrief,
    *,
    entities: Any = (),
    domains: Any = (),
    topics: Any = (),
    content_type: str | None = None,
) -> bool:
    """判定事件是否命中用户**显式排除**（实体 / 话题 / 禁止内容类型）。

    Args:
        brief: 创作者简报。
        entities: 事件实体 token。
        domains: 事件领域 token。
        topics: 事件话题 token。
        content_type: 事件内容类型（对应 ``risk_preferences.excluded_types``）。

    Returns:
        bool: 命中显式排除返回 ``True``。
    """
    excluded = brief.excluded_set
    if excluded & (_norm_set(entities) | _norm_set(domains) | _norm_set(topics)):
        return True
    if content_type is not None and _norm_token(content_type) in brief.excluded_content_types:
        return True
    return False


def missing_assets(brief: CreatorBrief, required_assets: Any = ()) -> set[str]:
    """返回**事件需要但用户不具备**的素材能力集合。

    Args:
        brief: 创作者简报。
        required_assets: 事件所需素材能力 token。

    Returns:
        set[str]: 缺失素材集合（``required - available``）。
    """
    return _norm_set(required_assets) - brief.available_asset_set


def investment_cap_hours(brief: CreatorBrief) -> float:
    """试做投入上限（取 ``max_experiment_hours`` 与 ``risk_preferences.max_hours`` 更严者）。"""
    cap = float(brief.max_experiment_hours)
    raw = brief.risk_preferences.get("max_hours") if isinstance(brief.risk_preferences, Mapping) else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        cap = min(cap, float(raw))
    return cap


__all__ = [
    "DEFAULT_DURATION_MAX_HOURS",
    "DEFAULT_MAX_EXPERIMENT_HOURS_LIMIT",
    "DURATION_FIELDS",
    "LIST_FIELDS",
    "ALLOWED_FIELDS",
    "ALLOWED_RISK_KEYS",
    "MATCH_EXPLICIT",
    "MATCH_PARTIAL",
    "MATCH_UNSPECIFIED",
    "MATCH_ORDER",
    "BriefValidationError",
    "CreatorBrief",
    "match_account",
    "is_excluded",
    "missing_assets",
    "investment_cap_hours",
]
