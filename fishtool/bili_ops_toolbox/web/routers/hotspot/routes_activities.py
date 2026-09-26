"""
热点发现模块 - 活动追踪接口

拆分自 hotspot.py 原始 L432-L531。
"""
from fastapi import HTTPException

from . import router
from .deps import ActivityTracker, get_api
from .schemas import ActivityRequest


@router.post("/activities")
async def fetch_activities(request: ActivityRequest):
    """
    拉取活动情报
    
    功能说明：
    - 从B站获取最新的官方活动和UGC活动信息
    - 提供活动名称、时间、参与条件等结构化数据
    - 帮助UP主发现参与机会，提升曝光
    
    Args:
        request: ActivityRequest对象，包含：
            - include_ugc: 是否包含UGC活动（默认True）
    
    Returns:
        dict: {
            "success": True,
            "data": {
                "official": [  # 官方活动列表
                    {
                        "title": str,          # 活动名称
                        "start_time": str,     # 开始时间
                        "end_time": str,       # 结束时间
                        "link": str,           # 活动链接
                        "description": str     # 活动描述
                    },
                    ...
                ],
                "ugc": [  # UGC活动列表（仅在include_ugc=True时返回）
                    {...}
                ],
                "total_count": int  # 活动总数
            }
        }
    
    Raises:
        HTTPException(500): 拉取失败（如网络错误、API限流）
    
    工作流程：
    1. 创建ActivityTracker实例
    2. 调用B站API获取活动列表
    3. 解析活动数据，提取关键信息
    4. 按类型分类返回（官方/UGC）
    
    数据来源：
    - 官方活动：B站活动中心API
    - UGC活动：动态话题、合集等
    
    使用场景：
    - UP主查看可参与的活动
    - 运营人员分析活动热度
    - 自动化工具监控新活动
    
    示例请求：
        POST /activities
        {
            "include_ugc": true
        }
    
    示例响应：
        {
            "success": true,
            "data": {
                "official": [
                    {
                        "title": "拜年祭2024",
                        "start_time": "2024-01-01",
                        "end_time": "2024-02-15",
                        "link": "https://...",
                        "description": "..."
                    }
                ],
                "ugc": [...],
                "total_count": 15
            }
        }
    """
    try:
        # 获取API客户端实例
        api = get_api()
        # 创建活动追踪器
        tracker = ActivityTracker(api)
        
        # 校验分区参数合法性（不合法回退全部）
        zone = request.zone if request.zone in ActivityTracker.SUPPORTED_ZONES else 'all'
        
        # 拉取活动数据（异步调用，按分区筛选官号）
        result = await tracker.fetch_all_activities(
            include_ugc=request.include_ugc,  # 是否包含UGC活动
            zone=zone  # 分区筛选
        )
        
        # 成功返回活动列表
        return {
            "success": True,
            "data": result
        }
        
    except Exception as e:
        # 拉取失败（网络错误、API限流等）
        raise HTTPException(status_code=500, detail=f"拉取活动失败: {str(e)}")
