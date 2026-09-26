"""热点信号持久化模型。"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, JSON, Index
from .base import Base


class HotspotSignal(Base):
    """记录标题、弹幕/评论词和播放增速等标准化信号。"""

    __tablename__ = "hotspot_signal"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tid = Column(Integer, nullable=False, index=True, comment="B站分区ID")
    bvid = Column(String(20), nullable=False, index=True, comment="视频BV号")
    collected_at = Column(DateTime, default=datetime.now, nullable=False, index=True, comment="采集时间")
    source = Column(String(30), nullable=False, comment="title/comment/danmaku/view_growth/tag")
    value = Column(JSON, nullable=False, default=dict, comment="带source元数据的结构化信号")

    __table_args__ = (Index("ix_hotspot_signal_tid_time", "tid", "collected_at"),)
