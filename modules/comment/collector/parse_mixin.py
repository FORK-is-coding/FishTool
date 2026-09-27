"""评论采集器 - 评论解析

提供：
- _parse_comment_replies: 批量解析
- _parse_comment_reply: 单条评论标准化（rpid/uid/uname/content/ctime/like/reply_count）
"""
# 从 typing 导入符号
from typing import List, Dict, Any
# 从 datetime 导入符号
from datetime import datetime
# 从 core.logger 导入符号
from core.logger import get_logger

logger = get_logger(__name__)


class CommentParseMixin:
    def _parse_comment_replies(self, replies: List[Dict[str, Any]], is_hot: bool = False) -> List[Dict[str, Any]]:
        """批量解析评论数据
        # 将原始文本转为结构化数据
        # 将数据从一种形态映射为另一种
        
        遍历原始评论列表，调用 _parse_comment_reply 进行逐条解析。
        # 对集合内每个元素执行相同处理
        # 对数据进行加工/分发
        
        Args:
            replies: 评论原始数据列表
            is_hot: 是否为热门评论
            
        Returns:
            解析后的评论列表
            # 将原始文本转为结构化数据
            # 将数据从一种形态映射为另一种
        """
        # 批量解析：过滤空数据项
        # 单条解析失败不影响其他评论
        # 列表推导式批量解析，过滤空数据
        # 剔除不符合条件的数据
        return [self._parse_comment_reply(reply, is_hot) for reply in replies if reply]

    def _parse_comment_reply(self, reply: Dict[str, Any], is_hot: bool = False) -> Dict[str, Any]:
        """解析单条评论数据
        # 将原始文本转为结构化数据
        # 将数据从一种形态映射为另一种
        
        从 B 站 API 返回的原始数据中提取核心字段：
        # 从数据中取出目标字段，供后续逻辑使用
        rpid, uid, uname, content, ctime, like, reply_count 等。
        
        Args:
            reply: 评论原始数据
            is_hot: 是否为热门评论
            
        Returns:
            解析后的标准化评论字典
            # 将原始文本转为结构化数据
            # 将数据从一种形态映射为另一种
        """
        # 标准化输出：所有字段统一为业务层所需格式
        # 缺省字段用空值兜底，保证下游不崩
        # 提取评论者信息（用户对象）
        # 从数据中取出目标字段，供后续逻辑使用
        member = reply.get('member', {})
        # 提取评论正文对象
        # 从数据中取出目标字段，供后续逻辑使用
        content = reply.get('content', {})
        
        level_info = member.get('level_info') or {}
        vip = member.get('vip') or {}

        # 构造标准化评论数据字典
        return {
            'rpid': reply.get('rpid'),  # 评论唯一ID（回复ID）
            'oid': reply.get('oid'),    # 视频对象ID（aid）
            'uid': member.get('mid'),   # 评论者用户ID
            'uname': member.get('uname', ''),  # 评论者昵称
            'avatar': member.get('avatar', ''),  # 评论者头像URL
            'level_info': level_info,  # 保留评论接口原始等级字段，供本地复用
            'vip': vip,  # 保留评论接口原始会员字段，区分普通/年度大会员
            'level': level_info.get('current_level'),  # 评论时用户等级
            'is_vip': bool(vip.get('vipStatus') or vip.get('vipType')),  # 是否大会员
            'vip_type': int(vip.get('vipType') or vip.get('type') or 0),
            'content': content.get('message', ''),  # 评论文本内容
            'ctime': datetime.fromtimestamp(reply.get('ctime', 0)),  # 评论发布时间戳转datetime
            'like': reply.get('like', 0),  # 点赞数（支持数）
            'reply_count': reply.get('rcount', 0),  # 回复数（评论下的子评论数）
            'is_hot': is_hot,  # 是否为热门评论标记
            'fetched_at': datetime.now()  # 本次采集的时间戳
        }
