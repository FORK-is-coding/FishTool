"""
情感分析模块
默认使用词典规则，可选LLM增强，实现松耦合配置

本模块提供评论情感分析能力，采用"词典规则优先、LLM可选增强"的设计：

一、词典规则分析（默认，零成本）
- 内置情感词典：正面词、负面词、风险词
- 多级优先级判定：
  1. 命中风险词（翻车/抄袭/举报等）→ risk（置信度0.9）
  2. 正面词多于负面词 → positive
  3. 负面词多于正面词 → negative
  4. 其余 → neutral
- 置信度计算：基础0.5 + 每命中一个词加0.1，上限0.95
# 对输入做运算得到结果
- 返回完整匹配关键词列表，便于审计与调参
# 将结果交回调用方

二、LLM增强分析（可选，产生token成本）
- analyze_with_llm_summary: 词典规则全量分析 + LLM分批总结
- 分批控制 token 成本（batch_size 参数）
- LLM 输出结构化 JSON（key_points/positive_highlights/
  negative_concerns/unusual_signals）
- 多批次结果自动合并去重
- LLM 不可用时降级回词典规则，不阻塞主流程

三、关键词提取
# 从数据中取出目标字段，供后续逻辑使用
- extract_keywords: 简单分词统计高频词（2-4字中文）
- 过滤停用词，返回 [(词, 频次)] 排序列表
# 剔除不符合条件的数据

设计原则：
- LLM 通过构造函数注入，松耦合（不强制依赖）
- 所有分析结果带置信度与匹配依据，可解释
- 批量分析输出情感分布与各情感占比

依赖：
- llm.client: LLMClient（可选）
- core.exceptions: LLMNotConfiguredError
"""
import asyncio
# 从 typing 导入符号
from typing import List, Dict, Any, Optional
# 从 datetime 导入符号
from datetime import datetime
# 从 collections 导入符号
from collections import Counter
# 导入模块
import re
# 导入模块
import logging

# 从 llm.client 导入符号
from llm.client import LLMClient
# 从 core.exceptions 导入符号
from core.exceptions import LLMNotConfiguredError
# 从 core.logger 导入符号
from core.logger import get_logger

logger = get_logger(__name__)


class SentimentAnalyzer:
    """情感分析器 - 词典规则优先，LLM可选
    
    核心方法：
    - analyze_comment: 单条评论情感判定
    - analyze_batch: 批量分析 + 情感分布统计
    - analyze_with_llm_summary: LLM增强分析（词典+总结）
    - extract_keywords: 高频关键词提取
    # 从数据中取出目标字段，供后续逻辑使用
    """
    
    # 情感词典
    # 正面关键词：用于判断积极情感
    # 根据条件走向不同处理分支
    POSITIVE_KEYWORDS = [
        '好', '棒', '赞', '优秀', '厉害', '牛', '强', '优质', '精彩', '喜欢',
        '爱了', '支持', '加油', '期待', '感动', '温馨', '舒服', '完美', '经典',
        '神', '顶', '绝', '妙', '美', '帅', '可爱', '有趣', '好看', '好听'
    ]
    
    # 负面关键词：用于判断消极情感
    # 根据条件走向不同处理分支
    NEGATIVE_KEYWORDS = [
        '差', '烂', '垃圾', '无聊', '难看', '难听', '恶心', '讨厌', '失望', '弃',
        '黑', '喷', '骂', '吐槽', '批评', '反对', '抵制', '举报', '翻车', '凉了',
        '抄袭', '洗稿', '营销', '炒作', '恰饭', '割韭菜', '不行', '拉跨', '寄了'
    ]
    
    # 风险关键词：命中即标记风险（优先级最高）
    RISK_KEYWORDS = [
        '翻车', '抄袭', '洗稿', '侵权', '盗用', '举报', '下架', '道歉',
        '争议', '舆论', '爆料', '实锤', '打假'
    ]
    
    def __init__(self, llm_client: Optional[LLMClient] = None, use_llm_summary: bool = False):
        """初始化情感分析器
        # 设置初始值/默认状态，避免后续空引用
        
        Args:
            llm_client: LLM客户端（可选）
            use_llm_summary: 是否使用LLM做聚合总结
        """
        # 保存LLM客户端引用（可选）
        self.llm_client = llm_client
        # 判断是否启用LLM总结：需要同时满足用户启用且LLM已配置
        # 根据条件走向不同处理分支
        self.use_llm_summary = use_llm_summary and llm_client is not None
    
    def analyze_comment(self, comment: Dict[str, Any]) -> Dict[str, Any]:
        """分析单条评论的情感倾向
        
        基于词典规则快速判断：
        # 根据条件走向不同处理分支
        - 匹配风险关键词 → risk（高优先级）
        - 正面词 > 负面词 → positive
        - 负面词 > 正面词 → negative
        - 其他 → neutral
        
        Args:
            comment: 评论数据
            
        Returns:
            分析结果，包含情感标签、置信度、匹配关键词
        """
        # 提取评论文本内容
        # 从数据中取出目标字段，供后续逻辑使用
        content = comment.get('content', '')
        
        # 词典规则分析：统计各类关键词出现次数
        # 统计正面关键词命中数（好、棒、赞等）
        positive_count = sum(1 for kw in self.POSITIVE_KEYWORDS if kw in content)
        # 统计负面关键词命中数（差、烂、垃圾等）
        negative_count = sum(1 for kw in self.NEGATIVE_KEYWORDS if kw in content)
        # 统计风险关键词命中数（翻车、抄袭、举报等）
        risk_count = sum(1 for kw in self.RISK_KEYWORDS if kw in content)
        
        # 情感倾向判定（多级优先级规则）
        if risk_count > 0:
            # 规则1：存在风险关键词，优先级最高
            sentiment = 'risk'  # 标记为风险评论
            confidence = 0.9  # 高置信度90%
        # 边界/有效性检查
        elif positive_count > negative_count:
            # 规则2：正面词多于负面词
            sentiment = 'positive'  # 标记为正面情感
            # 置信度计算：基础50% + 每个正面词增加10%，上限95%
            # 对输入做运算得到结果
            confidence = min(0.5 + positive_count * 0.1, 0.95)
        # 边界/有效性检查
        elif negative_count > positive_count:
            # 规则3：负面词多于正面词
            sentiment = 'negative'  # 标记为负面情感
            # 置信度计算：基础50% + 每个负面词增加10%，上限95%
            # 对输入做运算得到结果
            confidence = min(0.5 + negative_count * 0.1, 0.95)
        else:
            # 规则4：正负词数相等或均为0
            sentiment = 'neutral'  # 标记为中性评论
            confidence = 0.5  # 中等置信度50%
        
        # 组装分析结果：情感标签、置信度、得分与匹配关键词
        result = {
            'rpid': comment.get('rpid'),
            'content': content,
            'sentiment': sentiment,
            'confidence': confidence,
            'positive_score': positive_count,
            'negative_score': negative_count,
            'risk_score': risk_count,
            'matched_keywords': {
                'positive': [kw for kw in self.POSITIVE_KEYWORDS if kw in content],
                'negative': [kw for kw in self.NEGATIVE_KEYWORDS if kw in content],
                'risk': [kw for kw in self.RISK_KEYWORDS if kw in content]
            }
        }
        
        return result
    
    def analyze_batch(self, comments: List[Dict[str, Any]]) -> Dict[str, Any]:
        """批量分析评论情感
        
        逐条调用 analyze_comment，统计情感分布和占比。
        返回详细的分析结果，包括每条评论的情感标签。
        # 将结果交回调用方
        
        Args:
            comments: 评论列表
            
        Returns:
            批量分析结果，包含情感分布、占比、详细分析列表
        """
        logger.info(f"开始批量情感分析，共 {len(comments)} 条评论")
        
        # 逐条分析
        analyzed_comments = []
        # 遍历 comments 逐项处理
        # 对集合内每个元素执行相同处理
        for comment in comments:
            analysis = self.analyze_comment(comment)
            # 追加到列表
            analyzed_comments.append(analysis)
        
        # 统计分布
        sentiment_counts = Counter(c['sentiment'] for c in analyzed_comments)
        
        # 组装批量分析结果
        result = {
            'total_count': len(comments),
            'sentiment_distribution': dict(sentiment_counts),
            'positive_ratio': sentiment_counts.get('positive', 0) / len(comments) if comments else 0,
            'negative_ratio': sentiment_counts.get('negative', 0) / len(comments) if comments else 0,
            'neutral_ratio': sentiment_counts.get('neutral', 0) / len(comments) if comments else 0,
            'risk_ratio': sentiment_counts.get('risk', 0) / len(comments) if comments else 0,
            'analyzed_comments': analyzed_comments,
            'analyzed_at': datetime.now().isoformat()
        }
        
        logger.info(f"批量分析完成: 正面{sentiment_counts.get('positive', 0)}, "
                   f"负面{sentiment_counts.get('negative', 0)}, "
                   f"中性{sentiment_counts.get('neutral', 0)}, "
                   f"风险{sentiment_counts.get('risk', 0)}")
        
        return result
    
    async def analyze_with_llm_summary(self, comments: List[Dict[str, Any]], batch_size: int = 100) -> Dict[str, Any]:
        """使用LLM增强：批量分析+聚合总结（成本控制）
        
        两步流程：
        1. 词典规则快速分析全部评论
        2. 分批用 LLM 生成核心观点总结
        
        适合需要深度洞察的场景，注意 token 成本。
        
        Args:
            comments: 评论列表
            batch_size: 每批处理数量（控制token成本）
            # 对数据进行加工/分发
            
        Returns:
            分析结果（包含LLM总结）
        """
        if not self.llm_client:
            # 抛出异常中断流程
            raise LLMNotConfiguredError("LLM未配置")
        
        logger.info(f"使用LLM增强分析，共 {len(comments)} 条评论，批次大小 {batch_size}")
        
        # 先用词典规则分析
        base_result = self.analyze_batch(comments)
        
        # 分批处理，每批用LLM总结
        llm_summaries = []
        # 循环遍历处理
        # 对集合内每个元素执行相同处理
        for i in range(0, len(comments), batch_size):
            batch = comments[i:i + batch_size]
            summary = await self._llm_summarize_batch(batch, base_result)
            # 追加到列表
            llm_summaries.append(summary)
        
        # 合并批次结果
        merged_summary = self._merge_llm_summaries(llm_summaries)
        
        # 增强结果
        base_result['llm_summary'] = merged_summary
        base_result['used_llm'] = True
        
        logger.info("LLM增强分析完成")
        return base_result
    
    async def _llm_summarize_batch(self, batch: List[Dict[str, Any]], base_result: Dict[str, Any]) -> Dict[str, Any]:
        """LLM总结单个批次
        
        调用 LLM API，根据评论内容和基础统计生成核心观点总结。
        返回结构化 JSON，包含 key_points、positive_highlights、negative_concerns 等。
        # 将结果交回调用方
        
        Args:
            batch: 评论批次
            base_result: 基础分析结果
            
        Returns:
            LLM总结字典
        """
        # 构造提示词
        prompt = self._build_summary_prompt(batch, base_result)
        
        # 异常保护：局部失败不影响主流程
        try:
            response = await self.llm_client.chat_completion(
                messages=[
                    {"role": "system", "content": "你是一位专业的评论分析师，擅长快速总结用户反馈的核心观点。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=500
            )
            
            # 正确解析 OpenAI 格式返回：response['choices'][0]['message']['content']
            # 将结果交回调用方
            if 'choices' in response and len(response['choices']) > 0:
                content = response['choices'][0]['message']['content']
            else:
                content = ''
            
            # 解析LLM返回的结构化结果
            # 将结果交回调用方
            summary = self._parse_llm_summary(content)
            return summary
            
        except Exception as e:
            logger.error(f"LLM总结失败: {e}")
            return {'error': str(e)}
    
    def _build_summary_prompt(self, batch: List[Dict[str, Any]], base_result: Dict[str, Any]) -> str:
        """构造LLM总结提示词
        
        将评论批次和基础统计数据打包成结构化提示词，
        要求 LLM 返回 JSON 格式的核心观点总结。
        # 将结果交回调用方
        
        Args:
            batch: 评论批次
            base_result: 基础分析结果
            
        Returns:
            提示词字符串
        """
        # 提取评论文本（限制长度）
        # 每条最多100字，最多取50条，控制token
        comments_text = '\n'.join([
            f"{i+1}. {c['content'][:100]}"
            for i, c in enumerate(batch[:50])  # 最多50条
        ])
        
        prompt = f"""请分析以下用户评论，给出结构化总结：

评论数据：
{comments_text}

基础统计：
- 正面: {base_result.get('positive_ratio', 0):.1%}
- 负面: {base_result.get('negative_ratio', 0):.1%}
- 中性: {base_result.get('neutral_ratio', 0):.1%}

请以JSON格式返回：
# 将结果交回调用方
{{
  "key_points": ["核心观点1", "核心观点2"],
  "positive_highlights": ["正面亮点"],
  "negative_concerns": ["负面问题"],
  "unusual_signals": ["异常信号（如有）"]
}}

只返回JSON，不要其他说明。"""
# 将结果交回调用方
        
        return prompt
    
    def _parse_llm_summary(self, content: str) -> Dict[str, Any]:
        """解析LLM返回的总结
        # 将结果交回调用方
        
        从 LLM 返回文本中提取 JSON 结构。
        # 从数据中取出目标字段，供后续逻辑使用
        如果解析失败，返回原始文本。
        # 将结果交回调用方
        
        Args:
            content: LLM返回内容
            # 将结果交回调用方
            
        Returns:
            解析后的总结字典
            # 将原始文本转为结构化数据
        """
        import json
        # 异常保护：局部失败不影响主流程
        try:
            # 尝试提取JSON
            # LLM 可能在 JSON 前后加说明文字，用正则提取第一个大括号块
            # 从数据中取出目标字段，供后续逻辑使用
            import re
            # 搜索内容
            json_match = re.search(r'\{[\s\S]*\}', content)
            # 判断 json_match
            # 根据条件走向不同处理分支
            if json_match:
                return json.loads(json_match.group())
            return {'raw': content}
        except:
            # 解析失败时保留原文
            return {'raw': content}
    
    def _merge_llm_summaries(self, summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """合并多个批次的LLM总结
        
        将分批次的 LLM 总结合并为一个完整总结，去除重复内容。
        
        Args:
            summaries: 批次总结列表
            
        Returns:
            合并后的总结字典
        """
        merged = {
            'key_points': [],
            'positive_highlights': [],
            'negative_concerns': [],
            'unusual_signals': []
        }
        
        # 遍历各批次总结，跳过错误项
        # 对集合内每个元素执行相同处理
        for summary in summaries:
            # 边界/有效性检查
            if 'error' in summary:
                # 跳过本轮继续循环
                continue
            
            # 按字段合并
            for key in merged.keys():
                # 边界/有效性检查
                if key in summary:
                    # 批量扩展列表
                    merged[key].extend(summary[key])
        
        # 去重
        # 同一观点可能被多个批次提及
        for key in merged.keys():
            merged[key] = list(set(merged[key]))
        
        return merged
    
    def extract_keywords(self, comments: List[Dict[str, Any]], top_n: int = 20) -> List[tuple]:
        """提取高频关键词
        # 从数据中取出目标字段，供后续逻辑使用
        
        简单分词统计，提取评论中出现频率最高的 2-4 字词组。
        # 从数据中取出目标字段，供后续逻辑使用
        过滤停用词，仅保留纯中文词汇。
        # 剔除不符合条件的数据
        
        Args:
            comments: 评论列表
            top_n: 返回前N个
            # 将结果交回调用方
            
        Returns:
            [(关键词, 频次)] 列表
        """
        # 简单分词（按字符）
        # 遍历评论，切出所有2-4字的连续子串
        # 对集合内每个元素执行相同处理
        all_words = []
        # 遍历 comments 逐项处理
        # 对集合内每个元素执行相同处理
        for comment in comments:
            # 读取字典/配置项
            content = comment.get('content', '')
            # 提取2-4字的词
            # 从数据中取出目标字段，供后续逻辑使用
            for length in [2, 3, 4]:
                # 循环遍历处理
                # 对集合内每个元素执行相同处理
                for i in range(len(content) - length + 1):
                    word = content[i:i + length]
                    # 只保留纯中文
                    if not re.match(r'^[\u4e00-\u9fa5]+$', word):
                        # 跳过本轮继续循环
                        continue
                    # 追加到列表
                    all_words.append(word)
        
        # 统计词频
        word_counts = Counter(all_words)
        
        # 过滤停用词
        # 常见虚词无分析价值
        stop_words = {'的', '了', '是', '在', '有', '和', '就', '不', '这', '那', '我', '你', '他'}
        filtered_counts = {
            word: count for word, count in word_counts.items()
            if word not in stop_words
        }
        
        # 返回频率最高的 top_n 个词
        # 将结果交回调用方
        return Counter(filtered_counts).most_common(top_n)


# ============ 使用示例 ============

async def demo_sentiment_analysis():
    """演示：情感分析
    
    完整示例：用模拟数据展示批量情感分析。
    # 将内容呈现到界面上
    """
    
    # 模拟评论
    comments = [
        {'rpid': 1, 'content': '这个视频太好看了，制作很用心，支持UP主！'},
        {'rpid': 2, 'content': '质量真差，浪费时间'},
        {'rpid': 3, 'content': '一般般吧'},
        {'rpid': 4, 'content': '抄袭其他UP主的内容，举报了'},
        {'rpid': 5, 'content': '什么时候更新下一期'},
    ]
    
    # 词典规则分析
    analyzer = SentimentAnalyzer()
    result = analyzer.analyze_batch(comments)
    
    print(f"情感分布: {result['sentiment_distribution']}")
    print(f"正面占比: {result['positive_ratio']:.1%}")
    print(f"负面占比: {result['negative_ratio']:.1%}")
    print(f"风险占比: {result['risk_ratio']:.1%}")
    
    print("\n详细分析:")
    # 循环遍历处理
    # 对集合内每个元素执行相同处理
    for analysis in result['analyzed_comments']:
        print(f"  [{analysis['sentiment']}] {analysis['content'][:30]}")


# 边界/有效性检查
if __name__ == '__main__':
    # 运行任务
    asyncio.run(demo_sentiment_analysis())