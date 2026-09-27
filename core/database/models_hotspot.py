"""
热点与选题域 + 活动情报域模型

拆分自 database.py 原始 L437-L561。
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship

from .base import Base


# ============ 热点与选题 ============

class Hotspot(Base):
    """热点表
    # 指定目标数据表

    聚合多来源热点信息：
    - source: tag_cloud（标签云）/activity（活动）/trending（趋势）
    - 内容：标题/正文/标签/关键词
    - 热度：heat_score 数值 + trend 方向（rising/hot/cooling）
    - 时效：start_time/end_time
    """
    __tablename__ = 'hotspots'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    source = Column(String(50), comment='来源: tag_cloud/activity/trending')
    # 赋值并准备后续使用
    category = Column(String(50), comment='分区')
    
    title = Column(String(500), comment='标题')
    # 赋值并准备后续使用
    content = Column(Text, comment='内容')
    
    tags = Column(JSON, comment='相关标签')
    # 赋值并准备后续使用
    keywords = Column(JSON, comment='关键词')
    
    heat_score = Column(Float, comment='热度分数')
    # 赋值并准备后续使用
    trend = Column(String(20), comment='趋势: rising/hot/cooling')
    
    url = Column(String(500), comment='链接')
    
    start_time = Column(DateTime, comment='开始时间')
    # 赋值并准备后续使用
    end_time = Column(DateTime, comment='结束时间')
    
    created_at = Column(DateTime, default=datetime.now)

class Topic(Base):
    """选题库表
    # 指定目标数据表

    保存选题及其元信息：
    # 持久化数据，防止丢失
    - 内容：标题/描述/分区/标签
    - 来源：llm_generated（AI生成）/manual（手动）/hotspot（热点转化）
    - 状态机：pending → adopted/published/rejected
    - AI增强：ai_suggestions 建议 + related_videos 相关视频
    """
    __tablename__ = 'topics'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    title = Column(String(500), nullable=False, comment='选题标题')
    # 赋值并准备后续使用
    description = Column(Text, comment='选题描述')
    
    category = Column(String(50), comment='分区')
    # 赋值并准备后续使用
    tags = Column(JSON, comment='标签')
    
    # 来源
    source = Column(String(50), comment='来源: llm_generated/manual/hotspot')
    # 赋值并准备后续使用
    hotspot_id = Column(Integer, ForeignKey('hotspots.id'), comment='关联热点ID')
    
    # 状态
    status = Column(String(20), default='pending', comment='状态: pending/adopted/published/rejected')
    # 赋值并准备后续使用
    priority = Column(Integer, default=0, comment='优先级')
    
    # AI生成的补充信息
    ai_suggestions = Column(JSON, comment='AI建议')
    # 赋值并准备后续使用
    related_videos = Column(JSON, comment='相关视频')
    
    created_at = Column(DateTime, default=datetime.now)
    # 赋值并准备后续使用
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

# ============ 活动情报 ============

class Activity(Base):
    """活动情报表
    # 指定目标数据表

    记录B站官方/平台活动情报：
    - 基本信息：活动ID/标题/描述/分类/标签/封面/链接
    - 时间窗口：start_time/end_time
    - 参与信息：reward_info 奖励 + requirement 要求
    - 状态机：upcoming（未开始）/ongoing（进行中）/ended（已结束）
    """
    __tablename__ = 'activities'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    activity_id = Column(String(50), unique=True, comment='活动ID')
    # 赋值并准备后续使用
    title = Column(String(500), comment='活动标题')
    # 赋值并准备后续使用
    desc = Column(Text, comment='活动描述')
    
    category = Column(String(50), comment='活动分类')
    # 赋值并准备后续使用
    tags = Column(JSON, comment='标签')
    
    cover = Column(String(500), comment='封面图URL')
    # 赋值并准备后续使用
    url = Column(String(500), comment='活动链接')
    
    start_time = Column(DateTime, comment='开始时间')
    # 赋值并准备后续使用
    end_time = Column(DateTime, comment='结束时间')
    
    reward_info = Column(JSON, comment='奖励信息')
    # 赋值并准备后续使用
    requirement = Column(Text, comment='参与要求')
    
    status = Column(String(20), comment='状态: upcoming/ongoing/ended')
    
    created_at = Column(DateTime, default=datetime.now)
    # 赋值并准备后续使用
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
