"""FishTool 04 · 第三批 c：事件 → watch 共享需求映射（04 侧调用方）。

依据：
- ``FishTool_02_热点生命周期_专业方案与Agent执行(1).md`` **§8.3（L674-682 实为 L677-682）**
  「与04的需求合并及快采原子接线」；
- ``FishTool_04_R5执行规格_第三批c_有限外部发现与watch需求映射.md`` §1.2 / §5（E19 / E48 / E49 / E51）。

**本模块只是 04 侧调用方，不重写 ``WatchService.reconcile_demands``。** 钉死口径（§8.3 L677-682）：

1. caller 持有短事务，``reconcile_demands`` **只 flush，不 commit/rollback/close、不发网络**；
2. ``namespace`` 限 ``manual`` / ``ranking`` / ``events``，**只替换该 namespace**，其他一字不动；
3. ``desired`` 是 **events 的完整当前快照**，按 ``event_id`` 分组 —— **不许只传单事件**，
   否则别的事件需求被误撤；
4. **多事件共享一次采样**（采样由 02 统一调度，本层只交需求）；
5. 缺省 04 关闭撤全部 events 需求，``manual`` / ``ranking`` 原需求继续；仅 events 目标停止、保留历史；
6. 已有 lease 若最终需求被撤销，由 02 ``reconcile_demands`` 侧失效 token/active（本层不绕过）；
7. 02 显式 DELETE（``active=False, stop_reason='manual_stop'``）是全局用户停止命令，
   **04 后台 reconcile 见 manual_stop 只能返回 ``blocked_by_user``，不得改 active**（由 02 侧保证）。

对应 E 项：
- **E19** 两事件共享 watch，暂停其一 → 其它需求仍在，不停掉共享采样；
- **E48** 用户手动停止的 bvid，reconcile 不重启、``blocked_by_user`` 可见；
- **E49** 04 关闭后重启，启动清理遗留 events 需求并重算节奏，``manual`` / ``ranking`` 继续、无孤儿快采；
- **E51** 事件 A 更新、B 仍 active → 输入完整 events 映射，只撤 A 不误撤全 namespace。

边界（本批明确不做）：不做 fast panel 冻结 / ``activate_fast_panel`` / 容量 12（第四批）；
不改 02 ``watch_service``（含 ``reconcile_demands`` / ``recover_on_startup``）。

**第四批 e 追加（快采 20 分钟节奏接线，方案 A）**：``build_events_desired`` 在开关
``EVENT_FAST_WATCH_ENABLED`` 打开时，对当前有生效 fast panel 的事件**额外**出一个
``"<event_id>#fast"`` 键（descriptor 带 ``bvids`` + ``interval_s=FAST_WATCH_INTERVAL_S``）。
键后缀本身不产生节奏，节奏只来自 descriptor 内 ``interval_s``；关（默认）时不写 ``#fast``，
快照与既有实现逐字节一致。02 ``watch_demand.py`` 一行不改。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from core.database.models_hot_event import HotEvent, HotEventMember
from modules.hotspot.watch_service import DEMAND_NAMESPACES, WatchService

logger = logging.getLogger(__name__)

__all__ = [
    "EVENTS_NAMESPACE",
    "DEFAULT_MEMBER_STATUSES",
    "FAST_WATCH_CAPACITY",
    "FAST_WATCH_INTERVAL_S",
    "FAST_WATCH_KEY_SUFFIX",
    "EventWatchDemandReconciler",
]

#: 本模块负责的命名空间：只整编 ``events``。
EVENTS_NAMESPACE: str = "events"

#: 视为「有效 panel 成员」的成员状态（只取已接受的成员作为 watch 需求）。
DEFAULT_MEMBER_STATUSES: tuple = ("accepted",)

#: ``#fast`` 需求键后缀。**键后缀本身不产生节奏**——节奏只来自 descriptor 内 ``interval_s``。
FAST_WATCH_KEY_SUFFIX: str = "#fast"

#: 快采采样间隔（秒）：每 20 分钟一次（02 案 §6.3 L624）。**具名常量，禁止散魔法数字**。
FAST_WATCH_INTERVAL_S: int = 1200

#: 快采 panel 全局容量上限（BVID 并集口径；共享 BVID 只占 1，§6.3 L624 / §6.5 L643）。
FAST_WATCH_CAPACITY: int = 12

#: 会话工厂签名：无参调用返回一个 ``Session``。
SessionFactory = Callable[[], Session]


class EventWatchDemandReconciler:
    """04 侧的事件需求对账器：把 active 事件 + 有效成员整编成完整 events 映射，交给 02。

    事务口径（§8.3 L677）：本类所有方法**只 flush**（经 ``reconcile_demands``），
    **不 commit / rollback / close、不发任何网络**；提交由调用方短事务完成。
    """

    def __init__(
        self,
        *,
        watch_service: Optional[WatchService] = None,
        member_statuses: Sequence[str] = DEFAULT_MEMBER_STATUSES,
        clock: Optional[Callable[[], int]] = None,
        fast_watch_enabled: Optional[bool] = None,
    ) -> None:
        """初始化对账器。

        Args:
            watch_service: 02 的 ``WatchService``（含已验收的 ``reconcile_demands``）；
                缺省新建一个（复用其默认会话工厂，但本类只用调用方传入的 session）。
            member_statuses: 视为「有效成员」的成员状态集合；缺省只认 ``accepted``。
            clock: 秒级时钟（返回 UTC 秒），缺省 ``time.time`` 取整。
            fast_watch_enabled: 快采节奏开关；None（默认）时读 ``EVENT_FAST_WATCH_ENABLED``
                （环境变量 > ``config.yaml`` > 默认 false）；显式传 bool 便于测试。
        """
        self._watch_service = watch_service or WatchService()
        self._member_statuses = tuple(str(s) for s in member_statuses)
        self._clock: Callable[[], int] = clock or (lambda: int(time.time()))
        self._fast_watch_enabled_override: Optional[bool] = (
            None if fast_watch_enabled is None else bool(fast_watch_enabled)
        )

    @property
    def watch_service(self) -> WatchService:
        """暴露 02 的 ``WatchService``（供测试断言 / 组装）。"""
        return self._watch_service

    # ------------------------------------------------------------ 快照构建
    def build_events_desired(self, session: Session, *, now_s: Optional[int] = None) -> dict:
        """读**所有 active 事件**及其有效成员，构建 events 的完整当前快照。

        完整快照 = 本轮不存在的事件/成员即「已撤销」，由 ``reconcile_demands`` 从
        ``source_demands`` 的 ``events`` 命名空间里移除（**不许只传单事件**，否则别的事件被误撤）。

        快采节奏（4e）：开关打开时，对每个当前生效 fast panel 的事件**额外**出一个
        ``"<event_id>#fast"`` 键（descriptor 带 ``bvids`` + ``interval_s=FAST_WATCH_INTERVAL_S``）；
        键后缀本身不产生节奏，节奏只来自 descriptor 内 ``interval_s``。开关关（默认）时不写
        ``#fast`` 键，快照与既有实现**逐字节一致**。

        Args:
            session: 调用方会话（只读）。
            now_s: 判定 panel 是否过期用的时刻（UTC 秒）；缺省取注入时钟。

        Returns:
            dict: ``{event_id: {"bvids": [...]}}``；开关开且有生效 panel 时，另含
            ``{"<event_id>#fast": {"bvids": [...], "interval_s": 1200}}``。
            仅有 ≥1 个有效成员的 active 事件才出现本体键。
        """
        now = self._now(now_s)
        fast_on = self._fast_watch_enabled()
        desired: dict = {}
        # 全局 ``#fast`` BVID 并集：共享 BVID 只占 1，总量 ≤ FAST_WATCH_CAPACITY
        # （与 `_load_fast_union` / `activate_fast_panel` 的容量口径一致）。
        fast_used: set = set()
        events = (
            session.query(HotEvent)
            .filter(HotEvent.status == "active")
            .order_by(HotEvent.id.asc())
            .all()
        )
        for event in events:
            event_id = str(event.id)
            bvids = self._accepted_bvids(session, event_id)
            if not bvids:
                continue
            desired[event_id] = {"bvids": bvids}
            # 快采键只在开关开 + 当前有生效 panel 时出现；panel 过期 / 成员被拒 /
            # manual_stop 都会让它在下一轮快照里自然消失 → 02 取 min 通道自动退回 3600。
            if not fast_on:
                continue
            candidates = self._fast_panel_bvids(session, event, bvids, now_s=now)
            fast_bvids: list[str] = []
            for bvid in candidates:
                # 已在全局池里的共享 BVID 不占新名额；新 BVID 才增加全局计数。
                if bvid in fast_used or len(fast_used) < FAST_WATCH_CAPACITY:
                    fast_bvids.append(bvid)
                    fast_used.add(bvid)
            if fast_bvids:
                desired[self._fast_key(event_id)] = {
                    "bvids": fast_bvids,
                    "interval_s": FAST_WATCH_INTERVAL_S,
                }
        return desired

    def _accepted_bvids(self, session: Session, event_id: str) -> list[str]:
        """读某事件每个 bvid 的**最新 revision** 状态，返回有效成员的 bvid 列表。

        Args:
            session: 调用方会话。
            event_id: 事件 ID。

        Returns:
            list[str]: 最新 revision 命中 ``member_statuses`` 的 bvid（升序去重）。
        """
        rows = (
            session.query(HotEventMember)
            .filter(HotEventMember.event_id == event_id)
            .order_by(
                HotEventMember.revision.asc(),
                HotEventMember.decision_at_s.asc(),
                HotEventMember.id.asc(),
            )
            .all()
        )
        latest: dict[str, str] = {}
        for row in rows:
            latest[str(row.bvid)] = str(row.status)
        return sorted(b for b, status in latest.items() if status in self._member_statuses)

    # ------------------------------------------------------------ 快采节奏（4e）
    def _fast_watch_enabled(self) -> bool:
        """读快采开关 ``EVENT_FAST_WATCH_ENABLED``（显式注入优先，否则读环境 / 配置）。

        Returns:
            bool: 是否打开快采节奏（写 ``#fast`` 键）；**默认 false**。
        """
        if self._fast_watch_enabled_override is not None:
            return self._fast_watch_enabled_override
        try:
            # 复用 04 侧唯一开关入口（环境变量 > config.yaml > 默认 false），不另造口径。
            from modules.hotspot.events.service import _read_fast_watch_switch

            return bool(_read_fast_watch_switch())
        except Exception:  # noqa: BLE001 - 开关不可读时按「关」处理（与默认一致）
            return False

    @staticmethod
    def _fast_key(event_id: str) -> str:
        """事件本体键 → ``#fast`` 快采键（键后缀本身不产生节奏）。

        Args:
            event_id: 事件 ID。

        Returns:
            str: ``"<event_id>#fast"``。
        """
        return f"{event_id}{FAST_WATCH_KEY_SUFFIX}"

    def _fast_panel_bvids(
        self, session: Session, event: HotEvent, accepted_bvids: Sequence[str], *, now_s: int
    ) -> list[str]:
        """算某事件当前生效的 ``#fast`` 成员（与 ``_load_fast_union`` 同一口径）。

        口径：取事件 ``fast_panel_history`` 里 ``effective_s`` 非空且 ``expires_s > now_s``
        的 panel BVID 并集 → 与当前**有效成员**取交 → 排除 ``manual_stop`` → 去重后截到
        ``FAST_WATCH_CAPACITY``（共享 BVID 只占 1）。

        Args:
            session: 调用方会话（只读）。
            event: 已加载的 ``HotEvent``（含 ``fast_panel_history``）。
            accepted_bvids: 该事件当前有效成员的 bvid 列表。
            now_s: 判定 panel 是否过期的时刻（UTC 秒）。

        Returns:
            list[str]: 当前生效的快采 BVID（升序）；无生效 panel 时为空列表。
        """
        accepted = set(accepted_bvids)
        panel: set = set()
        for entry in event.fast_panel_history or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("effective_s") is None:
                continue  # 草稿（未激活）panel 不计名额
            expires = entry.get("expires_s")
            if type(expires) is not int or expires <= now_s:
                continue  # 过期 panel 不计名额 -> 下一轮快照自然无 #fast，退回 3600
            for bvid in entry.get("bvids") or []:
                clean = str(bvid).strip()
                if clean:
                    panel.add(clean)
        candidates = sorted(b for b in panel if b in accepted)
        if not candidates:
            return []
        blocked = self._manual_stop_bvids(session, candidates)
        return [b for b in candidates if b not in blocked][:FAST_WATCH_CAPACITY]

    @staticmethod
    def _manual_stop_bvids(session: Session, bvids: Sequence[str]) -> set:
        """读给定 bvid 中处于 ``active=0 AND stop_reason='manual_stop'`` 的集合（§6.5 L648）。

        Args:
            session: 调用方会话（只读）。
            bvids: 待检查的 BVID 列表。

        Returns:
            set[str]: 命中 ``manual_stop`` 的 BVID 集合；缺表 / 缺列时返回空集。
        """
        wanted = [str(b) for b in bvids if str(b).strip()]
        if not wanted:
            return set()
        stmt = text(
            "SELECT bvid FROM hotspot_watch "
            "WHERE active = 0 AND stop_reason = 'manual_stop' AND bvid IN :bvids"
        ).bindparams(bindparam("bvids", expanding=True))
        try:
            rows = session.execute(stmt, {"bvids": wanted}).all()
        except Exception:  # noqa: BLE001 - 缺表 / 缺列时按「无 manual_stop」处理
            return set()
        return {str(row[0]) for row in rows}

    # ------------------------------------------------------------ 整编入口
    def reconcile(self, session: Session, *, now_s: Optional[int] = None) -> dict:
        """整编 events 命名空间：把完整当前快照交给 02 ``reconcile_demands``。

        Args:
            session: 调用方持有的短事务会话；本方法只 flush。
            now_s: 本次整编时刻（UTC 秒）；缺省取注入时钟。

        Returns:
            dict: 本次提交的完整 events 快照（供测试 / 观测）。
        """
        now = self._now(now_s)
        desired = self.build_events_desired(session, now_s=now)
        # 断言调用的是 02 已验收实现；namespace 只限 events，其他一字不动。
        if EVENTS_NAMESPACE not in DEMAND_NAMESPACES:
            raise RuntimeError("events_namespace_missing")
        self._watch_service.reconcile_demands(
            session, namespace=EVENTS_NAMESPACE, desired=desired, now_s=now
        )
        logger.debug("events 需求整编：目标事件数=%s", len(desired))
        return desired

    def startup_reconcile(self, session: Session, *, now_s: Optional[int] = None) -> dict:
        """应用重启入口（E49）：先清遗留 events 需求 / 孤儿快采，再按当前事件快照整编。

        顺序：``recover_on_startup``（撤全部 events 需求、重算节奏、清孤儿快采）
        → ``reconcile``（把当前 active 事件重新纳入，若无 active 事件则保持撤空）。

        Args:
            session: 调用方持有的短事务会话；本方法只 flush。
            now_s: 本次恢复时刻（UTC 秒）；缺省取注入时钟。

        Returns:
            dict: 本次整编后的完整 events 快照。
        """
        now = self._now(now_s)
        self._watch_service.recover_on_startup(session, now_s=now)
        return self.reconcile(session, now_s=now)

    # ------------------------------------------------------------ 工具
    def demand_eligibility(self, session: Session, bvid: str) -> str:
        """现算某 bvid 的准入派生 reason（转发 02；``manual_stop`` → ``blocked_by_user``）。

        Args:
            session: 调用方会话。
            bvid: 目标 BV 号。

        Returns:
            str: ``blocked_by_user`` / ``tracking`` / ``released`` / ``no_demand`` 之一。
        """
        return self._watch_service.demand_eligibility(session, bvid)

    def _now(self, now_s: Optional[int]) -> int:
        """归一当前时刻为 epoch 秒（显式优先，其次注入时钟）。

        Args:
            now_s: 显式时刻。

        Returns:
            int: epoch 秒。

        Raises:
            ValueError: ``now_s`` 非 int / 为 bool / 为负。
        """
        if now_s is None:
            return int(self._clock())
        if type(now_s) is not int or now_s < 0:
            raise ValueError("invalid_now_s")
        return int(now_s)
