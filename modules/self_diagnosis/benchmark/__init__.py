"""FishTool 01 排名子包：正确排名的纯计算契约与短事务持久化。

按 01 规格 §3 拆分为：
- contracts.py：请求 / 指标 / 结果与质量契约 + snapshot_hash 规则
- metrics.py  ：纯函数统计、并列名次、分位数（不访问 Web / 网络 / DB / LLM）
- store.py    ：BenchmarkRun 短事务持久化（需要注入 session factory）

约定：``__init__`` 只导出纯计算层，避免 import 期拉起数据库 / 网络依赖。
"""
from .contracts import (
    ALGORITHM_VERSION,
    POLICY_VERSION,
    SCHEMA_VERSION,
    BenchmarkPolicy,
    BenchmarkResult,
    CreatorSample,
    build_snapshot_payload,
    canonicalize_samples,
    compute_snapshot_hash,
)
from .metrics import (
    median_twice,
    normalize_peer_uids,
    percentile_linear,
    rank_one,
    reference_distribution,
    score_twice,
)

__all__ = [
    # contracts
    'POLICY_VERSION', 'ALGORITHM_VERSION', 'SCHEMA_VERSION',
    'BenchmarkPolicy', 'CreatorSample', 'BenchmarkResult',
    'build_snapshot_payload', 'canonicalize_samples', 'compute_snapshot_hash',
    # metrics
    'median_twice', 'score_twice', 'rank_one', 'normalize_peer_uids',
    'percentile_linear', 'reference_distribution',
]
