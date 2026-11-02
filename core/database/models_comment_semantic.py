"""评论语义分组与可撤销折叠的持久化模型。

本模块承载「聊天模型语义分组 + 可撤销自动折叠」功能的全部新建表，与既有
``Comment`` / ``Task`` 并存，不改写旧字段：

- ``SemanticRun``      语义任务运行态元数据（复用 ``tasks`` 行做 DTO/checkpoint，
                       本表承载修订号、预算台账、in-flight 预约与覆盖度计数）。
- ``SemanticGroup``    已发布的等价组快照（服务端稳定 group_id、代表、理由、方面、立场）。
- ``SemanticGroupMember`` 组成员行；``(task_id, rpid)`` 唯一，保证一条评论不落入同一任务的多个有效等价组。
- ``SemanticPreference``  视频级偏好（auto_fold）与修订号，承载所有人工操作的 CAS 版本。
- ``SemanticProtection``  人工保护；主键 ``(video_id, rpid)``，跨任务生效，代表/组名变化不绕过。
- ``SemanticAudit``       人工操作审计事件；``(video_id, request_key)`` 幂等。
- ``SemanticDailyUsage``  语义模块自有的日用量台账（预约/结算 token），与既有 LLMUsage 的实际用量分记避免双计。

设计约定：
- 组 ID 为服务端稳定标识，模型永远无权创作平台 ID。
- JSON 列必须整对象赋值（in-place 修改不落库）。
- 不保存完整原文副本；成员仅存短 ID→rpid 映射与内容哈希，原文留在 ``comments`` 表。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Index,
)

from .base import Base


# ============ 上下文状态常量（parser/store/迁移共用） ============

#: 已知顶层评论：原接口 root/parent 字段明确为 0。
CONTEXT_TOP_LEVEL = 'top_level'
#: 已知回复：root/parent 字段明确非 0。
CONTEXT_REPLY = 'reply'
#: 未知上下文：旧库或接口未给 root/parent，不得用默认 0 冒充顶层。
CONTEXT_UNKNOWN = 'unknown'


class SemanticRun(Base):
    """语义任务运行态元数据表。

    与 ``tasks`` 一行一一对应（``task_id`` 外键）：``tasks`` 承载对外 DTO、
    params(snapshot)、checkpoint、result；本表承载并发安全所需的强类型列：
    修订号 ``revision``（CAS）、预算台账、in-flight 预约、覆盖度计数、幂等键。

    状态机落在 ``tasks.status``：queued/running/completed/partial/failed/
    cancelled/interrupted。本表只做"运行控制面"，不复制状态字符串以免漂移。
    """

    __tablename__ = 'semantic_runs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey('tasks.id'), nullable=False, unique=True, comment='对应 tasks.id')
    bvid = Column(String(50), nullable=False, index=True, comment='视频 BV 号')

    # 幂等键：同一 (bvid, snapshot_hash) 下 request_key 唯一，重复启动返回既有任务。
    request_key = Column(String(128), nullable=False, comment='创建幂等键')
    snapshot_hash = Column(String(64), nullable=False, comment='输入快照/上下文/模型版本派生的重放键')

    # CAS 修订号：所有运行态变更（update/reserve/settle/publish/cancel/resume）单调递增。
    revision = Column(Integer, nullable=False, default=0, comment='运行态修订号(CAS)')
    phase = Column(String(30), nullable=False, default='queued', comment='阶段标记')

    # 模型身份（不含密钥），创建时钉死，resume 要求当前身份一致。
    model_identity = Column(JSON, comment='{api_base, model}')

    # 预算台账（本任务维度）
    max_attempts = Column(Integer, nullable=False, default=20, comment='任务最大模型尝试数')
    attempt_count = Column(Integer, nullable=False, default=0, comment='已消耗尝试数(含重试)')
    request_count = Column(Integer, nullable=False, default=0, comment='已发请求次数')
    task_token_budget = Column(Integer, nullable=False, default=60000, comment='任务 token 预算上界')
    daily_token_budget = Column(Integer, nullable=False, default=100000, comment='语义日 token 预算上界')
    reserved_tokens = Column(Integer, nullable=False, default=0, comment='累计预约 token(保守估算)')
    settled_tokens = Column(Integer, nullable=False, default=0, comment='累计结算 token(实际或估算)')

    # in-flight 预约：发请求前持久化，结果写入时核对；崩溃恢复标 outcome_unknown。
    in_flight = Column(JSON, comment='{attempt_id, estimated_tokens, usage_estimated} 或 null')

    # 覆盖度计数（跨批复核）
    coverage = Column(JSON, comment='{batch_reviewed, cross_batch_reviewed, remaining_candidates, excluded, uncertain, unprocessed}')

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        # 幂等键唯一性以 (bvid, request_key) 为准：同一视频同一 request_key 只允许一条，
        # 即便快照不同也不得双插（应用层对"同键不同快照"返回 409；并发竞态由本约束兜底）。
        UniqueConstraint('bvid', 'request_key', name='ux_semantic_run_idem'),
    )


class SemanticGroup(Base):
    """已发布的等价组快照表。

    发布由 store.publish_groups 原子重写（删旧插新）。组 ID 为服务端稳定标识，
    人工保护在读取与发布两处生效。
    """

    __tablename__ = 'semantic_groups'

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(String(64), nullable=False, comment='服务端稳定组标识')
    task_id = Column(Integer, ForeignKey('tasks.id'), nullable=False, index=True, comment='所属任务')
    bvid = Column(String(50), nullable=False, index=True, comment='视频 BV 号')

    representative_id = Column(String(16), nullable=False, comment='代表的快照短 ID')
    representative_rpid = Column(String(50), nullable=False, comment='代表的原始 rpid')
    reason = Column(Text, comment='模型判定理由')
    aspect = Column(String(200), comment='评价对象/方面')
    stance = Column(String(50), comment='立场')
    member_count = Column(Integer, nullable=False, default=0, comment='发布时的有效成员数')

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('task_id', 'group_id', name='ux_semantic_group_task_gid'),
    )


class SemanticGroupMember(Base):
    """等价组成员行。

    ``(task_id, rpid)`` 唯一，保证一条评论不会落入同一任务的多个有效等价组
    （不交叉约束的数据库层兜底）。
    """

    __tablename__ = 'semantic_group_members'

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey('tasks.id'), nullable=False, index=True, comment='所属任务')
    group_id = Column(String(64), nullable=False, comment='所属组(服务端稳定标识)')
    bvid = Column(String(50), nullable=False, comment='视频 BV 号')

    local_id = Column(String(16), nullable=False, comment='快照短 ID')
    rpid = Column(String(50), nullable=False, comment='原始评论 rpid')
    content_hash = Column(String(64), nullable=False, comment='发布时的内容哈希(用于 stale 检测)')
    is_representative = Column(Boolean, nullable=False, default=False, comment='是否组代表')

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('task_id', 'rpid', name='ux_semantic_member_task_rpid'),
        Index('ix_semantic_member_group', 'task_id', 'group_id'),
    )


class SemanticPreference(Base):
    """视频级语义偏好与修订号表。

    一视频一行。``revision`` 是所有人工操作(set_preference/decide)的 CAS 版本，
    刷新/重启/后续任务不覆盖人工纠正。
    """

    __tablename__ = 'semantic_preferences'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bvid = Column(String(50), nullable=False, unique=True, comment='视频 BV 号')
    video_id = Column(Integer, comment='本地 videos.id(可空,便于早期 opt-in)')

    auto_fold = Column(Boolean, nullable=False, default=True, comment='是否自动展示折叠')
    revision = Column(Integer, nullable=False, default=0, comment='偏好修订号(CAS)')

    consent_version = Column(String(50), comment='已同意的隐私/外发版本')
    model_identity = Column(JSON, comment='同意绑定的 {api_base, model}')

    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class SemanticProtection(Base):
    """人工保护表。

    主键 ``(video_id, rpid)``——这里 ``video_id`` 用 bvid 字符串承载(本地稳定视频句柄)，
    **不以模型标签或代表原文哈希为键**，代表/组名变化不绕过保护。撤销整组给组内
    所有成员建行；拆出一个成员仅建该成员行。解除保护显式删除/置失效，不自动重跑模型。
    """

    __tablename__ = 'semantic_protections'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bvid = Column(String(50), nullable=False, comment='视频 BV 号(作为 video_id 稳定句柄)')
    rpid = Column(String(50), nullable=False, comment='被保护评论 rpid')

    keep_separate = Column(Boolean, nullable=False, default=True, comment='是否强制独立展示')
    source = Column(String(30), comment='来源: undo_group/split_member')
    revision = Column(Integer, nullable=False, default=0, comment='建立时的偏好修订号')

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint('bvid', 'rpid', name='ux_semantic_protect_video_rpid'),
    )


class SemanticAudit(Base):
    """人工操作审计事件表。

    ``(bvid, request_key)`` 唯一保证幂等：重复点击返回同一结果，不二次改状态。
    """

    __tablename__ = 'semantic_audits'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bvid = Column(String(50), nullable=False, index=True, comment='视频 BV 号')
    task_id = Column(Integer, comment='关联任务(可空)')
    action = Column(String(30), nullable=False, comment='undo_group/split_member/allow_member/allow_group')
    group_id = Column(String(64), comment='关联组')
    rpid = Column(String(50), comment='关联评论')
    request_key = Column(String(128), nullable=False, comment='幂等键')
    revision_before = Column(Integer, comment='操作前偏好修订号')
    revision_after = Column(Integer, comment='操作后偏好修订号')
    detail = Column(JSON, comment='操作明细(受影响 rpid 列表等)')

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('bvid', 'request_key', name='ux_semantic_audit_idem'),
    )


class SemanticDailyUsage(Base):
    """语义模块日用量台账表。

    语义层自管估算账本(预约/结算)，与既有 ``llm_usage`` 的实际用量分记，避免双计。
    所有语义请求共用此入口做日预算判定。
    """

    __tablename__ = 'semantic_daily_usage'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, unique=True, comment='自然日 YYYY-MM-DD')
    reserved_tokens = Column(Integer, nullable=False, default=0, comment='当日累计预约 token')
    settled_tokens = Column(Integer, nullable=False, default=0, comment='当日累计结算 token')
    request_count = Column(Integer, nullable=False, default=0, comment='当日请求次数')

    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
