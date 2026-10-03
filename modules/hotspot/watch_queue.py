"""watch 池的有界准入队列（FishTool 04 · 第二批 A）。

依据（逐字对齐，不自创）：
- ``FishTool_04_..._02补充执行案`` §6.3 L623「普通watch：02默认1小时；**全部active视频上限
  60**，新增超过上限显示 queued_capacity」；
- ``FishTool_02_...`` §8 L594「队列有预算上限、活跃目标上限、deadline，不能无界增长」；
- §6.4 L637「入队必须检查capacity；不能以成功HTTP返回掩盖实际未入队」。

语义（逐条对齐派单 A）：
- 活跃目标上限 ``active_capacity``（默认 60）：``active=1`` 行数达到上限后，新目标**不再直接
  入池**，而是返回 ``queued_capacity`` 并保留在**有界待入队队列**里；
- 待入队队列有独立上限 ``max_pending``（预算上限）与 ``queue_deadline_s``（deadline）：超过
  队列上限显式返回 ``queue_full``（**不静默丢弃**）；超过 deadline 的排队项在 :meth:`drain`
  时过期移除，不能无界增长；
- 同一 bvid 重复 ``try_admit`` **幂等**：已在池 -> ``existing``；已在队 -> 仍是
  ``queued_capacity``；两者都**不重置** ``next_due_epoch_s`` / ``state_json``（连续窗状态）；
- ``stop_reason='manual_stop'`` 的行（§6.5 L648）**不得自动重开**，``try_admit`` 直接返回
  ``blocked_by_user``；
- 队列只在内存里（与进程内预算同口径：进程重启即清空），**不新增任何表 / 列**。
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from .watch_store import (
    DEFAULT_SAMPLE_INTERVAL_S,
    DEFAULT_TTL_S,
    count_active_watch,
    reactivate_watch,
    upsert_watch,
)

__all__ = [
    "DEFAULT_ACTIVE_WATCH_CAPACITY",
    "DEFAULT_MAX_PENDING",
    "DEFAULT_QUEUE_DEADLINE_S",
    "STATUS_ADMITTED",
    "STATUS_BLOCKED_BY_USER",
    "STATUS_EXISTING",
    "STATUS_QUEUED_CAPACITY",
    "STATUS_QUEUE_FULL",
    "AdmissionResult",
    "WatchPool",
]

#: 普通 watch 的活跃目标上限（04 §6.3 L623：全部 active 视频上限 60）。
DEFAULT_ACTIVE_WATCH_CAPACITY: int = 60

#: 待入队队列的预算上限（条）：队列本身有界，不能无界增长（02 §8 L594）。
DEFAULT_MAX_PENDING: int = 240

#: 待入队项的 deadline（秒）：排队超过该时长仍未入池即过期移除。
DEFAULT_QUEUE_DEADLINE_S: int = 24 * 3600

#: 准入结果码（稳定字符串，供调用方/测试断言）。
STATUS_ADMITTED: str = "admitted"
STATUS_EXISTING: str = "existing"
STATUS_QUEUED_CAPACITY: str = "queued_capacity"
STATUS_QUEUE_FULL: str = "queue_full"
STATUS_BLOCKED_BY_USER: str = "blocked_by_user"


@dataclass(frozen=True)
class AdmissionResult:
    """一次准入尝试的结构化结果。

    Attributes:
        status: ``admitted`` / ``existing`` / ``queued_capacity`` / ``queue_full`` /
            ``blocked_by_user`` 之一。
        bvid: 目标 BV 号。
        reason_code: 附加说明（如 ``manual_stop`` / ``active_capacity`` / ``pending_timeout``）。
        active_count: 本次判定时池内 active 行数。
        pending_count: 本次判定时待入队队列条数。
        retry_after_s: 建议重试间隔（秒）；不适用时为 None。
    """

    status: str
    bvid: str
    reason_code: str | None = None
    active_count: int = 0
    pending_count: int = 0
    retry_after_s: int | None = None


@dataclass
class _PendingEntry:
    """待入队项（内存态，进程重启即清空）。"""

    bvid: str
    requested_epoch_s: int
    kwargs: dict


class WatchPool:
    """watch 池的有界准入控制器：容量检查 + 有界待入队队列 + 幂等。

    一个实例 = 一个进程内的一份队列状态；由 ``WatchService`` 持有（缺省惰性构造）。
    """

    def __init__(
        self,
        *,
        active_capacity: int = DEFAULT_ACTIVE_WATCH_CAPACITY,
        max_pending: int = DEFAULT_MAX_PENDING,
        queue_deadline_s: int = DEFAULT_QUEUE_DEADLINE_S,
    ) -> None:
        """构造准入控制器。

        Args:
            active_capacity: 活跃目标上限（默认 60）。
            max_pending: 待入队队列上限（默认 240）。
            queue_deadline_s: 排队 deadline（秒，默认 24h）。
        """
        self.active_capacity = max(1, int(active_capacity))
        self.max_pending = max(0, int(max_pending))
        self.queue_deadline_s = max(0, int(queue_deadline_s))
        self._pending: "OrderedDict[str, _PendingEntry]" = OrderedDict()

    # ---------------------------------------------------------------- 只读视图

    def pending_bvids(self) -> list[str]:
        """返回当前待入队 bvid 列表（按入队顺序）。"""
        return list(self._pending.keys())

    def is_pending(self, bvid: str) -> bool:
        """该 bvid 是否在待入队队列里。"""
        return str(bvid or "").strip() in self._pending

    # ---------------------------------------------------------------- 准入

    def try_admit(
        self,
        session: Session,
        *,
        bvid: str,
        now_epoch_s: int,
        ttl_end_epoch_s: int | None = None,
        category_key: str | None = None,
        collection_tid: int | None = None,
        discovery_source: str | None = None,
        sample_interval_s: int = DEFAULT_SAMPLE_INTERVAL_S,
        next_due_epoch_s: int | None = None,
    ) -> AdmissionResult:
        """检查容量后准入一个 bvid（幂等）；容量不足时进入有界待入队队列。

        与 ``upsert_watch`` 的分工：**本方法只负责容量闸门**，真正入池仍复用
        ``upsert_watch`` / ``reactivate_watch``，不另写入调度列。

        Args:
            session: 调用方会话；本方法只 flush，提交由调用方完成。
            bvid: 目标 BV 号。
            now_epoch_s: 本次准入时刻（UTC 秒）。
            ttl_end_epoch_s: 入池 deadline（None 时按 ``DEFAULT_TTL_S`` 推导）。
            category_key / collection_tid / discovery_source: 元信息，可为 None。
            sample_interval_s: 采样间隔（秒）。
            next_due_epoch_s: 首采下次应采样时刻（None 时按 ``now + interval``）。

        Returns:
            AdmissionResult: 见各 ``status``。

        Raises:
            ValueError: bvid 为空。
        """
        clean_bvid = str(bvid or "").strip()
        if not clean_bvid:
            raise ValueError("invalid_bvid")
        now_epoch_s = int(now_epoch_s)
        pending_kwargs = {
            "ttl_end_epoch_s": ttl_end_epoch_s,
            "category_key": category_key,
            "collection_tid": collection_tid,
            "discovery_source": discovery_source,
            "sample_interval_s": sample_interval_s,
            "next_due_epoch_s": next_due_epoch_s,
        }

        from core.database import HotspotWatch  # 延迟导入，避免模块导入期建表依赖

        row = session.query(HotspotWatch).filter(HotspotWatch.bvid == clean_bvid).first()
        if row is not None:
            # manual_stop 是持久阻止重开的标记：绝不自动重开（§6.5 L648）。
            if (not bool(row.active)) and row.stop_reason == "manual_stop":
                return AdmissionResult(
                    status=STATUS_BLOCKED_BY_USER,
                    bvid=clean_bvid,
                    reason_code="manual_stop",
                    active_count=count_active_watch(session),
                    pending_count=len(self._pending),
                )
            if bool(row.active):
                # 已在池：幂等，不碰任何调度列（不重置连续窗状态）。
                self._pending.pop(clean_bvid, None)
                return AdmissionResult(
                    status=STATUS_EXISTING,
                    bvid=clean_bvid,
                    active_count=count_active_watch(session),
                    pending_count=len(self._pending),
                )
            # 已释放（expired / events_revoked）：要看容量决定重新入池还是排队。
            active_count = count_active_watch(session)
            if active_count < self.active_capacity:
                interval = self._interval(sample_interval_s)
                reactivate_watch(
                    session,
                    clean_bvid,
                    now_epoch_s=now_epoch_s,
                    next_due_epoch_s=(
                        next_due_epoch_s if next_due_epoch_s is not None else now_epoch_s + interval
                    ),
                    sample_interval_s=interval,
                )
                self._pending.pop(clean_bvid, None)
                return AdmissionResult(
                    status=STATUS_ADMITTED,
                    bvid=clean_bvid,
                    reason_code="reactivated",
                    active_count=active_count + 1,
                    pending_count=len(self._pending),
                )
            return self._enqueue(clean_bvid, now_epoch_s, pending_kwargs, active_count)

        # 全新 bvid。
        active_count = count_active_watch(session)
        if active_count < self.active_capacity:
            interval = self._interval(sample_interval_s)
            ttl = ttl_end_epoch_s if ttl_end_epoch_s is not None else now_epoch_s + DEFAULT_TTL_S
            upsert_watch(
                session,
                bvid=clean_bvid,
                now_epoch_s=now_epoch_s,
                ttl_end_epoch_s=ttl,
                category_key=category_key,
                collection_tid=collection_tid,
                discovery_source=discovery_source,
                sample_interval_s=interval,
                next_due_epoch_s=(
                    next_due_epoch_s if next_due_epoch_s is not None else now_epoch_s + interval
                ),
            )
            self._pending.pop(clean_bvid, None)
            return AdmissionResult(
                status=STATUS_ADMITTED,
                bvid=clean_bvid,
                active_count=active_count + 1,
                pending_count=len(self._pending),
            )
        return self._enqueue(clean_bvid, now_epoch_s, pending_kwargs, active_count)

    # ---------------------------------------------------------------- 排队 / 放行

    def _enqueue(
        self, bvid: str, now_epoch_s: int, kwargs: dict, active_count: int
    ) -> AdmissionResult:
        """把 bvid 放入有界待入队队列（幂等）；队列已满时显式返回 ``queue_full``。"""
        if bvid in self._pending:
            # 已在队：幂等，不刷新入队时刻（否则反复 POST 会无限顺延 deadline）。
            self._pending[bvid].kwargs.update({k: v for k, v in kwargs.items() if v is not None})
            return AdmissionResult(
                status=STATUS_QUEUED_CAPACITY,
                bvid=bvid,
                reason_code="active_capacity",
                active_count=active_count,
                pending_count=len(self._pending),
            )
        if len(self._pending) >= self.max_pending:
            # 队列本身也有界：超上限显式拒绝，绝不静默丢弃。
            return AdmissionResult(
                status=STATUS_QUEUE_FULL,
                bvid=bvid,
                reason_code="pending_full",
                active_count=active_count,
                pending_count=len(self._pending),
                retry_after_s=DEFAULT_SAMPLE_INTERVAL_S,
            )
        self._pending[bvid] = _PendingEntry(bvid=bvid, requested_epoch_s=now_epoch_s, kwargs=dict(kwargs))
        return AdmissionResult(
            status=STATUS_QUEUED_CAPACITY,
            bvid=bvid,
            reason_code="active_capacity",
            active_count=active_count,
            pending_count=len(self._pending),
        )

    def drain(self, session: Session, *, now_epoch_s: int) -> dict:
        """把待入队队列里仍有效的项在有空位时放进池，并淘汰超过 deadline 的项。

        Args:
            session: 调用方会话；只 flush。
            now_epoch_s: 本次放行时刻（UTC 秒）。

        Returns:
            dict: ``{"admitted": [...], "expired": [...], "pending": int, "blocked": [...]}``。
        """
        now_epoch_s = int(now_epoch_s)
        admitted: list[str] = []
        expired: list[str] = []
        blocked: list[str] = []

        # 先淘汰超 deadline 的排队项（队列有 deadline，不能无界增长）。
        for bvid, entry in list(self._pending.items()):
            if now_epoch_s - entry.requested_epoch_s > self.queue_deadline_s:
                self._pending.pop(bvid, None)
                expired.append(bvid)

        for bvid, entry in list(self._pending.items()):
            if count_active_watch(session) >= self.active_capacity:
                break
            interval = self._interval(entry.kwargs.get("sample_interval_s"))
            result = self.try_admit(
                session,
                bvid=bvid,
                now_epoch_s=now_epoch_s,
                ttl_end_epoch_s=entry.kwargs.get("ttl_end_epoch_s"),
                category_key=entry.kwargs.get("category_key"),
                collection_tid=entry.kwargs.get("collection_tid"),
                discovery_source=entry.kwargs.get("discovery_source"),
                sample_interval_s=interval,
                next_due_epoch_s=entry.kwargs.get("next_due_epoch_s"),
            )
            if result.status == STATUS_ADMITTED:
                admitted.append(bvid)
            elif result.status == STATUS_BLOCKED_BY_USER:
                self._pending.pop(bvid, None)
                blocked.append(bvid)
        return {
            "admitted": admitted,
            "expired": expired,
            "blocked": blocked,
            "pending": len(self._pending),
        }

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _interval(sample_interval_s: Any) -> int:
        """把采样间隔规整成正整数秒；非法值回退 ``DEFAULT_SAMPLE_INTERVAL_S``。"""
        if type(sample_interval_s) is int and sample_interval_s > 0:
            return sample_interval_s
        return DEFAULT_SAMPLE_INTERVAL_S
