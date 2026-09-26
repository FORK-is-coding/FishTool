"""
评论监控模块
提供评论采集、去重分层、情感分析、舆情预警功能

本包是评论处理能力的完整集合：
- collector: CommentCollector 评论采集器（分级/增量/批量）
- deduplicator: CommentDeduplicator 四层去重器
- sentiment: SentimentAnalyzer 情感分析器（词典+LLM）
- monitor: CommentMonitor 舆情监控编排器（预警）

外部使用方式：
    from modules.comment import CommentCollector, CommentMonitor
"""
from .collector import CommentCollector
from .deduplicator import CommentDeduplicator
from .sentiment import SentimentAnalyzer
from .monitor import CommentMonitor

__all__ = ['CommentCollector', 'CommentDeduplicator', 'SentimentAnalyzer', 'CommentMonitor']
