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


def _coerce_rpid_field(value: Any):
    """把评论接口的 root/parent 字段规整为有效整数或 None。

    契约（语义折叠首版）：仅当字段是 **JSON number**（含 0）时才算"已知"；
    缺字段 / None / bool（True/False）一律判为未知，绝不用默认 0 冒充顶层。

    Args:
        value: 原接口 root/parent 原始值。

    Returns:
        int 或 None：合法数字返回其 int 值（0 合法），否则 None 表示未知。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # 浮点整数值（如 0.0）按整数处理；非整数值判未知。
        return int(value) if value.is_integer() else None
    return None


def classify_comment_context(reply: Dict[str, Any]) -> Dict[str, Any]:
    """从原始评论判定上下文状态与 root/parent rpid。

    - root 与 parent **都明确为 0** -> ``top_level``，root_rpid/parent_rpid 置 None；
    - root 或 parent 明确非 0 -> ``reply``，保留各自 rpid 字符串；
    - 任一字段缺失/None/bool -> ``unknown``，不臆造。

    Args:
        reply: B 站评论接口单条原始数据。

    Returns:
        dict: {root_rpid: str|None, parent_rpid: str|None, context_status: str}
    """
    root = _coerce_rpid_field(reply.get('root'))
    parent = _coerce_rpid_field(reply.get('parent'))
    if root is None or parent is None:
        return {'root_rpid': None, 'parent_rpid': None, 'context_status': 'unknown'}
    if root == 0 and parent == 0:
        return {'root_rpid': None, 'parent_rpid': None, 'context_status': 'top_level'}
    return {
        'root_rpid': str(root) if root else None,
        'parent_rpid': str(parent) if parent else None,
        'context_status': 'reply',
    }


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

        # 会员/等级缺失或空对象一律保持未知，不下沉为“非会员/0 级”。
        raw_level_info = member.get('level_info')
        level_info = raw_level_info if isinstance(raw_level_info, dict) and raw_level_info else None
        raw_vip = member.get('vip')
        vip_present = isinstance(raw_vip, dict) and any(
            key in raw_vip for key in ('vipStatus', 'vipType', 'status', 'type')
        )
        vip = raw_vip if vip_present else None
        if vip is not None:
            is_vip = bool(vip.get('vipStatus') or vip.get('vipType'))
            vip_type = int(vip.get('vipType') or vip.get('type') or 0)
        else:
            is_vip = None
            vip_type = None

        # 缺 ctime / 非法 ctime 保持 None，不由 0 伪造 Unix 纪元时间。
        ctime = None
        raw_ctime = reply.get('ctime')
        if isinstance(raw_ctime, (int, float)) and not isinstance(raw_ctime, bool) and raw_ctime:
            try:
                ctime = datetime.fromtimestamp(int(raw_ctime))
            except (OSError, OverflowError, ValueError):
                ctime = None

        # 上下文判定：仅在原接口字段明确时判顶层/回复，缺字段保持未知
        context = classify_comment_context(reply)

        # 构造标准化评论数据字典
        return {
            'rpid': reply.get('rpid'),  # 评论唯一ID（回复ID）
            'root_rpid': context['root_rpid'],  # 根评论 rpid（顶层为 None）
            'parent_rpid': context['parent_rpid'],  # 父评论 rpid（顶层为 None）
            'context_status': context['context_status'],  # top_level/reply/unknown
            'oid': reply.get('oid'),    # 视频对象ID（aid）
            'uid': member.get('mid'),   # 评论者用户ID
            'uname': member.get('uname', ''),  # 评论者昵称
            'avatar': member.get('avatar', ''),  # 评论者头像URL
            'level_info': level_info,  # 保留评论接口原始等级字段，供本地复用
            'vip': vip,  # 保留评论接口原始会员字段，区分普通/年度大会员
            'level': level_info.get('current_level') if level_info else None,  # 评论时用户等级
            'is_vip': is_vip,  # 是否大会员（未知为 None）
            'vip_type': vip_type,
            'content': content.get('message', ''),  # 评论文本内容
            'ctime': ctime,  # 评论发布时间戳转datetime（缺失为 None）
            'like': reply.get('like', 0),  # 点赞数（支持数）
            'reply_count': reply.get('rcount', 0),  # 回复数（评论下的子评论数）
            'is_hot': is_hot,  # 是否为热门评论标记
            'fetched_at': datetime.now()  # 本次采集的时间戳
        }
