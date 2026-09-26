"""
视频与UP主数据域模型

拆分自 database.py 原始 L173-L333。
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship

from .base import Base


# ============ 视频与UP主数据 ============

class Video(Base):
    """视频表
    # 指定目标数据表

    保存抓取到的视频完整信息：
    # 持久化数据，防止丢失
    - 基础元数据：BV号/AV号、标题、封面、简介、时长、发布时间
    - 作者信息：关联账号、UP主UID、UP主名称
    - 分区信息：tid/tname
    - 互动统计：播放/弹幕/评论/收藏/投币/分享/点赞
    - 标签：JSON 数组
    - 监控状态：is_monitoring 标记是否纳入监控
    与评论、统计历史级联关联。
    """
    __tablename__ = 'videos'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 赋值并准备后续使用
    bvid = Column(String(20), unique=True, nullable=False, comment='BV号')
    # 赋值并准备后续使用
    aid = Column(Integer, unique=True, comment='AV号')
    # 赋值并准备后续使用
    title = Column(String(500), comment='标题')
    # 赋值并准备后续使用
    cover = Column(String(500), comment='封面URL')
    # 赋值并准备后续使用
    desc = Column(Text, comment='简介')
    # 赋值并准备后续使用
    duration = Column(Integer, comment='时长(秒)')
    # 赋值并准备后续使用
    pubdate = Column(DateTime, comment='发布时间')
    
    # UP主信息
    account_id = Column(Integer, ForeignKey('accounts.id'))
    # 赋值并准备后续使用
    mid = Column(Integer, comment='UP主UID')
    # 赋值并准备后续使用
    author = Column(String(100), comment='UP主名称')
    
    # 分区信息
    tid = Column(Integer, comment='分区ID')
    # 赋值并准备后续使用
    tname = Column(String(50), comment='分区名称')
    
    # 统计数据
    view = Column(Integer, default=0, comment='播放量')
    # 赋值并准备后续使用
    danmaku = Column(Integer, default=0, comment='弹幕数')
    # 赋值并准备后续使用
    reply = Column(Integer, default=0, comment='评论数')
    # 赋值并准备后续使用
    favorite = Column(Integer, default=0, comment='收藏数')
    # 赋值并准备后续使用
    coin = Column(Integer, default=0, comment='投币数')
    # 赋值并准备后续使用
    share = Column(Integer, default=0, comment='分享数')
    # 赋值并准备后续使用
    like = Column(Integer, default=0, comment='点赞数')
    
    # 标签
    tags = Column(JSON, comment='标签列表')
    
    # 监控状态
    is_monitoring = Column(Boolean, default=False, comment='是否监控中')
    
    created_at = Column(DateTime, default=datetime.now)
    # 赋值并准备后续使用
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 关联
    account = relationship("Account", back_populates="videos")
    # 赋值并准备后续使用
    comments = relationship("Comment", back_populates="video", cascade="all, delete-orphan")
    # 赋值并准备后续使用
    stats_history = relationship("VideoStats", back_populates="video", cascade="all, delete-orphan")

class VideoStats(Base):
    """视频统计历史表
    # 指定目标数据表

    按快照时间保存视频的互动指标，
    # 持久化数据，防止丢失
    一条 Video 可对应多条历史记录，
    用于绘制播放/互动趋势图。
    """
    __tablename__ = 'video_stats'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 赋值并准备后续使用
    video_id = Column(Integer, ForeignKey('videos.id'), nullable=False)
    
    view = Column(Integer, default=0)
    # 赋值并准备后续使用
    danmaku = Column(Integer, default=0)
    # 赋值并准备后续使用
    reply = Column(Integer, default=0)
    # 赋值并准备后续使用
    favorite = Column(Integer, default=0)
    # 赋值并准备后续使用
    coin = Column(Integer, default=0)
    # 赋值并准备后续使用
    share = Column(Integer, default=0)
    # 赋值并准备后续使用
    like = Column(Integer, default=0)
    
    snapshot_time = Column(DateTime, default=datetime.now, comment='快照时间')
    
    video = relationship("Video", back_populates="stats_history")

class UPMaster(Base):
    """UP主信息表
    # 指定目标数据表

    保存UP主档案与运营分析数据：
    # 持久化数据，防止丢失
    - 基础资料：昵称/头像/签名/等级
    - 粉丝数据：follower/following/video_count
    - 分析数据：主要分区、投稿频率、平均播放、平均互动率
    - 第三方数据：zeroroku_data（如星空数据）
    is_head 标记是否属于头部UP主，供竞品分析使用。
    """
    __tablename__ = 'up_masters'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 赋值并准备后续使用
    mid = Column(Integer, unique=True, nullable=False, comment='UP主UID')
    # 赋值并准备后续使用
    name = Column(String(100), comment='昵称')
    # 赋值并准备后续使用
    face = Column(String(500), comment='头像URL')
    # 赋值并准备后续使用
    sign = Column(Text, comment='签名')
    # 赋值并准备后续使用
    level = Column(Integer, comment='等级')
    
    # 统计数据
    follower = Column(Integer, default=0, comment='粉丝数')
    # 赋值并准备后续使用
    following = Column(Integer, default=0, comment='关注数')
    # 赋值并准备后续使用
    video_count = Column(Integer, default=0, comment='投稿数')
    # 站内充电人数，旧库由 DatabaseManager 启动时自动迁移。
    charge_count = Column(Integer, default=0, comment='充电人数')
    
    # 分析数据
    category = Column(String(50), comment='主要分区')
    # 赋值并准备后续使用
    post_frequency = Column(Float, comment='投稿频率(天)')
    # 赋值并准备后续使用
    avg_view = Column(Integer, comment='平均播放量')
    # 赋值并准备后续使用
    avg_interaction = Column(Float, comment='平均互动率')
    
    # zeroroku等第三方数据
    zeroroku_data = Column(JSON, comment='zeroroku数据')
    
    is_head = Column(Boolean, default=False, comment='是否头部UP主')
    
    created_at = Column(DateTime, default=datetime.now)
    # 赋值并准备后续使用
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
