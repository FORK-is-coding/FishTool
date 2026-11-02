"""FishTool 04 · 第三批 d：两条通道数学内核（新目录，独立于 ``algorithm/``）。

依据：``FishTool_04_话题级热点研判与选题决策_02补充执行案(1).md``

- §7.1 通道A（匹配成员新增注意力趋势，UTC 零点锚定 W、U 冻结分母、P2 双窗交集）
- §7.2 / §7.2.1 / §7.2.2 日级三窗与八条判定（照抄 ``event_direction`` / ``interpret_triplet``）
- §7.3 通道B（扩散、供给与机会线索 + ``sampling_changed`` 门）
- §7.4 多事件重叠口径（事件内一次全额、跨事件并集去重）
- §8 两小时早期信号（early 2h，配置全量配置化 + policy_version）
- §9.2 ``events/aggregator.py`` 纯计算契约

边界（严格不越界）：
- **不修改** ``modules/hotspot/algorithm/`` 任何文件，只**调用** ``window_end_s`` 与
  ``lifecycle_v2`` 的纯窗口函数；
- 不碰 3a 六表列定义、3b 围栏、3c 发现与需求映射；
- ``F`` panel 冻结/激活/容量归**第四批**，本批 ``F`` 只作**传入输入**。
"""
from .aggregator import (
    PairedMeasure,
    assert_non_negative_deltas,
    event_direction,
    interpret_triplet,
    matched_totals,
    validate_outer_inputs,
)
from .channel_a import channel_a_trend
from .channel_b import DiscoveryRunView, discovery_signals, sampling_comparable
from .config import DAY_W, EARLY_W, DailyPolicy, EarlyPolicy
from .daily import aggregate_daily_triplet, canonical_fingerprint, interpret_daily
from .early import earliest_evaluation_end_s, evaluate_early
from .overlap import cross_event_union, event_internal_measure
from .windows import (
    MemberRevision,
    PanelResult,
    SnapshotPoint,
    WindowDelta,
    build_panel,
    build_panel_for,
)

__all__ = [
    # 纯计算契约（§9.2 / §7.2.2）
    "PairedMeasure",
    "matched_totals",
    "assert_non_negative_deltas",
    "event_direction",
    "interpret_triplet",
    "validate_outer_inputs",
    # 配置
    "DAY_W",
    "EARLY_W",
    "DailyPolicy",
    "EarlyPolicy",
    # 窗口与 panel
    "MemberRevision",
    "SnapshotPoint",
    "WindowDelta",
    "PanelResult",
    "build_panel",
    "build_panel_for",
    # 通道 A / 日级三窗
    "channel_a_trend",
    "aggregate_daily_triplet",
    "interpret_daily",
    "canonical_fingerprint",
    # 通道 B
    "DiscoveryRunView",
    "discovery_signals",
    "sampling_comparable",
    # 多事件重叠
    "event_internal_measure",
    "cross_event_union",
    # early 2h
    "evaluate_early",
    "earliest_evaluation_end_s",
]
