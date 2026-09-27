"""评论采集器 - 去重入库

提供：
- _save_comments_to_db: 按 rpid 去重插入，创建视频记录与增量断点
"""
# 从 typing 导入符号
from typing import List, Dict, Any, Set
# 从 datetime 导入符号
from datetime import datetime
# 从 core.database 导入符号
from core.database import get_session, Comment, Video
# 从 core.logger 导入符号
from core.logger import get_logger

logger = get_logger(__name__)


class CommentStorageMixin:
    async def _save_comments_to_db(self, bvid: str, comments: List[Dict[str, Any]]) -> Dict[str, Any]:
        """保存评论到数据库
        # 持久化数据，防止丢失
        
        按 rpid 去重插入，同时创建视频记录与增量断点。
        # 实例化对象并准备使用
        
        Args:
            bvid: 视频BV号
            comments: 评论列表
        """
        session = None
        try:
            # 获取数据库会话对象
            session = get_session()
            
            # 先查询或创建视频记录（评论需要关联到视频表）
            from core.database import Video, Task
            # 根据 bvid 查询视频是否已存在
            video = session.query(Video).filter_by(bvid=bvid).first()
            # 空值/异常保护：不满足条件时跳过
            if not video:
                # 视频记录不存在，创建占位符记录
                video = Video(bvid=bvid, title=f"视频_{bvid}")
                # 加入集合/数据库会话
                session.add(video)
                # 刷新数据库会话
                session.flush()  # 立即刷新获取自增的 video.id
            
            saved_count = 0
            batch_rpids: Set[Any] = set()
            # 同一批数据的重复项尚未 flush，必须先在内存中过滤。
            latest_rpid = None
            # 遍历所有待保存的评论数据
            # 每条评论按 rpid 去重，已存在则跳过
            for comment_data in comments:
                rpid = comment_data.get('rpid')
                # 先过滤本批次重复，避免 ORM 在 commit 时触发唯一键冲突。
                if rpid is None or rpid in batch_rpids:
                    continue
                batch_rpids.add(rpid)
                existing = session.query(Comment).filter_by(rpid=rpid).first()
                
                # 已存在评论只原位补齐缺失的等级/会员字段，不重复插入。
                if existing:
                    changed = False
                    if not existing.level_info and comment_data.get('level_info'):
                        existing.level_info = comment_data['level_info']
                        changed = True
                    if existing.vip is None and comment_data.get('vip') is not None:
                        existing.vip = comment_data['vip']
                        changed = True
                    if changed:
                        saved_count += 1
                    continue
                
                # 创建新的评论ORM对象
                comment = Comment(
                    rpid=comment_data['rpid'],  # 评论唯一ID
                    video_id=video.id,  # 关联视频表ID
                    uid=comment_data['uid'],  # 评论者UID
                    uname=comment_data['uname'],  # 评论者昵称
                    level_info=comment_data.get('level_info'),  # 评论接口等级原始字段
                    vip=comment_data.get('vip'),  # 评论接口会员原始字段
                    content=comment_data['content'],  # 评论正文
                    ctime=comment_data['ctime'],  # 评论时间
                    like=comment_data.get('like', 0),  # 点赞数
                    reply_count=comment_data.get('reply_count', 0),  # 回复数
                    sentiment='neutral',  # 默认情感标签为中性，等待后续情感分析
                )
                # 添加到会话等待提交
                # 将元素加入容器/布局
                session.add(comment)
                # 成功计数+1
                saved_count += 1
                
                # 更新最新rpid（用于下次增量采集的起点）
                # 按数值大小取最大，保证断点单调递增
                if latest_rpid is None or comment_data['rpid'] > latest_rpid:
                    # 赋值并准备后续使用
                    latest_rpid = comment_data['rpid']
            
            # 写入 checkpoint：保存最新 rpid 到 Task 表
            # 有新增评论才写 checkpoint
            # 记录最新 rpid 供下次增量采集使用
            if latest_rpid:
                # 赋值并准备后续使用
                task = Task(
                    task_type='comment_collect',
                    params={'bvid': bvid},
                    status='completed',
                    checkpoint={'last_rpid': latest_rpid},
                    result={'saved_count': saved_count},
                    completed_at=datetime.now(),
                    started_at=datetime.now()
                )
                # 加入集合/数据库会话
                session.add(task)
                logger.info(f"写入 checkpoint: last_rpid={latest_rpid}")
            
            # 提交事务
            session.commit()
            logger.info(f"保存了 {saved_count} 条新评论到数据库")
            self.last_save_result = {'success': True, 'saved_count': saved_count, 'warning': None}
            return self.last_save_result
            
        # 保存失败回滚事务，保证数据库一致性
        # 异常时已提交的评论不受影响
        except Exception as e:
            warning = f"评论已采集但落库失败: {e}"
            logger.exception(warning)
            if session is not None:
                session.rollback()
            self.last_save_result = {'success': False, 'saved_count': 0, 'warning': warning}
            return self.last_save_result
        # 异常处理
        finally:
            # 关闭连接释放资源
            # 释放连接/窗口资源
            if session is not None:
                session.close()
