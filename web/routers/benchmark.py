"""01 正确排名 Web 路由（规格 §10.1）。

路径前缀 ``/analysis/benchmark``，由 ``web/main.py`` include 到 ``/api``：:

    POST /api/analysis/benchmark/candidates   # 分区候选发现（不等于排名）
    POST /api/analysis/benchmark/tasks        # target_uid + peer_uids + policy
    GET  /api/analysis/benchmark/tasks/{id}   # 持久化状态 + stage + 终态 result
    POST /api/analysis/benchmark/tasks/{id}/cancel
    GET  /api/analysis/benchmark/runs/{id}    # 冻结结果，可重新打开
    POST /api/analysis/benchmark/runs/{id}/retry

router 只做校验 / 调用：统计公式在 ``benchmark.service``，采集在 ``benchmark.collector``，
**不在 router 里写统计逻辑**。状态码语义：404 不存在、422 参数、409 冲突、503 服务/数据库不可用；
样本不足是终态业务结果，不统一抛 500。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from core.logger import get_logger
from modules.self_diagnosis.benchmark.collector import discover_candidates
from web.local_guard import LOCAL_TOKEN_HEADER, get_local_guard, require_local_read, require_local_write

logger = get_logger(__name__)

router = APIRouter(prefix='/analysis/benchmark', tags=['creator-ranking'])

#: discovery_token 最长有效 1 小时（规格 §10.1）
DISCOVERY_TTL_S = 3600

#: 进程内注入的排名服务（由 web/main.py 组装后注入，modules 不反向 import web）
_service: Optional[Any] = None
#: 正在运行的 target（用于「运行中重复冲突」409）
_active_targets: set = set()
#: 无外部密钥时的进程内随机密钥（token 只在本次进程内有效）
_runtime_secret: Optional[bytes] = None


def set_benchmark_service(service: Any) -> None:
    """注入 BenchmarkService（lifespan 组装后调用）。

    Args:
        service: BenchmarkService 实例；None 表示未启用。

    Returns:
        无。
    """
    global _service
    _service = service


def get_benchmark_service() -> Any:
    """返回当前注入的 BenchmarkService。

    Returns:
        Any: BenchmarkService 实例。

    Raises:
        HTTPException: 503 服务未启用。
    """
    if _service is None:
        raise HTTPException(status_code=503, detail='benchmark_service_unavailable')
    return _service


# --------------------------------------------------------------------------
# 来源证明：服务器 HMAC 签名
# --------------------------------------------------------------------------
def _discovery_secret() -> bytes:
    """返回 discovery_token 的 HMAC 密钥。

    优先读环境变量 ``FISHTOOL_DISCOVERY_HMAC_KEY``（放本机秘密区，不进日志）；
    未配置时使用进程内随机密钥，token 仅本次进程有效。

    Returns:
        bytes: 密钥字节串。
    """
    import os

    raw = os.environ.get('FISHTOOL_DISCOVERY_HMAC_KEY')
    if raw:
        return raw.encode('utf-8')
    global _runtime_secret
    if _runtime_secret is None:
        _runtime_secret = secrets.token_bytes(32)
    return _runtime_secret


def _b64url(raw: bytes) -> str:
    """URL-safe base64 编码（去 padding）。"""
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def sign_discovery_token(payload: Dict[str, Any]) -> str:
    """对候选发现证据做 HMAC-SHA256 签名，生成 discovery_token。

    Args:
        payload: 含 uids / taxonomy / rid / created_s / exp_s 的字典。

    Returns:
        str: ``body.signature`` 形式的 token。
    """
    body = _b64url(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))
    signature = hmac.new(_discovery_secret(), body.encode('ascii'), hashlib.sha256).hexdigest()
    return f'{body}.{signature}'


def verify_discovery_token(token: str) -> Dict[str, Any]:
    """校验 discovery_token 的签名与期限。

    Args:
        token: 客户端回传的 token。

    Returns:
        Dict[str, Any]: 校验通过的 payload。

    Raises:
        ValueError: 格式非法 / 签名不符 / 已过期。
    """
    if not token or '.' not in token:
        raise ValueError('invalid_token')
    body, signature = token.rsplit('.', 1)
    expected = hmac.new(_discovery_secret(), body.encode('ascii'), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError('bad_signature')
    padded = body + '=' * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8'))
    except Exception as exc:  # noqa: BLE001
        raise ValueError('bad_payload') from exc
    if not isinstance(payload, dict):
        raise ValueError('bad_payload')
    if int(payload.get('exp_s') or 0) < int(time.time()):
        raise ValueError('token_expired')
    return payload


# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------
class BenchmarkRequest(BaseModel):
    """排名任务请求（严格模型，照规格 §10.1 抄）。"""

    model_config = ConfigDict(extra='forbid')

    target_uid: StrictInt = Field(gt=0)
    peer_uids: List[StrictInt] = Field(min_length=1, max_length=50)
    metric_version: Literal['recent10_age7_30_median_views_v1'] = 'recent10_age7_30_median_views_v1'
    content_scope: Literal['all_public_uploads', 'exact_raw_tid'] = 'all_public_uploads'
    raw_tid: Optional[StrictInt] = Field(default=None, gt=0)
    size_match: Literal['none', 'same_follower_band'] = 'none'
    discovery_token: Optional[str] = Field(default=None, max_length=8192)

    @model_validator(mode='after')
    def validate_scope(self):
        """跨字段一致性校验（raw_tid 与 content_scope 匹配、peer UID 严格正整数）。"""
        if self.content_scope == 'exact_raw_tid' and self.raw_tid is None:
            raise ValueError('raw_tid_required')
        if self.content_scope == 'all_public_uploads' and self.raw_tid is not None:
            raise ValueError('unexpected_raw_tid')
        if any(isinstance(x, bool) or x <= 0 for x in self.peer_uids):
            raise ValueError('invalid_peer_uid')
        return self


class CandidateDiscoveryRequest(BaseModel):
    """候选发现请求（不等于排名）。"""

    model_config = ConfigDict(extra='forbid')

    taxonomy: Literal['pid_v2', 'legacy_tid'] = 'pid_v2'
    rid: StrictInt = Field(gt=0)
    day: Optional[StrictInt] = Field(default=None, gt=0)
    original: bool = False
    limit: StrictInt = Field(default=20, ge=1, le=50)


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------
@router.get('/local-token', dependencies=[Depends(require_local_read)])
async def issue_local_token() -> Dict[str, Any]:
    """向同源页面 / CLI 下发本机 session token（不写日志、不进 URL）。

    Returns:
        Dict[str, Any]: {success, data:{token, header}}。
    """
    return {'success': True, 'data': {'token': get_local_guard().issue_token(), 'header': LOCAL_TOKEN_HEADER}}


@router.post('/candidates', dependencies=[Depends(require_local_write)])
async def discover_candidate_authors(request: CandidateDiscoveryRequest) -> Dict[str, Any]:
    """分区候选发现：只返回候选作者供用户确认，**不等于排名**。

    Args:
        request: 候选发现请求。

    Returns:
        Dict[str, Any]: 候选 + 服务器签名的 discovery_token + 来源证据。
    """
    try:
        service = get_benchmark_service()
        scope = {
            'taxonomy': request.taxonomy,
            'rid': request.rid,
            'day': request.day or 7,
            'original': int(bool(request.original)),
        }
        discovery = await discover_candidates(service.api, scope, request.limit)
        now_s = int(time.time())
        payload = {
            'uids': [candidate['uid'] for candidate in discovery['candidates']],
            'taxonomy': scope['taxonomy'],
            'rid': scope['rid'],
            'day': scope['day'],
            'created_s': now_s,
            'exp_s': now_s + DISCOVERY_TTL_S,
        }
        return {
            'success': True,
            'data': {
                'candidates': discovery['candidates'],
                'discovery_token': sign_discovery_token(payload),
                'token_expires_s': payload['exp_s'],
                'source_changed': discovery['source_changed'],
                'truncated': discovery['truncated'],
                'source_evidence': {
                    key: discovery[key]
                    for key in ('endpoint', 'params', 'pages_scanned', 'logical_calls', 'failures', 'discovered_s')
                },
            },
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("[排名] 候选发现失败: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post('/tasks', dependencies=[Depends(require_local_write)])
async def create_benchmark_task(request: BenchmarkRequest) -> Dict[str, Any]:
    """创建排名任务并后台执行。

    Args:
        request: 排名任务请求。

    Returns:
        Dict[str, Any]: 新建 run 的摘要。

    Raises:
        HTTPException: 422 参数 / token 非法；409 同目标运行中；503 数据库不可用。
    """
    service = get_benchmark_service()

    peer_source = 'manual_peer_set'
    extra_policy: Optional[Dict[str, Any]] = None
    if request.discovery_token:
        try:
            payload = verify_discovery_token(request.discovery_token)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f'invalid_discovery_token:{exc}')
        allowed_uids = {int(uid) for uid in payload.get('uids') or []}
        if not set(request.peer_uids).issubset(allowed_uids):
            raise HTTPException(status_code=422, detail='peer_not_in_discovered_set')
        peer_source = 'ranking_discovered_peer_set'
        # 证据整体固化进 run（不新增来源表）
        extra_policy = {
            'taxonomy': payload.get('taxonomy'),
            'rid': payload.get('rid'),
            'day': payload.get('day'),
            'created_s': payload.get('created_s'),
            'exp_s': payload.get('exp_s'),
        }

    if request.target_uid in _active_targets:
        raise HTTPException(status_code=409, detail='target_run_in_progress')

    policy = {
        'version': request.metric_version,
        'content_scope': request.content_scope,
        'raw_tid': request.raw_tid,
        'size_match': request.size_match,
        'peer_source': peer_source,
    }
    try:
        row = service.create_run(request.target_uid, request.peer_uids, policy, extra_policy=extra_policy)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - 数据库不可用统一 503
        logger.error("[排名] 创建 run 失败: %s", exc)
        raise HTTPException(status_code=503, detail='database_unavailable')

    run_id = row.get('id')
    target_uid = int(row.get('target_uid'))
    _active_targets.add(target_uid)
    try:
        task = service.start_run(run_id)
        task.add_done_callback(lambda _finished: _active_targets.discard(target_uid))
    except Exception as exc:  # noqa: BLE001
        _active_targets.discard(target_uid)
        logger.error("[排名] 启动 run 失败: %s", exc)
        raise HTTPException(status_code=503, detail='benchmark_start_failed')

    return {
        'success': True,
        'data': {
            'run_id': run_id,
            'status': row.get('status'),
            'target_uid': target_uid,
            'requested_peers': row.get('requested_peers'),
            'requested_peer_count': len(row.get('requested_peers') or []),
            'peer_source': (row.get('policy') or {}).get('peer_source', peer_source),
            'selection_as_of_s': row.get('selection_as_of_s'),
        },
    }


@router.get('/tasks/{run_id}', dependencies=[Depends(require_local_read)])
async def get_benchmark_task(run_id: str) -> Dict[str, Any]:
    """返回任务持久化状态 + stage + 终态 result。

    Args:
        run_id: run 标识。

    Returns:
        Dict[str, Any]: 任务视图。

    Raises:
        HTTPException: 404 不存在。
    """
    service = get_benchmark_service()
    view = service.get_task_view(run_id)
    if view is None:
        raise HTTPException(status_code=404, detail='run_not_found')
    return {'success': True, 'data': view}


@router.post('/tasks/{run_id}/cancel', dependencies=[Depends(require_local_write)])
async def cancel_benchmark_task(run_id: str) -> Dict[str, Any]:
    """取消本服务持有的同 run 任务。

    Args:
        run_id: run 标识。

    Returns:
        Dict[str, Any]: 取消后的 run 视图。

    Raises:
        HTTPException: 404 不存在；409 已 completed（不删除结果）。
    """
    service = get_benchmark_service()
    try:
        row = await service.cancel_run(run_id)
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("[排名] 取消失败: %s", exc)
        raise HTTPException(status_code=503, detail='database_unavailable')
    if row is None:
        raise HTTPException(status_code=404, detail='run_not_found')
    return {'success': True, 'data': service.get_task_view(run_id) or {}}


@router.get('/runs/{run_id}', dependencies=[Depends(require_local_read)])
async def get_benchmark_run(run_id: str) -> Dict[str, Any]:
    """返回冻结结果（只读数据库快照，不访问 B 站、不重算）。

    Args:
        run_id: run 标识。

    Returns:
        Dict[str, Any]: 冻结结果。

    Raises:
        HTTPException: 404 不存在；409 尚未产生冻结结果。
    """
    service = get_benchmark_service()
    row = service.read_result(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail='run_not_found')
    if not row.get('result'):
        raise HTTPException(status_code=409, detail='run_not_completed')
    return {
        'success': True,
        'data': {
            'run_id': row.get('id'),
            'status': row.get('status'),
            'snapshot_hash': row.get('snapshot_hash'),
            'selection_as_of_s': row.get('selection_as_of_s'),
            'error_codes': row.get('error_codes') or [],
            'result': row.get('result'),
        },
    }


@router.post('/runs/{run_id}/retry', dependencies=[Depends(require_local_write)])
async def retry_benchmark_run(run_id: str) -> Dict[str, Any]:
    """以新 as_of 新建 run 重试（不把新观测拼回旧 run）。

    Args:
        run_id: 旧 run 标识。

    Returns:
        Dict[str, Any]: 新 run 摘要。

    Raises:
        HTTPException: 404 不存在；409 旧 run 仍运行中。
    """
    service = get_benchmark_service()
    try:
        row = service.retry_as_new_run(run_id)
    except HTTPException:
        raise
    except LookupError:
        raise HTTPException(status_code=404, detail='run_not_found')
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("[排名] 重试失败: %s", exc)
        raise HTTPException(status_code=503, detail='database_unavailable')

    run_id_new = row.get('id')
    target_uid = int(row.get('target_uid'))
    _active_targets.add(target_uid)
    try:
        task = service.start_run(run_id_new)
        task.add_done_callback(lambda _finished: _active_targets.discard(target_uid))
    except Exception as exc:  # noqa: BLE001
        _active_targets.discard(target_uid)
        raise HTTPException(status_code=503, detail='benchmark_start_failed')
    return {
        'success': True,
        'data': {
            'run_id': run_id_new,
            'status': row.get('status'),
            'target_uid': target_uid,
            'requested_peers': row.get('requested_peers'),
            'retried_from': run_id,
            'selection_as_of_s': row.get('selection_as_of_s'),
        },
    }
