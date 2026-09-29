"""01 正确排名 · 契约与 snapshot_hash 稳定性（规格 §7.1 / §7.2）。

覆盖：同输入两次 hash 相同、字段顺序变化不影响 hash（sort_keys）、
peer UID 排序去重不影响 hash、样本/稿件顺序不影响 hash、
分数变化会改 hash、日志型易变字段被排除、hash 为 64 位十六进制。
"""
from __future__ import annotations

import copy

from modules.self_diagnosis.benchmark.contracts import (
    BenchmarkPolicy,
    CreatorSample,
    canonicalize_samples,
    compute_snapshot_hash,
)


def _policy() -> dict:
    """与 BenchmarkPolicy 默认值一致的策略字典。"""
    return {
        'version': 'recent10_age7_30_median_views_v1',
        'content_scope': 'all_public_uploads',
        'raw_tid': None,
        'size_match': 'none',
        'window_days': 30,
        'minimum_age_days': 7,
        'max_videos': 10,
        'min_videos': 3,
        'max_pages_per_creator': 10,
        'max_detail_requests_per_creator': 100,
        'max_external_requests': 1000,
        'max_observation_span_s': 7200,
        'peer_source': 'manual_peer_set',
    }


def _video(bvid: str, view: int) -> dict:
    return {
        'bvid': bvid, 'published_s': 1000, 'observed_s': 2000,
        'raw_tid': 4, 'view_count': view, 'view_status': 'ok', 'source': 'view',
    }


def _kwargs() -> dict:
    return {
        'schema_version': 3,
        'policy': _policy(),
        'target_uid': 100,
        'peer_uids': [200, 300],
        'selection_as_of_s': 1_700_000_000,
        'creator_samples': [
            {'uid': 200, 'status': 'valid', 'score_twice': 400, 'metric_value': 200.0,
             'follower_status': 'unknown',
             'collected_count': 1, 'selected_count': 1, 'fetch_complete': True,
             'selected_videos': [_video('BV2', 200)]},
            {'uid': 300, 'status': 'valid', 'score_twice': 600, 'metric_value': 300.0,
             'follower_status': 'unknown',
             'collected_count': 1, 'selected_count': 1, 'fetch_complete': True,
             'selected_videos': [_video('BV3', 300)]},
        ],
        'observation_started_s': 1_700_000_100,
        'observation_finished_s': 1_700_000_200,
        'result': {'comparison_state': 'complete', 'ranked_count': 3},
    }


def test_same_input_produces_same_hash():
    """同输入两次计算必须得到相同 hash（可复现）。"""
    assert compute_snapshot_hash(**_kwargs()) == compute_snapshot_hash(**_kwargs())


def test_field_order_does_not_change_hash():
    """字段顺序变化不影响 hash（sort_keys 生效）。"""
    original = _kwargs()
    shuffled = _kwargs()
    shuffled['policy'] = dict(reversed(list(shuffled['policy'].items())))
    shuffled['creator_samples'] = list(reversed(shuffled['creator_samples']))
    shuffled['result'] = dict(reversed(list(shuffled['result'].items())))
    assert compute_snapshot_hash(**original) == compute_snapshot_hash(**shuffled)


def test_peer_uid_sort_and_dedup_do_not_change_hash():
    """peer UID 排序 + 去重后相同 → hash 相同。"""
    compact = _kwargs()
    compact['peer_uids'] = [200, 300]
    verbose = _kwargs()
    verbose['peer_uids'] = [300, 200, 300, 200]
    assert compute_snapshot_hash(**compact) == compute_snapshot_hash(**verbose)


def test_video_order_does_not_change_hash():
    """同一账号的稿件顺序变化不影响 hash（canonicalize 会按 bvid 排序）。"""
    original = _kwargs()
    reordered = copy.deepcopy(_kwargs())
    reordered['creator_samples'][0]['selected_videos'] = [_video('BV2', 200)]
    reordered['creator_samples'][0]['selected_videos'].append(_video('BV2b', 100))
    original['creator_samples'][0]['selected_videos'] = [
        _video('BV2b', 100), _video('BV2', 200),
    ]
    assert compute_snapshot_hash(**original) == compute_snapshot_hash(**reordered)


def test_changing_score_changes_hash():
    """成绩变化必须改变 hash（否则无法用于一致性核对）。"""
    original = _kwargs()
    tampered = copy.deepcopy(_kwargs())
    tampered['creator_samples'][0]['score_twice'] = 999
    assert compute_snapshot_hash(**original) != compute_snapshot_hash(**tampered)


def test_volatile_log_fields_are_excluded():
    """errors / stop_reason 等日志型易变内容不纳入 hash。"""
    original = _kwargs()
    noisy = copy.deepcopy(_kwargs())
    noisy['creator_samples'][0]['errors'] = [{'code': 'boom'}]
    noisy['creator_samples'][0]['stop_reason'] = 'page_limit'
    noisy['creator_samples'][0]['source_evidence'] = {'retries': 7}
    assert compute_snapshot_hash(**original) == compute_snapshot_hash(**noisy)


def test_hash_is_sha256_hex():
    """hash 必须是 64 位十六进制字符串。"""
    digest = compute_snapshot_hash(**_kwargs())
    assert len(digest) == 64
    int(digest, 16)


def test_dataclass_inputs_are_supported_and_stable():
    """dataclass 输入同样可用且稳定；policy 默认值与等价 dict 得到同一 hash。"""
    dataclass_kwargs = _kwargs()
    dataclass_kwargs['policy'] = BenchmarkPolicy()
    dataclass_kwargs['creator_samples'] = [
        CreatorSample(uid=200, status='valid', score_twice=400, metric_value=200.0,
                      collected_count=1, selected_count=1, fetch_complete=True,
                      selected_videos=[_video('BV2', 200)]),
        CreatorSample(uid=300, status='valid', score_twice=600, metric_value=300.0,
                      collected_count=1, selected_count=1, fetch_complete=True,
                      selected_videos=[_video('BV3', 300)]),
    ]
    assert (
        compute_snapshot_hash(**dataclass_kwargs)
        == compute_snapshot_hash(**dataclass_kwargs)
    )
    # policy 默认值与手写 dict 完全一致 → hash 相同
    assert (
        compute_snapshot_hash(**dataclass_kwargs)
        == compute_snapshot_hash(**_kwargs())
    )


def test_canonicalize_samples_drops_log_fields_and_sorts():
    """规范化结果：按 uid 升序、只保留稳定事实字段。"""
    normalized = canonicalize_samples([
        {'uid': 300, 'score_twice': 600, 'errors': [{'code': 'x'}], 'stop_reason': 'r',
         'selected_videos': [_video('BVz', 1), _video('BVa', 2)]},
        {'uid': 200, 'score_twice': 400},
    ])
    assert [item['uid'] for item in normalized] == [200, 300]
    assert 'errors' not in normalized[1]
    assert 'stop_reason' not in normalized[1]
    assert [video['bvid'] for video in normalized[1]['selected_videos']] == ['BVa', 'BVz']
