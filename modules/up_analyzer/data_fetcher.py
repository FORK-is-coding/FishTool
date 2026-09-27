"""
UP主数据采集器 - 多源数据获取与退化策略
优先级: zeroroku三方站点 > B站公开页面爬取 > 本地估算

本模块实现 UP 主数据的多源采集与自动降级：

一、数据源优先级
1. zeroroku 三方站点（fetch_from_zeroroku）
   - 提供粉丝增长曲线/投稿频率/互动率等深度数据
   - 404/超时/异常均降级到 B 站
2. B 站公开 API（fetch_from_bilibili）
   - 基础信息 + 粉丝数 + 最近视频列表
   - 记录 api_success 标记数据可靠性
3. 本地估算（estimate_metrics）
   - 基于公开数据估算投稿频率/互动率/平均播放
   - data_completeness=partial 标记部分数据

二、统一入口（fetch_up_data）
- 自动解析 UID 或主页链接（extract_uid_from_url）
- 按优先级依次尝试，返回数据源标识与完整度
- 三种结果：full（三方）/ partial（B站+本地）/ failed（仅本地）

三、辅助能力
- fetch_category_top_ups: 分区头部 UP 主列表
  （基于周榜，内置分区ID映射）

四、资源管理
- 实现异步上下文管理器（__aenter__/__aexit__）
- 统一持有 aiohttp.ClientSession（30s超时）

注意：
- zeroroku API 端点为假设格式，实际需按其文档调整
- 三方失败时 B 站 API 也失败会导致数据不可靠，
  调用方应检查 completeness/数据源标识
"""
import asyncio
import re
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import aiohttp
from bs4 import BeautifulSoup
import logging

from core.exceptions import BilibiliAPIError, NetworkError, ValidationError
from core.logger import get_logger
from bilibili.api import BilibiliAPI
from bilibili.rate_limiter import RateLimiter
from core.database import get_session, UPMaster

logger = get_logger(__name__)


class UPDataFetcher:
    """UP主数据采集器 - 多源数据获取
    
    按优先级尝试三方站点/B站公开数据/本地估算，
    输出带数据源标识的完整数据。
    """
    
    def __init__(self, bili_api: BilibiliAPI, rate_limiter: RateLimiter):
        """初始化数据采集器
        
        Args:
            bili_api: B站API客户端
            rate_limiter: 限频器
        """
        self.bili_api = bili_api
        self.rate_limiter = rate_limiter
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        """异步上下文管理器入口
        
        进入时创建共享 HTTP 会话。
        # 实例化对象并准备使用
        """
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出
        
        退出时关闭共享 HTTP 会话。
        """
        if self.session:
            await self.session.close()
    
    def extract_uid_from_url(self, url: str) -> Optional[int]:
        """从B站主页链接提取UID
        # 从数据中取出目标字段，供后续逻辑使用
        
        Args:
            url: B站主页链接，如 https://space.bilibili.com/123456
            
        Returns:
            UID，解析失败返回None
        """
        try:
            # 匹配 space.bilibili.com/数字
            match = re.search(r'space\.bilibili\.com/(\d+)', url)
            if match:
                return int(match.group(1))
            
            # 直接是数字
            if url.isdigit():
                return int(url)
            
            logger.warning(f"无法从链接提取UID: {url}")
            return None
        except Exception as e:
            logger.error(f"解析UID失败: {e}")
            return None
    
    async def fetch_from_zeroroku(self, uid: int) -> Optional[Dict[str, Any]]:
        """从zeroroku获取UP主数据（三方数据源）
        
        提供粉丝增长曲线等 B 站公开 API 拿不到的深度数据。
        
        Args:
            uid: B站UID
            
        Returns:
            数据字典，获取失败返回None
            包含字段: fans_growth, post_frequency, engagement_rate, video_data
        """
        if not self.session:
            logger.error("Session未初始化")
            return None
        
        try:
            logger.info(f"[zeroroku] 尝试获取UP主数据: {uid}")
            
            # zeroroku API端点（假设接口格式）
            # 注：实际需要根据zeroroku真实API调整
            url = f"https://api.zeroroku.cn/v1/up/{uid}/stats"
            
            # 上下文管理：确保资源自动释放
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                # 404 表示未收录
                if resp.status == 404:
                    logger.warning(f"[zeroroku] UP主{uid}未收录，退化到B站爬取")
                    return None
                
                # 其他非200错误
                if resp.status != 200:
                    logger.warning(f"[zeroroku] 请求失败 {resp.status}")
                    return None
                
                data = await resp.json()
                
                # 解析zeroroku数据格式
                result = {
                    'source': 'zeroroku',
                    'fans_growth': data.get('fans_growth', []),  # 粉丝增长曲线
                    'post_frequency': data.get('post_stats', {}).get('avg_per_week', 0),
                    'engagement_rate': data.get('engagement', {}).get('avg_rate', 0),
                    'video_data': data.get('recent_videos', []),
                    'raw_data': data
                }
                
                logger.info(f"[zeroroku] 成功获取UP主{uid}数据")
                return result
                
        except asyncio.TimeoutError:
            logger.warning(f"[zeroroku] 请求超时，退化到B站爬取")
            return None
        except Exception as e:
            logger.warning(f"[zeroroku] 获取失败: {e}，退化到B站爬取")
            return None
    
    async def fetch_from_bilibili(self, uid: int) -> Dict[str, Any]:
        """从B站公开接口获取UP主数据，并按维度隔离失败。

        Args:
            uid: B站用户UID。

        Returns:
            包含基础资料、粉丝、累计播放和投稿列表的结构化结果。
        """
        logger.info(f"[B站爬取] 获取UP主{uid}公开数据")
        result: Dict[str, Any] = {
            'source': 'bilibili',
            'name': '',
            'face': '',
            'fans': 0,
            'charge_count': 0,
            'charge_source': 'unavailable',
            'guard_count': None,
            'guard_source': 'unavailable',
            'live_status': 0,
            'video_list': [],
            'stats': {},
            'api_success': False,
            'data_errors': {},
        }

        async def call_dimension(name: str, callback):
            """执行单个数据维度请求，失败时记录错误并继续其它维度。"""
            try:
                await self.rate_limiter.acquire('normal')
                return await callback()
            except Exception as exc:
                result['data_errors'][name] = str(exc)
                logger.warning(f"[B站爬取] {name} 获取失败，继续采集其它维度: {exc}")
                return None

        user_info = await call_dimension('user_info', lambda: self.bili_api.get_user_info(uid))
        if isinstance(user_info, dict) and user_info.get('data'):
            data = user_info['data']
            result['name'] = data.get('name') or ''
            result['mid'] = int(data.get('mid') or uid)
            result['face'] = data.get('face') or ''
            result['stats']['level'] = data.get('level', 0)
            result['stats']['official'] = data.get('official', {})

        relation = await call_dimension(
            'relation_stat', lambda: self.bili_api.get_user_relation_stat(uid)
        )
        if isinstance(relation, dict) and relation.get('data'):
            relation_data = relation['data']
            result['fans'] = relation_data.get('follower', relation_data.get('fans', 0)) or 0

        charge_data = await call_dimension('charge_count', lambda: self.bili_api.get_charge_count(uid))
        if isinstance(charge_data, dict):
            result['charge_count'] = int(charge_data.get('charge_count') or 0)
            result['charge_source'] = charge_data.get('source', 'unavailable')

        # 舰长数：直播基础信息 + 大航海 topList，失败降级 None 不阻塞主流程
        async def _fetch_guard():
            room_map = await self.bili_api.get_room_base_info([uid])
            room = ((room_map.get('data') or {}).get(str(uid)) or {}) if isinstance(room_map, dict) else {}
            room_id = int(room.get('room_id') or 0)
            live_status = int(room.get('live_status') or 0)
            guard_num = 0
            if room_id:
                guard_data = await self.bili_api.get_guard_top_list(room_id, int(uid))
                guard_num = int(((guard_data.get('data') or {}).get('info') or {}).get('num') or 0)
            return {'guard_count': guard_num, 'guard_source': 'guard_top_list' if room_id else 'no_room', 'live_status': live_status}
        guard_data = await call_dimension('guard_count', _fetch_guard)
        if isinstance(guard_data, dict):
            result['guard_count'] = guard_data.get('guard_count')
            result['guard_source'] = guard_data.get('guard_source', 'unavailable')
            result['live_status'] = guard_data.get('live_status', 0)

        upstat = await call_dimension('upstat', lambda: self.bili_api.get_user_upstat(uid))
        if isinstance(upstat, dict) and upstat.get('data'):
            archive = (upstat['data'].get('archive') or {})
            result['stats']['total_play'] = archive.get(
                'view', upstat['data'].get('archive_view', 0)
            ) or 0

        videos_data = await call_dimension(
            'user_videos', lambda: self.bili_api.get_user_videos(uid, page_size=30)
        )
        if isinstance(videos_data, dict) and videos_data.get('data'):
            result['video_list'] = (
                videos_data['data'].get('list', {}).get('vlist', []) or []
            )

        successful_dimensions = len(result['data_errors']) < 4
        result['api_success'] = successful_dimensions and bool(
            result['name'] or result['fans'] or result['video_list'] or result['stats']
        )
        if result['data_errors']:
            logger.warning(
                "[B站爬取] UP主%s 部分数据不可用: %s",
                uid,
                ', '.join(sorted(result['data_errors'])),
            )
        return result
    
    def _save_account_snapshot(self, data: Dict[str, Any]) -> None:
        """将本次 UP 主采集结果增量写入 SQLite。

        Args:
            data: 包含 mid/name/fans/charge_count 的采集结果。

        Returns:
            无；数据库失败时回滚并记录日志，不阻断接口返回。
        """
        session = None
        try:
            uid = int(data.get('mid') or 0)
            if not uid:
                return
            session = get_session()
            account = session.query(UPMaster).filter(UPMaster.mid == uid).first()
            if account is None:
                account = UPMaster(mid=uid)
                session.add(account)
            if data.get('name') is not None:
                account.name = data.get('name') or account.name
            account.follower = int(data.get('fans') or 0)
            account.charge_count = int(data.get('charge_count') or 0)
            account.updated_at = datetime.now()
            session.commit()
        except Exception as exc:
            if session is not None:
                session.rollback()
            logger.error("UP主增量快照落库失败: %s", exc)
        finally:
            if session is not None:
                session.close()

    def estimate_metrics(self, bili_data: Dict[str, Any]) -> Dict[str, Any]:
        """基于B站公开数据本地估算运营指标
        
        Args:
            bili_data: 从B站获取的数据
            
        Returns:
            估算的运营指标
        """
        logger.info("[本地估算] 计算运营指标")
        
        video_list = bili_data.get('video_list', [])
        fans = bili_data.get('fans', 0)
        
        metrics = {
            'fans_growth': [],  # 无法估算，留空
            'post_frequency': 0,
            'engagement_rate': 0,
            'avg_play': 0,
            'avg_comment': 0,
            'data_completeness': 'partial'  # 标记数据完整性
        }
        
        # 条件判断：not video_list
        if not video_list:
            logger.warning("[本地估算] 无视频数据，无法估算")
            return metrics
        
        # 异常保护：失败时降级处理
        try:
            # 1. 投稿频率估算（视频数/时间跨度）
            if len(video_list) >= 2:
                # 按创建时间排序
                sorted_videos = sorted(video_list, key=lambda x: x.get('created', 0))
                time_span_days = (sorted_videos[-1]['created'] - sorted_videos[0]['created']) / 86400
                
                # 条件判断：time_span_days > 0
                if time_span_days > 0:
                    videos_per_week = len(video_list) / (time_span_days / 7)
                    metrics['post_frequency'] = round(videos_per_week, 2)
            
            # 2. 互动率估算（平均播放/评论/点赞 vs 粉丝数）
            total_play = sum(v.get('play', 0) for v in video_list)
            total_comment = sum(v.get('comment', 0) for v in video_list)
            
            metrics['avg_play'] = int(total_play / len(video_list)) if video_list else 0
            metrics['avg_comment'] = int(total_comment / len(video_list)) if video_list else 0
            
            # 粗略互动率 = 平均播放 / 粉丝数
            if fans > 0:
                metrics['engagement_rate'] = round((metrics['avg_play'] / fans) * 100, 2)
            
            logger.info(f"[本地估算] 投稿频率: {metrics['post_frequency']}视频/周, "
                       f"互动率: {metrics['engagement_rate']}%")
            
        except Exception as e:
            logger.error(f"[本地估算] 计算失败: {e}")
        
        return metrics

    def _normalize_up_data(
        self,
        source_data: Dict[str, Any],
        metrics: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """归一化不同数据源的UP主字段。

        Args:
            source_data: B站或zeroroku返回的原始业务数据。
            metrics: 已计算的运营指标，未传时从源数据提取。

        Returns:
            固定暴露name/face/fans/total_play/video_list/metrics的数据字典。
        """
        raw_data = source_data.get('raw_data') or {}
        stats = source_data.get('stats') or raw_data.get('stats') or {}
        profile = (
            source_data.get('user_info')
            or raw_data.get('user_info')
            or raw_data.get('profile')
            or {}
        )

        # 三方接口字段可能存在别名，在唯一出口统一兼容，前端无需猜测层级。
        name = source_data.get('name') or raw_data.get('name') or profile.get('name') or ''
        face = source_data.get('face') or raw_data.get('face') or profile.get('face') or ''
        fans = source_data.get('fans')
        if fans is None:
            fans = source_data.get('follower_count', raw_data.get('fans', raw_data.get('follower', 0)))
        total_play = source_data.get('total_play')
        if total_play is None:
            total_play = source_data.get(
                'play_count',
                stats.get('total_play', raw_data.get('total_play', raw_data.get('archive_view', 0)))
            )
        video_list = (
            source_data.get('video_list')
            or source_data.get('video_data')
            or raw_data.get('recent_videos')
            or []
        )
        normalized_metrics = metrics if metrics is not None else source_data.get('metrics')
        if normalized_metrics is None:
            normalized_metrics = {
                'fans_growth': source_data.get('fans_growth', []),
                'post_frequency': source_data.get('post_frequency', 0),
                'engagement_rate': source_data.get('engagement_rate', 0),
            }

        return {
            **source_data,
            'name': name,
            'face': face,
            'fans': fans or 0,
            'charge_count': source_data.get('charge_count', raw_data.get('charge_count', 0)) or 0,
            'charge_source': source_data.get('charge_source', raw_data.get('charge_source', 'unavailable')),
            'total_play': total_play or 0,
            'video_list': video_list,
            'metrics': normalized_metrics,
        }
    
    async def fetch_up_data(self, uid_or_url: str) -> Dict[str, Any]:
        """获取UP主完整数据（多源退化策略）
        
        按优先级尝试：三方 -> B站 -> 本地估算。
        
        Args:
            uid_or_url: UID或B站主页链接
            
        Returns:
            完整数据字典
        """
        # 1. 解析UID
        if isinstance(uid_or_url, str) and not uid_or_url.isdigit():
            uid = self.extract_uid_from_url(uid_or_url)
            # 条件判断：not uid
            if not uid:
                raise ValidationError("uid", f"无法解析UID: {uid_or_url}")
        else:
            # 数值转换存入 uid
            uid = int(uid_or_url)
        
        logger.info(f"===== 开始获取UP主{uid}数据 =====")
        
        # 2. 尝试zeroroku（三方数据源）
        zeroroku_data = await self.fetch_from_zeroroku(uid)
        
        # 条件判断：zeroroku_data
        if zeroroku_data:
            # 三方数据完整，直接返回
            logger.info(f"[数据源] zeroroku（完整数据）")
            # 返回 {'uid': uid, 'data_source': 'zeroroku', 'data': zeroroku_dat
            normalized_data = self._normalize_up_data(zeroroku_data)
            return {
                'uid': uid,
                'data_source': 'zeroroku',
                # 所有数据源均通过归一化出口，固定暴露前端所需字段。
                'data': normalized_data,
                'completeness': 'full'
            }
        
        # 3. 退化到B站爬取
        bili_data = await self.fetch_from_bilibili(uid)
        
        # 检查API是否成功
        if not bili_data.get('api_success', False):
            logger.error(f"[降级警告] UP主{uid}的B站API调用失败，数据可能不完整或为默认值")
        
        # 4. 本地估算指标
        metrics = self.estimate_metrics(bili_data)
        
        # 根据API成功状态决定数据源标识
        if bili_data.get('api_success', False):
            data_source_label = 'bilibili+local'
            logger.info(f"[数据源] B站公开API+本地估算（部分数据）")
        else:
            data_source_label = 'local_fallback'
            logger.warning(f"[数据源] 本地降级估算（API失败，数据不可靠）")
        
        # 返回 {'uid': uid, 'data_source': data_source_label, 'data': {'sou
        normalized_data = self._normalize_up_data(bili_data, metrics)
        self._save_account_snapshot(bili_data)
        return {
            'uid': uid,
            'data_source': data_source_label,
            # data层显式暴露统一字段，并保留原始数据便于后端诊断。
            'data': {
                'source': 'bilibili',
                'name': normalized_data['name'],
                'face': normalized_data['face'],
                'fans': normalized_data['fans'],
                'charge_count': normalized_data['charge_count'],
                'charge_source': normalized_data['charge_source'],
                'total_play': normalized_data['total_play'],
                'video_list': normalized_data['video_list'],
                'metrics': normalized_data['metrics'],
                'raw_data': bili_data,
                'api_success': bili_data.get('api_success', False)
            },
            'completeness': 'partial' if bili_data.get('api_success', False) else 'failed'
        }
    
    async def fetch_category_top_ups(self, category: str, limit: int = 20) -> List[Dict[str, Any]]:
        """获取分区头部UP主列表
        
        基于分区周榜，提取 UP 主基本信息。
        # 从数据中取出目标字段，供后续逻辑使用
        
        Args:
            category: 分区名称，如 '美食', '游戏', '数码'
            limit: 返回数量
            
        Returns:
            UP主列表
        """
        logger.info(f"[分区榜单] 获取{category}分区头部UP主")
        
        # B站分区ID映射
        category_map = {
            '美食': 211,
            '游戏': 4,
            '数码': 188,
            '生活': 160,
            '知识': 36,
            '动画': 1,
            '音乐': 3,
            '舞蹈': 129,
            '娱乐': 5,
            '影视': 181,
            '科技': 188,
            '运动': 234,
            '汽车': 223,
            '时尚': 155,
            '资讯': 202,
            '鬼畜': 119,
            '动物圈': 217
        }
        
        # 兼容前端的“游戏区/游戏分区”等展示名称
        normalized_category = str(category or '').strip()
        normalized_category = normalized_category.removesuffix('分区').removesuffix('区')
        tid = category_map.get(normalized_category)
        if tid is None:
            for name, candidate_tid in category_map.items():
                if name in normalized_category or normalized_category in name:
                    tid = candidate_tid
                    break
        # 未知分区必须明确失败原因，不能静默显示 0 位UP主
        if tid is None:
            message = f"不支持的B站分区: {category}，请使用 /api/zones 返回的标准名称"
            logger.error(message)
            raise ValidationError("category", message)
        
        # 异常保护：失败时降级处理
        try:
            await self.rate_limiter.acquire('normal')
            
            # 获取分区排行榜
            ranking_data = await self.bili_api.get_ranking(
                rid=tid,
                day=7,  # 周榜
                original=0  # 全部
            )
            
            # 条件判断：not ranking_data or not ranking_data.get('data')
            if not ranking_data or not ranking_data.get('data'):
                message = f"获取{category}分区榜单失败：B站返回空数据，可能是Cookie失效或触发风控"
                logger.error(message)
                raise BilibiliAPIError(message)
            
            rank_list = ranking_data['data'].get('list', [])
            
            # 提取UP主信息
            # 从数据中取出目标字段，供后续逻辑使用
            up_list = []
            # 遍历 rank_list[:limit]
            for item in rank_list[:limit]:
                up_info = {
                    # 统一输出契约：前端展示的是粉丝总数和UP主累计播放量。
                    'uid': item.get('owner', {}).get('mid'),
                    'name': item.get('owner', {}).get('name'),
                    'face': item.get('owner', {}).get('face'),
                    'video_title': item.get('title'),
                    'bvid': item.get('bvid'),
                    'play': item.get('stat', {}).get('view', 0),
                    'follower_count': 0,
                    'charge_count': 0,
                    'charge_source': 'unavailable',
                    'total_play': 0,
                    'rank': item.get('rank', 0)
                }
                # 榜单只提供单视频 stat.view，补调新版用户统计接口获取真实账号指标。
                owner_uid = up_info['uid']
                if owner_uid:
                    try:
                        await self.rate_limiter.acquire('normal')
                        relation = await self.bili_api.get_user_relation_stat(owner_uid)
                        relation_data = (relation or {}).get('data') or {}
                        up_info['follower_count'] = relation_data.get(
                            'follower', relation_data.get('fans', 0)
                        ) or 0
                    except Exception as exc:
                        up_info['data_errors'] = {'relation_stat': str(exc)}
                        logger.warning(f"[分区榜单] UID {owner_uid} 粉丝数不可用: {exc}")
                    try:
                        await self.rate_limiter.acquire('normal')
                        charge_data = await self.bili_api.get_charge_count(owner_uid)
                        up_info['charge_count'] = int(charge_data.get('charge_count') or 0)
                        up_info['charge_source'] = charge_data.get('source', 'unavailable')
                    except Exception as exc:
                        up_info.setdefault('data_errors', {})['charge_count'] = str(exc)
                        logger.warning("[分区榜单] UID %s 充电人数不可用: %s", owner_uid, exc)
                    try:
                        await self.rate_limiter.acquire('normal')
                        upstat_data = (upstat or {}).get('data') or {}
                        up_info['total_play'] = (upstat_data.get('archive') or {}).get(
                            'view', upstat_data.get('archive_view', 0)
                        ) or 0
                    except Exception as exc:
                        up_info.setdefault('data_errors', {})['upstat'] = str(exc)
                        logger.warning(f"[分区榜单] UID {owner_uid} 累计播放不可用: {exc}")
                up_list.append(up_info)
            
            logger.info(f"[分区榜单] 获取到{len(up_list)}个UP主")
            return up_list
            
        except Exception as e:
            # 记录完整失败原因，交给路由转成明确错误
            message = f"获取{category}分区榜单失败：{e}（B站 ranking/v2 的 type 参数只接受 all/origin 字符串，请检查请求参数）"
            logger.error(f"[分区榜单] {message}")
            raise BilibiliAPIError(message) from e
