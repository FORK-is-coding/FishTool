"""
运营策略分析器 - LLM驱动的策略拆解
基于UP主数据，拆解运营策略并输出结构化建议

本模块使用 LLM 对 UP 主数据进行五维度运营策略拆解：

一、分析维度
1. 选题方向：视频选题规律与可复制策略
2. 标题套路：标题特征与模板技巧
3. 封面风格：封面设计与注意事项
4. 发布节奏：投稿频率与时间规律
5. 互动引导：粉丝互动技巧

二、流程（analyze_strategy）
1. 检查 LLM 配置（未配置返回引导提示）
2. format_up_data_for_prompt 格式化数据
   （UID/数据源/粉丝/指标/近期视频标题）
3. build_analysis_prompt 构造五维提示词
   （强制 ==== 分隔符格式）
4. 调用 LLM（temperature=0.7, max_tokens=2000）
5. parse_analysis_result 解析结构化输出

三、降级
- LLM 未配置：返回 llm_not_configured 错误与配置提示
- LLM 调用失败：返回 llm_error
- generate_fallback_tips 提供通用建议（备用）

四、解析健壮性
- 兼容中英文冒号（: / ：）
- 未解析到的维度记录 warning 日志
- 非法格式时保留已有字段

依赖：
- llm.client: LLMClient
- core.exceptions: LLMNotConfiguredError/LLMError
"""
import asyncio
import re
from typing import Dict, Any, Optional, List
from datetime import datetime
import logging

from core.logger import get_logger
from core.exceptions import LLMNotConfiguredError, LLMError
from llm.client import LLMClient

logger = get_logger(__name__)


class StrategyAnalyzer:
    """运营策略分析器 - LLM拆解运营打法"""
    
    def __init__(self, llm_client: Optional[LLMClient] = None):
        """初始化策略分析器
        
        Args:
            llm_client: LLM客户端，为None时检测配置
        """
        self.llm_client = llm_client
        self.has_llm = False
        
        # 尝试初始化LLM
        # 传入实例直接使用，否则尝试自动创建
        if llm_client:
            self.has_llm = True
        else:
            try:
                self.llm_client = LLMClient()
                self.has_llm = True
                logger.info("[策略分析] LLM已配置")
            except LLMNotConfiguredError:
                logger.warning("[策略分析] LLM未配置，将返回提示信息")
                self.has_llm = False
    
    def format_up_data_for_prompt(self, up_data: Dict[str, Any]) -> str:
        """格式化UP主数据为LLM提示词
        
        Args:
            up_data: UP主数据字典
            
        Returns:
            格式化的文本描述
        """
        uid = up_data.get('uid')
        data = up_data.get('data', {})
        completeness = up_data.get('completeness', 'unknown')
        name = data.get('name') or '未获取到昵称'
        total_play = data.get('total_play', 0)
        
        # 基础身份信息必须随每次请求一起发送，避免模型用外部记忆补全错误账号。
        prompt_parts = [
            f"目标UP主UID: {uid}",
            f"目标UP主昵称（仅作身份核对）: {name}",
            f"目标UP主累计播放量: {total_play:,}",
        ]
        
        # 数据完整性说明
        if completeness == 'full':
            prompt_parts.append("数据来源: zeroroku三方平台（完整数据）")
        else:
            prompt_parts.append("数据来源: B站公开页面+本地估算（部分数据）")
        
        # 粉丝数据
        fans = data.get('fans', 0)
        if fans:
            prompt_parts.append(f"粉丝数: {fans:,}")
        
        # 运营指标
        metrics = data.get('metrics', {})
        if metrics:
            post_freq = metrics.get('post_frequency', 0)
            engagement = metrics.get('engagement_rate', 0)
            avg_play = metrics.get('avg_play', 0)
            
            if post_freq:
                prompt_parts.append(f"投稿频率: {post_freq}视频/周")
            if engagement:
                prompt_parts.append(f"互动率: {engagement}%")
            if avg_play:
                prompt_parts.append(f"平均播放: {avg_play:,}")
        
        # 视频数据（展示前5个标题与播放）
        video_list = data.get('video_list', [])
        if video_list:
            prompt_parts.append(f"\n近期视频数据（共{len(video_list)}个）:")
            
            for i, video in enumerate(video_list, 1):
                title = video.get('title', '未知')
                play = video.get('play', 0)
                prompt_parts.append(f"  {i}. {title} (播放: {play:,})")
        
        return "\n".join(prompt_parts)
    
    def build_analysis_prompt(self, up_data: Dict[str, Any]) -> str:
        """构建运营策略分析提示词
        
        Args:
            up_data: UP主数据
            
        Returns:
            LLM提示词
        """
        formatted_data = self.format_up_data_for_prompt(up_data)
        
        prompt = f"""你是一位资深的B站运营专家，请分析以下UP主的运营策略，并给出结构化的拆解建议。

【身份与数据边界】
- 当前且仅当前分析对象是 UID={up_data.get('uid')}，昵称={up_data.get('data', {}).get('name') or '未获取到昵称'}。
- 只能使用下方提供的实时抓取数据，不得使用训练记忆、其他UP主资料或猜测来补全身份。
- 不认识该UP主时必须明确写“数据不足”，禁止编造其经历、赛道、粉丝画像或作品信息。
- 输出中必须保留并核对当前 UID；如果引用的事实无法在下方数据中找到，改为说明数据缺失。

{formatted_data}

请从以下5个维度进行拆解分析，每个维度必须包含：
1. 核心观察（基于数据的客观发现）
2. 具体打法（可借鉴的操作手法）
3. 关键要点（注意事项或成功要素）

输出格式要求：
===== 选题方向 =====
核心观察: [数据支撑的观察]
具体打法: [可复制的选题策略]
关键要点: [选题注意事项]

===== 标题套路 =====
核心观察: [标题规律分析]
具体打法: [标题模板或技巧]
关键要点: [标题注意事项]

===== 封面风格 =====
核心观察: [封面特征描述]
具体打法: [封面设计建议]
关键要点: [封面注意事项]

===== 发布节奏 =====
核心观察: [投稿频率和时间规律]
具体打法: [发布时间和频率建议]
关键要点: [节奏控制要点]

===== 互动引导 =====
核心观察: [互动数据特征]
具体打法: [粉丝互动技巧]
关键要点: [互动注意事项]

注意：
1. 必须严格按照上述格式输出，使用===分隔符
2. 避免空泛的建议，给出具体可执行的打法
3. 基于数据说话，避免猜测
4. 如果某个维度数据不足，说明数据缺失原因，但仍需给出通用建议
"""
        return prompt
    
    async def analyze_strategy(self, up_data: Dict[str, Any]) -> Dict[str, Any]:
        """分析UP主运营策略
        
        Args:
            up_data: UP主数据字典
            
        Returns:
            分析结果，包含5个维度的结构化建议
        """
        logger.info(f"[策略分析] 开始分析UP主{up_data.get('uid')}的运营策略")
        
        # 检查LLM配置
        # 未配置时返回引导提示
        if not self.has_llm:
            logger.warning("[策略分析] LLM未配置，返回提示信息")
            return {
                'success': False,
                'error': 'llm_not_configured',
                'message': '该功能需要配置大模型API才能使用',
                'config_hint': '请在配置页面填写LLM API密钥、Base URL和模型名称'
            }
        
        try:
            # 构建提示词
            prompt = self.build_analysis_prompt(up_data)
            
            # 每次调用都从本次抓取结果构造独立身份约束，不在实例上保存请求数据。
            target_uid = str(up_data.get('uid') or '')
            target_name = up_data.get('data', {}).get('name') or '未获取到昵称'
            system_prompt = (
                "你是B站运营数据分析器。你必须只分析本次消息给出的目标UP主和实时数据。"
                "禁止调用训练记忆补全UP主身份，禁止混入其他账号的信息；数据不足时直接说明不足。"
                f"本次唯一目标：UID={target_uid}，昵称={target_name}。"
                "回答第一行必须严格输出“身份核对: UID=<UID> | 昵称=<昵称>”，随后再输出五维分析。"
            )

            # 调用LLM。通用OpenAI兼容端点没有统一的联网搜索参数，避免发送供应商不支持的字段。
            logger.info(f"[策略分析] 调用LLM分析: uid={target_uid}, name={target_name}")
            response = await self.llm_client.chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=2000
            )
            
            # 解析LLM返回
            # 正确解析 OpenAI 格式返回：response['choices'][0]['message']['content']
            if 'choices' in response and len(response['choices']) > 0:
                analysis_text = response['choices'][0]['message']['content']
            else:
                analysis_text = ''
            
            if not analysis_text:
                raise LLMError("LLM返回内容为空")

            # 身份回显是防止模型串号的最后一道校验；不匹配时拒绝展示整份分析。
            identity_match = re.search(
                r'身份核对\s*[:：]\s*UID\s*=\s*(\d+)\s*\|\s*昵称\s*=\s*([^\n]+)',
                analysis_text,
            )
            if not identity_match:
                raise LLMError("LLM未按要求回显目标UP主身份，已拒绝不可靠分析")
            echoed_uid = identity_match.group(1).strip()
            echoed_name = identity_match.group(2).strip()
            if echoed_uid != target_uid or echoed_name != target_name:
                logger.error(
                    "[策略分析] 身份核对失败: expected uid=%s name=%s, got uid=%s name=%s",
                    target_uid,
                    target_name,
                    echoed_uid,
                    echoed_name,
                )
                raise LLMError("LLM返回了其他UP主身份，已拒绝串号分析")
            
            # 解析结构化输出
            parsed_result = self.parse_analysis_result(analysis_text)
            
            logger.info("[策略分析] 分析完成")
            
            return {
                'success': True,
                'uid': up_data.get('uid'),
                'data_completeness': up_data.get('completeness'),
                'analysis': parsed_result,
                'raw_text': analysis_text,
                'analyzed_at': datetime.now().isoformat()
            }
            
        except LLMError as e:
            logger.error(f"[策略分析] LLM错误: {e}")
            return {
                'success': False,
                'error': 'llm_error',
                'message': f'LLM调用失败: {str(e)}'
            }
        except Exception as e:
            logger.error(f"[策略分析] 分析失败: {e}")
            return {
                'success': False,
                'error': 'unknown_error',
                'message': f'分析失败: {str(e)}'
            }
    
    def parse_analysis_result(self, text: str) -> Dict[str, Dict[str, str]]:
        """解析LLM返回的五维运营分析。

        Args:
            text: LLM返回的原始文本，支持分隔符或Markdown标题格式。

        Returns:
            固定包含五个维度及三个小节的结构化字典。
        """
        dimensions = ['选题方向', '标题套路', '封面风格', '发布节奏', '互动引导']
        fields = ['核心观察', '具体打法', '关键要点']
        result = {
            dimension: {field: '' for field in fields}
            for dimension in dimensions
        }

        try:
            current_dimension: Optional[str] = None
            current_field: Optional[str] = None

            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue

                # 去除常见Markdown装饰及列表前缀，兼容模型未严格遵循分隔符的情况。
                normalized = line.replace('**', '').replace('__', '').strip()
                normalized = re.sub(r'^[#=>\-*+\s]+', '', normalized).strip()
                dimension_candidate = re.sub(r'^\d+[.、]\s*', '', normalized).strip(' =#')

                matched_dimension = next(
                    (dimension for dimension in dimensions if dimension_candidate == dimension),
                    None,
                )
                if matched_dimension:
                    current_dimension = matched_dimension
                    current_field = None
                    continue

                field_match = re.match(
                    r'^(核心观察|具体打法|关键要点)\s*[:：]?\s*(.*)$',
                    normalized,
                )
                if field_match and current_dimension:
                    current_field = field_match.group(1)
                    content = field_match.group(2).strip()
                    if content:
                        result[current_dimension][current_field] = content
                    continue

                # 字段正文可能跨多行，按原顺序累积并交给前端分段展示。
                if current_dimension and current_field:
                    previous = result[current_dimension][current_field]
                    result[current_dimension][current_field] = (
                        f'{previous}\n{normalized}'.strip() if previous else normalized
                    )

            empty_dims = [
                dimension for dimension, data in result.items() if not any(data.values())
            ]
            if empty_dims:
                logger.warning(f"[解析] 以下维度未解析到内容: {empty_dims}")
        except Exception as e:
            logger.error(f"[解析] 解析分析结果失败: {e}")

        return result
    
    def generate_fallback_tips(self) -> Dict[str, str]:
        """生成降级版通用运营建议（无LLM时使用）
        
        Returns:
            通用建议字典
        """
        return {
            '选题方向': '关注分区热门话题和趋势，结合自身特色定位细分赛道',
            '标题套路': '使用数字+痛点+好处的结构，适当加入悬念和争议性',
            '封面风格': '高对比度、大字体、人物特写，3秒内传达核心信息',
            '发布节奏': '固定时段发布（晚8-10点黄金时段），保持每周2-3更稳定产出',
            '互动引导': '视频结尾引导三连，置顶评论抛话题，及时回复前100条评论'
        }
