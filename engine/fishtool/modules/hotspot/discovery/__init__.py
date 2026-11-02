"""06 采集广度 · 聚合入口发现通道。

模块划分（对齐 06 规格 §4 与顶部复核意见）：

- ``contracts``：BroadKeyword / BroadVideo DTO、质量三态解析、来源优先级与合并规则（纯计算）；
- ``sources``：三个聚合入口的读取（I/O）与容错解析（纯 I/O，不含聚合逻辑）；
- ``store``：``hot_keyword_signal`` 与 ``HotspotSignal`` 的落库写入器；
- ``snapshot``：全局发现快照（失败状态落点 + 回放幂等依据）；
- ``service``：调度、共享轮询缓存、去重合并与快照冻结。

本包**不改造** ``modules/hotspot/tag_cloud.py``：词云与发现通道语义不同。
"""
from __future__ import annotations

from .contracts import (
    BroadKeyword,
    BroadVideo,
    ParseOutcome,
    DISCOVERY_SOURCE_PRIORITY,
    SOURCE_POPULAR,
    SOURCE_RANKING_ALL,
    SOURCE_RANKING_ALL_OTHERS,
    SOURCE_SEARCH_SQUARE,
    STATE_ERROR,
    STATE_OK,
    STATE_PARTIAL,
    merge_video_candidates,
    parse_heat_score,
)
from .service import (
    DiscoverRunConfig,
    DiscoveryPollCache,
    DiscoveryService,
    SourceRun,
    build_discovery_service,
    run_discovery_loop,
)
from .snapshot import DiscoverySnapshotStore

__all__ = [
    'BroadKeyword',
    'BroadVideo',
    'ParseOutcome',
    'DISCOVERY_SOURCE_PRIORITY',
    'SOURCE_SEARCH_SQUARE',
    'SOURCE_POPULAR',
    'SOURCE_RANKING_ALL',
    'SOURCE_RANKING_ALL_OTHERS',
    'STATE_OK',
    'STATE_PARTIAL',
    'STATE_ERROR',
    'parse_heat_score',
    'merge_video_candidates',
    'DiscoverRunConfig',
    'DiscoveryPollCache',
    'DiscoveryService',
    'SourceRun',
    'build_discovery_service',
    'run_discovery_loop',
    'DiscoverySnapshotStore',
]
