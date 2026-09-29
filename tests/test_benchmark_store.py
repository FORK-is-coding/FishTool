"""01 正确排名 · BenchmarkRun 短事务持久化验收（规格 §7.3 / §7.4 / §11）。

覆盖：新表幂等、JSON 真实落盘（非内存假象）、lease 只有持 token 者能写、
终态不可变、重启标 interrupted 且旧 token 写入失败、全链不读 Hotspot。
"""
from __future__ import annotations

import inspect
import sys

import pytest
from sqlalchemy import text

import modules.self_diagnosis.benchmark.store as store_module
from core.database import DatabaseManager
from modules.self_diagnosis.benchmark.store import BenchmarkStore


@pytest.fixture()
def manager(tmp_path):
    """隔离临时库，绝不触碰仓库 data/bili_ops.db。"""
    return DatabaseManager(str(tmp_path / "bench_ops.db"))


@pytest.fixture()
def store(manager):
    """基于临时库的存储实例。"""
    return BenchmarkStore(manager.get_session)


def _policy() -> dict:
    """冻结策略字典（与 BenchmarkPolicy 默认值一致）。"""
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


def _sample(uid: int, score2: int, **overrides) -> dict:
    """构造一个账号样本；可用 overrides 覆盖任意字段。"""
    data = {
        'uid': uid,
        'name': f'up{uid}',
        'follower_count': 1000,
        'follower_status': 'ok',
        'status': 'valid',
        'score_twice': score2,
        'metric_value': score2 / 2,
        'selected_videos': [
            {'bvid': f'BV{uid}', 'published_s': 1000, 'observed_s': 2000,
             'raw_tid': 4, 'view_count': score2 // 2, 'view_status': 'ok', 'source': 'view'},
        ],
        'collected_count': 1,
        'selected_count': 1,
        'fetch_complete': True,
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------
# 建表 / 创建
# --------------------------------------------------------------------------

def test_benchmark_table_created_and_idempotent(manager):
    """新表存在，且重复 create_tables 不报错（幂等）。"""
    manager.create_tables()
    manager.create_tables()
    session = manager.get_session()
    try:
        names = {
            row[0] for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    finally:
        session.close()
    assert 'benchmark_runs' in names


def test_create_run_persists_policy_and_peers(store):
    store.create_run('r0', target_uid=100, policy=_policy(),
                     requested_peers=[200, 300], selection_as_of_s=123, now_s=5)
    row = store.read_run('r0')
    assert row['status'] == 'queued'
    assert row['target_uid'] == 100
    assert row['selection_as_of_s'] == 123
    assert row['requested_peers'] == [200, 300]
    assert row['policy']['version'] == 'recent10_age7_30_median_views_v1'
    assert row['creator_samples'] == []
    assert row['error_codes'] == []
    assert row['created_s'] == 5
    assert row['lease_token'] is None


# --------------------------------------------------------------------------
# JSON 真实落盘
# --------------------------------------------------------------------------

def test_samples_persist_to_disk_not_memory(store, manager):
    """用全新引擎 + 全新会话重读，证明样本真实落盘而非内存假象。"""
    store.create_run('r1', target_uid=100, policy=_policy(),
                     requested_peers=[200, 300], selection_as_of_s=1, now_s=10)
    assert store.claim_run('r1', 'tok-A', now_s=11) is True
    assert store.save_creator_sample('r1', 'tok-A', _sample(200, 400), now_s=12) is True
    assert store.save_creator_sample('r1', 'tok-A', _sample(300, 600), now_s=13) is True

    fresh = DatabaseManager(str(manager.db_path))
    fresh_store = BenchmarkStore(fresh.get_session)
    row = fresh_store.read_run('r1')
    assert row is not None
    assert sorted(item['uid'] for item in row['creator_samples']) == [200, 300]
    assert {item['uid']: item['score_twice'] for item in row['creator_samples']} == {200: 400, 300: 600}


def test_sample_write_replaces_same_uid_whole_json(store):
    """同 uid 重复提交必须整体覆盖：若误用 in-place append 会保留旧值而失败。"""
    store.create_run('r2', target_uid=100, policy=_policy(),
                     requested_peers=[200], selection_as_of_s=1, now_s=1)
    assert store.claim_run('r2', 't', now_s=2) is True
    assert store.save_creator_sample('r2', 't', _sample(200, 400), now_s=3) is True
    assert store.save_creator_sample('r2', 't', _sample(200, 800), now_s=4) is True
    row = store.read_run('r2')
    assert len(row['creator_samples']) == 1
    assert row['creator_samples'][0]['score_twice'] == 800
    assert row['heartbeat_s'] == 4


# --------------------------------------------------------------------------
# lease / token 校验
# --------------------------------------------------------------------------

def test_only_lease_token_holder_can_write(store):
    """只有持 token 的 worker 能追加样本或终结 run。"""
    store.create_run('r3', target_uid=100, policy=_policy(),
                     requested_peers=[200], selection_as_of_s=1, now_s=1)
    assert store.claim_run('r3', 'tok-right', now_s=2) is True
    assert store.save_creator_sample('r3', 'tok-wrong', _sample(200, 400)) is False
    assert store.save_creator_sample('r3', 'tok-right', _sample(200, 400)) is True
    assert store.finish_run('r3', 'tok-wrong', result={'x': 1}) is False
    assert store.finish_run('r3', 'tok-right', result={'x': 1}, snapshot_hash='a' * 64) is True


def test_claim_requires_queued_status(store):
    """已 running 的 run 不能被二次抢占。"""
    store.create_run('r4', target_uid=100, policy=_policy(),
                     requested_peers=[200], selection_as_of_s=1, now_s=1)
    assert store.claim_run('r4', 'first', now_s=2) is True
    assert store.claim_run('r4', 'second', now_s=3) is False


# --------------------------------------------------------------------------
# 终态不可变
# --------------------------------------------------------------------------

def test_terminal_state_is_immutable(store):
    """已完成 run 拒绝追加样本、拒绝重写 result，旧 token 也不能再写。"""
    store.create_run('r5', target_uid=100, policy=_policy(),
                     requested_peers=[200], selection_as_of_s=1, now_s=1)
    store.claim_run('r5', 'tok', now_s=2)
    assert store.finish_run('r5', 'tok', result={'state': 'complete'},
                            snapshot_hash='b' * 64, now_s=3) is True
    assert store.save_creator_sample('r5', 'tok', _sample(200, 999)) is False
    assert store.finish_run('r5', 'tok', result={'state': 'tampered'},
                            snapshot_hash='c' * 64) is False
    row = store.read_run('r5')
    assert row['status'] == 'completed'
    assert row['result'] == {'state': 'complete'}
    assert row['snapshot_hash'] == 'b' * 64
    assert row['lease_token'] is None


def test_finish_run_rejects_non_terminal_status(store):
    store.create_run('r8', target_uid=1, policy=_policy(),
                     requested_peers=[2], selection_as_of_s=1, now_s=1)
    store.claim_run('r8', 'tok', now_s=2)
    with pytest.raises(ValueError):
        store.finish_run('r8', 'tok', status='running')


# --------------------------------------------------------------------------
# 重启 / interrupted
# --------------------------------------------------------------------------

def test_mark_interrupted_clears_lease_and_blocks_old_token(store):
    """重启标 interrupted 同时清除 token，旧 worker 写入必须失败。"""
    store.create_run('r6', target_uid=100, policy=_policy(),
                     requested_peers=[200], selection_as_of_s=1, now_s=1)
    assert store.claim_run('r6', 'tok-old', now_s=2) is True
    assert store.save_creator_sample('r6', 'tok-old', _sample(200, 400), now_s=3) is True
    assert store.mark_interrupted('r6', now_s=4) == 1

    row = store.read_run('r6')
    assert row['status'] == 'interrupted'
    assert row['lease_token'] is None
    assert store.save_creator_sample('r6', 'tok-old', _sample(300, 600)) is False
    assert store.finish_run('r6', 'tok-old', result={}) is False
    assert store.claim_run('r6', 'tok-new', now_s=5) is False
    # interrupted 不清空已采样本
    assert [item['uid'] for item in row['creator_samples']] == [200]


def test_mark_interrupted_sweep_skips_terminal_runs(store):
    """run_id=None 清扫所有非终态 run，但已终态不被改写。"""
    for run_id in ('q1', 'q2', 'done'):
        store.create_run(run_id, target_uid=1, policy=_policy(),
                         requested_peers=[2], selection_as_of_s=1, now_s=1)
    store.claim_run('q2', 't2', now_s=2)
    store.claim_run('done', 'td', now_s=2)
    store.finish_run('done', 'td', result={'ok': True}, now_s=3)

    affected = store.mark_interrupted(now_s=9)
    assert affected == 2
    assert store.read_run('q1')['status'] == 'interrupted'
    assert store.read_run('q2')['status'] == 'interrupted'
    assert store.read_run('done')['status'] == 'completed'


def test_mark_interrupted_can_replace_token(store):
    store.create_run('r7', target_uid=1, policy=_policy(),
                     requested_peers=[2], selection_as_of_s=1, now_s=1)
    store.claim_run('r7', 'tok-old', now_s=2)
    assert store.mark_interrupted('r7', token_replacement='tok-new', now_s=3) == 1
    assert store.read_run('r7')['lease_token'] == 'tok-new'
    assert store.save_creator_sample('r7', 'tok-old', _sample(2, 400)) is False


def test_read_missing_run_returns_none(store):
    assert store.read_run('nope') is None


# --------------------------------------------------------------------------
# 红线：全链不读 Hotspot
# --------------------------------------------------------------------------

def test_full_chain_does_not_read_hotspot(tmp_path):
    """规格红线：排名 store 全链不读 Hotspot（源码 + 运行时双重确认）。"""
    assert 'hotspot' not in inspect.getsource(store_module).lower()

    manager = DatabaseManager(str(tmp_path / "no_hotspot.db"))
    store = BenchmarkStore(manager.get_session)

    detached = {name: module for name, module in list(sys.modules.items()) if 'hotspot' in name}
    for name in detached:
        sys.modules.pop(name, None)
    try:
        store.create_run('h1', target_uid=1, policy=_policy(),
                         requested_peers=[2], selection_as_of_s=1, now_s=1)
        assert store.claim_run('h1', 'tok', now_s=2) is True
        assert store.save_creator_sample('h1', 'tok', _sample(2, 400), now_s=3) is True
        assert store.finish_run('h1', 'tok', result={'ok': True},
                                snapshot_hash='d' * 64, now_s=4) is True
        assert store.read_run('h1')['status'] == 'completed'
        reimported = [name for name in sys.modules if 'hotspot' in name]
        assert reimported == [], f'排名全链不应导入 Hotspot，却出现：{reimported}'
    finally:
        sys.modules.update(detached)
