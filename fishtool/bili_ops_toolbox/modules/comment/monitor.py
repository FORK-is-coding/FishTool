"""
评论舆情监控器
整合采集、去重、情感分析，实现舆情预警和实时监控

本模块是评论监控的编排层，将采集、去重、情感分析
三个子模块串联成完整的舆情监控流程：

一、监控流程（monitor_video）
1. 采集：CommentCollector 按普通策略采集评论
2. 情感分析：SentimentAnalyzer 词典规则批量分析
   （在去重前执行，确保时间窗热点能取到 sentiment）
3. 去重：CommentDeduplicator 四层去重管线
4. 预警检测：基于情感分布/评论突增/风险词/自定义词
5. 持久化：预警写入 CommentAlert 表

二、预警规则
- negative_surge: 负面占比 > 30%（>50% 升级为 high）
- comment_surge: 评论数 > 历史均值 * 2（10次滑动窗口）
- risk_keywords: 风险评论 ≥ 5 条
- custom_keywords: 自定义关键词命中

三、多级监控入口
- monitor_video: 单视频监控
- monitor_multiple_videos: 批量监控（聚合预警）
- monitor_user_account: 账号级监控（先取视频列表）

四、预警消费
- alert_callback: 实时推送回调（如 WebSocket/桌宠）
- get_alerts: 历史预警查询（多条件过滤）
# 剔除不符合条件的数据
- mark_alert_read: 标记已读

依赖：
- .collector / .deduplicator / .sentiment: 子模块
- core.database: CommentAlert/Video
"""
import asyncio
# 从 typing 导入符号
from typing import List, Dict, Any, Optional, Set
# 从 datetime 导入符号
from datetime import datetime, timedelta
# 从 collections 导入符号
from collections import deque
# 导入模块
import logging

# 从 bilibili.api 导入符号
from bilibili.api import BilibiliAPI
# 从 llm.client 导入符号
from llm.client import LLMClient
# 子模块按职责拆分：采集/去重/情感各自独立维护
from core.logger import get_logger
# 从 core.database 导入符号
from core.database import get_session, CommentAlert, Video
# 从 collector 导入符号
from .collector import CommentCollector
# 从 deduplicator 导入符号
from .deduplicator import CommentDeduplicator
# 从 sentiment 导入符号
from .sentiment import SentimentAnalyzer

logger = get_logger(__name__)


# ============ Mixin 拆分（2026-08-22） ============
# 原 CommentMonitor（803行）按职责拆为三个 Mixin：
# - _base.py: __init__ + 基础工具（原始 L79-L346）
# - _monitor.py: 监控动作（原始 L119-L220、L348-L443）
# - _alerts.py: 预警检测/入库/查询（原始 L445-L867）
# 注释均原样保留，未删除未错位
from ._base import MonitorBaseMixin
from ._monitor import MonitorActionsMixin
from ._alerts import MonitorAlertsMixin


class CommentMonitor(MonitorBaseMixin, MonitorActionsMixin, MonitorAlertsMixin):

    """评论舆情监控器
    
    编排采集/去重/情感分析流程，
    输出舆情预警并持久化。
    
    内部机制：
    - _monitoring_history: bvid -> deque(maxlen=10)，评论数滑动窗口
    - _detect_alerts: 四条预警规则检测
    - _check_comment_surge: 突增检测子流程
    - _check_custom_keywords: 自定义词扫描子流程
    - _save_monitoring_record: 预警入库 + 回调推送
    """





if __name__ == '__main__':
    # 运行任务
    asyncio.run(demo_monitor())
