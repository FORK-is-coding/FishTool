"""评论采集器 - 支持分级采集和严格限频
本模块是评论数据采集的核心组件，提供：

一、分级采集策略
- STRATEGY_FAST: 快速采集，仅获取热门评论（约20条）
- STRATEGY_NORMAL: 普通采集，热门+按时间排序的前100条
- STRATEGY_FULL: 完整采集，合并热门+全部普通评论并去重
适合不同场景（快速预览/常规分析/完整备份）

二、多种采集入口
- collect_video_comments: 单视频采集（按策略分级）
- collect_incremental_comments: 增量采集（基于断点续采）
- collect_user_videos_comments: 账号主页批量采集

三、限频与风控
- 评论接口使用专用限频器（4秒间隔，比普通API更严格）
- 429退避 + 熔断保护，避免触发B站风控

四、持久化
- 评论自动保存到数据库（按 rpid 去重）
- 增量采集断点写入 Task.checkpoint
- 视频记录自动创建（占位标题）

五、数据解析
- _parse_comment_reply: 单条评论标准化
- _parse_comment_replies: 批量解析
- 解析字段：rpid/uid/uname/content/ctime/like/reply_count

依赖：
- bilibili.api: BilibiliAPI 客户端
- bilibili.rate_limiter: RateLimiter 限频器
- core.database: Comment/Video/Task 模型

拆分结构（参照 desktop/pet_window 拆法）：
- base.py: 主类骨架 + 初始化 + 主入口
- strategy_mixin.py: 分级采集策略子流程
- incremental_mixin.py: 增量采集 + 断点查询
- parse_mixin.py: 评论解析
- source_mixin.py: oid/用户视频接口辅助
- storage_mixin.py: 去重入库
- runner.py: 示例入口
"""
from .base import CommentCollectorBase
from .strategy_mixin import CommentStrategyMixin
from .incremental_mixin import CommentIncrementalMixin
from .parse_mixin import CommentParseMixin
from .source_mixin import CommentSourceMixin
from .storage_mixin import CommentStorageMixin


class CommentCollector(CommentCollectorBase, CommentStrategyMixin, CommentIncrementalMixin, CommentParseMixin, CommentSourceMixin, CommentStorageMixin):
    """评论采集器 - 支持分级采集和严格限频

    封装B站评论API的完整采集流程，
    对外提供按策略采集/增量采集/账号批量采集三个入口。
    内部方法分布在各 mixin 中：
    - strategy_mixin: _collect_hot_comments / _collect_normal_comments / _collect_all_comments
    - incremental_mixin: collect_incremental_comments / _get_last_rpid
    - parse_mixin: _parse_comment_reply / _parse_comment_replies
    - source_mixin: _get_video_oid / _get_user_videos
    - storage_mixin: _save_comments_to_db
    """
    pass


from .runner import demo_collect_comments

__all__ = [
    "CommentCollector",
    "CommentCollectorBase",
    "demo_collect_comments",
]
