"""``watch`` 逻辑准入用例：真 ``RequestBudget`` 的单请求准入协议 + 真选择器的分桶行为。

W0 时本文件只是把 07 证据案的两条反例原样固化为 ``xfail(strict=True)``。
W1 落地后，这些断言被**改写为修复后的正确行为**并转为普通断言（见文件末尾三段）。

被测对象（都是**真实实现**，不打桩）：
- ``modules/hotspot/risk_control.RequestBudget`` —— 真预算器（``peek`` / ``reserve`` /
  ``redeem`` / ``release_unused`` / ``snapshot`` 单请求准入协议）；
- ``modules/hotspot/watch_service.WatchService._select_targets`` —— 真选择器（W1 起用
  ``peek`` 只读挑候选，不再消费预算）。

唯一替身是 ``watch_service`` 模块级从 ``watch_store`` 导入的两个 DB 读取口
（``find_due_for_eval`` / ``load_fast_until_map``），因为这两个函数要真库和真 session；
本用例关心的是「拿到行之后的预算裁决」，与行的来源无关。其余一律走真实代码路径。

注：仓库未装 ``pytest-asyncio``，异步场景统一 ``asyncio.run`` 驱动。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from modules.hotspot import watch_service as ws
from modules.hotspot.risk_control import (
    InvalidLogicalAdmission,
    RequestBudget,
)
from modules.hotspot.watch_service import (
    BUDGET_CATEGORY_FAST,
    BUDGET_CATEGORY_NORMAL,
    WatchService,
)

#: 统一的 UTC 秒级时刻（与 test_watch_service.py 的口径一致：2026-09-10 UTC 日界）。
NOW_EPOCH_S: int = 1_787_616_000  # 2026-09-10T00:00:00Z

#: 统一的单调时钟读数（只喂 RequestBudget，绝不落库）。
NOW_MONO: float = 1_000.0

#: 默认归属分区，喂给 ``_build_target``。
TID: int = 1008


class _Row:
    """``hotspot_watch`` 行的最小替身。

    ``WatchService._build_target`` 只读这四个属性，其余列与本轮无关；
    这里刻意不引 SQLAlchemy，免得为了造一行去建整张表。
    """

    def __init__(self, bvid: str, *, collection_tid: int | None = TID) -> None:
        """构造一行替身。

        Args:
            bvid: 视频 BV 号。
            collection_tid: 归属采集分区 ID，可为 None。
        """
        self.bvid = bvid
        self.collection_tid = collection_tid
        self.sample_interval_s = None
        self.state_json = None


class _TrapSession:
    """会话替身：一旦真被拿去查库就立刻炸，用来证明 DB 口确实被打桩了。"""

    def __getattr__(self, name: str):  # pragma: no cover - 命中即说明测试假定已失效
        """任何属性访问都视为「真实 DB 路径被意外走到」。"""
        raise AssertionError(
            f"测试替身失效：_select_targets 及其下游不应触碰真实 session 的 {name!r}"
        )


@pytest.fixture()
def fake_rows(monkeypatch):
    """把 ``watch_service`` 的 DB 读取口换成内存替身。

    W2 起选择器改为**按类查询**（``find_due_for_budget_category``），故这里补一个按类过滤的替身；
    旧的 ``find_due_for_eval`` / ``load_fast_until_map`` 仍打桩（无预算 legacy 路径仍走前者）。

    Returns:
        tuple: ``(rows, fast_until)`` —— 两个可变容器，用例先往里塞数据再跑选择器。
    """
    rows: list[_Row] = []
    fast_until: dict[str, int] = {}

    def _fake_find_due_for_eval(session, now_epoch_s, limit):
        """按 limit 截断返回内存行（legacy / 无预算路径仍用它）。"""
        return list(rows[: max(1, int(limit))])

    def _fake_load_fast_until_map(session, bvids):
        """只回映射里有的 bvid（保持与真实现同形）。"""
        return {bvid: fast_until[bvid] for bvid in bvids if bvid in fast_until}

    def _category_of(row):
        """按 ``fast_until_s`` 是否生效把行归 ``normal_watch`` / ``fast_watch``（与真实现同判据）。"""
        fast = fast_until.get(row.bvid)
        return (
            BUDGET_CATEGORY_FAST
            if (type(fast) is int and fast > NOW_EPOCH_S)
            else BUDGET_CATEGORY_NORMAL
        )

    def _fake_find_due_for_budget_category(
        session, now_epoch_s, *, category, limit=1, cursor=None
    ):
        """W2 新读取口：按类过滤内存行后按 limit 截断（真实现即「按类 LIMIT」）。"""
        picked = [row for row in rows if _category_of(row) == category]
        return picked[: max(1, int(limit))]

    monkeypatch.setattr(ws, "find_due_for_eval", _fake_find_due_for_eval)
    monkeypatch.setattr(ws, "load_fast_until_map", _fake_load_fast_until_map)
    monkeypatch.setattr(
        ws, "find_due_for_budget_category", _fake_find_due_for_budget_category
    )
    return rows, fast_until


def _service(budget: RequestBudget | None = None) -> WatchService:
    """构造只跑 ``_select_targets`` 的编排层（其余依赖都能缺省且本轮用不到）。

    Args:
        budget: 预算器；``_select_targets`` 走显式 ``budget=`` 入参，这里给的是
            构造参数版本，用于验证两者口径一致。

    Returns:
        WatchService: 固定时钟的编排层实例。
    """
    return WatchService(
        session_factory=_TrapSession,
        now_mono_fn=lambda: NOW_MONO,
        budget=budget,
    )


def _partitioned(**limits: dict[str, int]) -> RequestBudget:
    """构造分桶模式预算：normal_watch / fast_watch 各自可配，全局窗给足。

    Args:
        **limits: 形如 ``normal_watch={"per_minute": 1, ...}`` 的类别覆盖；缺省类别用
            一个足够大的默认额度，避免误挡其它类。

    Returns:
        RequestBudget: 分桶模式的真预算器。
    """
    default = {"per_minute": 100, "per_hour": 1000, "per_day": 10000}
    category_limits = {
        BUDGET_CATEGORY_NORMAL: dict(default, **limits.get("normal_watch", {})),
        BUDGET_CATEGORY_FAST: dict(default, **limits.get("fast_watch", {})),
    }
    return RequestBudget(
        per_minute=200, per_hour=2000, per_day=20000, category_limits=category_limits
    )


# ===========================================================================
# §11.1 单请求准入协议单元测试（1 / 2 / 3 / 3a / 4 / 5）
# ===========================================================================


def test_peek_does_not_charge_reserve_occupies_and_redeem_stays_one() -> None:
    """§11.1-1：peek 不记账；reserve 占一份；redeem 仍是一份（不追加第二条）。"""
    budget = RequestBudget(per_minute=2, per_hour=100, per_day=1000)

    # peek 连看多次都不占任何容量。
    for _ in range(5):
        assert budget.peek("normal_watch", NOW_MONO).granted is True
    assert len(budget._requests) == 0
    assert budget.snapshot(NOW_MONO)["reserved"] == 0

    # reserve 才占一份（写一条 reserved 条目）。
    result = budget.reserve("normal_watch", NOW_MONO, operation_key="BV1")
    assert result.decision.granted is True
    assert result.admission is not None
    assert budget.snapshot(NOW_MONO)["reserved"] == 1
    assert budget.snapshot(NOW_MONO)["global_used_60s"] == 1

    # redeem 只把同一条 reserved 变 committed，仍是「一份」，绝不 append 第二条。
    budget.redeem(result.admission, operation_key="BV1", now_mono=NOW_MONO)
    snapshot = budget.snapshot(NOW_MONO)
    assert snapshot["reserved"] == 0
    assert snapshot["committed"] == 1
    assert snapshot["global_used_60s"] == 1
    assert len(budget._requests) == 0  # 没走 legacy 队列，也就没有第二次记账


def test_release_only_cancels_unused_and_redeemed_not_refunded() -> None:
    """§11.1-2：release 只撤未 redeem 的 reservation；已 redeem 不退款。"""
    budget = RequestBudget(per_minute=2, per_hour=100, per_day=1000)

    # 未兑换：release 生效并腾出容量。
    unused = budget.reserve("normal_watch", NOW_MONO, operation_key="BV1").admission
    assert budget.release_unused(unused) is True
    assert budget.snapshot(NOW_MONO)["global_used_60s"] == 0
    assert budget.release_unused(unused) is False  # 已撤，不重复处理

    # 已兑换：release 返回 False，不扣回。
    redeemed = budget.reserve("normal_watch", NOW_MONO, operation_key="BV2").admission
    budget.redeem(redeemed, operation_key="BV2", now_mono=NOW_MONO)
    assert budget.release_unused(redeemed) is False
    snapshot = budget.snapshot(NOW_MONO)
    assert snapshot["committed"] == 1
    assert snapshot["global_used_60s"] == 1


def test_replay_wrong_operation_key_and_wrong_issuer_are_rejected() -> None:
    """§11.1-3：同票据重放 / 错 BVID / 错 issuer 一律拒绝。"""
    budget = RequestBudget(per_minute=5, per_hour=100, per_day=1000)
    admission = budget.reserve("normal_watch", NOW_MONO, operation_key="BV1").admission

    # 错 operation_key（BVID）。
    with pytest.raises(InvalidLogicalAdmission) as wrong_key:
        budget.redeem(admission, operation_key="BV9", now_mono=NOW_MONO)
    assert wrong_key.value.reason_code == "invalid_or_reused_admission"

    # 错 issuer：另一个预算对象不能兑换本对象签发的凭证。
    other = RequestBudget(per_minute=5, per_hour=100, per_day=1000)
    with pytest.raises(InvalidLogicalAdmission) as wrong_issuer:
        other.redeem(admission, operation_key="BV1", now_mono=NOW_MONO)
    assert wrong_issuer.value.reason_code == "invalid_or_reused_admission"

    # 伪造 issuer 字段（entry_id 仍指向真条目）同样被拒。
    forged = replace(admission, issuer=other)
    with pytest.raises(InvalidLogicalAdmission):
        budget.redeem(forged, operation_key="BV1", now_mono=NOW_MONO)

    # 合法兑换成功。
    budget.redeem(admission, operation_key="BV1", now_mono=NOW_MONO)

    # 同票据重放被拒（状态已不是 reserved）。
    with pytest.raises(InvalidLogicalAdmission) as replay:
        budget.redeem(admission, operation_key="BV1", now_mono=NOW_MONO)
    assert replay.value.reason_code == "invalid_or_reused_admission"

    # 错 issuer 的 release 也不生效。
    assert budget.release_unused(forged) is False


def test_cross_task_redeem_succeeds_without_owner_task_check() -> None:
    """§11.1-3（后半）：同一凭证在另一 task（create_task/gather 包装）内合法兑换必须成功。"""
    budget = RequestBudget(per_minute=5, per_hour=100, per_day=1000)
    admission = budget.reserve("normal_watch", NOW_MONO, operation_key="BV1").admission

    async def _worker() -> bool:
        """在子任务里兑换：不依赖任务身份。"""
        budget.redeem(admission, operation_key="BV1", now_mono=NOW_MONO)
        return True

    async def _runner() -> list:
        """用 create_task + gather 把兑换放进另一个 task。"""
        task = asyncio.create_task(_worker())
        return await asyncio.gather(task)

    assert asyncio.run(_runner()) == [True]
    assert budget.snapshot(NOW_MONO)["committed"] == 1


def test_reservation_timeout_auto_releases_and_expired_redeem_is_rejected() -> None:
    """§11.1-3a：预留超时自动释放；超时后的 redeem 返回 admission_expired 且不补记 committed。"""
    # (a) 不 redeem、也不进 finally：靠占用计算前的清扫自动释放。
    auto = RequestBudget(
        per_minute=5, per_hour=100, per_day=1000, reserve_timeout_s=10.0
    )
    auto.reserve("normal_watch", NOW_MONO, operation_key="BV1")
    assert auto.snapshot(NOW_MONO)["reserved"] == 1
    later = auto.snapshot(NOW_MONO + 11.0)
    assert later["reserved"] == 0
    assert later["committed"] == 0
    assert later["global_used_60s"] == 0

    # (b) 超时后的 redeem：置 cancelled 并抛 admission_expired，不产生 committed。
    expired = RequestBudget(
        per_minute=5, per_hour=100, per_day=1000, reserve_timeout_s=10.0
    )
    admission = expired.reserve("normal_watch", NOW_MONO, operation_key="BV1").admission
    with pytest.raises(InvalidLogicalAdmission) as excinfo:
        # 期间不做任何 peek / snapshot（它们会先触发清扫把状态置 cancelled）。
        expired.redeem(admission, operation_key="BV1", now_mono=NOW_MONO + 11.0)
    assert excinfo.value.reason_code == "admission_expired"
    final = expired.snapshot(NOW_MONO + 11.0)
    assert final["committed"] == 0
    assert final["reserved"] == 0


def test_category_quota_full_leaves_other_category_usable() -> None:
    """§11.1-4：一类额度满时，另一类仍可用（正反两个方向）。"""
    # fast 满 -> normal 仍可用
    b1 = _partitioned(fast_watch={"per_minute": 1})
    fast = b1.reserve(BUDGET_CATEGORY_FAST, NOW_MONO, operation_key="F1")
    assert fast.decision.granted is True
    b1.redeem(fast.admission, operation_key="F1", now_mono=NOW_MONO)
    assert b1.peek(BUDGET_CATEGORY_FAST, NOW_MONO).granted is False
    assert b1.peek(BUDGET_CATEGORY_NORMAL, NOW_MONO).granted is True
    normal = b1.reserve(BUDGET_CATEGORY_NORMAL, NOW_MONO, operation_key="N1")
    assert normal.decision.granted is True

    # 反向：normal 满 -> fast 仍可用
    b2 = _partitioned(normal_watch={"per_minute": 1})
    n = b2.reserve(BUDGET_CATEGORY_NORMAL, NOW_MONO, operation_key="N1")
    assert n.decision.granted is True
    b2.redeem(n.admission, operation_key="N1", now_mono=NOW_MONO)
    assert b2.peek(BUDGET_CATEGORY_NORMAL, NOW_MONO).granted is False
    assert b2.peek(BUDGET_CATEGORY_FAST, NOW_MONO).granted is True


def test_shared_global_full_rejects_both_categories() -> None:
    """§11.1-5：shared 模式全局满时两类都拒，不能误称 normal 保留了无限额度。"""
    budget = RequestBudget(per_minute=2, per_hour=100, per_day=1000)  # shared 模式
    first = budget.reserve("fast_watch", NOW_MONO, operation_key="F1")
    second = budget.reserve("normal_watch", NOW_MONO, operation_key="N1")
    assert first.decision.granted is True
    assert second.decision.granted is True

    assert budget.peek("fast_watch", NOW_MONO).granted is False
    assert budget.peek("normal_watch", NOW_MONO).granted is False

    # 释放一份 reservation 后全局腾出，两类的 peek 又恢复可放行（证明是占用而非永久封禁）。
    budget.release_unused(first.admission)
    assert budget.peek("normal_watch", NOW_MONO).granted is True


# ===========================================================================
# W0 反例 4 / 5（W1 落地后改为修复后的正确行为）
# ===========================================================================


def test_fast_category_does_not_starve_normal_of_minute_capacity() -> None:
    """修订（原反例 4 预算器侧）：fast 吃满自身分钟额度后，normal 仍有独立份额。

    修复前：``per_minute`` 是跨类别总管总数，fast 能把它一口吃干、把 normal 饿死。
    修复后：分桶模式下每类各自记各的账，fast 只占自己那份，normal 照常获准。
    """
    budget = _partitioned(
        fast_watch={"per_minute": 3}, normal_watch={"per_minute": 3}
    )

    fast_grants = [
        budget.reserve(BUDGET_CATEGORY_FAST, NOW_MONO, operation_key=f"F{i}").decision.granted
        for i in range(3)
    ]
    assert fast_grants == [True, True, True]

    assert budget.peek(BUDGET_CATEGORY_FAST, NOW_MONO).granted is False  # fast 自己满
    assert budget.peek(BUDGET_CATEGORY_NORMAL, NOW_MONO).granted is True  # normal 不受连坐

    normal = budget.reserve(BUDGET_CATEGORY_NORMAL, NOW_MONO, operation_key="N1")
    assert normal.decision.granted is True

    snapshot = budget.snapshot(NOW_MONO)
    assert snapshot["category_used"][BUDGET_CATEGORY_FAST] == 3
    assert snapshot["category_used"][BUDGET_CATEGORY_NORMAL] == 1


def test_fast_history_does_not_block_normal_selection(fake_rows) -> None:
    """修订（原反例 4 选择器侧）：fast 占满自身额度后，normal 目标照选不误。"""
    rows, _fast_until = fake_rows
    rows.append(_Row("BVNORMAL0001"))  # 一行 normal 目标

    budget = _partitioned(fast_watch={"per_minute": 1})
    fast = budget.reserve(BUDGET_CATEGORY_FAST, NOW_MONO, operation_key="BVFAST00001")
    assert fast.decision.granted is True
    budget.redeem(fast.admission, operation_key="BVFAST00001", now_mono=NOW_MONO)

    selection = _service()._select_targets(
        session=_TrapSession(),
        now_epoch_s=NOW_EPOCH_S,
        limit=1,
        budget=budget,
        now_mono=NOW_MONO,
    )

    # 修复后：fast 有 fast 的额度，normal 目标按 normal 额度照选不误。
    assert [target.bvid for target in selection.targets] == ["BVNORMAL0001"]
    assert selection.budget_exhausted is False


def test_bounded_scan_finds_eligible_normal_within_window(fake_rows) -> None:
    """修订（原反例 5）：同类前面的 fast 全被本类额度挡住时，后面的 normal 仍被选中。

    W0 反例把 normal 放在第 9 行、**超出** ``limit*8`` 扫描窗。W2 起选择器**按类分别取候选**
    （每类读取上限 = ``limit``），不再靠放大扫描倍数；本类被拒只跳过本类，另一类照常可运行。
    ``budget_skipped`` 因此按「被跳过的类别队首」计（本例 1 条 fast），而不是扫描窗内目标数。
    """
    rows, fast_until = fake_rows
    fast_bvids = [f"BVFAST{i:05d}" for i in range(7)]
    normal_bvid = "BVNORMAL01"
    rows.extend([_Row(bvid) for bvid in fast_bvids] + [_Row(normal_bvid)])
    for bvid in fast_bvids:
        fast_until[bvid] = NOW_EPOCH_S + 3600  # 7 行全在 fast 窗口内

    budget = _partitioned(fast_watch={"per_minute": 1})
    # fast 名额先被占满（1/1），故 fast 类整体被跳过。
    seeded = budget.reserve(BUDGET_CATEGORY_FAST, NOW_MONO, operation_key="SEEDFAST")
    budget.redeem(seeded.admission, operation_key="SEEDFAST", now_mono=NOW_MONO)

    selection = _service()._select_targets(
        session=_TrapSession(),
        now_epoch_s=NOW_EPOCH_S,
        limit=1,
        budget=budget,
        now_mono=NOW_MONO,
    )

    assert [target.bvid for target in selection.targets] == [normal_bvid]
    assert selection.budget_skipped == 1, "fast 类整体被跳过（按队首计 1 次，非扫描窗目标数 7）"
    assert selection.budget_exhausted is False, (
        "存在可运行的 normal 目标时不得回报「预算耗尽」"
    )


# ===========================================================================
# 对照组（修复前后都成立）
# ===========================================================================


def test_no_budget_means_no_gate_and_all_rows_pass(fake_rows) -> None:
    """对照组：``budget=None`` 时不做预算门，行原样通过。"""
    rows, _ = fake_rows
    rows.extend([_Row("BVNOBUD0001"), _Row("BVNOBUD0002")])

    selection = _service()._select_targets(
        session=_TrapSession(),
        now_epoch_s=NOW_EPOCH_S,
        limit=10,
        budget=None,
    )

    assert [target.bvid for target in selection.targets] == ["BVNOBUD0001", "BVNOBUD0002"]
    assert selection.budget_skipped == 0
    assert selection.budget_exhausted is False


def test_budget_exhausted_reported_when_every_scanned_row_is_denied(fake_rows) -> None:
    """对照组：读到的候选全被挡时，确实该报耗尽（W2 起按类读取上限 = ``limit``）。"""
    rows, fast_until = fake_rows
    fast_bvids = [f"BVDENY{i:05d}" for i in range(3)]
    rows.extend([_Row(bvid) for bvid in fast_bvids])
    for bvid in fast_bvids:
        fast_until[bvid] = NOW_EPOCH_S + 3600

    budget = RequestBudget(per_minute=1, per_hour=300, per_day=3000)
    # 共享模式下先用 legacy 入口占掉唯一名额（peek 会读到同一个全局总量）。
    assert budget.try_acquire(BUDGET_CATEGORY_FAST, NOW_MONO).granted is True

    selection = _service()._select_targets(
        session=_TrapSession(),
        now_epoch_s=NOW_EPOCH_S,
        limit=1,
        budget=budget,
        now_mono=NOW_MONO,
    )

    assert selection.targets == []
    # 只有 fast 类有候选，且本类读取上限 = limit = 1；本类被拒即跳过，故按队首计 1。
    assert selection.budget_skipped == 1
    assert selection.budget_exhausted is True
    assert selection.retry_delay_s is not None and selection.retry_delay_s > 0
