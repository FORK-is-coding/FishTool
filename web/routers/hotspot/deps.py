"""
热点发现模块 依赖与辅助函数

拆分自 hotspot.py 原始 L42-L138（全局实例 + 辅助函数）。

全局实例采用延迟初始化模式（单例）：
- 只有在首次调用时才创建API/LLM客户端实例，节省资源
- _tag_cloud_tasks 维护词云后台任务状态
"""
from typing import Optional
import asyncio
import time
import uuid

from bilibili.api import BilibiliAPI
from bilibili.cookie_pool import get_cookie_pool
from llm.client import LLMClient
from core.config import ConfigManager
from core.exceptions import ValidationError
from modules.hotspot import TagCloudGenerator, ActivityTracker, TopicGenerator

# ============ 全局实例（延迟初始化模式） ============
# 采用单例模式管理API和LLM客户端，避免重复初始化
# 延迟初始化：只有在首次调用时才创建实例，节省资源
_api: Optional[BilibiliAPI] = None  # B站API客户端实例
_llm_client: Optional[LLMClient] = None  # LLM客户端实例（可能为None）
_tag_cloud_tasks = {}


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
        )
    except Exception as exc:
        _tag_cloud_tasks[task_id].update(
            status="failed", message=f"生成词云失败: {exc}", estimated_seconds=0
        )

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
    获取LLM客户端实例（单例模式，允许失败）
    
    功能说明：
    - 尝试初始化LLM客户端，用于AI选题生成
    - 如果初始化失败（如未配置API Key），返回None
    - LLM功能是可选的，失败不影响其他功能
    
    Returns:
        Optional[LLMClient]: LLM客户端实例，或None（未配置/初始化失败）
        
    使用场景：
    - 生成AI选题时需要LLM客户端
    - 其他功能（词云、活动追踪）不依赖LLM
    
    容错设计：
    - 捕获所有异常，避免LLM初始化失败影响整个服务
    - 调用方需要检查返回值是否为None
    """
    global _llm_client
    if _llm_client is None:
        try:
            # 尝试初始化LLM客户端
            _llm_client = LLMClient()
        except Exception:
            # 初始化失败（如API Key未配置），设为None
            # 此处不抛出异常，允许系统继续运行
            _llm_client = None
    return _llm_client
