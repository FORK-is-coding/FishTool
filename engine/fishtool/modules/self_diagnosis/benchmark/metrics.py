"""01 正确排名：纯函数统计、并列名次与分位数（规格 §6.1）。

本模块只做纯计算：不访问 Web / 网络 / DB / LLM，可独立单测。
成绩一律用 ``score_twice``（中位值 × 2）做整数比较，避免浮点误差判断并列。

关键口径（不得违反）：
- competition rank：高分名次靠前，同分名次相同；
- ``rank_end`` 是包含目标自己的并列范围上界；
- 全相等时 percentile = 50，绝不输出 100；
- peer 数量 >= 5 才给 percentile；peer < 5 不给 percentile 但保留真实名次；
- 至少 1 个有效 peer 就能给出精确的集合内名次。
"""
from __future__ import annotations

from math import ceil, floor
from typing import Any, Dict, Iterable, List, Optional, Sequence

PERCENTILE_METHOD = 'peer_midrank_excluding_target_v1'
# 少于该 peer 数时不展示百分位，避免制造精密感
PERCENTILE_MIN_PEERS = 5


def median_twice(values: List[int]) -> int:
    """返回中位值的两倍（整数），用于无浮点误差的比较与并列判定。

    Args:
        values: 非负整数列表（如选中稿件的累计播放）。

    Returns:
        int: 奇数长度取中间元素 × 2；偶数长度取中间两元素之和。

    Raises:
        ValueError: ``values`` 为空时抛出 ``empty_values``。
    """
    if not values:
        raise ValueError('empty_values')
    xs = sorted(values)
    k = len(xs) // 2
    return xs[k] * 2 if len(xs) % 2 else xs[k - 1] + xs[k]


def score_twice(values: List[int]) -> int:
    """语义别名：由选中稿件成绩得到用于排名的 ``score_twice``。

    Args:
        values: 选中稿件的累计播放列表。

    Returns:
        int: 中位值 × 2，等价于 :func:`median_twice`。
    """
    return median_twice(values)


def rank_one(target_score2: int, peer_scores2: List[int]) -> Dict[str, Any]:
    """按竞争名次计算目标在参评集合中的名次与参照百分位。

    调用方必须保证 ``peer_scores2`` 已剔除目标本人（本函数不做 UID 去重）。

    Args:
        target_score2: 目标账号的 ``score_twice``（中位值 × 2）。
        peer_scores2: 有效同行账号的 ``score_twice`` 列表（不含目标）。

    Returns:
        Dict[str, Any]: 含 rank / rank_end / total / reference_count /
        percentile / percentile_method 的字典；无有效 peer 时 rank 为 None。
    """
    higher = sum(v > target_score2 for v in peer_scores2)
    equal = sum(v == target_score2 for v in peer_scores2)
    lower = sum(v < target_score2 for v in peer_scores2)
    m = len(peer_scores2)
    if not m:
        return {'rank': None, 'rank_end': None, 'total': 1,
                'percentile': None, 'reference_count': 0}
    start = higher + 1
    return {
        'rank': start,                    # competition rank，高分名次靠前
        'rank_end': start + equal,         # 包含目标自己的并列范围
        'total': m + 1,
        'reference_count': m,
        'percentile': (100 * (lower + .5 * equal) / m) if m >= 5 else None,
        'percentile_method': 'peer_midrank_excluding_target_v1',
    }


def normalize_peer_uids(target_uid: int, peer_uids: Iterable[int]) -> List[int]:
    """去重并剔除目标本人，返回升序 peer UID 列表（一人一票）。

    规格要求 ``reference_count`` 不含目标本人，且 UID 必须去重——目标即使
    在原始列表里出现多次，也只能从参评集合中彻底消失。

    Args:
        target_uid: 目标账号 UID（无论出现几次都剔除）。
        peer_uids: 原始同行 UID 列表（允许重复、乱序、含目标）。

    Returns:
        List[int]: 去重且剔除目标后的升序 UID 列表。
    """
    seen = set()
    normalized: List[int] = []
    for uid in peer_uids or []:
        if uid == target_uid:
            continue
        if uid in seen:
            continue
        seen.add(uid)
        normalized.append(uid)
    return sorted(normalized)


def percentile_linear(values: Sequence[float], quantile: float) -> float:
    """线性插值分位数（numpy 'linear' 口径）。

    规格要求 p25 / p50 / p75 使用 peer 成绩（不含目标）并明确线性插值。

    Args:
        values: 数值序列（如 peer 的 metric_value）。
        quantile: 分位点，取值 0.0 ~ 1.0。

    Returns:
        float: 对应分位数。

    Raises:
        ValueError: ``values`` 为空或 ``quantile`` 越界时抛出。
    """
    if not values:
        raise ValueError('empty_values')
    if not 0.0 <= quantile <= 1.0:
        raise ValueError('quantile_out_of_range')
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    position = quantile * (len(xs) - 1)
    lower_index = int(floor(position))
    upper_index = int(ceil(position))
    if lower_index == upper_index:
        return float(xs[lower_index])
    fraction = position - lower_index
    return float(xs[lower_index] + (xs[upper_index] - xs[lower_index]) * fraction)


def reference_distribution(peer_scores2: Sequence[int]) -> Optional[Dict[str, Any]]:
    """基于 peer 成绩（不含目标）计算参照分布，单位为 metric_value。

    Args:
        peer_scores2: 有效同行的 ``score_twice`` 列表（不含目标）。空列表返回 None。

    Returns:
        Optional[Dict[str, Any]]: 含 p25 / p50 / p75 / median / mean / count
        的字典；无 peer 时为 None。
    """
    if not peer_scores2:
        return None
    metric_values = [value / 2 for value in peer_scores2]
    median = percentile_linear(metric_values, 0.5)
    return {
        'p25': percentile_linear(metric_values, 0.25),
        'p50': median,
        'p75': percentile_linear(metric_values, 0.75),
        'median': median,
        'mean': sum(metric_values) / len(metric_values),
        'count': len(metric_values),
        'percentile_method': PERCENTILE_METHOD,
    }
