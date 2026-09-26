"""
评论舆情监控器 - 预警 Mixin（规则检测 + 入库 + 查询）

拆分自 monitor.py 原始 L445-L867。
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from collections import deque

from core.logger import get_logger
from core.database import get_session, CommentAlert, Video

logger = get_logger(__name__)


class MonitorAlertsMixin:
    """预警 Mixin：四条规则检测、预警入库、历史查询与已读标记"""


    async def _detect_alerts(self, 
                            bvid: str,
                            raw_comments: List[Dict[str, Any]],
                            processed_comments: List[Dict[str, Any]],
                            sentiment_result: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """检测舆情预警
        
        四条规则依次检查，返回所有命中的预警。
        # 验证状态/条件，决定下一步分支
        
        Args:
            bvid: 视频BV号
            raw_comments: 原始评论
            processed_comments: 处理后的评论
            # 对数据进行加工/分发
            sentiment_result: 情感分析结果
            
        Returns:
            预警列表
        """
        alerts = []
        # 四条预警规则独立检测，命中即加入列表
        # 同一次监控可触发多个不同类型的预警
        
        # 预警检测规则1：负面评论占比异常检测
        if sentiment_result:
            # 从情感分析结果中提取负面占比
            # 从数据中取出目标字段，供后续逻辑使用
            negative_ratio = sentiment_result.get('negative_ratio', 0)
            # 判断是否超过预警阈值（默认30%）
            # 根据条件走向不同处理分支
            if negative_ratio > self.NEGATIVE_RATIO_THRESHOLD:
                # 构造负面突增预警记录
                alerts.append({
                    'type': 'negative_surge',  # 预警类型标识
                    # 根据严重程度分级：>50%为高危，否则中危
                    'level': 'high' if negative_ratio > 0.5 else 'medium',
                    # 预警消息文本
                    'message': f'负面评论占比过高: {negative_ratio:.1%}',
                    # 附加数据供详细分析
                    'data': {
                        'negative_ratio': negative_ratio,  # 当前负面占比
                        'threshold': self.NEGATIVE_RATIO_THRESHOLD  # 预警阈值基准
                    }
                })
        
        # 预警2：评论数突增
        # 对比历史滑动窗口均值
        comment_surge = await self._check_comment_surge(bvid, len(raw_comments))
        # 判断 comment_surge
        # 根据条件走向不同处理分支
        if comment_surge:
            # 追加到列表
            alerts.append(comment_surge)
        
        # 预警3：风险关键词预警
        # 风险评论数超过阈值
        if sentiment_result:
            # 读取字典/配置项
            risk_count = sentiment_result['sentiment_distribution'].get('risk', 0)
            # 边界/有效性检查
            if risk_count >= self.RISK_COUNT_THRESHOLD:
                # 追加到列表
                alerts.append({
                    'type': 'risk_keywords',
                    'level': 'high',
                    'message': f'检测到 {risk_count} 条风险评论',
                    'data': {
                        'risk_count': risk_count,
                        'threshold': self.RISK_COUNT_THRESHOLD
                    }
                })
        
        # 预警4：自定义关键词
        custom_alerts = self._check_custom_keywords(processed_comments)
        # 判断 custom_alerts
        # 根据条件走向不同处理分支
        if custom_alerts:
            # 批量扩展列表
            alerts.extend(custom_alerts)
        
        return alerts

    async def _check_comment_surge(self, bvid: str, current_count: int) -> Optional[Dict[str, Any]]:
        """检查评论数突增
        # 验证状态/条件，决定下一步分支
        
        对比历史平均评论数（最近 10 次记录），判断是否异常突增。
        # 根据条件走向不同处理分支
        # 对数据进行加工/分发
        突增阈值：当前数 > 平均数 * 2
        
        Args:
            bvid: 视频BV号
            current_count: 当前评论数
            
        Returns:
            预警信息（如有），否则返回 None
            # 将结果交回调用方
        """
        # 获取历史记录
        # 定长队列自动淘汰旧数据，均值随窗口滑动
        # 首次监控时创建定长队列
        if bvid not in self._monitoring_history:
            self._monitoring_history[bvid] = deque(maxlen=10)
        
        history = self._monitoring_history[bvid]
        
        # 空值/异常保护：不满足条件时跳过
        if not history:
            # 首次监控，记录基准
            # 没有历史数据无法判断突增
            # 根据条件走向不同处理分支
            history.append({
                'count': current_count,
                'time': datetime.now()
            })
            return None
        
        # 计算平均评论数
        # 对输入做运算得到结果
        avg_count = sum(h['count'] for h in history) / len(history)
        
        # 突增检测
        if current_count > avg_count * self.COMMENT_SURGE_THRESHOLD:
            alert = {
                'type': 'comment_surge',
                'level': 'medium',
                'message': f'评论数突增: 当前{current_count}条，历史平均{avg_count:.0f}条',
                'data': {
                    'current_count': current_count,
                    'avg_count': avg_count,
                    'surge_ratio': current_count / avg_count if avg_count > 0 else 0
                }
            }
            
            # 记录当前值
            history.append({
                'count': current_count,
                'time': datetime.now()
            })
            
            return alert
        
        # 记录当前值
        history.append({
            'count': current_count,
            'time': datetime.now()
        })
        
        return None

    def _check_custom_keywords(self, comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """检查自定义关键词
        # 验证状态/条件，决定下一步分支
        
        扫描评论内容，匹配用户添加的自定义关键词。
        # 将元素加入容器/布局
        返回匹配的评论样本和关键词列表。
        # 将结果交回调用方
        
        Args:
            comments: 评论列表
            
        Returns:
            预警列表
        """
        # 无自定义关键词直接返回
        # 空集合时跳过全量扫描，省一次遍历
        # 对集合内每个元素执行相同处理
        if not self.custom_keywords:
            return []
        
        alerts = []
        matched_comments = []
        
        # 遍历评论匹配关键词
        # 对集合内每个元素执行相同处理
        for comment in comments:
            # 读取字典/配置项
            content = comment.get('content', '')
            # 赋值并准备后续使用
            matched_kws = [kw for kw in self.custom_keywords if kw in content]
            
            # 判断 matched_kws
            # 根据条件走向不同处理分支
            if matched_kws:
                # 追加到列表
                matched_comments.append({
                    'rpid': comment.get('rpid'),
                    'content': content[:100],
                    'keywords': matched_kws
                })
        
        # 有命中则生成预警
        if matched_comments:
            # 追加到列表
            alerts.append({
                'type': 'custom_keywords',
                'level': 'medium',
                'message': f'匹配到 {len(matched_comments)} 条自定义关键词评论',
                'data': {
                    'matched_count': len(matched_comments),
                    'samples': matched_comments[:5]
                }
            })
        
        return alerts

    async def _save_monitoring_record(self, 
                                     bvid: str, 
                                     comments: List[Dict[str, Any]],
                                     alerts: List[Dict[str, Any]]):
        """保存监控记录和预警
        # 持久化数据，防止丢失
        
        预警写入 CommentAlert 表，同时触发回调推送。
        
        Args:
            bvid: 视频BV号
            comments: 评论列表
            alerts: 预警列表
        """
        # 预警入库链路：CommentAlert 表 + 可选回调推送
        # 事务失败回滚，保证预警不丢失
        try:
            # 赋值并准备后续使用
            session = get_session()
            
            # 通过 bvid 查询 video_id
            from core.database import Video
            # 取第一条记录
            video = session.query(Video).filter_by(bvid=bvid).first()
            # 空值/异常保护：不满足条件时跳过
            if not video:
                # 如果视频不存在，创建一个基本记录
                video = Video(bvid=bvid, title=f"视频_{bvid}")
                # 加入集合/数据库会话
                session.add(video)
                # 刷新数据库会话
                session.flush()
            
            # 保存预警
            for alert in alerts:
                # 赋值并准备后续使用
                comment_alert = CommentAlert(
                    video_id=video.id,
                    alert_type=alert['type'],
                    alert_level=alert['level'],
                    message=alert['message'],
                    details=alert.get('data', {}),
                    is_read=False
                )
                # 加入集合/数据库会话
                session.add(comment_alert)
                
                # 如果有回调函数，实时推送预警
                # 回调失败只记日志，不影响入库主流程
                # 如 WebSocket 推送给桌宠/前端
                if self.alert_callback:
                    # 异常保护：局部失败不影响主流程
                    try:
                        alert_data = {
                            'bvid': bvid,
                            'video_id': video.id,
                            'type': alert['type'],
                            'level': alert['level'],
                            'message': alert['message'],
                            'details': alert.get('data', {}),
                            'created_at': datetime.now().isoformat()
                        }
                        # 异步等待结果
                        await self.alert_callback(alert_data)
                    # 异常处理
                    except Exception as e:
                        logger.error(f"调用预警回调失败: {e}")
            
            # 提交事务
            session.commit()
            logger.info(f"保存了 {len(alerts)} 条预警记录")
            
        except Exception as e:
            logger.error(f"保存监控记录失败: {e}")
            # 回滚事务
            session.rollback()
        # 异常处理
        finally:
            # 关闭连接释放资源
            # 释放连接/窗口资源
            session.close()

    async def get_alerts(self, 
                        # 赋值并准备后续使用
                        bvid: Optional[str] = None,
                        # 赋值并准备后续使用
                        level: Optional[str] = None,
                        # 赋值并准备后续使用
                        is_read: Optional[bool] = False,
                        # 赋值并准备后续使用
                        limit: int = 50) -> List[Dict[str, Any]]:
        """查询预警记录
        
        从数据库检索历史预警，支持多条件过滤。
        # 剔除不符合条件的数据
        可用于预警面板展示、统计分析等场景。
        # 将内容呈现到界面上
        
        Args:
            bvid: 视频BV号（可选）
            level: 预警级别（可选，如 high/medium/low）
            is_read: 是否已读（可选）
            limit: 返回数量上限
            # 将结果交回调用方
            
        Returns:
            预警列表，按创建时间倒序
            # 实例化对象并准备使用
        """
        # 查询链路：多条件叠加过滤，无条件的查询全量
        # 返回最近 limit 条，按时间倒序排列
        # 将结果交回调用方
        try:
            # 赋值并准备后续使用
            session = get_session()
            
            # 基础查询
            query = session.query(CommentAlert)
            
            # 按视频过滤
            # 传入的是 bvid，直接与 video_id 比对
            if bvid:
                # 按条件过滤查询
                # 剔除不符合条件的数据
                query = query.filter_by(video_id=bvid)
            
            # 按级别过滤
            # 剔除不符合条件的数据
            if level:
                # 按条件过滤查询
                # 剔除不符合条件的数据
                query = query.filter_by(alert_level=level)
            
            # 按已读状态过滤
            # 剔除不符合条件的数据
            if is_read is not None:
                # 按条件过滤查询
                # 剔除不符合条件的数据
                query = query.filter_by(is_read=is_read)
            
            # 按时间倒序取最新 limit 条
            # 最新预警优先展示，便于运营及时处理
            alerts = query.order_by(CommentAlert.created_at.desc()).limit(limit).all()
            
            # 序列化结果
            # ORM 对象转普通字典，避免会话关闭后访问报错
            # 释放连接/窗口资源
            result = []
            # 遍历 alerts 逐项处理
            # 对集合内每个元素执行相同处理
            for alert in alerts:
                # 追加到列表
                result.append({
                    'id': alert.id,
                    'video_id': alert.video_id,
                    'type': alert.alert_type,
                    'level': alert.alert_level,
                    'message': alert.message,
                    'details': alert.details,
                    'is_read': alert.is_read,
                    'created_at': alert.created_at.isoformat() if alert.created_at else None
                })
            
            return result
            
        except Exception as e:
            logger.error(f"查询预警失败: {e}")
            return []
        # 异常处理
        finally:
            # 关闭连接释放资源
            # 释放连接/窗口资源
            session.close()

    async def mark_alert_read(self, alert_id: int) -> bool:
        """标记预警为已读
        
        Args:
            alert_id: 预警ID
            
        Returns:
            是否成功
        """
        # 幂等操作：重复标记已读不报错
        # 返回布尔值供调用方判断是否找到记录
        # 根据条件走向不同处理分支
        try:
            # 赋值并准备后续使用
            session = get_session()
            
            # 查找目标预警
            alert = session.query(CommentAlert).filter_by(id=alert_id).first()
            # 空值/异常保护：不满足条件时跳过
            if not alert:
                return False
            
            # 标记已读并提交
            # 单字段更新，事务提交后立即生效
            alert.is_read = True
            # 提交事务
            session.commit()
            
            return True
            
        except Exception as e:
            logger.error(f"标记预警失败: {e}")
            # 回滚事务
            session.rollback()
            return False
        # 异常处理
        finally:
            # 关闭连接释放资源
            # 释放连接/窗口资源
            session.close()
