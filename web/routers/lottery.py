"""抽奖工具 Web API 路由。"""

# 后台协程调度任务执行。
import asyncio
# 计算请求体指纹，用于复用运行中的同参任务。
import hashlib
# 请求体规范化序列化，保证指纹稳定。
import json
# 用单调时钟计算任务耗时与剩余时间。
import time
# 生成不可预测的任务 ID。
import uuid
# 日期与时间处理，用于抽奖区间过滤。
from datetime import date, datetime, time as datetime_time
# 类型标注保证接口一致性与 IDE 提示。
from typing import Any, Coroutine, Dict, List, Literal, Optional

# FastAPI 路由与 HTTP 异常。
from fastapi import APIRouter, HTTPException
# Pydantic 请求体校验。
from pydantic import BaseModel, Field

# B 站客户端，负责数据请求。
from bilibili.api import BilibiliAPI
# 登录态池，负责账号轮换。
from bilibili.cookie_pool import get_cookie_pool
# 请求限频器，防止风控。
from bilibili.rate_limiter import RateLimiter
# 统一日志记录器。
from core.logger import get_logger
# 抽奖核心服务。
from modules.lottery import LotteryService
# 目标元数据获取工具。
from modules.lottery.target import fetch_target_metadata

# 抽奖模块路由前缀与标签。
router = APIRouter(prefix="/lottery", tags=["lottery"])
# 模块级日志实例。
logger = get_logger(__name__)
# 惰性初始化的抽奖服务单例。
_service: Optional[LotteryService] = None
# 内存任务表：task_id -> 任务状态，服务重启后任务即失效。
_tasks: Dict[str, Dict[str, Any]] = {}
# 已结束任务在内存表中的保留时长（秒），超时后在创建新任务时清理，避免内存无限增长。
_FINISHED_TASK_TTL_SECONDS = 3600
# 任务状态中仅供服务端使用的字段，查询接口不对外返回。
_INTERNAL_TASK_KEYS = frozenset({"started_monotonic", "finished_monotonic", "fingerprint"})
# 运行中后台协程的强引用（task_id -> asyncio.Task），防止事件循环只持弱引用导致任务被回收；完成后自动释放。
_background_tasks: Dict[str, "asyncio.Task[None]"] = {}
# 最近一次抽奖的中奖名单快照，供校验接口复用。
_latest_winners: List[Dict[str, Any]] = []


# 视频或动态目标请求体。
class TargetRequest(BaseModel):
    """视频或动态目标请求。"""

    # 用户输入的 BV 号或动态完整链接。
    target: str = Field(..., min_length=3, max_length=500, description="BV号或动态完整链接")


# 单 UID 真人快速筛选请求体。
class QuickFilterRequest(BaseModel):
    """单 UID 真人快速筛选请求。"""

    # B 站用户 UID，必须为正整数。
    uid: int = Field(..., gt=0, description="B站用户UID")
    # AI 判定侧重点模板，可选。
    focus_template: Optional[str] = Field(None, max_length=1200, description="AI判定侧重点模板")


# 中奖名单一键真人校验请求体。
class VerifyWinnersRequest(BaseModel):
    """中奖名单一键真人校验请求。"""

    # 待校验的中奖名单，可选。
    winners: List[Dict[str, Any]] = Field(default_factory=list, max_length=100, description="当前抽奖中奖名单；为空时读取服务端最近一次结果")
    # AI 判定侧重点模板，可选。
    focus_template: Optional[str] = Field(None, max_length=1200, description="AI判定侧重点模板")


# 评论区真人批量筛选任务请求体。
class FilterTaskRequest(TargetRequest):
    """评论区真人批量筛选任务请求。"""

    # AI 判定侧重点模板，可选。
    focus_template: Optional[str] = Field(None, max_length=1200, description="AI判定侧重点模板")


# 随机抽奖任务请求体。
class DrawTaskRequest(TargetRequest):
    """随机抽奖任务请求。"""

    # 期望抽取的中奖人数。
    winner_count: int = Field(1, ge=1, le=100, description="中奖人数")
    # 是否去重，同一 UID 只保留一次机会。
    unique_users: bool = Field(True, description="同一UID仅保留一次抽奖机会")
    # 是否只抽大会员。
    vip_only: bool = Field(False, description="是否仅保留大会员")
    # 最低用户等级限制。
    min_level: Optional[int] = Field(None, ge=0, le=6, description="最低用户等级")
    # 是否在抽奖前接入真人判定链，默认关闭以保持既有行为。
    real_only: bool = Field(False, description="是否仅保留真人判定候选")
    # 真人筛选开启时，是否保留证据不足的未知用户。
    include_indeterminate: bool = Field(False, description="是否保留未知判定用户")
    # 与真人筛选页面共用的 AI 判定侧重点。
    focus_template: Optional[str] = Field(None, max_length=1200, description="AI判定侧重点模板")
    # 评论开始日期（含）。
    date_start: Optional[date] = Field(None, description="评论开始日期（含）")
    # 评论结束日期（含）。
    date_end: Optional[date] = Field(None, description="评论结束日期（含）")


# 获取或创建抽奖服务单例。
def get_service() -> LotteryService:
    """惰性创建抽奖服务，复用登录态池与限频器。"""
    # 修改模块级单例变量。
    global _service
    # 首次访问时组装依赖（限频器 + Cookie 池），后续直接复用单例。
    if _service is None:
        # 构建统一限频与登录态配置的 B 站客户端。
        api = BilibiliAPI(rate_limiter=RateLimiter(), cookie_pool=get_cookie_pool())
        # 首次访问时组装服务实例。
        _service = LotteryService(api)
    # 返回可复用的服务单例。
    return _service


# 计算请求体指纹。
def _request_fingerprint(kind: Literal["filter", "draw"], request: BaseModel) -> str:
    """按任务类型与规范化请求体生成指纹，参数完全一致的请求得到相同指纹。"""
    # 日期等字段先转成 JSON 友好形式，再按键排序序列化，保证同参同指纹。
    payload = json.dumps(
        {"kind": kind, "request": request.model_dump(mode="json")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    # 只保留摘要，避免在任务表里留存完整请求体。
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# 查找参数相同且仍在运行的任务。
def _find_running_task(fingerprint: str) -> Optional[str]:
    """返回指纹相同且状态仍为 running 的任务 ID，没有则返回 None。"""
    # 遍历内存任务表，只复用尚未结束的任务。
    for task_id, task in _tasks.items():
        # 指纹一致且仍在运行时直接复用。
        if task.get("status") == "running" and task.get("fingerprint") == fingerprint:
            # 返回可复用的任务 ID。
            return task_id
    # 没有可复用的运行中任务。
    return None


# 清理过期的已结束任务。
def _prune_finished_tasks() -> None:
    """移除结束时间超过保留时长的任务，运行中任务一律保留。"""
    # 以单调时钟为准计算过期截止点。
    deadline = time.monotonic() - _FINISHED_TASK_TTL_SECONDS
    # 先收集再删除，避免遍历期间修改字典。
    expired = [
        task_id for task_id, task in _tasks.items()
        if task.get("status") != "running"
        and task.get("finished_monotonic") is not None
        and task["finished_monotonic"] < deadline
    ]
    # 逐个移除过期任务。
    for task_id in expired:
        # 已被其他路径移除时静默跳过。
        _tasks.pop(task_id, None)


# 后台协程结束时的收尾。
def _on_background_task_done(task_id: str) -> None:
    """释放强引用，记录结束时间；协程被取消等意外结束时把任务标记为失败。"""
    # 释放强引用，避免已结束协程常驻内存。
    _background_tasks.pop(task_id, None)
    # 任务状态可能已被清理，此时无需收尾。
    task = _tasks.get(task_id)
    # 任务不存在时直接返回。
    if task is None:
        # 无状态可更新。
        return
    # 协程已结束却仍是 running（如被取消），落为失败，避免同参请求一直复用一个已死任务。
    if task.get("status") == "running":
        # 意外中止统一提示重新发起。
        task.update(status="failed", stage="failed", message="任务意外中止，请重新发起", estimated_seconds=0)
    # 记录结束时间，供过期清理使用。
    task["finished_monotonic"] = time.monotonic()


# 启动后台协程并保存强引用。
def _spawn(task_id: str, coroutine: Coroutine[Any, Any, None]) -> None:
    """创建后台协程，持有强引用直至其结束。"""
    # 交给事件循环调度。
    background = asyncio.create_task(coroutine)
    # 事件循环只弱引用任务，必须自行持有强引用。
    _background_tasks[task_id] = background
    # 结束后释放引用并完成状态收尾。
    background.add_done_callback(lambda _done, tid=task_id: _on_background_task_done(tid))


# 创建后台任务状态。
def _create_task(kind: Literal["filter", "draw"], fingerprint: Optional[str] = None) -> str:
    """创建后台任务状态并返回不可预测任务 ID。"""
    # 新建任务前顺带清理过期的已结束任务，控制内存任务表规模。
    _prune_finished_tasks()
    # 用随机 UUID 作为任务句柄，避免顺序 ID 被前端猜测。
    task_id = uuid.uuid4().hex
    # 初始化任务状态，进度从排队阶段开始。
    _tasks[task_id] = {
        "task_id": task_id,
        "kind": kind,
        "status": "running",
        "stage": "queued",
        "progress": 0,
        "message": "任务已进入安全队列，正在准备本地数据检索",
        "estimated_seconds": None,
        "started_monotonic": time.monotonic(),
        "result": None,
        "fingerprint": fingerprint,
    }
    # 返回新任务 ID 供前端轮询。
    return task_id


# 更新任务进度与剩余时间估算。
def _progress(task_id: str, stage: str, percent: int, message: str) -> None:
    """更新任务进度并按当前处理速度估算剩余时间。"""
    # 取出后台任务状态，更新进度时先确认任务仍然存在。
    task = _tasks.get(task_id)
    # 任务不存在时直接跳过。
    if not task:
        # 不更新任何状态。
        return
    # 进度钳制在 1-99，避免过早显示完成。
    progress = max(1, min(99, int(percent)))
    # 用已耗时和当前进度外推剩余秒数。
    elapsed = max(0.1, time.monotonic() - task["started_monotonic"])
    # 批量更新任务进度字段。
    task.update(
        stage=stage,
        progress=progress,
        message=message,
        estimated_seconds=round(elapsed * (100 - progress) / progress) if progress >= 5 else None,
    )


# 后台执行真人筛选全流程。
async def _run_filter_task(task_id: str, request: FilterTaskRequest) -> None:
    """后台执行目标确认、评论复用/采集、画像采集和 AI 分类。"""
    # 全程异常捕获，失败统一落库。
    try:
        # 获取抽奖服务单例。
        service = get_service()
        # 通知前端进入目标校验阶段。
        _progress(task_id, "validating_target", 3, "正在重新核对标题与发布者，防止任务期间目标被替换")
        # 任务真正开始时重新拉取目标元数据，防止排队期间目标被换。
        target = await fetch_target_metadata(service.api, request.target)
        # 执行真人筛选，进度回调直接映射到任务状态。
        result = await service.filter_real_users(
            target,
            request.focus_template,
            lambda stage, percent, message: _progress(task_id, stage, percent, message),
        )
        # 成功后写入完整结果并标记完成。
        _tasks[task_id].update(
            status="completed", stage="completed", progress=100,
            message="真人筛选完成，结果已按可信类别整理", estimated_seconds=0, result=result,
        )
    except Exception as exc:
        # 任何异常都落到失败状态，错误信息返回给前端展示。
        logger.exception("真人筛选任务失败: %s", task_id)
        # 失败时写入错误信息供前端展示。
        _tasks[task_id].update(status="failed", stage="failed", message=f"筛选失败: {exc}", estimated_seconds=0)


# 后台执行随机抽奖全流程。
async def _run_draw_task(task_id: str, request: DrawTaskRequest) -> None:
    """后台执行目标复核、评论数据准备和随机抽取。"""
    # 修改模块级最近中奖名单。
    global _latest_winners
    # 全程异常捕获，失败统一落库。
    try:
        # 获取抽奖服务单例。
        service = get_service()
        # 通知前端进入目标校验阶段。
        _progress(task_id, "validating_target", 3, "正在重新核对标题与发布者，确保抽奖目标无误")
        # 重新拉取目标元数据，避免排队期间目标被替换。
        target = await fetch_target_metadata(service.api, request.target)
        # 校验日期区间，防止倒置范围导致过滤结果为空或异常。
        if request.date_start and request.date_end and request.date_start > request.date_end:
            # 日期区间倒置直接报错。
            raise ValueError("评论开始日期不能晚于结束日期")
        # 执行随机抽奖，日期转成当天起止时刻参与过滤。
        result = await service.draw(
            target,
            request.winner_count,
            request.unique_users,
            lambda stage, percent, message: _progress(task_id, stage, percent, message),
            vip_only=request.vip_only,
            min_level=request.min_level,
            date_start=datetime.combine(request.date_start, datetime_time.min) if request.date_start else None,
            date_end=datetime.combine(request.date_end, datetime_time.max) if request.date_end else None,
            real_only=request.real_only,
            include_indeterminate=request.include_indeterminate,
            focus_template=request.focus_template,
        )
        # 保存本次中奖名单快照，供后续校验接口复用。
        _latest_winners = [dict(item) for item in result.get("winners") or []]
        # 成功后写入中奖名单与完整结果。
        _tasks[task_id].update(
            status="completed", stage="completed", progress=100,
            message="抽奖完成，中奖名单已生成", estimated_seconds=0, result=result,
        )
    except Exception as exc:
        # 任何异常都落到失败状态，错误信息返回给前端展示。
        logger.exception("随机抽奖任务失败: %s", task_id)
        # 失败时写入错误信息供前端展示。
        _tasks[task_id].update(status="failed", stage="failed", message=f"抽奖失败: {exc}", estimated_seconds=0)


@router.post("/preview")
# 预览目标信息接口。
async def preview_target(request: TargetRequest) -> Dict[str, Any]:
    """预览目标标题与发布者，供前端确认弹窗展示。"""
    # 正常路径返回目标元数据。
    try:
        # 预览成功后直接返回目标元数据。
        return {"success": True, "data": await get_service().preview(request.target)}
    except ValueError as exc:
        # 输入不合法属于 4xx，直接透出错误文案。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # 服务端异常统一包装为 502。
        raise HTTPException(status_code=502, detail=f"读取目标信息失败: {exc}") from exc


@router.post("/quick-filter")
# 单 UID 快速分析接口。
async def quick_filter(request: QuickFilterRequest) -> Dict[str, Any]:
    """快速分析单个 UID，侧重点模板受严格数据边界约束。"""
    # 正常路径返回分析结果。
    try:
        # 直接调用服务层快速筛选，不创建后台任务。
        result = await get_service().quick_filter(request.uid, request.focus_template)
        # 分析成功后直接返回结果。
        return {"success": True, "data": result}
    except Exception as exc:
        # 记录异常堆栈便于排查。
        logger.exception("UID 快速筛选失败: %s", request.uid)
        # 服务端异常统一包装为 502。
        raise HTTPException(status_code=502, detail=f"UID分析失败: {exc}") from exc


@router.post("/verify-winners")
# 中奖名单真人校验接口。
async def verify_winners(request: VerifyWinnersRequest) -> Dict[str, Any]:
    """优先复用本地画像，一键校验请求名单或最近一次中奖名单。"""
    # 请求未携带名单时复用最近一次抽奖结果。
    winners = request.winners or _latest_winners
    # 名单为空时提示先抽奖。
    if not winners:
        # 未抽奖不能校验。
        raise HTTPException(status_code=400, detail="请先进行抽奖！")
    # 正常路径返回校验结果。
    try:
        # 调用服务层校验接口，返回每个用户的真人判定。
        result = await get_service().verify_winners(winners, request.focus_template)
        # 校验成功后直接返回判定结果。
        return {"success": True, "data": result}
    except ValueError as exc:
        # 业务校验错误透出 4xx。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # 记录异常堆栈便于排查。
        logger.exception("中奖名单真人校验失败")
        # 服务端异常统一包装为 502。
        raise HTTPException(status_code=502, detail=f"中奖名单校验失败: {exc}") from exc


@router.post("/filter/tasks", status_code=202)
# 创建真人筛选后台任务接口。
async def start_filter_task(request: FilterTaskRequest) -> Dict[str, Any]:
    """创建评论区真人批量筛选后台任务；同参任务仍在运行时直接复用。"""
    # 按请求体计算指纹，用于识别重复启动。
    fingerprint = _request_fingerprint("filter", request)
    # 同参任务仍在运行时返回原任务，避免重复采集。
    running_id = _find_running_task(fingerprint)
    # 命中运行中任务直接复用。
    if running_id:
        # 返回原任务 ID 并标记为复用。
        return {"success": True, "task_id": running_id, "reused": True}
    # 创建任务状态并立即返回 ID。
    task_id = _create_task("filter", fingerprint)
    # 将耗时工作交给后台协程，接口立即返回任务 ID 供前端轮询。
    _spawn(task_id, _run_filter_task(task_id, request))
    # 返回任务 ID 供前端轮询。
    return {"success": True, "task_id": task_id, "reused": False}


@router.post("/draw/tasks", status_code=202)
# 创建随机抽奖后台任务接口。
async def start_draw_task(request: DrawTaskRequest) -> Dict[str, Any]:
    """创建随机抽奖后台任务；同参任务仍在运行时直接复用。"""
    # 按请求体计算指纹，用于识别重复启动。
    fingerprint = _request_fingerprint("draw", request)
    # 同参任务仍在运行时返回原任务，避免重复抽奖。
    running_id = _find_running_task(fingerprint)
    # 命中运行中任务直接复用。
    if running_id:
        # 返回原任务 ID 并标记为复用。
        return {"success": True, "task_id": running_id, "reused": True}
    # 创建任务状态并立即返回 ID。
    task_id = _create_task("draw", fingerprint)
    # 将耗时工作交给后台协程，接口立即返回任务 ID 供前端轮询。
    _spawn(task_id, _run_draw_task(task_id, request))
    # 返回任务 ID 供前端轮询。
    return {"success": True, "task_id": task_id, "reused": False}


@router.get("/tasks/{task_id}")
# 查询任务进度接口。
async def get_task(task_id: str) -> Dict[str, Any]:
    """返回筛选或抽奖任务的实时进度与最终结果。"""
    # 按任务 ID 查找内存状态。
    task = _tasks.get(task_id)
    # 任务不存在时返回 404。
    if not task:
        # 任务不存在或服务重启后内存表已清空。
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启")
    # 剔除内部计时与指纹字段，避免把实现细节暴露给前端。
    public_task = {key: value for key, value in task.items() if key not in _INTERNAL_TASK_KEYS}
    # 返回任务进度与结果。
    return {"success": True, "data": public_task}