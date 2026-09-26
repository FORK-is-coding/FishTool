"""
评论相关域模型

拆分自 database.py 原始 L338-L432。
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship

from .base import Base


# ============ 评论相关 ============

class Comment(Base):
    """评论表
    # 指定目标数据表

    保存抓取到的评论完整信息：
    # 持久化数据，防止丢失
    - 内容：content/ctime
    - 作者：uid/uname
    - 互动：like/reply_count
    - 情感分析：sentiment（positive/negative/neutral）+ 得分
    - 反垃圾：is_spam 标记抽奖号/营销号
    - 去重：is_duplicate + duplicate_group/duplicate_count
    - 关键词：keywords JSON 数组
    """
    __tablename__ = 'comments'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 赋值并准备后续使用
    rpid = Column(String(50), unique=True, nullable=False, comment='评论ID')
    # 赋值并准备后续使用
    video_id = Column(Integer, ForeignKey('videos.id'), nullable=False)
    
    # 评论者信息
    uid = Column(Integer, comment='用户UID')
    # 赋值并准备后续使用
    uname = Column(String(100), comment='用户名')
    level_info = Column(JSON, comment='评论接口用户等级原始字段')
    vip = Column(JSON, comment='评论接口大会员原始字段')
    
    # 评论内容
    content = Column(Text, comment='评论内容')
    # 赋值并准备后续使用
    ctime = Column(DateTime, comment='评论时间')
    
    # 统计
    like = Column(Integer, default=0, comment='点赞数')
    # 赋值并准备后续使用
    reply_count = Column(Integer, default=0, comment='回复数')
    
    # 分析结果
    sentiment = Column(String(20), comment='情感倾向: positive/negative/neutral')
    # 赋值并准备后续使用
    sentiment_score = Column(Float, comment='情感得分')
    # 赋值并准备后续使用
    is_spam = Column(Boolean, default=False, comment='是否抽奖号')
    # 赋值并准备后续使用
    is_duplicate = Column(Boolean, default=False, comment='是否重复')
    # 赋值并准备后续使用
    duplicate_group = Column(String(50), comment='重复组ID')
    # 赋值并准备后续使用
    duplicate_count = Column(Integer, default=1, comment='重复次数')
    
    # 关键词
    keywords = Column(JSON, comment='提取的关键词')
    
    created_at = Column(DateTime, default=datetime.now)
    
    video = relationship("Video", back_populates="comments")

class CommentAlert(Base):
    """评论预警表
    # 指定目标数据表

    记录评论监控触发的预警事件：
    - alert_type: negative_surge（负面激增）/keyword_match（关键词命中）/
      volume_surge（数量激增）
    - alert_level: low/medium/high
    - 触发值与阈值：用于审计预警逻辑
    - 处理状态：is_read/is_handled
    # 对数据进行加工/分发
    """
    __tablename__ = 'comment_alerts'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 赋值并准备后续使用
    video_id = Column(Integer, ForeignKey('videos.id'), nullable=False)
    
    alert_type = Column(String(50), comment='预警类型: negative_surge/keyword_match/volume_surge')
    # 赋值并准备后续使用
    alert_level = Column(String(20), comment='预警级别: low/medium/high')
    
    trigger_value = Column(Float, comment='触发值')
    # 赋值并准备后续使用
    threshold = Column(Float, comment='阈值')
    
    message = Column(Text, comment='预警消息')
    # 赋值并准备后续使用
    details = Column(JSON, comment='详细信息')
    
    is_read = Column(Boolean, default=False, comment='是否已读')
    # 赋值并准备后续使用
    is_handled = Column(Boolean, default=False, comment='是否已处理')
    
    created_at = Column(DateTime, default=datetime.now)
