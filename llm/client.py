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
from datetime import datetime, date, timedelta
# 导入模块
import logging
# 导入模块
import httpx

# 从 core.exceptions 导入符号
from core.exceptions import (
    LLMError, LLMNotConfiguredError, LLMAPIError, LLMOutputTruncatedError, TokenLimitExceededError,
)
# 从 core.logger 导入符号
from core.logger import get_logger
# 从 core.config 导入符号
from core.config import config, LLM_DAILY_TOKEN_LIMIT_DEFAULT
# 从 core.database 导入符号
from core.database import get_session, LLMUsage

logger = get_logger(__name__)


def _coerce_daily_limit(value: Any) -> int:
    """把配置里的每日上限规整为非负整数；非法值回退到默认值。

    0 表示不限制。手改 yaml 写出负数、小数、字符串或布尔值时，记一条告警并按
    默认值处理，避免把限额意外放开或把所有 AI 功能锁死。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        logger.warning(
            "llm.daily_token_limit 配置非法(%r)，按默认值 %s 处理",
            value, LLM_DAILY_TOKEN_LIMIT_DEFAULT,
        )
        return LLM_DAILY_TOKEN_LIMIT_DEFAULT
    return value


def _remaining(limit: int, used: int) -> Optional[int]:
    """当日剩余额度；上限为 0（不限制）时返回 None。"""
    if limit <= 0:
        return None
    return max(0, limit - used)


def _usage_records_on(db, day: date) -> list:
    """查询指定自然日的 LLMUsage 记录。

    LLMUsage.date 是 String(10)（'YYYY-MM-DD'），必须用同类型的字符串区间比较：
    此前用 datetime 参数比较时 SQLite 绑定为 'YYYY-MM-DD 00:00:00'，
    '2026-10-26' >= '2026-10-26 00:00:00' 为假，导致当日用量恒为 0。
    半开区间 [当日, 次日) 同时兼容历史上带时分秒的脏数据。
    """
    start = day.isoformat()
    end = (day + timedelta(days=1)).isoformat()
    return db.query(LLMUsage).filter(LLMUsage.date >= start, LLMUsage.date < end).all()


def _db_tokens_used_on(day: date) -> Optional[int]:
    """读取指定自然日数据库中已记录的 token 总量；数据库不可用时返回 None。"""
    try:
        db = get_session()
        try:
            return sum(int(r.total_tokens or 0) for r in _usage_records_on(db, day))
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"读取当日Token用量失败，回退内存计数: {e}")
        return None


def raise_if_truncated(result: Any, *, max_tokens: Optional[int] = None) -> None:
    """响应的 finish_reason 为 length 时抛 LLMOutputTruncatedError，其余情况什么都不做。

    给自己从 ``choices[0].message.content`` 取正文、并且对缺 choices 有既定处理的调用方用：
    只补上截断检测，不改变它们对畸形响应的处理方式。
    """
    choices = result.get('choices') if isinstance(result, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return
    choice = choices[0]
    if choice.get('finish_reason') != 'length':
        return
    message = choice.get('message') if isinstance(choice.get('message'), dict) else {}
    content = message.get('content') if isinstance(message.get('content'), str) else ''
    reasoning = message.get('reasoning_content')
    reasoning_chars = len(reasoning) if isinstance(reasoning, str) else 0
    logger.warning(
        "LLM 输出被截断: finish_reason=length max_tokens=%s 正文=%d字 思考内容=%d字",
        max_tokens, len(content), reasoning_chars,
    )
    raise LLMOutputTruncatedError(max_tokens, len(content), reasoning_chars)


def configured_max_tokens() -> Any:
    """当前配置的单次输出上限 llm.max_tokens；不传 max_tokens 的请求按它限制输出。"""
    return config.get('llm.max_tokens', 2000)


def extract_reply(result: Any, *, max_tokens: Optional[int] = None) -> str:
    """从 chat completion 响应里取出回复正文；输出被截断时抛 LLMOutputTruncatedError。

    - ``finish_reason == "length"``：输出达到上限，正文不完整。不管正文里有没有半截 JSON，
      都不能当结果用，抛截断异常。
    - 推理模型把思考过程放在 ``message.reasoning_content``，那是草稿，不参与解析，
      只在日志里记录长度，便于判断是不是思考把额度用完了。
    - 其他 ``finish_reason``（stop、缺失等）照常返回正文，保持与旧行为一致。

    Args:
        result: ``chat_completion`` 返回的响应字典。
        max_tokens: 本次请求的输出上限，只用于报错信息。

    Raises:
        LLMAPIError: 响应缺少 choices 或 message。
        LLMOutputTruncatedError: finish_reason 为 length。
    """
    choices = result.get('choices') if isinstance(result, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMAPIError("API返回数据格式错误")
    choice = choices[0]
    message = choice.get('message') if isinstance(choice.get('message'), dict) else None
    if message is None:
        raise LLMAPIError("API返回数据格式错误")
    raise_if_truncated(result, max_tokens=max_tokens)
    content = message.get('content')
    content = content if isinstance(content, str) else ''
    reasoning = message.get('reasoning_content')
    reasoning_chars = len(reasoning) if isinstance(reasoning, str) else 0
    finish_reason = choice.get('finish_reason')
    if not content.strip():
        # 不是截断却没有正文，多半是端点或模型异常；记下结束原因便于排查，不在这里抛错。
        logger.warning(
            "LLM 返回空正文: finish_reason=%s 思考内容=%d字", finish_reason, reasoning_chars,
        )
    return content


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
        # 每日限额从配置读取（所有 AI 功能共用）；0 表示不限制
        self.daily_token_limit = _coerce_daily_limit(
            config.get('llm.daily_token_limit', LLM_DAILY_TOKEN_LIMIT_DEFAULT))
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
        
        # 已用量以数据库当日记录为准：各模块每次请求都会新建 LLMClient，重启也会清零内存计数，
        # 只看实例内计数会让每日限额形同虚设。取 max 兼容写库失败时内存计数更大的情况；
        # 数据库不可用时回退内存计数。并发请求可能各自通过预检，限额按软上限处理。
        db_used = _db_tokens_used_on(today)
        used = self.daily_tokens_used if db_used is None else max(db_used, self.daily_tokens_used)

        # 检查限额
        # 0 表示不限制：用量照常记账，但不拦截请求。
        if self.daily_token_limit <= 0:
            return
        # 预检（估算+已用）超过限额则拒绝本次调用
        if used + estimated_tokens > self.daily_token_limit:
            # 抛出异常中断流程
            raise TokenLimitExceededError(
                used + estimated_tokens,
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
                logger.error(f"记录Token使用失败: {e}", exc_info=True)
            # 异常处理
            finally:
                # 关闭连接释放资源
                # 释放连接/窗口资源
                db.close()
        except Exception as e:
            logger.error(f"记录Token使用时获取数据库会话失败: {e}", exc_info=True)
        
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
        # 内部专用 kwarg：只用于本地限额控制，必须在 _check_token_limit 之前 pop，
        # 绝不能混进发往服务端的 payload。False 表示调用方（语义无限制档）自带预算闸门，
        # 跳过全局日上限检查；用量照常写入 llm_usage。
        enforce_daily_limit = kwargs.pop('enforce_daily_limit', True)
        if enforce_daily_limit:
            self._check_token_limit(estimated_tokens)

        # 内部专用 kwarg：只用于本地记账/参数控制，绝不混入发往服务端的 payload。
        # - module: 用量归属标签（记录 LLMUsage 用）
        # - omit_temperature: 部分兼容端点不支持 temperature，置 True 时整段省略
        module = kwargs.pop('module', 'unknown')
        omit_temperature = kwargs.pop('omit_temperature', False)

        # 构建请求
        # temperature / max_tokens 未显式传入（None）时取配置默认值；
        # 注意用 `is None` 判断，避免 temperature=0 这类合法假值被覆盖。
        payload = {
            'model': self.model,
            'messages': messages,
            'max_tokens': max_tokens if max_tokens is not None else config.get('llm.max_tokens', 2000),
            'stream': stream,
            **kwargs
        }
        if not omit_temperature:
            payload['temperature'] = (
                temperature if temperature is not None
                else config.get('llm.temperature', 0.7)
            )

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
                    module=module
                )
            
            return result
            
        except httpx.HTTPStatusError as e:
            # 对模型不存在做可操作的中文提示，避免用户只能看到原始英文错误
            response_text = e.response.text or ''
            status_code = e.response.status_code
            # 只保留状态码进 message（#105）。message 会经 ai_report['error'] 与
            # /api/analysis/self-diagnosis 响应原样交给调用方，而上游错误体常回显
            # API Key 片段、内部 trace id、请求片段；原始体只放 detail 属性。
            # detail 不截断：它只在进程内供上层归类（错误码提取、semantic provider
            # 关键词匹配），截断会把 JSON 报文切坏、把关键词切掉。
            if 'model_not_found' in response_text or 'model does not exist' in response_text.lower():
                message = f"LLM模型不存在：{self.model}。请在配置中改为 qwen-plus、qwen-max 或 qwen-turbo，并确认API Key已配置"
            else:
                message = f"API请求失败: HTTP {status_code}"
            # 日志只记状态码，不落原始响应体（可能含密钥/敏感信息）
            logger.error(f"LLM API请求失败: HTTP {status_code}")
            # 在异常实例上附带状态码与原始细节，供上层（如 provider）归类后脱敏。
            err = LLMAPIError(message)
            err.status_code = status_code
            err.detail = response_text
            raise err
        # 异常处理
        except Exception as e:
            logger.error(f"LLM请求异常: {e}", exc_info=True)
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
        # 同 chat_completion：enforce_daily_limit 在限额检查前 pop，不进请求体。
        enforce_daily_limit = kwargs.pop('enforce_daily_limit', True)
        if enforce_daily_limit:
            self._check_token_limit(estimated_tokens)

        # 内部专用 kwarg 不入 payload（与 chat_completion 对齐）
        kwargs.pop('module', None)
        omit_temperature = kwargs.pop('omit_temperature', False)

        payload = {
            'model': self.model,
            'messages': messages,
            'max_tokens': max_tokens if max_tokens is not None else config.get('llm.max_tokens', 2000),
            'stream': True,
            **kwargs
        }
        if not omit_temperature:
            payload['temperature'] = (
                temperature if temperature is not None
                else config.get('llm.temperature', 0.7)
            )

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
                        except Exception as exc:
                            # 单个 SSE 块解析失败不应中断整个流；记录原因后跳过。
                            logger.debug("跳过无法解析的 SSE 数据块: %s", exc, exc_info=True)
                            continue
                            
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            logger.error(f"LLM API流式请求失败: HTTP {status_code}")
            # 与 chat_completion 一致：message 只带状态码，原始体放 detail。
            err = LLMAPIError(f"流式请求失败: HTTP {status_code}")
            err.status_code = status_code
            err.detail = e.response.text or ''
            raise err
        # 异常处理
        except Exception as e:
            logger.error(f"LLM流式请求异常: {e}", exc_info=True)
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

        max_tokens = kwargs.get('max_tokens')
        if max_tokens is None:
            max_tokens = configured_max_tokens()
        result = await self.chat_completion(messages, **kwargs)
        return extract_reply(result, max_tokens=max_tokens)
    
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
                # 取当日记录（字符串区间，与 _check_token_limit 共用同一口径）
                usage_records = _usage_records_on(db, today)
                
                total_tokens = sum(r.total_tokens for r in usage_records)
                # 赋值并准备后续使用
                total_requests = sum(r.request_count for r in usage_records)
                
                return {
                    'date': today.isoformat(),
                    'total_tokens': total_tokens,
                    'total_requests': total_requests,
                    'limit': self.daily_token_limit,
                    # 上限为 0 表示不限制，没有“剩余额度”的概念
                    'remaining': _remaining(self.daily_token_limit, total_tokens),
                    'usage_percent': (total_tokens / self.daily_token_limit * 100) if self.daily_token_limit > 0 else 0
                }
            # 异常处理
            finally:
                # 关闭连接释放资源
                # 释放连接/窗口资源
                db.close()
        except Exception as e:
            logger.error(f"读取Token使用统计失败: {e}", exc_info=True)
        
        # 数据库不可用时的内存回退
        return {
            'date': date.today().isoformat(),
            'total_tokens': self.daily_tokens_used,
            'limit': self.daily_token_limit,
            'remaining': _remaining(self.daily_token_limit, self.daily_tokens_used)
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


async def reset_llm_client() -> None:
    """重置全局LLM客户端（保存LLM配置后调用）

    旧实例在构造时已固定 api_base、model 和带密钥的请求头，
    仅重载配置无法让它切换到新配置，必须丢弃后按新配置重建。

    实现步骤：
    1. 先清空单例，后续 get_llm_client() 按新配置重建
    2. 再关闭旧实例持有的 httpx 连接池
    3. 关闭失败只记日志，不影响配置保存
    """
    global _global_llm_client
    old_client = _global_llm_client
    _global_llm_client = None
    if old_client is None:
        return
    try:
        await old_client.close()
    except Exception as e:
        logger.warning(f"关闭旧LLM客户端失败: {e}")


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