"""FishTool 04 · 第三批 a：话题级热点研判仓储层（六张表的读写封装）。

对应规格 §2「仓储层要求」五条：

1. 六张表的 create/get/update 基础读写，**JSON 列一律整对象赋值**；
2. ``hot_event_members``：:meth:`HotEventRepository.latest_status` 与
   :meth:`HotEventRepository.status_at`——后者 **先限 ``decision_at_s <= cutoff`` 再取最新
   revision**，顺序不许颠倒；
3. ``hot_event_assessments``：:meth:`HotEventRepository.get_by_fingerprint` 命中即返回已有行；
4. ``topic_generation_runs``：:meth:`HotEventRepository.get_by_id`（幂等读入口，claim/complete 归 3e）；
5. **不引入新依赖**（仅用已在用的 SQLAlchemy）。

设计口径（跟随仓库既有 store 风格，如 ``core/quota_store.py`` / ``modules/hotspot/discovery/store.py``）：

- 仓储 **接受可注入的 ``session_factory``**，便于单测指向临时库，绝不隐式连生产库；
- 每个公开方法 **自持事务**（内部 commit / rollback / close），调用方拿到的是已 detach 的对象；
- 时间一律 **epoch 整数秒**；缺省用 ``clock``（默认 ``time.time``）取当前时刻，
  测试可注入固定时钟或直接传 ``now_s``；
- JSON 列走 **整对象赋值**：``update_*`` 把新值整体 ``setattr`` 覆盖，从不原地改已加载对象。

边界：本批**只做读写 + 约束**，不写任何算法（窗口内核 / 归属 / 围栏 / 机会排序 / 生成 claim）。
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from .api import get_session
from .models_hot_event import (
    ASSESSMENT_STATUSES,
    DISCOVERY_RUN_STATUSES,
    DISCOVERY_EMPTY_REASONS,
    GENERATION_STATES,
    HOT_EVENT_STATUSES,
    MEMBER_STATUSES,
    WINDOW_KINDS,
    EventDiscoveryRun,
    HotEvent,
    HotEventAssessment,
    HotEventMember,
    OpportunityRun,
    TopicGenerationRun,
)

logger = logging.getLogger(__name__)

#: 会话工厂签名：无参调用返回一个 ``Session``。
SessionFactory = Callable[[], Session]

#: 时钟签名：无参调用返回 epoch 秒（int）。
Clock = Callable[[], int]


class FeedbackRevisionConflict(RuntimeError):
    """反馈 CAS（``revision``）冲突：同一 revision 已被其它写入抢先更新。"""


class HotEventRepository:
    """六张话题研判表的仓储封装。

    用法::

        repo = HotEventRepository()                       # 默认连全局库（生产）
        repo = HotEventRepository(session_factory=fac)   # 测试注入临时库
    """

    def __init__(
        self,
        session_factory: Optional[SessionFactory] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        """初始化仓储。

        Args:
            session_factory: 会话工厂；缺省用 ``core.database.get_session``。
            clock: 秒级时钟；缺省用 ``time.time``（取 int）。
        """
        self._session_factory: SessionFactory = session_factory or get_session
        self._clock: Clock = clock or (lambda: int(time.time()))

    # ------------------------------------------------------------------ 内部工具

    def _resolve_now(self, now_s: Optional[int]) -> int:
        """把可选 ``now_s`` 归一为 epoch 整数秒。

        Args:
            now_s: 调用方显式给定的时刻；为 None 时取注入时钟。

        Returns:
            int: epoch 秒。
        """
        if now_s is None:
            return int(self._clock())
        if type(now_s) is bool or not isinstance(now_s, int):
            raise ValueError("now_s_must_be_int")
        return int(now_s)

    @staticmethod
    def _require_enum(value: Any, allowed: tuple, code: str) -> None:
        """校验取值落在受控枚举内（口径 6/7：禁止随手拼枚举）。

        Args:
            value: 待校验值。
            allowed: 合法取值元组。
            code: 失败时抛出的稳定错误码。

        Raises:
            ValueError: ``value`` 不在 ``allowed`` 内。
        """
        if value not in allowed:
            raise ValueError(code)

    @staticmethod
    def _assign(row: Any, name: str, value: Any) -> None:
        """把某列整体赋值为 ``value``（JSON 列也是整体替换，不做 in-place 合并）。

        Args:
            row: ORM 行。
            name: 列名。
            value: 新值（整对象）。

        Raises:
            ValueError: 列名不在该表上。
        """
        if name not in row.__table__.c:
            raise ValueError(f"unknown_column:{row.__table__.name}.{name}")
        setattr(row, name, value)

    def _insert(self, instance: Any) -> Any:
        """插入一行并返回 detach 后的对象。

        Args:
            instance: 已构造、未入库的 ORM 实例。

        Returns:
            Any: 已 commit 并 refresh 的同一实例（会话已关闭）。

        Raises:
            Exception: 写库失败时回滚并原样抛出。
        """
        session = self._session_factory()
        try:
            session.add(instance)
            session.commit()
            session.refresh(instance)
            session.expunge(instance)
            return instance
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _fetch(self, model: Any, pk: Any) -> Any:
        """按主键读取一行（读不到返回 None）。

        Args:
            model: ORM 模型类。
            pk: 主键值。

        Returns:
            Any | None: 命中的行（detach）或 None。
        """
        session = self._session_factory()
        try:
            return session.get(model, pk)
        finally:
            session.close()

    def _update(self, model: Any, pk: Any, updates: Mapping[str, Any]) -> Any:
        """按主键更新若干列（整对象赋值）并返回 detach 后的对象。

        Args:
            model: ORM 模型类。
            pk: 主键值。
            updates: 列名 -> 新值 的映射（JSON 列整体替换）。

        Returns:
            Any: 更新后的行。

        Raises:
            KeyError: 主键不存在。
            ValueError: 出现未知列。
            Exception: 写库失败时回滚并原样抛出。
        """
        session = self._session_factory()
        try:
            row = session.get(model, pk)
            if row is None:
                raise KeyError(f"{model.__tablename__}_not_found:{pk!r}")
            for name, value in updates.items():
                self._assign(row, name, value)
            # 事件表带 updated_s，更新时同步刷新（若有该列）
            if "updated_s" in row.__table__.c and "updated_s" not in updates:
                row.updated_s = int(self._clock())
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _new_id(prefix: str) -> str:
        """生成服务端随机 ID（仅用于未显式给 id 的场景）。

        Args:
            prefix: 前缀，便于人读。

        Returns:
            str: ``"{prefix}_{32位hex}"``。
        """
        return f"{prefix}_{uuid.uuid4().hex}"

    # ====================================================== 5.1 hot_events

    def create_hot_event(
        self, *, event_id: str, name: str, now_s: Optional[int] = None, **fields: Any
    ) -> HotEvent:
        """新建事件锚点。

        Args:
            event_id: 事件ID（主键，客户端提供）。
            name: 事件名。
            now_s: 显式提交时刻；缺省取注入时钟。
            **fields: 其余 ``hot_events`` 列（含 ``status`` / ``source_policy`` 等整对象）。

        Returns:
            HotEvent: 落库后的行。

        Raises:
            ValueError: ``status`` 非法或未知列。
        """
        now = self._resolve_now(now_s)
        if "status" in fields:
            self._require_enum(fields["status"], HOT_EVENT_STATUSES, "invalid_hot_event_status")
        fields.setdefault("status", "draft")
        fields.setdefault("current_rule_version", 0)
        fields.setdefault("revision", 0)
        fields.setdefault("created_s", now)
        fields.setdefault("updated_s", now)
        return self._insert(HotEvent(id=event_id, name=name, **fields))

    def get_hot_event(self, event_id: str) -> Optional[HotEvent]:
        """按主键读取事件。

        Args:
            event_id: 事件ID。

        Returns:
            HotEvent | None
        """
        return self._fetch(HotEvent, event_id)

    def update_hot_event(self, event_id: str, **fields: Any) -> HotEvent:
        """按主键更新事件（JSON 列整对象赋值）。

        Args:
            event_id: 事件ID。
            **fields: 要更新的列。

        Returns:
            HotEvent

        Raises:
            KeyError: 事件不存在。
            ValueError: ``status`` 非法或未知列。
        """
        if "status" in fields:
            self._require_enum(fields["status"], HOT_EVENT_STATUSES, "invalid_hot_event_status")
        return self._update(HotEvent, event_id, fields)

    # ============================================== 5.2 hot_event_members

    def create_member_revision(
        self,
        *,
        event_id: str,
        bvid: str,
        revision: int,
        status: str,
        first_seen_s: int,
        rule_version: int,
        decision_source: str,
        now_s: Optional[int] = None,
        decision_at_s: Optional[int] = None,
        **fields: Any,
    ) -> HotEventMember:
        """追加一条成员版本（**追加历史，不覆盖旧行**）。

        口径 4：``decision_at_s`` = 该次版本**实际提交时间**（由服务端时钟决定，含 proposed）。
        客户端传入的 ``decision_at_s`` **即便更早也不会被采纳**，一律改写为实际提交时刻。

        Args:
            event_id: 所属事件ID。
            bvid: 视频BV号。
            revision: 版本号（同 (event,bvid) 严格递增）。
            status: proposed/accepted/rejected。
            first_seen_s: 首次发现时刻（不回填 pubdate）。
            rule_version: 判定规则版本。
            decision_source: 判定来源。
            now_s: 实际提交时刻；缺省取注入时钟。
            decision_at_s: 客户端可能提交的时间戳；**仅用于说明被改写**，不采纳更早值。
            **fields: 其余成员列（``evidence`` 等整对象）。

        Returns:
            HotEventMember: 落库后的行。

        Raises:
            ValueError: ``status`` 非法或未知列。
        """
        now = self._resolve_now(now_s)
        self._require_enum(status, MEMBER_STATUSES, "invalid_member_status")
        if decision_at_s is not None and int(decision_at_s) < now:
            # 记录而非采纳：客户端的更早时间被改写为实际提交时刻。
            logger.info(
                "成员 %s/%s rev=%s 客户端 decision_at_s=%s 早于提交时刻 %s，改写为实际提交时刻",
                event_id,
                bvid,
                revision,
                decision_at_s,
                now,
            )
        row = HotEventMember(
            event_id=event_id,
            bvid=bvid,
            revision=revision,
            status=status,
            first_seen_s=first_seen_s,
            decision_at_s=now,  # 口径 4：服务端实际提交时刻，不接受更早
            rule_version=rule_version,
            decision_source=decision_source,
            **fields,
        )
        return self._insert(row)

    def next_member_revision(self, event_id: str, bvid: str) -> int:
        """计算某成员的下一个版本号（当前最大 revision + 1；无记录则 1）。

        Args:
            event_id: 所属事件ID。
            bvid: 视频BV号。

        Returns:
            int: 下一个 revision。
        """
        session = self._session_factory()
        try:
            from sqlalchemy import func as _func

            current = (
                session.query(_func.max(HotEventMember.revision))
                .filter(HotEventMember.event_id == event_id, HotEventMember.bvid == bvid)
                .scalar()
            )
            return int(current) + 1 if current is not None else 1
        finally:
            session.close()

    def get_member_revision(self, member_id: int) -> Optional[HotEventMember]:
        """按自增主键读取某条成员版本。

        Args:
            member_id: ``hot_event_members.id``。

        Returns:
            HotEventMember | None
        """
        return self._fetch(HotEventMember, member_id)

    def update_member_revision(self, member_id: int, **fields: Any) -> HotEventMember:
        """按主键更新某条成员版本（JSON 列整对象赋值）。

        Args:
            member_id: ``hot_event_members.id``。
            **fields: 要更新的列。

        Returns:
            HotEventMember

        Raises:
            KeyError: 记录不存在。
            ValueError: ``status`` 非法或未知列。
        """
        if "status" in fields:
            self._require_enum(fields["status"], MEMBER_STATUSES, "invalid_member_status")
        return self._update(HotEventMember, member_id, fields)

    def latest_status(self, event_id: str, bvid: str) -> Optional[HotEventMember]:
        """当前状态：同 ``(event_id, bvid)`` 的**最新 revision**（无 cutoff）。

        口径 3：同秒多 revision 按 ``revision`` **严格排序**（以 revision 为主序）。

        Args:
            event_id: 所属事件ID。
            bvid: 视频BV号。

        Returns:
            HotEventMember | None: 最新一条版本；无记录返回 None。
        """
        session = self._session_factory()
        try:
            return (
                session.query(HotEventMember)
                .filter(
                    HotEventMember.event_id == event_id,
                    HotEventMember.bvid == bvid,
                )
                .order_by(
                    HotEventMember.revision.desc(),
                    HotEventMember.decision_at_s.desc(),
                    HotEventMember.id.desc(),
                )
                .first()
            )
        finally:
            session.close()

    def status_at(self, event_id: str, bvid: str, cutoff_s: int) -> Optional[HotEventMember]:
        """历史状态：**先限 ``decision_at_s <= cutoff_s``，再取最新 revision**。

        口径 2（关键，顺序不许颠倒）：**不能先过滤 accepted 再取最新**，否则已撤销成员
        会被旧 accepted 记录复活。这里第一个过滤条件就是时间，绝不带 ``status`` 过滤。

        Args:
            event_id: 所属事件ID。
            bvid: 视频BV号。
            cutoff_s: 历史截止时刻（epoch 秒，含）。

        Returns:
            HotEventMember | None: 截止时刻下最新的版本；无记录返回 None。
        """
        session = self._session_factory()
        try:
            return (
                session.query(HotEventMember)
                .filter(
                    HotEventMember.event_id == event_id,
                    HotEventMember.bvid == bvid,
                    # 口径 2：先限时间（第一步），再取最新 revision（第二步）。
                    HotEventMember.decision_at_s <= int(cutoff_s),
                )
                .order_by(
                    HotEventMember.revision.desc(),
                    HotEventMember.decision_at_s.desc(),
                    HotEventMember.id.desc(),
                )
                .first()
            )
        finally:
            session.close()

    # ============================================ 5.3 event_discovery_runs

    def create_discovery_run(
        self,
        *,
        run_id: str,
        event_id: str,
        rule_version: int,
        source_policy_hash: str,
        lease_token: str,
        trigger: str,
        now_s: Optional[int] = None,
        status: str = "running",
        **fields: Any,
    ) -> EventDiscoveryRun:
        """新建一次发现执行。

        口径 10 的四种结果（合法空 / 接口失败 / 页重复 / 达到上限）由 ``status`` +
        ``error_code`` + ``counters`` 共同表达，四者可分别落库、互不混淆；本方法**只负责
        原样持久化**，不决定究竟属于哪一种（那是 3b 的发现逻辑）。

        Args:
            run_id: 发现 run ID。
            event_id: 所属事件ID。
            rule_version: 领取时冻结的规则版本。
            source_policy_hash: 领取时冻结的策略 hash。
            lease_token: 本轮随机 token。
            trigger: scheduled/manual。
            now_s: 开始时刻；缺省取注入时钟。
            status: running/completed/partial/failed/cancelled/interrupted。
            **fields: 其余列（``candidates`` / ``counters`` / ``error_code`` 等整对象）。

        Returns:
            EventDiscoveryRun

        Raises:
            ValueError: ``status`` 非法或未知列。
        """
        now = self._resolve_now(now_s)
        self._require_enum(status, DISCOVERY_RUN_STATUSES, "invalid_discovery_run_status")
        fields.setdefault("started_s", now)
        return self._insert(
            EventDiscoveryRun(
                id=run_id,
                event_id=event_id,
                rule_version=rule_version,
                source_policy_hash=source_policy_hash,
                lease_token=lease_token,
                trigger=trigger,
                status=status,
                **fields,
            )
        )

    def get_discovery_run(self, run_id: str) -> Optional[EventDiscoveryRun]:
        """按主键读取发现 run。

        Args:
            run_id: 发现 run ID。

        Returns:
            EventDiscoveryRun | None
        """
        return self._fetch(EventDiscoveryRun, run_id)

    def update_discovery_run(self, run_id: str, **fields: Any) -> EventDiscoveryRun:
        """按主键更新发现 run（JSON 列整对象赋值）。

        Args:
            run_id: 发现 run ID。
            **fields: 要更新的列。

        Returns:
            EventDiscoveryRun

        Raises:
            KeyError: 记录不存在。
            ValueError: ``status`` 非法或未知列。
        """
        if "status" in fields:
            self._require_enum(fields["status"], DISCOVERY_RUN_STATUSES, "invalid_discovery_run_status")
        return self._update(EventDiscoveryRun, run_id, fields)

    # ============================================ 5.4 hot_event_assessments

    def create_assessment(
        self,
        *,
        event_id: str,
        revision: int,
        as_of_s: int,
        window_end_s: int,
        window_kind: str,
        rule_version: int,
        policy_version: str,
        status: str,
        input_fingerprint: str,
        assessment_id: Optional[str] = None,
        **fields: Any,
    ) -> HotEventAssessment:
        """新建一次窗口评估；**同 ``input_fingerprint`` 命中即返回已有行，不新建**。

        口径 5：唯一六元组 ``(event_id, window_kind, window_end_s, rule_version,
        policy_version, revision)`` 由数据库兜底；指纹去重在入库前短路。
        口径 6：``status`` 只限 :data:`ASSESSMENT_STATUSES`；``insufficient_fast_coverage``
        等一律进 ``interpretation['reason_codes']``。

        Args:
            event_id: 所属事件ID。
            revision: 评估版本号。
            as_of_s: 评估基准时刻。
            window_end_s: 窗口右边界。
            window_kind: daily24h/early2h。
            rule_version: 规则版本。
            policy_version: 政策版本。
            status: complete/partial/collecting/insufficient/stale。
            input_fingerprint: 输入指纹。
            assessment_id: 显式评估ID；缺省生成随机 ID。
            **fields: 其余列（``interpretation`` / ``metrics`` / ``provenance`` 等整对象）。

        Returns:
            HotEventAssessment: 新建的行，或指纹命中的已有行。

        Raises:
            ValueError: ``window_kind`` / ``status`` 非法或未知列。
        """
        self._require_enum(window_kind, WINDOW_KINDS, "invalid_window_kind")
        self._require_enum(status, ASSESSMENT_STATUSES, "invalid_assessment_status")
        existing = self.get_by_fingerprint(input_fingerprint)
        if existing is not None:
            # 口径 5：同一输入指纹重复执行返回已有结果，不新建。
            return existing
        return self._insert(
            HotEventAssessment(
                id=assessment_id or self._new_id("asmt"),
                event_id=event_id,
                revision=revision,
                as_of_s=as_of_s,
                window_end_s=window_end_s,
                window_kind=window_kind,
                rule_version=rule_version,
                policy_version=policy_version,
                status=status,
                input_fingerprint=input_fingerprint,
                **fields,
            )
        )

    def get_assessment(self, assessment_id: str) -> Optional[HotEventAssessment]:
        """按主键读取评估。

        Args:
            assessment_id: 评估ID。

        Returns:
            HotEventAssessment | None
        """
        return self._fetch(HotEventAssessment, assessment_id)

    def get_by_fingerprint(self, input_fingerprint: str) -> Optional[HotEventAssessment]:
        """按 ``input_fingerprint`` 读取已有评估（命中即复用）。

        同一指纹若有多个 revision，取最新 revision。

        Args:
            input_fingerprint: 输入指纹。

        Returns:
            HotEventAssessment | None
        """
        session = self._session_factory()
        try:
            return (
                session.query(HotEventAssessment)
                .filter(HotEventAssessment.input_fingerprint == input_fingerprint)
                .order_by(
                    HotEventAssessment.revision.desc(),
                    HotEventAssessment.id.desc(),
                )
                .first()
            )
        finally:
            session.close()

    def update_assessment(self, assessment_id: str, **fields: Any) -> HotEventAssessment:
        """按主键更新评估（JSON 列整对象赋值）。

        Args:
            assessment_id: 评估ID。
            **fields: 要更新的列。

        Returns:
            HotEventAssessment

        Raises:
            KeyError: 记录不存在。
            ValueError: ``status`` / ``window_kind`` 非法或未知列。
        """
        if "status" in fields:
            self._require_enum(fields["status"], ASSESSMENT_STATUSES, "invalid_assessment_status")
        if "window_kind" in fields:
            self._require_enum(fields["window_kind"], WINDOW_KINDS, "invalid_window_kind")
        return self._update(HotEventAssessment, assessment_id, fields)

    # ========================================== 5.5 hotspot_opportunity_runs

    def create_opportunity_run(
        self,
        *,
        run_id: str,
        policy_version: str,
        request_fingerprint: str,
        now_s: Optional[int] = None,
        revision: int = 1,
        **fields: Any,
    ) -> OpportunityRun:
        """新建一次机会运行。

        Args:
            run_id: 机会运行ID。
            policy_version: 政策版本。
            request_fingerprint: 请求指纹。
            now_s: 创建时刻；缺省取注入时钟。
            revision: 反馈 CAS 版本，默认 1。
            **fields: 其余列（``creator_brief`` / ``candidates`` / ``feedback`` 等整对象）。

        Returns:
            OpportunityRun
        """
        now = self._resolve_now(now_s)
        fields.setdefault("created_s", now)
        return self._insert(
            OpportunityRun(
                id=run_id,
                revision=revision,
                policy_version=policy_version,
                request_fingerprint=request_fingerprint,
                **fields,
            )
        )

    def get_opportunity_run(self, run_id: str) -> Optional[OpportunityRun]:
        """按主键读取机会运行。

        Args:
            run_id: 机会运行ID。

        Returns:
            OpportunityRun | None
        """
        return self._fetch(OpportunityRun, run_id)

    def update_opportunity_run(self, run_id: str, **fields: Any) -> OpportunityRun:
        """按主键更新机会运行（JSON 列整对象赋值；``feedback`` 为 append 语义由调用方保证）。

        Args:
            run_id: 机会运行ID。
            **fields: 要更新的列。

        Returns:
            OpportunityRun

        Raises:
            KeyError: 记录不存在。
            ValueError: 未知列。
        """
        return self._update(OpportunityRun, run_id, fields)

    def get_opportunity_run_by_fingerprint(
        self, request_fingerprint: str
    ) -> Optional[OpportunityRun]:
        """按 ``request_fingerprint`` 读取已有机会运行（同 assessment 的命中口径）。

        同一指纹若有多个 run，取**最近创建**的一行；调用方据此实现「同指纹不新建」。

        Args:
            request_fingerprint: 请求指纹。

        Returns:
            OpportunityRun | None
        """
        session = self._session_factory()
        try:
            return (
                session.query(OpportunityRun)
                .filter(OpportunityRun.request_fingerprint == request_fingerprint)
                .order_by(
                    OpportunityRun.created_s.desc(),
                    OpportunityRun.id.desc(),
                )
                .first()
            )
        finally:
            session.close()

    def append_opportunity_feedback(
        self,
        run_id: str,
        entry: Mapping[str, Any],
        *,
        expected_revision: Optional[int] = None,
        now_s: Optional[int] = None,
    ) -> OpportunityRun:
        """以 **append + CAS** 语义追加一条反馈（**不覆盖原推荐条件**）。

        只整体替换 ``feedback`` 列并自增 ``revision``；``creator_brief`` / ``candidates`` /
        ``result`` 等**原推荐事实逐字不变**。

        Args:
            run_id: 机会运行ID。
            entry: 反馈内容（自动补 ``appended_s`` 与 ``revision``）。
            expected_revision: 期望的当前 revision；不匹配即冲突（CAS 语义）。
            now_s: 追加时刻；缺省取注入时钟。

        Returns:
            OpportunityRun: 更新并 detach 后的行。

        Raises:
            KeyError: run 不存在。
            FeedbackRevisionConflict: 期望 revision 不匹配，或条件更新被并发抢先。
        """
        now = self._resolve_now(now_s)
        session = self._session_factory()
        try:
            row = session.get(OpportunityRun, run_id)
            if row is None:
                raise KeyError(f"opportunity_run_not_found:{run_id}")
            current_revision = int(row.revision)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise FeedbackRevisionConflict("feedback_revision_conflict")
            feedback = list(row.feedback or [])
            new_revision = current_revision + 1
            appended = dict(entry)
            appended.setdefault("appended_s", now)
            appended["revision"] = new_revision
            feedback.append(appended)
            outcome = session.execute(
                update(OpportunityRun)
                .where(
                    OpportunityRun.id == run_id,
                    OpportunityRun.revision == current_revision,
                )
                .values(feedback=feedback, revision=new_revision)
            )
            if outcome.rowcount != 1:
                session.rollback()
                raise FeedbackRevisionConflict("feedback_revision_conflict")
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ============================================ 5.6 topic_generation_runs

    @staticmethod
    def _validate_generation_payload(
        state: str, result: Any, finished_s: Any
    ) -> None:
        """校验生成账本 payload 口径（口径 7）。

        Args:
            state: 账本状态。
            result: 结果 JSON（可空）。
            finished_s: 结束时刻（可空）。

        Raises:
            ValueError: ``completed`` 缺 result/finished_s；或非 ``completed`` 携带了 result
                （即"失败不保存成功 saved_ids"）。
        """
        if state == "completed":
            if result is None or finished_s is None:
                raise ValueError("generation_completed_requires_result_and_finished_s")
        elif result is not None:
            raise ValueError("generation_result_only_allowed_when_completed")

    def create_generation_run(
        self,
        *,
        run_id: str,
        schema_version: int,
        request_hash: str,
        request_payload: Mapping[str, Any],
        state: str = "running",
        now_s: Optional[int] = None,
        **fields: Any,
    ) -> TopicGenerationRun:
        """新建生成请求账本行。

        口径 7：``state`` 合法值受控；``completed`` 必须有 ``result`` + ``finished_s``；
        非 ``completed`` 不得带 ``result``。``request_hash`` **不唯一**（重新生成允许同 hash 新 id）。

        Args:
            run_id: 账本ID（= 客户端 generation_request_id）。
            schema_version: 账本 schema 版本。
            request_hash: 请求规范化 hash（可重复）。
            request_payload: 规范化请求（整对象，不含 secret）。
            state: running/completed/failed/cancelled/interrupted。
            now_s: 创建时刻；缺省取注入时钟。
            **fields: 其余列。

        Returns:
            TopicGenerationRun

        Raises:
            ValueError: ``state`` 非法或 payload 口径不满足。
        """
        self._require_enum(state, GENERATION_STATES, "invalid_generation_state")
        now = self._resolve_now(now_s)
        fields.setdefault("created_s", now)
        self._validate_generation_payload(state, fields.get("result"), fields.get("finished_s"))
        return self._insert(
            TopicGenerationRun(
                id=run_id,
                schema_version=schema_version,
                request_hash=request_hash,
                request_payload=request_payload,
                state=state,
                **fields,
            )
        )

    def get_by_id(self, run_id: str) -> Optional[TopicGenerationRun]:
        """``topic_generation_runs`` 幂等读入口（本批只读；claim/complete 归 3e）。

        Args:
            run_id: 账本ID。

        Returns:
            TopicGenerationRun | None
        """
        return self._fetch(TopicGenerationRun, run_id)

    def update_generation_run(self, run_id: str, **fields: Any) -> TopicGenerationRun:
        """按主键更新生成账本行（JSON 列整对象赋值）。

        Args:
            run_id: 账本ID。
            **fields: 要更新的列。

        Returns:
            TopicGenerationRun

        Raises:
            KeyError: 记录不存在。
            ValueError: ``state`` 非法或 payload 口径不满足。
        """
        session = self._session_factory()
        try:
            row = session.get(TopicGenerationRun, run_id)
            if row is None:
                raise KeyError(f"topic_generation_runs_not_found:{run_id!r}")
            for name, value in fields.items():
                self._assign(row, name, value)
            self._require_enum(row.state, GENERATION_STATES, "invalid_generation_state")
            self._validate_generation_payload(row.state, row.result, row.finished_s)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


__all__ = ["HotEventRepository", "SessionFactory", "Clock", "FeedbackRevisionConflict"]
