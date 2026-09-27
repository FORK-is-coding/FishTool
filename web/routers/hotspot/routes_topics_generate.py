"""
热点发现模块 - AI选题生成接口

拆分自 hotspot.py 原始 L535-L662。
"""
from fastapi import HTTPException

from . import router
from .deps import TopicGenerator, get_api, get_llm_client
from .schemas import TopicGenerateRequest


@router.post("/topics/generate")
async def generate_topics(request: TopicGenerateRequest):
    """
    生成AI选题
    
    功能说明：
    - 基于创作方向和分区热点数据生成创作选题
    - 支持AI生成（LLM）和规则生成两种模式
    - 提供选题标题、描述、数据支撑等结构化信息
    
    Args:
        request: TopicGenerateRequest对象，包含：
            - direction: 创作方向（如'游戏攻略'）
            - zone_name: 目标分区（如'游戏'）
            - count: 生成数量（默认10）
            - use_llm: 是否使用LLM（默认True）
    
    Returns:
        dict: {
            "success": True,
            "data": {
                "topics": [  # 选题列表
                    {
                        "title": str,          # 选题标题
                        "description": str,    # 选题描述
                        "reasoning": str,      # 选题依据
                        "data_support": {      # 数据支撑
                            "hot_tags": List[str],      # 相关热门tag
                            "reference_videos": List,   # 参考视频
                            "trend_score": float        # 热度评分
                        },
                        "zone_name": str,      # 所属分区
                        "created_at": str,     # 生成时间
                        "status": "pending"    # 状态（新生成默认pending）
                    },
                    ...
                ],
                "count": int,           # 实际生成数量
                "llm_used": bool,       # 是否使用了LLM
                "generation_time": float  # 生成耗时（秒）
            }
        }
    
    Raises:
        HTTPException(500): 生成失败（如LLM调用失败、数据获取失败）
    
    工作流程：
    1. 获取目标分区的热点数据（热门视频、tag、趋势）
    2. 如果use_llm=True且LLM已配置：
       - 将热点数据和创作方向发送给LLM
       - LLM生成有创意的选题（标题+描述+依据）
    3. 如果use_llm=False或LLM不可用：
       - 使用规则生成基础选题（基于热门tag组合）
    4. 将生成的选题保存到数据库（状态为pending）
    5. 返回选题列表
    
    LLM生成 vs 规则生成：
    - LLM生成：更有创意，贴合创作方向，但需要配置API Key
    - 规则生成：基于数据统计，稳定可靠，但创意有限
    
    性能考虑：
    - 异步调用，不阻塞其他请求
    - LLM调用可能较慢（2-5秒），前端需要loading提示
    
    使用场景：
    - UP主寻找创作灵感
    - 批量生成选题库供后续选择
    - 数据驱动的内容规划
    
    示例请求：
        POST /topics/generate
        {
            "direction": "搞笑游戏实况，面向年轻观众",
            "zone_name": "游戏",
            "count": 10,
            "use_llm": true
        }
    
    示例响应：
        {
            "success": true,
            "data": {
                "topics": [
                    {
                        "title": "原神新角色无伤挑战，结局笑死我了",
                        "description": "结合当前热门角色，以搞笑视角展示挑战过程...",
                        "reasoning": "原神是当前最热话题，无伤挑战有话题性...",
                        "data_support": {
                            "hot_tags": ["原神", "挑战", "搞笑"],
                            "reference_videos": [...],
                            "trend_score": 0.92
                        },
                        "zone_name": "游戏",
                        "created_at": "2024-01-01 12:00:00",
                        "status": "pending"
                    }
                ],
                "count": 10,
                "llm_used": true,
                "generation_time": 3.2
            }
        }
    """
    try:
        # 获取API客户端实例
        api = get_api()
        # 获取LLM客户端（可能为None）
        llm_client = get_llm_client()
        
        # 创建选题生成器
        generator = TopicGenerator(api, llm_client)
        
        # 生成选题（异步调用）
        result = await generator.generate_topics(
            direction=request.direction,  # 创作方向
            zone_name=request.zone_name,  # 目标分区
            count=request.count,  # 生成数量
            use_llm=request.use_llm  # 是否使用LLM
        )
        
        # 成功返回选题列表
        return {
            "success": True,
            "data": result
        }
        
    except Exception as e:
        # 生成失败（LLM调用失败、数据获取失败等）
        raise HTTPException(status_code=500, detail=f"生成选题失败: {str(e)}")
