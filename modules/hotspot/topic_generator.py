"""
AI选题助手
基于分区热点tag，使用LLM生成创意选题，支持选题库管理

本模块提供"热点驱动的选题生成"能力（FishTool 04 · R5 第三批 f）：

一、LLM 选题生成（generate_topics_with_llm）
- 输入：创作方向 + 分区名 + 热门tag列表（可选推荐上下文 recommendation_context）
- LLM 返回 JSON 数组：标题/描述/关键词/理由/难度；事件模式下另有角度/形式/大纲/证据引用/待核实
- 解析失败时返回空列表（由调用方统一走回退）

二、降级方案（generate_topics_fallback）
- LLM 不可用时，用模板组合热门tag生成（旧 tag_only 路径行为不变）
- 事件模式下按事件动作走四类规则模板，并明确标记 research_only
- 保证功能不因 LLM 缺失而不可用

三、统一入口（generate_topics）
1. 无 context：保留原热门 Tag 路径（TagCloudGenerator Top15），输出 generation_mode='tag_only'
2. 有 context：从服务器冻结的机会证据取事件/评估/简报，不再必须重新调 TagCloud 拿 hot_tags
3. 优先 LLM：模型未配置/超时/无效 JSON/未知引用/空结果 → 统一走规则回退
4. persist 只控制统一入口是否调用旧保存 wrapper（带键路径由 TopicGenerationService 传 persist=False）
5. 返回完整结果（含 hot_tags、generation_mode、真实 used_llm）

四、选题库管理
- _insert_topics：flush-only 内核（不 commit / 不 rollback / 不 close / 不发网络）
- _save_to_topic_library：旧 async wrapper，自持短事务调用内核（仅服务旧 tag_only 无幂等键路径）
- get_topic_library: 多条件查询（分区/状态），回读 ai_suggestions 上下文
- update_topic_status: 状态流转（pending/adopted/published）

依赖：
- bilibili.api / llm.client
- .tag_cloud: TagCloudGenerator
- core.database: Topic/Hotspot
"""
import asyncio
# 从 typing 导入符号
from typing import List, Dict, Any, Optional
# 从 datetime 导入符号
from datetime import datetime
# 导入模块
import logging
# 导入模块
import json

# 从 bilibili.api 导入符号
from bilibili.api import BilibiliAPI
# 从 llm.client 导入符号
from llm.client import LLMClient
# 从 core.exceptions 导入符号
from core.exceptions import LLMNotConfiguredError
# 从 core.logger 导入符号
from core.logger import get_logger
# 从 core.database 导入符号
from core.database import get_session, Topic, Hotspot
# 从 tag_cloud 导入符号
from .tag_cloud import TagCloudGenerator

logger = get_logger(__name__)

#: 无 context 的旧路径生成模式。
GENERATION_MODE_TAG_ONLY: str = "tag_only"
#: 有服务器冻结 context 的事件生成模式。
GENERATION_MODE_EVENT: str = "event"
#: 写入 ai_suggestions / 账本的上下文 schema 版本。
CONTEXT_SCHEMA_VERSION: int = 1

#: 用户未声明能力时，标题里禁止出现的词（§4.3：不许出"亲测/采访/独家"等）。
UNDECLARED_ABILITY_TERMS: tuple = ("亲测", "采访", "独家", "实测", "开箱", "试吃")

#: 可透传进 ``Topic.ai_suggestions`` 的上下文键（§5 逐字清单）。
AI_SUGGESTION_CONTEXT_KEYS: tuple = (
    "context_schema_version",
    "hot_event_id",
    "hot_event_assessment_ids",
    "opportunity_run_id",
    "generation_request_id",
    "creator_brief_version",
    "angle",
    "format",
    "outline",
    "evidence_refs",
    "limitations",
    "action",
)

#: 事件规则模板的四类（§4.3）。
RULE_CATEGORY_VERIFY: str = "verify"
RULE_CATEGORY_PRODUCE: str = "produce"
RULE_CATEGORY_DIFFERENTIATE: str = "differentiate"
RULE_CATEGORY_RETROSPECT: str = "retrospect"
RULE_CATEGORY_RESEARCH: str = "research"


def _coerce_datetime(value: Any) -> datetime:
    """把 ``generated_at`` 归一为 ``datetime``（不对任意字符串直接 ``.isoformat()``）。

    Args:
        value: ``datetime`` / 合法 ISO 字符串 / 其它任意值。

    Returns:
        datetime: ``datetime`` 原样返回；合法 ISO 字符串解析；其余（含 ``None``、非法字符串）
        回退为当前时间，避免 ``AttributeError``。
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                # 兼容末尾 'Z'（Python 3.11 起 fromisoformat 也认，但显式替换更稳）。
                return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                pass
    return datetime.now()


def _to_iso(value: Any) -> str:
    """把任意 ``generated_at`` 取值安全转换为 ISO 字符串。"""
    return _coerce_datetime(value).isoformat()


def _title_uses_undeclared_ability(title: Any, available_assets: Any) -> bool:
    """标题是否用了用户**未声明**的能力词（§4.3）。

    Args:
        title: 待检查标题。
        available_assets: 用户声明的素材/能力列表（子串命中即视为已声明）。

    Returns:
        bool: 命中未声明能力词返回 ``True``。
    """
    if not isinstance(title, str) or not title:
        return False
    declared = " ".join(str(asset).strip().lower() for asset in (available_assets or []))
    for term in UNDECLARED_ABILITY_TERMS:
        if term in title and term.lower() not in declared:
            return True
    return False


def _jsonable(value: Any) -> Any:
    """把值递归转换为可 JSON 序列化对象（``datetime`` → ISO 字符串）。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class TopicGenerator:
    """AI选题助手
    
    基于热点 tag 生成创意选题，
    支持 LLM 与降级模板两种模式。
    """
    
    def __init__(self, 
                 api: BilibiliAPI, 
                 llm_client: Optional[LLMClient] = None,
                 tag_generator: Optional[TagCloudGenerator] = None):
        """初始化选题助手
        # 设置初始值/默认状态，避免后续空引用
        # 写入配置/属性，影响后续行为
        
        Args:
            api: B站API实例
            llm_client: LLM客户端（可选）
            tag_generator: Tag词云生成器（可选）
        """
        self.api = api
        self.llm_client = llm_client
        self.tag_generator = tag_generator or TagCloudGenerator(api)
        
    def is_llm_available(self) -> bool:
        """检查LLM是否可用"""
        return self.llm_client is not None and self.llm_client.is_configured()
    
    async def generate_topics_with_llm(self, 
                                       direction: str, 
                                       zone_name: str,
                                       hot_tags: List[str],
                                       count: int = 10,
                                       recommendation_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """使用LLM基于热点tag生成选题
        
        Args:
            direction: 创作方向关键词（如"游戏解说"、"美食测评"等）
            zone_name: 分区名称
            hot_tags: 热门tag列表
            count: 生成数量
            recommendation_context: 服务器冻结的推荐上下文（事件模式；只追加，不影响旧调用）
            
        Returns:
            选题列表
        """
        if not self.is_llm_available():
            # 抛出异常中断流程
            raise LLMNotConfiguredError("LLM未配置，无法使用AI选题功能")
        
        logger.info(f"开始生成【{zone_name}】分区【{direction}】方向的选题，目标 {count} 个")
        
        # 构造提示词
        prompt = self._build_generation_prompt(
            direction, zone_name, hot_tags, count, recommendation_context=recommendation_context
        )
        
        # 调用LLM
        try:
            response = await self.llm_client.chat_completion(
                messages=[
                    {"role": "system", "content": "你是一位资深的B站内容创作顾问，擅长根据热点趋势生成创意选题。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.8,  # 提高创意性
                max_tokens=2000
            )
            
            # 解析LLM返回的JSON
            # 正确解析 OpenAI 格式返回：response['choices'][0]['message']['content']
            # 将结果交回调用方
            if 'choices' in response and len(response['choices']) > 0:
                content = response['choices'][0]['message']['content']
            else:
                content = ''
            topics_data = self._parse_llm_response(content)
            
            # 格式化选题
            topics = []
            # 循环遍历处理
            # 对集合内每个元素执行相同处理
            for i, topic_data in enumerate(topics_data[:count], 1):
                # 从LLM原始数据组装标准化选题
                topic = {
                    'title': topic_data.get('title', f'选题{i}'),
                    'description': topic_data.get('description', ''),
                    'keywords': topic_data.get('keywords', []),
                    'reason': topic_data.get('reason', ''),
                    'difficulty': topic_data.get('difficulty', 'medium'),
                    'zone_name': zone_name,
                    'direction': direction,
                    'related_tags': topic_data.get('related_tags', []),
                    'generated_at': datetime.now(),
                    'status': 'pending'  # pending/adopted/published
                }
                # LLM 只允许写文字：角度/形式/大纲/证据引用/待核实（服务端另行校验）
                for optional_key in ('angle', 'format', 'outline', 'evidence_refs', 'requires_verification'):
                    if optional_key in topic_data:
                        topic[optional_key] = topic_data[optional_key]
                # 追加到列表
                topics.append(topic)
            
            logger.info(f"LLM生成了 {len(topics)} 个选题")
            return topics
            
        except Exception as e:
            logger.error(f"LLM生成选题失败: {e}")
            # 抛出异常中断流程
            raise
    
    async def generate_topics_fallback(self,
                                       direction: str,
                                       zone_name: str,
                                       hot_tags: List[str],
                                       count: int = 10,
                                       recommendation_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """无LLM时的降级方案

        - 无 context：沿用旧模板组合热门 tag（原行为逐字保持）；
        - 有 context：按事件动作走四类规则模板（验证/制作/差异化/复盘/研究草案）。

        Args:
            direction: 创作方向
            zone_name: 分区名称
            hot_tags: 热门tag列表
            count: 生成数量（事件模式为总题数）
            recommendation_context: 服务器冻结的推荐上下文（可选）

        Returns:
            选题列表（简化版）
        """
        events = self._context_events(recommendation_context)
        if events:
            return self._fallback_event_topics(direction, zone_name, count, recommendation_context, events)

        logger.info(f"使用降级方案生成选题（无LLM）")
        
        topics = []
        # 预设标题模板
        templates = [
            "{tag1}与{tag2}的碰撞：{direction}新玩法",
            "深度解析：{tag1}为什么能成为{zone}热门？",
            "盘点{zone}区那些关于{tag1}的经典作品",
            "{tag1} + {tag2}，这个组合你见过吗？",
            "从{tag1}看{zone}区的发展趋势",
        ]
        
        # 循环用不同tag组合填充模板
        for i in range(min(count, len(hot_tags))):
            template = templates[i % len(templates)]
            tag1 = hot_tags[i] if i < len(hot_tags) else '热门话题'
            tag2 = hot_tags[(i + 1) % len(hot_tags)] if len(hot_tags) > 1 else '创意内容'
            
            # 填充模板生成标题
            title = template.format(
                tag1=tag1,
                tag2=tag2,
                direction=direction,
                zone=zone_name
            )
            
            # 组装选题对象
            topic = {
                'title': title,
                'description': f'结合当前热点【{tag1}】的创意选题',
                'keywords': [tag1, tag2, direction],
                'reason': '基于热门tag自动生成',
                'difficulty': 'medium',
                'zone_name': zone_name,
                'direction': direction,
                'related_tags': [tag1, tag2],
                'generated_at': datetime.now(),
                'status': 'pending'
            }
            # 追加到列表
            topics.append(topic)
        
        logger.info(f"降级方案生成了 {len(topics)} 个选题")
        return topics
    
    async def generate_topics(self,
                             direction: str,
                             zone_name: str,
                             # 赋值并准备后续使用
                             count: int = 10,
                             # 赋值并准备后续使用
                             use_llm: bool = True,
                             *,
                             recommendation_context: Optional[Dict[str, Any]] = None,
                             persist: bool = True) -> Dict[str, Any]:
        """生成选题（统一入口）
        
        Args:
            direction: 创作方向关键词
            zone_name: 分区名称
            count: 生成数量
            use_llm: 是否使用LLM（False时使用降级方案）
            recommendation_context: 服务器冻结的推荐上下文（事件模式）
            persist: 是否调用旧保存 wrapper 落库（带键路径由服务层传 False）
            
        Returns:
            选题生成结果
        """
        if recommendation_context is not None:
            return await self._generate_with_context(
                direction, zone_name, count, use_llm, recommendation_context, persist
            )

        logger.info(f"开始生成选题：分区={zone_name}, 方向={direction}, 数量={count}")
        
        # 1. 获取当前分区热门tag
        cloud_data = await self.tag_generator.generate_cloud_data(zone_name, limit=50, top_n=20)
        # 赋值并准备后续使用
        hot_tags = list(cloud_data['word_frequency'].keys())[:15]  # 取Top15
        
        # 无热门tag无法生成
        if not hot_tags:
            logger.warning(f"分区【{zone_name}】未获取到热门tag，无法生成选题")
            # 保持前端响应契约稳定，避免 hot_tags.slice 对 undefined 调用；此失败结构逐字不变
            return {
                'success': False,
                'error': '未获取到热门tag数据',
                'hot_tags': [],
                'topics': []
            }
        
        # 2. 生成选题（模型侧错误统一走规则回退；DB/取消错误不许降级）
        topics, used_llm, warning = await self._draft_topics(
            direction, zone_name, hot_tags, count, use_llm, recommendation_context=None
        )
        
        # 3. persist 只控制是否调用旧保存 wrapper（旧 tag_only 无幂等键路径）
        saved_ids: List[int] = []
        if persist:
            saved_ids = await self._save_to_topic_library(topics)
        
        # 组装成功结果
        result = {
            'success': True,
            'zone_name': zone_name,
            'direction': direction,
            'hot_tags': hot_tags,
            'topics': topics,
            'saved_ids': saved_ids,
            'generated_at': datetime.now().isoformat(),
            'used_llm': used_llm,
            'generation_mode': GENERATION_MODE_TAG_ONLY,
        }
        if warning:
            result['warning'] = warning
        
        logger.info(f"选题生成完成，共 {len(topics)} 个")
        return result

    # ------------------------------------------------------------------ 生成内核

    async def _draft_topics(self,
                            direction: str,
                            zone_name: str,
                            hot_tags: List[str],
                            count: int,
                            use_llm: bool,
                            *,
                            recommendation_context: Optional[Dict[str, Any]]) -> tuple:
        """产出选题草稿（LLM 优先，模型侧失败 → 规则回退）。

        只把**模型调用/解析/校验**包进回退 catch；数据库错误、``CancelledError`` 一律向上抛。

        Args:
            direction: 创作方向。
            zone_name: 分区名。
            hot_tags: 热点/事件标签。
            count: 生成数量。
            use_llm: 用户是否勾选 LLM。
            recommendation_context: 推荐上下文（事件模式）。

        Returns:
            tuple: ``(topics, used_llm, warning)``；``used_llm`` 记录**本次实际成功路径**。
        """
        used_llm = False
        warning: Optional[str] = None

        if use_llm and self.is_llm_available():
            try:
                topics = await self.generate_topics_with_llm(
                    direction, zone_name, hot_tags, count,
                    recommendation_context=recommendation_context,
                )
                # 空模型结果属于无效，触发回退（不把空批次当成功）
                if topics:
                    return topics, True, None
                logger.warning("LLM 返回空选题，视为无效并触发规则回退")
                warning = 'LLM返回空结果，使用了降级方案'
            except asyncio.CancelledError:
                # 取消不能被当成模型失败而降级（否则会阻止关机）
                raise
            except LLMNotConfiguredError:
                logger.warning("LLM未配置，切换到降级方案")
                warning = 'LLM未配置，使用了降级方案'
            except Exception as e:
                # 其它模型侧错误同样统一走规则回退（修 §11.6 缺陷一）
                logger.warning(f"LLM生成选题失败，切换到降级方案: {e}")
                warning = 'LLM生成失败，使用了降级方案'
        elif use_llm and not self.is_llm_available():
            logger.warning("LLM未配置，使用降级方案")
            warning = 'LLM未配置，使用了降级方案'

        topics = await self.generate_topics_fallback(
            direction, zone_name, hot_tags, count, recommendation_context=recommendation_context
        )
        return topics, used_llm, warning

    async def _generate_with_context(self,
                                     direction: str,
                                     zone_name: str,
                                     count: int,
                                     use_llm: bool,
                                     context: Dict[str, Any],
                                     persist: bool) -> Dict[str, Any]:
        """事件模式生成：吃服务器冻结的机会证据，不依赖 TagCloud。

        Args:
            direction: 创作方向。
            zone_name: 分区名。
            count: **总题数**（多事件按冻结顺序分配，总量 <= count）。
            use_llm: 是否用 LLM。
            context: 服务器冻结的推荐上下文。
            persist: 是否调用旧保存 wrapper。

        Returns:
            dict: 生成结果（``generation_mode='event'``）。
        """
        events = self._context_events(context)
        if not events:
            logger.warning("推荐上下文缺少事件，无法生成事件选题")
            return {
                'success': False,
                'error': '未获取到事件上下文',
                'hot_tags': [],
                'topics': [],
                'generation_mode': GENERATION_MODE_EVENT,
            }

        # 以事件实体/锚点作 related_tags，不重新调 TagCloud（E17）
        hot_tags = self._context_hot_tags(events)
        allocation = self._allocate_events(events, count)

        drafts: Optional[List[Dict[str, Any]]] = None
        warning: Optional[str] = None
        if use_llm and self.is_llm_available():
            try:
                candidate_drafts = await self.generate_topics_with_llm(
                    direction, zone_name, hot_tags, len(allocation),
                    recommendation_context=context,
                )
                if self._llm_topics_valid(candidate_drafts, allocation):
                    drafts = candidate_drafts[:len(allocation)]
                else:
                    logger.warning("事件模式模型结果无效（空/未知引用/未声明能力），切换规则模板")
                    warning = '模型结果无效，使用了事件规则模板'
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"事件模式模型生成失败，切换到规则模板: {e}")
                warning = '模型生成失败，使用了事件规则模板'
        elif use_llm:
            warning = 'LLM未配置，使用了事件规则模板'

        topics = self._bind_event_topics(drafts, allocation, context)

        used_llm = drafts is not None
        result: Dict[str, Any] = {
            'success': True,
            'generation_mode': GENERATION_MODE_EVENT,
            'zone_name': zone_name,
            'direction': direction,
            'hot_tags': hot_tags,
            'topics': topics,
            'saved_ids': [],
            'generated_at': datetime.now().isoformat(),
            'used_llm': used_llm,
            'context_snapshot': context,
        }
        if warning:
            result['warning'] = warning

        if persist:
            result['saved_ids'] = await self._save_to_topic_library(topics)
        return result

    # ------------------------------------------------------------------ context 工具

    @staticmethod
    def _context_events(context: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """从上下文里取出**冻结顺序**的事件列表（缺 context → 空列表）。"""
        if not isinstance(context, dict):
            return []
        raw = context.get('events')
        if not isinstance(raw, (list, tuple)):
            return []
        return [item for item in raw if isinstance(item, dict) and item.get('event_id')]

    @staticmethod
    def _context_hot_tags(events: List[Dict[str, Any]]) -> List[str]:
        """以事件实体/锚点拼出标签（去重、保序、取前 15）。"""
        tags: List[str] = []
        for event in events:
            for token in list(event.get('anchors') or []) + list(event.get('entities') or []):
                text = str(token).strip()
                if text and text not in tags:
                    tags.append(text)
        return tags[:15]

    @staticmethod
    def _allocate_events(events: List[Dict[str, Any]], count: int) -> List[tuple]:
        """按事件冻结顺序分配题数（数量是**总题数**，总量 <= count）。

        Returns:
            list[tuple]: ``(event_context, index)``，第 ``index`` 题依次排布，每题只绑定一个 primary event。
        """
        if count <= 0 or not events:
            return []
        base, remainder = divmod(int(count), len(events))
        allocation: List[tuple] = []
        for index, event in enumerate(events):
            slots = base + (1 if index < remainder else 0)
            for _ in range(slots):
                allocation.append((event, index))
        return allocation[:int(count)]

    def _llm_topics_valid(self, drafts: Any, allocation: List[tuple]) -> bool:
        """校验模型草稿：条数足够、标题非空、引用来自 context、无未声明能力词。"""
        if not isinstance(drafts, (list, tuple)) or len(drafts) < len(allocation):
            return False
        for draft, (event, _index) in zip(drafts, allocation):
            if not isinstance(draft, dict):
                return False
            if not str(draft.get('title') or '').strip():
                return False
            allowed = self._allowed_fact_ids(event)
            refs = draft.get('evidence_refs') or []
            if not isinstance(refs, (list, tuple)):
                return False
            if any(str(ref) not in allowed for ref in refs):
                return False
            if _title_uses_undeclared_ability(draft.get('title'), event.get('available_assets')):
                return False
        return True

    @staticmethod
    def _allowed_fact_ids(event: Dict[str, Any]) -> set:
        """某事件上下文里**允许引用**的事实 ID 全集。"""
        allowed = set()
        for fact in event.get('verified_facts') or []:
            if isinstance(fact, dict) and fact.get('id'):
                allowed.add(str(fact['id']))
        for anchor in event.get('anchors') or []:
            allowed.add(str(anchor))
        return allowed

    def _bind_event_topics(self,
                           drafts: Optional[List[Dict[str, Any]]],
                           allocation: List[tuple],
                           context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """把草稿（或规则模板）绑定到具体事件，产出可入库的选题。

        每题只绑定一个 primary event；其它关联事件放 ``related_event_ids``。
        """
        event_ids = [event['event_id'] for event in self._context_events(context)]
        topics: List[Dict[str, Any]] = []
        for position, (event, _index) in enumerate(allocation):
            if drafts is not None:
                draft = drafts[position]
                title = draft.get('title')
                description = draft.get('description', '')
                reason = draft.get('reason', '')
                angle = draft.get('angle', '')
                content_format = draft.get('format', '')
                outline = draft.get('outline', '')
                difficulty = draft.get('difficulty', 'medium')
                evidence_refs = list(draft.get('evidence_refs') or [])
                requires_verification = list(draft.get('requires_verification') or [])
            else:
                rule = self._event_rule_draft(event)
                title = rule['title']
                description = rule['description']
                reason = rule['reason']
                angle = rule['angle']
                content_format = rule['format']
                outline = rule['outline']
                difficulty = rule['difficulty']
                evidence_refs = []
                requires_verification = rule['requires_verification']

            anchors = [str(token) for token in (event.get('anchors') or [])]
            bvid_refs = [str(bvid) for bvid in (event.get('bvid_refs') or [])]
            entity = event.get('event_id')
            related_event_ids = [eid for eid in event_ids if eid != entity]

            # 规则结果也要校验：空标题或使用未声明能力词 → 兜底为安全标题（模型结果已在校验阶段过滤）
            if not str(title or '').strip() or _title_uses_undeclared_ability(title, event.get('available_assets')):
                fallback_anchor = anchors[0] if anchors else str(entity)
                title = f'选题草案：{fallback_anchor} 的相关信息梳理'

            topic: Dict[str, Any] = {
                'title': title,
                'description': description,
                'keywords': anchors or [entity],
                'reason': reason,
                'difficulty': difficulty,
                'zone_name': context.get('zone_name', ''),
                'direction': context.get('direction', ''),
                'related_tags': anchors,
                'generated_at': datetime.now(),
                'status': 'pending',
                # ---- 服务器写入的上下文（模型不可覆盖） ----
                'context_schema_version': context.get('context_schema_version', CONTEXT_SCHEMA_VERSION),
                'hot_event_id': entity,
                'hot_event_assessment_ids': list(event.get('assessment_ids') or []),
                'opportunity_run_id': context.get('opportunity_run_id'),
                'creator_brief_version': event.get('creator_brief_version') or context.get('creator_brief_version'),
                'angle': angle,
                'format': content_format,
                'outline': outline,
                'evidence_refs': evidence_refs,
                'requires_verification': requires_verification,
                'limitations': list(event.get('limitations') or []),
                'action': event.get('opportunity_action'),
                'phase': event.get('phase'),
                'primary_event_id': entity,
                'related_event_ids': related_event_ids,
                'research_only': bool(event.get('research_only')),
                'generation_mode': GENERATION_MODE_EVENT,
                'used_llm': drafts is not None,
            }
            if bvid_refs:
                topic['related_videos'] = [
                    {'bvid': bvid, 'source_url': f'https://www.bilibili.com/video/{bvid}'}
                    for bvid in bvid_refs
                ]
            topics.append(topic)
        return topics

    def _fallback_event_topics(self,
                               direction: str,
                               zone_name: str,
                               count: int,
                               context: Dict[str, Any],
                               events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """事件模式规则回退（供 ``generate_topics_fallback(..., context)`` 调用）。"""
        allocation = self._allocate_events(events, count)
        return self._bind_event_topics(None, allocation, context)

    @staticmethod
    def _event_rule_draft(event: Dict[str, Any]) -> Dict[str, Any]:
        """按事件动作产出**四类规则模板**草稿（无可执行动作 → research_only 草案）。"""
        action = event.get('opportunity_action')
        research_only = bool(event.get('research_only'))
        anchors = [str(token) for token in (event.get('anchors') or [])]
        anchor = anchors[0] if anchors else str(event.get('event_id'))

        if research_only or action in (None, 'not_suitable', 'deadline_missed'):
            return {
                'title': f'研究草案：{anchor} 的可用证据与信息缺口',
                'description': f'围绕 {anchor} 梳理公开证据，明确还缺哪些可执行依据；仅作研究草案。',
                'reason': '该事件当前没有可执行动作，只生成明确标记 research_only 的研究草案，不给制作优先级建议。',
                'angle': '研究梳理',
                'format': 'research_note',
                'outline': ['已有证据清单', '信息缺口', '下一步观察条件'],
                'difficulty': 'easy',
                'requires_verification': ['补充可执行证据后再评估制作'],
            }
        if action == 'prepare_or_pilot':
            return {
                'title': f'低成本验证：{anchor} 的公开信息核对',
                'description': f'以低投入核对 {anchor} 的关键事实，先验证再决定是否加大投入。',
                'reason': '早期信号阶段，先出验证型/资料整理型低成本选题。',
                'angle': '资料整理',
                'format': 'verify_note',
                'outline': ['公开资料要点', '待核实项', '结论与建议'],
                'difficulty': 'easy',
                'requires_verification': ['核对引用来源与更新时间'],
            }
        if action == 'make_candidate':
            return {
                'title': f'解读拆解：{anchor} 的关键看点',
                'description': f'结合已确认事实，拆解 {anchor} 的关键看点与脉络。',
                'reason': '日级趋势已确认且适配，出教程/解读型选题（以用户真实能力为准）。',
                'angle': '解读',
                'format': 'explainer',
                'outline': ['背景事实', '关键看点', '观点与延伸'],
                'difficulty': 'medium',
                'requires_verification': ['复核事实与来源链接'],
            }
        if action == 'differentiate_research':
            return {
                'title': f'差异化解读：{anchor} 的独特角度',
                'description': f'供给密集时，围绕 {anchor} 找一个差异化比较/答疑角度。',
                'reason': '角度密集，走差异化比较/答疑/细分问题。',
                'angle': '差异化比较',
                'format': 'comparison',
                'outline': ['对比维度', '相同点与差异点', '更适合谁'],
                'difficulty': 'medium',
                'requires_verification': ['补齐对比对象证据'],
            }
        # watch_and_collect 等：下降但有更新信息 → 复盘/总结 + 时效提醒
        return {
            'title': f'复盘梳理：{anchor} 的更新动态与时效提醒',
            'description': f'梳理 {anchor} 的传播脉络与最新更新，并提示时效风险。',
            'reason': '证据仍在收集，先做复盘/总结型内容，并提醒及时性风险。',
            'angle': '复盘',
            'format': 'retrospective',
            'outline': ['时间线', '更新动态', '时效提醒'],
            'difficulty': 'easy',
            'requires_verification': ['持续观察后续更新'],
        }

    # ------------------------------------------------------------------ 提示词 / 解析

    def _build_generation_prompt(self, direction: str, zone_name: str, hot_tags: List[str], count: int,
                                 recommendation_context: Optional[Dict[str, Any]] = None) -> str:
        """构建LLM生成选题的提示词

        Args:
            direction: 创作方向
            zone_name: 分区名称
            hot_tags: 热门tag列表
            count: 生成数量
            recommendation_context: 服务器冻结的推荐上下文（事件模式，只追加）

        Returns:
            提示词
        """
        events = self._context_events(recommendation_context)
        if events:
            return self._build_context_prompt(direction, zone_name, hot_tags, count, recommendation_context, events)

        hot_tags_str = '、'.join(hot_tags[:10])
        
        prompt = f"""请基于B站【{zone_name}】分区的当前热点，为【{direction}】方向生成 {count} 个创意选题。

当前热门Tag：{hot_tags_str}

要求：
1. 选题要结合热门tag，但要有创意性，不能生硬堆砌关键词
2. 标题控制在15-30字，吸引眼球但不标题党
3. 每个选题要说明创意点和为什么能火
4. 评估制作难度（easy/medium/hard）
5. 给出相关的关键词标签

请以JSON数组格式返回，每个选题包含：
{{
  "title": "选题标题",
  "description": "详细描述（100字内）",
  "keywords": ["关键词1", "关键词2"],
  "reason": "为什么这个选题能火（50字内）",
  "difficulty": "easy/medium/hard",
  "related_tags": ["相关热门tag"]
}}

直接返回JSON数组，不要其他说明文字。"""
        
        return prompt

    def _build_context_prompt(self,
                              direction: str,
                              zone_name: str,
                              hot_tags: List[str],
                              count: int,
                              context: Dict[str, Any],
                              events: List[Dict[str, Any]]) -> str:
        """事件模式提示词：把冻结证据 ID 交给模型，只让它写文字。"""
        lines = [
            f"请基于以下**已冻结**的话题事件证据，为【{direction}】方向产出 {count} 个选题（数量为总题数）。",
            f"分区：{zone_name}；可用锚点：{'、'.join(hot_tags[:10]) or '无'}。",
            "",
            "事件证据（按给定顺序一一对应，不得混用事件）：",
        ]
        for index, event in enumerate(events):
            facts = [fact for fact in (event.get('verified_facts') or []) if isinstance(fact, dict)]
            fact_desc = '；'.join(
                f"{fact.get('id')}={fact.get('value')}" for fact in facts[:12] if fact.get('id') is not None
            ) or '（无）'
            lines.append(
                f"- 事件{index + 1} event_id={event.get('event_id')}，动作={event.get('opportunity_action')}，"
                f"事实ID可用集合=[{fact_desc}]"
            )
        lines.extend([
            "",
            "硬性要求：",
            "1. 只允许输出字段 title/description/angle/format/outline/reason/evidence_refs/requires_verification；",
            "2. evidence_refs 必须是**上面列出的事实ID**之一，未知引用会被拒绝；",
            "3. 不得编造数字、来源链接、品牌事实、发布日期、收益或热度剩余寿命；",
            "4. 不得使用用户未声明的能力词（如'亲测''采访''独家'）；",
            "5. 不得改写阶段/指标/动作/优先级/可靠截止。",
            "",
            "请以JSON数组返回，数组长度等于上面的事件数量（一题一事件）：",
            "[{",
            '  "title": "选题标题",',
            '  "description": "详细描述",',
            '  "angle": "切入角度",',
            '  "format": "内容形式",',
            '  "outline": ["要点1", "要点2"],',
            '  "reason": "推荐理由",',
            '  "evidence_refs": ["事实ID"],',
            '  "requires_verification": ["待核实项"]',
            "}]",
            "直接返回JSON数组，不要其他说明文字。",
        ])
        return "\n".join(lines)

    def _parse_llm_response(self, content: str) -> List[Dict[str, Any]]:
        """解析LLM返回的JSON内容
        
        先尝试直接解析，失败则用正则提取 JSON 数组。
        
        Args:
            content: LLM返回的文本
            
        Returns:
            解析后的选题列表
        """
        try:
            # 尝试直接解析JSON
            if content.strip().startswith('['):
                return json.loads(content)
            
            # 尝试提取JSON数组
            # LLM 可能在 JSON 前后加说明文字
            import re
            # 搜索内容
            json_match = re.search(r'\[[\s\S]*\]', content)
            # 判断 json_match
            if json_match:
                return json.loads(json_match.group())
            
            logger.warning("LLM返回内容不是有效的JSON格式")
            return []
            
        except json.JSONDecodeError as e:
            logger.error(f"解析LLM返回的JSON失败: {e}\n内容: {content[:200]}")
            return []
    
    # ------------------------------------------------------------------ 入库内核 / 旧 wrapper

    def _insert_topics(self, session, topics: List[Dict[str, Any]]) -> List[int]:
        """**flush-only 内核**：先全部校验，再逐题 add/flush 返回 ID。

        **不 commit / 不 rollback / 不 close / 不发网络**（事务由调用方掌控）。

        Args:
            session: 调用方持有的 SQLAlchemy 会话。
            topics: 选题列表。

        Returns:
            List[int]: 新建 Topic 的主键列表。

        Raises:
            ValueError: 非序列 / 非法选题 / 缺标题（**先于任何写入**抛出）。
        """
        if not isinstance(topics, (list, tuple)):
            raise ValueError('topics_must_be_sequence')

        prepared: List[Dict[str, Any]] = []
        for index, topic_data in enumerate(topics):
            if not isinstance(topic_data, dict):
                raise ValueError(f'invalid_topic_payload:{index}')
            title = topic_data.get('title')
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f'topic_title_required:{index}')
            prepared.append(topic_data)

        saved_ids: List[int] = []
        for topic_data in prepared:
            keywords = topic_data.get('keywords', []) or []
            topic = Topic(
                title=topic_data['title'],
                description=topic_data.get('description', ''),
                tags=keywords,  # 保存到 tags 字段
                category=topic_data.get('zone_name', ''),
                source='llm_generated',
                status='pending',
                ai_suggestions=_build_ai_suggestions(topic_data),
            )
            # Topic.hotspot_id 仍指旧 hotspots 表，**不写 HotEvent ID**（避免外键错误）
            related_videos = topic_data.get('related_videos')
            if related_videos:
                topic.related_videos = related_videos
            session.add(topic)
            session.flush()  # 获取ID
            saved_ids.append(topic.id)
        return saved_ids

    async def _save_to_topic_library(self, topics: List[Dict[str, Any]]) -> List[int]:
        """保存选题到选题库（旧 async wrapper，仅服务旧 tag_only 无幂等键路径）。

        自持**短事务**：上下文管理 session + 单事务；先全部校验再 flush；
        失败**全部回滚并向上抛出**（不 catch 后 `return []`，避免外层误称 success）。

        Args:
            topics: 选题列表。

        Returns:
            List[int]: 保存的选题ID列表。
        """
        session = None
        try:
            session = get_session()
            saved_ids = self._insert_topics(session, topics)
            session.commit()
            logger.info(f"选题已保存到选题库，共 {len(saved_ids)} 个")
            return saved_ids
        except Exception as e:
            logger.error(f"保存选题到数据库失败: {e}")
            if session is not None:
                # 修 §11.6 缺陷三：session 未赋值时不再 rollback/close 覆盖原错误
                session.rollback()
            raise
        finally:
            if session is not None:
                session.close()
    
    async def get_topic_library(self, 
                                # 赋值并准备后续使用
                                zone_name: Optional[str] = None,
                                # 赋值并准备后续使用
                                status: Optional[str] = None,
                                # 赋值并准备后续使用
                                limit: int = 50) -> List[Dict[str, Any]]:
        """查询选题库
        
        Args:
            zone_name: 分区名称（可选）
            status: 状态过滤（pending/adopted/published）
            limit: 返回数量
            
        Returns:
            选题列表
        """
        session = None
        try:
            session = get_session()
            
            # 构建查询
            query = session.query(Topic)
            
            # 按分区过滤
            if zone_name:
                # 按条件过滤查询
                query = query.filter_by(category=zone_name)
            
            # 按状态过滤
            if status:
                # 按条件过滤查询
                query = query.filter_by(status=status)
            
            # 按创建时间倒序
            topics = query.order_by(Topic.created_at.desc()).limit(limit).all()
            
            # 序列化结果（含可选的生成上下文，老记录缺字段不算 invalid）
            result = []
            for topic in topics:
                # 从 ai_suggestions JSON 中提取额外字段
                ai_suggestions = topic.ai_suggestions or {}
                result.append({
                    'id': topic.id,
                    'title': topic.title,
                    'description': topic.description,
                    'tags': topic.tags or [],
                    'category': topic.category,
                    'direction': ai_suggestions.get('direction', ''),
                    'difficulty': ai_suggestions.get('difficulty', ''),
                    'keywords': ai_suggestions.get('keywords', []),
                    'status': topic.status,
                    'ai_suggestions': ai_suggestions,
                    'created_at': topic.created_at.isoformat() if topic.created_at else None,
                    # ---- 04 生成上下文回读（可选字段） ----
                    'generation_mode': ai_suggestions.get('generation_mode'),
                    'hot_event_id': ai_suggestions.get('hot_event_id'),
                    'opportunity_run_id': ai_suggestions.get('opportunity_run_id'),
                    'generation_request_id': ai_suggestions.get('generation_request_id'),
                    'angle': ai_suggestions.get('angle'),
                    'format': ai_suggestions.get('format'),
                    'outline': ai_suggestions.get('outline'),
                    'evidence_refs': ai_suggestions.get('evidence_refs', []),
                    'limitations': ai_suggestions.get('limitations', []),
                    'action': ai_suggestions.get('action'),
                    'research_only': ai_suggestions.get('research_only'),
                    'related_videos': topic.related_videos or [],
                    'context': {key: ai_suggestions.get(key) for key in AI_SUGGESTION_CONTEXT_KEYS if key in ai_suggestions},
                })
            
            return result
            
        except Exception as e:
            logger.error(f"查询选题库失败: {e}")
            raise
        finally:
            if session is not None:
                session.close()
    
    async def update_topic_status(self, topic_id: int, status: str) -> bool:
        """更新选题状态
        
        Args:
            topic_id: 选题ID
            status: 新状态（pending/adopted/published）
            
        Returns:
            是否成功
        """
        session = None
        try:
            session = get_session()
            
            # 查找选题
            topic = session.query(Topic).filter_by(id=topic_id).first()
            # 空值/异常保护：不满足条件时跳过
            if not topic:
                logger.warning(f"选题 {topic_id} 不存在")
                return False
            
            # 更新状态
            topic.status = status
            topic.updated_at = datetime.now()
            
            # 提交事务
            session.commit()
            logger.info(f"选题 {topic_id} 状态已更新为 {status}")
            return True
            
        except Exception as e:
            logger.error(f"更新选题状态失败: {e}")
            if session is not None:
                # 修 §11.6 缺陷三：session 未赋值时不再 rollback/close 覆盖原错误
                session.rollback()
            return False
        finally:
            if session is not None:
                session.close()


def _build_ai_suggestions(topic_data: Dict[str, Any]) -> Dict[str, Any]:
    """把选题草稿组装成 ``Topic.ai_suggestions``（保留旧字段 + 追加可选上下文）。

    Args:
        topic_data: 选题草稿。

    Returns:
        dict: 可直接写入 JSON 列的字典。
    """
    keywords = topic_data.get('keywords', []) or []
    suggestions: Dict[str, Any] = {
        'reason': topic_data.get('reason', ''),
        'direction': topic_data.get('direction', ''),
        'difficulty': topic_data.get('difficulty', 'medium'),
        'related_tags': topic_data.get('related_tags', []) or [],
        'keywords': keywords,  # 同时保存到 ai_suggestions
        'generated_at': _to_iso(topic_data.get('generated_at')),
    }
    for key in AI_SUGGESTION_CONTEXT_KEYS:
        if key in topic_data and topic_data[key] is not None:
            suggestions[key] = _jsonable(topic_data[key])
    for key in ('phase', 'requires_verification', 'research_only', 'generation_mode'):
        if key in topic_data and topic_data[key] is not None:
            suggestions[key] = _jsonable(topic_data[key])
    return suggestions


# ============ 使用示例 ============

async def demo_generate_topics():
    """演示：生成AI选题"""
    # 初始化
    from bilibili.cookie_pool import get_cookie_pool
    
    cookie_pool = get_cookie_pool()
    # 赋值并准备后续使用
    api = BilibiliAPI(cookie_pool=cookie_pool)
    
    # 初始化LLM（如果配置了）
    try:
        llm_client = LLMClient()
        generator = TopicGenerator(api, llm_client)
    # 异常处理
    except Exception:
        logger.warning("LLM未配置，将使用降级方案")
        generator = TopicGenerator(api, None)
    
    # 生成选题
    result = await generator.generate_topics(
        direction='游戏解说',
        zone_name='游戏',
        count=5,
        use_llm=True
    )
    
    print(f"生成结果: {'使用LLM' if result.get('used_llm') else '降级方案'}")
    # 输出信息到控制台
    print(f"热门Tag: {', '.join(result['hot_tags'][:5])}")
    # 输出信息到控制台
    print(f"\n生成的选题:")
    # 循环遍历处理
    for i, topic in enumerate(result['topics'], 1):
        # 输出信息到控制台
        print(f"\n{i}. {topic['title']}")
        # 输出信息到控制台
        print(f"   描述: {topic['description']}")
        # 输出信息到控制台
        print(f"   难度: {topic['difficulty']} | 关键词: {', '.join(topic['keywords'])}")


# 边界/有效性检查
if __name__ == '__main__':
    # 运行任务
    asyncio.run(demo_generate_topics())
