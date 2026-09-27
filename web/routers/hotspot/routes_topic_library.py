"""
热点发现模块 - 选题库接口

拆分自 hotspot.py 原始 L666-L855。
"""
from typing import Optional

from fastapi import HTTPException

from . import router
from .deps import TopicGenerator, get_api, get_llm_client
from .schemas import TopicUpdateRequest


@router.get("/topics")
async def get_topic_library(
    zone_name: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50
):
    """
    查询选题库
    
    功能说明：
    - 从数据库查询历史生成的选题
    - 支持按分区、状态筛选
    - 用于管理和回顾选题库
    
    Args:
        zone_name: 分区名称筛选（可选）
                   如'游戏'，为None则返回所有分区
        status: 状态筛选（可选）
                支持'pending'/'adopted'/'published'
                为None则返回所有状态
        limit: 返回数量上限，默认50
               避免一次性返回过多数据
    
    Returns:
        dict: {
            "success": True,
            "topics": [  # 选题列表
                {
                    "id": int,             # 选题ID
                    "title": str,          # 选题标题
                    "description": str,    # 选题描述
                    "zone_name": str,      # 所属分区
                    "status": str,         # 当前状态
                    "created_at": str,     # 创建时间
                    "updated_at": str      # 更新时间
                },
                ...
            ],
            "count": int  # 返回的选题数量
        }
    
    Raises:
        HTTPException(500): 查询失败（如数据库错误）
    
    工作流程：
    1. 构建查询条件（分区、状态）
    2. 从数据库查询符合条件的选题
    3. 按创建时间倒序排序（最新的在前）
    4. 限制返回数量（避免过多数据）
    5. 返回选题列表
    
    使用场景：
    - UP主查看历史生成的选题
    - 筛选特定分区的选题
    - 查看已采纳但未发布的选题
    - 统计选题采纳率
    
    示例请求：
        GET /topics?zone_name=游戏&status=pending&limit=20
    
    示例响应：
        {
            "success": true,
            "topics": [
                {
                    "id": 123,
                    "title": "原神新角色无伤挑战",
                    "description": "...",
                    "zone_name": "游戏",
                    "status": "pending",
                    "created_at": "2024-01-01 12:00:00",
                    "updated_at": "2024-01-01 12:00:00"
                }
            ],
            "count": 1
        }
    """
    try:
        # 获取API客户端实例
        api = get_api()
        # 获取LLM客户端（查询不需要LLM，但TopicGenerator需要初始化）
        llm_client = get_llm_client()
        # 创建选题生成器（用于访问数据库）
        generator = TopicGenerator(api, llm_client)
        
        # 查询选题库（异步调用）
        topics = await generator.get_topic_library(
            zone_name=zone_name,  # 分区筛选（可选）
            status=status,  # 状态筛选（可选）
            limit=limit  # 数量上限
        )
        
        # 成功返回选题列表
        return {
            "success": True,
            "topics": topics,  # 选题列表
            "count": len(topics)  # 实际返回数量
        }
        
    except Exception as e:
        # 查询失败（数据库错误等）
        raise HTTPException(status_code=500, detail=f"查询选题库失败: {str(e)}")

@router.put("/topics/{topic_id}")
async def update_topic_status(topic_id: int, request: TopicUpdateRequest):
    """
    更新选题状态
    
    功能说明：
    - 更新指定选题的状态（如从pending改为adopted）
    - 用于跟踪选题生命周期
    - 支持选题管理和效果分析
    
    Args:
        topic_id: 选题ID（从选题库查询获得）
        request: TopicUpdateRequest对象，包含：
            - status: 新状态（'pending'/'adopted'/'published'）
    
    Returns:
        dict: {
            "success": True,
            "message": str  # 操作结果描述
        }
    
    Raises:
        HTTPException(404): 选题不存在
        HTTPException(500): 更新失败（如数据库错误）
    
    工作流程：
    1. 验证topic_id是否存在
    2. 验证新状态是否合法
    3. 更新数据库中的状态字段
    4. 更新updated_at时间戳
    5. 返回操作结果
    
    状态说明：
    - pending: 待审核（新生成的选题）
    - adopted: 已采纳（UP主决定使用）
    - published: 已发布（基于该选题的视频已发布）
    
    使用场景：
    - UP主采纳某个选题：pending → adopted
    - 视频发布后标记选题：adopted → published
    - 分析选题采纳率和发布率
    
    示例请求：
        PUT /topics/123
        {
            "status": "adopted"
        }
    
    示例响应：
        {
            "success": true,
            "message": "选题状态已更新为 adopted"
        }
    
    错误响应：
        {
            "detail": "选题不存在"
        }
    """
    try:
        # 获取API客户端实例
        api = get_api()
        # 获取LLM客户端
        llm_client = get_llm_client()
        # 创建选题生成器（用于访问数据库）
        generator = TopicGenerator(api, llm_client)
        
        # 更新选题状态（异步调用）
        success = await generator.update_topic_status(topic_id, request.status)
        
        # 检查更新是否成功
        if not success:
            # 选题不存在，返回404
            raise HTTPException(status_code=404, detail="选题不存在")
        
        # 成功返回结果
        return {
            "success": True,
            "message": f"选题状态已更新为 {request.status}"
        }
        
    except HTTPException:
        # 重新抛出HTTP异常（如404）
        raise
    except Exception as e:
        # 其他错误（数据库错误等）
        raise HTTPException(status_code=500, detail=f"更新选题失败: {str(e)}")
