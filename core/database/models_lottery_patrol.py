"""抽奖工具 · AI 伪人巡检的持久化模型。

本模块承载「按视频扫描评论区 → 规则巡检 → AI 甄别 → 举报」子功能的全部新建表，
与既有 ``Comment`` / ``Task`` 并存，不改写旧字段：

- ``PatrolScan``        一次扫描批次（视频、扫描账号、采集选项、统计、终态）。
- ``PatrolComment``     批次内每条评论的巡检结果（规则 / AI / 最终伪人率、档位、举报状态）。
- ``PatrolReport``      举报台账；POST 前先写 ``sending`` 行，返回后只更新一次终态。
- ``PatrolPreference``  单行偏好（id 恒为 1）：同意版本、白名单、自定义关键词、冷却截止时间等，
                        带 ``revision`` 做 CAS。
- ``PatrolLlmCache``    AI 甄别结果缓存（只存结果，不存外发原文）。

设计约定：
- 不保存 IP 属地、头像（数据最小化）。
- JSON 列必须整对象赋值（in-place 修改不落库）。
- 清除扫描数据时台账保留：``patrol_reports.scan_id`` 为 ``SET NULL``。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)

from .base import Base


class PatrolScan(Base):
    """一次 AI 伪人巡检扫描批次。"""

    __tablename__ = 'patrol_scans'

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_key = Column(String(64), nullable=False, unique=True, comment='任务 UUID，等于 tasks.task_key')
    bvid = Column(String(20), nullable=False, index=True, comment='视频 BV 号')
    aid = Column(Integer, nullable=False, comment='评论区 oid（视频 aid）')
    comment_type = Column(Integer, nullable=False, default=1, comment='评论区类型，视频固定为 1')
    title = Column(String(200), comment='视频标题')
    owner_mid = Column(Integer, comment='UP 主 mid（仅用于白名单）')
    owner_name = Column(String(100), comment='UP 主昵称')
    scanner_mid = Column(Integer, comment='发起扫描时的登录账号 mid')
    options = Column(JSON, comment='采集选项：max_comments / include_replies / mode')
    stats = Column(JSON, comment='采集与分档统计')
    rules_version = Column(String(64), comment='规则词典版本')
    status = Column(String(20), nullable=False, default='running',
                    comment='running/completed/failed/cancelled/interrupted')
    partial = Column(Boolean, nullable=False, default=False, comment='是否不完整采集')
    partial_reason = Column(String(200), comment='不完整原因')
    created_at = Column(DateTime, default=datetime.now, index=True)
    completed_at = Column(DateTime)


class PatrolComment(Base):
    """扫描批次内一条评论的巡检结果。"""

    __tablename__ = 'patrol_comments'

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_id = Column(Integer, ForeignKey('patrol_scans.id', ondelete='CASCADE'), nullable=False, index=True)
    rpid = Column(Integer, nullable=False, comment='评论 rpid')
    root_rpid = Column(Integer, comment='楼中楼所属一级评论 rpid')
    parent_rpid = Column(Integer, comment='直接回复的评论 rpid')
    is_reply = Column(Boolean, nullable=False, default=False, comment='是否楼中楼')
    mid = Column(Integer, nullable=False, index=True, comment='评论者 mid')
    uname = Column(String(100))
    sign = Column(String(200))
    level = Column(Integer)
    is_vip = Column(Boolean)
    official_type = Column(Integer, comment='官方认证类型，-1 为无认证')
    content = Column(Text)
    ctime = Column(DateTime)
    like = Column(Integer)
    rcount = Column(Integer)
    at_users = Column(JSON, comment='被 @ 的用户 [{mid, uname}]')
    rule_score = Column(Integer, nullable=False, default=0)
    rule_hits = Column(JSON, comment='规则命中 [{rule_id, group, field, weight, kind, label, snippet}]')
    hard_groups = Column(JSON, comment='命中的硬命中组')
    has_focus_hit = Column(Boolean, nullable=False, default=False, comment='是否命中重点词或硬命中')
    ai_score = Column(Integer)
    ai_reason = Column(String(200))
    final_score = Column(Integer, nullable=False, default=0)
    tier = Column(String(16), nullable=False, default='human')
    score_source = Column(String(16), nullable=False, default='rule')
    disagreement = Column(Boolean, nullable=False, default=False)
    report_status = Column(String(24), nullable=False, default='none')

    __table_args__ = (
        UniqueConstraint('scan_id', 'rpid', name='ux_patrol_comment_scan_rpid'),
        Index('ix_patrol_comment_scan_score', 'scan_id', 'final_score'),
    )


class PatrolReport(Base):
    """举报台账（原生 / 代发 / 演练 / 跳过都记录）。"""

    __tablename__ = 'patrol_reports'

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_key = Column(String(64), nullable=False, comment='前端生成的幂等键')
    mode = Column(String(12), nullable=False, comment='native/proxy/dry_run')
    reporter_mid = Column(Integer, nullable=False, index=True)
    scan_id = Column(Integer, ForeignKey('patrol_scans.id', ondelete='SET NULL'))
    oid = Column(Integer, nullable=False)
    type = Column(Integer, nullable=False, default=1)
    rpid = Column(Integer, nullable=False)
    target_mid = Column(Integer)
    target_uname = Column(String(100))
    reason = Column(Integer)
    content = Column(String(200))
    final_score = Column(Integer)
    tier = Column(String(16))
    bili_code = Column(Integer)
    bili_message = Column(String(200))
    status = Column(String(24), nullable=False)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint('request_key', 'rpid', name='ux_patrol_report_request_rpid'),
        # 同一账号对同一评论只允许一条“发送中 / 已举报 / 已举报过”的记录，挡住并发重复举报。
        Index(
            'ux_patrol_report_active', 'reporter_mid', 'oid', 'rpid', unique=True,
            sqlite_where=text("status IN ('sending', 'reported', 'duplicate')"),
        ),
    )


class PatrolPreference(Base):
    """巡检偏好（单行，id 恒为 1）。"""

    __tablename__ = 'patrol_preferences'

    id = Column(Integer, primary_key=True)
    revision = Column(Integer, nullable=False, default=0)
    ai_consent_version = Column(String(32))
    ai_model_identity = Column(JSON)
    risk_notice_version = Column(String(32))
    risk_notice_at = Column(DateTime)
    proxy_consent_version = Column(String(32))
    proxy_consent_at = Column(DateTime)
    proxy_enabled = Column(Boolean, nullable=False, default=False)
    include_replies = Column(Boolean, nullable=False, default=True)
    whitelist_mids = Column(JSON)
    user_keywords = Column(JSON)
    default_report_content = Column(String(200))
    report_cooldown_until = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class PatrolLlmCache(Base):
    """AI 甄别结果缓存。"""

    __tablename__ = 'patrol_llm_cache'

    cache_key = Column(String(64), primary_key=True)
    ai_score = Column(Integer)
    reason = Column(String(200))
    model = Column(String(100))
    created_at = Column(DateTime, default=datetime.now, index=True)
