"""单视频跟踪表 ``hotspot_watch`` 的 ORM 模型（FishTool 02 · 批 2）。

依据：
- ``FishTool_02_热点生命周期_专业方案与Agent执行`` §8（字段表）、§0 裁定一/二/三；
- ``FishTool_前置方案_预算重分与02租约规格`` §3.2「跟踪三态」。

设计要点：
- **一张唯一 bvid 表**：一个 bvid 只采一次；``bvid`` 建 UNIQUE，重复入库走 UPSERT，
  同一目标重复写不产生重复行（02 §7.3 第 3 条：重复发现只更新 last_seen / 元信息，
  不清候选、不重置 next_due、不无限延长 deadline）。
- **三态**（前置方案 §3.2，态名照方案、不自创）：
  跟踪中 ``active=1 AND ttl_end_epoch_s > now`` ／
  已到期 ``active=1 AND ttl_end_epoch_s <= now`` ／
  已释放 ``active=0``。
- **fencing 代际**：``state_revision`` 作写回代际（02 §0 裁定一），任何落盘都走
  「条件 UPDATE ``WHERE bvid=:bvid AND state_revision=:claim_revision``」，旧代际写入丢弃。
- **不发租约**：``lease_token`` / ``lease_until_epoch`` 不建列（02 §0 裁定一）。
- **命名口径**：时间字段一律 ``*_epoch_s`` 后缀、秒级 int（沿用批 1 算法层
  ``TrendState.last_evaluation_epoch_s`` 口径），不使用 ``_ts`` / ``_at`` / 毫秒。

02 定名 → 本批落地名（仅后缀对齐批 1 口径，语义一一对应，非新造字段）：
``first_seen_epoch``→``first_seen_epoch_s``、``last_seen_epoch``→``last_seen_epoch_s``、
``next_due_epoch``→``next_due_epoch_s``、``last_attempt_epoch``→``last_attempt_epoch_s``、
``last_success_epoch``→``last_success_epoch_s``、``ttl_end_epoch``→``ttl_end_epoch_s``、
``released_epoch``→``released_epoch_s``、``last_evaluation_T``→``last_evaluation_epoch_s``。

覆盖范围：本批只落 02 单表所需的列；04 的扩展列（``fast_until_s`` / ``manual_pinned`` /
``source_demands``）按 02 §8.3 由后续整合任务经 ``_migrate_hotspot_watch_event_columns``
幂等补列，本批不预建。
"""
from sqlalchemy import Boolean, Column, Float, Index, Integer, JSON, String, text

from .base import Base


class HotspotWatch(Base):
    """单视频持续跟踪目标（一张唯一 bvid 表）。

    一行 = 一个被持续采样的 bvid 及其调度 / 评估状态。
    三态由 ``(active, ttl_end_epoch_s)`` 组合决定，见模块头注释。
    """

    __tablename__ = 'hotspot_watch'

    # ---- 唯一目标组（02 §8）----
    id = Column(Integer, primary_key=True, autoincrement=True, comment='自增主键')
    bvid = Column(String(20), nullable=False, unique=True, comment='视频BV号；唯一，一个bvid只采一次')
    category_key = Column(String(50), nullable=True, comment='主分类键（兼容原 tid 口径）')
    collection_tid = Column(Integer, nullable=True, comment='用户选择的采集分区ID')
    discovery_source = Column(String(30), nullable=True, comment='发现来源分层标签：ranking/paint_c/manual/events')

    # ---- 生命周期组（02 §8；ttl_end_epoch_s / released_epoch_s 见 §0 裁定二）----
    active = Column(Boolean, nullable=False, default=True, server_default=text('1'), comment='是否在池：1=在池，0=已释放')
    stop_reason = Column(String(32), nullable=True, comment='释放原因码：expired / manual_stop')
    first_seen_epoch_s = Column(Integer, nullable=False, comment='首次入池时刻（UTC 秒）')
    last_seen_epoch_s = Column(Integer, nullable=True, comment='最近一次被发现的时刻（UTC 秒）')
    ttl_end_epoch_s = Column(Integer, nullable=False, comment='本档到期时刻（UTC 秒）；NOT NULL，新建行入池必写')
    released_epoch_s = Column(Integer, nullable=True, comment='释放时刻（UTC 秒）；active=0 时写')

    # ---- 调度组（02 §8）----
    next_due_epoch_s = Column(Integer, nullable=False, comment='下次应采样时刻（UTC 秒）')
    last_attempt_epoch_s = Column(Integer, nullable=True, comment='最近一次尝试时刻（UTC 秒）')
    last_success_epoch_s = Column(Integer, nullable=True, comment='最近一次成功采集时刻（UTC 秒）')
    failure_count = Column(Integer, nullable=False, default=0, server_default=text('0'), comment='连续失败次数；成功清零')
    last_error_code = Column(String(64), nullable=True, comment='最近一次失败错误码（不含错误正文 / Cookie）')
    sample_interval_s = Column(Integer, nullable=False, default=3600, server_default=text('3600'), comment='采样间隔（秒）；崩溃恢复与下次调度只读此列')

    # ---- 评估组（02 §8）----
    last_evaluation_epoch_s = Column(Integer, nullable=True, comment='最近一次已提交评估的固定网格右边界 T（UTC 秒）')
    last_confirmed_stage = Column(String(16), nullable=True, comment='最近一次已确认阶段（独立保留为历史，不随新段重写）')
    state_json = Column(JSON, nullable=True, comment='候选基线 / 计数 / segment 标识等状态 JSON')
    state_revision = Column(Integer, nullable=False, default=0, server_default=text('0'), comment='写回代际（fencing）；每次提交 +1')

    # ---- coverage 两级（02 §0 裁定三：coverage_ratio 必须随评估结果落库）----
    coverage_ratio = Column(Float, nullable=True, comment='最近一次评估的覆盖比例（0~1 数值）')
    coverage_state = Column(String(20), nullable=True, comment='覆盖级别枚举：full_support / provisional / insufficient')

    __table_args__ = (
        Index('ix_hotspot_watch_active_due', 'active', 'next_due_epoch_s'),
        Index('ix_hotspot_watch_active_ttl', 'active', 'ttl_end_epoch_s'),
    )
