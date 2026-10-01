"""
B站运营工具箱 - 数据库模型

使用SQLAlchemy ORM，支持关系映射、查询构建、数据迁移。

本模块定义了整个工具箱的持久化数据模型，按业务域划分如下：

一、用户与认证域
- Account: B站账号表
- CookiePool: Cookie池表

二、视频与UP主数据域
- Video: 视频表
- VideoStats: 视频统计历史表
- UPMaster: UP主信息表

三、评论相关域
- Comment: 评论表
- CommentAlert: 评论预警表

四、热点与选题域
- Hotspot: 热点表
- Topic: 选题库表
- HotspotSignal: 视频类热点信号表
- HotKeywordSignal: 热搜关键词信号表（06 采集广度 · search/square）

五、活动情报域
- Activity: 活动情报表

六、任务与日志域
- Task: 任务表
- OperationLog: 操作日志表

七、LLM使用统计域
- LLMUsage: LLM使用统计表

八、HTTP 配额域
- HttpQuotaBucket: HTTP 尝试配额小时桶（滚动 24h 计数持久化，规格 §2.6）

数据库管理器（DatabaseManager）：
- 使用 SQLite + SQLAlchemy ORM
- StaticPool + check_same_thread=False 支持多线程访问
- 提供建表、会话获取、备份、清库能力
- 全局单例通过 init_database() 初始化

拆分说明（2026-08-22）：
原 database.py（885行）按职责拆分为：
- base.py: logger 与 Base（原始 L85-L88）
- models_account.py: 用户与认证域模型（原始 L93-L168）
- models_video.py: 视频与UP主数据域模型（原始 L173-L333）
- models_comment.py: 评论相关域模型（原始 L338-L432）
- models_hotspot.py: 热点/选题/活动域模型（原始 L437-L561）
- models_system.py: 任务/日志/LLM统计/监控状态模型（原始 L566-L688）
- manager.py: DatabaseManager（原始 L693-L814）
- api.py: 工厂函数 init_database/get_session/get_db（原始 L817-L885）
- models_quota.py: HTTP 尝试配额小时桶（2026-10-02 新增，规格 §2.6）
注释均原样保留，未删除未错位。外部兼容：from core.database import X 全部可用。
"""
from .base import Base, logger
from .models_account import Account, CookiePool
from .models_video import Video, VideoStats, UPMaster
from .models_comment import Comment, CommentAlert
from .models_hotspot import Hotspot, Topic, Activity
from .models_hotspot_signal import HotspotSignal, HotKeywordSignal
from .models_system import Task, OperationLog, LLMUsage, MonitorState
from .models_benchmark import BenchmarkRun
from .models_quota import HttpQuotaBucket
from .manager import DatabaseManager
from .api import init_database, get_session, get_db, db_manager

__all__ = [
    'Base', 'logger',
    'Account', 'CookiePool', 'Video', 'VideoStats', 'UPMaster',
    'Comment', 'CommentAlert', 'Hotspot', 'Topic', 'Activity', 'HotspotSignal',
    'HotKeywordSignal',
    'Task', 'OperationLog', 'LLMUsage', 'MonitorState', 'BenchmarkRun',
    'HttpQuotaBucket',
    'DatabaseManager', 'init_database', 'get_session', 'get_db', 'db_manager',
]
