"""01 正确排名 · Web 路由与请求预算测试（规格 §10.1 / §10.2 / §11）。

覆盖用例：
- strict 请求：StrictInt 拒绝 bool / float / str UID，extra 字段被拒，raw_tid 一致性；
- task 终态：queued -> completed，GET 返回 stage + result；
- 同 run 导出/读取：重复读同一 run 不访问 B 站、结果与 snapshot_hash 完全一致；
- 跨 UID 拒绝：run.target_uid != uid -> 409；
- 候选仅发现：/candidates 只发现候选 + 签名 token，不创建任务；
- 限频预算：HTTP 尝试预算耗尽时 RequestBudgetExceeded 原样抛出、不被吞成普通错误；
- 本机防护：缺 token / 跨站 Origin -> 403；
- 404 / 422 / 409 / 503 语义区分。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bilibili.api.client import BilibiliAPICore
from core.database.manager import DatabaseManager
from core.request_budget import AttemptBudget, RequestBudgetExceeded, current_attempt_budget
from modules.self_diagnosis.benchmark.contracts import BenchmarkPolicy
from modules.self_diagnosis.benchmark.service import BenchmarkService
from modules.self_diagnosis.benchmark.store import BenchmarkStore
from web.local_guard import get_local_guard
from web.routers import analysis as analysis_module
from web.routers import benchmark as benchmark_router

DAY_S = 86400
AS_OF = 1_700_000_000


def run_async(coro):
    """用独立事件循环驱动协程。"""
    return asyncio.run(coro)


class StubAPI:
    """契约级 stub API（含榜单页，用于候选发现）。"""

    def __init__(self, creators: Dict[int, Dict[str, Any]], ranking_pages: Optional[Dict[int, List[Any]]] = None):
        """初始化。"""
        self.creators = creators
        self.ranking_pages = ranking_pages or {}
        self.calls: List[tuple] = []

    async def get_user_info(self, uid: int) -> Dict[str, Any]:
        """返回带 meta 的用户资料。"""
        follower = (self.creators.get(uid) or {}).get('follower')
        return {'data': {'name': f'UP{uid}', 'follower': follower or 0},
                '_meta': {'field_status': {'follower': 'ok' if follower is not None else 'missing'}}}

    async def get_user_relation_stat(self, uid: int) -> Dict[str, Any]:
        """relation 接口返回明确粉丝数。"""
        follower = (self.creators.get(uid) or {}).get('follower')
        return {'data': {'follower': follower} if follower is not None else {}}

    async def get_user_videos(self, uid: int, page: int = 1, page_size: int = 30) -> Dict[str, Any]:
        """返回投稿列表。"""
        self.calls.append(('videos', uid, page))
        videos = (self.creators.get(uid) or {}).get('videos') or []
        if page > 1:
            videos = []
        vlist = [{'bvid': b, 'created': created, 'tid': tid, 'play': 0} for b, created, tid, _ in videos]
        return {'data': {'list': {'vlist': vlist}, 'page': {'pn': page, 'ps': page_size, 'count': len(vlist)}}}

    async def get(self, url: str, params: Optional[Dict[str, Any]] = None, need_sign: bool = False, **kwargs):
        """返回详情（已解包 data）。"""
        self.calls.append(('view', (params or {}).get('bvid')))
        bvid = (params or {}).get('bvid')
        for uid, payload in self.creators.items():
            for entry in payload.get('videos') or []:
                if entry[0] == bvid:
                    stat: Dict[str, Any] = {}
                    if entry[3] is not None:
                        stat['view'] = entry[3]
                    return {'bvid': bvid, 'pubdate': entry[1], 'tid': entry[2], 'owner': {'mid': uid}, 'stat': stat}
        return None

    async def get_ranking(self, rid: int, day: int = 7, original: int = 0, page: int = 1):
        """返回预置榜单页。"""
        self.calls.append(('ranking', rid, page))
        return {'data': {'list': list(self.ranking_pages.get(page, []))}}


class FakeTask:
    """可控的假 asyncio.Task，用于确定性测试取消 / 运行中冲突。"""

    def __init__(self, done: bool = False) -> None:
        """初始化。"""
        self._done = done
        self._callbacks: List[Any] = []

    def done(self) -> bool:
        """是否已完成。"""
        return self._done

    def add_done_callback(self, callback) -> None:
        """注册完成回调（已完成的立刻触发）。"""
        if self._done:
            callback(self)
        else:
            self._callbacks.append(callback)

    def cancel(self) -> None:
        """标记完成并触发回调。"""
        self._done = True
        for callback in list(self._callbacks):
            callback(self)

    def __await__(self):
        """允许 await（取消时 service 会 await 该任务）。"""
        async def _noop():
            """空协程。"""
            return None

        return _noop().__await__()


def flat_videos(uid: int, view: int, count: int = 3) -> Dict[str, Any]:
    """构造账号数据（3 条同播放稿件使中位值 = view）。"""
    return {'follower': 1000 + uid,
            'videos': [(f'BV{uid}_{i}', AS_OF - (10 + i) * DAY_S, 4, view) for i in range(count)]}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """构造挂载 benchmark router 的 TestClient + 真实 service（stub API + 临时库）。"""
    creators = {uid: flat_videos(uid, 100 * uid) for uid in range(1, 8)}
    creators[99] = flat_videos(99, 5000)
    api = StubAPI(creators, ranking_pages={
        1: [{'bvid': 'BV1', 'owner': {'mid': 11, 'name': '甲'}},
            {'bvid': 'BV2', 'owner': {'mid': 12, 'name': '乙'}}],
    })
    manager = DatabaseManager(str(tmp_path / 'bench_api.db'))
    store = BenchmarkStore(lambda: manager.get_session())
    service = BenchmarkService(api, store, clock=lambda: AS_OF, policy_config={})

    started: List[Any] = []

    def _fake_start(run_id: str):
        """记录启动并返回可控假任务。"""
        task = FakeTask(done=True)
        started.append(run_id)
        return task

    monkeypatch.setattr(service, 'start_run', _fake_start)
    benchmark_router.set_benchmark_service(service)
    benchmark_router._active_targets.clear()

    app = FastAPI()
    app.include_router(benchmark_router.router, prefix='/api')
    client = TestClient(app, base_url='http://127.0.0.1')
    token = get_local_guard().issue_token()
    yield {
        'client': client, 'service': service, 'api': api, 'token': token,
        'started': started, 'store': store,
    }
    benchmark_router.set_benchmark_service(None)
    benchmark_router._active_targets.clear()


def write_headers(token: str, **extra) -> Dict[str, str]:
    """写请求头（本机 token）。"""
    headers = {'X-Local-Token': token}
    headers.update(extra)
    return headers


def complete_run(service: BenchmarkService, run_id: str) -> Dict[str, Any]:
    """同步执行一个 run 直到终结（绕过后台任务）。"""
    return run_async(service.execute_run(run_id))


# --------------------------------------------------------------------------- #
# strict 请求模型
# --------------------------------------------------------------------------- #
def test_strict_model_rejects_bool_float_string_uids(env) -> None:
    """StrictInt 拒绝 true/false、float 与字符串 UID（前端须先解析成整数）。"""
    client, headers = env['client'], write_headers(env['token'])
    for bad in (True, 1.5, '1'):
        response = client.post('/api/analysis/benchmark/tasks',
                               json={'target_uid': bad, 'peer_uids': [2]}, headers=headers)
        assert response.status_code == 422, bad
    response = client.post('/api/analysis/benchmark/tasks',
                           json={'target_uid': 1, 'peer_uids': [True]}, headers=headers)
    assert response.status_code == 422


def test_strict_model_rejects_unknown_fields_and_scope_mismatch(env) -> None:
    """未知字段一律 422；raw_tid 与 content_scope 必须一致。"""
    client, headers = env['client'], write_headers(env['token'])
    assert client.post('/api/analysis/benchmark/tasks',
                       json={'target_uid': 1, 'peer_uids': [2], 'sneaky': 1},
                       headers=headers).status_code == 422
    assert client.post('/api/analysis/benchmark/tasks',
                       json={'target_uid': 1, 'peer_uids': [2], 'content_scope': 'exact_raw_tid'},
                       headers=headers).status_code == 422
    assert client.post('/api/analysis/benchmark/tasks',
                       json={'target_uid': 1, 'peer_uids': [2], 'raw_tid': 4},
                       headers=headers).status_code == 422


def test_create_task_and_read_terminal_snapshot(env) -> None:
    """创建 -> 执行 -> GET 返回终态 stage + 冻结 result。"""
    client, service, headers = env['client'], env['service'], write_headers(env['token'])
    created = client.post('/api/analysis/benchmark/tasks',
                          json={'target_uid': 1, 'peer_uids': [2, 3, 4, 5, 6]},
                          headers=headers).json()['data']
    assert env['started'] == [created['run_id']]
    complete_run(service, created['run_id'])

    view = client.get(f"/api/analysis/benchmark/tasks/{created['run_id']}").json()['data']
    assert view['status'] == 'completed'
    assert view['progress'] == 100
    assert view['result']['schema_version'] == 3
    assert view['result']['unit'] == 'creator'
    assert view['result']['comparison_state'] == 'complete'
    # 目标 uid=1 -> 中位 100；peer 200/300/400/500/600 -> 名次 = 5 个更高 + 1 = 6
    assert view['result']['target']['rank'] == 6
    assert view['result']['target']['reference_count'] == 5


def test_unknown_run_returns_404(env) -> None:
    """不存在 run -> 404。"""
    assert env['client'].get('/api/analysis/benchmark/tasks/nope').status_code == 404
    assert env['client'].get('/api/analysis/benchmark/runs/nope').status_code == 404


def test_run_endpoint_409_before_freeze(env) -> None:
    """尚未产生冻结结果时 GET /runs 返回 409（任务没假装成功）。"""
    client, headers = env['client'], write_headers(env['token'])
    created = client.post('/api/analysis/benchmark/tasks',
                          json={'target_uid': 1, 'peer_uids': [2]}, headers=headers).json()['data']
    assert client.get(f"/api/analysis/benchmark/runs/{created['run_id']}").status_code == 409


def test_duplicate_active_target_returns_409(env, monkeypatch) -> None:
    """同目标运行中再次创建 -> 409。"""
    client, service, headers = env['client'], env['service'], write_headers(env['token'])
    monkeypatch.setattr(service, 'start_run', lambda run_id: FakeTask(done=False))
    first = client.post('/api/analysis/benchmark/tasks',
                        json={'target_uid': 1, 'peer_uids': [2]}, headers=headers)
    assert first.status_code == 200
    second = client.post('/api/analysis/benchmark/tasks',
                         json={'target_uid': 1, 'peer_uids': [3]}, headers=headers)
    assert second.status_code == 409


# --------------------------------------------------------------------------- #
# 同 run 读取 / 跨 UID
# --------------------------------------------------------------------------- #
def test_same_run_read_is_stable_and_offline(env) -> None:
    """重复读同一 run：结果与 hash 一致，且不再访问 B 站。"""
    client, service, headers = env['client'], env['service'], write_headers(env['token'])
    created = client.post('/api/analysis/benchmark/tasks',
                          json={'target_uid': 1, 'peer_uids': [2, 3, 4, 5, 6]},
                          headers=headers).json()['data']
    complete_run(service, created['run_id'])
    calls_after_run = len(env['api'].calls)

    first = client.get(f"/api/analysis/benchmark/runs/{created['run_id']}").json()['data']
    second = client.get(f"/api/analysis/benchmark/runs/{created['run_id']}").json()['data']
    assert first['result'] == second['result']
    assert first['snapshot_hash'] == second['snapshot_hash']
    assert len(env['api'].calls) == calls_after_run       # 读取不触网


def test_cross_uid_creator_ranking_is_rejected(env) -> None:
    """跨 UID 读取冻结排名 -> 409（拒绝把别人的名次塞进自己的报告）。"""
    client, service, headers = env['client'], env['service'], write_headers(env['token'])
    created = client.post('/api/analysis/benchmark/tasks',
                          json={'target_uid': 1, 'peer_uids': [2, 3, 4]}, headers=headers).json()['data']
    complete_run(service, created['run_id'])

    with pytest.raises(HTTPException) as excinfo:
        analysis_module._load_creator_ranking(999, created['run_id'])
    assert excinfo.value.status_code == 409


def test_creator_ranking_same_uid_returns_frozen_result(env) -> None:
    """同 UID 读取返回同一 run 的冻结结果（附 run_id / as_of / hash）。"""
    client, service, headers = env['client'], env['service'], write_headers(env['token'])
    created = client.post('/api/analysis/benchmark/tasks',
                          json={'target_uid': 1, 'peer_uids': [2, 3, 4]}, headers=headers).json()['data']
    complete_run(service, created['run_id'])

    result, status = analysis_module._load_creator_ranking(1, created['run_id'])
    assert status == 'completed'
    assert result['benchmark_run_id'] == created['run_id']
    assert result['unit'] == 'creator'


def test_not_requested_when_no_run_id() -> None:
    """不传 run_id -> not_requested（不是错误、不删除入口）。"""
    result, status = analysis_module._load_creator_ranking(1, None)
    assert (result, status) == (None, 'not_requested')


# --------------------------------------------------------------------------- #
# 候选发现 + 来源证明
# --------------------------------------------------------------------------- #
def test_candidates_only_discover_not_rank(env) -> None:
    """候选发现只返回候选与签名 token，不创建排名任务。"""
    client, headers = env['client'], write_headers(env['token'])
    body = client.post('/api/analysis/benchmark/candidates',
                       json={'taxonomy': 'pid_v2', 'rid': 4, 'limit': 10},
                       headers=headers).json()
    data = body['data']
    assert [c['uid'] for c in data['candidates']] == [11, 12]
    assert data['discovery_token']
    assert 'run_id' not in data
    assert env['started'] == []


def test_discovery_token_verified_and_peer_subset_enforced(env) -> None:
    """带合法 token 且 peer 为候选子集 -> ranking_discovered_peer_set；否则 422。"""
    client, headers = env['client'], write_headers(env['token'])
    token = client.post('/api/analysis/benchmark/candidates',
                        json={'rid': 4, 'limit': 10}, headers=headers).json()['data']['discovery_token']

    ok = client.post('/api/analysis/benchmark/tasks',
                     json={'target_uid': 1, 'peer_uids': [11], 'discovery_token': token},
                     headers=headers).json()['data']
    assert ok['peer_source'] == 'ranking_discovered_peer_set'

    not_subset = client.post('/api/analysis/benchmark/tasks',
                             json={'target_uid': 1, 'peer_uids': [77], 'discovery_token': token},
                             headers=headers)
    assert not_subset.status_code == 422

    tampered = client.post('/api/analysis/benchmark/tasks',
                           json={'target_uid': 1, 'peer_uids': [11], 'discovery_token': token + 'x'},
                           headers=headers)
    assert tampered.status_code == 422


def test_candidates_without_token_is_manual_source(env) -> None:
    """无 token -> peer_source=manual_peer_set（客户端不能自行填写已验证来源）。"""
    client, headers = env['client'], write_headers(env['token'])
    data = client.post('/api/analysis/benchmark/tasks',
                       json={'target_uid': 1, 'peer_uids': [2]},
                       headers=headers).json()['data']
    assert data['peer_source'] == 'manual_peer_set'


# --------------------------------------------------------------------------- #
# 取消 / 重试
# --------------------------------------------------------------------------- #
def test_cancel_queued_run(env, monkeypatch) -> None:
    """取消 queued run -> cancelled，不删除任何结果。"""
    client, service, headers = env['client'], env['service'], write_headers(env['token'])
    monkeypatch.setattr(service, 'start_run', lambda run_id: FakeTask(done=False))
    created = client.post('/api/analysis/benchmark/tasks',
                          json={'target_uid': 1, 'peer_uids': [2]}, headers=headers).json()['data']
    body = client.post(f"/api/analysis/benchmark/tasks/{created['run_id']}/cancel", headers=headers).json()
    assert body['data']['status'] == 'cancelled'


def test_cancel_completed_run_returns_409(env) -> None:
    """已 completed 请求取消 -> 409，且结果保留。"""
    client, service, headers = env['client'], env['service'], write_headers(env['token'])
    created = client.post('/api/analysis/benchmark/tasks',
                          json={'target_uid': 1, 'peer_uids': [2, 3, 4]}, headers=headers).json()['data']
    complete_run(service, created['run_id'])
    assert client.post(f"/api/analysis/benchmark/tasks/{created['run_id']}/cancel",
                       headers=headers).status_code == 409
    assert client.get(f"/api/analysis/benchmark/runs/{created['run_id']}").status_code == 200


def test_retry_active_run_returns_409(env) -> None:
    """queued 状态下重试 -> 409（不把新观测拼回旧 run）。"""
    client, headers = env['client'], write_headers(env['token'])
    created = client.post('/api/analysis/benchmark/tasks',
                          json={'target_uid': 1, 'peer_uids': [2]}, headers=headers).json()['data']
    assert client.post(f"/api/analysis/benchmark/runs/{created['run_id']}/retry",
                       headers=headers).status_code == 409


def test_retry_completed_run_creates_new_run(env) -> None:
    """completed 后重试 -> 新 run（新 as_of），旧结果保留。"""
    client, service, headers = env['client'], env['service'], write_headers(env['token'])
    created = client.post('/api/analysis/benchmark/tasks',
                          json={'target_uid': 1, 'peer_uids': [2, 3, 4]}, headers=headers).json()['data']
    complete_run(service, created['run_id'])
    retried = client.post(f"/api/analysis/benchmark/runs/{created['run_id']}/retry", headers=headers).json()['data']
    assert retried['run_id'] != created['run_id']
    assert retried['retried_from'] == created['run_id']


# --------------------------------------------------------------------------- #
# 本机防护
# --------------------------------------------------------------------------- #
def test_write_requires_local_token(env) -> None:
    """写端点缺 token -> 403（无 Origin 的 CLI 请求同样须带 token）。"""
    response = env['client'].post('/api/analysis/benchmark/tasks',
                                  json={'target_uid': 1, 'peer_uids': [2]})
    assert response.status_code == 403


def test_cross_site_origin_is_blocked(env) -> None:
    """跨站 Origin -> 403（防跨站发起大规模采集）。"""
    response = env['client'].post(
        '/api/analysis/benchmark/tasks',
        json={'target_uid': 1, 'peer_uids': [2]},
        headers=write_headers(env['token'], origin='http://evil.example.com'),
    )
    assert response.status_code == 403
    assert env['started'] == []


def test_service_unavailable_returns_503() -> None:
    """未注入 service -> 503。"""
    benchmark_router.set_benchmark_service(None)
    app = FastAPI()
    app.include_router(benchmark_router.router, prefix='/api')
    client = TestClient(app, base_url='http://127.0.0.1')
    response = client.post('/api/analysis/benchmark/tasks',
                           json={'target_uid': 1, 'peer_uids': [2]},
                           headers={'X-Local-Token': get_local_guard().issue_token()})
    assert response.status_code == 503


# --------------------------------------------------------------------------- #
# 限频预算（HTTP 尝试预算）
# --------------------------------------------------------------------------- #
class _FakeResponse:
    """最小 aiohttp 响应替身。"""

    status = 200
    headers: Dict[str, str] = {}

    async def json(self) -> Dict[str, Any]:
        """返回成功业务码。"""
        return {'code': 0, 'data': {'ok': True}}


class _FakeRequestContext:
    """异步上下文替身。"""

    async def __aenter__(self) -> _FakeResponse:
        """返回假响应。"""
        return _FakeResponse()

    async def __aexit__(self, *exc) -> bool:
        """不吞异常。"""
        return False


class _FakeSession:
    """最小 aiohttp 会话替身。"""

    closed = False

    def request(self, **kwargs) -> _FakeRequestContext:
        """返回假请求上下文。"""
        return _FakeRequestContext()

    def get(self, *args, **kwargs) -> _FakeRequestContext:
        """返回假请求上下文。"""
        return _FakeRequestContext()


def test_attempt_budget_counts_and_raises() -> None:
    """AttemptBudget 在超限时抛 RequestBudgetExceeded，而不是静默放行。"""
    budget = AttemptBudget(max_attempts=2, deadline_monotonic=time.monotonic() + 60)
    budget.before_send()
    budget.before_send()
    assert budget.attempts == 2
    with pytest.raises(RequestBudgetExceeded):
        budget.before_send()

    expired = AttemptBudget(max_attempts=5, deadline_monotonic=time.monotonic() - 1)
    with pytest.raises(RequestBudgetExceeded):
        expired.before_send()


def test_client_hook_counts_real_attempts_and_preserves_exception_type() -> None:
    """client 每次真实 HTTP 发送前扣预算；预算耗尽原样抛出、不进入重试。"""
    api = BilibiliAPICore()
    api.session = _FakeSession()
    budget = AttemptBudget(max_attempts=1, deadline_monotonic=time.monotonic() + 60)
    token = current_attempt_budget.set(budget)
    try:
        assert run_async(api.get('/x/web-interface/view')) == {'ok': True}
        assert budget.attempts == 1
        with pytest.raises(RequestBudgetExceeded):
            run_async(api.get('/x/web-interface/view'))
    finally:
        current_attempt_budget.reset(token)
    # 被抛出的预算异常不得被转成普通 APIError 后重试（尝试数仍为 1）
    assert budget.attempts == 1


def test_client_without_budget_keeps_old_behavior() -> None:
    """默认上下文 None：旧调用完全不计数、行为不变。"""
    api = BilibiliAPICore()
    api.session = _FakeSession()
    assert current_attempt_budget.get() is None
    for _ in range(5):
        assert run_async(api.get('/x/web-interface/view')) == {'ok': True}


def test_service_sets_and_resets_context_budget(env, monkeypatch) -> None:
    """service 在采集协程内设置预算、结束后 reset，不污染后续普通请求。"""
    client, service, headers = env['client'], env['service'], write_headers(env['token'])
    created = client.post('/api/analysis/benchmark/tasks',
                          json={'target_uid': 1, 'peer_uids': [2]}, headers=headers).json()['data']
    complete_run(service, created['run_id'])
    assert current_attempt_budget.get() is None
