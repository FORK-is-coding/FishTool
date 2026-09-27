"""评论采集器 - 分级采集策略

提供三个采集子流程：
- _collect_hot_comments: 热门评论（mode=3）
- _collect_normal_comments: 普通评论按时间翻页（mode=2）
- _collect_all_comments: 完整采集（热门+全量普通，rpid去重）
"""
import json
from typing import List, Dict, Any, Optional, Set
from core.exceptions import BilibiliAPIError
from core.logger import get_logger

logger = get_logger(__name__)


class CommentStrategyMixin:
    async def _collect_hot_comments(self, bvid: str) -> List[Dict[str, Any]]:
        """采集热门评论
        
        调用 B 站评论 API mode=3，获取热门评论（通常 ~20 条）。
        热门评论由点赞数、回复数等综合排序。
        
        Args:
            bvid: 视频BV号
            
        Returns:
            热门评论列表
        """
        # 热门评论是运营分析的主力数据，优先采集
        # 单次约 20 条，耗时低、命中率高
        logger.info(f"采集视频 {bvid} 的热门评论")
        
        try:
            # 限频：评论接口走专用限频器
            await self.rate_limiter.acquire(endpoint='comment')
            
            # 获取视频oid（aid）
            # 评论API需要 aid 作为 oid 参数
            oid = await self._get_video_oid(bvid)
            if not oid:
                return []
            
            # B站评论API - 热门评论
            # mode=3 按热度排序，返回点赞/回复最多的评论
            url = "https://api.bilibili.com/x/v2/reply/main"
            params = {
                'oid': oid,   # 视频ID
                'type': 1,    # 1表示视频
                'mode': 3,    # 3表示热门评论
                'ps': 20,     # 每页20条
            }
            
            data = await self.api.get(url, params=params, need_sign=False)
            
            # 数据异常时返回空列表
            # 记录 warning 便于排查接口问题
            if not data or 'replies' not in data:
                logger.warning(f"视频 {bvid} 热门评论返回数据异常")
                return []
            
            # 解析评论列表
            comments = self._parse_comment_replies(data['replies'], is_hot=True)
            logger.info(f"获取到 {len(comments)} 条热门评论")
            
            return comments
            
        except BilibiliAPIError as e:
            logger.error(f"采集热门评论失败: {e}")
            return []

    async def _collect_normal_comments(self, bvid: str, limit: int = 100) -> List[Dict[str, Any]]:
        """采集普通评论（按时间排序）
        
        调用 B 站评论 API mode=2，按时间倒序获取评论。
        分页采集直到达到 limit 或没有更多评论。
        
        Args:
            bvid: 视频BV号
            limit: 限制数量
            
        Returns:
            普通评论列表
        """
        # 普通评论按时间倒序翻页，直到达到 limit
        # 每页 20 条，翻页期间持续限频防 429
        logger.info(f"采集视频 {bvid} 的普通评论，限制 {limit} 条")
        
        try:
            # 获取视频 oid
            oid = await self._get_video_oid(bvid)
            if not oid:
                return []
            
            comments: List[Dict[str, Any]] = []
            seen_rpids: Set[Any] = set()
            ps = 20  # 每页20条
            pagination_str: Optional[str] = None

            # reply/main 使用 cursor，而不是 pn；重复页立即停止防止虚高统计。
            while len(comments) < limit:
                await self.rate_limiter.acquire(endpoint='comment')
                params = {
                    'oid': oid,
                    'type': 1,
                    'mode': 2,  # 2 表示按时间排序
                    'ps': ps,
                }
                if pagination_str:
                    params['pagination_str'] = pagination_str

                data = await self.api.get(
                    "https://api.bilibili.com/x/v2/reply/main",
                    params=params,
                    need_sign=False,
                )
                replies = data.get('replies') if data else None
                if not replies:
                    break

                page_comments = self._parse_comment_replies(replies, is_hot=False)
                page_rpids = {item.get('rpid') for item in page_comments if item.get('rpid') is not None}
                new_comments = [item for item in page_comments if item.get('rpid') not in seen_rpids]
                # API 忽略游标或返回重复页时停止，避免无限重复采集。
                if not new_comments:
                    logger.warning('普通评论分页返回重复 rpid，停止翻页')
                    break
                seen_rpids.update(page_rpids)
                comments.extend(new_comments)
                # 进度与日志同源：翻页后立即更新 collected，供进度条轮询
                # 注意不在此处覆盖 limit：limit 由 base.py 按策略预置（normal=100 / full=None）
                self._update_progress(bvid, phase='normal', collected=len(comments))
                logger.info(f"已采集 {len(comments)} 条去重后的普通评论")

                cursor = data.get('cursor') or {}
                pagination_reply = cursor.get('pagination_reply') or {}
                next_offset = pagination_reply.get('next_offset') or cursor.get('next')
                if cursor.get('is_end') or not next_offset:
                    break
                # B站接口要求将下一页 offset 包装到 pagination_str JSON 中。
                pagination_str = json.dumps({'offset': str(next_offset)}, separators=(',', ':'))

            return comments[:limit]
            
        except BilibiliAPIError as e:
            logger.error(f"采集普通评论失败: {e}")
            return []

    async def _collect_all_comments(self, bvid: str, max_count: Optional[int]) -> List[Dict[str, Any]]:
        """采集所有评论（完整采集）
        
        合并热门评论 + 全部普通评论，基于 rpid 去重。
        适合需要完整评论数据的场景（如数据分析、备份）。
        
        Args:
            bvid: 视频BV号
            max_count: 最大数量限制
            
        Returns:
            所有评论列表（已去重）
        """
        # 完整采集 = 热门 + 全量普通评论
        # 大视频可能上万条，调用方应谨慎使用
        logger.info(f"开始完整采集视频 {bvid} 的所有评论")
        
        # 先采集热门评论
        hot_comments = await self._collect_hot_comments(bvid)
        # 热门阶段完成，回写进度；limit=None 表示全量无上限
        self._update_progress(bvid, phase='full', collected=len(hot_comments), limit=max_count)
        
        # 再采集全部普通评论
        normal_comments = await self._collect_normal_comments(bvid, limit=max_count or 10000)
        
        # 复用统一合并逻辑，确保 full 与 normal 的 rpid 语义一致。
        all_comments = self._merge_comments_by_rpid(hot_comments + normal_comments)
        
        # 全量合并完成后回写最终进度（普通翻页内已逐步更新）
        self._update_progress(bvid, phase='full', collected=len(all_comments), limit=max_count)
        
        # 去重后的结果顺序保持：热门在前，普通在后
        logger.info(f"完整采集完成，共 {len(all_comments)} 条评论")
        return all_comments
