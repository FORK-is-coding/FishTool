"""
评论舆情监控器 - 监控动作 Mixin（采集/去重/情感编排）

拆分自 monitor.py 原始 L119-L220、L348-L443。
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

from core.logger import get_logger
from .collector import CommentCollector

logger = get_logger(__name__)


class MonitorActionsMixin:
    """监控动作 Mixin：单视频/批量/账号级监控"""


    def add_custom_keywords(self, keywords: List[str]):
        """添加自定义预警关键词
        
        补充默认风险词库，支持业务特定关键词监控。
        例如：品牌名、产品名、竞品名等。
        
        Args:
            keywords: 关键词列表
        """
        self.custom_keywords.update(keywords)
        logger.info(f"添加自定义预警关键词: {keywords}")

    async def monitor_video(self, 
                           bvid: str,
                           enable_dedup: bool = True,
                           enable_sentiment: bool = True,
                           strategy: str = CommentCollector.STRATEGY_NORMAL) -> Dict[str, Any]:
        """监控单个视频的评论
        
        完整流程：采集 -> 情感分析 -> 去重 -> 预警检测 -> 持久化。
        
        Args:
            bvid: 视频BV号
            enable_dedup: 是否启用去重
            enable_sentiment: 是否启用情感分析
            strategy: 采集策略（fast/normal/full），默认普通策略（热门+100条）
            
        Returns:
            监控结果
        """
        # 主流程：采集 -> 情感 -> 去重 -> 预警 -> 持久化
        # 每个阶段失败都不阻塞后续，保证监控可用性
        logger.info(f"开始监控视频 {bvid} 的评论，策略: {strategy}")
        
        # 1. 采集评论
        # 策略由调用方传入：normal=热门+100条普通，full=全量采集
        comments = await self.collector.collect_video_comments(
            bvid,
            strategy=strategy
        )
        
        # 保存后端状态，落库异常必须在监控响应中显式返回。
        save_result = getattr(self.collector, 'last_save_result', {})
        warning = save_result.get('warning') if save_result else None
        
        if not comments:
            logger.warning(f"视频 {bvid} 未采集到评论")
            return {
                'bvid': bvid,
                'success': False,
                'collected_count': 0,
                'processed_count': 0,
                'visualization': self._empty_visualization_data(),
                'warning': warning,
                'error': '未采集到评论数据，请检查B站Cookie/登录态是否有效，或确认接口是否触发风控'
            }
        
        # 2. 情感分析（在去重前进行，以便时间窗热点检测能获取sentiment）
        sentiment_result = None
        if enable_sentiment:
            sentiment_result = self.analyzer.analyze_batch(comments)
            # 分析器返回独立结果列表，需要显式回写到原评论，后续去重和图表才能读取真实标签。
            self._apply_sentiment_results(comments, sentiment_result)
        
        # 3. 去重处理（此时评论已有sentiment字段）
        # 去重结果可能缩减评论数，预警基于处理后数据
        dedup_result = None
        if enable_dedup:
            dedup_result = self.deduplicator.deduplicate(comments)
            processed_comments = dedup_result['deduplicated_comments']
        else:
            processed_comments = comments
        
        # 4. 预警检测
        # 基于情感分布/评论突增/风险词/自定义词
        alerts = await self._detect_alerts(bvid, comments, processed_comments, sentiment_result)
        
        # 5. 保存监控记录
        # 预警入库 + 回调推送
        await self._save_monitoring_record(bvid, comments, alerts)
        
        # 组装监控结果
        visualization = self._build_visualization_data(comments, processed_comments, dedup_result)
        result = {
            'bvid': bvid,
            'success': True,
            'collected_count': len(comments),
            'processed_count': len(processed_comments),
            'dedup_result': dedup_result,
            'sentiment_result': sentiment_result,
            'visualization': visualization,
            'warning': warning,
            'alerts': alerts,
            'monitored_at': datetime.now().isoformat()
        }
        
        logger.info(f"视频 {bvid} 监控完成，发现 {len(alerts)} 个预警")
        return result

    async def monitor_multiple_videos(self, bvids: List[str], strategy: str = CommentCollector.STRATEGY_NORMAL) -> Dict[str, Any]:
        """批量监控多个视频
        
        逐个调用 monitor_video，聚合所有预警结果。
        适合账号全量监控场景。
        
        Args:
            bvids: BV号列表
            strategy: 采集策略（fast/normal/full），透传给每个视频的监控流程
            
        Returns:
            批量监控结果，包含成功数、总预警数等统计
        """
        # 批量场景：每个视频独立监控，互不影响
        # 单个视频失败不影响其他视频的结果聚合
        logger.info(f"开始批量监控 {len(bvids)} 个视频，策略: {strategy}")
        
        results = []
        total_alerts = []
        
        # 逐个视频执行监控
        for bvid in bvids:
            result = await self.monitor_video(bvid, strategy=strategy)
            results.append(result)
            
            # 聚合所有预警
            if result.get('alerts'):
                # 批量扩展列表
                total_alerts.extend(result['alerts'])
        
        # 组装批量汇总结果
        summary = {
            'total_videos': len(bvids),
            'successful_count': sum(1 for r in results if r.get('success')),
            'total_alerts': len(total_alerts),
            'results': results,
            'monitored_at': datetime.now().isoformat()
        }
        
        logger.info(f"批量监控完成，共发现 {len(total_alerts)} 个预警")
        return summary

    async def monitor_user_account(self, uid: str, video_limit: int = 10, strategy: str = CommentCollector.STRATEGY_NORMAL) -> Dict[str, Any]:
        """监控用户账号的所有视频
        
        先获取用户视频列表，再批量监控每个视频的评论。
        适合 UP 主自查场景。
        
        Args:
            uid: 用户UID
            video_limit: 监控视频数量
            strategy: 采集策略（fast/normal/full），透传给每个视频的批量监控
            
        Returns:
            账号监控结果，包含所有视频的预警汇总
        """
        logger.info(f"开始监控账号 {uid} 的视频，策略: {strategy}")
        
        # 先复用统一的用户资料接口获取昵称；该步骤独立容错，失败不会影响后续视频采集。
        nickname = ''
        try:
            # get_user_info 内部已经封装 /x/space/wbi/acc/info 和 WBI 签名逻辑。
            user_info = await self.api.get_user_info(int(uid))
            user_data = (user_info or {}).get('data') or {}
            # B站用户资料接口的 name 字段就是 UP 主昵称。
            nickname = str(user_data.get('name') or '').strip()
        except Exception as exc:
            # 昵称只是报告增强信息，获取失败时保留 UID 作为唯一标识。
            logger.warning(f"获取账号 {uid} 昵称失败，继续执行视频采集: {exc}")

        # 获取用户视频列表
        # 限制数量防止一次监控过多视频
        videos = await self.collector._get_user_videos(uid, video_limit)
        
        # 无视频直接返回失败
        if not videos:
            return {
                'uid': uid,
                'nickname': nickname,
                'username': nickname,
                'success': False,
                'error': '未找到视频'
            }
        
        # 批量监控
        bvids = [v['bvid'] for v in videos]
        result = await self.monitor_multiple_videos(bvids, strategy=strategy)
        
        # 附加账号信息；昵称为空时前端会自动回退显示 UID。
        result['uid'] = uid
        result['nickname'] = nickname
        result['username'] = nickname
        result['videos'] = videos
        
        return result
