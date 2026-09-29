"""01 正确排名 · 服务与数值验收测试（规格 §6 / §9 / §11）。

覆盖 §11 数值验收中属于「服务 + 纯计算」的部分：
- 目标 score=200、peer=[100,200,300,400,500] -> rank=4 / rank_end=5 / total=6 / percentile=30；
- 全等 -> 并列 1—N、percentile=50（不是 100）；
- 单 peer -> 有名次、无百分位；
- peer 列表含目标 UID 3 次 -> 去重且不含本人；
- 请求 10 同行 3 失败 -> 只按 7 有效同行排名，并显示 requested=10 / excluded=3；
- target_unavailable / insufficient_peers 与 task completed 分离；
- 刷新后同 run 名次与 hash 完全一致（不访问 B 站重算）；
- 观测跨度 > 7200s -> 该组不排名；
- finalize 写失败 / 迟到 token 写入被拒。

测试策略：真实 BenchmarkStore + 临时 sqlite（隔离），stub API 不触网。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from core.database.manager import DatabaseManager
from modules.self_diagnosis.benchmark.contracts import BenchmarkPolicy
from modules.self_diagnosis.benchmark.service import BenchmarkService
from modules.self_diagnosis.benchmark.store import BenchmarkStore

DAY_S = 86400
AS_OF = 1_700_000_000


def run_async(coro):
    """用独立事件循环驱动协程。"""
    return asyncio.run(coro)


class Clock:
    """可控时钟：从 base 起按 step 递增，用于制造观测跨度。"""

    def __init__(self, base: int = AS_OF, step: int = 0) -> None:
        self.value = base
        self.step = step

    def __call__(self) -> int:
        """返回当前时刻并前进。"""
        current = self.value
        self.value += self.step
        return current


class StubAPI:
    """契约级 stub API：按 uid 提供投稿列表与详情。"""

    def __init__(self, creators: Dict[int, Dict[str, Any]], fail_uids: Optional[set] = None) -> None:
        """初始化。

        Args:
            creators: uid -> {'videos': [(bvid, created, tid, view)], 'follower': int}
                view 为 None 时模拟 stat.view 缺失。
            fail_uids: 投稿列表直接抛错的 uid 集合。
        """
        self.creators = creators
        self.fail_uids = fail_uids or set()

    async def get_user_info(self, uid: int) -> Dict[str, Any]:
        """返回带 meta 的用户资料。"""
        follower = (self.creators.get(uid) or {}).get('follower')
        status = 'ok' if follower is not None else 'missing'
        return {'data': {'name': f'UP{uid}', 'follower': follower or 0},
                '_meta': {'source': 'space_info', 'field_status': {'follower': status}}}

    async def get_user_relation_stat(self, uid: int) -> Dict[str, Any]:
        """relation 接口：本次明确返回 follower。"""
        follower = (self.creators.get(uid) or {}).get('follower')
        return {'data': {'follower': follower} if follower is not None else {}}

    async def get_user_videos(self, uid: int, page: int = 1, page_size: int = 30) -> Dict[str, Any]:
        """返回该 UID 的投稿列表（单页）。"""
        if uid in self.fail_uids:
            raise RuntimeError(f'list_failed_{uid}')
        videos = (self.creators.get(uid) or {}).get('videos') or []
        if page > 1:
            videos = []
        vlist = [{'bvid': b, 'created': created, 'tid': tid, 'play': 0} for b, created, tid, _ in videos]
        return {'data': {'list': {'vlist': vlist}, 'page': {'pn': page, 'ps': page_size, 'count': len(vlist)}}}

    async def get(self, url: str, params: Optional[Dict[str, Any]] = None, need_sign: bool = False, **kwargs):
        """返回详情（已解包 data）。"""
        bvid = (params or {}).get('bvid')
        for uid, payload in self.creators.items():
            for entry in payload.get('videos') or []:
                if entry[0] == bvid:
                    stat: Dict[str, Any] = {}
                    if entry[3] is not None:
                        stat['view'] = entry[3]
                    return {'bvid': bvid, 'pubdate': entry[1], 'tid': entry[2],
                            'owner': {'mid': uid}, 'stat': stat}
        return None


def make_store(tmp_path) -> BenchmarkStore:
    """构造指向临时库的短事务存储。"""
    manager = DatabaseManager(str(tmp_path / 'bench_service.db'))
    return BenchmarkStore(lambda: manager.get_session())


def flat_videos(uid: int, view: int, count: int = 3) -> Dict[str, Any]:
    """构造 count 条同播放稿件的账号数据。"""
    return {
        'follower': 1000 + uid,
        'videos': [(f'BV{uid}_{i}', AS_OF - (10 + i) * DAY_S, 4, view) for i in range(count)],
    }


def run_service(tmp_path, creators, target, peers, *, policy=None, clock=None, fail_uids=None):
    """创建并执行一个 run，返回 (service, final_row, result)。"""
    store = make_store(tmp_path)
    service = BenchmarkService(StubAPI(creators, fail_uids), store, clock=clock or Clock(), policy_config={})
    row = service.create_run(target, peers, policy or BenchmarkPolicy(min_videos=3))
    final = run_async(service.execute_run(row['id']))
    return service, final, final.get('result') or {}


# --------------------------------------------------------------------------- #
# 数值验收
# --------------------------------------------------------------------------- #
def test_acceptance_target200_peers_100_to_500(tmp_path) -> None:
    """目标 200、peer=[100,200,300,400,500] -> rank=4、rank_end=5、total=6、percentile=30。"""
    creators = {1: flat_videos(1, 200)}
    for index, view in enumerate([100, 200, 300, 400, 500], start=2):
        creators[index] = flat_videos(index, view)
    _service, final, result = run_service(tmp_path, creators, 1, [2, 3, 4, 5, 6])

    assert final['status'] == 'completed'
    assert result['comparison_state'] == 'complete'
    target = result['target']
    assert target['rank'] == 4
    assert target['rank_end'] == 5
    assert target['total'] == 6
    assert target['percentile'] == 30      # 不是 P70
    assert target['reference_count'] == 5
    assert result['reference_count'] == 5
    assert result['ranked_count'] == 6


def test_acceptance_all_equal_ties_and_percentile_50(tmp_path) -> None:
    """目标与 5 同行均 100 -> 并列 1—6、percentile=50（不得全为 100）。"""
    creators = {uid: flat_videos(uid, 100) for uid in range(1, 7)}
    _service, _final, result = run_service(tmp_path, creators, 1, [2, 3, 4, 5, 6])

    target = result['target']
    assert target['rank'] == 1
    assert target['rank_end'] == 6
    assert target['percentile'] == 50
    assert all(row['rank'] == 1 and row['rank_end'] == 6 for row in result['leaderboard'])


def test_acceptance_single_peer_has_rank_without_percentile(tmp_path) -> None:
    """目标 100、peer=[50] -> rank 1/2、percentile=null，但名次仍真实存在。"""
    creators = {1: flat_videos(1, 100), 2: flat_videos(2, 50)}
    _service, _final, result = run_service(tmp_path, creators, 1, [2])

    target = result['target']
    assert target['rank'] == 1
    assert target['total'] == 2
    assert target['percentile'] is None
    assert result['comparison_state'] == 'complete'


def test_acceptance_peer_list_dedups_target_and_repeats(tmp_path) -> None:
    """peer 原始列表含目标 3 次 -> 去重且不含本人。"""
    creators = {uid: flat_videos(uid, 100 + uid) for uid in range(1, 5)}
    service = BenchmarkService(StubAPI(creators), make_store(tmp_path), clock=Clock(), policy_config={})
    row = service.create_run(1, [1, 1, 1, 2, 2, 3, 4], BenchmarkPolicy(min_videos=3))
    assert row['requested_peers'] == [2, 3, 4]
    final = run_async(service.execute_run(row['id']))
    assert final['result']['reference_count'] == 3
    assert all(entry['uid'] != 1 for entry in final['result']['excluded_peers'])


def test_acceptance_ten_peers_three_fail_only_seven_ranked(tmp_path) -> None:
    """请求 10 同行、3 个失败 -> 只按 7 有效同行排名；失败者不进榜、不被排末尾。"""
    creators = {uid: flat_videos(uid, 100 + uid * 10) for uid in range(1, 12)}
    peers = list(range(2, 12))  # 10 个
    fail = {9, 10, 11}
    _service, final, result = run_service(tmp_path, creators, 1, peers, fail_uids=fail)

    assert result['requested_peer_count'] == 10
    assert result['valid_peer_count'] == 7
    assert len(result['excluded_peers']) == 3
    assert result['comparison_state'] == 'partial'
    assert result['ranked_count'] == 8
    ranked_uids = {row['uid'] for row in result['leaderboard']}
    assert ranked_uids.isdisjoint(fail)


def test_target_failure_is_target_unavailable_not_zero(tmp_path) -> None:
    """目标失败但 peer 成功 -> target_unavailable，且不以 0 排序目标。"""
    creators = {pid: flat_videos(pid, 100 * pid) for pid in (2, 3, 4)}
    creators[1] = {'follower': 1001, 'videos': []}   # 目标没有窗口内稿件
    _service, final, result = run_service(tmp_path, creators, 1, [2, 3, 4])

    assert final['status'] == 'completed'                # run 冻结了
    assert result['comparison_state'] == 'target_unavailable'  # 但比较状态不是 complete
    assert result['target']['rank'] is None
    assert result['target']['metric_value'] is None
    assert result['valid_peer_count'] == 3               # peer 事实仍可展示
    assert len(result['leaderboard']) == 3


def test_zero_valid_peers_is_insufficient_peers(tmp_path) -> None:
    """目标有效但 0 有效 peer -> insufficient_peers（不是 complete）。"""
    creators = {1: flat_videos(1, 200), 2: {'follower': 1002, 'videos': []}}
    _service, final, result = run_service(tmp_path, creators, 1, [2])

    assert final['status'] == 'completed'
    assert result['comparison_state'] == 'insufficient_peers'
    assert result['target']['rank'] is None
    assert result['valid_peer_count'] == 0


def test_refresh_returns_identical_result_and_hash(tmp_path) -> None:
    """刷新（重复读取同 run）不访问 B 站、不重算，名次与 hash 完全一致。"""
    creators = {uid: flat_videos(uid, 100 * uid) for uid in range(1, 6)}
    service, final, result = run_service(tmp_path, creators, 1, [2, 3, 4, 5])
    first = service.read_result(final['id'])
    second = service.read_result(final['id'])
    assert first['result'] == second['result'] == result
    assert first['snapshot_hash'] == second['snapshot_hash'] == result['snapshot_hash']
    assert first['result']['target']['rank'] == result['target']['rank']


def test_observation_span_over_two_hours_disables_ranking(tmp_path) -> None:
    """观测跨度 > 7200s -> 该组不排名（但仍冻结结果并给出明确警告）。"""
    creators = {uid: flat_videos(uid, 100 * uid) for uid in range(1, 4)}
    service, final, result = run_service(
        tmp_path, creators, 1, [2, 3], clock=Clock(base=AS_OF, step=2000)
    )
    assert final['status'] == 'completed'
    assert any(w['code'] == 'observation_span_exceeded' for w in result['warnings'])
    assert result['target']['rank'] is None
    assert result['leaderboard'] == []


# --------------------------------------------------------------------------- #
# 事务 / 迟到写入
# --------------------------------------------------------------------------- #
def test_finalize_write_failure_keeps_run_non_completed(tmp_path, monkeypatch) -> None:
    """finalize 写失败时不产生 success 终态（客户端不能拿未保存榜单冒充可复现 run）。"""
    creators = {uid: flat_videos(uid, 100 * uid) for uid in range(1, 4)}
    store = make_store(tmp_path)
    service = BenchmarkService(StubAPI(creators), store, clock=Clock(), policy_config={})
    row = service.create_run(1, [2, 3], BenchmarkPolicy(min_videos=3))

    monkeypatch.setattr(store, 'finish_run', lambda *a, **kw: False)
    final = run_async(service.execute_run(row['id']))
    assert final['status'] != 'completed'
    assert service.read_result(row['id'])['result'] is None


def test_late_token_sample_and_refinish_are_rejected(tmp_path) -> None:
    """迟到 token 的样本写入与二次 finish 都被拒（终态后不可改写）。"""
    creators = {uid: flat_videos(uid, 100 * uid) for uid in range(1, 4)}
    service, final, _result = run_service(tmp_path, creators, 1, [2, 3])
    store = service.store
    assert final['status'] == 'completed'
    assert store.save_creator_sample(final['id'], 'stale-token', {'uid': 99}) is False
    assert store.finish_run(final['id'], 'stale-token', status='completed') is False
    # 旧结果不能被冒充成新 run
    assert service.read_result(final['id'])['result']['run_id'] == final['id']


def test_retry_creates_new_run_not_overwrite(tmp_path) -> None:
    """重试生成新 run（新 as_of），不把新观测拼回旧 run。"""
    creators = {uid: flat_videos(uid, 100 * uid) for uid in range(1, 4)}
    service, final, _result = run_service(tmp_path, creators, 1, [2, 3])
    new_row = service.retry_as_new_run(final['id'])
    assert new_row['id'] != final['id']
    assert new_row['status'] == 'queued'
    assert service.read_result(final['id'])['result'] is not None  # 旧 run 结果保留


def test_cancel_completed_run_is_rejected(tmp_path) -> None:
    """已 completed 的 run 请求取消 -> 拒绝且不删除结果。"""
    import pytest

    creators = {uid: flat_videos(uid, 100 * uid) for uid in range(1, 4)}
    service, final, _result = run_service(tmp_path, creators, 1, [2, 3])
    with pytest.raises(RuntimeError):
        run_async(service.cancel_run(final['id']))
    assert service.read_result(final['id'])['result'] is not None


def test_create_run_rejects_invalid_inputs(tmp_path) -> None:
    """UID 严格正整数；名单去重剔目标后为空 / exact_raw_tid 缺 raw_tid 都要拒绝。"""
    import pytest

    service = BenchmarkService(StubAPI({}), make_store(tmp_path), clock=Clock(), policy_config={})
    with pytest.raises(ValueError):
        service.create_run(1, [1], BenchmarkPolicy(min_videos=3))          # 剔目标后为空
    with pytest.raises(ValueError):
        service.create_run(True, [2], BenchmarkPolicy(min_videos=3))       # bool 不是 UID
    with pytest.raises(ValueError):
        service.create_run(1, [2], BenchmarkPolicy(content_scope='exact_raw_tid'))  # 缺 raw_tid
