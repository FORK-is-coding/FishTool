"""FishTool 04 · 第三批 b：发现围栏服务（claim / finish / invalidate + 异步调度）。

依据：
- ``FishTool_04_..._02补充执行案(1).md`` §6.2.1（共用领取入口、默认 lease=300s / 硬
  deadline=120s、manual cooldown=60s、预算不绕过、409 ``discovery_in_progress``）、
  §6.2.2（提交围栏、先失效 DB token 再取消 task、迟到结果不落业务、崩溃恢复）；
- ``FishTool_04_R5执行规格_第三批b_事件归属与发现围栏.md`` §1.2（D01—D07）。

本模块**不接真·外部发现源、不写真实 B 站调用**（归 3c）：外部来源以可注入的
``fetch_fn`` 表达，测试只 mock 它，围栏的 claim/finish/落库全部走真实临时 SQLite。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from sqlalchemy.orm import Session

from core.database.event_discovery_repository import (
    Clock,
    DiscoverySuperseded,
    EventDiscoveryRepository,
    InvalidateOutcome,
)

__all__ = [
    "DiscoveryFenceError",
    "DiscoveryInProgress",
    "DiscoveryNotDue",
    "DiscoveryBudgetUnavailable",
    "DiscoveryCapacityExceeded",
    "DiscoveryStoreUnavailable",
    "DiscoveryFetchResult",
    "DiscoveryStartResult",
    "EventDiscoveryFence",
]

SessionFactory = Callable[[], Session]

#: 外部发现来源签名（可注入 / 可 mock）：返回本轮结果。
FetchFn = Callable[[str, int, str], Any]


class DiscoveryFenceError(Exception):
    """发现围栏服务错误的基类，携带稳定 ``code``。"""

    code: str = "discovery_fence_error"

    def __init__(self, message: str = "", **extra: Any) -> None:
        """初始化错误。

        Args:
            message: 人读信息。
            **extra: 附加上下文（run_id / retry_after_s 等），供上层转 409/429。
        """
        super().__init__(message or self.code)
        self.message = message or self.code
        self.extra = extra


class DiscoveryNotDue(DiscoveryFenceError):
    """未到期 / 未过 cooldown / 规则或策略已变等：不创建可执行 run。"""

    code: str = "discovery_not_due"


class DiscoveryInProgress(DiscoveryFenceError):
    """手动触发遇到正在运行的发现 → 409 ``discovery_in_progress`` + 当前 run_id（D01）。"""

    code: str = "discovery_in_progress"


class DiscoveryBudgetUnavailable(DiscoveryFenceError):
    """预算当前不可授予 → 429 + retry_after，**不先创建会无限排队的 running run**（D07）。"""

    code: str = "discovery_budget_unavailable"


class DiscoveryCapacityExceeded(DiscoveryFenceError):
    """active 事件容量已满：不创建 run，不发外部请求（§6.3）。"""

    code: str = "active_capacity_exceeded"


class DiscoveryStoreUnavailable(DiscoveryFenceError):
    """存储不可用 → 503，且不宣称已接受发现任务（§6.2.2）。"""

    code: str = "discovery_store_unavailable"


@dataclass
class DiscoveryFetchResult:
    """一轮发现的外部结果（3c 会真正填充；本批测试里由 mock 提供）。"""

    candidates: list = field(default_factory=list)
    counters: Mapping[str, Any] = field(default_factory=dict)
    new_bvids: list = field(default_factory=list)
    member_decisions: list = field(default_factory=list)
    source_attempts: list = field(default_factory=list)
    final_status: str = "completed"
    error_code: Optional[str] = None


@dataclass
class DiscoveryStartResult:
    """``start_discovery`` 的返回。"""

    started: bool
    run_id: Optional[str]
    trigger: str
    lease_token: Optional[str] = None
    deadline_s: Optional[int] = None
    reason: str = ""
    current_run_id: Optional[str] = None


class EventDiscoveryFence:
    """发现围栏服务：手动与后台发现**共用同一条领取入口**（§6.2.1）。"""

    def __init__(
        self,
        *,
        repository: EventDiscoveryRepository,
        session_factory: SessionFactory,
        fetch_fn: FetchFn,
        clock: Optional[Clock] = None,
        lease_seconds: int = 300,
        deadline_seconds: int = 120,
        manual_cooldown_seconds: int = 60,
        due_interval_seconds: int = 7200,
        max_active_events: int = 5,
        budget_acquire: Optional[Callable[[int], bool]] = None,
        reconcile: Optional[Callable[[Session], Any]] = None,
    ) -> None:
        """初始化围栏服务。

        Args:
            repository: 发现围栏仓储原语。
            session_factory: 会话工厂（绑定临时/生产库）。
            fetch_fn: 外部发现来源（可注入 / 可 mock，本批不接真实来源）。
            clock: 秒级时钟。
            lease_seconds: 发现租约时长（默认 300s）。
            deadline_seconds: 领取后整轮硬 deadline（默认 120s，必须 < lease）。
            manual_cooldown_seconds: 手动触发最短间隔（默认 60s）。
            due_interval_seconds: 下次发现间隔（默认 2h）。
            max_active_events: active 事件容量（默认 5）。
            budget_acquire: 预算授予回调 ``now_s -> bool``（None 表示不设限）。
            reconcile: flush-only 需求对账回调（3c 实现；本批只保证同事务）。
        """
        if deadline_seconds >= lease_seconds:
            raise ValueError("deadline_must_be_less_than_lease")
        self._repo = repository
        self._session_factory = session_factory
        self._fetch_fn = fetch_fn
        self._clock: Clock = clock or (lambda: int(time.time()))
        self._lease_seconds = int(lease_seconds)
        self._deadline_seconds = int(deadline_seconds)
        self._manual_cooldown_seconds = int(manual_cooldown_seconds)
        self._due_interval_seconds = int(due_interval_seconds)
        self._max_active_events = int(max_active_events)
        self._budget_acquire = budget_acquire
        self._reconcile = reconcile
        self._tasks: dict[str, asyncio.Task] = {}

    # --------------------------------------------------------------- 内部工具

    def _now(self, now_s: Optional[int]) -> int:
        """归一当前时刻为 epoch 秒。"""
        if now_s is None:
            return int(self._clock())
        if type(now_s) is bool or not isinstance(now_s, int):
            raise ValueError("now_s_must_be_int")
        return int(now_s)

    def _new_run_id(self) -> str:
        """生成发现 run ID。"""
        return f"run_{uuid.uuid4().hex}"

    def _new_token(self) -> str:
        """生成本轮随机 lease token（请求方不能伪造）。"""
        return f"tok_{uuid.uuid4().hex}"

    def _capacity_ok(self, now_s: int) -> bool:
        """检查 active 事件容量（不持 lease、不占事务）。"""
        session = self._session_factory()
        try:
            return self._repo.active_event_count(session) <= self._max_active_events
        finally:
            session.close()

    def _budget_ok(self, now_s: int) -> bool:
        """检查逻辑预算是否可授予（等待时**不持 lease / DB 事务**）。"""
        if self._budget_acquire is None:
            return True
        return bool(self._budget_acquire(now_s))

    # ======================================================== start（D01/D07）

    async def start_discovery(
        self,
        event_id: str,
        *,
        trigger: str,
        rule_version: int,
        source_policy_hash: str,
        now_s: Optional[int] = None,
        deadline_s: Optional[int] = None,
    ) -> DiscoveryStartResult:
        """手动 / 后台发现共用领取入口：先容量+预算，再短事务 CAS，成功才起任务。

        Args:
            event_id: 事件 ID。
            trigger: scheduled/manual（由服务端设定，请求方不能伪造）。
            rule_version: 冻结规则版本。
            source_policy_hash: 冻结策略 hash。
            now_s: 当前时刻；缺省取注入时钟。
            deadline_s: 本轮硬 deadline（缺省用配置值，且必须 < lease）。

        Returns:
            DiscoveryStartResult: 已启动 / 后台竞争失败跳过。

        Raises:
            ValueError: ``trigger`` 非法或 deadline >= lease。
            DiscoveryCapacityExceeded: active 事件容量已满。
            DiscoveryBudgetUnavailable: 预算不可授予（429）。
            DiscoveryInProgress: 手动触发遇到正在运行（409，带 run_id）。
            DiscoveryNotDue: 其它未到期情形。
            DiscoveryStoreUnavailable: 存储不可用（503）。
        """
        now = self._now(now_s)
        if trigger not in ("scheduled", "manual"):
            raise ValueError(f"invalid_trigger:{trigger}")
        deadline = int(deadline_s) if deadline_s is not None else self._deadline_seconds
        if deadline >= self._lease_seconds:
            raise ValueError("deadline_must_be_less_than_lease")

        # 1) 轻量容量检查（active 事件数）。
        if not self._capacity_ok(now):
            raise DiscoveryCapacityExceeded("active_capacity_exceeded", limit=self._max_active_events)
        # 2) 预算检查（等待时不持 lease / 事务）；预算不可绕过。
        if not self._budget_ok(now):
            raise DiscoveryBudgetUnavailable(
                "discovery_budget_unavailable",
                retry_after_s=self._manual_cooldown_seconds,
            )

        run_id = self._new_run_id()
        token = self._new_token()
        session = self._session_factory()
        try:
            outcome = self._repo.claim_discovery(
                session,
                event_id=event_id,
                run_id=run_id,
                lease_token=token,
                trigger=trigger,
                now_s=now,
                rule_version=rule_version,
                source_policy_hash=source_policy_hash,
                lease_seconds=self._lease_seconds,
                manual_cutoff_s=now - self._manual_cooldown_seconds,
            )
            if not outcome.claimed:
                session.rollback()
                if outcome.reason == "in_progress":
                    if trigger == "manual":
                        # D01：手动返回 409 + 当前 run_id；失败方无任何外部调用。
                        raise DiscoveryInProgress(
                            "discovery_in_progress",
                            run_id=outcome.current_run_id,
                        )
                    # 后台竞争失败则跳过：不创建可执行 run，不发请求。
                    return DiscoveryStartResult(
                        started=False,
                        run_id=None,
                        trigger=trigger,
                        reason="in_progress",
                        current_run_id=outcome.current_run_id,
                    )
                raise DiscoveryNotDue(outcome.reason)
            session.commit()
        except (DiscoveryInProgress, DiscoveryNotDue):
            session.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - 存储故障须显式上报，不静默
            session.rollback()
            raise DiscoveryStoreUnavailable("discovery_store_unavailable") from exc
        finally:
            session.close()

        # 领取成功：起后台任务执行网络（mock）+ 提交；任务持 token，提交受 deadline/lease 约束。
        task = asyncio.ensure_future(
            self._execute(run_id, token, event_id, rule_version, source_policy_hash, deadline)
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: self._tasks.pop(rid, None))
        return DiscoveryStartResult(
            started=True,
            run_id=run_id,
            trigger=trigger,
            lease_token=token,
            deadline_s=deadline,
        )

    async def _execute(
        self,
        run_id: str,
        token: str,
        event_id: str,
        rule_version: int,
        source_policy_hash: str,
        deadline: int,
    ) -> None:
        """执行一轮：网络（mock）受硬 deadline 限制，完成后同一事务提交围栏。"""
        try:
            raw = await asyncio.wait_for(
                self._fetch_fn(event_id, rule_version, source_policy_hash),
                timeout=deadline,
            )
        except asyncio.CancelledError:
            # 被 invalidate/shutdown 取消：DB token 已先失效，绝不落业务。
            raise
        except asyncio.TimeoutError:
            # D07：有界 deadline 生效，只释放自己仍持有的 lease 并退避。
            self._finish_error(run_id, token, event_id, "deadline_exceeded")
            return
        except Exception as exc:  # noqa: BLE001 - 外部来源失败：记错误、释放自己的 lease
            self._finish_error(run_id, token, event_id, f"fetch_error:{type(exc).__name__}")
            return

        result = self._normalize_result(raw)
        try:
            self._commit(run_id, token, event_id, rule_version, source_policy_hash, result)
        except DiscoverySuperseded:
            # D02/D03/D04：已失去所有权或规则/策略已变 → 不写成员、不覆盖别人的 lease。
            return
        except Exception as exc:  # noqa: BLE001 - 存储失败：整事务已回滚，不宣称成功
            self._finish_error(run_id, token, event_id, f"commit_error:{type(exc).__name__}")

    @staticmethod
    def _normalize_result(raw: Any) -> DiscoveryFetchResult:
        """把 mock / 真来源的返回值归一为 :class:`DiscoveryFetchResult`。"""
        if isinstance(raw, DiscoveryFetchResult):
            return raw
        if isinstance(raw, Mapping):
            return DiscoveryFetchResult(
                candidates=list(raw.get("candidates", [])),
                counters=dict(raw.get("counters", {})),
                new_bvids=list(raw.get("new_bvids", [])),
                member_decisions=list(raw.get("member_decisions", [])),
                source_attempts=list(raw.get("source_attempts", [])),
                final_status=str(raw.get("final_status", "completed")),
                error_code=raw.get("error_code"),
            )
        return DiscoveryFetchResult()

    def _commit(
        self,
        run_id: str,
        token: str,
        event_id: str,
        rule_version: int,
        source_policy_hash: str,
        result: DiscoveryFetchResult,
    ) -> bool:
        """用独立会话把一轮结果提交进围栏（同一个短事务；失败整体回滚）。"""
        session = self._session_factory()
        try:
            self._repo.commit_discovery(
                session,
                run_id=run_id,
                lease_token=token,
                event_id=event_id,
                rule_version=rule_version,
                source_policy_hash=source_policy_hash,
                now_s=self._now(None),
                final_status=result.final_status,
                error_code=result.error_code,
                candidates=result.candidates,
                counters=result.counters,
                newly_discovered_bvids=result.new_bvids,
                source_attempts=result.source_attempts,
                member_decisions=result.member_decisions,
                reconcile=self._reconcile,
                due_interval_s=self._due_interval_seconds,
            )
            session.commit()
            return True
        except DiscoverySuperseded:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _finish_error(self, run_id: str, token: str, event_id: str, error_code: str) -> None:
        """错误/超时收尾（只释放自己仍持有的 lease；失去所有权只记安全诊断）。"""
        session = self._session_factory()
        try:
            self._repo.finish_discovery_error(
                session,
                run_id=run_id,
                lease_token=token,
                event_id=event_id,
                error_code=error_code,
                now_s=self._now(None),
                retry_backoff_s=self._manual_cooldown_seconds,
            )
            session.commit()
        except Exception:  # noqa: BLE001 - 收尾失败不应再抛进事件循环
            session.rollback()
        finally:
            session.close()

    # ==================================================== invalidate / 规则变更

    async def invalidate(
        self,
        event_id: str,
        *,
        reason: str,
        expected_lease_token: Optional[str] = None,
        cancel_tasks: bool = True,
        now_s: Optional[int] = None,
    ) -> InvalidateOutcome:
        """D02：**先使 DB token 失效，再（最佳努力）取消 task**；迟到结果 commit 必失败。

        Args:
            event_id: 事件 ID。
            reason: 失效原因（event_paused / event_archived / explicit_cancel / ...）。
            expected_lease_token: 仅当 token 匹配才失效。
            cancel_tasks: 失效后是否取消本进程 task（默认 True；测试可用 False 单验 DB 围栏）。
            now_s: 当前时刻。

        Returns:
            InvalidateOutcome: 失效结果。
        """
        session = self._session_factory()
        try:
            outcome = self._repo.invalidate_discovery(
                session,
                event_id=event_id,
                now_s=self._now(now_s),
                reason=reason,
                expected_lease_token=expected_lease_token,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        # 顺序要点：DB token 已先失效，此处才取消协程（迟到的网络结果无法落业务）。
        if cancel_tasks and outcome.cancelled_run_id:
            self._cancel_task(outcome.cancelled_run_id)
        return outcome

    async def change_rule(
        self,
        event_id: str,
        *,
        rule_version: int,
        source_policy_hash: str,
        rule_config: Optional[Mapping[str, Any]] = None,
        now_s: Optional[int] = None,
        cancel_tasks: bool = True,
    ) -> InvalidateOutcome:
        """D03：同一事务更新规则版本 + 失效旧发现 run；新任务用新版本。

        Args:
            event_id: 事件 ID。
            rule_version: 新规则版本。
            source_policy_hash: 新策略 hash。
            rule_config: 规则配置快照。
            now_s: 当前时刻。
            cancel_tasks: 失效后是否取消本进程 task（测试可传 False 单验 DB 围栏）。
        """
        session = self._session_factory()
        try:
            outcome = self._repo.update_event_rule(
                session,
                event_id=event_id,
                rule_version=rule_version,
                source_policy_hash=source_policy_hash,
                now_s=self._now(now_s),
                rule_config=rule_config,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        if cancel_tasks and outcome.cancelled_run_id:
            self._cancel_task(outcome.cancelled_run_id)
        return outcome

    def _cancel_task(self, run_id: str) -> None:
        """取消本进程内某 run 的 task（不 await；DB 围栏已保证安全）。"""
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()

    async def wait_for_tasks(self) -> None:
        """等待当前所有已起 task 自然结束（用于测试观察迟到结果是否被围栏挡住）。"""
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        """D07：关停时先失效自己的 token（由调用方保证），再取消并 await 全部 task。"""
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def pending_run_ids(self) -> list[str]:
        """当前进程仍在跑的 run ID（供测试观察失联状态）。"""
        return [rid for rid, task in self._tasks.items() if not task.done()]
