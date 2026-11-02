"""
热点发现模块 - LLM状态接口

拆分自 hotspot.py 原始 L859-L929。
"""
from . import router
from .deps import get_llm_client


@router.get("/llm-status")
async def check_llm_status():
    """
    检查LLM配置状态
    
    功能说明：
    - 检测LLM客户端是否已配置并可用
    - 用于前端判断是否显示AI选题功能
    - 提供配置状态反馈
    
    Returns:
        dict: {
            "configured": bool,  # 是否已配置（API Key有效）
            "message": str       # 状态描述
        }
    
    可能的返回：
    1. LLM未初始化：
       {"configured": False, "message": "LLM未配置"}
    2. LLM已配置但无效：
       {"configured": False, "message": "LLM配置无效"}
    3. LLM已配置且可用：
       {"configured": True, "message": "LLM已配置"}
    
    HTTP状态码：
        200: 成功返回状态（无论LLM是否配置）
    
    工作流程：
    1. 尝试获取LLM客户端实例
    2. 如果实例为None，说明初始化失败（未配置）
    3. 如果实例存在，调用is_configured()验证配置有效性
    4. 返回配置状态和描述
    
    使用场景：
    - 前端页面加载时检查LLM状态
    - 决定是否显示"AI生成"按钮
    - 配置页面验证API Key是否生效
    - 管理员检查系统功能完整性
    
    注意事项：
    - 此接口不会抛出异常，总是返回200状态码
    - 即使LLM不可用，其他功能（词云、活动追踪）仍可正常使用
    - LLM是可选功能，未配置不影响系统稳定性
    
    示例响应（已配置）：
        {
            "configured": true,
            "message": "LLM已配置"
        }
    
    示例响应（未配置）：
        {
            "configured": false,
            "message": "LLM未配置"
        }
    """
    # 获取LLM客户端实例（可能为None）
    llm_client = get_llm_client()
    
    # 检查LLM客户端是否初始化成功
    if llm_client is None:
        # LLM未初始化（API Key未配置或初始化失败）
        return {
            "configured": False,
            "message": "LLM未配置"
        }
    
    # LLM实例存在，检查配置是否有效
    return {
        "configured": llm_client.is_configured(),  # 验证配置有效性
        "message": "LLM已配置" if llm_client.is_configured() else "LLM配置无效"
    }
