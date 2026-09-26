"""评论采集器 - 主类骨架

保留 CommentCollector 的核心入口与初始化逻辑：
- 类常量 STRATEGY_FAST/NORMAL/FULL
- __init__ 初始化 API / 限频器 / 断点缓存
- collect_video_comments 主入口（按策略分发）
- _merge_comments_by_rpid 入库前去重工具

采集策略 / 增量 / 解析 / 接口辅助 / 入库
分别由 strategy_mixin / incremental_mixin / parse_mixin /
source_mixin / storage_mixin 提供，在 collector/__init__.py 组合。
"""
from typing import List, Dict, Any, Optional, Set
from datetime import datetime
from bilibili.api import BilibiliAPI
from bilibili.rate_limiter import RateLimiter
from core.logger import get_logger

logger = get_logger(__name__)


# 策略常量：控制采集深度
# fast=仅热门，normal=热门+普通100条，full=全量

# 采集进度存储：bvid -> 进度字典
# 供 web 进度接口轮询展示，字段与日志"已采集 N 条"保持一致
# 键为 bvid，值为 {phase, collected, limit, finished, error, updated_at}
_COLLECT_PROGRESS: Dict[str, Dict[str, Any]] = {}


class CommentCollectorBase:
    """评论采集器基类 - 核心入口与初始化

    保留采集器的主入口 collect_video_comments 与
    初始化逻辑，说明文档见 collector/__init__.py。
    """
    STRATEGY_FAST = 'fast'       # 快速采集（仅热门评论）
    STRATEGY_NORMAL = 'normal'   # 普通采集（热门+部分普通）
    STRATEGY_FULL = 'full'       # 完整采集（全部评论）

    def __init__(self, api: BilibiliAPI, rate_limiter: Optional[RateLimiter] = None):
        """初始化评论采集器
        
        Args:
            api: B站API实例
            rate_limiter: 限频器（专用于评论）
        """
        # 保存B站API客户端实例引用
        self.api = api
        # 评论采集使用专用限频器，4秒间隔（比普通API更严格）
        # 如果未传入限频器，则创建默认配置的限频器实例
        # 未注入限频器时使用默认配置
        # 评论接口限频更严：4 秒/次，429 退避最长 10 分钟
        self.rate_limiter = rate_limiter or RateLimiter(
            rate=4.0,  # 4秒间隔
            retry_delays=[30, 60, 120, 300, 600],
            max_429_count=5
        )
        # 增量采集的游标缓存
        # 键为 bvid，值为断点数据（预留扩展）
        # 记录最近一次持久化结果，供 API 将落库异常明确反馈给调用方。
        self.last_save_result: Dict[str, Any] = {
            'success': True,
            'saved_count': 0,
            'warning': None,
        }
        self._cursor_cache: Dict[str, Any] = {}

    def _update_progress(self, bvid: str, **fields: Any) -> None:
        """更新采集进度（内存态，供前端进度条轮询）。

        字段与 strategy_mixin 每页日志保持同源：
        - phase: hot/normal/full/done
        - collected: 已去重累计条数
        - limit: 快速模式的条数上限；None 表示全量
        - finished: 是否结束

        Args:
            bvid: 视频BV号
            **fields: 要更新的进度字段
        """
        entry = _COLLECT_PROGRESS.setdefault(bvid, {
            'bvid': bvid,
            'phase': 'hot',
            'collected': 0,
            'limit': None,
            'finished': False,
            'error': None,
            'updated_at': datetime.now().isoformat(),
        })
        entry.update(fields)
        entry['updated_at'] = datetime.now().isoformat()

    @classmethod
    def get_progress(cls, bvid: str) -> Dict[str, Any]:
        """读取指定视频的采集进度，供进度接口返回。"""
        return _COLLECT_PROGRESS.get(bvid) or {}

    @classmethod
    def clear_progress(cls, bvid: str) -> None:
        """清理指定视频的采集进度记录。"""
        _COLLECT_PROGRESS.pop(bvid, None)

    async def collect_video_comments(self, 
                                     bvid: str,
                                     strategy: str = STRATEGY_NORMAL,
                                     max_count: Optional[int] = None) -> List[Dict[str, Any]]:
        """采集单个视频的评论
        
        根据策略执行不同深度的采集：
        - fast: 仅热门评论（~20条）
        - normal: 热门 + 部分普通评论（~100条）
        - full: 全部评论（可能数千条）
        
        Args:
            bvid: 视频BV号
            strategy: 采集策略（fast/normal/full）
            max_count: 最大采集数量（None表示不限制）
            
        Returns:
            评论列表，自动保存到数据库
            # 持久化数据，防止丢失
        """
        # 策略分发：fast 只取热门，normal 取热门+普通，full 全量
        # 所有路径最终统一保存并返回评论列表
        logger.info(f"开始采集视频 {bvid} 的评论，策略: {strategy}")
        
        comments = []
        
        # 按策略分发到不同的采集子流程
        if strategy == self.STRATEGY_FAST:
            # 快速模式：仅采集热门评论
            comments = await self._collect_hot_comments(bvid)
            # 进度标记完成：fast 只采热门，无普通翻页
            self._update_progress(bvid, phase='done', collected=len(comments), finished=True)
        elif strategy == self.STRATEGY_NORMAL:
            # 普通模式：热门 + 部分普通评论
            # 进度预置为普通阶段，limit=100 与日志"限制 100 条"一致
            self._update_progress(bvid, phase='normal', collected=0, limit=100)
            hot_comments = await self._collect_hot_comments(bvid)
            # 热门采集完成后先回写进度，再进入普通翻页
            self._update_progress(bvid, phase='normal', collected=len(hot_comments), limit=100)
            normal_comments = await self._collect_normal_comments(bvid, limit=100)
            # 热门与普通来源会重叠；普通策略也必须在返回前按 rpid 合并。
            comments = self._merge_comments_by_rpid(hot_comments + normal_comments)
        else:  # STRATEGY_FULL
            # 完整模式：所有评论
            # 进度预置为全量阶段；max_count 为 None 时 limit 置空表示不设上限
            self._update_progress(bvid, phase='full', collected=0, limit=max_count)
            comments = await self._collect_all_comments(bvid, max_count)
        
        # 保存到数据库
        # 入库前先按 rpid 去重，避免重复记录
        await self._save_comments_to_db(bvid, comments)
        
        # 进度收尾：统一标记完成，collected 取最终去重后条数
        self._update_progress(bvid, phase='done', collected=len(comments), finished=True)
        
        # 最终统一出口：所有策略路径都走这里返回
        logger.info(f"视频 {bvid} 评论采集完成，共 {len(comments)} 条")
        return comments

    def _merge_comments_by_rpid(self, comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 rpid 稳定合并评论，保留首次出现记录。

        Args:
            comments: 待合并的热门、普通或分页评论列表。

        Returns:
            去重后的评论列表；缺失 rpid 的异常记录会被跳过并写入警告日志。
        """
        seen_rpids: Set[Any] = set()
        merged_comments: List[Dict[str, Any]] = []
        for comment in comments:
            # rpid 是评论主键；没有该值的记录无法安全保存或统计。
            rpid = comment.get('rpid')
            if rpid is None:
                logger.warning('跳过缺少 rpid 的评论记录')
                continue
            if rpid not in seen_rpids:
                seen_rpids.add(rpid)
                merged_comments.append(comment)
        return merged_comments
