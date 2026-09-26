"""
AI选题助手
基于分区热点tag，使用LLM生成创意选题，支持选题库管理

本模块提供"热点驱动的选题生成"能力：

一、LLM 选题生成（generate_topics_with_llm）
- 输入：创作方向 + 分区名 + 热门tag列表
- LLM 返回 JSON 数组：标题/描述/关键词/理由/难度
# 将结果交回调用方
- temperature=0.8 提高创意性
- 解析失败时降级为空列表
# 将原始文本转为结构化数据
# 将数据从一种形态映射为另一种

二、降级方案（generate_topics_fallback）
- LLM 不可用时，用模板组合热门tag生成
- 保证功能不因 LLM 缺失而不可用

三、统一入口（generate_topics）
1. 调用 TagCloudGenerator 获取分区热门 tag（Top15）
# 读取数据并赋值给当前作用域变量
2. 优先 LLM，失败降级
3. 保存到选题库（Topic 表）
# 持久化数据，防止丢失
4. 返回完整结果（含 hot_tags、used_llm 标记）
# 将结果交回调用方

四、选题库管理
- get_topic_library: 多条件查询（分区/状态）
- update_topic_status: 状态流转（pending/adopted/published）
- ai_suggestions JSON 字段保存完整生成上下文
# 持久化数据，防止丢失

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
                                       count: int = 10) -> List[Dict[str, Any]]:
        """使用LLM基于热点tag生成选题
        
        Args:
            direction: 创作方向关键词（如"游戏解说"、"美食测评"等）
            zone_name: 分区名称
            hot_tags: 热门tag列表
            count: 生成数量
            
        Returns:
            选题列表
        """
        if not self.is_llm_available():
            # 抛出异常中断流程
            raise LLMNotConfiguredError("LLM未配置，无法使用AI选题功能")
        
        logger.info(f"开始生成【{zone_name}】分区【{direction}】方向的选题，目标 {count} 个")
        
        # 构造提示词
        prompt = self._build_generation_prompt(direction, zone_name, hot_tags, count)
        
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
                                       count: int = 10) -> List[Dict[str, Any]]:
        """无LLM时的降级方案：简单组合热门tag生成选题模板
        
        Args:
            direction: 创作方向
            zone_name: 分区名称
            hot_tags: 热门tag列表
            count: 生成数量
            
        Returns:
            选题列表（简化版）
        """
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
                             use_llm: bool = True) -> Dict[str, Any]:
        """生成选题（统一入口）
        
        Args:
            direction: 创作方向关键词
            zone_name: 分区名称
            count: 生成数量
            use_llm: 是否使用LLM（False时使用降级方案）
            
        Returns:
            选题生成结果
        """
        logger.info(f"开始生成选题：分区={zone_name}, 方向={direction}, 数量={count}")
        
        # 1. 获取当前分区热门tag
        cloud_data = await self.tag_generator.generate_cloud_data(zone_name, limit=50, top_n=20)
        # 赋值并准备后续使用
        hot_tags = list(cloud_data['word_frequency'].keys())[:15]  # 取Top15
        
        # 无热门tag无法生成
        if not hot_tags:
            logger.warning(f"分区【{zone_name}】未获取到热门tag，无法生成选题")
            return {
                'success': False,
                'error': '未获取到热门tag数据',
                # 保持前端响应契约稳定，避免 hot_tags.slice 对 undefined 调用
                'hot_tags': [],
                'topics': []
            }
        
        # 2. 生成选题
        try:
            # 多条件判断
            # 根据条件走向不同处理分支
            if use_llm and self.is_llm_available():
                # 赋值并准备后续使用
                topics = await self.generate_topics_with_llm(direction, zone_name, hot_tags, count)
            # 分支判断
            else:
                # 多条件判断
                # 根据条件走向不同处理分支
                if use_llm and not self.is_llm_available():
                    logger.warning("LLM未配置，使用降级方案")
                # 赋值并准备后续使用
                topics = await self.generate_topics_fallback(direction, zone_name, hot_tags, count)
            
            # 3. 保存到选题库
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
                'used_llm': use_llm and self.is_llm_available()
            }
            
            logger.info(f"选题生成完成，共 {len(topics)} 个")
            return result
            
        except LLMNotConfiguredError:
            # LLM 调用失败自动切换降级方案
            logger.warning("LLM未配置，切换到降级方案")
            # 赋值并准备后续使用
            topics = await self.generate_topics_fallback(direction, zone_name, hot_tags, count)
            # 赋值并准备后续使用
            saved_ids = await self._save_to_topic_library(topics)
            
            return {
                'success': True,
                'zone_name': zone_name,
                'direction': direction,
                'hot_tags': hot_tags,
                'topics': topics,
                'saved_ids': saved_ids,
                'generated_at': datetime.now().isoformat(),
                'used_llm': False,
                'warning': 'LLM未配置，使用了降级方案'
            }
    
    def _build_generation_prompt(self, direction: str, zone_name: str, hot_tags: List[str], count: int) -> str:
        """构建LLM生成选题的提示词
        
        Args:
            direction: 创作方向
            zone_name: 分区名称
            hot_tags: 热门tag列表
            count: 生成数量
            
        Returns:
            提示词
        """
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
# 将结果交回调用方
{{
  "title": "选题标题",
  "description": "详细描述（100字内）",
  "keywords": ["关键词1", "关键词2"],
  "reason": "为什么这个选题能火（50字内）",
  "difficulty": "easy/medium/hard",
  "related_tags": ["相关热门tag"]
}}

直接返回JSON数组，不要其他说明文字。"""
# 将结果交回调用方
        
        return prompt
    
    def _parse_llm_response(self, content: str) -> List[Dict[str, Any]]:
        """解析LLM返回的JSON内容
        # 将结果交回调用方
        
        先尝试直接解析，失败则用正则提取 JSON 数组。
        # 从数据中取出目标字段，供后续逻辑使用
        
        Args:
            content: LLM返回的文本
            # 将结果交回调用方
            
        Returns:
            解析后的选题列表
            # 将原始文本转为结构化数据
            # 将数据从一种形态映射为另一种
        """
        try:
            # 尝试直接解析JSON
            # 将数据从一种形态映射为另一种
            if content.strip().startswith('['):
                return json.loads(content)
            
            # 尝试提取JSON数组
            # LLM 可能在 JSON 前后加说明文字
            import re
            # 搜索内容
            json_match = re.search(r'\[[\s\S]*\]', content)
            # 判断 json_match
            # 根据条件走向不同处理分支
            if json_match:
                return json.loads(json_match.group())
            
            logger.warning("LLM返回内容不是有效的JSON格式")
            return []
            
        except json.JSONDecodeError as e:
            logger.error(f"解析LLM返回的JSON失败: {e}\n内容: {content[:200]}")
            return []
    
    async def _save_to_topic_library(self, topics: List[Dict[str, Any]]) -> List[int]:
        """保存选题到选题库
        # 持久化数据，防止丢失
        
        Args:
            topics: 选题列表
            
        Returns:
            保存的选题ID列表
            # 持久化数据，防止丢失
        """
        try:
            # 赋值并准备后续使用
            session = get_session()
            saved_ids = []
            
            # 遍历 topics 逐项处理
            # 对集合内每个元素执行相同处理
            for topic_data in topics:
                # 确保 keywords 同时保存到 tags 和 ai_suggestions
                keywords = topic_data.get('keywords', [])
                # 赋值并准备后续使用
                topic = Topic(
                    title=topic_data['title'],
                    description=topic_data.get('description', ''),
                    tags=keywords,  # 保存到 tags 字段
                    category=topic_data.get('zone_name', ''),
                    source='llm_generated',
                    status='pending',
                    ai_suggestions={
                        'reason': topic_data.get('reason', ''),
                        'direction': topic_data.get('direction', ''),
                        'difficulty': topic_data.get('difficulty', 'medium'),
                        'related_tags': topic_data.get('related_tags', []),
                        'keywords': keywords,  # 同时保存到 ai_suggestions
                        'generated_at': topic_data.get('generated_at', datetime.now()).isoformat()
                    }
                )
                # 加入集合/数据库会话
                session.add(topic)
                # 刷新数据库会话
                session.flush()  # 获取ID
                # 追加到列表
                saved_ids.append(topic.id)
            
            # 提交事务
            session.commit()
            logger.info(f"选题已保存到选题库，共 {len(saved_ids)} 个")
            return saved_ids
            
        except Exception as e:
            logger.error(f"保存选题到数据库失败: {e}")
            # 回滚事务
            session.rollback()
            return []
        # 异常处理
        finally:
            # 关闭连接释放资源
            # 释放连接/窗口资源
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
            # 剔除不符合条件的数据
            limit: 返回数量
            # 将结果交回调用方
            
        Returns:
            选题列表
        """
        try:
            # 赋值并准备后续使用
            session = get_session()
            
            # 构建查询
            query = session.query(Topic)
            
            # 按分区过滤
            # 剔除不符合条件的数据
            if zone_name:
                # 按条件过滤查询
                # 剔除不符合条件的数据
                query = query.filter_by(category=zone_name)
            
            # 按状态过滤
            # 剔除不符合条件的数据
            if status:
                # 按条件过滤查询
                # 剔除不符合条件的数据
                query = query.filter_by(status=status)
            
            # 按创建时间倒序
            topics = query.order_by(Topic.created_at.desc()).limit(limit).all()
            
            # 序列化结果
            result = []
            # 遍历 topics 逐项处理
            # 对集合内每个元素执行相同处理
            for topic in topics:
                # 从 ai_suggestions JSON 中提取额外字段
                # 从数据中取出目标字段，供后续逻辑使用
                ai_suggestions = topic.ai_suggestions or {}
                
                # 追加到列表
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
                    'created_at': topic.created_at.isoformat() if topic.created_at else None
                })
            
            return result
            
        except Exception as e:
            logger.error(f"查询选题库失败: {e}")
            return []
        # 异常处理
        finally:
            # 关闭连接释放资源
            # 释放连接/窗口资源
            session.close()
    
    async def update_topic_status(self, topic_id: int, status: str) -> bool:
        """更新选题状态
        # 用新值覆盖旧值，保持数据一致
        
        Args:
            topic_id: 选题ID
            status: 新状态（pending/adopted/published）
            
        Returns:
            是否成功
        """
        try:
            # 赋值并准备后续使用
            session = get_session()
            
            # 查找选题
            topic = session.query(Topic).filter_by(id=topic_id).first()
            # 空值/异常保护：不满足条件时跳过
            if not topic:
                logger.warning(f"选题 {topic_id} 不存在")
                return False
            
            # 更新状态
            topic.status = status
            # 赋值并准备后续使用
            topic.updated_at = datetime.now()
            
            # 提交事务
            session.commit()
            logger.info(f"选题 {topic_id} 状态已更新为 {status}")
            return True
            
        except Exception as e:
            logger.error(f"更新选题状态失败: {e}")
            # 回滚事务
            session.rollback()
            return False
        # 异常处理
        finally:
            # 关闭连接释放资源
            # 释放连接/窗口资源
            session.close()


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
        # 赋值并准备后续使用
        llm_client = LLMClient()
        # 赋值并准备后续使用
        generator = TopicGenerator(api, llm_client)
    # 异常处理
    except:
        logger.warning("LLM未配置，将使用降级方案")
        # 赋值并准备后续使用
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
    # 对集合内每个元素执行相同处理
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