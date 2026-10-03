"""P3 · 需求整编 ``reconcile_demands``（FishTool 04 · R5 前置）。

被测：``modules/hotspot/watch_service.py::WatchService.reconcile_demands``。

覆盖点（逐条对齐派单 P3）：
- manual / ranking 的键即 bvid；events 按 event_id 分组，条目可用 ``bvid`` 或 ``bvids``；
- 快照语义：先写 ``{e1,e2}`` 再写 ``{e2}``，该命名空间只留 e2（撤销生效、不残留）；
- 只换自己那格：manual 整编后 ranking 子快照一字不动（原始 JSON 对比）；
- 非法输入抛 ValueError：namespace 不在白名单 / desired 非 dict / 条目非 dict /
  events 条目缺 bvid·bvids / now_s 非法（bool·负数·float）；
- 只 flush 不 commit：调用后 session 未提交、且采集端口零调用（无网络）；
- 无变化时不写：同快照连调两次，第二次不产生 UPDATE。

测试用真实 ``DatabaseManager`` 建临时库（含 04 迁移补齐的 ``source_demands`` 列），
全程不触网、不读密钥。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest import mock

import pytest
from sqlalchemy import text

from core.database import DatabaseManager
from modules.hotspot.watch_service import DEMAND_NAMESPACES, WatchService
from modules.hotspot.watch_store import upsert_watch

#: 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int）。
E: int = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())


class _NoNetworkCollector:
    """采集端口替身：一旦被调用即失败，用于断言整编过程零网络行为。"""

    def __init__(self) -> None:
        """初始化调用计数。"""
        self.calls = 0

    async def collect(self, *args, **kwargs):
        """被调用即计数并抛错（reconcile_demands 不应触网）。"""
        self.calls += 1
        raise AssertionError("reconcile_demands 不应触发任何网络采集")


@pytest.fixture()
def session(tmp_path):
    """临时文件库（含 04 迁移列）上的独立会话，用例结束释放引擎。"""
    mgr = DatabaseManager(str(tmp_path / "p3_reconcile.db"))
    db = mgr.get_session()
    try:
        yield db
    finally:
        db.close()
        mgr.engine.dispose()


def _seed(session, *bvids: str, now: int = E) -> None:
    """把若干 bvid 入库（幂等）并提交，作为整编的落点行。"""
    for bvid in bvids:
        upsert_watch(session, bvid=bvid, now_epoch_s=now)
    session.commit()


def _raw(session) -> dict:
    """读出每行 ``source_demands`` 的原始值（JSON 文本 -> Python 对象）。"""
    rows = session.execute(text("SELECT bvid, source_demands FROM hotspot_watch")).all()
    out = {}
    for bvid, payload in rows:
        out[str(bvid)] = None if payload is None else json.loads(payload)
    return out


def _count_updates(session, call) -> int:
    """执行 ``call``，返回期间针对 ``hotspot_watch`` 的 UPDATE 次数。"""
    counter = {"n": 0}
    real_execute = session.execute

    def spy(statement, *args, **kwargs):
        if "UPDATE HOTSPOT_WATCH" in str(statement).upper():
            counter["n"] += 1
        return real_execute(statement, *args, **kwargs)

    with mock.patch.object(session, "execute", side_effect=spy):
        call()
    return counter["n"]


# --------------------------------------------------------------------------- 键口径


def test_manual_key_is_bvid(session) -> None:
    """manual 的键即 bvid，落成 ``{"manual": {"<bvid>": {...}}}``。"""
    _seed(session, "BV1")
    WatchService().reconcile_demands(
        session, namespace="manual", desired={"BV1": {"note": "pin"}}, now_s=E
    )
    session.commit()
    assert _raw(session)["BV1"] == {"manual": {"BV1": {"note": "pin"}}}


def test_ranking_key_is_bvid(session) -> None:
    """ranking 的键即 bvid。"""
    _seed(session, "BV1")
    WatchService().reconcile_demands(
        session, namespace="ranking", desired={"BV1": {"rank": 7}}, now_s=E
    )
    session.commit()
    assert _raw(session)["BV1"] == {"ranking": {"BV1": {"rank": 7}}}


def test_events_group_by_event_id_and_accept_bvid_or_bvids(session) -> None:
    """events 按 event_id 分组；条目既可用 ``bvid`` 也可用 ``bvids``。"""
    _seed(session, "BV1", "BV2", "BV3")
    WatchService().reconcile_demands(
        session,
        namespace="events",
        desired={"e1": {"bvid": "BV1"}, "e2": {"bvids": ["BV2", "BV3"]}},
        now_s=E,
    )
    session.commit()
    raw = _raw(session)
    assert raw["BV1"]["events"] == {"e1": {"bvid": "BV1"}}
    assert raw["BV2"]["events"] == {"e2": {"bvids": ["BV2", "BV3"]}}
    assert raw["BV3"]["events"] == {"e2": {"bvids": ["BV2", "BV3"]}}


# --------------------------------------------------------------------------- 快照语义


def test_snapshot_revocation_leaves_no_residue(session) -> None:
    """先写 {e1,e2} 再写 {e2}：e1 撤销、该命名空间不残留 e1。"""
    _seed(session, "BV1", "BV2")
    svc = WatchService()
    svc.reconcile_demands(
        session,
        namespace="events",
        desired={"e1": {"bvid": "BV1"}, "e2": {"bvid": "BV2"}},
        now_s=E,
    )
    session.commit()

    svc.reconcile_demands(
        session, namespace="events", desired={"e2": {"bvid": "BV2"}}, now_s=E + 1
    )
    session.commit()

    raw = _raw(session)
    # BV1 只有 events，撤销后整列为空（无残留）。
    assert raw["BV1"] is None
    assert raw["BV2"] == {"events": {"e2": {"bvid": "BV2"}}}


def test_only_own_namespace_replaced(session) -> None:
    """整编 manual 时，ranking 子快照一字不动（原始 JSON 对比）。"""
    _seed(session, "BV1")
    svc = WatchService()

    svc.reconcile_demands(session, namespace="ranking", desired={"BV1": {"rank": 1}}, now_s=E)
    session.commit()
    ranking_before = json.dumps(_raw(session)["BV1"]["ranking"], sort_keys=True)

    svc.reconcile_demands(
        session, namespace="manual", desired={"BV1": {"pinned": True}}, now_s=E + 1
    )
    session.commit()

    after = _raw(session)["BV1"]
    assert json.dumps(after["ranking"], sort_keys=True) == ranking_before
    assert after["manual"] == {"BV1": {"pinned": True}}


# --------------------------------------------------------------------------- 非法输入


def test_namespace_whitelist(session) -> None:
    """白名单固定为 {manual, ranking, events}，其它值必须 ValueError。"""
    assert DEMAND_NAMESPACES == frozenset({"manual", "ranking", "events"})
    with pytest.raises(ValueError):
        WatchService().reconcile_demands(session, namespace="other", desired={}, now_s=E)


@pytest.mark.parametrize("bad_desired", [("BV1",), 123, "x", None, ["BV1"]])
def test_desired_must_be_dict(session, bad_desired) -> None:
    """desired 非 dict 必须 ValueError（None 也不算 dict）。"""
    with pytest.raises(ValueError):
        WatchService().reconcile_demands(
            session, namespace="manual", desired=bad_desired, now_s=E
        )


@pytest.mark.parametrize("bad_entry", [123, "BV1", ["x"], 1.5])
def test_entry_must_be_dict(session, bad_entry) -> None:
    """list / ranking 条目非 dict（且非 None）必须 ValueError。"""
    with pytest.raises(ValueError):
        WatchService().reconcile_demands(
            session, namespace="manual", desired={"BV1": bad_entry}, now_s=E
        )


def test_events_entry_without_bvid_or_bvids_raises(session) -> None:
    """events 条目既无 bvid 也无 bvids 必须 ValueError（不许拿 event_id 兜底）。"""
    with pytest.raises(ValueError):
        WatchService().reconcile_demands(
            session, namespace="events", desired={"e1": {}}, now_s=E
        )
    with pytest.raises(ValueError):
        WatchService().reconcile_demands(
            session, namespace="events", desired={"e1": {"foo": 1}}, now_s=E
        )


@pytest.mark.parametrize("bad_now", [True, False, -1, 1.5, "1", None])
def test_now_s_must_be_non_negative_int(session, bad_now) -> None:
    """now_s 非法（bool/负数/float/字符串/None）必须 ValueError。"""
    with pytest.raises(ValueError):
        WatchService().reconcile_demands(
            session, namespace="manual", desired={}, now_s=bad_now
        )


# --------------------------------------------------------------------------- flush-only / 无网络


def test_flush_only_no_commit_and_no_network(session) -> None:
    """只 flush 不 commit，且采集端口零调用（不触网）。"""
    collector = _NoNetworkCollector()
    svc = WatchService(collector_port=collector)
    _seed(session, "BV1")

    with mock.patch.object(session, "commit", wraps=session.commit) as commit_spy:
        svc.reconcile_demands(session, namespace="manual", desired={"BV1": {}}, now_s=E + 2)
        assert commit_spy.call_count == 0, "reconcile_demands 不得 commit"

    assert collector.calls == 0, "reconcile_demands 不得触发网络采集"
    # flush 已生效：同一会话内可见。
    assert _raw(session)["BV1"] == {"manual": {"BV1": {}}}


def test_flush_only_changes_discarded_by_rollback(session) -> None:
    """未 commit：flush 后会话内可见，rollback 即整段回退（证明确实没落库）。"""
    _seed(session, "BV1")
    WatchService().reconcile_demands(
        session, namespace="manual", desired={"BV1": {"k": 1}}, now_s=E
    )
    assert _raw(session)["BV1"] == {"manual": {"BV1": {"k": 1}}}

    session.rollback()
    assert _raw(session)["BV1"] is None


# --------------------------------------------------------------------------- 无变化不写


def test_no_change_second_call_produces_no_update(session) -> None:
    """同快照连调两次：第二次不产生 UPDATE（无谓写被跳过）。"""
    svc = WatchService()
    _seed(session, "BV1")
    svc.reconcile_demands(session, namespace="manual", desired={"BV1": {"k": 1}}, now_s=E)
    session.commit()

    def _second() -> None:
        svc.reconcile_demands(session, namespace="manual", desired={"BV1": {"k": 1}}, now_s=E + 1)

    assert _count_updates(session, _second) == 0

    # 反向 sanity：快照真的变了就必须写（证明上面的计数器不是恒 0）。
    def _changed() -> None:
        svc.reconcile_demands(session, namespace="manual", desired={"BV1": {"k": 2}}, now_s=E + 2)

    assert _count_updates(session, _changed) >= 1
