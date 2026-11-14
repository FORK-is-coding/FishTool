"""
LLM接口封装 - 模块初始化

本包提供统一的大模型调用入口：
- client: LLMClient 客户端（OpenAI兼容接口封装）
- get_llm_client(): 全局单例获取
- is_llm_available(): 可用性探测

外部使用方式：
    from llm import LLMClient, get_llm_client
"""
from .client import LLMClient, get_llm_client, is_llm_available

__all__ = [
    'LLMClient',
    'get_llm_client',
    'is_llm_available',
]
