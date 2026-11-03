"""SemanticStore 契约测试（modules/comment/semantic/store.py）。

全部用 tmp_path 下的真实 SQLite + 真实 ORM，不打桩数据库。覆盖：
- snapshot 选取/合格判定/排除计数/limit 严格范围
- preference / set_preference CAS 与缺省
- create_task 幂等与冲突、queued 初态
- update_task 白名单（不含 groups）、取消后拒写
- reserve/settle 预算三闸门、唯一 attempt_id、重复 settle 幂等、cancelled 仍可结算
- publish_groups 校验（未知/重复/交叉/代表/不足2人）、不交叉、原子重写
- 人工保护：undo/split/allow，(bvid,rpid) 键跨任务生效、CAS409、幂等、不复活
- result 分页无遗漏、stats 去重、stale 标记
- recover_interrupted / resume（未知结果需确认）/ cancel 幂等
- 组成员不复制全文（只存 content_hash）
"""
import uuid

import pytest

from core.database import (
    Comment,
    DatabaseManager,
    Video,
    SemanticGroupMember,
    SemanticPreference,
    SemanticProtection,
)
from modules.comment.semantic.store import (
    SemanticStore,
    SemanticConflict,
    SemanticBudgetExceeded,
    SemanticNotFound,
    SemanticValidationError,
    content_hash,
    MAX_LIMIT,
)

BVID = "BV1semantic"
IDENTITY = {"api_base": "http://local.test", "model": "fake-chat"}


@pytest.fixture()
def store(tmp_path):
    """指向 tmp 库的真实 store。"""
    manager = DatabaseManager(str(tmp_path / "semantic.db"))
    return SemanticStore(session_factory=manager.get_session), manager


def seed_video(manager, bvid=BVID, title="真实标题"):
    """建视频占位记录，返回 video_id。"""
    session = manager.get_session()
    try:
        v = Video(bvid=bvid, title=title)
        session.add(v)
        session.commit()
        return v.id
    finally:
        session.close()


def seed_comment(manager, rpid, content, video_id, context="top_level", uid=1, like=0):
    """插入一条评论。"""
    session = manager.get_session()
    try:
        session.add(Comment(
            rpid=str(rpid), video_id=video_id, uid=uid, uname=f"u{uid}",
            content=content, like=like, sentiment="neutral", context_status=context,
        ))
        session.commit()
    finally:
        session.close()


def make_snapshot(store_obj, bvid=BVID, limit=200):
    """便捷取快照。"""
    return store_obj.snapshot(bvid, limit=limit)


def new_task(store_obj, manager, seed=True):
    """建视频+若干评论并创建任务，返回 (state, snapshot)。"""
    if seed:
        vid = seed_video(manager)
        seed_comment(manager, 100, "这个剧情很好看", vid)
        seed_comment(manager, 101, "剧情真不错", vid)
        seed_comment(manager, 102, "画质太差了", vid)
    snap = make_snapshot(store_obj)
    state = store_obj.create_task(snap, IDENTITY, None, request_key=uuid.uuid4().hex)
    return state, snap


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

def test_snapshot_unknown_video_raises(store):
    s, _ = store
    with pytest.raises(SemanticNotFound):
        s.snapshot("BVnope")


def test_snapshot_strips_bvid_whitespace(store):
    """issue #46：snapshot 是 create_task 的键来源，带空白入参也必须落到规范 bvid。"""
    s, m = store
    seed_video(m)
    snap = s.snapshot(f"  {BVID}\t")
    assert snap["video"]["bvid"] == BVID
    state = s.create_task(snap, IDENTITY, None, request_key=uuid.uuid4().hex)
    assert state["bvid"] == BVID
    assert s.latest_task(BVID)["task_id"] == state["task_id"]


def test_snapshot_eligibility_and_counts(store):
    s, m = store
    vid = seed_video(m)
    seed_comment(m, 1, "顶层短评", vid, context="top_level")
    seed_comment(m, 2, "回复评论", vid, context="reply")
    seed_comment(m, 3, "未知上下文", vid, context="unknown")
    seed_comment(m, 4, "x" * 5000, vid, context="top_level")  # 超长
    snap = s.snapshot(BVID)
    counts = snap["counts"]
    assert counts["raw_total"] == 4
    assert counts["selected"] == 4
    assert counts["eligible"] == 1  # 只有 rpid=1
    assert counts["excluded_context"] == 2  # reply + unknown
    assert counts["excluded_long"] == 1
    assert len(snap["eligible_ids"]) == 1
    # 所有原文都在 items 里，未静默丢弃
    assert len(snap["items"]) == 4


def test_snapshot_limit_strict_range(store):
    s, m = store
    seed_video(m)
    with pytest.raises(SemanticValidationError):
        s.snapshot(BVID, limit=MAX_LIMIT + 1)
    with pytest.raises(SemanticValidationError):
        s.snapshot(BVID, limit=0)


def test_snapshot_deterministic_newest_order(store):
    s, m = store
    vid = seed_video(m)
    for i in range(5):
        seed_comment(m, 200 + i, f"评论{i}", vid)
    snap = s.snapshot(BVID, limit=3)
    # 取最新 3 条(id 大)，反转为老->新输入顺序
    rpids = [it["rpid"] for it in snap["items"]]
    assert rpids == ["202", "203", "204"]
    assert snap["counts"]["outside_scope"] == 2


# ---------------------------------------------------------------------------
# preference
# ---------------------------------------------------------------------------

def test_preference_default_no_row(store):
    s, m = store
    seed_video(m)
    pref = s.preference(BVID)
    assert pref == {"auto_fold": True, "revision": 0, "consent_version": None, "model_identity": None}


def _pref_rows(manager):
    """读出 semantic_preferences 全部 (bvid, video_id)。"""
    session = manager.get_session()
    try:
        return [(p.bvid, p.video_id) for p in session.query(SemanticPreference).all()]
    finally:
        session.close()


def test_preference_unknown_video_raises(store):
    """issue #45：视频不存在时与 snapshot 契约一致抛 NotFound，不返回缺省 DTO。"""
    s, _ = store
    with pytest.raises(SemanticNotFound):
        s.preference("BVghost00000")


def test_set_preference_unknown_video_raises_and_writes_nothing(store):
    """issue #45：视频不存在时拒写，不落 video_id 为 NULL 的孤儿行。"""
    s, m = store
    with pytest.raises(SemanticNotFound):
        s.set_preference("BVghost00000", 0, auto_fold=True)
    assert _pref_rows(m) == []


def test_set_preference_links_video_id(store):
    s, m = store
    vid = seed_video(m)
    s.set_preference(BVID, 0, auto_fold=False)
    assert _pref_rows(m) == [(BVID, vid)]


def test_set_preference_backfills_legacy_null_video_id(store):
    """修复前遗留的 video_id=NULL 行：视频存在时下一次写入回填，不涨 revision。"""
    s, m = store
    vid = seed_video(m)
    session = m.get_session()
    try:
        session.add(SemanticPreference(bvid=BVID, video_id=None, auto_fold=True, revision=0))
        session.commit()
    finally:
        session.close()
    pref = s.set_preference(BVID, 0, auto_fold=True)  # 无实际变更
    assert pref["revision"] == 0
    assert _pref_rows(m) == [(BVID, vid)]


def test_bvid_whitespace_normalized_to_same_row(store):
    """issue #46：首尾空白不能产生第二条偏好，读写都落到同一个键。"""
    s, m = store
    vid = seed_video(m)
    s.set_preference(f" {BVID}", 0, auto_fold=False)
    assert s.preference(f"{BVID} ")["auto_fold"] is False
    s.set_preference(f"\t{BVID}\n", 0, auto_fold=True)  # 命中同一行：revision 0 -> 1
    assert s.preference(BVID)["revision"] == 1
    with pytest.raises(SemanticConflict):  # 若空白变体是新键，这里会以 rev0 静默新建
        s.set_preference(f" {BVID}", 0, auto_fold=False)
    assert _pref_rows(m) == [(BVID, vid)]


@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_store_rejects_blank_or_non_string_bvid(store, bad):
    s, _ = store
    with pytest.raises(SemanticValidationError):
        s.preference(bad)
    with pytest.raises(SemanticValidationError):
        s.set_preference(bad, 0, auto_fold=True)
    with pytest.raises(SemanticValidationError):
        s.latest_task(bad)


def test_set_preference_cas_and_revision(store):
    s, m = store
    seed_video(m)
    # 首次创建安全落在 revision 0（契约：creates initial revision0 safely）
    p0 = s.set_preference(BVID, 0, auto_fold=False, consent_version="semantic-fold-v1")
    assert p0["auto_fold"] is False
    assert p0["revision"] == 0
    # 已存在后再改：陈旧修订冲突（期望仍传 0，但已有变更会 +1）
    p1 = s.set_preference(BVID, 0, auto_fold=True)
    assert p1["revision"] == 1
    with pytest.raises(SemanticConflict):
        s.set_preference(BVID, 0, auto_fold=False)  # 陈旧
    # 无变更不涨修订
    p_same = s.set_preference(BVID, 1, auto_fold=True)
    assert p_same["revision"] == 1


def test_set_preference_initial_requires_rev0(store):
    s, m = store
    seed_video(m)
    with pytest.raises(SemanticConflict):
        s.set_preference(BVID, 5, auto_fold=True)


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------

def test_create_task_queued_and_idempotent(store):
    s, m = store
    state, snap = new_task(s, m)
    assert state["status"] == "queued"
    assert state["phase"] == "queued"
    assert state["revision"] == 0
    rk = uuid.uuid4().hex
    a = s.create_task(snap, IDENTITY, None, request_key=rk)
    b = s.create_task(snap, IDENTITY, None, request_key=rk)
    assert a["task_id"] == b["task_id"]  # 幂等返回同任务


def test_create_task_idem_mismatch_conflict(store):
    s, m = store
    _, snap = new_task(s, m)
    rk = uuid.uuid4().hex
    s.create_task(snap, IDENTITY, None, request_key=rk)
    other = {"api_base": "http://local.test", "model": "different"}
    with pytest.raises(SemanticConflict):
        s.create_task(snap, other, None, request_key=rk)


# ---------------------------------------------------------------------------
# update_task
# ---------------------------------------------------------------------------

def test_update_task_whitelist_rejects_groups(store):
    s, m = store
    state, _ = new_task(s, m)
    with pytest.raises(SemanticValidationError):
        s.update_task(state["task_id"], state["revision"], groups=[{"x": 1}])


@pytest.mark.parametrize("field,value", [
    ("progress", {"done": 1}), ("progress", []), ("progress", "0.5"),
    ("progress", None), ("progress", True), ("progress", False),
    ("progress", -0.01), ("progress", 1.01), ("progress", 10 ** 400),
    ("progress", float("nan")), ("progress", float("inf")),
    ("progress", float("-inf")),
    ("error", {"message": "private-marker"}), ("error", ["private-marker"]),
    ("error", 1), ("error", False), ("error", b"private-marker"),
])
def test_update_task_invalid_scalar_types_leave_state_unchanged(store, field, value):
    """类型错误在写库前拒绝；与其同传的有效字段也不能部分落库。"""
    s, m = store
    before, _ = new_task(s, m)
    with pytest.raises(SemanticValidationError) as exc:
        s.update_task(before["task_id"], before["revision"],
                      status="running", phase="batch", checkpoint={"changed": True},
                      **{field: value})
    assert "private-marker" not in str(exc.value)
    after = s.get_task(before["task_id"])
    for key in ("revision", "status", "phase", "progress", "error", "checkpoint"):
        assert after[key] == before[key]


@pytest.mark.parametrize("progress", [0, 1, 0.0, 0.5, 1.0])
@pytest.mark.parametrize("error", [None, "", "请求失败，请重试"])
def test_update_task_valid_scalar_types_round_trip(store, progress, error):
    s, m = store
    before, _ = new_task(s, m)
    after = s.update_task(before["task_id"], before["revision"],
                          status="running", progress=progress, error=error)
    assert after["revision"] == before["revision"] + 1
    assert after["progress"] == progress
    assert after["error"] == error


def test_update_task_cas_and_cancel_block(store):
    s, m = store
    state, _ = new_task(s, m)
    r1 = s.update_task(state["task_id"], 0, status="running")
    assert r1["status"] == "running"
    assert r1["revision"] == 1
    with pytest.raises(SemanticConflict):
        s.update_task(state["task_id"], 0, phase="batch")  # 陈旧
    s.cancel_task(state["task_id"], r1["revision"])
    cur = s.get_task(state["task_id"])
    with pytest.raises(SemanticConflict):
        s.update_task(state["task_id"], cur["revision"], status="completed")


# ---------------------------------------------------------------------------
# reserve / settle budget
# ---------------------------------------------------------------------------

def test_reserve_settle_happy_and_unique_attempt(store):
    s, m = store
    state, _ = new_task(s, m)
    r = s.update_task(state["task_id"], 0, status="running")
    res = s.reserve_attempt(state["task_id"], r["revision"], 500)
    inflight = res["budget"]["in_flight"]
    assert inflight is not None and inflight["estimated_tokens"] == 500
    aid1 = inflight["attempt_id"]
    # 结算实际用量
    settled = s.settle_attempt(state["task_id"], res["revision"], usage_tokens=400)
    assert settled["budget"]["in_flight"] is None
    assert settled["budget"]["settled_tokens"] == 400
    # 下一次预约 attempt_id 不同
    res2 = s.reserve_attempt(state["task_id"], settled["revision"], 100)
    assert res2["budget"]["in_flight"]["attempt_id"] != aid1


def test_reserve_rejects_concurrent_inflight(store):
    s, m = store
    state, _ = new_task(s, m)
    r = s.update_task(state["task_id"], 0, status="running")
    res = s.reserve_attempt(state["task_id"], r["revision"], 100)
    with pytest.raises(SemanticConflict):
        s.reserve_attempt(state["task_id"], res["revision"], 100)


def test_reserve_task_token_budget_exceeded(store):
    s, m = store
    _, snap = new_task(s, m)
    state = s.create_task(snap, IDENTITY, {"task_token_budget": 300}, request_key=uuid.uuid4().hex)
    r = s.update_task(state["task_id"], 0, status="running")
    with pytest.raises(SemanticBudgetExceeded):
        s.reserve_attempt(state["task_id"], r["revision"], 500)


def test_reserve_daily_budget_exceeded(store):
    s, m = store
    _, snap = new_task(s, m)
    state = s.create_task(snap, IDENTITY, {"daily_token_budget": 200}, request_key=uuid.uuid4().hex)
    r = s.update_task(state["task_id"], 0, status="running")
    with pytest.raises(SemanticBudgetExceeded):
        s.reserve_attempt(state["task_id"], r["revision"], 500)


def test_reserve_max_attempts_exceeded(store):
    s, m = store
    _, snap = new_task(s, m)
    state = s.create_task(snap, IDENTITY, {"max_attempts": 1}, request_key=uuid.uuid4().hex)
    r = s.update_task(state["task_id"], 0, status="running")
    res = s.reserve_attempt(state["task_id"], r["revision"], 10)
    st = s.settle_attempt(state["task_id"], res["revision"], 10)
    with pytest.raises(SemanticBudgetExceeded):
        s.reserve_attempt(state["task_id"], st["revision"], 10)


def test_settle_idempotent_no_inflight(store):
    s, m = store
    state, _ = new_task(s, m)
    r = s.update_task(state["task_id"], 0, status="running")
    res = s.reserve_attempt(state["task_id"], r["revision"], 100)
    s1 = s.settle_attempt(state["task_id"], res["revision"], 100)
    # 重复 settle：幂等 no-op，不重复记账
    s2 = s.settle_attempt(state["task_id"], s1["revision"], 999)
    assert s2["budget"]["settled_tokens"] == s1["budget"]["settled_tokens"] == 100


def test_settle_on_cancel_accounting_only(store):
    s, m = store
    state, _ = new_task(s, m)
    r = s.update_task(state["task_id"], 0, status="running")
    res = s.reserve_attempt(state["task_id"], r["revision"], 100)
    c = s.cancel_task(state["task_id"], res["revision"])
    # 取消后仍可结算（只记账）
    settled = s.settle_attempt(state["task_id"], c["revision"], 80)
    assert settled["status"] == "cancelled"
    assert settled["budget"]["settled_tokens"] == 80
    assert settled["budget"]["in_flight"] is None


@pytest.mark.parametrize("usage_tokens", [60, None])
def test_task_budget_counts_settled_on_next_attempt(store, usage_tokens):
    """结算不会重置任务预算；缺 usage 的估算也持续占用额度。"""
    s, m = store
    _, snap = new_task(s, m)
    task = s.create_task(snap, IDENTITY, {"task_token_budget": 100}, uuid.uuid4().hex)
    task = s.update_task(task["task_id"], task["revision"], status="running")
    task = s.reserve_attempt(task["task_id"], task["revision"], 60)
    task = s.settle_attempt(task["task_id"], task["revision"], usage_tokens)
    assert task["budget"]["reserved_tokens"] == 0
    assert task["budget"]["settled_tokens"] == 60
    with pytest.raises(SemanticBudgetExceeded):
        s.reserve_attempt(task["task_id"], task["revision"], 60)
    # 恰好剩余 40 可用，证明未将已结算费用重复计入 reserved。
    task = s.reserve_attempt(task["task_id"], task["revision"], 40)
    assert task["budget"]["reserved_tokens"] + task["budget"]["settled_tokens"] == 100


@pytest.mark.parametrize("usage_tokens", [60, None])
def test_daily_budget_counts_settled_across_tasks(store, usage_tokens):
    """同日第二个任务必须计入第一个任务已结算的实际/估算用量。"""
    s, m = store
    _, snap = new_task(s, m)
    limits = {"daily_token_budget": 100}
    first = s.create_task(snap, IDENTITY, limits, uuid.uuid4().hex)
    first = s.update_task(first["task_id"], first["revision"], status="running")
    first = s.reserve_attempt(first["task_id"], first["revision"], 60)
    first = s.settle_attempt(first["task_id"], first["revision"], usage_tokens)
    assert first["budget"]["daily_reserved"] == 0
    assert first["budget"]["daily_settled"] == 60
    second = s.create_task(snap, IDENTITY, limits, uuid.uuid4().hex)
    second = s.update_task(second["task_id"], second["revision"], status="running")
    with pytest.raises(SemanticBudgetExceeded):
        s.reserve_attempt(second["task_id"], second["revision"], 60)
    second = s.reserve_attempt(second["task_id"], second["revision"], 40)
    assert second["budget"]["daily_reserved"] + second["budget"]["daily_settled"] == 100


def test_reservation_date_sampled_once_for_lookup_and_inflight(store, monkeypatch):
    """午夜日期序列：查询命中的日行与 in_flight 的预约日期必须一致。"""
    from core.database import SemanticDailyUsage
    import modules.comment.semantic.store as store_mod
    s, m = store
    task, _ = new_task(s, m)
    task = s.update_task(task["task_id"], task["revision"], status="running")
    with m.get_session() as session:
        session.add(SemanticDailyUsage(
            date="2026-11-01", reserved_tokens=10, settled_tokens=20, request_count=1,
        ))
        session.commit()
    dates = iter(["2026-11-01", "2026-11-02"])
    monkeypatch.setattr(store_mod, "_today", lambda: next(dates, "2026-11-02"))
    reserved = s.reserve_attempt(task["task_id"], task["revision"], 60)
    assert reserved["budget"]["in_flight"]["reserve_date"] == "2026-11-01"
    s.settle_attempt(task["task_id"], reserved["revision"], usage_tokens=50)
    with m.get_session() as session:
        day = session.query(SemanticDailyUsage).filter_by(date="2026-11-01").one()
        assert (day.reserved_tokens, day.settled_tokens) == (10, 70)
        assert session.query(SemanticDailyUsage).filter_by(date="2026-11-02").first() is None


def test_settle_cross_midnight_does_not_touch_other_day_reservation(store, monkeypatch):
    """跨午夜结算：预约记在 day A，结算发生在 day B。

    必须对 day A 回收预约，day B 的 reserved_tokens（代表当天别的任务活跃预约）
    绝不被扣减，否则日预算会被绕过。
    """
    import modules.comment.semantic.store as store_mod
    s, m = store
    stateA, _ = new_task(s, m)
    rA = s.update_task(stateA["task_id"], 0, status="running")

    # day A 预约 300
    monkeypatch.setattr(store_mod, "_today", lambda: "2026-11-01")
    reservedA = s.reserve_attempt(stateA["task_id"], rA["revision"], 300)
    assert reservedA["budget"]["daily_reserved"] == 300

    # 切到 day B：另一个任务在当天预约 400（代表 day B 的活跃预约）
    monkeypatch.setattr(store_mod, "_today", lambda: "2026-11-02")
    _, snapB = new_task(s, m, seed=False)  # 复用同视频
    stateB = s.create_task(snapB, IDENTITY, None, request_key=uuid.uuid4().hex)
    rB = s.update_task(stateB["task_id"], 0, status="running")
    reservedB = s.reserve_attempt(stateB["task_id"], rB["revision"], 400)
    assert reservedB["budget"]["daily_reserved"] == 400  # 仅 B 自己的预约

    # 在 day B 结算 A 的请求：day B 预约不受影响，day A 预约被回收
    s.settle_attempt(stateA["task_id"], reservedA["revision"], usage_tokens=250)

    session = m.get_session()
    try:
        from core.database import SemanticDailyUsage
        dayA = session.query(SemanticDailyUsage).filter_by(date="2026-11-01").first()
        dayB = session.query(SemanticDailyUsage).filter_by(date="2026-11-02").first()
        # day A：预约 300 回收至 0，结算入账 250
        assert dayA.reserved_tokens == 0
        assert dayA.settled_tokens == 250
        # day B：仍保有另一任务的 400 预约，未被 A 的结算污染
        assert dayB.reserved_tokens == 400
    finally:
        session.close()


def test_create_task_duplicate_key_different_snapshot_conflict_db(store):
    """DB 层 (bvid,request_key) 唯一：同键不同快照直接 409（含应用复查）。"""
    s, m = store
    _, snap = new_task(s, m)
    rk = uuid.uuid4().hex
    s.create_task(snap, IDENTITY, None, request_key=rk)
    # 构造不同快照（改一条内容哈希）：应 409
    snap2 = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
             for k, v in snap.items()}
    snap2["items"] = [dict(it) for it in snap["items"]]
    snap2["items"][0]["content_hash"] = "deadbeef"
    with pytest.raises(SemanticConflict):
        s.create_task(snap2, IDENTITY, None, request_key=rk)


def test_settle_invalid_usage_keeps_estimate(store):
    s, m = store
    state, _ = new_task(s, m)
    r = s.update_task(state["task_id"], 0, status="running")
    res = s.reserve_attempt(state["task_id"], r["revision"], 250)
    settled = s.settle_attempt(state["task_id"], res["revision"], usage_tokens=None)
    assert settled["budget"]["settled_tokens"] == 250  # 保留估算
    assert settled["coverage"].get("last_settlement_estimated") is True


# ---------------------------------------------------------------------------
# publish_groups
# ---------------------------------------------------------------------------

def running_task_with_two_equiv(store_obj, manager):
    """建含两条近义顶层评论的 running 任务，返回 (state, snapshot)。"""
    vid = seed_video(manager)
    seed_comment(manager, 100, "这个剧情很好看", vid)
    seed_comment(manager, 101, "剧情真不错", vid)
    seed_comment(manager, 102, "画质太差了", vid)
    snap = store_obj.snapshot(BVID)
    state = store_obj.create_task(snap, IDENTITY, None, request_key=uuid.uuid4().hex)
    state = store_obj.update_task(state["task_id"], 0, status="running")
    return state, snap


def equiv_group(snap, rpids):
    """根据 snapshot 把 rpid 列表映射为短 ID 组。"""
    by_rpid = {it["rpid"]: it["id"] for it in snap["items"]}
    ids = [by_rpid[str(r)] for r in rpids]
    return {"representative_id": ids[0], "member_ids": ids, "reason": "近义", "aspect": "剧情", "stance": "正面"}


def test_publish_groups_happy(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    g = equiv_group(snap, [100, 101])
    res = s.publish_groups(state["task_id"], state["revision"], [g])
    assert len(res["groups"]) == 1
    grp = res["groups"][0]
    assert grp["group_id"]
    assert set(grp["current_folded_rpids"]) == {"100", "101"}
    assert grp["original_count"] == 2


def test_publish_rejects_unknown_id(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    bad = {"representative_id": "c0", "member_ids": ["c0", "cZZZ"], "reason": "x"}
    with pytest.raises(SemanticValidationError):
        s.publish_groups(state["task_id"], state["revision"], [bad])


def test_publish_rejects_rep_not_in_group(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    by_rpid = {it["rpid"]: it["id"] for it in snap["items"]}
    bad = {"representative_id": by_rpid["102"], "member_ids": [by_rpid["100"], by_rpid["101"]]}
    with pytest.raises(SemanticValidationError):
        s.publish_groups(state["task_id"], state["revision"], [bad])


def test_publish_rejects_cross_group_overlap(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    g1 = equiv_group(snap, [100, 101])
    g2 = equiv_group(snap, [101, 102])  # 101 交叉
    with pytest.raises(SemanticValidationError):
        s.publish_groups(state["task_id"], state["revision"], [g1, g2])


def test_publish_rejects_under_two(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    by_rpid = {it["rpid"]: it["id"] for it in snap["items"]}
    with pytest.raises(SemanticValidationError):
        s.publish_groups(state["task_id"], state["revision"],
                         [{"representative_id": by_rpid["100"], "member_ids": [by_rpid["100"]]}])


def test_publish_rejects_when_cancelled(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    c = s.cancel_task(state["task_id"], state["revision"])
    g = equiv_group(snap, [100, 101])
    with pytest.raises(SemanticConflict):
        s.publish_groups(state["task_id"], c["revision"], [g])


def test_publish_atomic_rewrite(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    r1 = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    r2 = s.publish_groups(state["task_id"], r1["revision"], [equiv_group(snap, [100, 102])])
    assert len(r2["groups"]) == 1
    assert set(r2["groups"][0]["current_folded_rpids"]) == {"100", "102"}
    # 旧成员行被原子清掉：不残留 101
    session = m.get_session()
    try:
        rpids = {str(x.rpid) for x in session.query(SemanticGroupMember).filter_by(task_id=state["task_id"]).all()}
        assert rpids == {"100", "102"}
    finally:
        session.close()


def test_group_members_do_not_copy_fulltext(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    session = m.get_session()
    try:
        members = session.query(SemanticGroupMember).filter_by(task_id=state["task_id"]).all()
        for mem in members:
            # 只存 content_hash 与短 ID→rpid，不存原文；content_hash 不等于原文
            assert mem.content_hash and mem.content_hash != "这个剧情很好看"
            assert not hasattr(mem, "content") or getattr(mem, "content", None) is None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# manual protection / decide
# ---------------------------------------------------------------------------

def test_undo_group_protects_all_members(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    pub = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    gid = pub["groups"][0]["group_id"]
    dec = s.decide(BVID, state["task_id"], "undo_group", 0, group_id=gid, request_key=uuid.uuid4().hex)
    assert set(dec["affected_rpids"]) == {"100", "101"}
    # 再读 result：组内全成员 keep_separate，folded 为空
    res = s.result(state["task_id"])
    grp = res["groups"][0]
    assert grp["current_folded_rpids"] == []
    assert all(mem["keep_separate"] for mem in grp["members"])


def test_split_member_protects_one(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    pub = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    s.decide(BVID, state["task_id"], "split_member", 0, rpid="100", request_key=uuid.uuid4().hex)
    res = s.result(state["task_id"])
    grp = res["groups"][0]
    folded = set(grp["current_folded_rpids"])
    assert "100" not in folded and "101" in folded


def test_decide_cas_conflict(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    pub = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    gid = pub["groups"][0]["group_id"]
    s.decide(BVID, state["task_id"], "undo_group", 0, group_id=gid, request_key=uuid.uuid4().hex)
    # 修订已 +1，用旧 0 冲突
    with pytest.raises(SemanticConflict):
        s.decide(BVID, state["task_id"], "allow_group", 0, group_id=gid, request_key=uuid.uuid4().hex)


def test_decide_idempotent_request_key(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    pub = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    gid = pub["groups"][0]["group_id"]
    rk = uuid.uuid4().hex
    d1 = s.decide(BVID, state["task_id"], "undo_group", 0, group_id=gid, request_key=rk)
    d2 = s.decide(BVID, state["task_id"], "undo_group", 0, group_id=gid, request_key=rk)
    assert d2["idempotent"] is True
    assert d1["revision"] == d2["revision"]


def _delete_video(manager, bvid=BVID):
    """模拟视频行事后缺失（任务/审计仍在）。"""
    session = manager.get_session()
    try:
        session.query(Video).filter_by(bvid=bvid).delete()
        session.commit()
    finally:
        session.close()


def test_result_and_decide_replay_survive_missing_video(store):
    """回归：存在性校验只加在公共入口；已持久化任务的结果与幂等重放不因视频缺失而 404。"""
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    pub = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    gid = pub["groups"][0]["group_id"]
    rk = uuid.uuid4().hex
    s.decide(BVID, state["task_id"], "undo_group", 0, group_id=gid, request_key=rk)
    _delete_video(m)

    res = s.result(state["task_id"])
    assert res["preference"]["revision"] == 1
    replay = s.decide(BVID, state["task_id"], "undo_group", 0, group_id=gid, request_key=rk)
    assert replay["idempotent"] is True
    assert replay["preference"]["revision"] == 1
    # 公共入口仍按契约 404。
    with pytest.raises(SemanticNotFound):
        s.preference(BVID)


def test_decide_first_row_unknown_video_writes_nothing(store):
    """decide 首次建偏好行时视频已缺失：拒写，不落 video_id 为 NULL 的孤儿行。"""
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    pub = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    gid = pub["groups"][0]["group_id"]
    _delete_video(m)
    with pytest.raises(SemanticNotFound):
        s.decide(BVID, state["task_id"], "undo_group", 0, group_id=gid, request_key=uuid.uuid4().hex)
    assert _pref_rows(m) == []
    session = m.get_session()
    try:
        assert session.query(SemanticProtection).count() == 0  # 整体回滚，保护行也未写
    finally:
        session.close()


def test_decide_strips_bvid_whitespace(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    pub = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    gid = pub["groups"][0]["group_id"]
    s.decide(f" {BVID} ", state["task_id"], "undo_group", 0, group_id=gid, request_key=uuid.uuid4().hex)
    assert [r[0] for r in _pref_rows(m)] == [BVID]


def test_protection_survives_regroup_new_task(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    pub = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    s.decide(BVID, state["task_id"], "split_member", 0, rpid="100", request_key=uuid.uuid4().hex)
    # 新任务重新分组，代表换成 101 也不能复活对 100 的折叠
    snap2 = s.snapshot(BVID)
    state2 = s.create_task(snap2, IDENTITY, None, request_key=uuid.uuid4().hex)
    state2 = s.update_task(state2["task_id"], 0, status="running")
    by_rpid = {it["rpid"]: it["id"] for it in snap2["items"]}
    g = {"representative_id": by_rpid["101"], "member_ids": [by_rpid["101"], by_rpid["100"]],
         "reason": "近义"}
    pub2 = s.publish_groups(state2["task_id"], state2["revision"], [g])
    folded = set(pub2["groups"][0]["current_folded_rpids"]) if pub2["groups"] else set()
    assert "100" not in folded


def test_allow_member_clears_protection(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    pub = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    d1 = s.decide(BVID, state["task_id"], "split_member", 0, rpid="100", request_key=uuid.uuid4().hex)
    d2 = s.decide(BVID, state["task_id"], "allow_member", d1["revision"], rpid="100", request_key=uuid.uuid4().hex)
    session = m.get_session()
    try:
        row = session.query(SemanticProtection).filter_by(bvid=BVID, rpid="100").first()
        assert row is not None and row.keep_separate is False
    finally:
        session.close()


# ---------------------------------------------------------------------------
# result / recover / resume
# ---------------------------------------------------------------------------

def test_result_pagination_no_loss_and_dedup(store):
    s, m = store
    vid = seed_video(m)
    for i in range(5):
        seed_comment(m, 300 + i, f"评论{i}", vid, uid=(1 if i < 3 else 2))
    snap = s.snapshot(BVID)
    state = s.create_task(snap, IDENTITY, None, request_key=uuid.uuid4().hex)
    res = s.result(state["task_id"], page=1, page_size=2)
    assert res["pagination"]["total"] == 5
    assert res["pagination"]["pages"] == 3
    assert len(res["items"]) == 2
    # 独立用户数按 uid 去重
    assert res["counts"]["independent_users"] == 2


def test_update_task_rejects_illegal_status_and_terminal(store):
    """status 白名单：cancelled/interrupted 不能走 update_task；终态拒绝改写。"""
    s, m = store
    state, _ = new_task(s, m)
    # 非法 status 值
    with pytest.raises(SemanticValidationError):
        s.update_task(state["task_id"], 0, status="cancelled")
    with pytest.raises(SemanticValidationError):
        s.update_task(state["task_id"], 0, status="interrupted")
    # 推进到 failed（终态），之后任何 worker update 被拒
    r = s.update_task(state["task_id"], 0, status="running")
    f = s.update_task(state["task_id"], r["revision"], status="failed")
    with pytest.raises(SemanticConflict):
        s.update_task(state["task_id"], f["revision"], status="running")


def test_deleted_comment_is_stale_and_not_folded(store):
    """被删评论(current_hash None)必须 stale 且不自动折叠。"""
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    # 删除其中一条原文
    session = m.get_session()
    try:
        session.query(Comment).filter_by(rpid="100").delete()
        session.commit()
    finally:
        session.close()
    res = s.result(state["task_id"])
    grp = res["groups"][0]
    assert grp["stale"] is True
    assert "100" not in set(grp["current_folded_rpids"])  # 删除成员不折叠
    mem100 = next(x for x in grp["members"] if x["rpid"] == "100")
    assert mem100["stale"] is True


def test_group_id_stable_across_republish_same_membership(store):
    """同 membership 重发 group_id 稳定；membership 变化得到新 ID。"""
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    r1 = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    gid1 = r1["groups"][0]["group_id"]
    # 同成员集合重发：ID 不变
    r2 = s.publish_groups(state["task_id"], r1["revision"], [equiv_group(snap, [100, 101])])
    gid2 = r2["groups"][0]["group_id"]
    assert gid1 == gid2
    # membership 变化（换成 100,102）：新 ID
    r3 = s.publish_groups(state["task_id"], r2["revision"], [equiv_group(snap, [100, 102])])
    gid3 = r3["groups"][0]["group_id"]
    assert gid3 != gid1


def test_result_over_50_raw_page2_and_total_pages(store):
    """>50 原始评论：page2 可达、total_pages 正确、无成员丢失。"""
    s, m = store
    vid = seed_video(m)
    for i in range(55):
        seed_comment(m, 400 + i, f"评论{i}", vid)
    snap = s.snapshot(BVID, limit=500)
    state = s.create_task(snap, IDENTITY, None, request_key=uuid.uuid4().hex)
    res1 = s.result(state["task_id"], page=1, page_size=50)
    assert res1["pagination"]["total"] == 55
    assert res1["pagination"]["total_pages"] == 2
    assert res1["pagination"]["pages"] == 2  # 兼容别名
    assert len(res1["items"]) == 50
    res2 = s.result(state["task_id"], page=2, page_size=50)
    assert len(res2["items"]) == 5
    # page1+page2 覆盖全部 rpid，无遗漏
    seen = {it["rpid"] for it in res1["items"]} | {it["rpid"] for it in res2["items"]}
    assert len(seen) == 55


def test_group_members_carry_full_snapshot_text(store):
    """组成员/代表携带完整原文+情感+用户字段（从快照 local_id 回填）。"""
    s, m = store
    vid = seed_video(m)
    seed_comment(m, 100, "这个剧情很好看", vid, uid=11, like=5)
    seed_comment(m, 101, "剧情真不错", vid, uid=22, like=3)
    seed_comment(m, 102, "画质太差了", vid, uid=33)
    snap = s.snapshot(BVID)
    state = s.create_task(snap, IDENTITY, None, request_key=uuid.uuid4().hex)
    state = s.update_task(state["task_id"], 0, status="running")
    pub = s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    grp = pub["groups"][0]
    assert grp["representative_content"] in ("这个剧情很好看", "剧情真不错")
    contents = {mem["content"] for mem in grp["members"]}
    assert contents == {"这个剧情很好看", "剧情真不错"}
    assert all("uname" in mem and "sentiment" in mem and "ctime" in mem for mem in grp["members"])
    # uid 完整携带
    assert {mem["uid"] for mem in grp["members"]} == {11, 22}


def test_result_stale_flag(store):
    s, m = store
    state, snap = running_task_with_two_equiv(s, m)
    s.publish_groups(state["task_id"], state["revision"], [equiv_group(snap, [100, 101])])
    # 改动原文 -> stale
    session = m.get_session()
    try:
        c = session.query(Comment).filter_by(rpid="100").first()
        c.content = "完全不同的新内容"
        session.commit()
    finally:
        session.close()
    res = s.result(state["task_id"])
    grp = res["groups"][0]
    assert grp["stale"] is True


def test_recover_and_resume_requires_ack(store):
    s, m = store
    state, _ = new_task(s, m)
    r = s.update_task(state["task_id"], 0, status="running")
    s.reserve_attempt(state["task_id"], r["revision"], 100)
    affected = s.recover_interrupted()
    assert affected == 1
    cur = s.get_task(state["task_id"])
    assert cur["status"] == "interrupted"
    assert cur["budget"]["in_flight"]["outcome_unknown"] is True
    # 未确认未知结果 -> 拒绝
    with pytest.raises(SemanticConflict):
        s.resume_task(state["task_id"], cur["revision"], acknowledge_unknown=False)
    resumed = s.resume_task(state["task_id"], cur["revision"], acknowledge_unknown=True)
    assert resumed["status"] == "queued"
    # 既有计费保留
    assert resumed["budget"]["reserved_tokens"] >= 100


def test_cancel_idempotent(store):
    s, m = store
    state, _ = new_task(s, m)
    c1 = s.cancel_task(state["task_id"], 0)
    c2 = s.cancel_task(state["task_id"], 999)  # 幂等，不校验修订
    assert c1["status"] == c2["status"] == "cancelled"


def test_get_task_unknown_raises(store):
    s, _ = store
    with pytest.raises(SemanticNotFound):
        s.get_task(999999)
