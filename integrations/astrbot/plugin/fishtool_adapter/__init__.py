"""FishTool → AstrBot 适配层。

运行在 AstrBot 进程内，只负责：引擎子进程管理、HTTP 代理、事件中继。
不 import FishTool 的任何模块，也不读写 FishTool 的数据库与密钥。
"""
