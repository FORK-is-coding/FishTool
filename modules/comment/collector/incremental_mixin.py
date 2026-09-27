"""评论采集器 - 增量采集与断点

提供：
- collect_incremental_comments: 基于上次断点的增量采集
- _get_last_rpid: 优先读 Task.checkpoint，回退查评论表最新 rpid
"""
from typing import List, Dict, Any, Optional
import json
from datetime import datetime
from core.database import get_session, Comment, Video
from core.logger import get_logger

logger = get_logger(__name__)


class CommentIncrementalMixin:
    async def collect_incremental_comments(self, bvid: str) -> List[Dict[str, Any]]:
        """增量采集新评论（基于上次采集的游标）
        
        从上次采集的断点（last_rpid）开始，只获取新增评论。
        适合定时监控场景，避免重复采集。
        
        Args:
            bvid: 视频BV号
            
        Returns:
            新增评论列表，自动保存到数据库并更新断点
            # 持久化数据，防止丢失
        """
        # 增量模式：从上一次断点继续，只抓新增
        # 适合定时任务，避免每次全量扫描浪费配额
        logger.info(f"增量采集视频 {bvid} 的新评论")
        
        # 获取上次采集的最新rpid
        # 从 Task.checkpoint 或评论表推断
        last_rpid = await self._get_last_rpid(bvid)
        
        # 采集最新评论
        new_comments = []
        oid = await self._get_video_oid(bvid)
        if not oid:
            return []
        
        # 初始化分页游标，None 表示第一页（不传 pagination_str）
        pagination_str: Optional[str] = None
        previous_offset: Optional[str] = None
        # 循环分页采集，直到没有更多评论
        while True:
            # 限频控制：确保不超过 B 站 API 限频阈值
            await self.rate_limiter.acquire(endpoint='comment')
            
            # 构造评论 API 请求 URL
            url = "https://api.bilibili.com/x/v2/reply/main"
            # 设置请求参数：oid=视频ID, mode=2表示按时间排序
            params = {
                'oid': oid,        # 视频对象ID
                'type': 1,         # 1表示视频类型
                'mode': 2,         # 2=按时间排序，3=热门排序
                'ps': 20,          # 每页返回20条
            }
            # reply/main 使用游标分页：第一页不传，后续页用上一页返回的
            # cursor.pagination_reply.next_offset 包装成 pagination_str。
            if pagination_str:
                params['pagination_str'] = pagination_str
            
            # 发送 API 请求，评论主接口不需要 WBI 签名
            data = await self.api.get(url, params=params, need_sign=False)
            
            # 检查返回数据有效性
            # 接口异常或结构不符时终止翻页，避免死循环
            if not data or 'replies' not in data:
                break  # 数据异常，结束采集
            
            # 提取评论列表
            # 当前页为空说明已经翻到最后一页
            replies = data['replies']
            if not replies:
                break  # 当前页无评论，结束采集
            
            # 遍历当前页的所有评论
            for reply in replies:
                # 获取评论唯一ID
                rpid = reply.get('rpid')
                # 检查是否到达上次采集断点
                if rpid == last_rpid:
                    # 遇到上次采集的最新评论，说明增量采集完成
                    logger.info(f"遇到上次采集的评论 {rpid}，增量采集结束")
                    # 保存本次新增的评论到数据库
                    if new_comments:
                        await self._save_comments_to_db(bvid, new_comments)
                    return new_comments
                
                # 解析单条评论数据为标准格式
                comment = self._parse_comment_reply(reply, is_hot=False)
                # 添加到新增评论列表
                new_comments.append(comment)
            
            # 取下一页游标：优先 cursor.pagination_reply.next_offset，
            # 兼容旧结构 cursor.next；重复游标直接停止防止死循环。
            cursor = data.get('cursor') or {}
            pagination_reply = cursor.get('pagination_reply') or {}
            next_offset = pagination_reply.get('next_offset') or cursor.get('next')
            if cursor.get('is_end') or next_offset is None:
                break
            offset_text = str(next_offset)
            if offset_text == previous_offset:
                logger.warning('增量评论分页返回重复游标，停止翻页')
                break
            previous_offset = offset_text
            pagination_str = json.dumps({'offset': offset_text}, separators=(',', ':'))
        
        logger.info(f"增量采集完成，新增 {len(new_comments)} 条评论")
        # 保存新增评论到数据库（checkpoint自动更新）
        if new_comments:
            await self._save_comments_to_db(bvid, new_comments)
        return new_comments

    async def _get_last_rpid(self, bvid: str) -> Optional[int]:
        """获取上次采集的最新评论ID
        
        优先从 Task.checkpoint 读取断点，如果没有则从评论表推断。
        用于增量采集，避免重复抓取。
        
        Args:
            bvid: 视频BV号
            
        Returns:
            最新评论rpid，若从未采集则返回 None
        """
        # 断点优先级：Task.checkpoint > 评论表最新 rpid
        # 无断点且无评论时返回 None，走全量采集
        try:
            session = get_session()
            
            # 通过 bvid 查询 video_id
            from core.database import Video, Task
            video = session.query(Video).filter_by(bvid=bvid).first()
            if not video:
                return None
            
            # 优先从 Task.checkpoint 读取断点
            task = session.query(Task).filter_by(
                task_type='comment_collect',
                params={'bvid': bvid}
            ).order_by(Task.created_at.desc()).first()
            
            # 有断点则直接使用
            # 断点存在且含 last_rpid 时直接采用
            # 这是增量采集的核心：从断点之后开始
            if task and task.checkpoint:
                last_rpid = task.checkpoint.get('last_rpid')
                if last_rpid:
                    logger.info(f"从 checkpoint 恢复断点: last_rpid={last_rpid}")
                    return last_rpid
            
            # 回退：查询该视频最新的评论
            latest_comment = session.query(Comment).filter_by(
                video_id=video.id
            ).order_by(Comment.ctime.desc()).first()
            
            if latest_comment:
                return latest_comment.rpid
            
            return None
            
        # 查询失败返回 None，调用方按无断点处理
        # finally 保证会话无论如何都关闭，避免连接泄漏
        except Exception as e:
            logger.error(f"查询最新评论失败: {e}")
            return None
        finally:
            session.close()
