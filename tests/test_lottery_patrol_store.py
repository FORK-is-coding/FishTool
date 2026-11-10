"""AI 伪人巡检存储层。"""
from datetime import datetime, timedelta

import pytest

from core.database import DatabaseManager, OperationLog, PatrolComment, PatrolReport, Task
from modules.lottery.patrol import store
from modules.lottery.patrol.contracts import PreferenceConflict, build_settings

META = {"bvid": "BV1xx411c7mD", "aid": 123, "title": "T", "owner_mid": 9, "owner_name": "UP"}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    manager = DatabaseManager(str(tmp_path / "patrol_store.db"))
    monkeypatch.setattr("modules.lottery.patrol.store.get_session", manager.get_session)
    return manager


def _comment(rpid, *, mid=None, is_reply=False, root=None, score=10, tier="human", focus=False, content=None):
    return {"rpid": rpid, "mid": mid or rpid + 100, "uname": f"u{rpid}", "sign": "", "level": 3, "is_vip": False,
            "official_type": -1, "content": content or f"评论{rpid}", "ctime": datetime(2026, 11, 1),
            "like": rpid, "rcount": 0, "at_users": [], "is_reply": is_reply, "root_rpid": root, "parent_rpid": root,
            "rule_score": score, "hits": [], "hard_groups": [], "has_focus_hit": focus, "final_score": score,
            "tier": tier, "score_source": "rule", "disagreement": False}


def _report_row(rpid=1, key="rk1", mid=7, scan_id=None, mode="proxy"):
    return {"request_key": key, "mode": mode, "reporter_mid": mid, "scan_id": scan_id, "oid": 123, "rpid": rpid}


def test_upsert_is_idempotent_and_updates_scores(db):
    sid = store.create_scan("k", META, 7, {}, "v")
    store.upsert_comments(sid, [_comment(1), _comment(2)])
    store.upsert_comments(sid, [{**_comment(1), "final_score": 88, "tier": "bot"}])
    rows = {c["rpid"]: c for c in store.all_comments(sid)}
    assert len(rows) == 2 and rows[1]["final_score"] == 88 and rows[1]["tier"] == "bot"


def test_list_comments_paging_folding_and_filters(db):
    sid = store.create_scan("k", META, 7, {}, "v")
    items = [_comment(i, score=i) for i in range(1, 8)]
    items += [_comment(100, is_reply=True, root=7, score=90, tier="bot", focus=True)]
    store.upsert_comments(sid, items)
    page = store.list_comments(sid, page=1, page_size=5)
    assert page["total"] == 7 and page["total_pages"] == 2
    assert [c["rpid"] for c in page["items"]][0] == 7
    assert page["items"][0]["replies"][0]["rpid"] == 100
    only_bot = store.list_comments(sid, tier="bot")
    assert [c["rpid"] for c in only_bot["items"]] == [100]
    assert store.list_comments(sid, focus_only=True)["total"] == 1
    assert store.list_comments(sid, keyword="评论3")["total"] == 1
    assert store.list_comments(sid, page_size=999)["page_size"] == 100


def test_preferences_cas(db):
    pref = store.load_preferences()
    assert pref["revision"] == 0 and pref["include_replies"] is True and pref["proxy_enabled"] is True
    new = store.save_preferences({"include_replies": False}, expected_revision=0)
    assert new["revision"] == 1 and new["include_replies"] is False
    with pytest.raises(PreferenceConflict):
        store.save_preferences({"include_replies": True}, expected_revision=0)
    assert store.save_preferences({"bogus": 1}, None)["revision"] == 2


def test_report_sending_conflict_and_finalize_once(db):
    sid = store.create_scan("k", META, 7, {}, "v")
    rid = store.insert_report_sending(_report_row(scan_id=sid))
    assert rid > 0
    # 另一个确认框（不同 request_key）对同一评论并发提交 → 被部分唯一索引挡住
    assert store.insert_report_sending(_report_row(key="rk2", scan_id=sid)) == 0
    assert store.finalize_report(rid, "reported", 0, "") is True
    assert store.finalize_report(rid, "failed", 1, "x") is False
    assert store.has_active_report(7, 123, 1) is True


def test_failed_report_frees_slot(db):
    rid = store.insert_report_sending(_report_row())
    store.finalize_report(rid, "failed", -400, "x")
    assert store.has_active_report(7, 123, 1) is False
    assert store.insert_report_sending(_report_row(key="rk2")) > 0


def test_count_attempts_excludes_not_attempted(db):
    rid = store.insert_report_sending(_report_row(rpid=1, key="a"))
    store.finalize_report(rid, "failed", -400, "")
    rid = store.insert_report_sending(_report_row(rpid=2, key="b"))
    store.finalize_report(rid, "unknown", None, "")
    store.insert_report_simple(_report_row(rpid=3, key="c", mode="dry_run"), "dry_run")
    store.insert_report_simple(_report_row(rpid=4, key="d", mode="native"), "native_opened")
    store.insert_report_simple(_report_row(rpid=5, key="e"), "skipped")
    assert store.count_report_attempts(7, datetime.utcnow() - timedelta(hours=24)) == 2
    assert store.has_active_report(7, 123, 3) is False  # dry_run 不占位


def test_mark_native(db):
    sid = store.create_scan("k", META, 7, {}, "v")
    store.upsert_comments(sid, [_comment(4)])
    rid = store.insert_report_simple(_report_row(rpid=4, key="n", mode="native", scan_id=sid), "native_opened")
    assert store.mark_native(rid, True)["status"] == "native_done"
    assert store.all_comments(sid)[0]["report_status"] == "native_done"
    assert store.mark_native(999, True) is None


def test_cooldown_roundtrip(db):
    assert store.get_cooldown() is None
    store.set_cooldown(datetime.utcnow() + timedelta(minutes=30))
    assert store.get_cooldown() is not None
    store.set_cooldown(datetime.utcnow() - timedelta(minutes=1))
    assert store.get_cooldown() is None


def test_llm_cache_expiry(db):
    store.put_llm_cache({"a" * 64: {"ai_score": 70, "reason": "r"}}, "m")
    assert store.get_llm_cache(["a" * 64], 7)["a" * 64]["ai_score"] == 70
    assert store.get_llm_cache(["a" * 64], 0) == {}


def test_recover_on_startup(db):
    sid = store.create_scan("k", META, 7, {}, "v")
    store.upsert_comments(sid, [_comment(1)])
    store.set_comment_report_status(sid, 1, "queued")
    store.task_create("t1", "scan", {"target": "x"}, scan_id=sid)
    store.insert_report_sending(_report_row(scan_id=sid))
    out = store.recover_on_startup(7)
    assert out["interrupted"] == 1 and out["reports_unknown"] == 1
    assert store.get_scan(sid)["status"] == "interrupted"
    assert store.all_comments(sid)[0]["report_status"] == "none"
    assert store.task_load("t1")["status"] == "interrupted"


def test_task_load_isolated_from_lottery(db):
    session = db.get_session()
    session.add(Task(task_key="lot", task_type="lottery_filter", status="running"))
    session.commit()
    session.close()
    assert store.task_load("lot") is None
    store.task_create("p1", "judge", {"scope": "gray"}, scan_id=3)
    store.task_checkpoint("p1", {"pagination_str": "x"})
    loaded = store.task_load("p1")
    assert loaded["kind"] == "judge" and loaded["scan_id"] == 3 and loaded["checkpoint"] == {"pagination_str": "x"}


def test_clear_all_keeps_reports_and_nulls_scan(db):
    sid = store.create_scan("k", META, 7, {}, "v")
    store.finish_scan(sid, "completed")
    store.upsert_comments(sid, [_comment(1)])
    store.insert_report_simple(_report_row(scan_id=sid, key="x", mode="native"), "native_opened")
    out = store.clear_all(keep_reports=True)
    assert out["scans"] == 1
    session = db.get_session()
    try:
        assert session.query(PatrolComment).count() == 0
        report = session.query(PatrolReport).one()
        assert report.scan_id is None
    finally:
        session.close()


def test_prune_by_retention(db):
    sid = store.create_scan("old", META, 7, {}, "v")
    store.finish_scan(sid, "completed")
    session = db.get_session()
    from core.database import PatrolScan
    session.query(PatrolScan).update({"created_at": datetime.utcnow() - timedelta(days=30)})
    session.commit()
    session.close()
    out = store.prune(build_settings({}))
    assert out["scans"] == 1 and store.get_scan(sid) is None


def test_operation_log_drops_sensitive_keys(db):
    store.write_operation_log("report_batch", {"scan_id": 1, "content": "原文", "cookie": "SESSDATA=x", "total": 3},
                              "completed")
    session = db.get_session()
    try:
        row = session.query(OperationLog).one()
        assert row.module == "lottery_patrol" and row.details == {"scan_id": 1, "total": 3}
    finally:
        session.close()
