"""FishTool 04 · 第三批 d：两条通道策略配置（**全量配置化 + policy_version**）。

依据：
- §7.2 L680：绝对 **20 播放/视频/天** + 总量相对 **20%**（版本化初始业务规则，非统计显著性）；
- §7.2 L681-682：样本门默认 P3 ``>=3 视频``、``>=2 已知作者``、成员覆盖率 ``>=0.7``；
  ``top_author_share>0.7``→``concentrated``；未知贡献 ``>0.2``→``author_coverage_insufficient``；
- §7.2.1 L691：daily ``request_as_of_s - window_end_s <= 36h``；
- §8.2 L781-794：early 2h 全量数值。

关键口径（§7.2.2 L745）：**日/快算法使用各自绝对和相对阈值，不共享全局数值常量**。
因此本模块把 daily 与 early 拆成两个独立策略对象，绝不互相复用阈值。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

#: daily 窗口宽度（秒）——UTC 零点锚定（86400）。
DAY_W: int = 86400
#: early 快窗宽度（秒）——UTC 偶数小时锚定（7200）。
EARLY_W: int = 7200

#: discovery 侧租约（秒）——外部发现领取 run 的 lease（原案 L1258，3f 未做，第四批 a 补齐）。
DEFAULT_DISCOVERY_LEASE_SECONDS: int = 300
#: discovery 侧整轮硬 deadline（秒）——必须 < lease（原案 L1258）。
DEFAULT_DISCOVERY_DEADLINE_SECONDS: int = 120
#: 手动发现的冷却时长（秒）（原案 L1258）。
DEFAULT_MANUAL_DISCOVERY_COOLDOWN_SECONDS: int = 60

#: 供给角度密度 / 缺口候选策略默认值（06 §7.1-§7.5，第四批 c）。
#: ``candidate_gap`` 需正文可评估覆盖 >= 此值，否则只能 ``coverage_probe``。
DEFAULT_SUPPLY_CANDIDATE_GAP_COVERAGE_THRESHOLD: float = 0.7
#: ``candidate_gap`` 最少参与者（作者）数。
DEFAULT_SUPPLY_CANDIDATE_GAP_MIN_AUTHORS: int = 3
#: 定向检索至少查询次数。
DEFAULT_SUPPLY_CANDIDATE_GAP_MIN_QUERIES: int = 2
#: 可评价「拥挤」的最少可分类成员数 |C|（小样本门）。
DEFAULT_SUPPLY_ANGLE_MIN_CLASSIFIED: int = 3
#: 可评价「拥挤」所需的最低 ``angle_coverage``（低覆盖门）。
DEFAULT_SUPPLY_ANGLE_MIN_COVERAGE: float = 0.7
#: ``supply_present`` 所需的最少可确认 ``addresses`` 作品数。
DEFAULT_SUPPLY_PRESENT_MIN_ADDRESSES: int = 2
#: 供给策略版本；随结果输出，便于复算。
DEFAULT_SUPPLY_POLICY_VERSION: str = "supply_c_v1"


@dataclass(frozen=True)
class DailyPolicy:
    """日级三窗（P3）阈值与样本门。全部可配置，随结果输出 ``policy_version``。"""

    #: 窗口宽度（秒）；daily 固定 86400，不滑动。
    window_seconds: int = DAY_W
    #: 单个日窗「完整有效」所需最小覆盖（1.0 = 满窗双边支撑）。
    window_require_coverage: float = 1.0
    #: 相邻有效观测点最大间隔；超过即断段，不跨空洞插值（沿用 02 的 36h 门）。
    gap_max_seconds: int = 36 * 3600
    #: 绝对阈值：每视频每天新增播放（B-A、C-A 需 >= ``absolute_per_video * N``）。
    absolute_per_video: float = 20.0
    #: 相对阈值：变化量 / 基线 (A)。
    relative_min: float = 0.2
    #: 样本门：最少视频数。
    min_videos: int = 3
    #: 样本门：最少**已知**作者数。
    min_authors: int = 2
    #: 样本门：成员覆盖率（paired / eligible）。
    min_member_coverage: float = 0.7
    #: ``top_author_share`` 超过该值判 ``concentrated``。
    top_author_share_max: float = 0.7
    #: ``unknown_author_delta_share`` 超过该值判 ``author_coverage_insufficient``。
    unknown_author_share_max: float = 0.2
    #: daily 新鲜度门：``request_as_of_s - window_end_s`` 上限（36h）。
    daily_staleness_max_s: int = 36 * 3600
    #: 成员最后有效观察新鲜度门（02 的 36h 门）。
    member_staleness_max_s: int = 36 * 3600
    #: 政策版本；进入 canonical fingerprint。
    policy_version: str = "daily24h_v1"

    def as_dict(self) -> dict[str, Any]:
        """返回可序列化配置字典，供 fingerprint / 日志 / 回放消费。"""
        return asdict(self)


@dataclass(frozen=True)
class EarlyPolicy:
    """两小时早期信号（early 2h）阈值；照 §8.2 L781-794 逐项配置化。"""

    #: early_window_seconds=7200（UTC 偶数小时锚定）。
    window_seconds: int = EARLY_W
    #: boundary_anchor=UTC偶数小时。
    boundary_anchor: str = "utc_even_hour"
    #: early_gap_max_seconds=2400（40 分钟，**不沿用 02 的 36 小时**）。
    gap_max_seconds: int = 2400
    #: early_min_window_coverage=1.0。
    min_window_coverage: float = 1.0
    #: early_max_staleness_seconds=2400。
    max_staleness_seconds: int = 2400
    #: fast_sample_interval_seconds=1200（20 分钟）。
    fast_sample_interval_seconds: int = 1200
    #: min_paired_videos=3。
    min_paired_videos: int = 3
    #: min_paired_authors=2。
    min_paired_authors: int = 2
    #: min_member_coverage=0.7（**作用于 ``|P2∩F|/|F|`` 快通道覆盖，不用 U 当分母**）。
    min_member_coverage: float = 0.7
    #: relative_change_min=0.5（相对变化 50%）。
    relative_change_min: float = 0.5
    #: absolute_mean_delta_min=20（每个匹配视频每 2h 比前窗多 20 播放）。
    absolute_mean_delta_min: float = 20.0
    #: fast_ttl_seconds=7200（**从证据 window_end_s 算**，不从刷新时间算）。
    fast_ttl_seconds: int = 7200
    #: 快通道质量门 ``|P2∩F| / |F|`` 下限（0.7）。
    fast_coverage_min: float = 0.7
    #: 政策版本；进入 fingerprint。
    policy_version: str = "early2h_v1"

    def as_dict(self) -> dict[str, Any]:
        """返回可序列化配置字典，供 fingerprint / 日志 / 回放消费。"""
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryPolicy:
    """discovery 侧三项配置（原案 L1258）。只读现有 ``hotspot.events`` 段，**不改 02 的 24h 默认阈值**。

    三项与策略（:class:`DailyPolicy` / :class:`EarlyPolicy`）**互相独立**：本类不并入日 / 快算法阈值，
    也不改变 02 侧既有 24h 默认阈值，只把 discovery 的运行参数配置化。

    Attributes:
        discovery_lease_seconds: 外部发现领取 run 的租约（秒）；默认 300。
        discovery_deadline_seconds: 整轮硬 deadline（秒）；默认 120，必须 **< lease**。
        manual_discovery_cooldown_seconds: 手动发现冷却时长（秒）；默认 60。
    """

    discovery_lease_seconds: int = DEFAULT_DISCOVERY_LEASE_SECONDS
    discovery_deadline_seconds: int = DEFAULT_DISCOVERY_DEADLINE_SECONDS
    manual_discovery_cooldown_seconds: int = DEFAULT_MANUAL_DISCOVERY_COOLDOWN_SECONDS

    def as_dict(self) -> dict[str, Any]:
        """返回可序列化配置字典，供日志 / 观测消费。"""
        return asdict(self)

    @classmethod
    def from_config(cls, config_manager: Any = None) -> "DiscoveryPolicy":
        """从 ``ConfigManager`` 的 ``hotspot.events`` 段读三项（缺省回落内置默认值）。

        Args:
            config_manager: 既有配置管理器（须提供 ``get``）；None 时只用默认值。

        Returns:
            DiscoveryPolicy: 校验通过的不可变配置。

        Raises:
            ValueError: 段不是映射，或三项非正整数 / ``deadline >= lease``（不静默降级）。
        """
        raw: Any = None
        if config_manager is not None:
            raw = config_manager.get("hotspot.events", None)
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValueError("hotspot_events_section_must_be_mapping")

        def _pos_int(name: str, default: int) -> int:
            """取正整数项：缺失取默认，非法即抛错（绝不静默改用别的值）。"""
            value = raw.get(name, default)
            if type(value) is not int or value <= 0:
                raise ValueError(f"invalid_discovery_config:{name}")
            return int(value)

        policy = cls(
            discovery_lease_seconds=_pos_int(
                "discovery_lease_seconds", DEFAULT_DISCOVERY_LEASE_SECONDS
            ),
            discovery_deadline_seconds=_pos_int(
                "discovery_deadline_seconds", DEFAULT_DISCOVERY_DEADLINE_SECONDS
            ),
            manual_discovery_cooldown_seconds=_pos_int(
                "manual_discovery_cooldown_seconds", DEFAULT_MANUAL_DISCOVERY_COOLDOWN_SECONDS
            ),
        )
        if policy.discovery_deadline_seconds >= policy.discovery_lease_seconds:
            raise ValueError("invalid_discovery_config:deadline_not_less_than_lease")
        return policy


@dataclass(frozen=True)
class SupplyPolicy:
    """供给角度密度与缺口候选策略（06 §7.1-§7.5，第四批 c）。

    读取**独立顶层段** ``supply``（与 ``hotspot.events`` 的 ``EventPolicy`` 严格白名单段
    隔离，避免触发策略白名单校验），缺省回落内置默认值，全部随结果输出 ``policy_version``。

    Attributes:
        candidate_gap_coverage_threshold: ``candidate_gap`` 需正文可评估覆盖 >= 此值（默认 0.7）。
        candidate_gap_min_authors: 最少参与者（作者）数（默认 3）。
        candidate_gap_min_queries: 定向检索至少查询次数（默认 2）。
        angle_min_classified: 可评价「拥挤」的最少可分类成员数 |C|（小样本门）。
        angle_min_coverage: 可评价「拥挤」所需的最低 ``angle_coverage``（低覆盖门）。
        supply_present_min_addresses: ``supply_present`` 所需最少可确认 ``addresses`` 作品数。
        policy_version: 策略版本号，随结果输出。
    """

    candidate_gap_coverage_threshold: float = DEFAULT_SUPPLY_CANDIDATE_GAP_COVERAGE_THRESHOLD
    candidate_gap_min_authors: int = DEFAULT_SUPPLY_CANDIDATE_GAP_MIN_AUTHORS
    candidate_gap_min_queries: int = DEFAULT_SUPPLY_CANDIDATE_GAP_MIN_QUERIES
    angle_min_classified: int = DEFAULT_SUPPLY_ANGLE_MIN_CLASSIFIED
    angle_min_coverage: float = DEFAULT_SUPPLY_ANGLE_MIN_COVERAGE
    supply_present_min_addresses: int = DEFAULT_SUPPLY_PRESENT_MIN_ADDRESSES
    policy_version: str = DEFAULT_SUPPLY_POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        """返回可序列化策略字典（供日志 / 回放 / 版本比对消费）。"""
        return asdict(self)

    @classmethod
    def from_config(
        cls,
        config_manager: Any = None,
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> "SupplyPolicy":
        """从 ``ConfigManager`` 的独立 ``supply`` 段载入策略（缺省回落内置默认值）。

        Args:
            config_manager: 既有配置管理器（须提供 ``get``）；None 时只用默认值。
            overrides: 显式覆盖（测试 / 调用方传入），优先级最高。

        Returns:
            SupplyPolicy: 校验通过的不可变策略。

        Raises:
            ValueError: 段不是映射 / 出现不支持字段 / 阈值非法（**绝不静默降级**）。
        """
        raw: Any = None
        if config_manager is not None:
            raw = config_manager.get("supply", None)
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValueError("supply_section_must_be_mapping")

        merged: dict[str, Any] = {**asdict(cls()), **dict(raw)}
        if overrides:
            merged.update(dict(overrides))

        unknown = set(merged) - set(asdict(cls()))
        if unknown:
            raise ValueError("unsupported_supply_field:" + ",".join(sorted(unknown)))

        def _ratio(name: str) -> float:
            """取 (0, 1] 比例项；非法即抛错。"""
            value = merged[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"invalid_supply_config:{name}")
            ratio = float(value)
            if not (0.0 < ratio <= 1.0):
                raise ValueError(f"invalid_supply_config:{name}")
            return ratio

        def _pos_int(name: str) -> int:
            """取正整数项；非法即抛错。"""
            value = merged[name]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"invalid_supply_config:{name}")
            return int(value)

        version = merged["policy_version"]
        if not isinstance(version, str) or not version.strip():
            raise ValueError("invalid_supply_config:policy_version")

        return cls(
            candidate_gap_coverage_threshold=_ratio("candidate_gap_coverage_threshold"),
            candidate_gap_min_authors=_pos_int("candidate_gap_min_authors"),
            candidate_gap_min_queries=_pos_int("candidate_gap_min_queries"),
            angle_min_classified=_pos_int("angle_min_classified"),
            angle_min_coverage=_ratio("angle_min_coverage"),
            supply_present_min_addresses=_pos_int("supply_present_min_addresses"),
            policy_version=str(version),
        )


__all__ = [
    "DAY_W",
    "EARLY_W",
    "DEFAULT_DISCOVERY_DEADLINE_SECONDS",
    "DEFAULT_DISCOVERY_LEASE_SECONDS",
    "DEFAULT_MANUAL_DISCOVERY_COOLDOWN_SECONDS",
    "DEFAULT_SUPPLY_ANGLE_MIN_CLASSIFIED",
    "DEFAULT_SUPPLY_ANGLE_MIN_COVERAGE",
    "DEFAULT_SUPPLY_CANDIDATE_GAP_COVERAGE_THRESHOLD",
    "DEFAULT_SUPPLY_CANDIDATE_GAP_MIN_AUTHORS",
    "DEFAULT_SUPPLY_CANDIDATE_GAP_MIN_QUERIES",
    "DEFAULT_SUPPLY_POLICY_VERSION",
    "DEFAULT_SUPPLY_PRESENT_MIN_ADDRESSES",
    "DailyPolicy",
    "DiscoveryPolicy",
    "EarlyPolicy",
    "SupplyPolicy",
]
