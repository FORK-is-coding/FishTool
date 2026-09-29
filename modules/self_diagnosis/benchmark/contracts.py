"""01 正确排名：请求、指标、结果与质量契约（规格 §7.1 / §7.2）。

设计约束：
- 纯计算层优先 dataclass，不引入 Pydantic，也不访问 Web / 网络 / DB / LLM。
- ``compute_snapshot_hash`` 只覆盖不可变事实：不把 hash 自身、易变进度、
  日志字符串（errors / stop_reason / source_evidence）纳入。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 版本常量（避免魔法字符串散落各处）
# --------------------------------------------------------------------------
POLICY_VERSION = 'recent10_age7_30_median_views_v1'
ALGORITHM_VERSION = 'recent10_age7_30_median_views_v1'
SCHEMA_VERSION = 3
PERCENTILE_METHOD = 'peer_midrank_excluding_target_v1'

# snapshot_hash 纳入的稿件稳定字段（只取必要事实）
_SAMPLE_VIDEO_KEYS = (
    'bvid', 'published_s', 'observed_s', 'raw_tid', 'view_count', 'view_status', 'source',
)
# snapshot_hash 纳入的样本稳定字段。
# 刻意排除 errors / stop_reason / source_evidence：它们属于日志型易变内容，纳入会导致
# 同一批逻辑结果因重试次数、错误文案差异而 hash 漂移。
_SAMPLE_HASH_KEYS = (
    'uid', 'name', 'follower_count', 'follower_status', 'status', 'score_twice',
    'metric_value', 'collected_count', 'selected_count', 'fetch_complete', 'selected_videos',
)


@dataclass
class BenchmarkPolicy:
    """一次排名批次冻结后的固定策略（口径与预算）。"""

    version: str = POLICY_VERSION
    content_scope: str = 'all_public_uploads'          # all_public_uploads | exact_raw_tid
    raw_tid: Optional[int] = None
    size_match: str = 'none'                           # none | same_follower_band
    window_days: int = 30
    minimum_age_days: int = 7
    max_videos: int = 10
    min_videos: int = 3
    max_pages_per_creator: int = 10
    max_detail_requests_per_creator: int = 100
    max_external_requests: int = 1000
    max_observation_span_s: int = 7200
    peer_source: str = 'manual_peer_set'               # manual_peer_set | ranking_discovered_peer_set

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的纯字典（保留字段声明顺序）。"""
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass
class CreatorSample:
    """单个账号一轮采集的事实快照（一个账号只有一票）。"""

    uid: int
    name: Optional[str] = None
    follower_count: Optional[int] = None
    follower_status: str = 'unknown'
    # valid | insufficient_posts | incomplete_selection | missing_selected_metrics | error
    status: str = 'valid'
    score_twice: Optional[int] = None
    metric_value: Optional[float] = None
    selected_videos: List[Dict[str, Any]] = field(default_factory=list)
    collected_count: int = 0
    selected_count: int = 0
    fetch_complete: bool = False
    stop_reason: Optional[str] = None
    errors: List[Dict[str, Any]] = field(default_factory=list)
    source_evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转成纯字典；嵌套的稿件列表保持为普通 dict 列表。"""
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass
class BenchmarkResult:
    """一次排名批次的唯一不可变结果契约（schema_version=3）。"""

    schema_version: int = SCHEMA_VERSION
    run_id: Optional[str] = None
    target_uid: Optional[int] = None
    policy: Optional[Dict[str, Any]] = None
    selection_as_of_s: Optional[int] = None
    observation_started_s: Optional[int] = None
    observation_finished_s: Optional[int] = None
    # complete | partial | insufficient_peers | target_unavailable
    comparison_state: str = 'complete'
    scope: str = 'selected_creator_set'
    unit: str = 'creator'
    metric: str = 'median_cumulative_views'
    requested_peer_count: int = 0
    valid_peer_count: int = 0
    ranked_count: int = 0
    reference_count: int = 0
    excluded_peers: List[Dict[str, Any]] = field(default_factory=list)
    target: Optional[Dict[str, Any]] = None
    reference_distribution: Optional[Dict[str, Any]] = None
    leaderboard: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    snapshot_hash: Optional[str] = None
    algorithm_version: str = ALGORITHM_VERSION

    def to_dict(self) -> Dict[str, Any]:
        """转成纯字典；嵌套的榜单 / 警告保持为普通结构。"""
        return {item.name: getattr(self, item.name) for item in fields(self)}


# --------------------------------------------------------------------------
# snapshot_hash 计算
# --------------------------------------------------------------------------

def _as_plain(obj: Any) -> Any:
    """尽力把 dataclass / dict / list 递归转成纯 Python 结构。

    Args:
        obj: 任意对象（dataclass、dict、list、标量或 None）。

    Returns:
        Any: 与之等价的纯 Python 结构，便于 JSON 序列化与稳定比较。
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if is_dataclass(obj) and not isinstance(obj, type):
        return {item.name: _as_plain(getattr(obj, item.name)) for item in fields(obj)}
    if isinstance(obj, dict):
        return {key: _as_plain(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_as_plain(item) for item in obj]
    return obj


def canonicalize_samples(samples: Any) -> List[Dict[str, Any]]:
    """把 CreatorSample（或等价 dict）规范化为稳定、可哈希的最小事实列表。

    规范化规则（保证「同逻辑结果 → 同 hash」）：
    - 每个样本只保留稳定事实字段（排除 errors / stop_reason / source_evidence）；
    - ``selected_videos`` 只保留必要稿件字段，并按 bvid 升序稳定排序；
    - 样本按 uid 升序稳定排序。

    Args:
        samples: CreatorSample 列表，或等价的 dict 列表；允许为 None。

    Returns:
        List[Dict[str, Any]]: 规范化后的样本列表。
    """
    normalized: List[Dict[str, Any]] = []
    for sample in samples or []:
        data = _as_plain(sample) or {}
        videos: List[Dict[str, Any]] = []
        for video in data.get('selected_videos') or []:
            video_data = _as_plain(video) or {}
            videos.append({key: video_data.get(key) for key in _SAMPLE_VIDEO_KEYS})
        # bvid 可能为 None：用 (is_none, bvid) 保证 None 排在最后且排序稳定
        videos.sort(key=lambda item: (item.get('bvid') is None, item.get('bvid') or ''))
        entry = {key: data.get(key) for key in _SAMPLE_HASH_KEYS}
        entry['selected_videos'] = videos
        normalized.append(entry)
    normalized.sort(key=lambda item: (item.get('uid') is None, item.get('uid')))
    return normalized


def build_snapshot_payload(
    *,
    schema_version: int = SCHEMA_VERSION,
    policy: Any,
    target_uid: Optional[int],
    peer_uids: Any,
    selection_as_of_s: Optional[int],
    creator_samples: Any,
    observation_started_s: Optional[int] = None,
    observation_finished_s: Optional[int] = None,
    result: Any = None,
) -> Dict[str, Any]:
    """构造 snapshot_hash 的规范化输入。

    纳入：schema_version、policy、target_uid、排序去重后的 peer UID、as_of、
    各 CreatorSample 稳定事实、实际观测时间、结果。
    不纳入：hash 自身、易变进度、日志字符串。

    Args:
        schema_version: 结果契约版本。
        policy: BenchmarkPolicy 或等价 dict。
        target_uid: 目标账号 UID。
        peer_uids: 请求同行 UID（可为未去重 / 未排序的原始列表）。
        selection_as_of_s: 选稿参考时刻。
        creator_samples: 各账号样本（dataclass 或 dict 均支持）。
        observation_started_s: 采集开始时刻。
        observation_finished_s: 采集结束时刻。
        result: 结果对象（dataclass 或 dict），其 snapshot_hash 字段会被剔除。

    Returns:
        Dict[str, Any]: 可稳定 JSON 序列化的 payload。
    """
    policy_plain = _as_plain(policy)
    # peer UID：排序 + 去重（目标是否剔除由上层负责，此处只做规范化）
    unique_peers = sorted({int(uid) for uid in (peer_uids or [])})
    result_plain = _as_plain(result)
    if isinstance(result_plain, dict):
        result_plain = {key: value for key, value in result_plain.items() if key != 'snapshot_hash'}
    return {
        'schema_version': schema_version,
        'policy': policy_plain,
        'target_uid': target_uid,
        'peer_uids': unique_peers,
        'selection_as_of_s': selection_as_of_s,
        'observation_started_s': observation_started_s,
        'observation_finished_s': observation_finished_s,
        'creator_samples': canonicalize_samples(creator_samples),
        'result': result_plain,
    }


def compute_snapshot_hash(**kwargs: Any) -> str:
    """对规范化 payload 计算 SHA-256（UTF-8 十六进制）。

    固定使用 ``json.dumps(..., ensure_ascii=False, sort_keys=True,
    separators=(',', ':'), allow_nan=False)``，因此字段顺序变化不影响 hash，
    NaN / Infinity 会直接报错而不是被静默写入。

    Args:
        **kwargs: 透传给 :func:`build_snapshot_payload` 的关键字参数。

    Returns:
        str: 64 位十六进制 SHA-256。
    """
    payload = build_snapshot_payload(**kwargs)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def compute_run_snapshot_hash(run_row: Dict[str, Any]) -> str:
    """从 BenchmarkRun 行字典计算 snapshot_hash（供 store / 测试复用）。

    Args:
        run_row: 含 schema_version / policy / target_uid / requested_peers /
            selection_as_of_s / creator_samples / started_s / finished_s / result 的字典。

    Returns:
        str: 64 位十六进制 SHA-256。
    """
    return compute_snapshot_hash(
        schema_version=run_row.get('schema_version', SCHEMA_VERSION),
        policy=run_row.get('policy'),
        target_uid=run_row.get('target_uid'),
        peer_uids=run_row.get('requested_peers') or [],
        selection_as_of_s=run_row.get('selection_as_of_s'),
        creator_samples=run_row.get('creator_samples') or [],
        observation_started_s=run_row.get('started_s'),
        observation_finished_s=run_row.get('finished_s'),
        result=run_row.get('result'),
    )
