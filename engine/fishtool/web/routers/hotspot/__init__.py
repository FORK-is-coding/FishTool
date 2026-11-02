"""
热点发现模块 API 路由

本模块提供热点发现相关的Web API端点，包括：
1. 词云生成：基于分区热门视频生成tag词云，帮助UP主发现热门话题
2. 活动追踪：自动拉取B站官方活动和UGC活动，挖掘参与机会
3. AI选题：结合LLM生成针对特定方向的创作选题，提供数据支撑

技术栈：
- FastAPI：异步Web框架
- Pydantic：请求/响应数据验证
- BilibiliAPI：封装B站API调用
- LLMClient：AI选题生成（可选）

依赖关系：
- bilibili.api: B站API客户端
- bilibili.cookie_pool: Cookie池管理
- modules.hotspot: 热点分析核心逻辑
- llm.client: LLM客户端（可选，未配置时仍可使用基础功能）

使用场景：
- UP主寻找创作灵感：查看分区热门tag、参与活动、生成选题
- 数据分析人员：获取结构化的热点数据进行二次分析

拆分说明（2026-08-22）：
原 hotspot.py（929行）按功能拆分为：
- deps.py: 全局实例与辅助函数（原始 L42-L138）
- schemas.py: 请求数据模型（原始 L143-L256）
- routes_zones.py: 分区列表接口（原始 L262-L295）
- routes_tag_cloud.py: 词云任务接口（原始 L299-L428）
- routes_activities.py: 活动追踪接口（原始 L432-L531）
- routes_topics_generate.py: AI选题生成接口（原始 L535-L662）
- routes_topic_library.py: 选题库接口（原始 L666-L855）
- routes_status.py: LLM状态接口（原始 L859-L929）
注释均原样保留，未删除未错位。
"""
from fastapi import APIRouter

router = APIRouter()

# ============ 路由端点 ============
# 路由按功能拆分至各子模块，装饰器在子模块内注册到本 router
from .routes_zones import get_supported_zones
from .routes_tag_cloud import start_tag_cloud_task, get_tag_cloud_task, generate_tag_cloud
from .routes_activities import fetch_activities
from .routes_topics_generate import generate_topics
from .routes_topic_library import get_topic_library, update_topic_status
from .routes_status import check_llm_status
from .routes_lifecycle import get_lifecycle, get_collect_progress
from .routes_watch import list_watch, get_watch, create_watch, release_watch
from .routes_discovery import get_discovery_latest, create_research_draft
# 第三批 g：事件 API（§14 完整清单）。导入即用装饰器把端点注册到本 router。
from . import routes_events  # noqa: F401  （注册事件 / 机会 / 反馈端点）

__all__ = ['router']
