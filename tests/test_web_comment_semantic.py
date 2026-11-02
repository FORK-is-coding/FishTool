"""web.routers.comment_semantic 语义折叠接口测试（D 段 · API/UI）。

覆盖：
- 响应信封 {success, data} 与端点编排（preflight/preferences/tasks/result/cancel/resume/latest/decisions）
- 本机写防护：缺 token -> 403；带 token 放行
- typed exceptions -> HTTP 映射：404/409/429/400/502/503
- model_identity 净化：PUT 偏好只透传 {api_base, model}，剔除夹带的 api_key
- 未装配 service -> 503
- public_state 不含 snapshot/checkpoint（router 层不回传内部字段）

策略：TestClient 挂真实 router；service/store 全用契约级假对象，绝不触网/触库。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.comment.semantic.contracts import (
    ProviderErrorCategory,
    SemanticBudgetExceeded,
    SemanticConflict,
    SemanticNotFound,
    SemanticProviderError,
    SemanticValidationError,
)
from web.local_guard import get_local_guard
from web.routers import comment_semantic as router_module


# ---------------------------------------------------------------------------
# 契约级假对象
# ---------------------------------------------------------------------------
class _FakeStore:
    """只记录调用并回放预置返回值的假 store。"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.preference_value: Dict[str, Any] = {
            'auto_fold': False, 'revision': 0, 'consent_version': None, 'model_identity': None,
        }
        self.set_preference_error: Optional[Exception] = None
        self.last_set_preference_kwargs: Dict[str, Any] = {}
        self.latest_value: Optional[Dict[str, Any]] = None
        self.decide_value: Dict[str, Any] = {'revision': 1, 'preference': {'revision': 1}}
        self.decide_error: Optional[Exception] = None

    def preference(self, bvid: str) -> Dict[str, Any]:
        self.calls.append(('preference', bvid))
        return self.preference_value

    def set_preference(self, bvid, expected_revision, auto_fold=None,
                       consent_version=None, model_identity=None):
        self.last_set_preference_kwargs = {
            'bvid': bvid, 'expected_revision': expected_revision, 'auto_fold': auto_fold,
            'consent_version': consent_version, 'model_identity': model_identity,
        }
        if self.set_preference_error is not None:
            raise self.set_preference_error
        return {
            'auto_fold': bool(auto_fold), 'revision': expected_revision + 1,
            'consent_version': consent_version, 'model_identity': model_identity,
        }

    def latest_task(self, bvid: str):
        self.calls.append(('latest_task', bvid))
        return self.latest_value

    def decide(self, bvid, task_id, action, expected_revision,
              group_id=None, rpid=None, request_key=None):
        self.calls.append(('decide', bvid, task_id, action, group_id, rpid))
        if self.decide_error is not None:
            raise self.decide_error
        return self.decide_value


class _FakeService:
    """契约级假 SemanticService：持有假 store，方法可注入异常。"""

    def __init__(self) -> None:
        self.store = _FakeStore()
        self.preflight_value: Dict[str, Any] = {
            'counts': {'raw_total': 10, 'selected': 8, 'eligible': 6},
            'scope': {'limit': 200, 'order': 'newest'},
            'model_configured': True,
            'provider_identity': {'api_base': 'https://api.example.com', 'model': 'demo'},
            'consent_needed': True,
            'consent_version': 'semantic-fold-v1',
            'limits': {'max_attempts': 20},
        }
        self.preflight_error: Optional[Exception] = None
        self.start_value: Dict[str, Any] = {
            'task_id': 't1', 'bvid': 'BV1', 'status': 'queued', 'revision': 0,
        }
        self.start_error: Optional[Exception] = None
        self.status_value: Optional[Dict[str, Any]] = {
            'task_id': 't1', 'bvid': 'BV1', 'status': 'running', 'revision': 1,
            'progress': {'percent': 40},
        }
        self.results_value: Optional[Dict[str, Any]] = {
            'task': {'task_id': 't1', 'status': 'completed'},
            'groups': [], 'items': [], 'pagination': {'page': 1, 'total_pages': 1},
            'preference': {'revision': 2},
        }
        self.cancel_value: Dict[str, Any] = {'task_id': 't1', 'status': 'cancelled', 'revision': 2}
        self.resume_value: Dict[str, Any] = {'task_id': 't1', 'status': 'queued', 'revision': 2}

    async def preflight(self, bvid, limit=200):
        if self.preflight_error is not None:
            raise self.preflight_error
        return self.preflight_value

    async def start(self, bvid, limit, consent_version, request_key):
        if self.start_error is not None:
            raise self.start_error
        return self.start_value

    def status(self, task_id):
        return self.status_value

    def results(self, task_id, page=1, page_size=50):
        return self.results_value

    async def cancel(self, task_id, expected_revision):
        return self.cancel_value

    async def resume(self, task_id, expected_revision, acknowledge_unknown=False):
        return self.resume_value


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def service() -> _FakeService:
    return _FakeService()


@pytest.fixture()
def client(service):
    """挂载真实 router 的 TestClient（base_url 为回环，满足同源校验）。"""
    router_module.set_semantic_service(service)
    app = FastAPI()
    app.include_router(router_module.router, prefix='/api')
    test_client = TestClient(app, base_url='http://127.0.0.1')
    yield test_client
    router_module.set_semantic_service(None)


@pytest.fixture()
def token() -> str:
    return get_local_guard().issue_token()


def _wh(token: str) -> Dict[str, str]:
    return {'X-Local-Token': token}


# ---------------------------------------------------------------------------
# local-token / preflight / preferences
# ---------------------------------------------------------------------------
def test_local_token_issued(client):
    resp = client.get('/api/comment/semantic/local-token')
    assert resp.status_code == 200
    body = resp.json()
    assert body['success'] is True
    assert body['data']['header'] == 'X-Local-Token'
    assert body['data']['token']


def test_preflight_returns_counts_scope_identity(client, service):
    resp = client.get('/api/comment/semantic/preflight?bvid=BV1&limit=200')
    assert resp.status_code == 200
    data = resp.json()['data']
    assert data['counts']['raw_total'] == 10
    assert data['provider_identity'] == {'api_base': 'https://api.example.com', 'model': 'demo'}
    # 不回传完整原始快照（只给计数/范围）。
    assert 'items' not in data and 'snapshot' not in data


def test_preflight_unknown_video_404(client, service):
    service.preflight_error = SemanticNotFound('unknown video')
    resp = client.get('/api/comment/semantic/preflight?bvid=BVX')
    assert resp.status_code == 404
    assert resp.json()['detail']['error_code'] == 'not_found'


def test_get_preferences_delegates_store(client, service):
    resp = client.get('/api/comment/semantic/preferences?bvid=BV1')
    assert resp.status_code == 200
    assert resp.json()['data']['revision'] == 0
    assert ('preference', 'BV1') in service.store.calls


def test_put_preferences_requires_token(client):
    resp = client.put('/api/comment/semantic/preferences',
                      json={'bvid': 'BV1', 'expected_revision': 0, 'auto_fold': True})
    assert resp.status_code == 403


def test_put_preferences_sanitizes_model_identity(client, service, token):
    # 客户端夹带 api_key：必须被丢弃，只透传 api_base/model。
    resp = client.put(
        '/api/comment/semantic/preferences',
        json={
            'bvid': 'BV1', 'expected_revision': 0, 'consent_version': 'semantic-fold-v1',
            'model_identity': {'api_base': 'https://a.com', 'model': 'm', 'api_key': 'SECRET'},
        },
        headers=_wh(token),
    )
    assert resp.status_code == 200
    passed = service.store.last_set_preference_kwargs['model_identity']
    assert passed == {'api_base': 'https://a.com', 'model': 'm'}
    assert 'api_key' not in passed


def test_put_preferences_rejects_bad_model_identity(client, token):
    resp = client.put(
        '/api/comment/semantic/preferences',
        json={'bvid': 'BV1', 'expected_revision': 0, 'model_identity': {'model': 'm'}},
        headers=_wh(token),
    )
    assert resp.status_code == 400
    assert resp.json()['detail']['error_code'] == 'validation_error'


def test_put_preferences_conflict_409(client, service, token):
    service.store.set_preference_error = SemanticConflict('stale revision')
    resp = client.put(
        '/api/comment/semantic/preferences',
        json={'bvid': 'BV1', 'expected_revision': 5, 'auto_fold': True},
        headers=_wh(token),
    )
    assert resp.status_code == 409
    assert resp.json()['detail']['error_code'] == 'revision_conflict'


# ---------------------------------------------------------------------------
# tasks lifecycle
# ---------------------------------------------------------------------------
def test_create_task_requires_token(client):
    resp = client.post('/api/comment/semantic/tasks',
                       json={'bvid': 'BV1', 'consent_version': 'semantic-fold-v1', 'request_key': 'k1'})
    assert resp.status_code == 403


def test_create_task_success(client, token):
    resp = client.post(
        '/api/comment/semantic/tasks',
        json={'bvid': 'BV1', 'limit': 200, 'consent_version': 'semantic-fold-v1', 'request_key': 'k1'},
        headers=_wh(token),
    )
    assert resp.status_code == 200
    assert resp.json()['data']['task_id'] == 't1'


def test_create_task_budget_429(client, service, token):
    service.start_error = SemanticBudgetExceeded('daily budget exhausted')
    resp = client.post(
        '/api/comment/semantic/tasks',
        json={'bvid': 'BV1', 'consent_version': 'semantic-fold-v1', 'request_key': 'k1'},
        headers=_wh(token),
    )
    assert resp.status_code == 429
    assert resp.json()['detail']['error_code'] == 'budget_exceeded'


def test_create_task_provider_not_configured_503(client, service, token):
    service.start_error = SemanticProviderError('not configured', ProviderErrorCategory.NOT_CONFIGURED)
    resp = client.post(
        '/api/comment/semantic/tasks',
        json={'bvid': 'BV1', 'consent_version': 'semantic-fold-v1', 'request_key': 'k1'},
        headers=_wh(token),
    )
    assert resp.status_code == 503
    assert resp.json()['detail']['error_code'] == ProviderErrorCategory.NOT_CONFIGURED


def test_create_task_provider_server_502(client, service, token):
    service.start_error = SemanticProviderError('upstream 500', ProviderErrorCategory.SERVER)
    resp = client.post(
        '/api/comment/semantic/tasks',
        json={'bvid': 'BV1', 'consent_version': 'semantic-fold-v1', 'request_key': 'k1'},
        headers=_wh(token),
    )
    assert resp.status_code == 502


def test_get_task_public_state_no_internal_fields(client, service):
    resp = client.get('/api/comment/semantic/tasks/t1')
    assert resp.status_code == 200
    data = resp.json()['data']
    assert data['status'] == 'running'
    # router 透传 service.status()，不注入 snapshot/checkpoint（假 service 也不含）。
    assert 'snapshot' not in data and 'checkpoint' not in data


def test_get_task_not_found_404(client, service):
    service.status_value = None
    resp = client.get('/api/comment/semantic/tasks/missing')
    assert resp.status_code == 404


def test_get_result_includes_items_and_groups(client):
    resp = client.get('/api/comment/semantic/tasks/t1/result?page=1&page_size=50')
    assert resp.status_code == 200
    data = resp.json()['data']
    assert 'groups' in data and 'items' in data and 'pagination' in data


def test_cancel_task_validation_on_missing_revision(client, token):
    resp = client.post('/api/comment/semantic/tasks/t1/cancel', json={}, headers=_wh(token))
    assert resp.status_code == 422  # pydantic：expected_revision 必填


def test_cancel_task_success(client, token):
    resp = client.post('/api/comment/semantic/tasks/t1/cancel',
                       json={'expected_revision': 1}, headers=_wh(token))
    assert resp.status_code == 200
    assert resp.json()['data']['status'] == 'cancelled'


def test_resume_task_success(client, token):
    resp = client.post('/api/comment/semantic/tasks/t1/resume',
                       json={'expected_revision': 1, 'acknowledge_unknown': True}, headers=_wh(token))
    assert resp.status_code == 200
    assert resp.json()['data']['status'] == 'queued'


def test_latest_none_when_no_task(client, service):
    service.store.latest_value = None
    resp = client.get('/api/comment/semantic/latest?bvid=BV1')
    assert resp.status_code == 200
    assert resp.json()['data'] is None


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------
def test_decision_requires_token(client):
    resp = client.post('/api/comment/semantic/decisions',
                       json={'bvid': 'BV1', 'task_id': 't1', 'action': 'undo_group',
                             'expected_revision': 1, 'group_id': 'g1', 'request_key': 'r1'})
    assert resp.status_code == 403


def test_decision_rejects_unknown_action(client, token):
    resp = client.post(
        '/api/comment/semantic/decisions',
        json={'bvid': 'BV1', 'task_id': 't1', 'action': 'nuke',
              'expected_revision': 1, 'request_key': 'r1'},
        headers=_wh(token),
    )
    assert resp.status_code == 400
    assert resp.json()['detail']['error_code'] == 'validation_error'


def test_decision_success_returns_revision(client, service, token):
    resp = client.post(
        '/api/comment/semantic/decisions',
        json={'bvid': 'BV1', 'task_id': 't1', 'action': 'undo_group',
              'expected_revision': 1, 'group_id': 'g1', 'request_key': 'r1'},
        headers=_wh(token),
    )
    assert resp.status_code == 200
    assert resp.json()['data']['revision'] == 1
    assert any(c[0] == 'decide' for c in service.store.calls)


def test_decision_stale_group_409(client, service, token):
    service.store.decide_error = SemanticConflict('stale group')
    resp = client.post(
        '/api/comment/semantic/decisions',
        json={'bvid': 'BV1', 'task_id': 't1', 'action': 'split_member',
              'expected_revision': 1, 'rpid': '123', 'request_key': 'r2'},
        headers=_wh(token),
    )
    assert resp.status_code == 409


def test_validation_error_maps_400(client, service, token):
    service.store.decide_error = SemanticValidationError('bad id', reason='unknown_id')
    resp = client.post(
        '/api/comment/semantic/decisions',
        json={'bvid': 'BV1', 'task_id': 't1', 'action': 'allow_member',
              'expected_revision': 1, 'rpid': '9', 'request_key': 'r3'},
        headers=_wh(token),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# service 未装配
# ---------------------------------------------------------------------------
def test_service_unavailable_503(token):
    router_module.set_semantic_service(None)
    app = FastAPI()
    app.include_router(router_module.router, prefix='/api')
    local = TestClient(app, base_url='http://127.0.0.1')
    resp = local.get('/api/comment/semantic/preflight?bvid=BV1')
    assert resp.status_code == 503
    assert resp.json()['detail']['error_code'] == 'semantic_service_unavailable'
