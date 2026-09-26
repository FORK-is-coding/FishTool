"""
热点发现模块 - 词云任务接口

拆分自 hotspot.py 原始 L299-L428。
"""
import asyncio
import time
import uuid

from fastapi import HTTPException

from . import router
from .deps import TagCloudGenerator, ValidationError, _run_tag_cloud_task, _tag_cloud_tasks, get_api
from .schemas import TagCloudRequest


@router.post("/tag-cloud/tasks")
async def start_tag_cloud_task(request: TagCloudRequest):
    """启动词云后台任务，返回用于读取真实进度的任务 ID。"""
    if not TagCloudGenerator.is_collectable_zone(request.zone_name):
        raise HTTPException(status_code=400, detail=f"未知的分区名称: {request.zone_name}")
    task_id = uuid.uuid4().hex
    _tag_cloud_tasks[task_id] = {
        "task_id": task_id,
        "status": "running",
        "stage": "queued",
        "progress": 2,
        "message": "任务已创建，等待采集",
        "estimated_seconds": None,
        "started_monotonic": time.monotonic(),
        "result": None,
    }
    # 将耗时工作交给后台协程，接口立即返回任务 ID 供前端轮询。
    asyncio.create_task(_run_tag_cloud_task(task_id, request))
    return {"success": True, "task_id": task_id}

@router.get("/tag-cloud/tasks/{task_id}")
async def get_tag_cloud_task(task_id: str):
    """返回词云任务状态，隐藏仅供服务端估时的内部字段。"""
    # 取出后台任务状态，更新进度时先确认任务仍然存在。
    task = _tag_cloud_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="词云任务不存在或已过期")
    return {"success": True, "data": {
        key: value for key, value in task.items() if key != "started_monotonic"
    }}

@router.post("/tag-cloud")
async def generate_tag_cloud(request: TagCloudRequest):
    """
    生成分区热门tag词云
    
    功能说明：
    - 拉取指定分区的热门视频
    - 统计视频tag出现频率
    - 生成词云数据（tag+权重）
    
    Args:
        request: TagCloudRequest对象，包含：
            - zone_name: 分区名称
            - limit: 拉取视频数量
            - top_n: 返回top N个tag
    
    Returns:
        dict: {
            "success": True,
            "data": {
                "tags": [
                    {"tag": str, "weight": int},  # tag名称和出现次数
                    ...
                ],
                "total_videos": int,  # 分析的视频总数
                "zone_name": str      # 分区名称
            }
        }
    
    Raises:
        HTTPException(400): 分区名称无效
        HTTPException(500): 内部错误（如网络请求失败）
    
    工作流程：
    1. 验证分区名称是否支持
    2. 调用B站API拉取热门视频列表
    3. 提取所有视频的tag并统计频率
    4. 按频率降序排序，返回top N
    
    性能优化：
    - 异步并发拉取视频数据
    - 缓存API客户端实例
    
    使用场景：
    - UP主查看当前分区热点话题
    - 数据分析人员研究内容趋势
    
    示例请求：
        POST /tag-cloud
        {
            "zone_name": "游戏",
            "limit": 200,
            "top_n": 30
        }
    
    示例响应：
        {
            "success": true,
            "data": {
                "tags": [
                    {"tag": "原神", "weight": 45},
                    {"tag": "攻略", "weight": 32}
                ],
                "total_videos": 200,
                "zone_name": "游戏"
            }
        }
    """
    try:
        # 获取API客户端实例
        api = get_api()
        # 创建词云生成器
        generator = TagCloudGenerator(api)
        
        # 生成词云数据（异步调用）
        result = await generator.generate_cloud_data(
            zone_name=request.zone_name,  # 目标分区
            limit=request.limit,  # 视频数量上限
            top_n=request.top_n  # 返回top N个tag
        )
        
        # 成功返回数据，统一字段名并标记同步任务已完成
        result = dict(result or {})
        result.setdefault("video_count", result.get("total_videos", 0))
        result.setdefault("tag_count", len(result.get("word_frequency", {})))
        result.setdefault("progress", 100)
        result.setdefault("stage", "completed")
        return {
            "success": True,
            "data": result
        }
        
    except (ValueError, ValidationError) as e:
        # 参数错误（如分区名称无效）
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # 其他错误（如网络请求失败、API限流）
        raise HTTPException(status_code=500, detail=f"生成词云失败: {str(e)}")
