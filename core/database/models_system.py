"""
任务/日志/LLM统计/监控状态模型

拆分自 database.py 原始 L566-L688。
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship

from .base import Base


# ============ 任务与日志 ============

class Task(Base):
    """任务表
    # 指定目标数据表

    支持异步任务的全生命周期管理：
    - 状态机：pending → running → completed/failed/cancelled
    - progress: 0-1 浮点进度
    - checkpoint: 断点数据，支持中断后恢复（断点续爬）
    - result/error: 执行结果与错误信息
    """
    __tablename__ = 'tasks'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    task_type = Column(String(50), comment='任务类型')
    # 赋值并准备后续使用
    task_name = Column(String(200), comment='任务名称')
    
    params = Column(JSON, comment='任务参数')
    
    status = Column(String(20), default='pending', comment='状态: pending/running/completed/failed/cancelled')
    # 赋值并准备后续使用
    progress = Column(Float, default=0.0, comment='进度 0-1')
    
    result = Column(JSON, comment='执行结果')
    # 赋值并准备后续使用
    error = Column(Text, comment='错误信息')
    
    # 断点续爬支持
    checkpoint = Column(JSON, comment='断点数据')
    
    started_at = Column(DateTime, comment='开始时间')
    # 赋值并准备后续使用
    completed_at = Column(DateTime, comment='完成时间')
    # 赋值并准备后续使用
    created_at = Column(DateTime, default=datetime.now)

class OperationLog(Base):
    """操作日志表
    # 指定目标数据表

    审计日志：记录谁在什么时间做了什么操作。
    - operation: 操作类型
    - module: 所属模块
    - details: 操作详情（JSON）
    - user_id/ip_address: 操作者身份
    """
    __tablename__ = 'operation_logs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    operation = Column(String(100), comment='操作类型')
    # 赋值并准备后续使用
    module = Column(String(50), comment='模块')
    
    details = Column(JSON, comment='详细信息')
    
    status = Column(String(20), comment='状态: success/failed')
    # 赋值并准备后续使用
    error = Column(Text, comment='错误信息')
    
    user_id = Column(Integer, comment='用户ID')
    # 赋值并准备后续使用
    ip_address = Column(String(50), comment='IP地址')
    
    created_at = Column(DateTime, default=datetime.now)

# ============ LLM使用统计 ============

class LLMUsage(Base):
    """LLM使用统计表
    # 指定目标数据表

    按日期+模型记录LLM调用消耗：
    - prompt/completion/total_tokens
    - request_count 请求次数
    - module 调用模块
    用于成本控制、每日限额和用量趋势分析。
    """
    __tablename__ = 'llm_usage'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 自然日字符串与 SQLite 的 date() 输出一致，格式固定为 YYYY-MM-DD。
    date = Column(String(10), nullable=False, comment='统计日期（YYYY-MM-DD）')
    
    model = Column(String(50), comment='模型名称')
    
    prompt_tokens = Column(Integer, default=0, comment='输入token数')
    # 赋值并准备后续使用
    completion_tokens = Column(Integer, default=0, comment='输出token数')
    # 赋值并准备后续使用
    total_tokens = Column(Integer, default=0, comment='总token数')
    
    request_count = Column(Integer, default=0, comment='请求次数')
    
    module = Column(String(50), comment='使用模块')
    
    created_at = Column(DateTime, default=datetime.now)

class MonitorState(Base):
    """常驻评论监控状态表。

    该表是 WebUI 与后台任务之间的本地 SQLite 控制面，保存目标视频、
    运行状态、断点采集统计和最近错误，服务重启后仍可恢复展示。
    """
    __tablename__ = 'monitor_state'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False, default='comment_monitor')
    enabled = Column(Boolean, default=False, nullable=False)
    paused = Column(Boolean, default=False, nullable=False)
    status = Column(String(20), default='stopped', nullable=False)
    target_bvids = Column(JSON, default=list)
    last_collect_at = Column(DateTime)
    total_collected = Column(Integer, default=0, nullable=False)
    last_error = Column(Text)
    consecutive_failures = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
