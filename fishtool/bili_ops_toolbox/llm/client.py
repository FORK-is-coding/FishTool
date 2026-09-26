"""
LLM接口封装 - OpenAI兼容接口
支持OpenAI、Azure、自定义端点，统一接口调用

本模块提供对大语言模型的统一访问层，核心能力：

一、LLMClient 统一客户端
- 配置来源：优先显式参数，其次读取全局 config
  （api_key 优先从加密 secrets 读取）
- 接口兼容：OpenAI / Azure / 任意兼容端点，
  通过 api_base 切换
- 统一封装：chat_completion（单轮/多轮）、
  simple_chat（一句话）、batch_process（批量）、
  chat_completion_stream（流式）
- Token 限额：每日 token 上限控制，超限抛
  TokenLimitExceededError
- 用量统计：每次调用记录到 LLMUsage 表，
  get_daily_usage 查询当日消耗

二、全局入口
- get_llm_client(): 全局单例，惰性初始化
# 设置初始值/默认状态，避免后续空引用
# 写入配置/属性，影响后续行为
- is_llm_available(): 探测是否配置可用

异常体系（core.exceptions）：
- LLMNotConfiguredError: 未配置密钥/地址
- LLMAPIError: API 层错误（HTTP 状态码非 2xx）
- TokenLimitExceededError: 超出每日限额
- LLMError: 其他通用错误

典型用法：
    async with LLMClient() as client:
        reply = await client.simple_chat("你好")
"""
import asyncio
# 从 typing 导入符号
from typing import List, Dict, Any, Optional, AsyncIterator
# 从 datetime 导入符号
from datetime import datetime, date
# 导入模块
import logging
# 导入模块
import httpx

# 从 core.exceptions 导入符号
from core.exceptions import LLMError, LLMNotConfiguredError, LLMAPIError, TokenLimitExceededError
# 从 core.logger 导入符号
from core.logger import get_logger
# 从 core.config 导入符号
from core.config import config
# 从 core.database 导入符号
from core.database import get_session, LLMUsage

logger = get_logger(__name__)


class LLMClient:
    """LLM客户端 - OpenAI兼容接口
    
    统一封装 chat/completions 接口调用，
    支持密钥管理、限额控制、用量记录。
    """
    
    def __init__(self,
                 api_key: Optional[str] = None,
                 api_base: Optional[str] = None,
                 model: Optional[str] = None,
                 timeout: int = 60):
        """初始化LLM客户端
        # 设置初始值/默认状态，避免后续空引用
        # 写入配置/属性，影响后续行为
        
        配置优先级：显式参数 > 全局配置。
        api_key 从加密 secrets 读取，其次普通配置。
        
        Args:
            api_key: API密钥
            api_base: API基础URL
            model: 模型名称
            timeout: 超时时间(秒)
        """
        # 从配置读取
        # 密钥优先走 secrets（加密存储），兼容旧配置的明文
        self.api_key = api_key or config.get_secret('llm_api_key') or config.get('llm.api_key')
        self.api_base = api_base or config.get('llm.api_base', 'https://api.openai.com/v1')
        self.model = model or config.get('llm.model', 'gpt-3.5-turbo')
        self.timeout = timeout
        
        # 配置检查
        # 缺密钥或地址直接抛异常，避免运行期才暴露
        if not self.api_key:
            # 抛出异常中断流程
            raise LLMNotConfiguredError("LLM API密钥未配置")
        
        # 空值/异常保护：不满足条件时跳过
        if not self.api_base:
            # 抛出异常中断流程
            raise LLMNotConfiguredError("LLM API基础URL未配置")
        
        # HTTP客户端
        # 统一 httpx 异步客户端，带上 Bearer 鉴权头
        self.client = httpx.AsyncClient(
            base_url=self.api_base,
            timeout=self.timeout,
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json'
            }
        )
        
        # Token统计
        # 每日限额从配置读取，默认10万
        self.daily_token_limit = config.get('llm.daily_token_limit', 100000)
        # 记录当前日期，跨天自动重置计数
        self.current_date = date.today()
        self.daily_tokens_used = 0
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        await self.close()
    
    async def close(self):
        """关闭客户端
        # 释放连接/窗口资源
        
        释放 httpx 连接池。
        """
        await self.client.aclose()
    
    def _check_token_limit(self, estimated_tokens: int = 0):
        """检查Token用量限制
        # 验证状态/条件，决定下一步分支
        
        跨天时自动重置计数；预计用量超过
        限额则抛 TokenLimitExceededError。
        
        Args:
            estimated_tokens: 预计使用的Token数
        """
        # 检查日期变更
        # 新的一天重置累计用量
        today = date.today()
        # 边界/有效性检查
        if today != self.current_date:
            self.current_date = today
            self.daily_tokens_used = 0
        
        # 检查限额
        # 预检（估算+已用）超过限额则拒绝本次调用
        if self.daily_tokens_used + estimated_tokens > self.daily_token_limit:
            # 抛出异常中断流程
            raise TokenLimitExceededError(
                self.daily_tokens_used + estimated_tokens,
                self.daily_token_limit
            )
    
    def _record_token_usage(self, 
                           prompt_tokens: int,
                           completion_tokens: int,
                           # 赋值并准备后续使用
                           module: str = "unknown"):
        """记录Token使用情况
        
        更新内存累计，并写入 LLMUsage 表持久化。
        # 用新值覆盖旧值，保持数据一致
        
        Args:
            prompt_tokens: 输入Token数
            completion_tokens: 输出Token数
            module: 使用模块
        """
        total_tokens = prompt_tokens + completion_tokens
        self.daily_tokens_used += total_tokens
        
        # 保存到数据库
        # 便于跨进程/重启后仍能统计
        try:
            # 通过模块级 get_session 获取会话，避免 db_manager 导入时仍为 None
            db = get_session()
            # 异常保护：局部失败不影响主流程
            try:
                # 赋值并准备后续使用
                usage = LLMUsage(
                    # 使用 ISO 自然日字符串，确保 SQLite 中不再写入带时分秒的脏数据。
                    date=datetime.now().date().isoformat(),
                    model=self.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    request_count=1,
                    module=module
                )
                # 加入集合/数据库会话
                db.add(usage)
                # 提交事务
                db.commit()
            # 异常处理
            except Exception as e:
                logger.error(f"记录Token使用失败: {e}")
            # 异常处理
            finally:
                # 关闭连接释放资源
                # 释放连接/窗口资源
                db.close()
        except Exception as e:
            logger.error(f"记录Token使用时获取数据库会话失败: {e}")
        
        logger.info(
            f"Token使用: {total_tokens} "
            f"(输入: {prompt_tokens}, 输出: {completion_tokens}), "
            f"今日累计: {self.daily_tokens_used}/{self.daily_token_limit}"
        )
    
    async def chat_completion(self,
                             messages: List[Dict[str, str]],
                             # 赋值并准备后续使用
                             temperature: float = None,
                             # 赋值并准备后续使用
                             max_tokens: int = None,
                             # 赋值并准备后续使用
                             stream: bool = False,
                             **kwargs) -> Dict[str, Any]:
        """聊天补全
        
        核心接口：发送消息列表到 /chat/completions，
        返回完整响应字典（含 choices/usage 等）。
        # 将结果交回调用方
        
        Args:
            messages: 消息列表 [{"role": "user", "content": "..."}]
            temperature: 温度参数
            max_tokens: 最大Token数
            stream: 是否流式输出
            **kwargs: 其他参数
            
        Returns:
            响应字典
        """
        # 检查Token限制
        # 按字符数/4 粗略估算输入 token
        estimated_tokens = sum(len(m['content']) // 4 for m in messages)
        self._check_token_limit(estimated_tokens)
        
        # 构建请求
        # 温度与 max_tokens 未显式传入时取配置默认值
        payload = {
            'model': self.model,
            'messages': messages,
            'temperature': temperature or config.get('llm.temperature', 0.7),
            'max_tokens': max_tokens or config.get('llm.max_tokens', 2000),
            'stream': stream,
            **kwargs
        }
        
        # 异常保护：局部失败不影响主流程
        try:
            # 赋值并准备后续使用
            response = await self.client.post('/chat/completions', json=payload)
            response.raise_for_status()
            # 解析接口返回的 JSON 数据
            # 将结果交回调用方
            result = response.json()
            
            # 记录Token使用
            # 从响应 usage 字段提取实际消耗
            # 从数据中取出目标字段，供后续逻辑使用
            if 'usage' in result:
                # 赋值并准备后续使用
                usage = result['usage']
                self._record_token_usage(
                    prompt_tokens=usage.get('prompt_tokens', 0),
                    completion_tokens=usage.get('completion_tokens', 0),
                    module=kwargs.get('module', 'unknown')
                )
            
            return result
            
        except httpx.HTTPStatusError as e:
            # 对模型不存在做可操作的中文提示，避免用户只能看到原始英文错误
            response_text = e.response.text or ''
            if 'model_not_found' in response_text or 'model does not exist' in response_text.lower():
                message = f"LLM模型不存在：{self.model}。请在配置中改为 qwen-plus、qwen-max 或 qwen-turbo，并确认API Key已配置"
            else:
                message = f"API请求失败: {response_text}"
            logger.error(f"LLM API请求失败: HTTP {e.response.status_code}, {message}")
            raise LLMAPIError(message)
        # 异常处理
        except Exception as e:
            logger.error(f"LLM请求异常: {e}")
            # 抛出异常中断流程
            raise LLMError(f"请求失败: {e}")
    
    async def chat_completion_stream(self,
                                    messages: List[Dict[str, str]],
                                    # 赋值并准备后续使用
                                    temperature: float = None,
                                    # 赋值并准备后续使用
                                    max_tokens: int = None,
                                    **kwargs) -> AsyncIterator[str]:
        """流式聊天补全
        
        使用 SSE 流式接收增量内容，逐段 yield。
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大Token数
            **kwargs: 其他参数
            
        Yields:
            文本片段
        """
        # 检查Token限制
        estimated_tokens = sum(len(m['content']) // 4 for m in messages)
        self._check_token_limit(estimated_tokens)
        
        payload = {
            'model': self.model,
            'messages': messages,
            'temperature': temperature or config.get('llm.temperature', 0.7),
            'max_tokens': max_tokens or config.get('llm.max_tokens', 2000),
            'stream': True,
            **kwargs
        }
        
        # 异常保护：局部失败不影响主流程
        try:
            # 上下文管理：确保资源自动释放
            async with self.client.stream('POST', '/chat/completions', json=payload) as response:
                response.raise_for_status()
                
                # 逐行解析 SSE 数据
                # 格式：data: {json}\n\n，结束标记 data: [DONE]
                async for line in response.aiter_lines():
                    # 条件分支处理
                    if line.startswith('data: '):
                        # 赋值并准备后续使用
                        data = line[6:]
                        # 边界/有效性检查
                        if data == '[DONE]':
                            # 退出循环
                            break
                        
                        # 异常保护：局部失败不影响主流程
                        try:
                            # 导入模块
                            import json
                            # 赋值并准备后续使用
                            chunk = json.loads(data)
                            # 多条件判断
                            # 根据条件走向不同处理分支
                            if 'choices' in chunk and len(chunk['choices']) > 0:
                                # 读取字典/配置项
                                delta = chunk['choices'][0].get('delta', {})
                                # 边界/有效性检查
                                if 'content' in delta:
                                    yield delta['content']
                        # 异常处理
                        except:
                            # 跳过本轮继续循环
                            continue
                            
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM API流式请求失败: HTTP {e.response.status_code}")
            # 抛出异常中断流程
            raise LLMAPIError(f"流式请求失败: {e.response.text}")
        # 异常处理
        except Exception as e:
            logger.error(f"LLM流式请求异常: {e}")
            # 抛出异常中断流程
            raise LLMError(f"流式请求失败: {e}")
    
    async def simple_chat(self, prompt: str, system: str = None, **kwargs) -> str:
        """简单对话（单轮）
        
        便捷方法：只给用户提示词（可选系统提示），
        返回纯文本回复。
        # 将结果交回调用方
        
        Args:
            prompt: 用户提示词
            system: 系统提示词
            **kwargs: 其他参数
            
        Returns:
            回复文本
        """
        messages = []
        # 判断 system
        # 根据条件走向不同处理分支
        if system:
            # 追加到列表
            messages.append({"role": "system", "content": system})
        # 追加到列表
        messages.append({"role": "user", "content": prompt})
        
        result = await self.chat_completion(messages, **kwargs)
        
        # 校验响应结构
        if 'choices' not in result or len(result['choices']) == 0:
            # 抛出异常中断流程
            raise LLMAPIError("API返回数据格式错误")
        
        return result['choices'][0]['message']['content']
    
    async def batch_process(self,
                          prompts: List[str],
                          # 赋值并准备后续使用
                          system: str = None,
                          # 赋值并准备后续使用
                          batch_size: int = None,
                          **kwargs) -> List[str]:
        """批量处理
        # 对数据进行加工/分发
        
        将多个提示词按批次并发处理，
        # 对数据进行加工/分发
        单条失败时记日志并返回空串，不中断整体。
        # 将结果交回调用方
        
        Args:
            prompts: 提示词列表
            system: 系统提示词
            batch_size: 批次大小
            **kwargs: 其他参数
            
        Returns:
            回复列表
        """
        batch_size = batch_size or config.get('llm.batch_size', 100)
        results = []
        
        # 分批处理
        # 每批内用 asyncio.gather 并发调用
        for i in range(0, len(prompts), batch_size):
            # 赋值并准备后续使用
            batch = prompts[i:i + batch_size]
            # 赋值并准备后续使用
            batch_results = await asyncio.gather(
                *[self.simple_chat(p, system, **kwargs) for p in batch],
                return_exceptions=True
            )
            
            # 处理异常
            # 单条失败不影响整批，返回空串占位
            # 将结果交回调用方
            for j, result in enumerate(batch_results):
                # 条件分支处理
                if isinstance(result, Exception):
                    logger.error(f"批次{i + j}处理失败: {result}")
                    # 追加到列表
                    results.append("")
                # 分支判断
                else:
                    # 追加到列表
                    results.append(result)
        
        return results
    
    def get_daily_usage(self) -> Dict[str, int]:
        """获取今日Token使用统计
        # 读取数据并赋值给当前作用域变量
        
        优先从数据库聚合当日记录（跨进程准确），
        数据库不可用时回退到内存计数。
        
        Returns:
            使用统计
        """
        try:
            # 通过模块级 get_session 获取会话，避免 db_manager 导入时仍为 None
            db = get_session()
            # 异常保护：局部失败不影响主流程
            try:
                # 赋值并准备后续使用
                today = date.today()
                # 取全部记录
                usage_records = db.query(LLMUsage).filter(
                    LLMUsage.date >= datetime.combine(today, datetime.min.time())
                ).all()
                
                total_tokens = sum(r.total_tokens for r in usage_records)
                # 赋值并准备后续使用
                total_requests = sum(r.request_count for r in usage_records)
                
                return {
                    'date': today.isoformat(),
                    'total_tokens': total_tokens,
                    'total_requests': total_requests,
                    'limit': self.daily_token_limit,
                    'remaining': max(0, self.daily_token_limit - total_tokens),
                    'usage_percent': (total_tokens / self.daily_token_limit * 100) if self.daily_token_limit > 0 else 0
                }
            # 异常处理
            finally:
                # 关闭连接释放资源
                # 释放连接/窗口资源
                db.close()
        except Exception as e:
            logger.error(f"读取Token使用统计失败: {e}")
        
        # 数据库不可用时的内存回退
        return {
            'date': date.today().isoformat(),
            'total_tokens': self.daily_tokens_used,
            'limit': self.daily_token_limit,
            'remaining': max(0, self.daily_token_limit - self.daily_tokens_used)
        }
    
    def is_configured(self) -> bool:
        """检查LLM是否已配置
        # 验证状态/条件，决定下一步分支
        
        Returns:
            是否已配置
        """
        return bool(self.api_key and self.api_base)


# 全局LLM客户端实例
# 惰性初始化，首次 get_llm_client() 时创建
_global_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """获取全局LLM客户端实例
    # 读取数据并赋值给当前作用域变量
    
    Returns:
        LLMClient实例
    """
    global _global_llm_client
    # 边界/有效性检查
    if _global_llm_client is None:
        # 赋值并准备后续使用
        _global_llm_client = LLMClient()
    return _global_llm_client


def is_llm_available() -> bool:
    """检查LLM是否可用
    # 验证状态/条件，决定下一步分支
    
    未配置密钥时返回 False 而不抛异常。
    # 将结果交回调用方
    
    Returns:
        是否可用
    """
    try:
        # 赋值并准备后续使用
        client = get_llm_client()
        return client.is_configured()
    # 异常处理
    except LLMNotConfiguredError:
        return False


# 使用示例
async def example_usage():
    """使用示例
    
    演示简单对话、批量情感分类与用量查询。
    """
    async with LLMClient() as client:
        # 简单对话
        response = await client.simple_chat(
            prompt="你好，请介绍一下自己",
            system="你是一个有帮助的AI助手"
        )
        # 输出信息到控制台
        print(response)
        
        # 批量处理
        prompts = [
            "这是一条正面评论：很棒！",
            "这是一条负面评论：太差了",
            "这是一条中性评论：还行吧"
        ]
        # 赋值并准备后续使用
        results = await client.batch_process(
            prompts,
            system="判断评论情感，回答positive/negative/neutral"
        )
        # 输出信息到控制台
        print(results)
        
        # 查看使用统计
        usage = client.get_daily_usage()
        # 输出信息到控制台
        print(f"今日已使用: {usage}")


# 边界/有效性检查
if __name__ == '__main__':
    # 运行任务
    asyncio.run(example_usage())