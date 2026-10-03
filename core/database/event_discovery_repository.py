"""FishTool 04 · 第三批 b：发现围栏仓储原语（claim / commit / finish / invalidate）。

依据：
- ``FishTool_04_话题级热点研判与选题决策_02补充执行案(1).md``
  §6.2.1 L581-604（共用领取入口、CAS 领取 SQL）、§6.2.2 L606-619（提交围栏、迟到结果、
  错误/超时只释放同 token 的 lease、崩溃恢复）；
- ``FishTool_04_R5执行规格_第三批b_事件归属与发现围栏.md`` §1.2（D01—D07）。

设计口径（逐条对齐上游，**不写真实外部发现**）：
- 所有公开方法 **接受调用方 session、只 flush，不 commit**；由服务层在同一短事务内
  commit / rollback（存储失败必须整事务回滚，见 D05）；
- 领取 = 一个短事务：先 CAS 改 ``hot_events``（取写锁），``rowcount==1`` 才 INSERT
  ``running`` run（D01：仅一方成功，失败方不产生可执行 run）；
- 领取时把 ``rule_version`` / ``source_policy_hash`` 冻结进 run（D03）；
- 提交围栏谓词同时含 event_id / status=active / lease_token / lease_until_s>now /
  active_discovery_run_id / 冻结 rule_version / 冻结 source_policy_hash，``rowcount!=1``
  抛 :class:`DiscoverySuperseded` 并禁止写成员/需求（D04）；
- 提交时**读取当前成员 latest revision**，不使用网络开始前的旧缓存；人工 rejected 不得被
  自动规则复活（D06）；
- 失效（invalidating）在规则变更 / 暂停 / 归档 / 显式取消的**同一事务**里清 token/run 引用并
  把旧 run 标 cancelled —— 先使 DB token 失效，之后迟到的结果自然 commit 失败（D02/D03）；
- 错误/超时只允许**持有同 token 的 worker** 释放自己的 lease 并设退避；失去所有权者绝不
  清理新 worker 的 lease，至多对自身旧 run 记安全诊断（D04）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session

from .models_hot_event import EventDiscoveryRun, HotEvent, HotEventMember

__all__ = [
    "DiscoveryError",
    "DiscoverySuperseded",
    "ClaimOutcome",
    "CommitOutcome",
    "FinishOutcome",
    "InvalidateOutcome",
    "EventDiscoveryRepository",
]

#: 时钟签名：无参调用返回 epoch 秒（int）。
Clock = Callable[[], int]

#: 发现 run 的「可执行 / 终态」四因（与 models_hot_event 口径一致，仅作常量引用说明）。
_RUNNING: str = "running"


class DiscoveryError(Exception):
    """发现围栏相关错误的基类，携带稳定 ``code``。"""

    code: str = "discovery_error"

    def __init__(self, message: str = "", **extra: Any) -> None:
        """初始化错误。

        Args:
            message: 人读信息。
            **extra: 附加上下文（如 run_id / reason），供上层转 409/429。
        """
        super().__init__(message or self.code)
        self.message = message or self.code
        self.extra = extra


class DiscoverySuperseded(DiscoveryError):
    """D04：迟到 / 失去所有权的发现结果不得业务提交。"""

    code: str = "discovery_superseded"


@dataclass
class ClaimOutcome:
    """领取结果。"""

    claimed: bool
    reason: str = ""
    run: Optional[EventDiscoveryRun] = None
    current_run_id: Optional[str] = None
    event_revision: Optional[int] = None


@dataclass
class CommitOutcome:
    """提交结果。"""

    committed: bool
    run_id: str
    member_revisions: list[int] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


@dataclass
class FinishOutcome:
    """错误 / 超时收尾结果。"""

    released: bool
    lost: bool
    run_id: str


@dataclass
class InvalidateOutcome:
    """失效结果（清 token/run 引用，可选取消旧 run）。"""

    invalidated: bool
    cancelled_run_id: Optional[str] = None


class EventDiscoveryRepository:
    """发现围栏的数据库原语（caller session + flush-only）。"""

    def __init__(self, clock: Optional[Clock] = None) -> None:
        """初始化仓储。

        Args:
            clock: 秒级时钟；缺省 ``time.time``。
        """
        self._clock: Clock = clock or (lambda: int(time.time()))

    # ------------------------------------------------------------------ 内部工具

    @staticmethod
    def _resolve_now(now_s: Optional[int], clock: Clock) -> int:
        """归一为 epoch 整数秒（显式值优先，其次注入时钟）。"""
        if now_s is None:
            return int(clock())
        if type(now_s) is bool or not isinstance(now_s, int):
            raise ValueError("now_s_must_be_int")
        return int(now_s)

    @staticmethod
    def _latest_member(session: Session, event_id: str, bvid: str) -> Optional[HotEventMember]:
        """读取同 ``(event_id, bvid)`` 的**最新 revision**（D06：绝不用旧缓存）。

        Args:
            session: 调用方会话。
            event_id: 事件 ID。
            bvid: 视频 BV 号。

        Returns:
            HotEventMember | None: 最新一条版本。
        """
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

    def _claim_failure_reason(
        self,
        session: Session,
        *,
        event_id: str,
        now_s: int,
        rule_version: int,
        source_policy_hash: str,
        trigger: str,
        manual_cutoff_s: Optional[int],
    ) -> tuple[str, Optional[str]]:
        """CAS 失败后读取 event，给出稳定 reason 与当前 run_id（供上层转 409/跳过）。

        Args:
            session: 调用方会话。
            event_id: 事件 ID。
            now_s: 当前时刻。
            rule_version: 冻结规则版本。
            source_policy_hash: 冻结策略 hash。
            trigger: scheduled/manual。
            manual_cutoff_s: 手动最短触发间隔的截止时刻。

        Returns:
            tuple[str, Optional[str]]: ``(reason, current_run_id)``。
        """
        event = session.get(HotEvent, event_id)
        if event is None:
            return "event_not_found", None
        current_run_id = event.active_discovery_run_id
        if event.status != "active":
            return "not_active", current_run_id
        if current_run_id and event.lease_until_s is not None and int(event.lease_until_s) > now_s:
            return "in_progress", current_run_id
        if int(event.current_rule_version or 0) != int(rule_version):
            return "rule_changed", current_run_id
        if (event.source_policy_hash or "") != (source_policy_hash or ""):
            return "policy_changed", current_run_id
        if trigger == "manual" and manual_cutoff_s is not None:
            last_attempt = event.last_discovery_attempt_s
            if last_attempt is not None and int(last_attempt) > int(manual_cutoff_s):
                return "manual_cooldown", current_run_id
        if trigger == "scheduled":
            due = event.discovery_due_s
            if due is not None and int(due) > now_s:
                return "not_due", current_run_id
        return "not_claimed", current_run_id

    # ====================================================== claim（§6.2.1）

    def claim_discovery(
        self,
        session: Session,
        *,
        event_id: str,
        run_id: str,
        lease_token: str,
        trigger: str,
        now_s: Optional[int] = None,
        rule_version: int,
        source_policy_hash: str,
        lease_seconds: int = 300,
        manual_cutoff_s: Optional[int] = None,
        supersede_running_runs: bool = True,
    ) -> ClaimOutcome:
        """同一短事务领取一次发现：先 CAS 改 event，``rowcount==1`` 才 INSERT running run。

        Args:
            session: 调用方会话（只 flush，不 commit）。
            event_id: 事件 ID。
            run_id: 本轮新 run ID。
            lease_token: 本轮随机 token（服务端生成，请求方不能伪造）。
            trigger: scheduled/manual。
            now_s: 当前时刻；缺省取注入时钟。
            rule_version: 领取时冻结的规则版本。
            source_policy_hash: 领取时冻结的规范化策略 hash。
            lease_seconds: 租约时长（默认 300s）。
            manual_cutoff_s: 手动最短触发间隔截止时刻（``now - cooldown``）。
            supersede_running_runs: 领取成功时，是否把该 event 旧的 running run 标 interrupted。

        Returns:
            ClaimOutcome: 是否领取成功；失败时带稳定 reason 与当前 run_id。

        Raises:
            ValueError: ``trigger`` 非法。
        """
        if trigger not in ("scheduled", "manual"):
            raise ValueError(f"invalid_trigger:{trigger}")
        now = self._resolve_now(now_s, self._clock)
        lease_until = now + int(lease_seconds)

        # 触发条件：scheduled 需到期；manual 需过最短触发间隔。NULL 一律视为「尚无记录，可触发」。
        if trigger == "scheduled":
            trigger_predicate = or_(
                HotEvent.discovery_due_s.is_(None), HotEvent.discovery_due_s <= now
            )
        else:
            cutoff = manual_cutoff_s if manual_cutoff_s is not None else now
            trigger_predicate = or_(
                HotEvent.last_discovery_attempt_s.is_(None),
                HotEvent.last_discovery_attempt_s <= cutoff,
            )

        stmt = (
            update(HotEvent)
            .where(
                HotEvent.id == event_id,
                HotEvent.status == "active",
                HotEvent.current_rule_version == int(rule_version),
                HotEvent.source_policy_hash == source_policy_hash,
                or_(HotEvent.lease_until_s.is_(None), HotEvent.lease_until_s <= now),
                trigger_predicate,
            )
            .values(
                lease_token=lease_token,
                lease_until_s=lease_until,
                active_discovery_run_id=run_id,
                last_discovery_attempt_s=now,
                revision=HotEvent.revision + 1,
                updated_s=now,
            )
            .execution_options(synchronize_session=False)
        )
        result = session.execute(stmt)
        if result.rowcount != 1:  # 只有一方 CAS 成功（D01）
            reason, current_run_id = self._claim_failure_reason(
                session,
                event_id=event_id,
                now_s=now,
                rule_version=rule_version,
                source_policy_hash=source_policy_hash,
                trigger=trigger,
                manual_cutoff_s=manual_cutoff_s,
            )
            return ClaimOutcome(claimed=False, reason=reason, current_run_id=current_run_id)

        if supersede_running_runs:
            # 旧 lease 过期后成功新领取：同事务把旧的 running run 标 interrupted（不接管旧 run）。
            for stale in (
                session.query(EventDiscoveryRun)
                .filter(
                    EventDiscoveryRun.event_id == event_id,
                    EventDiscoveryRun.status == _RUNNING,
                    EventDiscoveryRun.id != run_id,
                )
                .all()
            ):
                stale.status = "interrupted"
                stale.finished_s = now
                stale.error_code = stale.error_code or "superseded_by_new_claim"

        run = EventDiscoveryRun(
            id=run_id,
            event_id=event_id,
            rule_version=int(rule_version),
            source_policy_hash=source_policy_hash,
            lease_token=lease_token,
            trigger=trigger,
            started_s=now,
            status=_RUNNING,
        )
        session.add(run)
        session.flush()
        event = session.get(HotEvent, event_id)
        return ClaimOutcome(
            claimed=True,
            reason="claimed",
            run=run,
            current_run_id=run_id,
            event_revision=int(event.revision) if event is not None else None,
        )

    # ====================================================== commit（§6.2.2）

    def commit_discovery(
        self,
        session: Session,
        *,
        run_id: str,
        lease_token: str,
        event_id: str,
        rule_version: int,
        source_policy_hash: str,
        now_s: Optional[int] = None,
        final_status: str = "completed",
        error_code: Optional[str] = None,
        candidates: Optional[Sequence[Any]] = None,
        counters: Optional[Mapping[str, Any]] = None,
        newly_discovered_bvids: Optional[Sequence[str]] = None,
        source_attempts: Optional[Sequence[Any]] = None,
        member_decisions: Optional[Sequence[Mapping[str, Any]]] = None,
        reconcile: Optional[Callable[[Session], Any]] = None,
        due_interval_s: int = 7200,
        release_lease: bool = True,
    ) -> CommitOutcome:
        """提交围栏：一个短事务内完成 CAS 取写锁 → 写 run 终态 + 成员 → reconcile → 清 lease。

        任何一步抛错（含 ``reconcile`` 故障）→ 由调用方 rollback，**整事务回滚**（D05）。

        Args:
            session: 调用方会话（只 flush，不 commit）。
            run_id: 本轮 run ID。
            lease_token: 本轮 token（必须与 event 当前 token 一致）。
            event_id: 事件 ID。
            rule_version: 冻结规则版本（必须与 event 当前一致）。
            source_policy_hash: 冻结策略 hash（必须与 event 当前一致）。
            now_s: 提交时刻；缺省取注入时钟。
            final_status: run 终态（completed/partial/failed/...）。
            error_code: 终态错误码。
            candidates: 本轮候选（上限 100，bvid 去重）。
            counters: 计数 / empty_reason 等。
            newly_discovered_bvids: 本轮新增 bvid。
            source_attempts: 来源尝试明细。
            member_decisions: 成员决定（``{bvid,status,first_seen_s,rule_version,
                decision_source,evidence,raw_tid,published_epoch_s,owner_mid}``）。
            reconcile: flush-only 需求对账回调（3c 负责具体实现，本批只保证同事务）。
            due_interval_s: 下次发现间隔（默认 2h）。
            release_lease: 是否在提交末尾清 lease（默认 True）。

        Returns:
            CommitOutcome: 提交结果（含新增成员 revision 与被跳过的成员）。

        Raises:
            DiscoverySuperseded: CAS 谓词不满足（迟到 / 规则或策略变更 / 暂停归档）。
        """
        now = self._resolve_now(now_s, self._clock)

        # (1) 条件 UPDATE 取写锁；谓词同时含 token 与规则语义（D03/D04）。
        lock_stmt = (
            update(HotEvent)
            .where(
                HotEvent.id == event_id,
                HotEvent.status == "active",
                HotEvent.active_discovery_run_id == run_id,
                HotEvent.lease_token == lease_token,
                HotEvent.lease_until_s > now,
                HotEvent.current_rule_version == int(rule_version),
                HotEvent.source_policy_hash == source_policy_hash,
            )
            .values(
                lease_token=lease_token,  # 自持 token，取得写锁
                revision=HotEvent.revision + 1,
                updated_s=now,
            )
            .execution_options(synchronize_session=False)
        )
        lock_result = session.execute(lock_stmt)
        if lock_result.rowcount != 1:
            # D04：迟到结果不得写成员/需求或释放新 lease；一律回滚。
            raise DiscoverySuperseded(
                "commit_predicate_failed",
                run_id=run_id,
                event_id=event_id,
            )

        # (2) 读取当前成员 latest revision（绝不用网络开始前的旧缓存，D06）。
        member_revisions: list[int] = []
        skipped: list[dict] = []
        seen_bvids: set[str] = set()
        for decision in member_decisions or []:
            bvid = str(decision.get("bvid", ""))
            if not bvid:
                continue
            if bvid in seen_bvids:
                # §17-4：重复 BVID 入同一事件 → 事件内去重，只留一条成员。
                skipped.append({"bvid": bvid, "reason": "duplicate_bvid_in_batch"})
                continue
            seen_bvids.add(bvid)
            status = str(decision.get("status", "proposed"))
            latest = self._latest_member(session, event_id, bvid)
            if latest is not None and latest.decision_source == "manual" and latest.status in (
                "accepted",
                "rejected",
            ):
                # 人工决定优先：rejected 不复活；accepted 不重复追加。
                skipped.append(
                    {
                        "bvid": bvid,
                        "reason": (
                            "manual_rejected_preserved"
                            if latest.status == "rejected"
                            else "already_accepted_manual"
                        ),
                        "latest_revision": int(latest.revision),
                    }
                )
                continue
            next_revision = int(latest.revision) + 1 if latest is not None else 1
            session.add(
                HotEventMember(
                    event_id=event_id,
                    bvid=bvid,
                    revision=next_revision,
                    status=status,
                    first_seen_s=int(decision.get("first_seen_s", now)),
                    decision_at_s=now,  # 服务端实际提交时刻
                    rule_version=int(decision.get("rule_version", rule_version)),
                    decision_source=str(decision.get("decision_source", "strict_rule")),
                    raw_tid=decision.get("raw_tid"),
                    published_epoch_s=decision.get("published_epoch_s"),
                    owner_mid=decision.get("owner_mid"),
                    evidence=decision.get("evidence"),
                )
            )
            member_revisions.append(next_revision)

        # (3) 原子写 run 终态（含候选/计数/四因）。
        run = session.get(EventDiscoveryRun, run_id)
        if run is not None:
            run.status = final_status
            run.finished_s = now
            run.error_code = error_code
            run.candidates = list(candidates) if candidates is not None else run.candidates
            run.counters = dict(counters) if counters is not None else run.counters
            run.newly_discovered_bvids = (
                list(newly_discovered_bvids)
                if newly_discovered_bvids is not None
                else run.newly_discovered_bvids
            )
            run.source_attempts = (
                list(source_attempts) if source_attempts is not None else run.source_attempts
            )
        session.flush()

        # (3') 需求对账：flush-only，故障必须让整事务回滚（D05）。
        if reconcile is not None:
            reconcile(session)

        # (4) 清本 event 的 lease/token/run 引用并设下次 due（与提交同一次 commit）。
        if release_lease:
            clear_stmt = (
                update(HotEvent)
                .where(
                    HotEvent.id == event_id,
                    HotEvent.active_discovery_run_id == run_id,
                    HotEvent.lease_token == lease_token,
                )
                .values(
                    lease_token=None,
                    lease_until_s=None,
                    active_discovery_run_id=None,
                    discovery_due_s=now + int(due_interval_s),
                    revision=HotEvent.revision + 1,
                    updated_s=now,
                )
                .execution_options(synchronize_session=False)
            )
            session.execute(clear_stmt)
        session.flush()
        return CommitOutcome(committed=True, run_id=run_id, member_revisions=member_revisions, skipped=skipped)

    # ============================================== finish（错误 / 超时收尾）

    def finish_discovery_error(
        self,
        session: Session,
        *,
        run_id: str,
        lease_token: str,
        event_id: str,
        error_code: str,
        now_s: Optional[int] = None,
        retry_backoff_s: int = 300,
        release: bool = True,
    ) -> FinishOutcome:
        """错误/超时收尾：**只释放自己仍是 owner 的 lease**；失去所有权只记安全诊断。

        Args:
            session: 调用方会话（只 flush，不 commit）。
            run_id: 本轮 run ID。
            lease_token: 本轮 token。
            event_id: 事件 ID。
            error_code: 稳定错误码（如 deadline_exceeded）。
            now_s: 当前时刻；缺省取注入时钟。
            retry_backoff_s: 退避秒数。
            release: 是否尝试释放 lease。

        Returns:
            FinishOutcome: ``released`` 表示确实释放了自己持有的 lease；``lost`` 表示已失去所有权。
        """
        now = self._resolve_now(now_s, self._clock)
        released = False
        if release:
            stmt = (
                update(HotEvent)
                .where(
                    HotEvent.id == event_id,
                    HotEvent.active_discovery_run_id == run_id,
                    HotEvent.lease_token == lease_token,
                )
                .values(
                    lease_token=None,
                    lease_until_s=None,
                    active_discovery_run_id=None,
                    discovery_due_s=now + int(retry_backoff_s),
                    last_discovery_error_code=error_code,
                    revision=HotEvent.revision + 1,
                    updated_s=now,
                )
                .execution_options(synchronize_session=False)
            )
            result = session.execute(stmt)
            released = result.rowcount == 1

        # 安全诊断：仅当自身旧 run 仍 running 且 token 匹配时才补终态；绝不改别人的 run。
        run = session.get(EventDiscoveryRun, run_id)
        if run is not None and run.status == _RUNNING and run.lease_token == lease_token:
            run.status = "failed" if released else "interrupted"
            run.finished_s = now
            run.error_code = error_code
        session.flush()
        return FinishOutcome(released=released, lost=not released, run_id=run_id)

    # ============================================================ invalidate

    def invalidate_discovery(
        self,
        session: Session,
        *,
        event_id: str,
        now_s: Optional[int] = None,
        reason: str = "event_paused",
        expected_lease_token: Optional[str] = None,
        cancel_run: bool = True,
    ) -> InvalidateOutcome:
        """D02/D03：先使 DB token 失效（清 token/run 引用），可选把旧 run 标 cancelled。

        必须在暂停 / 归档 / 改规则 / 显式取消的**修改事务中**调用 —— 之后本进程才取消 task，
        迟到结果因谓词不满足而 commit 失败。

        Args:
            session: 调用方会话（只 flush，不 commit）。
            event_id: 事件 ID。
            now_s: 当前时刻；缺省取注入时钟。
            reason: 失效原因（event_paused / rule_changed / ...）。
            expected_lease_token: 仅当 token 匹配才失效（``None`` 表示无条件）。
            cancel_run: 是否把旧 run 标 cancelled。

        Returns:
            InvalidateOutcome: 是否失效成功 + 被取消的旧 run ID。
        """
        now = self._resolve_now(now_s, self._clock)
        event = session.get(HotEvent, event_id)
        if event is None:
            return InvalidateOutcome(False, None)
        active_run_id = event.active_discovery_run_id

        stmt = update(HotEvent).where(HotEvent.id == event_id)
        if expected_lease_token is not None:
            stmt = stmt.where(HotEvent.lease_token == expected_lease_token)
        stmt = stmt.values(
            lease_token=None,
            lease_until_s=None,
            active_discovery_run_id=None,
            revision=HotEvent.revision + 1,
            updated_s=now,
        ).execution_options(synchronize_session=False)
        result = session.execute(stmt)
        if result.rowcount != 1:
            session.flush()
            return InvalidateOutcome(False, active_run_id)

        if cancel_run and active_run_id:
            run = session.get(EventDiscoveryRun, active_run_id)
            if run is not None and run.status == _RUNNING:
                run.status = "cancelled"
                run.finished_s = now
                run.error_code = reason
        session.flush()
        return InvalidateOutcome(True, active_run_id)

    # ========================================================= 辅助 / 查询

    def compare_and_swap_event(
        self,
        session: Session,
        *,
        event_id: str,
        expected_revision: int,
        values: Mapping[str, Any],
    ) -> bool:
        """对 ``hot_events`` 做带 ``revision`` 谓词的 CAS（真实 DB 行为，非函数 mock）。

        Args:
            session: 调用方会话（只 flush，不 commit）。
            event_id: 事件 ID。
            expected_revision: 期望版本。
            values: 需要更新的列（不得含主键 / ``revision``）。

        Returns:
            bool: ``rowcount == 1`` 才为 True（并发下只应有一方成功）；成功时 ``revision + 1``。
        """
        # CAS 语义：命中即自增 revision，使后续同期望版本的更新必然失败。
        payload = {k: v for k, v in dict(values).items() if k != "revision"}
        payload["revision"] = HotEvent.revision + 1
        stmt = (
            update(HotEvent)
            .where(HotEvent.id == event_id, HotEvent.revision == int(expected_revision))
            .values(**payload)
            .execution_options(synchronize_session=False)
        )
        return session.execute(stmt).rowcount == 1

    def active_event_count(self, session: Session) -> int:
        """统计当前 ``active`` 事件数（用于 §6.3「最多 5 个 active 事件」类容量门）。"""
        return int(session.query(func.count(HotEvent.id)).filter(HotEvent.status == "active").scalar() or 0)

    def update_event_rule(
        self,
        session: Session,
        *,
        event_id: str,
        rule_version: int,
        source_policy_hash: str,
        now_s: Optional[int] = None,
        rule_config: Optional[Mapping[str, Any]] = None,
        reason: str = "rule_changed",
    ) -> InvalidateOutcome:
        """同一事务内更新规则版本 + 失效旧发现 token/run（D03）。

        Args:
            session: 调用方会话（只 flush，不 commit）。
            event_id: 事件 ID。
            rule_version: 新规则版本。
            source_policy_hash: 新策略 hash。
            now_s: 当前时刻；缺省取注入时钟。
            rule_config: 规则配置快照（写入 ``rule_history``）。
            reason: 失效原因。

        Returns:
            InvalidateOutcome: 失效结果。

        Raises:
            KeyError: 事件不存在。
        """
        now = self._resolve_now(now_s, self._clock)
        event = session.get(HotEvent, event_id)
        if event is None:
            raise KeyError(f"hot_events_not_found:{event_id!r}")
        history = list(event.rule_history or [])
        history.append(
            {
                "version": int(rule_version),
                "config": dict(rule_config or {}),
                "effective_s": now,
                "hash": source_policy_hash,
            }
        )
        event.rule_history = history
        event.current_rule_version = int(rule_version)
        event.source_policy_hash = source_policy_hash
        event.updated_s = now
        event.revision = int(event.revision or 0) + 1
        session.flush()
        return self.invalidate_discovery(session, event_id=event_id, now_s=now, reason=reason)
