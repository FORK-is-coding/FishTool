"""热点信号持久化模型。

- :class:`HotspotSignal`：视频类信号（榜单/热门条目），tid / bvid 非空。
- :class:`HotKeywordSignal`：热搜关键词信号（``search/square``），单独建表——
  ``HotspotSignal`` 的 tid / bvid 均 ``nullable=False``，关键词两者皆无，
  放宽约束在 SQLite 需重建表（12 步流程），风险高于新建一张纯事实表。
  本表**只存观测事实**（「看到了什么」），不存 lease / run / 状态机（「做到哪一步」）。
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, JSON, Index, UniqueConstraint
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


class HotKeywordSignal(Base):
    """热搜关键词信号（``search/square``），视频类信号的纯事实姊妹表。

    硬约束（顶部复核意见 §3.1 / §5.3）：

    - ``heat_score`` **语义**：平台接口返回热搜分数，不得命名搜索人数 / 播放量 /
      独立用户数，**不进入播放增量计算**；
    - ``heat_score`` **可空**：``keyword`` 有效但分数缺失 -> 保留候选、score 写 SQL
      ``NULL``、``heat_status='missing'``，**不整条丢**；非法值在解析层已跳过，不写 0；
    - 同一词跨时点保留历史（不同 ``captured_epoch_s`` 各一条）；
    - 唯一约束 ``(keyword, captured_epoch_s)``：**同一响应重放**（保留原采样时间）幂等，
      新抓一次生成新时间是**新观察**，允许新增。
    """

    __tablename__ = "hot_keyword_signal"
    id = Column(Integer, primary_key=True, autoincrement=True)
    keyword = Column(String(100), nullable=False, index=True, comment="热搜关键词")
    heat_score = Column(Integer, nullable=True, comment="平台接口返回热搜分数；缺失为 NULL，不用 0 顶替")
    heat_status = Column(String(12), nullable=False, default="ok", comment="ok / missing")
    rank = Column(Integer, nullable=True, comment="列表内名次，从 1 开始")
    captured_epoch_s = Column(Integer, nullable=False, index=True, comment="观测时刻（UTC 秒）")
    source = Column(String(30), nullable=False, default="search_square", comment="固定 search_square")
    snapshot_id = Column(String(64), nullable=True, index=True, comment="所属全局发现快照，便于回放溯源")

    __table_args__ = (
        UniqueConstraint("keyword", "captured_epoch_s", name="uq_hot_keyword_signal_keyword_epoch"),
        Index("ix_hot_keyword_signal_source_time", "source", "captured_epoch_s"),
    )
