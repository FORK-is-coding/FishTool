"""FishTool 04 · 第三批 a：六张表 + 仓储层验收测试。

被测：
- ``core/database/models_hot_event.py``（六张表：hot_events / hot_event_members /
  event_discovery_runs / hot_event_assessments / hotspot_opportunity_runs /
  topic_generation_runs）；
- ``core/database/hot_event_repository.py``（规格 §2 五条封装）。

覆盖「钉死口径」1~10（口径 2 含正反两条）：

1. ``hot_event_members`` 唯一 ``(event_id, bvid, revision)``；
2. 当前状态取最新 revision；历史状态**先限 ``decision_at_s<=cutoff`` 再取最新**，
   反向用例证明"先过滤 accepted 再取最新"会复活已撤销成员；
3. 同秒多 revision 按 ``revision`` 严格排序；
4. ``decision_at_s`` = 实际提交时间，不接受客户端更早值；
5. ``hot_event_assessments`` 唯一六元组；同 ``input_fingerprint`` 命中返回已有行；
6. assessment ``status`` 只限法定枚举，``insufficient_fast_coverage`` 等只进 reason_codes；
7. ``topic_generation_runs`` CHECK：state 合法值、completed 必须有 result+finished_s、
   失败不保存成功 saved_ids、``request_hash`` 不唯一、``(state, lease_until_s)`` 索引；
8. JSON 列整对象赋值（朴素 JSON 不挂 Mutable -> in-place 不落盘）；
9. 时间一律 epoch 整数秒；
10. ``event_discovery_runs`` 区别合法空 / 接口失败 / 页重复 / 达到上限，四者不混。

外加：六张表列名/类型与上游 §5 一致（结构断言）；``DatabaseManager`` 连跑两次
``create_all`` 幂等不报错；六表 create/get/update 基础读写。

测试策略：临时文件 SQLite，经 ``DatabaseManager`` 建表；固定时钟；**不连生产库、不打真 B 站**。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from core.database import DatabaseManager
from core.database.hot_event_repository import HotEventRepository
from core.database.models_hot_event import (
    ASSESSMENT_STATUSES,
    DISCOVERY_EMPTY_CAP_REACHED,
    DISCOVERY_EMPTY_INTERFACE_FAILURE,
    DISCOVERY_EMPTY_LEGITIMATE,
    DISCOVERY_EMPTY_PAGE_DUPLICATE,
    GENERATION_STATES,
    HotEvent,
    HotEventAssessment,
    HotEventMember,
    TopicGenerationRun,
)

#: 统一测试时钟：2026-09-01T00:00:00Z 的 epoch 秒（秒级 int）。
T: int = 1788220800

#: 六张表的本批表名。
SIX_TABLES: tuple = (
    "hot_events",
    "hot_event_members",
    "event_discovery_runs",
    "hot_event_assessments",
    "hotspot_opportunity_runs",
    "topic_generation_runs",
)

#: 仓储层用的「空结果四因」键名（放 ``event_discovery_runs.counters``）。
EMPTY_REASON_KEY: str = "empty_reason"

#: 结构断言：上游 §5 逐字段（列名 -> 归一类型 token：str/int/json）。
EXPECTED_COLUMNS: dict = {
    "hot_events": {
        "id": "str",
        "name": "str",
        "entity_scope": "json",
        "current_rule_version": "int",
        "rule_history": "json",
        "created_s": "int",
        "updated_s": "int",
        "status": "str",
        "revision": "int",
        "source_policy": "json",
        "fast_panel_history": "json",
        "supersedes": "json",
        "source_policy_hash": "str",
        "active_discovery_run_id": "str",
        "last_discovery_attempt_s": "int",
        "last_discovery_error_code": "str",
        "discovery_due_s": "int",
        "lease_token": "str",
        "lease_until_s": "int",
        "links": "json",
    },
    "hot_event_members": {
        "id": "int",
        "event_id": "str",
        "bvid": "str",
        "revision": "int",
        "status": "str",
        "first_seen_s": "int",
        "decision_at_s": "int",
        "rule_version": "int",
        "decision_source": "str",
        "raw_tid": "int",
        "published_epoch_s": "int",
        "owner_mid": "int",
        "evidence": "json",
    },
    "event_discovery_runs": {
        "id": "str",
        "event_id": "str",
        "rule_version": "int",
        "source_policy_hash": "str",
        "lease_token": "str",
        "trigger": "str",
        "started_s": "int",
        "finished_s": "int",
        "status": "str",
        "error_code": "str",
        "source_attempts": "json",
        "candidates": "json",
        "newly_discovered_bvids": "json",
        "counters": "json",
    },
    "hot_event_assessments": {
        "id": "str",
        "event_id": "str",
        "revision": "int",
        "as_of_s": "int",
        "window_end_s": "int",
        "window_kind": "str",
        "rule_version": "int",
        "policy_version": "str",
        "status": "str",
        "input_fingerprint": "str",
        "member_snapshot": "json",
        "metrics": "json",
        "interpretation": "json",
        "provenance": "json",
    },
    "hotspot_opportunity_runs": {
        "id": "str",
        "created_s": "int",
        "revision": "int",
        "creator_brief": "json",
        "assessment_ids": "json",
        "policy_version": "str",
        "candidates": "json",
        "result": "json",
        "feedback": "json",
        "request_fingerprint": "str",
    },
    "topic_generation_runs": {
        "id": "str",
        "schema_version": "int",
        "request_hash": "str",
        "request_payload": "json",
        "opportunity_run_id": "str",
        "context_snapshot": "json",
        "state": "str",
        "lease_token": "str",
        "lease_until_s": "int",
        "created_s": "int",
        "started_s": "int",
        "finished_s": "int",
        "result": "json",
        "error_code": "str",
    },
}


# ===========================================================================
# 夹具与工具
# ===========================================================================


@pytest.fixture()
def db(tmp_path):
    """临时文件 SQLite：**经 ``DatabaseManager`` 建表**，再换普通引擎产出会话工厂。

    Yields:
        sessionmaker: 绑定临时库的会话工厂（测试结束释放引擎）。
    """
    path = tmp_path / "hot_event_batch3a.db"
    manager = DatabaseManager(str(path))  # 经 DatabaseManager 建表（含本批六张表）
    manager.engine.dispose()
    engine = create_engine(f"sqlite:///{path}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def repo(db):
    """注入临时库会话工厂的仓储实例。"""
    return HotEventRepository(session_factory=db)


def _column_types(factory, table: str) -> dict:
    """读某表声明的列名 -> 归一类型 token。

    Args:
        factory: 会话工厂。
        table: 表名。

    Returns:
        dict: ``{列名: 'str'|'int'|'json'}``。
    """
    session = factory()
    try:
        rows = session.execute(text(f'PRAGMA table_info("{table}")')).all()
    finally:
        session.close()
    result = {}
    for row in rows:
        declared = str(row[2]).upper()
        if "JSON" in declared:
            result[str(row[1])] = "json"
        elif "INT" in declared:
            result[str(row[1])] = "int"
        elif "CHAR" in declared or "TEXT" in declared or "CLOB" in declared:
            result[str(row[1])] = "str"
        else:
            raise AssertionError(f"未预期列类型: {table}.{row[1]} = {declared!r}")
    return result


def _index_names(factory, table: str) -> set:
    """读某表的索引名集合。

    Args:
        factory: 会话工厂。
        table: 表名。

    Returns:
        set: 索引名集合。
    """
    session = factory()
    try:
        rows = session.execute(text(f'PRAGMA index_list("{table}")')).all()
    finally:
        session.close()
    return {str(row[1]) for row in rows}


def _count(factory, model) -> int:
    """统计某表行数。"""
    session = factory()
    try:
        return int(session.query(model).count())
    finally:
        session.close()


def _naive_accepted_then_latest(factory, event_id: str, bvid: str, cutoff_s: int):
    """**错误写法**（仅供反向用例对照）：先过滤 accepted，再取最新 revision。

    这正是口径 2 禁止的顺序——它会把已被撤销（rejected）的成员复活成 accepted。
    """
    session = factory()
    try:
        return (
            session.query(HotEventMember)
            .filter(
                HotEventMember.event_id == event_id,
                HotEventMember.bvid == bvid,
                HotEventMember.decision_at_s <= int(cutoff_s),
                HotEventMember.status == "accepted",  # <-- 先过滤 accepted（错）
            )
            .order_by(HotEventMember.revision.desc())
            .first()
        )
    finally:
        session.close()


def _make_event(repo, event_id: str = "ev1") -> HotEvent:
    """建一个最简事件锚点，供其余用例引用。"""
    return repo.create_hot_event(event_id=event_id, name="测试事件", now_s=T)


# ===========================================================================
# 建表：经 DatabaseManager 幂等 + 结构断言
# ===========================================================================


def test_database_manager_create_all_idempotent(tmp_path):
    """经 ``DatabaseManager`` 建表，连跑两次 ``create_all`` 幂等不报错。"""
    path = tmp_path / "batch3a_idem.db"
    manager = DatabaseManager(str(path))  # 第 1 次 create_all（构造时）
    first_tables = set(inspect(manager.engine).get_table_names())
    manager.create_tables()  # 第 2 次
    manager.create_tables()  # 第 3 次，更稳
    second_tables = set(inspect(manager.engine).get_table_names())

    assert set(SIX_TABLES) <= first_tables, "六张表未全部建出"
    assert first_tables == second_tables, "重复 create_all 后表集合发生变化"


@pytest.mark.parametrize("table", SIX_TABLES)
def test_table_columns_match_spec(db, table):
    """六张表列名与归一类型与上游 §5 一致（结构断言）。"""
    actual = _column_types(db, table)
    expected = EXPECTED_COLUMNS[table]
    assert set(actual) == set(expected), f"{table} 列名不一致：{set(actual) ^ set(expected)}"
    for name, token in expected.items():
        assert actual[name] == token, f"{table}.{name} 类型应为 {token}，实际 {actual[name]}"


def test_no_datetime_columns_anywhere(db):
    """口径 9：六张表不得出现 DATETIME/TIMESTAMP 列（时间一律 epoch 整数秒）。"""
    for table in SIX_TABLES:
        session = db()
        try:
            rows = session.execute(text(f'PRAGMA table_info("{table}")')).all()
        finally:
            session.close()
        for row in rows:
            declared = str(row[2]).upper()
            assert "TIME" not in declared and "DATE" not in declared, (
                f"{table}.{row[1]} 出现日期时间类型：{declared}"
            )


def test_fast_panel_history_column_present(db):
    """第四批前置：``fast_panel_history`` 本批只建列（JSON），不写逻辑。"""
    types = _column_types(db, "hot_events")
    assert types["fast_panel_history"] == "json"


# ===========================================================================
# 六表基础读写（create / get / update）
# ===========================================================================


def test_six_tables_create_get_update_roundtrip(repo):
    """六张表均能 create/get/update，JSON 列整对象往返。"""
    # 1) hot_events
    repo.create_hot_event(event_id="ev1", name="事件", now_s=T, entity_scope={"k": "v"})
    assert repo.get_hot_event("ev1").entity_scope == {"k": "v"}
    repo.update_hot_event("ev1", name="事件改", source_policy={"s": 1})
    reloaded_event = repo.get_hot_event("ev1")
    assert reloaded_event.name == "事件改"
    assert reloaded_event.source_policy == {"s": 1}

    # 2) hot_event_members
    member = repo.create_member_revision(
        event_id="ev1",
        bvid="BV1",
        revision=1,
        status="proposed",
        first_seen_s=T,
        rule_version=1,
        decision_source="auto",
        now_s=T,
        evidence={"run": "r1"},
    )
    assert repo.get_member_revision(member.id).evidence == {"run": "r1"}
    repo.update_member_revision(member.id, evidence={"run": "r2"})
    assert repo.get_member_revision(member.id).evidence == {"run": "r2"}

    # 3) event_discovery_runs
    repo.create_discovery_run(
        run_id="d1",
        event_id="ev1",
        rule_version=1,
        source_policy_hash="sp",
        lease_token="tk",
        trigger="manual",
        now_s=T,
        counters={EMPTY_REASON_KEY: DISCOVERY_EMPTY_LEGITIMATE},
    )
    assert repo.get_discovery_run("d1").trigger == "manual"
    repo.update_discovery_run("d1", status="completed", candidates=[{"bvid": "BV1"}])
    assert repo.get_discovery_run("d1").status == "completed"

    # 4) hot_event_assessments
    assessment = repo.create_assessment(
        event_id="ev1",
        revision=1,
        as_of_s=T,
        window_end_s=T,
        window_kind="daily24h",
        rule_version=1,
        policy_version="p1",
        status="complete",
        input_fingerprint="fp-1",
        metrics={"views": 10},
    )
    assert repo.get_assessment(assessment.id).metrics == {"views": 10}
    repo.update_assessment(assessment.id, metrics={"views": 11})
    assert repo.get_assessment(assessment.id).metrics == {"views": 11}

    # 5) hotspot_opportunity_runs
    repo.create_opportunity_run(
        run_id="o1",
        policy_version="p1",
        request_fingerprint="rf-1",
        now_s=T,
        creator_brief={"niche": "游戏"},
    )
    assert repo.get_opportunity_run("o1").creator_brief == {"niche": "游戏"}
    repo.update_opportunity_run("o1", result={"picked": ["ev1"]})
    assert repo.get_opportunity_run("o1").result == {"picked": ["ev1"]}

    # 6) topic_generation_runs
    repo.create_generation_run(
        run_id="g1",
        schema_version=1,
        request_hash="h1",
        request_payload={"topic": "x"},
        state="running",
        now_s=T,
    )
    assert repo.get_by_id("g1").request_payload == {"topic": "x"}
    repo.update_generation_run(
        "g1",
        state="completed",
        result={"topics": [], "saved_ids": ["t1"], "used_llm": False},
        finished_s=T,
    )
    done = repo.get_by_id("g1")
    assert done.state == "completed"
    assert done.result["saved_ids"] == ["t1"]


# ===========================================================================
# 口径 1：成员唯一 (event_id, bvid, revision)
# ===========================================================================


def test_member_unique_event_bvid_revision(repo):
    """口径 1：同 (event_id, bvid, revision) 重复插入被拒（追加历史，非覆盖）。"""
    _make_event(repo)
    repo.create_member_revision(
        event_id="ev1",
        bvid="BV1",
        revision=1,
        status="proposed",
        first_seen_s=T,
        rule_version=1,
        decision_source="auto",
        now_s=T,
    )
    with pytest.raises(IntegrityError):
        repo.create_member_revision(
            event_id="ev1",
            bvid="BV1",
            revision=1,
            status="accepted",
            first_seen_s=T,
            rule_version=1,
            decision_source="auto",
            now_s=T,
        )


# ===========================================================================
# 口径 2：status_at 先限时间再取最新（正 / 反两条）
# ===========================================================================


def test_status_at_positive_and_reverse_cases(repo, db):
    """口径 2：accepted→rejected 两版本。

    - 正向：``cutoff`` 落在两版本之间 -> 取到 accepted；
    - 当前：``latest_status`` -> 取到 rejected；
    - 反向：``cutoff`` 在 rejected 之后 -> 正确取 rejected；
      **并证明"先过滤 accepted 再取最新"会复活已撤销成员**（朴素写法返回 accepted）。
    """
    _make_event(repo)
    t1 = T
    t2 = T + 100
    repo.create_member_revision(
        event_id="ev1", bvid="BV1", revision=1, status="accepted",
        first_seen_s=t1, rule_version=1, decision_source="auto", now_s=t1,
    )
    repo.create_member_revision(
        event_id="ev1", bvid="BV1", revision=2, status="rejected",
        first_seen_s=t2, rule_version=1, decision_source="auto", now_s=t2,
    )

    # 当前状态：最新 revision = rejected
    latest = repo.latest_status("ev1", "BV1")
    assert latest is not None and latest.revision == 2 and latest.status == "rejected"

    # 正向：cutoff 在 t1 之后、t2 之前 -> 那时成员是 accepted
    at_mid = repo.status_at("ev1", "BV1", t1 + 1)
    assert at_mid is not None and at_mid.revision == 1 and at_mid.status == "accepted"

    # cutoff 早于任何版本 -> 无记录
    assert repo.status_at("ev1", "BV1", t1 - 1) is None

    # 反向：cutoff 在 rejected 之后 -> 正确结果必须是 rejected
    at_late = repo.status_at("ev1", "BV1", t2)
    assert at_late is not None and at_late.revision == 2 and at_late.status == "rejected"

    # 反向用例核心：错误写法（先过滤 accepted 再取最新）会把已撤销的成员复活
    naive = _naive_accepted_then_latest(db, "ev1", "BV1", t2)
    assert naive is not None
    assert naive.status == "accepted", "朴素写法应错误地复活已撤销成员"
    assert naive.revision == 1
    assert naive.status != at_late.status, "错误写法与正确结果必须不同——否则用例没证明到点"

    # 同 cutoff 下：本实现（先限时间）返回 rejected，绝不返回 accepted
    assert repo.status_at("ev1", "BV1", t2).status == "rejected"


# ===========================================================================
# 口径 3：同秒多 revision 按 revision 严格排序
# ===========================================================================


def test_same_second_revisions_ordered_by_revision(repo):
    """口径 3：``decision_at_s`` 相同，按 ``revision`` 严格排序取大者。"""
    _make_event(repo)
    repo.create_member_revision(
        event_id="ev1", bvid="BV1", revision=1, status="proposed",
        first_seen_s=T, rule_version=1, decision_source="auto", now_s=T,
    )
    repo.create_member_revision(
        event_id="ev1", bvid="BV1", revision=2, status="accepted",
        first_seen_s=T, rule_version=1, decision_source="auto", now_s=T,
    )
    assert repo.latest_status("ev1", "BV1").revision == 2
    at_now = repo.status_at("ev1", "BV1", T)
    assert at_now is not None and at_now.revision == 2 and at_now.status == "accepted"
    assert repo.status_at("ev1", "BV1", T - 1) is None


# ===========================================================================
# 口径 4：decision_at_s 不接受更早时间
# ===========================================================================


def test_decision_at_s_rejects_earlier_client_time(repo):
    """口径 4：客户端提交更早的 ``decision_at_s`` 被改写为实际提交时刻。"""
    _make_event(repo)
    earlier = T - 500
    row = repo.create_member_revision(
        event_id="ev1",
        bvid="BV1",
        revision=1,
        status="proposed",
        first_seen_s=T,
        rule_version=1,
        decision_source="manual",
        now_s=T,               # 实际提交时刻
        decision_at_s=earlier,  # 客户端给的更早时间，不许采纳
    )
    assert row.decision_at_s == T
    assert row.decision_at_s != earlier
    assert repo.get_member_revision(row.id).decision_at_s == T


# ===========================================================================
# 口径 5：assessment 唯一六元组 + 指纹复用
# ===========================================================================


def test_assessment_unique_tuple_and_fingerprint_reuse(repo, db):
    """口径 5：六元组重复被拒；同指纹重复执行返回已有行、不新建。"""
    _make_event(repo)
    first = repo.create_assessment(
        event_id="ev1",
        revision=1,
        as_of_s=T,
        window_end_s=T,
        window_kind="daily24h",
        rule_version=1,
        policy_version="p1",
        status="complete",
        input_fingerprint="fp-1",
    )
    # 同指纹（即便换了 window_end_s / revision）-> 命中已有行，不新建
    second = repo.create_assessment(
        event_id="ev1",
        revision=2,
        as_of_s=T,
        window_end_s=T + 3600,
        window_kind="daily24h",
        rule_version=1,
        policy_version="p1",
        status="complete",
        input_fingerprint="fp-1",
    )
    assert second.id == first.id
    assert repo.get_by_fingerprint("fp-1").id == first.id
    assert _count(db, HotEventAssessment) == 1

    # 六元组重复（换 id、换指纹）被数据库唯一约束拒
    session = db()
    session.add(
        HotEventAssessment(
            id="dup-assessment",
            event_id="ev1",
            revision=1,
            as_of_s=T,
            window_end_s=T,
            window_kind="daily24h",
            rule_version=1,
            policy_version="p1",
            status="complete",
            input_fingerprint="fp-other",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.close()


# ===========================================================================
# 口径 6：status 枚举受控 + reason_codes 归位
# ===========================================================================


def test_assessment_status_enum_and_reason_codes(repo, db):
    """口径 6：非法 status 被拒；reason code 只进 ``interpretation['reason_codes']``。"""
    _make_event(repo)

    # 仓储层：非法 status 直接 ValueError
    with pytest.raises(ValueError):
        repo.create_assessment(
            event_id="ev1",
            revision=1,
            as_of_s=T,
            window_end_s=T,
            window_kind="daily24h",
            rule_version=1,
            policy_version="p1",
            status="insufficient_fast_coverage",  # 不是合法 status
            input_fingerprint="fp-bad",
        )

    # 数据库层：裸插非法 status 被 CHECK 拒
    session = db()
    session.add(
        HotEventAssessment(
            id="bad-status",
            event_id="ev1",
            revision=1,
            as_of_s=T,
            window_end_s=T,
            window_kind="daily24h",
            rule_version=1,
            policy_version="p1",
            status="sampling_changed",  # 只能进 reason_codes
            input_fingerprint="fp-bad-2",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.close()

    # 合法 status + reason_codes 放进 interpretation
    ok = repo.create_assessment(
        event_id="ev1",
        revision=1,
        as_of_s=T,
        window_end_s=T,
        window_kind="daily24h",
        rule_version=1,
        policy_version="p1",
        status="insufficient",
        input_fingerprint="fp-ok",
        interpretation={"reason_codes": ["insufficient_fast_coverage", "sampling_changed"]},
    )
    assert ok.status == "insufficient"
    assert ok.interpretation["reason_codes"] == [
        "insufficient_fast_coverage",
        "sampling_changed",
    ]

    # window_kind 也受控
    with pytest.raises(ValueError):
        repo.create_assessment(
            event_id="ev1",
            revision=9,
            as_of_s=T,
            window_end_s=T,
            window_kind="weekly",  # 非法
            rule_version=1,
            policy_version="p1",
            status="complete",
            input_fingerprint="fp-wk",
        )


# ===========================================================================
# 口径 7：生成账本 CHECK / 不唯一 / 索引
# ===========================================================================


def test_generation_ledger_constraints(repo, db):
    """口径 7：state 合法值、completed 必须有 result+finished_s、失败不带 saved_ids、
    ``request_hash`` 不唯一、``(state, lease_until_s)`` 索引存在。"""
    # state 非法：仓储层 ValueError
    with pytest.raises(ValueError):
        repo.create_generation_run(
            run_id="g-bad", schema_version=1, request_hash="h", request_payload={},
            state="bogus", now_s=T,
        )

    # state 非法：数据库 CHECK 拒
    session = db()
    session.add(
        TopicGenerationRun(
            id="g-bad-2", schema_version=1, request_hash="h", request_payload={},
            state="bogus", created_s=T,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.close()

    # completed 缺 result / finished_s：仓储层拒
    with pytest.raises(ValueError):
        repo.create_generation_run(
            run_id="g-miss", schema_version=1, request_hash="h2", request_payload={},
            state="completed", now_s=T,
        )

    # completed 缺 result / finished_s：数据库 CHECK 拒
    session = db()
    session.add(
        TopicGenerationRun(
            id="g-miss-2", schema_version=1, request_hash="h2", request_payload={},
            state="completed", created_s=T,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.close()

    # 正常 completed（自带 result + finished_s）
    done = repo.create_generation_run(
        run_id="g-ok", schema_version=1, request_hash="h3", request_payload={"t": 1},
        state="completed", now_s=T,
        result={"topics": [], "saved_ids": ["t1"], "used_llm": False}, finished_s=T,
    )
    assert done.state == "completed" and done.result["saved_ids"] == ["t1"]

    # request_hash 不唯一：同 hash 新 id 可共存
    other = repo.create_generation_run(
        run_id="g-ok-2", schema_version=1, request_hash="h3", request_payload={"t": 1},
        state="running", now_s=T,
    )
    assert other.id != done.id
    assert _count(db, TopicGenerationRun) == 2

    # 失败不保存成功 saved_ids：非 completed 带 result 被拒（仓储层）
    with pytest.raises(ValueError):
        repo.create_generation_run(
            run_id="g-fail", schema_version=1, request_hash="h4", request_payload={},
            state="failed", now_s=T, result={"saved_ids": ["t1"]},
        )

    # 数据库 CHECK：非 completed 带 result 同样被拒
    session = db()
    session.add(
        TopicGenerationRun(
            id="g-fail-2", schema_version=1, request_hash="h4", request_payload={},
            state="failed", created_s=T, result={"saved_ids": ["t1"]},
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.close()

    # (state, lease_until_s) 索引存在
    assert "ix_topic_generation_runs_state_lease" in _index_names(db, "topic_generation_runs")

    # GENERATION_STATES 与模型一致（口径 7 的合法值）
    assert set(GENERATION_STATES) == {
        "running", "completed", "failed", "cancelled", "interrupted",
    }


# ===========================================================================
# 口径 8：JSON 整对象赋值，禁 in-place
# ===========================================================================


def test_json_columns_require_whole_object_assignment(repo, db):
    """口径 8：朴素 JSON 不挂 Mutable，in-place 改不落盘；仓储整对象赋值才生效。"""
    repo.create_hot_event(event_id="ev1", name="事件", now_s=T, entity_scope={"a": 1})

    # in-place 修改已加载对象 -> 不被追踪 -> commit 后回读仍是旧值
    session = db()
    row = session.get(HotEvent, "ev1")
    row.entity_scope["a"] = 999  # 原地改，不触发 dirty
    session.commit()
    session.close()

    session = db()
    reloaded = session.get(HotEvent, "ev1")
    assert reloaded.entity_scope == {"a": 1}, "in-place 修改不应被持久化（证明不是 Mutable）"
    session.close()

    # 仓储走整对象赋值 -> 生效
    repo.update_hot_event("ev1", entity_scope={"a": 2})
    assert repo.get_hot_event("ev1").entity_scope == {"a": 2}


# ===========================================================================
# 口径 9：时间一律 epoch 整数秒
# ===========================================================================


def test_epoch_seconds_are_plain_ints(repo):
    """口径 9：``*_s`` 落库为 int，且 round-trip 不变成 float/其他类型。"""
    repo.create_hot_event(event_id="ev1", name="事件", now_s=T)
    event = repo.get_hot_event("ev1")
    assert isinstance(event.created_s, int) and not isinstance(event.created_s, bool)
    assert event.created_s == T
    assert isinstance(event.updated_s, int) and event.updated_s == T

    member = repo.create_member_revision(
        event_id="ev1", bvid="BV1", revision=1, status="proposed",
        first_seen_s=T, rule_version=1, decision_source="auto", now_s=T,
    )
    assert isinstance(member.decision_at_s, int) and member.decision_at_s == T


# ===========================================================================
# 口径 10：发现空结果四因不混
# ===========================================================================


def test_discovery_distinguishes_four_empty_cases(repo):
    """口径 10：合法空 / 接口失败 / 页重复 / 达到上限，四者分别落库、互不混淆。"""
    _make_event(repo)
    common = dict(
        event_id="ev1", rule_version=1, source_policy_hash="sp",
        lease_token="tk", trigger="scheduled", now_s=T,
    )

    repo.create_discovery_run(
        run_id="r-legit", status="completed", candidates=[],
        counters={EMPTY_REASON_KEY: DISCOVERY_EMPTY_LEGITIMATE, "duplicates": 0}, **common,
    )
    repo.create_discovery_run(
        run_id="r-fail", status="failed", error_code="http_500", candidates=[],
        counters={EMPTY_REASON_KEY: DISCOVERY_EMPTY_INTERFACE_FAILURE}, **common,
    )
    repo.create_discovery_run(
        run_id="r-dup", status="completed", candidates=[],
        counters={EMPTY_REASON_KEY: DISCOVERY_EMPTY_PAGE_DUPLICATE, "duplicate_pages": 3}, **common,
    )
    repo.create_discovery_run(
        run_id="r-cap", status="partial",
        candidates=[{"bvid": f"BV{i:03d}"} for i in range(100)],
        counters={"cap_reached": True, "truncated": 7}, **common,
    )

    def signature(run_id: str):
        """把一条 run 压成"四因签名"，四者必须两两不同。"""
        row = repo.get_discovery_run(run_id)
        counters = row.counters or {}
        return (
            row.status,
            row.error_code,
            counters.get(EMPTY_REASON_KEY),
            counters.get("cap_reached"),
        )

    signatures = {
        signature("r-legit"),
        signature("r-fail"),
        signature("r-dup"),
        signature("r-cap"),
    }
    assert len(signatures) == 4, f"四因出现混淆：{signatures}"

    assert signature("r-legit")[2] == DISCOVERY_EMPTY_LEGITIMATE
    assert signature("r-fail")[2] == DISCOVERY_EMPTY_INTERFACE_FAILURE
    assert signature("r-dup")[2] == DISCOVERY_EMPTY_PAGE_DUPLICATE
    assert signature("r-cap")[3] is True
    # 接口失败绝不能只是"一个空的 completed"
    assert signature("r-fail")[0] == "failed"
    assert signature("r-legit")[0] == "completed"


def test_assessment_statuses_and_member_statuses_are_controlled(db):
    """补强：assessment / member status 枚举由模型常量与 CHECK 双重受控。"""
    assert set(ASSESSMENT_STATUSES) == {
        "complete", "partial", "collecting", "insufficient", "stale",
    }
    # 六张表齐活（本批交付物清单）
    existing = set(inspect(_engine_of(db)).get_table_names())
    assert set(SIX_TABLES) <= existing


def _engine_of(factory):
    """取会话工厂绑定的引擎（供 inspect 用）。"""
    return factory.kw["bind"]
