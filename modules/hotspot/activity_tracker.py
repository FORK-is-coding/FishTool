"""
B站活动情报追踪器
从B站官方活动中心抓取活动信息，补充UGC运营号动态

本模块负责收集"活动情报"，为选题建议提供素材：

一、官方活动采集（get_official_activities）
- 调用活动中心 API /x/activity/page/list
- 解析活动名称/链接/封面/简介/起止时间/标签
# 将原始文本转为结构化数据
# 将数据从一种形态映射为另一种
- 自动判定活动状态（upcoming/ongoing/ended）

二、UGC运营号动态补充（get_ugc_account_dynamics）
- 追踪重点运营账号（KEY_ACCOUNTS 配置）
- 解析动态文字/图片/发布时间
# 将原始文本转为结构化数据
# 将数据从一种形态映射为另一种
- 仅保留含活动关键词的动态（_is_activity_related）
- 图片只存 URL 不下载，节省流量

三、活动详情（get_activity_detail）
- 获取活动完整规则/奖励/参与要求
# 读取数据并赋值给当前作用域变量

四、汇总入库（fetch_all_activities）
- 官方 + UGC 合并
- 按 activity_id 去重 upsert 到 Activity 表
- 返回统计与完整活动列表
# 将结果交回调用方

关键辅助方法：
- _dynamic_to_activity: UGC动态转活动格式
- _extract_title_from_text: 提取标题（首行截断）
# 从数据中取出目标字段，供后续逻辑使用
- _parse_timestamp: 秒/毫秒时间戳兼容解析
# 将原始文本转为结构化数据
# 将数据从一种形态映射为另一种
- _get_activity_status: 状态判定

依赖：
- bilibili.api / bilibili.rate_limiter
- core.database: Activity 模型
"""
import asyncio
# 从 typing 导入符号
from typing import List, Dict, Any, Optional
# 从 datetime 导入符号
from datetime import datetime, timedelta
# 从 pathlib 导入符号
from pathlib import Path
# 导入模块
import re
# 导入模块
import logging

# 从 bilibili.api 导入符号
from bilibili.api import BilibiliAPI
# 从 bilibili.rate_limiter 导入符号
from bilibili.rate_limiter import RateLimiter
# 从 core.exceptions 导入符号
from core.exceptions import BilibiliAPIError
# 从 core.logger 导入符号
from core.logger import get_logger
# 从 core.database 导入符号
from core.database import get_session, Activity

logger = get_logger(__name__)


class ActivityTracker:
    """B站活动情报追踪器
    
    聚合官方活动中心与 UGC 运营号动态，
    输出结构化的活动情报列表。
    """
    
    # 重点关注的运营账号UID（画师同人站、官方运营号等）
    # 不同分区对应不同官号组：选游戏区爬游戏官号活动，选动画/绘画区爬平台官号与蓝标UGC账号动态
    # 注意：这些UID为2026-08实测可用的真实UID（B站搜索接口验证，蓝标verify=1）
    KEY_ACCOUNTS = {
        '26366366': '哔哩哔哩活动',
        '928123': '哔哩哔哩番剧',
        '1328260': '哔哩哔哩游戏中心',
        '9617619': '哔哩哔哩直播',
        '37090048': '哔哩哔哩创作中心',
        '1105739740': '画师同人站',
        '98627270': '哔哩哔哩国创',
        '326499679': '哔哩哔哩漫画',
        '401742377': '原神',
        '161775300': '明日方舟',
        '1340190821': '崩坏星穹铁道',
        '1636034895': '绝区零',
    }

    # 分区 -> 官号账号组 [(uid, 名称)]
    # 说明：UID 如失效可在 config.yaml 的 activity.accounts 下覆盖维护
    # 官方账号（蓝标 verify=1）+ 各分区头部游戏/创作官号，均实测可拉取动态
    ZONE_ACCOUNTS = {
        'all': [
            ('26366366', '哔哩哔哩活动'),
            ('37090048', '哔哩哔哩创作中心'),
            ('928123', '哔哩哔哩番剧'),
            ('1328260', '哔哩哔哩游戏中心'),
            ('9617619', '哔哩哔哩直播'),
        ],
        'game': [
            ('401742377', '原神'),
            ('161775300', '明日方舟'),
            ('1340190821', '崩坏星穹铁道'),
            ('1636034895', '绝区零'),
            ('1328260', '哔哩哔哩游戏中心'),
        ],
        'anime': [
            ('928123', '哔哩哔哩番剧'),
            ('98627270', '哔哩哔哩国创'),
            ('326499679', '哔哩哔哩漫画'),
            ('26366366', '哔哩哔哩活动'),
        ],
        'paint': [
            # 绘画区蓝标UGC账号（画师同人站等），可在 config.yaml activity.accounts.paint 下维护
            ('1105739740', '画师同人站'),
            ('2072860945', 'B站绘画小课堂'),
        ],
    }

    # 可选：支持的分区列表（与前端下拉保持一致）
    SUPPORTED_ZONES = ['all', 'game', 'anime', 'paint']
    
    def __init__(self, api: BilibiliAPI, rate_limiter: Optional[RateLimiter] = None):
        """初始化活动追踪器
        # 设置初始值/默认状态，避免后续空引用
        # 写入配置/属性，影响后续行为
        
        Args:
            api: B站API实例
            rate_limiter: 限频器实例
        """
        self.api = api
        # 确保 rate_limiter 永远不为 None，自动创建兜底实例
        self.rate_limiter = rate_limiter or api.rate_limiter or RateLimiter()
        
    async def get_official_activities(self, page: int = 1, page_size: int = 20) -> List[Dict[str, Any]]:
        """获取B站官方活动中心的活动列表
        # 读取数据并赋值给当前作用域变量
        
        Args:
            page: 页码
            page_size: 每页数量
            
        Returns:
            活动列表
        """
        logger.info(f"开始获取官方活动列表，第 {page} 页")
        
        # 异常保护：局部失败不影响主流程
        try:
            # 请求前限频
            await self.rate_limiter.acquire()
            
            # B站活动中心API（PC端 plat=1，无需WBI签名）
            url = "https://api.bilibili.com/x/activity/page/list"
            params = {
                'pn': page,
                'ps': page_size,
                'type': 0,  # 0=全部
                'plat': 1,  # 平台：1=PC（缺失该参数接口会报-400）
            }
            
            data = await self.api.get(url, params=params, need_sign=False)
            
            # 数据异常处理
            if not data or 'list' not in data:
                logger.warning("官方活动列表返回数据异常")
                return []
            
            # 解析活动列表
            # 将数据从一种形态映射为另一种
            activities = []
            # 循环遍历处理
            # 对集合内每个元素执行相同处理
            for item in data['list']:
                # 提取活动核心字段并标准化
                # 从数据中取出目标字段，供后续逻辑使用
                # 时间字段先统一解析为 datetime（接口返回的是时间戳）
                start_dt = self._parse_timestamp(item.get('stime'))
                end_dt = self._parse_timestamp(item.get('etime'))
                activity = {
                    'id': item.get('id'),
                    'title': item.get('name', '').strip(),
                    'link': item.get('pc_url') or item.get('h5_url') or item.get('url', ''),
                    'cover': item.get('cover', ''),
                    'desc': item.get('desc', '').strip(),
                    'start_time': start_dt,
                    'end_time': end_dt,
                    'status': self._get_activity_status(start_dt, end_dt),
                    'tags': item.get('tags', []),
                    'source': 'official',
                    'fetched_at': datetime.now()
                }
                # 追加到列表
                activities.append(activity)
            
            logger.info(f"获取到 {len(activities)} 个官方活动")
            return activities
            
        except BilibiliAPIError as e:
            logger.error(f"获取官方活动失败: {e}")
            return []
        except Exception as e:
            # 兜底：NetworkError 等非 APIError 异常也不能让官方活动模块崩掉
            logger.error(f"获取官方活动异常（已忽略）: {e}")
            return []
    
    async def get_ugc_account_dynamics(self, uid: str, offset: int = 0, limit: int = 10) -> List[Dict[str, Any]]:
        """获取UGC运营账号的动态（用于补充活动信息）
        # 读取数据并赋值给当前作用域变量
        
        解析动态中的文字/图片/时间，过滤活动相关内容。
        # 剔除不符合条件的数据
        
        Args:
            uid: 用户UID
            offset: 偏移量
            limit: 获取数量
            # 读取数据并赋值给当前作用域变量
            
        Returns:
            动态列表
        """
        logger.info(f"获取账号 {uid} 的动态")
        
        # 异常保护：局部失败不影响主流程
        try:
            # 异步等待结果
            await self.rate_limiter.acquire(endpoint='dynamic')
            
            # 动态空间API（无需WBI签名；无登录态时可能被412风控，需cookie池或登录态）
            url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
            params = {
                'host_mid': uid,
            }
            # 注意：offset 为 0 时不能传该参数！B站动态接口传 offset=0 会直接返回
            # 错误码 4101129（参数异常/请求频繁），导致所有账号动态拉取失败、一直报0
            if offset and offset > 0:
                params['offset'] = offset
            
            data = await self.api.get(url, params=params, need_sign=False)
            
            # 数据异常处理
            if not data or 'items' not in data:
                logger.warning(f"账号 {uid} 动态返回数据异常")
                return []
            
            # 逐条解析动态
            # 将数据从一种形态映射为另一种
            dynamics = []
            # 循环遍历处理
            # 对集合内每个元素执行相同处理
            for item in data['items'][:limit]:
                # 异常保护：局部失败不影响主流程
                try:
                    # 解析动态类型
                    # 将数据从一种形态映射为另一种
                    dynamic_type = item.get('type', '')
                    # 读取字典/配置项
                    modules = item.get('modules', {})
                    
                    # 提取文字内容（B站新版动态接口：图文/视频动态 desc.text 常为 null，
                    # 文字在 desc.rich_text_nodes 里；画师同人站等活动信息常挂在 topic 话题名、
                    # 视频标题、专栏标题上，必须全部拼起来参与活动关键词过滤）
                    text_content = ''
                    topic_name = ''
                    major_title = ''
                    module_dynamic = modules.get('module_dynamic') if isinstance(modules, dict) else None
                    if isinstance(module_dynamic, dict):
                        desc = module_dynamic.get('desc') or {}
                        if isinstance(desc, dict):
                            if desc.get('text'):
                                text_content = desc['text']
                            else:
                                # 新接口文字在 rich_text_nodes 节点列表里
                                nodes = desc.get('rich_text_nodes') or []
                                if isinstance(nodes, list):
                                    text_content = ''.join(
                                        n.get('text', '') for n in nodes if isinstance(n, dict) and n.get('text')
                                    )
                        # 活动话题名（画师同人站等活动动态基本都挂在话题上）
                        topic = module_dynamic.get('topic') or {}
                        if isinstance(topic, dict) and topic.get('name'):
                            topic_name = topic['name']
                        # 视频/专栏/剧集标题
                        major = module_dynamic.get('major') or {}
                        if isinstance(major, dict):
                            for key in ('archive', 'article', 'pgc', 'common'):
                                sub = major.get(key) or {}
                                if isinstance(sub, dict) and sub.get('title'):
                                    major_title = sub['title']
                                    break
                    
                    # 提取图片URL（不下载，只保存URL；draw.items 可能为 None）
                    image_urls = []
                    if isinstance(module_dynamic, dict):
                        major = module_dynamic.get('major') or {}
                        if major.get('type') == 'MAJOR_TYPE_DRAW':
                            draw_items = (major.get('draw') or {}).get('items') or []
                            if isinstance(draw_items, list):
                                image_urls = [img.get('src', '') for img in draw_items if isinstance(img, dict)]
                    
                    # 提取时间（module_author 可能为 None，pub_ts 可能是字符串）
                    module_author = item.get('modules', {}).get('module_author') if isinstance(item.get('modules'), dict) else None
                    timestamp = (module_author or {}).get('pub_ts', 0)
                    try:
                        pub_time = datetime.fromtimestamp(int(timestamp)) if timestamp else None
                    except (ValueError, TypeError, OSError):
                        pub_time = None
                    
                    # 组装动态数据
                    dynamic = {
                        'id': item.get('id_str', ''),
                        'uid': uid,
                        'username': self.KEY_ACCOUNTS.get(uid, f'UID:{uid}'),
                        'type': dynamic_type,
                        'text': text_content,
                        'topic': topic_name,
                        'title': major_title,
                        'images': image_urls,  # 只存URL，让用户点击查看
                        'pub_time': pub_time,
                        'source': 'ugc_dynamic',
                        'fetched_at': datetime.now()
                    }
                    
                    # 过滤：只保留包含活动关键词的动态（文字+topic+标题合并判断）
                    # 画师同人站很多活动动态是纯图文，正文为空，但 topic 就是活动名
                    if self._is_activity_related(f"{text_content} {topic_name} {major_title}"):
                        dynamics.append(dynamic)
                        
                except Exception as e:
                    logger.warning(f"解析动态失败: {e}")
                    # 跳过本轮继续循环
                    continue
            
            logger.info(f"账号 {uid} 获取到 {len(dynamics)} 条活动相关动态")
            return dynamics
            
        except BilibiliAPIError as e:
            logger.error(f"获取账号 {uid} 动态失败: {e}")
            return []
        except Exception as e:
            # 兜底：B站风控偶发断连/超时抛 NetworkError 等非 APIError 异常，
            # 单个账号失败不能冒泡导致整个活动模块 500 甚至崩进程
            logger.error(f"获取账号 {uid} 动态异常（已忽略）: {e}")
            return []
    
    async def fetch_all_activities(self, include_ugc: bool = True, zone: str = 'all') -> Dict[str, Any]:
        """拉取所有活动情报（官方+UGC，支持按分区筛选官号）

        Args:
            include_ugc: 是否包含UGC运营号动态
            zone: 分区筛选，取值：
                - all: 全部官号（默认）
                - game: 游戏区（各大游戏官号）
                - anime: 动画区（B站番剧/动画官号）
                - paint: 绘画区（画师同人站等蓝标UGC账号）

        Returns:
            活动汇总数据（含 zone 字段与分区账号信息）
        """
        logger.info(f"开始拉取活动情报，分区={zone}")
        
        # 1. 获取官方活动（活动中心接口，全平台统一，不按分区过滤）
        official_activities = await self.get_official_activities(page=1, page_size=30)
        
        # 2. 获取UGC动态（如果启用）——按分区选择重点官号账号组
        ugc_dynamics = []
        accounts = self._get_zone_accounts(zone)
        # 判断 include_ugc
        # 根据条件走向不同处理分支
        if include_ugc:
            # 循环遍历处理
            # 对集合内每个元素执行相同处理
            for uid, uname in accounts:
                # 赋值并准备后续使用
                dynamics = await self.get_ugc_account_dynamics(uid, limit=10)
                # 为动态补充分区与账号名，便于前端展示与入库
                for d in dynamics:
                    d['zone'] = zone
                    d['source_username'] = uname
                # 批量扩展列表
                ugc_dynamics.extend(dynamics)
        
        # 3. 合并去重
        # UGC动态转为活动格式后合并
        # 将数据从一种形态映射为另一种
        all_activities = official_activities + [self._dynamic_to_activity(d) for d in ugc_dynamics]
        # 给官方活动补充分区标记（官方活动属全站，标记为 all）
        for act in all_activities:
            act.setdefault('zone', zone if act.get('source') == 'ugc' else 'all')
        
        # 4. 保存到数据库
        await self._save_to_database(all_activities, zone=zone)
        
        # 组装汇总结果
        result = {
            'official_count': len(official_activities),
            'ugc_count': len(ugc_dynamics),
            'total_count': len(all_activities),
            'zone': zone,
            'zone_accounts': [{'uid': u, 'name': n} for u, n in accounts],
            'activities': all_activities,
            'fetched_at': datetime.now().isoformat()
        }
        
        logger.info(f"活动情报拉取完成，分区={zone}，官方 {len(official_activities)} 个，UGC {len(ugc_dynamics)} 条")
        return result

    def _get_zone_accounts(self, zone: str) -> List[tuple]:
        """获取指定分区的官号账号组（支持 config.yaml activity.accounts 覆盖）

        优先级：config.yaml 中 activity.accounts.<zone> 配置 > 代码内置默认值。

        Args:
            zone: 分区标识（all/game/anime/paint）

        Returns:
            [(uid, 名称), ...] 账号列表
        """
        # 尝试从配置读取自定义账号，未配置或读取失败时回退内置默认
        try:
            from core.config import ConfigManager
            cfg = ConfigManager()
            custom = (cfg.get('activity', {}) or {}).get('accounts', {}) or {}
            if custom.get(zone):
                # 配置格式兼容两种：["uid:名称"] 或 [{"uid":..., "name":...}]
                parsed = []
                for item in custom[zone]:
                    if isinstance(item, dict):
                        parsed.append((str(item.get('uid', '')), item.get('name', str(item.get('uid', '')))))
                    else:
                        parts = str(item).split(':', 1)
                        uid = parts[0].strip()
                        name = parts[1].strip() if len(parts) > 1 else uid
                        if uid:
                            parsed.append((uid, name))
                if parsed:
                    return parsed
        except Exception as e:
            # 配置读取失败不阻塞主流程，回退内置默认
            logger.warning(f"读取活动账号配置失败，使用内置默认: {e}")
        
        # 内置默认账号组
        return self.ZONE_ACCOUNTS.get(zone, self.ZONE_ACCOUNTS.get('all', []))
    
    def _is_activity_related(self, text: str) -> bool:
        """判断文本是否与活动相关
        # 根据条件走向不同处理分支
        # 对数据进行加工/分发
        
        匹配活动常见关键词。
        
        Args:
            text: 文本内容
            
        Returns:
            是否相关
        """
        if not text:
            return False
        
        # 活动关键词
        keywords = [
            '活动', '征稿', '比赛', '大赛', '投稿',
            '参与', '报名', '截止', '奖励', '奖品',
            '联动', '企划', '周年', '庆典',
            '激励', '创作激励', '激励计划', '征集', '招募',
            '赛', '应援', '投票', '评选',
            '挑战', '同人', 'Bonly', '拜年纪', '皮肤设计',
        ]
        
        return any(kw in text for kw in keywords)
    
    def _dynamic_to_activity(self, dynamic: Dict[str, Any]) -> Dict[str, Any]:
        """将动态转换为活动格式
        # 将数据从一种形态映射为另一种
        
        Args:
            dynamic: 动态数据
            
        Returns:
            活动格式数据
        """
        # 标题优先用 topic（活动话题名）/视频标题，正文为空时也能展示活动名
        title = dynamic.get('topic') or dynamic.get('title') or ''
        if not title:
            title = self._extract_title_from_text(dynamic['text'])
        return {
            'id': f"ugc_{dynamic['id']}",
            'title': title,
            'link': f"https://t.bilibili.com/{dynamic['id']}",
            'cover': dynamic['images'][0] if dynamic['images'] else '',
            'desc': dynamic['text'],
            'start_time': dynamic['pub_time'],
            'end_time': None,
            'status': 'unknown',
            'tags': [],
            'source': 'ugc',
            'source_uid': dynamic['uid'],
            'source_username': dynamic['username'],
            'images': dynamic['images'],
            'fetched_at': dynamic['fetched_at']
        }
    
    def _extract_title_from_text(self, text: str) -> str:
        """从文本中提取标题（取第一行或前30字）
        # 从数据中取出目标字段，供后续逻辑使用
        
        Args:
            text: 文本内容
            
        Returns:
            标题
        """
        if not text:
            return '无标题'
        
        # 取第一行
        first_line = text.split('\n')[0].strip()
        
        # 限制长度
        if len(first_line) > 30:
            return first_line[:30] + '...'
        
        return first_line if first_line else '无标题'
    
    def _parse_timestamp(self, ts: Any) -> Optional[datetime]:
        """解析时间戳
        # 将原始文本转为结构化数据
        # 将数据从一种形态映射为另一种
        
        兼容秒与毫秒两种格式。
        
        Args:
            ts: 时间戳（秒或毫秒）
            
        Returns:
            datetime对象
        """
        if not ts:
            return None
        
        # 异常保护：局部失败不影响主流程
        try:
            # 数值转换存入 ts
            # 将数据从一种形态映射为另一种
            ts = int(ts)
            # 判断是秒还是毫秒
            # 毫秒时间戳为13位（>10^10）
            if ts > 10000000000:  # 毫秒
                # 计算结果存入 ts
                # 对输入做运算得到结果
                ts = ts / 1000
            return datetime.fromtimestamp(ts)
        # 异常处理
        except Exception:
            return None
    
    def _get_activity_status(self, start_time: Optional[datetime], end_time: Optional[datetime]) -> str:
        """判断活动状态
        # 根据条件走向不同处理分支
        # 对数据进行加工/分发
        
        依据起止时间与当前时间比较：
        - 未开始：upcoming
        - 进行中：ongoing
        - 已结束：ended
        
        Args:
            start_time: 开始时间
            end_time: 结束时间
            
        Returns:
            状态：upcoming/ongoing/ended
        """
        now = datetime.now()
        
        # 三种状态判定
        if start_time and now < start_time:
            return 'upcoming'
        # 多条件判断
        # 根据条件走向不同处理分支
        elif end_time and now > end_time:
            return 'ended'
        # 多条件判断
        # 根据条件走向不同处理分支
        elif start_time and end_time and start_time <= now <= end_time:
            return 'ongoing'
        # 分支判断
        else:
            # 时间信息不全时状态未知
            return 'unknown'
    
    async def _save_to_database(self, activities: List[Dict[str, Any]], zone: str = 'all'):
        """保存活动信息到数据库
        # 持久化数据，防止丢失
        
        按 activity_id upsert：存在则更新，不存在则新增。
        # 用新值覆盖旧值，保持数据一致
        
        Args:
            activities: 活动列表
            zone: 活动所属分区（存入 category 字段，便于按分区检索）
        """
        # 初始化为 None，防止 get_session() 抛异常后 except/finally 引用未定义变量
        session = None
        try:
            # 赋值并准备后续使用
            session = get_session()
            
            # 遍历 activities 逐项处理
            # 对集合内每个元素执行相同处理
            for act in activities:
                # 检查是否已存在
                existing = session.query(Activity).filter_by(
                    activity_id=str(act['id'])
                ).first()
                
                # 判断 existing
                # 根据条件走向不同处理分支
                if existing:
                    # 更新
                    existing.title = act['title']
                    # 赋值并准备后续使用
                    existing.status = act['status']
                    # 分区标记
                    existing.category = act.get('zone') or zone
                    # 赋值并准备后续使用
                    existing.updated_at = datetime.now()
                # 分支判断
                else:
                    # 新增
                    activity = Activity(
                        activity_id=str(act['id']),
                        title=act['title'],
                        url=act['link'],
                        cover=act.get('cover', ''),
                        desc=act.get('desc', ''),
                        category=act.get('zone') or zone,
                        tags=act.get('tags', []),
                        start_time=act.get('start_time'),
                        end_time=act.get('end_time'),
                        status=act['status'],
                        reward_info={
                            'images': act.get('images', []),
                            'source_uid': act.get('source_uid'),
                            'source_username': act.get('source_username')
                        }
                    )
                    # 加入集合/数据库会话
                    session.add(activity)
            
            # 提交事务
            session.commit()
            logger.info(f"活动信息已保存到数据库（分区={zone}）")
            
        except Exception as e:
            logger.error(f"保存活动信息失败: {e}")
            # 回滚事务
            if session is not None:
                session.rollback()
        # 异常处理
        finally:
            # 关闭连接释放资源
            # 释放连接/窗口资源
            if session is not None:
                session.close()


# ============ 使用示例 ============

async def demo_track_activities():
    """演示：追踪活动情报"""
    from bilibili.cookie_pool import CookiePoolManager
    # 从 bilibili.cookie_pool 导入符号
    from bilibili.cookie_pool import get_cookie_pool
    
    # 初始化
    cookie_pool = get_cookie_pool()
    # 赋值并准备后续使用
    api = BilibiliAPI(cookie_pool=cookie_pool)
    
    tracker = ActivityTracker(api)
    
    # 拉取活动情报
    result = await tracker.fetch_all_activities(include_ugc=True)
    
    print(f"官方活动: {result['official_count']} 个")
    # 输出信息到控制台
    print(f"UGC动态: {result['ugc_count']} 条")
    # 输出信息到控制台
    print(f"总计: {result['total_count']} 条情报")
    # 输出信息到控制台
    print("\n最新活动:")
    # 循环遍历处理
    # 对集合内每个元素执行相同处理
    for i, act in enumerate(result['activities'][:5], 1):
        # 输出信息到控制台
        print(f"  {i}. [{act['status']}] {act['title']}")
        # 输出信息到控制台
        print(f"     来源: {act['source']} | {act['link']}")


# 边界/有效性检查
if __name__ == '__main__':
    # 运行任务
    asyncio.run(demo_track_activities())