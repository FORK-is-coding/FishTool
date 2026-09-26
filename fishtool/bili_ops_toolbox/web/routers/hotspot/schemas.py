"""
热点发现模块 请求数据模型

拆分自 hotspot.py 原始 L143-L256。
"""
from pydantic import BaseModel
from typing import List, Optional


# ============ 请求/响应数据模型 ============

class TagCloudRequest(BaseModel):
    """
    词云生成请求模型
    
    用于生成指定分区的热门tag词云数据，帮助UP主发现当前热点话题。
    
    Attributes:
        zone_name: 分区名称，如'游戏'、'科技'、'生活'等
                   必须是B站支持的标准分区名称
        limit: 拉取视频数量上限，默认100
               数值越大数据越全面，但耗时越长
               建议范围：50-500
        top_n: 输出top N个高频tag，默认50
               用于筛选最热门的tag，避免长尾噪音
    
    使用示例：
        request = TagCloudRequest(
            zone_name="游戏",
            limit=200,
            top_n=30
        )
    """
    zone_name: str  # 分区名称
    limit: int = 100  # 拉取视频数上限
    top_n: int = 50  # 输出top N个tag

class ActivityRequest(BaseModel):
    """
    活动拉取请求模型
    
    用于获取B站最新的官方活动和UGC活动情报。
    
    Attributes:
        include_ugc: 是否包含UGC活动，默认True
                     True: 同时拉取官方活动和UGC活动
                     False: 仅拉取官方活动
        zone: 分区筛选，默认'all'，取值：
              - all: 全部官号
              - game: 游戏区（各大游戏官号，如绝区零/明日方舟激励计划）
              - anime: 动画区（B站番剧/动画官号）
              - paint: 绘画区（画师同人站等蓝标UGC账号）
    
    数据来源：
    - 官方活动：B站官方运营的大型活动（如拜年祭、夏日绘卷）
    - UGC活动：按分区抓取对应官号动态中带活动关键词的内容
    
    使用场景：
    - UP主按自己创作分区寻找参与机会
    - 运营人员分析特定分区活动趋势
    """
    include_ugc: bool = True  # 是否包含UGC活动
    zone: str = 'all'  # 分区筛选: all/game/anime/paint

class TopicGenerateRequest(BaseModel):
    """
    AI选题生成请求模型
    
    基于创作方向和分区数据，生成AI推荐的创作选题。
    
    Attributes:
        direction: 创作方向描述，如'游戏攻略'、'科技评测'、'生活vlog'
                   越具体效果越好，建议包含内容类型+目标受众
        zone_name: 目标分区，如'游戏'、'科技'
                   用于获取分区热点数据作为选题依据
        count: 生成选题数量，默认10
               建议范围：5-20，过多会降低质量
        use_llm: 是否使用LLM生成，默认True
                 True: 使用AI生成有创意的选题
                 False: 仅基于数据统计生成基础选题
    
    工作流程：
    1. 获取目标分区的热点数据（热门视频、tag、趋势）
    2. 如果use_llm=True，调用LLM结合创作方向生成选题
    3. 如果use_llm=False或LLM不可用，使用规则生成基础选题
    4. 返回包含标题、描述、数据支撑的结构化选题
    
    使用示例：
        request = TopicGenerateRequest(
            direction="搞笑游戏实况，面向年轻观众",
            zone_name="游戏",
            count=10,
            use_llm=True
        )
    """
    direction: str  # 创作方向描述
    zone_name: str  # 目标分区
    count: int = 10  # 生成数量
    use_llm: bool = True  # 是否使用LLM

class TopicUpdateRequest(BaseModel):
    """
    选题状态更新请求模型
    
    用于更新选题库中选题的状态，跟踪选题生命周期。
    
    Attributes:
        status: 选题状态，支持以下值：
                - 'pending': 待审核（新生成的选题初始状态）
                - 'adopted': 已采纳（UP主决定使用该选题）
                - 'published': 已发布（基于该选题的视频已发布）
    
    状态流转：
        pending → adopted → published
                ↓
              rejected（可选）
    
    使用场景：
    - UP主从选题库选择创作方向
    - 跟踪选题效果，分析哪些选题更受欢迎
    """
    status: str  # 选题状态: pending/adopted/published
