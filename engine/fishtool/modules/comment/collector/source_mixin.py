"""评论采集器 - 接口辅助

提供：
- _get_video_oid: bvid -> aid（评论API需要 oid）
- _get_user_videos: 用户空间投稿列表（账号批量监控用）
"""
from typing import List, Dict, Any, Optional
from core.exceptions import BilibiliAPIError
from core.logger import get_logger

logger = get_logger(__name__)


class CommentSourceMixin:
    async def _get_video_oid(self, bvid: str) -> Optional[int]:
        """获取视频的oid（aid）
        
        调用 B 站视频详情 API，从 bvid 转换为 aid（oid）。
        评论 API 需要使用 oid 作为参数。
        
        Args:
            bvid: 视频BV号
            
        Returns:
            oid（aid），失败返回 None
        """
        # bvid 转 aid：评论接口只认数字 oid
        # 视频详情接口返回 aid 字段
        try:
            url = "https://api.bilibili.com/x/web-interface/view"
            params = {'bvid': bvid}
            
            data = await self.api.get(url, params=params, need_sign=False)
            
            # 多条件判断
            if data and 'aid' in data:
                return data['aid']
            
            return None
            
        except BilibiliAPIError as e:
            logger.error(f"获取视频 {bvid} 的oid失败: {e}")
            return None

    async def _get_user_videos(self, uid: str, limit: int) -> List[Dict[str, Any]]:
        """获取用户的视频列表
        
        调用 B 站空间 API，获取指定用户的投稿视频列表。
        用于账号批量监控场景。
        
        Args:
            uid: 用户UID
            limit: 数量限制
            
        Returns:
            视频列表 [{bvid, title, aid}]
        """
        # 用户空间接口同样受限频保护
        # 空间接口走默认限频器（非评论专用）
        try:
            # 请求前限频
            await self.rate_limiter.acquire()
            
            url = "https://api.bilibili.com/x/space/wbi/arc/search"
            # 空间投稿接口，ps 上限 30，超过按 30 处理
            params = {
                'mid': uid,
                'pn': 1,
                'ps': min(limit, 30),
            }
            
            data = await self.api.get(url, params=params, need_sign=True)
            
            # api.get() 已经返回 data 字段，检查结构
            if not data or 'list' not in data:
                return []
            
            # data['list'] 包含 'vlist' 数组
            vlist = data['list'].get('vlist', [])
            if not vlist:
                return []
            
            # 提取视频关键字段
            # 从数据中取出目标字段，供后续逻辑使用
            videos = []
            # 只取前 limit 个视频，字段精简为后续采集所需
            for item in vlist[:limit]:
                videos.append({
                    'bvid': item.get('bvid'),
                    'title': item.get('title'),
                    'aid': item.get('aid')
                })
            
            return videos
            
        except BilibiliAPIError as e:
            logger.error(f"获取用户 {uid} 视频列表失败: {e}")
            return []
