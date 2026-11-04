"""适配层异常类型。

单独成模块、不导入第三方库：聊天指令逻辑（commands.py）只依赖这里，单测环境不需要 httpx。
"""
from __future__ import annotations


class EngineUnavailable(RuntimeError):
    """引擎不可达（未运行、连接失败、超时）。"""


class CommandError(Exception):
    """用户可读的指令错误（参数不对、引擎没运行等），消息原样回复给用户。"""
