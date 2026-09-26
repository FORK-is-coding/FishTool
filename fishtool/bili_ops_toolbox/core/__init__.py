"""
B站运营工具箱 - 核心模块初始化
本包提供全项目的基础设施，统一导出：
- config: ConfigManager 配置管理（单例实例）
- logger: 分级日志系统（init_logger/get_logger）
- database: 数据库模型与管理器（init_database/get_db）
- exceptions: 统一异常体系
各模块的全局实例（config/db_manager/logger_manager）
在首次使用时惰性初始化，导入本包不触发副作用。
使用方式：
    from core import config, init_logger, get_db
"""
# 配置模块：ConfigManager 负责读写 config.yaml 与加密 secrets
# config 为进程级单例，导入即加载配置文件
from .config import config, ConfigManager
# 日志模块：分级日志、文件输出、风控级别标记
# logger_manager 为全局单例，get_logger 供业务模块按名取子 logger
# RiskControlLevel 标记风控事件级别，写盘为独立 risk_control.log
from .logger import init_logger, get_logger, logger_manager, LoggerManager, RiskControlLevel
# 数据库模块：ORM 模型、连接管理器、会话工厂
# 业务代码用 get_session() 拿会话，查询/提交/关闭一套流程
from .database import init_database, get_db, get_session, db_manager, DatabaseManager, Base
# 异常模块：全项目统一异常体系，星号导入暴露全部异常类
# 各模块抛出统一异常，上层捕获后按类型分流处理
# 避免裸抛 ValueError/KeyError 等标准异常导致的语义模糊
# 星号导入依赖 exceptions 模块定义 __all__
from .exceptions import *

# 对外导出清单
# 分为配置/日志/数据库/异常四组，保持命名空间整洁
# 外部统一从 core 导入，不直接触碰子模块内部符号
# 新增子模块时需同步更新 __all__ 与文档
# 惰性初始化保证导入 core 包本身无副作用
# 副作用操作（建表/写文件）统一放在显式 init_* 函数中
__all__ = [
    # 配置
    'config',          # 配置单例实例
    # 全局唯一，导入即加载
    'ConfigManager',   # 配置管理类

    # 日志
    'init_logger',     # 初始化日志系统
    'get_logger',      # 获取模块日志器
    'logger_manager',  # 日志管理器实例
    'LoggerManager',   # 日志管理器类
    'RiskControlLevel',  # 风控级别枚举

    # 数据库
    'init_database',   # 初始化数据库
    'get_db',          # 获取数据库连接
    'get_session',     # 获取ORM会话
    'db_manager',      # 数据库管理器实例
    'DatabaseManager', # 数据库管理器类
    'Base',            # ORM基类

    # 异常
    'BiliOpsException',      # 基础异常
    'ConfigError',           # 配置错误
    'DatabaseError',         # 数据库错误
    'BilibiliAPIError',      # B站API错误
    'AuthenticationError',   # 认证错误
    'CookieExpiredError',    # Cookie过期
    'RateLimitError',        # 限频错误
    'Status429Error',        # 429状态错误
    'CrawlerError',          # 爬虫错误
    'LLMError',              # LLM错误
    'LLMNotConfiguredError', # LLM未配置错误
]
