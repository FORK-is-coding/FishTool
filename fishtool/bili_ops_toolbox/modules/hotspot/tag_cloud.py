"""
分区热门Tag词云生成器
从B站分区热门榜爬取视频tag，生成词云图

本模块实现"分区热点标签提取"：

一、分区映射（ZONE_MAP）
- 内置 29 个 B 站新一级分区（pid_v2 体系）的中文名 -> ID 映射
- 支持动画/音乐/游戏/知识/科技/生活/美食等
- 番剧/国创已移除（ranking/v2 不支持 UP 主投稿外的分区）

二、采集流程
1. get_zone_ranking: 分页拉取分区热门榜
   （/x/web-interface/ranking/v2，每页50条）
2. extract_tags_from_videos: 逐个视频请求详情
   提取 tag 字段与分区名（tname）
   # 从数据中取出目标字段，供后续逻辑使用
3. generate_word_frequency: 词频统计
   - 过滤短tag（<2字）与通用词（视频/投稿/原创）
   # 剔除不符合条件的数据
   - Counter 统计，返回 TopN
   # 将结果交回调用方

三、数据产出（generate_cloud_data）
- 词云数据字典：video_count/tag_count/word_frequency
- 同步写入 Hotspot 表（Top20，heat_score=频次）
- 供前端词云渲染使用

注意：
- 榜单 API 本身不带 tag，需要逐个视频请求详情，
  请求量较大，务必配好限频器
- 未知分区名抛 ValueError，用 get_supported_zones
  查询支持列表

依赖：
- bilibili.api / bilibili.rate_limiter
- core.database: Hotspot 模型
"""
import asyncio
# 从 typing 导入符号
from typing import List, Dict, Any, Optional, Callable
# 从 collections 导入符号
from collections import Counter
# 从 datetime 导入符号
from datetime import datetime, timedelta
# 从 pathlib 导入符号
from pathlib import Path
# 导入模块
import logging

# 从 bilibili.api 导入符号
from bilibili.api import BilibiliAPI
# 从 bilibili.rate_limiter 导入符号
from bilibili.rate_limiter import RateLimiter
# 从 core.exceptions 导入符号
from core.exceptions import BilibiliAPIError, ValidationError
# 从 core.logger 导入符号
from core.logger import get_logger
# 从 core.database 导入符号
from core.database import get_session, Hotspot

logger = get_logger(__name__)


class TagCloudGenerator:
    """分区热门Tag词云生成器
    
    拉取分区热门榜视频的 tag，
    统计词频并输出词云数据。
    """
    
    # B站一级分区 ID 映射（pid_v2 体系）。
    # 产品契约固定暴露 28 个公开可采集分区；绘画由方案 C 单独暴露，
    # 未公开的神秘学与不在当前产品契约内的资讯不进入该映射。
    ZONE_MAP = {
        '动画': 1005,
        '音乐': 1003,
        '舞蹈': 1004,
        '游戏': 1008,
        '知识': 1010,
        '科技': 1012,
        '体育': 1018,
        '汽车': 1013,
        '美食': 1020,
        '动物圈': 1024,
        '鬼畜': 1007,
        '时尚': 1014,
        '娱乐': 1002,
        '影视': 1001,
        '生活': 1031,
        'AI': 1011,
        '家居': 1015,
        '户外': 1016,
        '健身': 1017,
        '手工': 1019,
        '短剧': 1021,
        '旅游': 1022,
        '三农': 1023,
        '亲子': 1025,
        '健康': 1026,
        '情感': 1027,
        'vlog': 1029,
        '潮玩': 1030,
    }
    
    PAINT_TID = 27
    PAINT_WINDOW_DAYS = 7
    # 二次过滤关键词：标题/简介/标签命中任一即视为绘画内容。
    PAINT_KEYWORDS = (
        '绘画', '画画', '画师', '插画', '原画', '板绘', '手绘', '水彩',
        '速写', '素描', '厚涂', '线稿', '上色', '绘画过程', '绘画教程',
    )
    # 搜索主采关键词：单关键词分跑（B 站多词空格是 AND 语义，合搜会把结果集
    # 压缩到极小，order=click 也只能在低播放集合里排；单词跑能拿到百万级热门）。
    # 保持精简：关键词越多搜索请求越密，越容易触发 412 风控；3 个高频词够覆盖。
    PAINT_SEARCH_KEYWORDS = (
        '板绘', '绘画过程', '画师',
    )
    PAINT_ACCOUNTS = (1105739740, 2072860945)
    # 播放量下限默认值：仅对绘画搜索主采生效，低于该值的视为无热点参考价值。
    PAINT_MIN_VIEW_DEFAULT = 50000
    # B 路动态跟踪账号池上限：搜索热门结果里播放达标的 up 会自动进池补漏。
    MAX_TRACKED_ACCOUNTS = 15
    # 动态账号池（类级），初始为固定官号，采集过程中自动扩展。
    _tracked_paint_uids: set[int] = set(PAINT_ACCOUNTS)

    def __init__(self, api: BilibiliAPI, rate_limiter: Optional[RateLimiter] = None):
        """初始化Tag词云生成器
        # 设置初始值/默认状态，避免后续空引用
        # 写入配置/属性，影响后续行为
        
        Args:
            api: B站API实例
            rate_limiter: 限频器实例（可选，不传则使用API自带的）
        """
        self.api = api
        # 确保 rate_limiter 永远不为 None，自动创建兜底实例
        self.rate_limiter = rate_limiter or api.rate_limiter or RateLimiter()
        
    @classmethod
    def _is_paint_video(cls, video: Dict[str, Any]) -> bool:
        """按标题、简介与搜索标签执行绘画关键词二次过滤。"""
        text = " ".join(
            str(video.get(key) or "")
            for key in ('title', 'description', 'desc', 'tag', 'tname')
        ).lower()
        return any(keyword.lower() in text for keyword in cls.PAINT_KEYWORDS)

    @staticmethod
    def _published_timestamp(video: Dict[str, Any]) -> int:
        """提取视频发布时间戳，无法识别时返回 0。

        Args:
            video: 搜索或投稿列表中的视频字典。

        Returns:
            Unix 秒级时间戳。
        """
        value = video.get("created") or video.get("pubdate")
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value or "").strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            pass
        try:
            normalized = text.replace("Z", "+00:00")
            return int(datetime.fromisoformat(normalized).timestamp())
        except (TypeError, ValueError, OverflowError):
            return 0

    @classmethod
    def _is_recent_paint_video(cls, video: Dict[str, Any], cutoff: int) -> bool:
        """判断绘画视频是否存在且不早于 7 天时间窗。"""
        published = cls._published_timestamp(video)
        return published >= cutoff and cls._is_paint_video(video)

    async def _search_paint_videos(self, limit: int, min_view: int = 0) -> List[Dict[str, Any]]:
        """A 路：单关键词分跑 + 播放量排序搜索投稿，合并去重。

        策略说明：
        - B 站搜索多关键词空格是 AND 语义，'绘画 插画 板绘 手绘' 这类合搜会把
          结果集压到极小（实测仅 10 条且 max 播放 2.7 万）；改为每个关键词单独
          拉一页 order=click，实测播放量可到百万级。
        - 搜索接口同时传递 7 天时间窗；本地再硬过滤，防止接口忽略参数。
        - 播放量排序只影响结果优先级，不替代时间窗过滤。
        - 播放量达标的 up mid 自动进入动态跟踪池，供 B 路补漏。
        """
        videos: List[Dict[str, Any]] = []
        seen: set[str] = set()
        consecutive_failures = 0
        threshold = max(min_view, 0)
        cutoff = int((datetime.now() - timedelta(days=self.PAINT_WINDOW_DAYS)).timestamp())
        begin = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        for keyword in self.PAINT_SEARCH_KEYWORDS:
            await self.rate_limiter.acquire()
            try:
                data = await self.api.get(
                    "https://api.bilibili.com/x/web-interface/search/type",
                    params={
                        'search_type': 'video',
                        'keyword': keyword,
                        'order': 'click',
                        'tids': self.PAINT_TID,
                        'page': 1,
                        'page_size': 50,
                        'pubtime_begin': begin,
                        'pubtime_end': end,
                    },
                    need_sign=False,
                )
            except Exception as exc:
                # 搜索接口是风控重灾区（412/空 result 常见）。单关键词失败不中断整轮，
                # 降级走 B 路官号补漏，避免一次 412 把整个绘画采集打挂。
                # 连续失败说明 IP 已被搜索风控标记，继续试后面的关键词只会加重，
                # 直接放弃剩余关键词。
                consecutive_failures += 1
                logger.warning("绘画搜索关键词 '%s' 失败，降级跳过: %s", keyword, exc)
                if consecutive_failures >= 2:
                    logger.warning("绘画搜索连续失败 %s 次，停止搜索降级 B 路", consecutive_failures)
                    break
                continue
            consecutive_failures = 0
            batch = list((data or {}).get('result') or [])
            for item in batch:
                bvid = str(item.get('bvid') or "")
                if not bvid or bvid in seen:
                    continue
                if not self._is_recent_paint_video(item, cutoff):
                    continue
                play = int(item.get('play') or 0)
                if play < threshold:
                    continue
                seen.add(bvid)
                videos.append(item)
                mid = int(item.get('mid') or 0)
                if mid and play >= max(threshold, self.PAINT_MIN_VIEW_DEFAULT):
                    TagCloudGenerator._tracked_paint_uids.add(mid)
            if len(videos) >= limit:
                break
        return sorted(videos, key=lambda item: int(item.get('play') or 0), reverse=True)[:limit]

    async def _fetch_paint_account_videos(self, limit: int, min_view: int = 0) -> List[Dict[str, Any]]:
        """B 路：跟踪绘画区头部画师/官方账号近 7 天投稿补漏。

        账号池 = 固定官号 + 搜索主采自动挖掘的头部 up（上限 MAX_TRACKED_ACCOUNTS）。
        头部账号的新作即使播放未达下限也是趋势信号，故不套 min_view 硬过滤，
        仅保留关键词过滤与时间窗。
        """
        cutoff = int((datetime.now() - timedelta(days=self.PAINT_WINDOW_DAYS)).timestamp())
        videos: List[Dict[str, Any]] = []
        # 固定官号恒排最前（画师同人站/官方小课堂优先补漏），动态挖掘的头部 up 排后。
        # 搜索类接口（arc/search）IP 级 412 风控严格，B 路账号数压到最少：
        # 2 个固定官号 + 最多 2 个动态账号，降低单轮请求密度。
        fixed = list(self.PAINT_ACCOUNTS)
        dynamic = sorted(TagCloudGenerator._tracked_paint_uids - set(self.PAINT_ACCOUNTS))
        uids = (fixed + dynamic)[:4]
        per_account = min(30, max(5, limit))
        for uid in uids:
            try:
                await self.rate_limiter.acquire()
                payload = await self.api.get_user_videos(uid, page=1, page_size=per_account)
                vlist = (((payload or {}).get('data') or {}).get('list') or {}).get('vlist') or []
                for video in vlist:
                    created = int(video.get('created') or video.get('pubdate') or 0)
                    if self._is_recent_paint_video(video, cutoff):
                        videos.append(video)
            except Exception as exc:
                logger.warning("绘画账号投稿补漏失败(uid=%s): %s", uid, exc)
        return videos

    async def get_paint_videos(self, limit: int = 100, min_view: int = 0) -> List[Dict[str, Any]]:
        """执行绘画方案 C，合并搜索主采与重点账号投稿补漏结果。

        Args:
            limit: 目标视频数上限（1-200）。
            min_view: 搜索主采播放量下限；低于该值的视频不作为热点参考。
        """
        limit = max(1, min(int(limit), 200))
        # B 路官号补漏优先执行：官号投稿稳定且多为 7 天内新作，作为主采来源；
        # A 路搜索（风控易 412）作为补充。合并时 B 路结果排前面。
        supplement = await self._fetch_paint_account_videos(limit)
        primary = await self._search_paint_videos(limit, min_view=min_view)
        cutoff = int((datetime.now() - timedelta(days=self.PAINT_WINDOW_DAYS)).timestamp())
        merged: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for video in [*supplement, *primary]:
            bvid = str(video.get('bvid') or "")
            if not bvid or bvid in seen or not self._is_recent_paint_video(video, cutoff):
                continue
            seen.add(bvid)
            merged.append(video)
            if len(merged) >= limit:
                break
        logger.info("绘画方案C采集完成: A路=%s, B路=%s, 去重后=%s", len(primary), len(supplement), len(merged))
        return sorted(merged, key=lambda item: int(item.get('play') or 0), reverse=True)

    async def _fetch_newlist(self, zone_id: int, limit: int) -> List[Dict[str, Any]]:
        """通过公开最新投稿接口获取分区视频。

        Args:
            zone_id: B站主分区 ID。
            limit: 最多返回的视频数。

        Returns:
            标准视频字典列表。
        """
        page_size = min(max(limit, 1), 50)
        url = "https://api.bilibili.com/x/web-interface/newlist"
        data = await self.api.get(
            url,
            params={"rid": zone_id, "pn": 1, "ps": page_size},
            need_sign=False,
        )
        return list((data or {}).get("archives") or [])[:limit]

    async def get_zone_ranking(self, zone_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        """获取分区热门视频，并在榜单接口受限时自动降级。

        Args:
            zone_id: B站主分区 ID。
            limit: 最多获取的视频数量。

        Returns:
            视频字典列表。

        Raises:
            BilibiliAPIError: 主接口与降级接口均不可用。
        """
        logger.info(f"开始获取分区 {zone_id} 的视频，目标 {limit} 个")

        # 番剧、国创不受 ranking/v2 支持，直接使用公开 newlist。
        if zone_id in {13, 167}:
            try:
                await self.rate_limiter.acquire()
                videos = await self._fetch_newlist(zone_id, limit)
                if not videos:
                    raise BilibiliAPIError("newlist 返回空 archives")
                logger.info(f"分区 {zone_id} newlist 获取完成，共 {len(videos)} 个")
                return videos
            except Exception as exc:
                raise BilibiliAPIError(
                    f"分区 {zone_id} 的 newlist 接口失败: {exc}"
                ) from exc

        ranking_error: Optional[Exception] = None
        try:
            await self.rate_limiter.acquire()
            data = await self.api.get(
                "https://api.bilibili.com/x/web-interface/ranking/v2",
                params={"rid": zone_id, "type": "all"},
                need_sign=False,
            )
            videos = list((data or {}).get("list") or [])[:limit]
            if videos:
                logger.info(f"分区 {zone_id} ranking/v2 获取完成，共 {len(videos)} 个")
                return videos
            ranking_error = BilibiliAPIError("ranking/v2 返回空 list")
        except Exception as exc:
            ranking_error = exc
            logger.warning(f"分区 {zone_id} ranking/v2 不可用，降级 newlist: {exc}")

        try:
            await self.rate_limiter.acquire()
            videos = await self._fetch_newlist(zone_id, limit)
            if not videos:
                raise BilibiliAPIError("newlist 返回空 archives")
            logger.info(f"分区 {zone_id} 已降级 newlist，共 {len(videos)} 个")
            return videos
        except Exception as fallback_error:
            raise BilibiliAPIError(
                f"分区 {zone_id} 视频接口均失败；ranking/v2: {ranking_error}；"
                f"newlist: {fallback_error}"
            ) from fallback_error

    async def extract_tags_from_videos(
        self,
        videos: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[str]:
        """从视频列表中提取所有tag
        # 从数据中取出目标字段，供后续逻辑使用
        
        逐个请求视频 tag 接口（/x/tag/archive/tags），
        提取 tag_name 字段与分区名 tname。
        # 从数据中取出目标字段，供后续逻辑使用
        
        注意：
        - 新版 view 接口（/x/web-interface/view）返回中
          **没有 tag 字段**，tname 也常为空串，
          tag 必须走专用接口 /x/tag/archive/tags
        - 榜单接口（/x/web-interface/ranking/v2）同样不带 tag，
          所以此处逐个视频请求是必要的
        
        Args:
            videos: 视频列表
            
        Returns:
            tag列表
        """
        all_tags = []
        
        # 遍历 videos 逐项处理
        # 对集合内每个元素执行相同处理
        for index, video in enumerate(videos, 1):
            # 从榜单数据中可能没有tag，需要单独请求视频详情
            bvid = video.get('bvid')
            # 空值/异常保护：不满足条件时跳过
            if not bvid:
                # 跳过本轮继续循环
                continue
            
            # 异常保护：局部失败不影响主流程
            try:
                # 异步等待结果
                await self.rate_limiter.acquire()
                
                # 获取视频 tag 列表（专用接口，返回 list）
                # 新版 view 接口已不含 tag 字段，必须走这里
                tag_url = "https://api.bilibili.com/x/tag/archive/tags"
                params = {'bvid': bvid}
                
                tag_list = await self.api.get(tag_url, params=params, need_sign=False)
                
                # api.get() 返回的是 list（该接口 data 就是 tag 数组）
                # 将结果交回调用方
                if isinstance(tag_list, list):
                    # 遍历每个tag对象
                    # 对集合内每个元素执行相同处理
                    for tag_obj in tag_list:
                        # 提取tag名
                        tag_name = tag_obj.get('tag_name', '') if isinstance(tag_obj, dict) else ''
                        # 边界/有效性检查
                        if tag_name:
                            # 追加到列表
                            all_tags.append(tag_name)
                
                # 补充分区名（tname 非空时加入，增加一个来源）
                # 从数据中取出目标字段，供后续逻辑使用
                tname = video.get('tname') or ''
                # 边界/有效性检查
                if tname:
                    # 追加到列表
                    all_tags.append(tname)
                    
            except BilibiliAPIError as e:
                # 单个视频失败不阻断整批采集，进度仍按已处理数量推进。
                logger.warning(f"获取视频 {bvid} tag失败: {e}")
            finally:
                if progress_callback:
                    progress_callback(index, len(videos))
        
        logger.info(f"从 {len(videos)} 个视频中提取到 {len(all_tags)} 个tag")
        return all_tags
    
    def generate_word_frequency(self, tags: List[str], top_n: int = 50) -> Dict[str, int]:
        """生成词频统计
        
        过滤无意义词后统计频次，返回 TopN。
        # 剔除不符合条件的数据
        
        Args:
            tags: tag列表
            top_n: 返回前N个高频词
            # 将结果交回调用方
            
        Returns:
            {tag: 频次} 字典
        """
        # 过滤短tag和无意义tag
        # 剔除不符合条件的数据
        filtered_tags = [
            tag for tag in tags 
            # 分支判断
            if len(tag) >= 2 and tag not in ['视频', '投稿', '原创']
        ]
        
        # 统计词频
        counter = Counter(filtered_tags)
        # 类型转换后存入 top_tags
        # 将数据从一种形态映射为另一种
        top_tags = dict(counter.most_common(top_n))
        
        logger.info(f"词频统计完成，Top {len(top_tags)}: {list(top_tags.keys())[:10]}")
        return top_tags
    
    async def generate_cloud_data(
        self,
        zone_name: str,
        limit: int = 100,
        top_n: int = 50,
        progress_callback: Optional[Callable[[str, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """生成词云数据（用于前端渲染）
        
        完整流程：拉榜 -> 提tag -> 词频统计 -> 入库。
        
        Args:
            zone_name: 分区名称
            limit: 爬取视频数量
            top_n: 返回前N个高频词
            # 将结果交回调用方
            
        Returns:
            词云数据字典
        """
        # 获取分区ID；绘画虽已有新 pid_v2=1006 但榜单返回老数据，继续使用专用方案 C，不写入 ZONE_MAP。
        is_paint = zone_name == '绘画'
        zone_id = self.PAINT_TID if is_paint else self.ZONE_MAP.get(zone_name)
        # 边界/有效性检查
        if zone_id is None:
            # 抛出异常中断流程
            raise ValidationError("zone_name", f"未知的分区名称: {zone_name}，支持的分区: {list(self.ZONE_MAP.keys())}")
        
        logger.info(f"开始生成分区【{zone_name}】的Tag词云")
        if progress_callback:
            progress_callback("ranking", 8, "正在获取分区热门视频")
        
        # 1. 获取热门视频；绘画使用搜索主采 + 重点账号投稿补漏。
        videos = await self.get_paint_videos(limit) if is_paint else await self.get_zone_ranking(zone_id, limit)
        
        # 两路接口都返回空时才视为采集失败；错误中保留真实接口诊断。
        if not videos:
            message = f"分区【{zone_name}】视频接口返回空数据"
            logger.error(message)
            raise BilibiliAPIError(message)
        
        # 2. 提取tag；百分比由真实完成的视频数计算。
        def report_video_progress(done: int, total: int) -> None:
            """将标签采集进度映射到 15%-85% 区间并回传。

            15% 是进入标签采集阶段时的初始进度，剩余 70% 按
            已完成视频数占总数的比例推进，最后 15% 留给统计/保存。
            """
            if progress_callback and total:
                percent = 15 + int(done / total * 70)
                progress_callback("collecting_tags", percent, f"正在采集视频标签 {done}/{total}")

        tags = await self.extract_tags_from_videos(videos, report_video_progress)
        if progress_callback:
            progress_callback("statistics", 90, "正在统计热门标签")
        
        # 3. 生成词频
        word_frequency = self.generate_word_frequency(tags, top_n)
        
        # 4. 保存到数据库
        if progress_callback:
            progress_callback("saving", 96, "正在保存词云结果")
        await self._save_to_database(zone_name, zone_id, word_frequency)
        
        # 组装词云数据
        result = {
            'zone_name': zone_name,
            'zone_id': zone_id,
            'video_count': len(videos),
            'tag_count': len(tags),
            'word_frequency': word_frequency,
            'generated_at': datetime.now().isoformat()
        }
        
        logger.info(f"分区【{zone_name}】词云数据生成完成")
        return result
    
    async def _save_to_database(self, zone_name: str, zone_id: int, word_frequency: Dict[str, int]):
        """保存热点数据到数据库
        # 持久化数据，防止丢失
        
        Args:
            zone_name: 分区名称
            zone_id: 分区ID
            word_frequency: 词频统计
        """
        try:
            # 赋值并准备后续使用
            session = get_session()
            
            # 保存Top热点tag
            # 只保存Top20，避免数据膨胀
            for tag, frequency in list(word_frequency.items())[:20]:
                # 赋值并准备后续使用
                hotspot = Hotspot(
                    source='tag_cloud',
                    category=zone_name,
                    title=tag,
                    content=f"{zone_name}分区热门标签",
                    tags=[tag],
                    keywords=[tag],
                    heat_score=float(frequency),
                    trend='hot'
                )
                # 加入集合/数据库会话
                session.add(hotspot)
            
            # 提交事务
            session.commit()
            logger.info(f"热点数据已保存到数据库")
            
        except Exception as e:
            logger.error(f"保存热点数据失败: {e}")
            # 回滚事务
            session.rollback()
        # 异常处理
        finally:
            # 关闭连接释放资源
            # 释放连接/窗口资源
            session.close()
    
    @staticmethod
    def is_collectable_zone(zone_name: str) -> bool:
        """判断分区是否可采集；绘画通过专用方案 C 支持但不进入一级分区列表。"""
        return zone_name == '绘画' or zone_name in TagCloudGenerator.ZONE_MAP

    @staticmethod
    def get_zone_options() -> List[Dict[str, Any]]:
        """返回全站一级分区（新 pid_v2 体系）的名称与热门榜 TID。"""
        return [
            {'name': name, 'tid': tid}
            for name, tid in TagCloudGenerator.ZONE_MAP.items()
        ]

    @staticmethod
    def get_supported_zones() -> List[str]:
        """获取全站一级分区名称（新 pid_v2 体系）。"""
        return list(TagCloudGenerator.ZONE_MAP.keys())


# ============ 使用示例 ============

async def demo_generate_tag_cloud():
    """演示：生成分区Tag词云"""
    from bilibili.cookie_pool import get_cookie_pool
    
    # 初始化
    cookie_pool = get_cookie_pool()
    # 赋值并准备后续使用
    api = BilibiliAPI(cookie_pool=cookie_pool)
    
    generator = TagCloudGenerator(api)
    
    # 生成游戏区词云
    cloud_data = await generator.generate_cloud_data('游戏', limit=100, top_n=50)
    
    print(f"分区: {cloud_data['zone_name']}")
    # 输出信息到控制台
    print(f"视频数: {cloud_data['video_count']}")
    # 输出信息到控制台
    print(f"Tag数: {cloud_data['tag_count']}")
    # 输出信息到控制台
    print(f"Top 10 热门Tag:")
    # 循环遍历处理
    # 对集合内每个元素执行相同处理
    for i, (tag, freq) in enumerate(list(cloud_data['word_frequency'].items())[:10], 1):
        # 输出信息到控制台
        print(f"  {i}. {tag}: {freq}次")


# 边界/有效性检查
if __name__ == '__main__':
    # 运行任务
    asyncio.run(demo_generate_tag_cloud())