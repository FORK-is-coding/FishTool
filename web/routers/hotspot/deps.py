"""
热点发现模块 依赖与辅助函数

拆分自 hotspot.py 原始 L42-L138（全局实例 + 辅助函数）。

全局实例采用延迟初始化模式（单例）：
- 只有在首次调用时才创建API/LLM客户端实例，节省资源
- _tag_cloud_tasks 维护词云后台任务状态
"""
from typing import TYPE_CHECKING, Optional
import asyncio
import time
import uuid

from bilibili.api import BilibiliAPI
from bilibili.cookie_pool import get_cookie_pool
from llm.client import LLMClient, get_llm_client as get_global_llm_client
from core.config import ConfigManager
from core.exceptions import LLMNotConfiguredError, ValidationError
from core.logger import get_logger
from modules.hotspot import TagCloudGenerator, ActivityTracker, TopicGenerator

if TYPE_CHECKING:  # 仅供类型注解；运行时不导入，避免与 schemas 形成循环依赖
    from .schemas import TagCloudRequest

logger = get_logger(__name__)

# ============ 全局实例（延迟初始化模式） ============
# 采用单例模式管理API客户端，避免重复初始化
# 延迟初始化：只有在首次调用时才创建实例，节省资源
# LLM客户端不在本模块缓存，统一委托 llm.client 全局单例（保存配置后会被重置）
_api: Optional[BilibiliAPI] = None  # B站API客户端实例
_tag_cloud_tasks = {}
# 词云后台协程的强引用集合：事件循环只持弱引用，不留引用可能被 GC 提前回收；
# 任务完成时由 done callback 自动移除，避免长期驻留。
_tag_cloud_task_refs: set = set()
# 已结束词云任务在内存中的保留时长（秒），与分析任务一致；超时记录在创建新任务时清理。
_TAG_CLOUD_TASK_TTL_SECONDS = 3600


def _prune_tag_cloud_tasks() -> None:
    """清理已结束且超过 TTL 的词云任务记录，避免任务表只增不减。

    只在创建新任务时调用；运行中的任务永远不清理，查询接口不会读到「消失的任务」。
    旧记录没有 ``finished_monotonic`` 时按 ``started_monotonic`` 计时。
    """
    if not _tag_cloud_tasks:
        return
    now = time.monotonic()
    expired = [
        task_id
        for task_id, task in _tag_cloud_tasks.items()
        if task.get("status") != "running"
        and now - float(task.get("finished_monotonic") or task.get("started_monotonic") or now)
        > _TAG_CLOUD_TASK_TTL_SECONDS
    ]
    for task_id in expired:
        _tag_cloud_tasks.pop(task_id, None)


def _update_tag_cloud_task(task_id: str, stage: str, progress: int, message: str) -> None:
    """更新词云任务的真实阶段进度与基于实测速率的剩余时间。"""
    # 取出后台任务状态，更新进度时先确认任务仍然存在。
    task = _tag_cloud_tasks.get(task_id)
    if not task:
        return
    task.update(stage=stage, progress=progress, message=message)
    elapsed = max(time.monotonic() - task["started_monotonic"], 0.1)
    task["estimated_seconds"] = round(elapsed * (100 - progress) / progress) if progress > 8 else None

async def _run_tag_cloud_task(task_id: str, request: "TagCloudRequest") -> None:
    """后台执行词云采集，任务状态供前端轮询。"""
    try:
        generator = TagCloudGenerator(get_api())
        result = await generator.generate_cloud_data(
            zone_name=request.zone_name,
            limit=request.limit,
            top_n=request.top_n,
            progress_callback=lambda stage, progress, message: _update_tag_cloud_task(
                task_id, stage, progress, message
            ),
        )
        _tag_cloud_tasks[task_id].update(
            status="completed", stage="completed", progress=100,
            message="词云生成完成", estimated_seconds=0, result=result,
            finished_monotonic=time.monotonic(),
        )
    except Exception as exc:
        logger.exception("词云后台任务失败 task=%s", task_id)
        _tag_cloud_tasks[task_id].update(
            status="failed", message=f"生成词云失败: {exc}", estimated_seconds=0,
            finished_monotonic=time.monotonic(),
        )


def _spawn_tag_cloud_task(task_id: str, request: "TagCloudRequest") -> None:
    """启动词云后台协程并保留强引用，完成后自动释放。

    Args:
        task_id: 任务 ID（同时是 ``_tag_cloud_tasks`` 的键）。
        request: 已校验的词云请求体。
    """
    background = asyncio.create_task(_run_tag_cloud_task(task_id, request))
    _tag_cloud_task_refs.add(background)
    background.add_done_callback(_tag_cloud_task_refs.discard)


def get_api() -> BilibiliAPI:
    """
    获取B站API客户端实例（单例模式）
    
    功能说明：
    - 首次调用时创建BilibiliAPI实例并初始化cookie池
    - 后续调用直接返回已创建的实例
    - 保证整个应用生命周期内只有一个API客户端
    
    Returns:
        BilibiliAPI: 已初始化的B站API客户端
        
    实现细节：
    - 使用全局变量缓存实例
    - 自动从cookie池获取可用cookie
    - 线程安全性：FastAPI单进程模型下无需加锁
    """
    global _api
    if _api is None:
        # 首次初始化：从cookie池获取cookie并创建API实例
        cookie_pool = get_cookie_pool()  # 获取全局cookie池
        _api = BilibiliAPI(cookie_pool=cookie_pool)  # 创建API实例
    return _api

def get_llm_client() -> Optional[LLMClient]:
    """
    获取LLM客户端实例（委托全局单例，允许失败）
    
    功能说明：
    - 委托 llm.client.get_llm_client()，与配置保存后的重置共用同一实例
    - 如果初始化失败（如未配置API Key），返回None，且不缓存失败结果
    - LLM功能是可选的，失败不影响其他功能
    
    Returns:
        Optional[LLMClient]: LLM客户端实例，或None（未配置/初始化失败）
        
    使用场景：
    - 生成AI选题时需要LLM客户端
    - 其他功能（词云、活动追踪）不依赖LLM
    
    容错设计：
    - 未配置记 info，其他初始化异常记完整堆栈
    - 调用方需要检查返回值是否为None
    """
    try:
        return get_global_llm_client()
    except LLMNotConfiguredError as e:
        # 未配置属于正常状态，降级为非LLM模式
        logger.info(f"LLM未配置，跳过LLM客户端初始化: {e}")
    except Exception:
        # 此处不抛出异常，允许系统继续运行
        logger.exception("LLM客户端初始化失败")
    return None
