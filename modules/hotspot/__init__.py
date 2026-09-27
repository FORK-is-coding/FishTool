"""
热点发现模块
提供分区热门tag词云、活动情报追踪、AI选题助手功能

本包是"热点发现"能力的集合：
- tag_cloud: TagCloudGenerator 分区热门标签词云
- activity_tracker: ActivityTracker 活动情报追踪
- topic_generator: TopicGenerator AI选题助手（LLM+降级）

依赖关系：
TopicGenerator 依赖 TagCloudGenerator 获取热门tag；
ActivityTracker 独立采集官方活动与UGC动态。

外部使用方式：
    from modules.hotspot import TagCloudGenerator, TopicGenerator
"""
from .tag_cloud import TagCloudGenerator
from .activity_tracker import ActivityTracker
from .topic_generator import TopicGenerator

__all__ = ['TagCloudGenerator', 'ActivityTracker', 'TopicGenerator']
