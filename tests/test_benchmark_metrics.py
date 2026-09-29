"""01 正确排名 · 纯计算层数值验收（规格 §6.1 / §11）。

覆盖：中位与 score_twice 整数比较、全并列、target 剔参照、名次方向、
None 不零、至少 1 个 peer 出精确名次、peer<5 不给百分位、UID 去重剔本人、
p25/p50/p75 只用 peer 成绩。
"""
from __future__ import annotations

import pytest

from modules.self_diagnosis.benchmark.metrics import (
    median_twice,
    normalize_peer_uids,
    percentile_linear,
    rank_one,
    reference_distribution,
    score_twice,
)


# --------------------------------------------------------------------------
# 中位与 score_twice
# --------------------------------------------------------------------------

def test_median_twice_odd_and_even():
    """奇数长度取中间值×2，偶数长度取中间两值之和。"""
    assert median_twice([100, 200, 300, 400, 500]) == 600     # 中位 300 → 600
    assert median_twice([100, 200, 300, 400]) == 500          # 200 + 300
    assert median_twice([7]) == 14


def test_median_twice_empty_raises_not_zero():
    """空列表必须报错，绝不返回 0（避免把缺失当 0 分）。"""
    with pytest.raises(ValueError):
        median_twice([])


def test_score_twice_integer_compare_avoids_float_error():
    """半整数中位值用整数 score_twice 比较，判等与判序都精确。"""
    assert score_twice([100, 200, 301]) == 400
    assert median_twice([100, 200, 301, 402]) == 501          # 半整数
    tied = rank_one(501, [500, 501, 502])
    assert tied['rank'] == 2
    assert tied['rank_end'] == 3


# --------------------------------------------------------------------------
# 数值验收（规格 §11）
# --------------------------------------------------------------------------

def test_rank_target200_peers_100_500_acceptance():
    """目标 200、peer=[100,200,300,400,500] → rank4 / rank_end5 / total6 / P30（不是 P70）。"""
    result = rank_one(200, [100, 200, 300, 400, 500])
    assert result['rank'] == 4
    assert result['rank_end'] == 5
    assert result['total'] == 6
    assert result['reference_count'] == 5
    assert result['percentile'] == pytest.approx(30.0)
    assert result['percentile'] != pytest.approx(70.0)


def test_rank_all_equal_ties_1_to_6_and_percentile_50():
    """目标与 5 同行均为 100 → 并列 1—6，percentile 必须是 50，不得为 100。"""
    result = rank_one(100, [100, 100, 100, 100, 100])
    assert result['rank'] == 1
    assert result['rank_end'] == 6
    assert result['total'] == 6
    assert result['reference_count'] == 5
    assert result['percentile'] == pytest.approx(50.0)
    assert result['percentile'] != 100


def test_rank_single_peer_keeps_exact_rank_without_percentile():
    """目标 100、peer=[50] → rank1/2、percentile=None，但仍有精确名次。"""
    result = rank_one(100, [50])
    assert result['rank'] == 1
    assert result['rank_end'] == 1
    assert result['total'] == 2
    assert result['reference_count'] == 1
    assert result['percentile'] is None


def test_rank_no_peer_returns_null_not_zero():
    """无有效 peer：rank 为 None（不以 0 排序目标），total 仍为 1。"""
    result = rank_one(100, [])
    assert result['rank'] is None
    assert result['rank_end'] is None
    assert result['total'] == 1
    assert result['reference_count'] == 0
    assert result['percentile'] is None


def test_rank_direction_higher_score_ranks_first():
    """高分名次靠前：500 在低分 peer 中排第 1，100 在高分 peer 中排最后。"""
    assert rank_one(500, [100, 200, 300, 400])['rank'] == 1
    assert rank_one(100, [200, 300, 400, 500])['rank'] == 5


def test_rank_is_order_insensitive():
    """peer 顺序不影响名次与并列范围。"""
    peers = [100, 200, 300, 400, 500]
    assert rank_one(300, peers) == rank_one(300, list(reversed(peers)))


# --------------------------------------------------------------------------
# UID 去重 / 剔本人
# --------------------------------------------------------------------------

def test_peer_uid_dedup_excludes_target_even_repeated():
    """peer 原始列表含目标 UID 3 次 → 去重后不含本人，reference_count 只数有效同行。"""
    peers = normalize_peer_uids(100, [100, 100, 200, 200, 100, 300])
    assert peers == [200, 300]
    assert 100 not in peers
    result = rank_one(500, [400, 600])
    assert result['reference_count'] == len(peers) == 2
    assert result['total'] == 3


# --------------------------------------------------------------------------
# 分位数（只用 peer 成绩）
# --------------------------------------------------------------------------

def test_percentile_linear_interpolation():
    """线性插值分位数：明确插值口径，不四舍五入。"""
    assert percentile_linear([100, 200, 300, 400], 0.25) == pytest.approx(175.0)
    assert percentile_linear([100, 200, 300, 400], 0.50) == pytest.approx(250.0)
    assert percentile_linear([100, 200, 300, 400], 0.75) == pytest.approx(325.0)
    assert percentile_linear([100, 200, 300, 400], 1.00) == pytest.approx(400.0)


def test_percentile_linear_rejects_empty_and_bad_quantile():
    with pytest.raises(ValueError):
        percentile_linear([], 0.5)
    with pytest.raises(ValueError):
        percentile_linear([1, 2], 1.5)


def test_reference_distribution_uses_only_peer_scores():
    """参照分布只由 peer 成绩构成（单位 metric_value = score_twice/2）。"""
    distribution = reference_distribution([200, 400, 600, 800, 1000])
    assert distribution['count'] == 5
    assert distribution['p50'] == pytest.approx(300.0)
    assert distribution['median'] == pytest.approx(300.0)
    assert distribution['mean'] == pytest.approx(300.0)
    assert distribution['p25'] == pytest.approx(200.0)
    assert distribution['p75'] == pytest.approx(400.0)
    # 空 peer 无分布
    assert reference_distribution([]) is None
