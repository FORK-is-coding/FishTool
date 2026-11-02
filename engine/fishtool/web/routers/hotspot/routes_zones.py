"""
热点发现模块 - 分区列表接口

拆分自 hotspot.py 原始 L262-L295。
"""
from . import router
from .deps import TagCloudGenerator


# ============ 路由端点 ============

@router.get("/zones")
async def get_supported_zones():
    """
    获取支持的分区列表
    
    功能说明：
    - 返回系统支持的所有B站分区名称
    - 用于前端展示分区选择器
    - 数据来源于TagCloudGenerator的静态配置
    
    Returns:
        dict: {
            "zones": List[str],  # 分区名称列表，如['游戏', '科技', '生活']
            "count": int         # 分区总数
        }
    
    HTTP状态码：
        200: 成功返回分区列表
    
    使用场景：
    - 前端页面初始化时加载分区选项
    - 验证用户输入的分区名称是否有效
    
    示例响应：
        {
            "zones": ["游戏", "科技", "生活", "娱乐"],
            "count": 4
        }
    """
    options = TagCloudGenerator.get_zone_options()
    zones = [item["name"] for item in options]
    # 绘画是二级分区，不走榜单接口，由方案 C（搜索主采 + 重点账号补漏）采集；
    # 前端需要在下拉框里看到它，因此单独追加到展示列表，不污染一级分区契约。
    collect_options = options + [{"name": "绘画", "tid": TagCloudGenerator.PAINT_TID, "scheme": "paint_c"}]
    return {
        "zones": zones + ["绘画"],
        "zone_options": collect_options,
        "count": len(zones) + 1,
    }
