"""FishTool 04 · 第三批 a：话题级热点研判数据模型（六张表）。

依据：
- ``FishTool_04_话题级热点研判与选题决策_02补充执行案(1).md`` §5（L426-552）；
- ``FishTool_04_R5执行规格_第三批a_数据模型与仓储.md`` §1 / §5。

六张表（全部注册到现有 ``Base``，必须经 ``core/database/__init__.py`` 显式 import）：

1. ``hot_events``               —— :class:`HotEvent`（事件锚点 + 发现策略 / 租约 / CAS 版本）
2. ``hot_event_members``        —— :class:`HotEventMember`（成员**追加历史**，非覆盖状态行）
3. ``event_discovery_runs``     —— :class:`EventDiscoveryRun`（一次发现执行）
4. ``hot_event_assessments``    —— :class:`HotEventAssessment`（一次窗口评估快照）
5. ``hotspot_opportunity_runs`` —— :class:`OpportunityRun`（一次机会运行）
6. ``topic_generation_runs``    —— :class:`TopicGenerationRun`（生成请求账本）

全局口径（§5 L428）：
- 时间一律 **epoch 整数秒**，本模块字段统一以 ``_s`` 结尾（秒级 int）；
- JSON 列一律 **整对象赋值**，**禁止无追踪 in-place 更新**：本模块 JSON 列使用朴素
  ``JSON`` 类型、**不挂** ``Mutable``，因此对已加载对象的原地修改不会被追踪，从而
  强制调用方每次都整体赋值（仓储层已按此实现）。

边界（第三批 a 只建表 + 立约束，**不写任何逻辑**）：
- 不写窗口内核 / 归属判定 / 发现围栏 / CreatorBrief / 机会排序等任何算法；
- ``fast_panel_history`` **只建列不写逻辑**（fast panel 冻结属第四批）；
- 不接路由、不接真事件源。

口径要点（与规格 §1 逐条对应，供测试引用）：
- ``hot_event_members`` 唯一 ``(event_id, bvid, revision)``；历史/当前状态查询顺序见仓储层。
- ``hot_event_assessments`` status 只限 :data:`ASSESSMENT_STATUSES`；``insufficient_fast_coverage``
  等**只进 reason_codes**（放 ``interpretation['reason_codes']``），不许自造 status 枚举。
- ``topic_generation_runs`` 的 ``request_hash`` **不唯一**（"重新生成"允许同 hash 新 id）。
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)

from .base import Base

# ===========================================================================
# 受控枚举（唯一口径来源；禁止在别处随手拼枚举）
# ===========================================================================

#: ``hot_events.status`` 合法值（§5.1）。
HOT_EVENT_STATUSES: tuple = ("draft", "active", "paused", "archived")

#: ``hot_event_members.status`` 合法值（§5.2）。
MEMBER_STATUSES: tuple = ("proposed", "accepted", "rejected")

#: ``event_discovery_runs.status`` 合法值（§5.3）。
DISCOVERY_RUN_STATUSES: tuple = (
    "running",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "interrupted",
)

#: ``hot_event_assessments.window_kind`` 合法值（§5.4）。
WINDOW_KINDS: tuple = ("daily24h", "early2h")

#: ``hot_event_assessments.status`` 合法值（§5.4 / 口径 6）。**这里是唯一权威列表。**
ASSESSMENT_STATUSES: tuple = ("complete", "partial", "collecting", "insufficient", "stale")

#: ``topic_generation_runs.state`` 合法值（§5.6 / 口径 7）。
GENERATION_STATES: tuple = ("running", "completed", "failed", "cancelled", "interrupted")

#: 评估 ``reason_codes`` 常见取值（口径 6）。这些**绝不**能当 status 写，
#: 只放 ``interpretation['reason_codes']``。
ASSESSMENT_REASON_CODES: tuple = (
    "insufficient_fast_coverage",
    "sampling_changed",
    "author_coverage_insufficient",
)

# ---------------------------------------------------------------------------
# 发现空结果四因（口径 10）：合法空 / 接口失败 / 页重复 / 达到上限——四者不混
# ---------------------------------------------------------------------------

#: 合法空：接口正常返回但没有新候选。
DISCOVERY_EMPTY_LEGITIMATE: str = "legitimate_empty"
#: 接口失败：请求出错导致无候选（应同时有 status=failed 与 error_code）。
DISCOVERY_EMPTY_INTERFACE_FAILURE: str = "interface_failure"
#: 页重复：分页内容与已抓页面重复，无新增。
DISCOVERY_EMPTY_PAGE_DUPLICATE: str = "page_duplicate"
#: 达到上限：因候选上限截断（``counters`` 记截断量）。
DISCOVERY_EMPTY_CAP_REACHED: str = "cap_reached"

#: 四因全集：空候选的 run 必须显式声明其一，禁止混成裸"空"。
DISCOVERY_EMPTY_REASONS: tuple = (
    DISCOVERY_EMPTY_LEGITIMATE,
    DISCOVERY_EMPTY_INTERFACE_FAILURE,
    DISCOVERY_EMPTY_PAGE_DUPLICATE,
    DISCOVERY_EMPTY_CAP_REACHED,
)


def _in_clause(column: str, values: Iterable[str]) -> str:
    """把受控枚举渲染成 SQLite ``CHECK`` 用的 ``col IN ('a','b')`` 片段。

    Args:
        column: 列名（本模块均为简单标识符，无注入面）。
        values: 合法取值序列。

    Returns:
        str: 形如 ``"status IN ('draft','active')"`` 的 SQL 片段。
    """
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


# ===========================================================================
# 5.1 HotEvent / hot_events
# ===========================================================================

class HotEvent(Base):
    """事件锚点（``hot_events``）。一行 = 一个可研判的话题事件。

    发现策略（``source_policy`` / ``source_policy_hash``）与租约
    （``active_discovery_run_id`` / ``lease_token`` / ``lease_until_s``）都落这里；
    ``revision`` 是 CAS 写版本。
    """

    __tablename__ = "hot_events"

    id = Column(String(64), primary_key=True, comment="事件ID")
    name = Column(String(200), nullable=False, comment="事件名")
    entity_scope = Column(JSON, nullable=True, comment="实体范围 JSON")
    current_rule_version = Column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="当前规则版本"
    )
    rule_history = Column(
        JSON, nullable=True, comment="不可变规则版本列表 [{version,config,effective_s,hash}]"
    )
    created_s = Column(Integer, nullable=False, comment="创建时刻（epoch 秒）")
    updated_s = Column(Integer, nullable=False, comment="更新时刻（epoch 秒）")
    status = Column(
        String(16),
        nullable=False,
        default="draft",
        server_default=text("'draft'"),
        comment="draft/active/paused/archived",
    )
    revision = Column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="CAS 写版本"
    )
    source_policy = Column(JSON, nullable=True, comment="每个来源的固定发现计划")
    fast_panel_history = Column(
        JSON, nullable=True, comment="事前冻结 panel 列表（本批只建列，不写逻辑）"
    )
    supersedes = Column(JSON, nullable=True, comment="合并/拆分来源 event ID，历史身份不覆盖")
    source_policy_hash = Column(
        String(64), nullable=True, comment="当前规范化发现策略 hash（变更策略时同事务更新）"
    )
    active_discovery_run_id = Column(
        String(64), nullable=True, comment="当前领取 run ID；与 lease 一起清理"
    )
    last_discovery_attempt_s = Column(Integer, nullable=True, comment="最近一次发现尝试时刻")
    last_discovery_error_code = Column(String(64), nullable=True, comment="最近一次发现错误码")
    discovery_due_s = Column(Integer, nullable=True, comment="下次应发现时刻（epoch 秒）")
    lease_token = Column(String(64), nullable=True, comment="发现租约 token")
    lease_until_s = Column(Integer, nullable=True, comment="发现租约到期时刻（epoch 秒）")
    links = Column(JSON, nullable=True, comment="显式 Activity.id/url/来源时间/截止可信度")

    __table_args__ = (
        CheckConstraint(_in_clause("status", HOT_EVENT_STATUSES), name="ck_hot_events_status"),
    )


# ===========================================================================
# 5.2 HotEventMember / hot_event_members
# ===========================================================================

class HotEventMember(Base):
    """事件成员版本（``hot_event_members``）。**追加历史，不是覆盖状态行**。

    唯一 ``(event_id, bvid, revision)``。每一 ``revision`` 都独立留痕：

    - ``decision_at_s`` = 该次版本（含 proposed）**实际提交时间**，由服务端时钟决定，
      **不接受客户端提交的更早时间**（仓储层负责改写为实际提交时刻）；
    - 当前状态 = 同 ``(event_id, bvid)`` 的**最新 revision**；
    - 历史状态 = **先限 ``decision_at_s <= cutoff``，再取最新 revision**（顺序不许颠倒）。
    """

    __tablename__ = "hot_event_members"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    event_id = Column(
        String(64), ForeignKey("hot_events.id"), nullable=False, comment="所属事件ID"
    )
    bvid = Column(String(20), nullable=False, comment="视频BV号")
    revision = Column(Integer, nullable=False, comment="成员版本号（同一成员严格递增）")
    status = Column(
        String(16), nullable=False, comment="proposed/accepted/rejected"
    )
    first_seen_s = Column(
        Integer, nullable=False, comment="首次发现时刻（从实际发现记录取得，不回填 pubdate）"
    )
    decision_at_s = Column(
        Integer, nullable=False, comment="该次版本实际提交时刻（含 proposed）"
    )
    rule_version = Column(Integer, nullable=False, comment="判定所用规则版本")
    decision_source = Column(String(32), nullable=False, comment="判定来源：manual/auto/...")
    raw_tid = Column(Integer, nullable=True, comment="原始分区ID（可空）")
    published_epoch_s = Column(Integer, nullable=True, comment="视频发布时间（可空）")
    owner_mid = Column(Integer, nullable=True, comment="UP主 mid（可空）")
    evidence = Column(JSON, nullable=True, comment="来源 run、title/tag 片段、match spans、manual 理由")

    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "bvid",
            "revision",
            name="uq_hot_event_members_event_bvid_revision",
        ),
        CheckConstraint(_in_clause("status", MEMBER_STATUSES), name="ck_hot_event_members_status"),
        Index(
            "ix_hot_event_members_event_bvid_decision",
            "event_id",
            "bvid",
            "decision_at_s",
            "revision",
        ),
    )


# ===========================================================================
# 5.3 EventDiscoveryRun / event_discovery_runs
# ===========================================================================

class EventDiscoveryRun(Base):
    """一次事件发现执行（``event_discovery_runs``）。

    ``rule_version`` / ``source_policy_hash`` 领取时冻结，不随当前 event 改写；
    ``lease_token`` 与本轮随机 token 核对。

    **合法空 / 接口失败 / 页重复 / 达到上限四者必须区分**（口径 10）：
    ``status`` + ``error_code`` + ``counters`` 共同表达，禁止混成一个裸"空"。
    """

    __tablename__ = "event_discovery_runs"

    id = Column(String(64), primary_key=True, comment="发现 run ID")
    event_id = Column(
        String(64), ForeignKey("hot_events.id"), nullable=False, comment="所属事件ID"
    )
    rule_version = Column(Integer, nullable=False, comment="领取时冻结的规则版本")
    source_policy_hash = Column(
        String(64), nullable=False, comment="领取时冻结的规范化发现策略 hash"
    )
    lease_token = Column(String(64), nullable=False, comment="本轮随机 token，与 HotEvent 当前 token 核对")
    trigger = Column(String(16), nullable=False, comment="scheduled/manual")
    started_s = Column(Integer, nullable=False, comment="开始时刻（epoch 秒）")
    finished_s = Column(Integer, nullable=True, comment="结束时刻（epoch 秒）；未结束为空")
    status = Column(
        String(16),
        nullable=False,
        comment="running/completed/partial/failed/cancelled/interrupted",
    )
    error_code = Column(String(64), nullable=True, comment="lease_lost/rule_changed/event_paused 等稳定码")
    source_attempts = Column(JSON, nullable=True, comment="endpoint/query/page/order/成功失败/计数/延迟")
    candidates = Column(JSON, nullable=True, comment="上限 100，bvid 去重 + 必要证据，禁止 headers/secret")
    newly_discovered_bvids = Column(JSON, nullable=True, comment="本轮新增 bvid")
    counters = Column(JSON, nullable=True, comment="重复、缺 ID、超窗、上限、规则排除、empty_reason")

    __table_args__ = (
        CheckConstraint(
            _in_clause("status", DISCOVERY_RUN_STATUSES), name="ck_event_discovery_runs_status"
        ),
    )


# ===========================================================================
# 5.4 HotEventAssessment / hot_event_assessments
# ===========================================================================

class HotEventAssessment(Base):
    """一次窗口评估快照（``hot_event_assessments``）。

    唯一 ``(event_id, window_kind, window_end_s, rule_version, policy_version, revision)``。
    同一 ``input_fingerprint`` 重复执行**返回已有结果**，不新建（仓储层 ``get_by_fingerprint``）。

    ``status`` **只限** :data:`ASSESSMENT_STATUSES`；``insufficient_fast_coverage`` /
    ``sampling_changed`` / ``author_coverage_insufficient`` 等一律进
    ``interpretation['reason_codes']``，**不许自造 status 枚举**（口径 6）。
    """

    __tablename__ = "hot_event_assessments"

    id = Column(String(64), primary_key=True, comment="评估ID")
    event_id = Column(
        String(64), ForeignKey("hot_events.id"), nullable=False, comment="所属事件ID"
    )
    revision = Column(Integer, nullable=False, comment="评估版本号（显式重评 revision+1）")
    as_of_s = Column(Integer, nullable=False, comment="评估基准时刻（epoch 秒）")
    window_end_s = Column(Integer, nullable=False, comment="窗口右边界（epoch 秒）")
    window_kind = Column(String(16), nullable=False, comment="daily24h/early2h")
    rule_version = Column(Integer, nullable=False, comment="规则版本")
    policy_version = Column(String(32), nullable=False, comment="政策版本")
    status = Column(
        String(16), nullable=False, comment="complete/partial/collecting/insufficient/stale"
    )
    input_fingerprint = Column(String(128), nullable=False, comment="输入指纹；命中即复用")
    member_snapshot = Column(JSON, nullable=True, comment="各成员 revision、matched panel 和剔除原因")
    metrics = Column(JSON, nullable=True, comment="数值指标")
    interpretation = Column(
        JSON, nullable=True, comment="方向/阶段/扩散标签/证据等级 + reason_codes（不混数值）"
    )
    provenance = Column(JSON, nullable=True, comment="具体 VideoStats IDs、发现 run、as_of、来源")

    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "window_kind",
            "window_end_s",
            "rule_version",
            "policy_version",
            "revision",
            name="uq_hot_event_assessments_window_revision",
        ),
        CheckConstraint(
            _in_clause("status", ASSESSMENT_STATUSES), name="ck_hot_event_assessments_status"
        ),
        CheckConstraint(
            _in_clause("window_kind", WINDOW_KINDS), name="ck_hot_event_assessments_window_kind"
        ),
        Index(
            "ix_hot_event_assessments_input_fingerprint", "input_fingerprint"
        ),
    )


# ===========================================================================
# 5.5 OpportunityRun / hotspot_opportunity_runs
# ===========================================================================

class OpportunityRun(Base):
    """一次机会运行（``hotspot_opportunity_runs``）。

    ``CreatorBrief`` 保存在机会 run 内形成**不可变输入**；``feedback`` 为 **append 型**
    记录（带 id/时间/revision），**反馈不能覆盖原推荐条件**。``revision`` 是反馈 CAS
    版本，DEFAULT 1。
    """

    __tablename__ = "hotspot_opportunity_runs"

    id = Column(String(64), primary_key=True, comment="机会运行ID")
    created_s = Column(Integer, nullable=False, comment="创建时刻（epoch 秒）")
    revision = Column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
        comment="反馈 CAS；原推荐事实仍不可变",
    )
    creator_brief = Column(JSON, nullable=True, comment="CreatorBrief（不可变输入）")
    assessment_ids = Column(JSON, nullable=True, comment="引用的评估ID列表")
    policy_version = Column(String(32), nullable=False, comment="政策版本")
    candidates = Column(JSON, nullable=True, comment="每事件的分项证据、action、rank_key、排除原因")
    result = Column(JSON, nullable=True, comment="最终机会结果")
    feedback = Column(JSON, nullable=True, comment="append 型反馈记录（带 id/时间/revision）")
    request_fingerprint = Column(String(128), nullable=False, comment="请求指纹")


# ===========================================================================
# 5.6 TopicGenerationRun / topic_generation_runs
# ===========================================================================

class TopicGenerationRun(Base):
    """生成请求账本（``topic_generation_runs``）。

    约束（§5.6 / 口径 7）：

    - ``id`` 主键 = 客户端 ``generation_request_id``；
    - ``state`` 合法值 CHECK（:data:`GENERATION_STATES`）；
    - ``completed`` **必须有** ``result`` 与 ``finished_s``；
    - 非 ``completed`` **不得**携带 ``result``（即"失败不保存成功 saved_ids"）；
    - ``request_hash`` **不唯一** —— 用户明确"重新生成"允许同内容新 id；
    - ``(state, lease_until_s)`` 索引便于重启恢复。

    本批（3a）只做模型 + 读入口 ``get_by_id``；claim/complete 流程归 3e。
    """

    __tablename__ = "topic_generation_runs"

    id = Column(String(64), primary_key=True, comment="= 客户端 generation_request_id")
    schema_version = Column(Integer, nullable=False, comment="账本 schema 版本")
    request_hash = Column(String(128), nullable=False, comment="请求规范化 hash（不唯一）")
    request_payload = Column(JSON, nullable=False, comment="规范化请求，未包含 secret")
    opportunity_run_id = Column(
        String(64),
        ForeignKey("hotspot_opportunity_runs.id"),
        nullable=True,
        comment="关联机会运行ID",
    )
    context_snapshot = Column(
        JSON, nullable=True, comment="首次领取冻结的服务端 context/证据 IDs/政策版本"
    )
    state = Column(
        String(16), nullable=False, comment="running/completed/failed/cancelled/interrupted"
    )
    lease_token = Column(String(64), nullable=True, comment="租约 token")
    lease_until_s = Column(Integer, nullable=True, comment="租约到期时刻（epoch 秒）")
    created_s = Column(Integer, nullable=False, comment="创建时刻（epoch 秒）")
    started_s = Column(Integer, nullable=True, comment="开始时刻（epoch 秒）")
    finished_s = Column(Integer, nullable=True, comment="结束时刻（epoch 秒）")
    result = Column(
        JSON, nullable=True, comment="完整成功响应含 topics/saved_ids/used_llm，非仅计数"
    )
    error_code = Column(String(64), nullable=True, comment="失败错误码")

    __table_args__ = (
        CheckConstraint(
            _in_clause("state", GENERATION_STATES), name="ck_topic_generation_runs_state"
        ),
        CheckConstraint(
            "state <> 'completed' OR (result IS NOT NULL AND finished_s IS NOT NULL)",
            name="ck_topic_generation_runs_completed_result",
        ),
        CheckConstraint(
            "state = 'completed' OR result IS NULL",
            name="ck_topic_generation_runs_result_only_completed",
        ),
        Index(
            "ix_topic_generation_runs_state_lease", "state", "lease_until_s"
        ),
    )


__all__ = [
    "HOT_EVENT_STATUSES",
    "MEMBER_STATUSES",
    "DISCOVERY_RUN_STATUSES",
    "WINDOW_KINDS",
    "ASSESSMENT_STATUSES",
    "GENERATION_STATES",
    "ASSESSMENT_REASON_CODES",
    "DISCOVERY_EMPTY_LEGITIMATE",
    "DISCOVERY_EMPTY_INTERFACE_FAILURE",
    "DISCOVERY_EMPTY_PAGE_DUPLICATE",
    "DISCOVERY_EMPTY_CAP_REACHED",
    "DISCOVERY_EMPTY_REASONS",
    "HotEvent",
    "HotEventMember",
    "EventDiscoveryRun",
    "HotEventAssessment",
    "OpportunityRun",
    "TopicGenerationRun",
]
