"""语义折叠评论 Web 路由（规格：实现契约 §API/UI wire contract）。

路径前缀 ``/comment/semantic``，由 ``web/main.py`` include 到 ``/api``，于是对外为
``/api/comment/semantic/*``：

    GET  /api/comment/semantic/preflight?bvid=&limit=
    GET  /api/comment/semantic/preferences?bvid=
    PUT  /api/comment/semantic/preferences {bvid,expected_revision,auto_fold?,consent_version?,model_identity?}
    POST /api/comment/semantic/tasks {bvid,limit,consent_version,request_key}
    GET  /api/comment/semantic/tasks/{task_id}
    GET  /api/comment/semantic/tasks/{task_id}/result?page=&page_size=
    POST /api/comment/semantic/tasks/{task_id}/cancel {expected_revision}
    POST /api/comment/semantic/tasks/{task_id}/resume {expected_revision,acknowledge_unknown}
    GET  /api/comment/semantic/latest?bvid=
    POST /api/comment/semantic/decisions {bvid,task_id,action,expected_revision,group_id?,rpid?,request_key}
    GET  /api/comment/semantic/local-token    # 下发本机 session token（同源读）

设计约束（本文件只做校验/编排，不实现业务/统计/模型逻辑）：
- 调用注入的 ``SemanticService`` 单例，把 service/store 的结果与异常投影成 HTTP 契约。
- 统一响应体 ``{success:true, data:...}``；错误以安全结构 ``{error_code, message}``
  作为 HTTPException.detail，不含任何密钥 / 原始服务端负载。
- 状态码：400 校验、404 不存在、409 版本陈旧(CAS)/冲突、429 预算、502/503 模型/服务不可用。
- model_identity 固定为 ``{api_base, model}``，绝不含 API Key；写偏好时做结构净化。
- 读端点挂 ``require_local_read``、写端点挂 ``require_local_write``，复用 local_guard 既有依赖。
- service 实例由 ``web/main.py`` lifespan 装配后注入；未装配时端点返回 503。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from core.logger import get_logger
from modules.comment.semantic.contracts import (
    ProviderErrorCategory,
    SemanticBudgetExceeded,
    SemanticConflict,
    SemanticNotFound,
    SemanticProviderError,
    SemanticValidationError,
)
from web.local_guard import (
    LOCAL_TOKEN_HEADER,
    get_local_guard,
    require_local_read,
    require_local_write,
)

logger = get_logger(__name__)

router = APIRouter(prefix='/comment/semantic', tags=['comment-semantic'])

#: 进程内注入的语义折叠服务（由 web/main.py lifespan 装配后注入）。
_service: Optional[Any] = None

#: limit 契约边界：默认 200，最大 500（与 store/service/snapshot 契约一致）。
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 500

#: 合法手动决策动作（与 store.decide 契约一致）。
_DECISION_ACTIONS = ('undo_group', 'split_member', 'allow_member', 'allow_group')

#: result 分页上限（与 store.result 契约 page_size 对齐，避免一次拉爆）。
_MAX_PAGE_SIZE = 200


def set_semantic_service(service: Any) -> None:
    """注入 SemanticService 单例（lifespan 装配后调用；传 None 表示未启用）。

    Args:
        service: SemanticService 实例或 None。

    Returns:
        无。
    """
    global _service
    _service = service


def get_semantic_service() -> Any:
    """返回当前注入的 SemanticService。

    Returns:
        Any: SemanticService 实例。

    Raises:
        HTTPException: 503 服务未装配 / 未启用。
    """
    if _service is None:
        raise HTTPException(
            status_code=503,
            detail={'error_code': 'semantic_service_unavailable', 'message': '语义折叠服务未启用'},
        )
    return _service


# --------------------------------------------------------------------------
# 错误投影：store/service 的类型化异常 -> HTTP 契约（不泄露密钥/原始负载）
# --------------------------------------------------------------------------
#: store/service 异常类名 -> (HTTP 状态码, 安全 error_code)
#: 按类名匹配而非 import，避免对尚未落地的 store/contracts 模块产生 import 期耦合。
def _raise_for_domain_error(exc: Exception) -> None:
    """把类型化语义异常投影为 HTTPException；其它异常留给调用方兜底 raise。

    异常消息由 contracts 保证已脱敏（不含密钥 / 原始服务端报文）。

    Raises:
        HTTPException: 命中已知语义异常时抛出对应状态码 + 安全 detail。
    """
    if isinstance(exc, SemanticNotFound):
        status_code, error_code = 404, 'not_found'
    elif isinstance(exc, SemanticConflict):
        status_code, error_code = 409, 'revision_conflict'
    elif isinstance(exc, SemanticBudgetExceeded):
        status_code, error_code = 429, 'budget_exceeded'
    elif isinstance(exc, SemanticValidationError):
        status_code, error_code = 400, 'validation_error'
    elif isinstance(exc, SemanticProviderError):
        # 未配置属于“服务不可用”，映射 503；其余 provider 失败是上游 502。
        status_code = 503 if exc.category == ProviderErrorCategory.NOT_CONFIGURED else 502
        error_code = exc.category or 'provider_error'
    else:
        return
    raise HTTPException(status_code=status_code, detail={'error_code': error_code, 'message': str(exc) or error_code})


def _sanitize_model_identity(raw: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """净化 model_identity：只保留 ``api_base`` / ``model``，剔除任何其它字段（含 Key）。

    Args:
        raw: 客户端传入的 model_identity 字典或 None。

    Returns:
        Optional[Dict[str, str]]: 仅含 api_base/model 的字典；入参为 None 时返回 None。

    Raises:
        HTTPException: 400 字段缺失或类型非法。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400,
            detail={'error_code': 'validation_error', 'message': 'model_identity 必须是对象'},
        )
    api_base = raw.get('api_base')
    model = raw.get('model')
    if not isinstance(api_base, str) or not api_base.strip() or not isinstance(model, str) or not model.strip():
        raise HTTPException(
            status_code=400,
            detail={'error_code': 'validation_error', 'message': 'model_identity 需含非空 api_base 与 model'},
        )
    # 只回传两个字段：即便客户端夹带 api_key 等字段也在此被丢弃，绝不进入持久化。
    return {'api_base': api_base.strip(), 'model': model.strip()}


# --------------------------------------------------------------------------
# 请求模型（严格模型：拒绝未知字段，照契约字段抄）
# --------------------------------------------------------------------------
class PreferenceUpdateRequest(BaseModel):
    """PUT /preferences 请求体（CAS：expected_revision 必填）。"""

    # model_identity 字段与 pydantic 默认保护命名空间 "model_" 冲突，显式清空避免告警。
    model_config = ConfigDict(extra='forbid', protected_namespaces=())

    bvid: StrictStr = Field(min_length=1, max_length=64)
    expected_revision: StrictInt = Field(ge=0)
    auto_fold: Optional[StrictBool] = None
    consent_version: Optional[StrictStr] = Field(default=None, max_length=128)
    # 结构净化在端点内做（只留 api_base/model）；此处仅接收任意对象以便拒绝非对象。
    model_identity: Optional[Dict[str, Any]] = None


class TaskCreateRequest(BaseModel):
    """POST /tasks 请求体。"""

    model_config = ConfigDict(extra='forbid')

    bvid: StrictStr = Field(min_length=1, max_length=64)
    limit: StrictInt = Field(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT)
    consent_version: StrictStr = Field(min_length=1, max_length=128)
    request_key: StrictStr = Field(min_length=1, max_length=256)


class RevisionRequest(BaseModel):
    """POST /tasks/{id}/cancel 请求体（仅 CAS 版本）。"""

    model_config = ConfigDict(extra='forbid')

    expected_revision: StrictInt = Field(ge=0)


class ResumeRequest(BaseModel):
    """POST /tasks/{id}/resume 请求体（需显式确认未知结果）。"""

    model_config = ConfigDict(extra='forbid')

    expected_revision: StrictInt = Field(ge=0)
    acknowledge_unknown: StrictBool = False


class DecisionRequest(BaseModel):
    """POST /decisions 请求体（撤销/拆分/显式允许）。"""

    model_config = ConfigDict(extra='forbid')

    bvid: StrictStr = Field(min_length=1, max_length=64)
    task_id: StrictStr = Field(min_length=1, max_length=128)
    action: StrictStr = Field(min_length=1, max_length=32)
    expected_revision: StrictInt = Field(ge=0)
    group_id: Optional[StrictStr] = Field(default=None, max_length=128)
    rpid: Optional[StrictStr] = Field(default=None, max_length=64)
    request_key: StrictStr = Field(min_length=1, max_length=256)


# --------------------------------------------------------------------------
# 本机 session token（同源读；不写日志、不进 URL）
# --------------------------------------------------------------------------
@router.get('/local-token', dependencies=[Depends(require_local_read)])
async def issue_local_token() -> Dict[str, Any]:
    """向同源页面 / CLI 下发本机 session token，供写端点携带 ``X-Local-Token``。

    Returns:
        Dict[str, Any]: ``{success, data:{token, header}}``。
    """
    return {'success': True, 'data': {'token': get_local_guard().issue_token(), 'header': LOCAL_TOKEN_HEADER}}


# --------------------------------------------------------------------------
# 预检 / 偏好（跨视频状态切换 + consent 回写）
# --------------------------------------------------------------------------
@router.get('/preflight', dependencies=[Depends(require_local_read)])
async def preflight(
    bvid: str = Query(min_length=1, max_length=64),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> Dict[str, Any]:
    """返回快照计数/范围 + 模型是否已配置 / provider 身份 / 是否需要 consent / 版本 / 限额。

    不返回完整原始快照（仅浏览器预检所需的计数与范围）。

    Args:
        bvid: 目标视频 BV 号。
        limit: 采样上限（1..500，默认 200）。

    Returns:
        Dict[str, Any]: ``{success, data: <preflight 投影>}``。

    Raises:
        HTTPException: 404 未知视频；503 服务未装配。
    """
    service = get_semantic_service()
    try:
        data = await service.preflight(bvid, limit=limit)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 已知语义异常投影为 HTTP，其它上抛兜底处理
        _raise_for_domain_error(exc)
        raise
    return {'success': True, 'data': data}


@router.get('/preferences', dependencies=[Depends(require_local_read)])
async def get_preferences(bvid: str = Query(min_length=1, max_length=64)) -> Dict[str, Any]:
    """读取某视频的语义折叠偏好（auto_fold / revision / consent_version / model_identity）。

    偏好读经 store 同步方法（``service.store.preference``）；model_identity 不含密钥。

    Args:
        bvid: 目标视频 BV 号。

    Returns:
        Dict[str, Any]: ``{success, data: <preference>}``。

    Raises:
        HTTPException: 503 服务未装配。
    """
    service = get_semantic_service()
    try:
        data = service.store.preference(bvid)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_for_domain_error(exc)
        raise
    return {'success': True, 'data': data}


@router.put('/preferences', dependencies=[Depends(require_local_write)])
async def put_preferences(request: PreferenceUpdateRequest) -> Dict[str, Any]:
    """CAS 更新偏好：开关 auto_fold / 回写 consent_version / 绑定 model_identity。

    model_identity 仅保留 ``{api_base, model}``（净化后持久化，绝不含 Key）；
    expected_revision 做乐观锁，陈旧版本由 store 抛 SemanticConflict -> 409。

    Args:
        request: 偏好更新请求体。

    Returns:
        Dict[str, Any]: ``{success, data: <preference>}``。

    Raises:
        HTTPException: 400 校验；409 版本冲突；503 服务未装配。
    """
    service = get_semantic_service()
    model_identity = _sanitize_model_identity(request.model_identity)
    try:
        data = service.store.set_preference(
            request.bvid,
            request.expected_revision,
            auto_fold=request.auto_fold,
            consent_version=request.consent_version,
            model_identity=model_identity,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_for_domain_error(exc)
        raise
    return {'success': True, 'data': data}


# --------------------------------------------------------------------------
# 任务生命周期：创建 / 查询 / 结果 / 取消 / 继续
# --------------------------------------------------------------------------
@router.post('/tasks', dependencies=[Depends(require_local_write)])
async def create_task(request: TaskCreateRequest) -> Dict[str, Any]:
    """显式发起一次语义折叠任务（异步执行，不随页面加载自动计费）。

    service 先校验「持久化的、绑定 model_identity 的 consent」再创建任务，快速返回公开状态；
    模型请求在 durable create 之后由后台串行队列发起，本端点不等待模型结果。

    Args:
        request: 创建请求体（bvid/limit/consent_version/request_key）。

    Returns:
        Dict[str, Any]: ``{success, data: <public_state>}``。

    Raises:
        HTTPException: 400 校验；404 未知视频；409 身份不符/幂等冲突；429 预算；503 未配置。
    """
    service = get_semantic_service()
    try:
        data = await service.start(
            request.bvid,
            request.limit,
            request.consent_version,
            request.request_key,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_for_domain_error(exc)
        raise
    return {'success': True, 'data': data}


@router.get('/tasks/{task_id}', dependencies=[Depends(require_local_read)])
async def get_task(task_id: str) -> Dict[str, Any]:
    """返回任务公开状态（进度/阶段/覆盖/预算/错误）。不含内部 snapshot/checkpoint。

    Args:
        task_id: 任务标识。

    Returns:
        Dict[str, Any]: ``{success, data: <public_state>}``。

    Raises:
        HTTPException: 404 任务不存在；503 服务未装配。
    """
    service = get_semantic_service()
    try:
        data = service.status(task_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_for_domain_error(exc)
        raise
    if data is None:
        raise HTTPException(
            status_code=404,
            detail={'error_code': 'not_found', 'message': '任务不存在'},
        )
    return {'success': True, 'data': data}


@router.get('/tasks/{task_id}/result', dependencies=[Depends(require_local_read)])
async def get_task_result(
    task_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=_MAX_PAGE_SIZE),
) -> Dict[str, Any]:
    """返回任务结果：分组 + 全量原始评论（分页）+ 独立保留的排除/不确定/未分组行。

    结果包含成员原文与 keep_separate/stale 标记；分页不会隐藏任何无其它访问路径的成员。

    Args:
        task_id: 任务标识。
        page: 页码（从 1 开始）。
        page_size: 每页条数（1..200，默认 50）。

    Returns:
        Dict[str, Any]: ``{success, data: <result 投影>}``。

    Raises:
        HTTPException: 404 任务不存在；503 服务未装配。
    """
    service = get_semantic_service()
    try:
        data = service.results(task_id, page=page, page_size=page_size)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_for_domain_error(exc)
        raise
    if data is None:
        raise HTTPException(
            status_code=404,
            detail={'error_code': 'not_found', 'message': '任务不存在'},
        )
    return {'success': True, 'data': data}


@router.post('/tasks/{task_id}/cancel', dependencies=[Depends(require_local_write)])
async def cancel_task(task_id: str, request: RevisionRequest) -> Dict[str, Any]:
    """取消任务（幂等终态处理；版本围栏；不自动退费）。

    Args:
        task_id: 任务标识。
        request: 仅含 expected_revision。

    Returns:
        Dict[str, Any]: ``{success, data: <public_state>}``。

    Raises:
        HTTPException: 404 任务不存在；409 版本冲突；503 服务未装配。
    """
    service = get_semantic_service()
    try:
        data = await service.cancel(task_id, request.expected_revision)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_for_domain_error(exc)
        raise
    return {'success': True, 'data': data}


@router.post('/tasks/{task_id}/resume', dependencies=[Depends(require_local_write)])
async def resume_task(task_id: str, request: ResumeRequest) -> Dict[str, Any]:
    """继续被中断/部分/失败的任务：需显式确认未知结果，不重发已成功的 checkpoint。

    Args:
        task_id: 任务标识。
        request: expected_revision + acknowledge_unknown。

    Returns:
        Dict[str, Any]: ``{success, data: <public_state>}``。

    Raises:
        HTTPException: 404 任务不存在；409 版本冲突/状态不允许；429 预算；503 服务未装配。
    """
    service = get_semantic_service()
    try:
        data = await service.resume(
            task_id,
            request.expected_revision,
            acknowledge_unknown=request.acknowledge_unknown,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_for_domain_error(exc)
        raise
    return {'success': True, 'data': data}


@router.get('/latest', dependencies=[Depends(require_local_read)])
async def latest_task(bvid: str = Query(min_length=1, max_length=64)) -> Dict[str, Any]:
    """返回某视频最近一次任务的公开状态（刷新/重启后加载最新任务与偏好用）。

    Args:
        bvid: 目标视频 BV 号。

    Returns:
        Dict[str, Any]: ``{success, data: <public_state> | None}``（无任务时 data 为 None）。

    Raises:
        HTTPException: 503 服务未装配。
    """
    service = get_semantic_service()
    try:
        state = service.store.latest_task(bvid)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_for_domain_error(exc)
        raise
    return {'success': True, 'data': state}


@router.post('/decisions', dependencies=[Depends(require_local_write)])
async def submit_decision(request: DecisionRequest) -> Dict[str, Any]:
    """手动决策：撤销整组 / 拆分单条 / 显式允许单条或整组（保护按 (video,rpid)）。

    所有手动操作 CAS 视频偏好版本；store 记录可审计事件、幂等 request_key；
    未来任务无论代表项是谁都应用保护。返回新版本 + 偏好，客户端据此重载结果。

    Args:
        request: 决策请求体。

    Returns:
        Dict[str, Any]: ``{success, data: <result_summary>}``。

    Raises:
        HTTPException: 400 动作非法/校验；404 任务/分组不存在；409 版本冲突/陈旧分组；503 未装配。
    """
    service = get_semantic_service()
    if request.action not in _DECISION_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail={'error_code': 'validation_error', 'message': f'不支持的动作: {request.action}'},
        )
    try:
        data = service.store.decide(
            request.bvid,
            request.task_id,
            request.action,
            request.expected_revision,
            group_id=request.group_id,
            rpid=request.rpid,
            request_key=request.request_key,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_for_domain_error(exc)
        raise
    return {'success': True, 'data': data}
