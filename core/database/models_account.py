"""
用户与认证域模型

拆分自 database.py 原始 L93-L168。
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship

from .base import Base


# ============ 用户与认证 ============

class Account(Base):
    """B站账号表

    存储用户绑定的B站账号基础信息。
    一个账号可拥有多个 Cookie 和多个视频记录。
    多账号场景下通过 is_primary 区分主账号，
    is_active 控制账号是否参与抓取任务。
    """
    __tablename__ = 'accounts'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    uid = Column(String(50), unique=True, nullable=False, comment='B站UID')
    # 数据库操作
    username = Column(String(100), comment='用户名')
    # 数据库操作
    face = Column(String(500), comment='头像URL')
    # 数据库操作
    is_primary = Column(Boolean, default=False, comment='是否主账号')
    # 数据库操作
    is_active = Column(Boolean, default=True, comment='是否有效')
    # 数据库操作
    cookie_hash = Column(String(64), comment='Cookie哈希值')
    # 数据库操作
    last_check = Column(DateTime, comment='最后检查时间')
    # 数据库操作
    created_at = Column(DateTime, default=datetime.now)
    # 数据库操作
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 关联
    # 级联删除：账号删除时关联 Cookie 一并删除
    # 移除不再需要的数据/对象
    cookies = relationship("CookiePool", back_populates="account", cascade="all, delete-orphan")
    # 数据库操作
    videos = relationship("Video", back_populates="account")

class CookiePool(Base):
    """Cookie池表
    # 指定目标数据表

    保存账号关联的 Cookie 数据（加密存储）。
    # 持久化数据，防止丢失
    关键字段：
    - is_valid: 当前是否可用
    - fail_count: 连续失败次数，用于失效判定
    - last_used/last_check: 使用与检查时间戳
    # 验证状态/条件，决定下一步分支
    池化设计支持多 Cookie 轮换，降低单点失效风险。
    """
    __tablename__ = 'cookie_pool'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 数据库操作
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False)
    # 赋值并准备后续使用
    cookie_data = Column(Text, nullable=False, comment='加密的Cookie数据')
    # 赋值并准备后续使用
    sessdata = Column(String(100), comment='SESSDATA')
    # 赋值并准备后续使用
    bili_jct = Column(String(100), comment='bili_jct')
    # 赋值并准备后续使用
    buvid3 = Column(String(100), comment='buvid3')
    # 赋值并准备后续使用
    is_valid = Column(Boolean, default=True, comment='是否有效')
    # 赋值并准备后续使用
    fail_count = Column(Integer, default=0, comment='失败次数')
    # 赋值并准备后续使用
    last_used = Column(DateTime, comment='最后使用时间')
    # 赋值并准备后续使用
    last_check = Column(DateTime, comment='最后检查时间')
    # 赋值并准备后续使用
    created_at = Column(DateTime, default=datetime.now)
    
    # 反向关联到所属账号
    account = relationship("Account", back_populates="cookies")
