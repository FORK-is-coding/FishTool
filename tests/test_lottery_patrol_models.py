"""AI 伪人巡检数据模型：建表、部分唯一索引、级联 / 置空行为。"""
from datetime import datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from core.database import (
    DatabaseManager,
    PatrolComment,
    PatrolLlmCache,
    PatrolPreference,
    PatrolReport,
    PatrolScan,
)


@pytest.fixture()
def manager(tmp_path):
    return DatabaseManager(str(tmp_path / "patrol_models.db"))


def _scan(session, key="k1"):
    scan = PatrolScan(scan_key=key, bvid="BV1xx411c7mD", aid=100, title="t")
    session.add(scan)
    session.commit()
    return scan


def _report(scan_id, *, rpid=1, status="reported", request_key="r1", mid=9):
    return PatrolReport(
        request_key=request_key, mode="proxy", reporter_mid=mid, scan_id=scan_id,
        oid=100, type=1, rpid=rpid, status=status,
    )


def test_tables_created(manager):
    names = set(inspect(manager.engine).get_table_names())
    assert {"patrol_scans", "patrol_comments", "patrol_reports",
            "patrol_preferences", "patrol_llm_cache"} <= names


def test_partial_unique_index_blocks_duplicate_active_reports(manager):
    session = manager.get_session()
    try:
        scan = _scan(session)
        session.add(_report(scan.id, request_key="a"))
        session.commit()
        session.add(_report(scan.id, request_key="b"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()


def test_partial_unique_index_allows_inactive_statuses(manager):
    session = manager.get_session()
    try:
        scan = _scan(session)
        session.add_all([
            _report(scan.id, request_key="a", status="reported"),
            _report(scan.id, request_key="b", status="failed"),
            _report(scan.id, request_key="c", status="dry_run"),
            _report(scan.id, request_key="d", status="dry_run"),
        ])
        session.commit()
        assert session.query(PatrolReport).count() == 4
    finally:
        session.close()


def test_same_request_key_and_rpid_is_unique(manager):
    session = manager.get_session()
    try:
        scan = _scan(session)
        session.add(_report(scan.id, request_key="a", status="failed"))
        session.commit()
        session.add(_report(scan.id, request_key="a", status="skipped"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()


def test_delete_scan_cascades_comments_and_nulls_reports(manager):
    session = manager.get_session()
    try:
        scan = _scan(session)
        session.add(PatrolComment(scan_id=scan.id, rpid=1, mid=2, content="x"))
        session.add(_report(scan.id))
        session.commit()
        scan_id = scan.id
        session.query(PatrolScan).filter(PatrolScan.id == scan_id).delete(synchronize_session=False)
        session.commit()
        session.expire_all()
        assert session.query(PatrolComment).count() == 0
        report = session.query(PatrolReport).one()
        assert report.scan_id is None
    finally:
        session.close()


def test_preference_and_cache_roundtrip(manager):
    session = manager.get_session()
    try:
        session.add(PatrolPreference(id=1, whitelist_mids=[1, 2], user_keywords=[]))
        session.add(PatrolLlmCache(cache_key="h" * 64, ai_score=80, reason="r", model="m",
                                   created_at=datetime.utcnow()))
        session.commit()
        pref = session.get(PatrolPreference, 1)
        assert pref.revision == 0 and pref.proxy_enabled is True and pref.include_replies is True
        assert session.get(PatrolLlmCache, "h" * 64).ai_score == 80
    finally:
        session.close()
