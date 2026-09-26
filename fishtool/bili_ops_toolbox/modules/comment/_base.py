"""
评论舆情监控器 - 基础 Mixin（__init__ + 基础工具方法）

拆分自 monitor.py 原始 L79-L346（类常量 + 初始化 + 基础方法）。
类常量与 __init__ 必须保留在继承链首位，确保实例化时先初始化。
"""
from typing import List, Dict, Any, Optional, Set
from datetime import datetime, timedelta
from collections import deque
import logging

from bilibili.api import BilibiliAPI
from llm.client import LLMClient
from core.logger import get_logger
from .collector import CommentCollector
from .deduplicator import CommentDeduplicator
from .sentiment import SentimentAnalyzer

logger = get_logger(__name__)


class MonitorBaseMixin:
    """基础 Mixin：初始化 + 数据整理工具方法"""

    NEGATIVE_RATIO_THRESHOLD = 0.3  # 负面占比超过30%预警

    COMMENT_SURGE_THRESHOLD = 2.0   # 评论数突增2倍预警

    RISK_COUNT_THRESHOLD = 5        # 风险评论超过5条预警

    def __init__(self,
                 api: BilibiliAPI,
                 llm_client: Optional[LLMClient] = None,
                 alert_callback: Optional[callable] = None):
        """初始化监控器

        Args:
            api: B站API实例
            llm_client: LLM客户端（可选）
            alert_callback: 预警回调函数（可选），用于实时推送预警
        """
        # 保存B站API客户端引用
        self.api = api
        # 保存LLM客户端引用（可选，用于深度分析）
        self.llm_client = llm_client
        # 保存预警回调函数引用（可选，用于实时推送预警消息）
        self.alert_callback = alert_callback

        # 初始化子模块
        # 采集器：默认普通策略
        self.collector = CommentCollector(api)
        # 去重器：默认参数
        self.deduplicator = CommentDeduplicator()
        # 情感分析器：LLM 注入但默认不启用总结（省成本）
        self.analyzer = SentimentAnalyzer(llm_client, use_llm_summary=False)

        # 监控历史
        # 键为 bvid，值为定长队列（最近10次评论数）
        self._monitoring_history: Dict[str, deque] = {}

        # 自定义预警关键词
        self.custom_keywords: Set[str] = set()

    @staticmethod
    def _apply_sentiment_results(comments: List[Dict[str, Any]], sentiment_result: Dict[str, Any]) -> None:
        """将情感分析结果按评论 ID 回写，保证后续统计读取到真实标签。

        Args:
            comments: 原始评论字典列表，将在原地补充 sentiment 等字段。
            sentiment_result: SentimentAnalyzer.analyze_batch 返回的分析结果。

        Returns:
            None: 本函数原地更新评论列表。
        """
        analyses = sentiment_result.get('analyzed_comments', []) if sentiment_result else []
        analysis_by_rpid = {str(item.get('rpid')): item for item in analyses if item.get('rpid') is not None}
        for comment in comments:
            analysis = analysis_by_rpid.get(str(comment.get('rpid')))
            if not analysis:
                continue
            comment['sentiment'] = analysis.get('sentiment') or 'neutral'
            comment['sentiment_score'] = analysis.get('confidence', 0)
            comment['matched_keywords'] = analysis.get('matched_keywords', {})

    @staticmethod
    def _empty_visualization_data() -> Dict[str, Any]:
        """返回字段完整的空大屏数据，供无数据状态稳定渲染。

        Returns:
            dict: 所有图表和列表均可直接消费的空结构。
        """
        return {
            'sentiment_distribution': {},
            'date_comment_counts': [],
            'dedup_statistics': {
                'before_count': 0,
                'after_count': 0,
                'removed_count': 0,
                'reasons': {
                    'same_user_repeat': 0,
                    'cross_user_aggregation': 0,
                    'fuzzy_similarity': 0,
                    'time_window_hotspot': 0,
                },
            },
            'top10_voice_comments': [],
        }

    def _build_visualization_data(self, raw_comments: List[Dict[str, Any]], processed_comments: List[Dict[str, Any]], dedup_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """构造前端大屏所需的情感、日期、去重和 Top10 统计结构。"""
        # 日期字段可能是 datetime、时间戳或字符串，统一截取自然日。
        date_counts: Dict[str, int] = {}
        for comment in raw_comments:
            value = comment.get('ctime')
            day = value.date().isoformat() if isinstance(value, datetime) else str(value or '')[:10]
            if day:
                date_counts[day] = date_counts.get(day, 0) + 1
        details = dedup_result or {}
        reasons = {
            'same_user_repeat': len(details.get('user_duplicates', [])),
            'cross_user_aggregation': len(details.get('cross_user_groups', [])),
            'fuzzy_similarity': len(details.get('fuzzy_groups', [])),
            'time_window_hotspot': len(details.get('time_hotspots', [])),
        }
        # 声量分同时考虑点赞与跨用户复读权重，确保“高声量”不退化为单纯点赞榜。
        def voice_score(item: Dict[str, Any]) -> int:
            """计算单条评论的展示声量分。"""
            likes = int(item.get('like', 0) or 0)
            weight = max(1, int(item.get('voice_weight', item.get('duplicate_count', 1)) or 1))
            return likes + (weight - 1) * 10

        top10 = sorted(processed_comments, key=voice_score, reverse=True)[:10]
        return {
            'sentiment_distribution': self._sentiment_counts(raw_comments),
            'date_comment_counts': [{'date': day, 'count': date_counts[day]} for day in sorted(date_counts)],
            'dedup_statistics': {
                'before_count': len(raw_comments),
                'after_count': len(processed_comments),
                'removed_count': max(0, len(raw_comments) - len(processed_comments)),
                'reasons': reasons,
            },
            'top10_voice_comments': [self._serialize_comment(item) for item in top10],
        }

    @staticmethod
    def _sentiment_counts(comments: List[Dict[str, Any]]) -> Dict[str, int]:
        """统计评论情感标签，未知标签归入 neutral。"""
        counts: Dict[str, int] = {}
        for comment in comments:
            label = comment.get('sentiment') or 'neutral'
            counts[label] = counts.get(label, 0) + 1
        return counts

    @staticmethod
    def _serialize_comment(comment: Dict[str, Any]) -> Dict[str, Any]:
        """提取高声量评论字段，并生成前端可解释的清洗结论。

        Args:
            comment: 去重处理后的评论字典。

        Returns:
            dict: 包含内容、点赞、情感、声量分与清洗原因的安全字段。
        """
        weight = max(1, int(comment.get('voice_weight', comment.get('duplicate_count', 1)) or 1))
        likes = int(comment.get('like', 0) or 0)
        duplicate_type = comment.get('duplicate_type')
        voice_type = comment.get('voice_type')
        if voice_type == 'cross_user_same':
            cleaning_reason = f'跨用户同内容聚合，合并 {weight} 条声量'
        elif duplicate_type == 'user_repeat':
            cleaning_reason = f'同用户重复评论折叠，保留 1 条代表'
        elif duplicate_type == 'fuzzy_similar':
            cleaning_reason = '相似内容去重，保留互动更高的代表评论'
        elif comment.get('is_duplicate'):
            cleaning_reason = '历史去重标记，保留代表评论'
        else:
            cleaning_reason = '唯一内容，清洗后保留'
        return {
            'rpid': comment.get('rpid'),
            'uname': comment.get('uname') or '匿名用户',
            'content': comment.get('content') or '',
            'like': likes,
            'ctime': comment.get('ctime'),
            'voice_weight': weight,
            'voice_score': likes + (weight - 1) * 10,
            'sentiment': comment.get('sentiment') or 'neutral',
            'cleaning_reason': cleaning_reason,
        }
