"""
账号自我分析器 - 拉取自己账号的全量数据并进行诊断

本模块实现"账号自诊"的数据采集层：

一、数据采集流程（fetch_self_data）
1. 基础信息：昵称/头像/签名/等级/认证状态
2. 粉丝数据：粉丝数/关注数（relation_stat）
3. 投稿数据：全量视频列表（分页拉取）
4. 投稿节奏：周更频率/最长断更/近30天投稿
5. 互动率：粉丝触达率/评论率/收藏率
6. 三方数据：尝试从 ZeroRoku 补充粉丝增长曲线

二、核心分析
- _analyze_video_stats: 播放/评论/收藏汇总与均值
- _analyze_post_rhythm: 时间跨度、投稿频率、断更间隔
- _calculate_engagement: 三种互动率指标
- benchmark_with_category: 与分区热点数据的对比
  （使用 Hotspot.heat_score 作为分区均值参照，
   计算 p25/p50/p75 分位与自身位置判定）
   # 对输入做运算得到结果

三、数据可用性标记
- data_availability 记录各维度是否成功获取
# 读取数据并赋值给当前作用域变量
- 完播率/观众画像/流量来源标记为 False
  （仅创作中心可见，公开 API 拿不到）

四、benchmark 对比
- 参照数据：Hotspot 表（热点发现模块积累的同分区记录）
- 参照指标：heat_score（热度分）
- 输出：分区均值 + p25/p50/p75 分位 + 自身档位判定
- 无参照样本时返回 has_benchmark=False 提示先积累
# 将结果交回调用方

依赖：
- bilibili.api / bilibili.rate_limiter
- modules.up_analyzer.data_fetcher: UPDataFetcher
- core.database: Video/VideoStats/Hotspot

错误处理：
# 对数据进行加工/分发
- 采集过程任何异常会向上抛出，由调用方决定是否展示
# 将内容呈现到界面上
- 单接口失败（如三方数据）降级为对应维度不可用，
  不影响其他维度数据
"""
import asyncio
# 从 typing 导入符号
from collections import Counter
from typing import Dict, Any, Optional, List
# 从 datetime 导入符号
from datetime import datetime, timedelta
# 从 sqlalchemy.orm 导入符号
from sqlalchemy.orm import Session
# 导入模块
import logging

# 从 core.exceptions 导入符号
from core.exceptions import BilibiliAPIError
# 从 core.logger 导入符号
from core.logger import get_logger
# 从 core.database 导入符号
from core.database import get_session, Video, VideoStats, Hotspot, UPMaster
# 从 bilibili.api 导入符号
from bilibili.api import BilibiliAPI
# 从 bilibili.rate_limiter 导入符号
from bilibili.rate_limiter import RateLimiter
# 从 modules.up_analyzer.data_fetcher 导入符号
from modules.up_analyzer.data_fetcher import UPDataFetcher

logger = get_logger(__name__)


class SelfAnalyzer:
    """账号自我分析器 - 全量数据采集与诊断
    
    采集自身账号公开数据，
    输出结构化统计供诊断报告使用。
    
    对外主入口：
    - fetch_self_data(uid): 全量采集入口
    - benchmark_with_category(self_data, category): 分区对比
    
    内部协作：
    - UPDataFetcher：封装三方数据源（ZeroRoku）获取
    # 读取数据并赋值给当前作用域变量
    - BilibiliAPI：B站公开接口
    - RateLimiter：请求限频
    
    数据流：
    fetch_self_data 采集 → 各维度分析函数处理 →
    # 对数据进行加工/分发
    data_availability 标记可用性 → 报告模块消费
    """
    
    def __init__(self, bili_api: BilibiliAPI, rate_limiter: RateLimiter):
        """初始化自我分析器
        # 设置初始值/默认状态，避免后续空引用
        
        Args:
            bili_api: B站API客户端
            rate_limiter: 限频器
        """
        self.bili_api = bili_api
        self.rate_limiter = rate_limiter
        self.data_fetcher = UPDataFetcher(bili_api, rate_limiter)

    @staticmethod
    def _to_non_negative_int(value: Any) -> Optional[int]:
        """将接口字段转换为非负整数。

        Args:
            value: 接口返回的待转换字段。

        Returns:
            合法时返回非负整数，缺失或非法时返回 ``None``。
        """
        if value is None or isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    async def _fetch_fan_stats(
        self,
        uid: int,
        user_data: Dict[str, Any],
    ) -> tuple[Dict[str, Optional[int]], bool, Optional[str]]:
        """获取自诊所需粉丝统计，并在可判定时使用公开名片兜底。

        Args:
            uid: B站用户 UID。
            user_data: ``get_user_info`` 返回的标准化基础资料。

        Returns:
            ``(粉丝统计, 是否可用, 错误原因)``。relation 接口异常时粉丝数
            返回 ``None``；接口返回零或缺字段时，仅使用明确存在的 card
            follower 兜底，避免把接口不可用伪装成真实零粉丝。
        """
        try:
            await self.rate_limiter.acquire('normal')
            relation_stat = await self.bili_api.get_user_relation_stat(uid)
        except Exception as exc:
            message = str(exc)
            logger.warning("[自诊] 粉丝关系接口不可用 (uid=%s): %s", uid, message)
            return {'follower': None, 'following': None}, False, message

        relation_data = (
            relation_stat.get('data')
            if isinstance(relation_stat, dict) and isinstance(relation_stat.get('data'), dict)
            else {}
        )
        relation_follower = self._to_non_negative_int(relation_data.get('follower'))
        card_follower = (
            self._to_non_negative_int(user_data.get('follower'))
            if 'follower' in user_data
            else None
        )

        # relation 返回正数时可信；零或缺字段可能是风控降级，仅接受 card 的明确值。
        follower = relation_follower if relation_follower is not None and relation_follower > 0 else card_follower
        following = self._to_non_negative_int(relation_data.get('following'))
        if follower is None:
            message = "relation/stat 未返回可靠粉丝数，基础资料也没有 card follower 兜底"
            logger.warning("[自诊] %s (uid=%s)", message, uid)
            return {'follower': None, 'following': following}, False, message

        if relation_follower in (None, 0):
            logger.info("[自诊] relation 粉丝数不可用，已使用 card follower 兜底: %s", follower)
        return {'follower': follower, 'following': following}, True, None
    
    async def fetch_self_data(self, uid: int) -> Dict[str, Any]:
        """拉取自己账号的全量数据
        
        按顺序采集基础信息/粉丝/投稿/节奏/互动/三方数据。
        
        采集顺序设计：
        1. 基础信息（user_info 接口）—— 最基础资料
        2. 粉丝数（relation/stat 接口）—— 后续互动率分母
        3. 投稿列表（分页）—— 播放/评论/收藏统计源
        4. 投稿节奏 —— 基于投稿时间戳计算
        # 对输入做运算得到结果
        5. 互动率 —— 依赖粉丝数与投稿统计
        6. 三方数据（ZeroRoku）—— 补充粉丝增长曲线
        
        可用性标记：
        - 每个维度成功后置 True
        - 完播率/观众画像/流量来源恒为 False
          （仅创作中心可见，公开 API 无法获取）
          # 读取数据并赋值给当前作用域变量
        
        Args:
            uid: 自己的B站UID
            
        Returns:
            完整账号数据字典
        """
        logger.info(f"===== 开始采集账号{uid}的全量数据 =====")
        
        # 初始化结果结构，各维度独立存放
        # 先构造空骨架，后续步骤逐个填充对应键值
        # 结果字典分层设计：
        # - basic_info: 基础资料（昵称/头像/签名等）
        # - fan_stats: 粉丝与关注数
        # - video_stats: 视频播放/评论/收藏统计
        # - engagement_metrics: 互动率指标
        # - post_rhythm: 投稿节奏分析
        # - third_party_data: 三方平台补充数据（可选）
        # - data_availability: 各维度数据可用性标记
        # - fetched_at: 采集时间戳，用于报告展示
        # 将内容呈现到界面上
        result = {
            'uid': uid,
            'basic_info': {},
            'fan_stats': {},
            'video_stats': {},
            'engagement_metrics': {},
            'post_rhythm': {},
            'tag_cloud': {
                'video_count': 0,
                'tagged_video_count': 0,
                'tag_count': 0,
                'word_frequency': {},
            },
            'data_availability': {
                'basic_info': False,
                'fan_stats': False,
                'video_stats': False,
                'post_rhythm': False,
                'engagement_metrics': False,
                'tag_cloud': False,
            },
            'data_errors': {},
            'fetched_at': datetime.now().isoformat()
        }
        
        # 异常保护：局部失败不影响主流程
        try:
            # 1. 基础信息（粉丝数、等级、认证状态）
            logger.info("[自诊] 采集基础信息...")
            # 请求前先获取限频令牌，遵守接口频率限制
            await self.rate_limiter.acquire('normal')
            user_info = await self.bili_api.get_user_info(uid)
            
            # 提取基础信息字段
            # 只保留诊断需要的字段，避免把完整响应塞进结果
            user_data: Dict[str, Any] = {}
            if user_info and user_info.get('data'):
                user_data = user_info['data']
                result['basic_info'] = {
                    'name': user_data.get('name'),
                    'face': user_data.get('face'),
                    'sign': user_data.get('sign'),
                    'level': user_data.get('level'),
                    'birthday': user_data.get('birthday'),
                    'official': user_data.get('official'),
                }
                result['data_availability']['basic_info'] = True
            
            # 1.5 获取粉丝数。relation 的零值或缺字段仅在 card 有明确值时兜底。
            fan_stats, fan_stats_available, fan_stats_error = await self._fetch_fan_stats(
                uid,
                user_data,
            )
            result['fan_stats'] = fan_stats
            result['data_availability']['fan_stats'] = fan_stats_available
            if fan_stats_error:
                result['data_errors']['relation_stat'] = fan_stats_error
            if fan_stats_available:
                logger.info(f"[自诊] 粉丝数: {fan_stats['follower']:,}")
            try:
                # 充电人数是账号基础画像的一部分，失败时保留 0 并记录来源。
                await self.rate_limiter.acquire('normal')
                charge_data = await self.bili_api.get_charge_count(uid)
                result['fan_stats']['charge_count'] = int(charge_data.get('charge_count') or 0)
                result['fan_stats']['charge_source'] = charge_data.get('source', 'unavailable')
            except Exception as exc:
                result['fan_stats']['charge_count'] = 0
                result['fan_stats']['charge_source'] = 'unavailable'
                result['data_errors']['charge_count'] = str(exc)
                logger.warning("[自诊] 充电人数获取失败(uid=%s): %s", uid, exc)

            # 舰长数：直播间基础信息(批量1个) + 大航海 topList，失败降级 None。
            result['fan_stats']['guard_count'] = None
            result['fan_stats']['guard_source'] = 'unavailable'
            result['fan_stats']['live_status'] = 0
            try:
                await self.rate_limiter.acquire('normal')
                room_info = await self.bili_api.get_room_base_info([uid])
                room_map = (room_info.get('data') or {}) if isinstance(room_info, dict) else {}
                room = room_map.get(str(uid)) or {}
                room_id = int(room.get('room_id') or 0)
                result['fan_stats']['live_status'] = int(room.get('live_status') or 0)
                if room_id:
                    guard_data = await self.bili_api.get_guard_top_list(room_id, int(uid))
                    guard_num = int(((guard_data.get('data') or {}).get('info') or {}).get('num') or 0)
                    result['fan_stats']['guard_count'] = guard_num
                    result['fan_stats']['guard_source'] = 'guard_top_list'
                else:
                    result['fan_stats']['guard_source'] = 'no_room'
            except Exception as exc:
                result['fan_stats']['guard_count'] = None
                result['fan_stats']['guard_source'] = 'unavailable'
                result['data_errors']['guard_count'] = str(exc)
                logger.warning("[自诊] 舰长人数获取失败(uid=%s): %s", uid, exc)

            # 2. 投稿数据（全量视频列表）
            # 分页拉取全部视频，用于播放/评论/收藏统计
            logger.info("[自诊] 采集投稿数据...")
            videos = await self._fetch_all_videos(uid)
            result['tag_cloud'] = await self._enrich_video_stats_and_tags(videos)
            result['data_availability']['tag_cloud'] = bool(
                result['tag_cloud'].get('tagged_video_count')
            )
            result['video_stats'] = self._analyze_video_stats(videos)
            result['data_availability']['video_stats'] = True
            
            # 3. 投稿节奏分析
            # 基于视频发布时间计算频率/断更间隔
            # 对输入做运算得到结果
            result['post_rhythm'] = self._analyze_post_rhythm(videos)
            result['data_availability']['post_rhythm'] = True
            
            # 4. 互动率估算
            # 需要粉丝数作为分母，因此必须在前面的步骤完成后执行
            result['engagement_metrics'] = self._calculate_engagement(
                videos, 
                result['fan_stats'].get('follower') or 0
            )
            result['data_availability']['engagement_metrics'] = True
            
            # 5. 尝试从三方数据源补充数据
            logger.info("[自诊] 尝试从三方数据源补充...")
            # 上下文管理：确保资源自动释放
            async with self.data_fetcher as fetcher:
                third_party_data = await fetcher.fetch_from_zeroroku(uid)
                
                # 判断 third_party_data
                # 根据条件走向不同处理分支
                if third_party_data:
                    result['third_party_data'] = third_party_data
                    result['data_availability']['fans_growth_curve'] = True
                    logger.info("[自诊] 三方数据获取成功")
                else:
                    result['data_availability']['fans_growth_curve'] = False
                    logger.warning("[自诊] 三方数据获取失败")
            
            # 6. 标记不可得数据
            # 以下维度仅创作中心可见，公开 API 无法获取
            # 明确置 False 而不是缺键，让前端/报告能感知"不可用"
            result['data_availability']['completion_rate'] = False  # 完播率（仅创作中心）
            result['data_availability']['audience_profile'] = False  # 观众画像（仅创作中心）
            result['data_availability']['traffic_source'] = False  # 流量来源（仅创作中心）
            
            logger.info(f"===== 账号{uid}数据采集完成 =====")
            return result
            
        except Exception as e:
            logger.error(f"[自诊] 数据采集失败: {e}")
            # 抛出异常中断流程
            raise
    
    async def _fetch_all_videos(self, uid: int) -> List[Dict[str, Any]]:
        """拉取用户全部视频（分页获取）
        # 读取数据并赋值给当前作用域变量
        
        循环翻页直到拉完或达到上限。
        
        分页参数：
        - page: 页码从1开始递增
        - page_size: 每页50条（B站接口上限）
        
        结束条件（满足任一即停）：
        - 响应为空或缺少 data 字段
        - 当前页列表为空
        - 已采集数量 >= 接口返回总数
        # 将结果交回调用方
        - 页码超过100（防死循环兜底）
        
        异常处理：
        # 对数据进行加工/分发
        - BilibiliAPIError 记录日志后跳出循环，
          返回已采集的部分数据而非抛错中断
          # 将结果交回调用方
        
        Args:
            uid: B站UID
            
        Returns:
            视频列表
        """
        all_videos = []
        page = 1
        page_size = 50
        
        # 循环处理，满足条件后退出
        while True:
            # 异常保护：局部失败不影响主流程
            try:
                # 每页请求前限频
                # 防止高频请求触发B站风控
                await self.rate_limiter.acquire('normal')
                
                videos_data = await self.bili_api.get_user_videos(
                    uid, 
                    page=page, 
                    page_size=page_size
                )
                
                # 无数据结束翻页
                # 响应为空或缺少 data 字段说明没有更多视频
                if not videos_data or not videos_data.get('data'):
                    # 退出循环
                    break
                
                # 视频列表位于 data.list.vlist 三层嵌套结构
                vlist = videos_data['data'].get('list', {}).get('vlist', [])
                
                # 空值/异常保护：不满足条件时跳过
                if not vlist:
                    # 退出循环
                    break
                
                # 当前页视频追加到总列表
                all_videos.extend(vlist)
                logger.info(f"[自诊] 已采集{len(all_videos)}个视频...")
                
                # 检查是否还有更多
                # B站接口通过 page.count 返回视频总数
                # 将结果交回调用方
                page_info = videos_data['data'].get('page', {})
                # 读取字典/配置项
                total = page_info.get('count', 0)
                
                # 达到总数则停止
                # 已采集数量 >= 总数说明翻页完成
                if len(all_videos) >= total:
                    # 退出循环
                    break
                
                page += 1
                
                # 防止无限循环
                # 极端情况（接口异常）下最多翻100页，避免死循环
                if page > 100:
                    logger.warning("[自诊] 达到分页上限，停止采集")
                    # 退出循环
                    break
                
            except BilibiliAPIError as e:
                # 单页失败不中断整体，记录后跳出
                # 保留已采集的数据供后续分析使用
                logger.error(f"[自诊] 采集视频失败: {e}")
                # 退出循环
                break
        
        logger.info(f"[自诊] 共采集到{len(all_videos)}个视频")
        return all_videos

    async def _enrich_video_stats_and_tags(
        self,
        videos: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """用专用接口补齐收藏统计并聚合全部投稿标签。

        Args:
            videos: 空间投稿列表，函数会就地写入 ``favorite`` 与 ``tags``。

        Returns:
            词云频次、标签数及接口采集覆盖率。
        """
        tags: List[str] = []
        stats_video_count = 0
        tagged_video_count = 0
        stat_url = f"{self.bili_api.BASE_URL}/x/web-interface/view"
        tag_url = f"{self.bili_api.BASE_URL}/x/tag/archive/tags"

        for index, video in enumerate(videos, start=1):
            bvid = str(video.get('bvid') or '').strip()
            if not bvid:
                continue

            # 同一视频的两个只读接口共享一次限频窗口，并发数固定为2。
            await self.rate_limiter.acquire('normal')
            stat_result, tag_result = await asyncio.gather(
                self.bili_api.get(stat_url, params={'bvid': bvid}, need_sign=False),
                self.bili_api.get(tag_url, params={'bvid': bvid}, need_sign=False),
                return_exceptions=True,
            )

            # 空间投稿列表不含收藏数，view 接口的 stat.favorite 才是目标字段。
            if isinstance(stat_result, Exception):
                logger.warning(f"[自诊] 视频 {bvid} 收藏统计获取失败: {stat_result}")
                video['favorite'] = 0
            elif isinstance(stat_result, dict):
                stat_fields = stat_result.get('stat', {})
                video['favorite'] = int(stat_fields.get('favorite') or 0)
                stats_video_count += 1
            else:
                video['favorite'] = 0

            # 新版 view 接口不再返回 tag，标签必须取 archive/tags 的 tag_name。
            if isinstance(tag_result, Exception):
                logger.warning(f"[自诊] 视频 {bvid} tag获取失败: {tag_result}")
                video['tags'] = []
            else:
                video_tags = [
                    str(item.get('tag_name')).strip()
                    for item in tag_result
                    if isinstance(item, dict) and item.get('tag_name')
                ] if isinstance(tag_result, list) else []
                video['tags'] = video_tags
                if video_tags:
                    tagged_video_count += 1
                    tags.extend(video_tags)

            if index % 20 == 0 or index == len(videos):
                logger.info(f"[自诊] 投稿详情补齐进度: {index}/{len(videos)}")

        filtered_tags = [
            tag for tag in tags
            if len(tag) >= 2 and tag not in {'视频', '投稿', '原创'}
        ]
        frequency = dict(Counter(filtered_tags).most_common(60))
        return {
            'video_count': len(videos),
            'stats_video_count': stats_video_count,
            'tagged_video_count': tagged_video_count,
            'tag_count': len(tags),
            'word_frequency': frequency,
            'source_endpoint': '/x/tag/archive/tags',
        }
    
    def _analyze_video_stats(self, videos: List[Dict[str, Any]]) -> Dict[str, Any]:
        """分析视频统计数据
        
        汇总播放/评论/收藏，计算均值与最高播放视频。
        # 对输入做运算得到结果
        
        输出指标：
        - total_count: 视频总数
        - total_play/comment/favorite: 三指标总量
        - avg_play/comment/favorite: 三指标均值（取整）
        - max_play_video: 播放量最高的视频摘要
        
        边界处理：
        # 对数据进行加工/分发
        - videos 为空时返回全零结果，不抛异常
        # 将结果交回调用方
        - 单个视频字段缺失时用 .get() 缺省为 0
        
        Args:
            videos: 视频列表
            
        Returns:
            统计结果
        """
        if not videos:
            return {
                'total_count': 0,
                'total_play': 0,
                'total_comment': 0,
                'total_favorite': 0,
                'avg_play': 0,
                'avg_comment': 0,
                'max_play_video': None
            }
        
        # 汇总总量
        # 逐视频累加播放/评论/收藏，v.get 缺省为 0 防报错
        total_play = sum(v.get('play', 0) for v in videos)
        total_comment = sum(v.get('comment', 0) for v in videos)
        total_favorite = sum(v.get('favorite', 0) for v in videos)
        
        # 找到播放量最高的视频
        # 用于展示"代表作"；key 指定按 play 字段比较
        # 将内容呈现到界面上
        max_play_video = max(videos, key=lambda x: x.get('play', 0))
        
        # 组装统计结果
        # 均值取整，避免报告出现小数
        stats = {
            'total_count': len(videos),
            'total_play': total_play,
            'total_comment': total_comment,
            'total_favorite': total_favorite,
            'avg_play': int(total_play / len(videos)),
            'avg_comment': int(total_comment / len(videos)),
            'avg_favorite': int(total_favorite / len(videos)),
            # 最高播放视频的摘要信息（标题/BV号/播放量）
            'max_play_video': {
                'title': max_play_video.get('title'),
                'bvid': max_play_video.get('bvid'),
                'play': max_play_video.get('play')
            }
        }
        
        logger.info(f"[统计] 总视频数: {stats['total_count']}, "
                   f"平均播放: {stats['avg_play']:,}")
        
        return stats
    
    def _analyze_post_rhythm(self, videos: List[Dict[str, Any]]) -> Dict[str, Any]:
        """分析投稿节奏
        
        计算投稿频率、最长断更间隔、近30天投稿数。
        # 对输入做运算得到结果
        
        输出指标：
        - videos_per_week: 平均每周投稿数（活跃期口径）
        - videos_per_month: 平均每月投稿数
        - longest_gap_days: 最长断更天数（相邻视频间隔最大值）
        - recent_30d_count: 近30天投稿数
        - total_days_active: 活跃总天数（首个到最近投稿）
        
        注意：
        - 时间戳为 Unix 秒，需要除以86400换算为天
        - 少于2个视频时无法计算节奏，返回全零
        # 将结果交回调用方
        
        Args:
            videos: 视频列表
            
        Returns:
            节奏分析结果
        """
        if not videos or len(videos) < 2:
            return {
                'videos_per_week': 0,
                'videos_per_month': 0,
                'longest_gap_days': 0,
                'recent_30d_count': 0
            }
        
        # 按创建时间排序
        # 时间戳升序排列，后续计算断更间隔需要相邻顺序
        # 对输入做运算得到结果
        sorted_videos = sorted(videos, key=lambda x: x.get('created', 0))
        
        # 计算时间跨度
        # 最早投稿与最近投稿之间的天数，作为活跃周期
        first_video_time = sorted_videos[0].get('created', 0)
        # 读取字典/配置项
        last_video_time = sorted_videos[-1].get('created', 0)
        # 计算结果存入 time_span_days
        # 对输入做运算得到结果
        time_span_days = (last_video_time - first_video_time) / 86400
        
        # 投稿频率
        # 总投稿数 / 活跃周数（或月数）
        videos_per_week = 0
        videos_per_month = 0
        
        # 边界/有效性检查
        if time_span_days > 0:
            videos_per_week = round(len(videos) / (time_span_days / 7), 2)
            videos_per_month = round(len(videos) / (time_span_days / 30), 2)
        
        # 最长断更间隔
        # 遍历相邻视频的时间差
        # 相邻两视频发布时间间隔的最大值即为最长断更
        max_gap = 0
        # 循环遍历处理
        # 对集合内每个元素执行相同处理
        for i in range(1, len(sorted_videos)):
            # 计算结果存入 gap
            # 对输入做运算得到结果
            gap = (sorted_videos[i]['created'] - sorted_videos[i-1]['created']) / 86400
            max_gap = max(max_gap, gap)
        
        # 最近30天投稿数
        # 统计活跃度：最近一个月是否保持更新
        now = datetime.now().timestamp()
        recent_30d_count = sum(1 for v in videos if (now - v.get('created', 0)) <= 30 * 86400)
        
        # 组装节奏分析结果
        # 断更天数与活跃天数取整，避免小数
        rhythm = {
            'videos_per_week': videos_per_week,
            'videos_per_month': videos_per_month,
            'longest_gap_days': int(max_gap),
            'recent_30d_count': recent_30d_count,
            'total_days_active': int(time_span_days)
        }
        
        logger.info(f"[节奏] 投稿频率: {videos_per_week}视频/周, "
                   f"最长断更: {rhythm['longest_gap_days']}天")
        
        return rhythm
    
    def _calculate_engagement(self, videos: List[Dict[str, Any]], fans: int) -> Dict[str, Any]:
        """计算互动率指标
        # 对输入做运算得到结果
        
        三种比率：粉丝触达率/评论率/收藏率。
        
        指标定义：
        - play_to_fans_ratio: 平均播放/粉丝数*100
          反映视频对粉丝的触达程度，>100% 说明有站外流量
        - comment_to_play_ratio: 平均评论/平均播放*100
          评论率反映观众讨论意愿
        - favorite_to_play_ratio: 平均收藏/平均播放*100
          收藏率反映内容实用价值
        
        边界处理：
        # 对数据进行加工/分发
        - fans 为 0 时触达率返回 0（无法计算）
        # 将结果交回调用方
        - avg_play 为 0 时评论/收藏率返回 0（避免除零）
        # 将结果交回调用方
        
        Args:
            videos: 视频列表
            fans: 粉丝数
            
        Returns:
            互动率指标
        """
        if not videos:
            return {
                'play_to_fans_ratio': 0,
                'comment_to_play_ratio': 0,
                'favorite_to_play_ratio': 0
            }
        
        # 计算均值
        # 三个维度的平均播放/评论/收藏，作为互动率分子
        avg_play = sum(v.get('play', 0) for v in videos) / len(videos)
        # 计算结果存入 avg_comment
        # 对输入做运算得到结果
        avg_comment = sum(v.get('comment', 0) for v in videos) / len(videos)
        # 计算结果存入 avg_favorite
        # 对输入做运算得到结果
        avg_favorite = sum(v.get('favorite', 0) for v in videos) / len(videos)
        
        # 播放/粉丝比（粗略估算粉丝触达率）
        # 平均播放量占粉丝数的比例，>100% 说明有外部流量
        play_to_fans_ratio = 0
        # 边界/有效性检查
        if fans > 0:
            play_to_fans_ratio = round((avg_play / fans) * 100, 2)
        
        # 评论/播放比
        # 评论率反映观众讨论意愿，越高越好
        comment_to_play_ratio = 0
        # 边界/有效性检查
        if avg_play > 0:
            comment_to_play_ratio = round((avg_comment / avg_play) * 100, 4)
        
        # 收藏/播放比
        # 收藏率反映内容实用价值，是优质内容的标志
        favorite_to_play_ratio = 0
        # 边界/有效性检查
        if avg_play > 0:
            favorite_to_play_ratio = round((avg_favorite / avg_play) * 100, 4)
        
        # 组装互动率指标
        # 三个比率全部用百分比表示，报告直接展示
        # 将内容呈现到界面上
        metrics = {
            'play_to_fans_ratio': play_to_fans_ratio,  # 粉丝触达率（%）
            'comment_to_play_ratio': comment_to_play_ratio,  # 评论率（%）
            'favorite_to_play_ratio': favorite_to_play_ratio  # 收藏率（%）
        }
        
        logger.info(f"[互动] 粉丝触达率: {play_to_fans_ratio}%, "
                   f"评论率: {comment_to_play_ratio}%")
        
        return metrics
    
    def benchmark_with_category(self, self_data: Dict[str, Any], category: str) -> Dict[str, Any]:
        """与分区数据进行benchmark对比
        
        使用 Hotspot 表的 heat_score 作为分区参照，
        计算分位数并判定自身位置。
        # 对输入做运算得到结果
        
        参照口径：
        - 数据源：Hotspot 表（热点发现模块积累）
        - 参照量：heat_score（热度分），替代播放量
        - 样本数：最多取100条同分区记录
        
        输出：
        - has_benchmark: 是否有参照数据
        - category_metrics: 分区均值 + p25/p50/p75 分位
        - self_metrics: 自己的平均播放
        - comparison: 位置判定（顶部25%/中上/中下/底部）
        
        注意：
        - 无参照数据时返回 has_benchmark=False，
        # 将结果交回调用方
          提示先跑热点发现模块积累数据
        
        Args:
            self_data: 自己的账号数据
            category: 分区名称
            
        Returns:
            对比结果
        """
        logger.info(f"[benchmark] 与{category}分区数据对比")
        
        # 从数据库读取同分区的热点数据（作为参照）
        with get_session() as session:
            # 查询同分区的热点视频数据
            # 最多取100条，避免查询过重
            hotspots = session.query(Hotspot).filter(
                Hotspot.category == category
            ).limit(100).all()
            
            # 无参照数据时提示先用热点发现积累
            # benchmark 依赖参照样本，样本为空无法比较
            if not hotspots:
                logger.warning(f"[benchmark] 分区{category}暂无参照数据")
                return {
                    'has_benchmark': False,
                    'message': f'分区{category}暂无参照数据，请先使用热点发现模块积累数据'
                }
            
            # 计算分区均值（使用 heat_score 替代 view_count）
            # Hotspot 表存的是热度分而非播放量，直接用作分区均值参照
            category_avg_play = sum(h.heat_score or 0 for h in hotspots) / len(hotspots)
            
            # 简单对比
            # 自己的平均播放量，来自 video_stats 分析结果
            self_avg_play = self_data.get('video_stats', {}).get('avg_play', 0)
            
            # 计算分位（简化版）
            # 参照样本按热度排序，取 p25/p50/p75 三个分位点
            all_plays = sorted([h.heat_score or 0 for h in hotspots])
            percentile_25 = all_plays[int(len(all_plays) * 0.25)] if all_plays else 0
            percentile_50 = all_plays[int(len(all_plays) * 0.50)] if all_plays else 0
            percentile_75 = all_plays[int(len(all_plays) * 0.75)] if all_plays else 0
            
            # 判断自己的位置
            # 与三个分位点比较，得出在分区中的大致档位
            position = "底部25%"
            # 边界/有效性检查
            if self_avg_play >= percentile_75:
                position = "顶部25%"
            # 边界/有效性检查
            elif self_avg_play >= percentile_50:
                position = "中上50-75%"
            # 边界/有效性检查
            elif self_avg_play >= percentile_25:
                position = "中下25-50%"
            
            # 组装对比结果
            # 分区均值与分位点用于横向比较，
            # vs_avg 表示自身播放占分区均值的百分比
            result = {
                'has_benchmark': True,
                'category': category,
                'sample_size': len(hotspots),
                'category_metrics': {
                    'avg_play': int(category_avg_play),
                    'p25': int(percentile_25),
                    'p50': int(percentile_50),
                    'p75': int(percentile_75)
                },
                'self_metrics': {
                    'avg_play': self_avg_play
                },
                'comparison': {
                    'position': position,
                    'vs_avg': round((self_avg_play / category_avg_play * 100) if category_avg_play > 0 else 0, 2)
                }
            }
            
            logger.info(f"[benchmark] 你的播放量处于分区{position}位置")
            return result
