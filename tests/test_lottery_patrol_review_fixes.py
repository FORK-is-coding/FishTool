"""审查修复的回归测试：代发默认关闭、断点续采、缓存含 focus、结果未知重试确认、CSV 换行转义。"""
import asyncio
from datetime import datetime, timedelta

import pytest

from core.database import DatabaseManager
from modules.lottery.patrol import reporter, service, store
from modules.lottery.patrol.contracts import PROXY_CONSENT_VERSION, build_settings
from modules.lottery.patrol.judge import cache_key
from web.routers.lottery_patrol import _csv_safe


def test_proxy_disabled_by_default_config():
    """安全默认：无 patrol 配置时代发不可用（审查 Major 1）。"""
    s = build_settings({})
    assert s.report_proxy_allowed is False
    ok, why = reporter.proxy_report_available(s, {"proxy_enabled": True,
                                                  "proxy_consent_version": PROXY_CONSENT_VERSION}, "127.0.0.1")
    assert ok is False and "proxy_enabled" in why


def test_cache_key_includes_focus():
    """不同判定侧重点不得命中同一缓存（审查 Major 3）。"""
    item = {"id": "c1", "name": "x", "text": "y"}
    identity = {"api_base": "a", "model": "m"}
    assert cache_key(item, identity, "看昵称") != cache_key(item, identity, "看括号动作")
    assert cache_key(item, identity, "看昵称") == cache_key(item, identity, "看昵称")


@pytest.mark.parametrize("value,expected", [
    ("=1+1", "'=1+1"),
    ("abc\n=1+1", "abc\n'=1+1"),
    ("abc\n@x", "abc\n'@x"),
    ("\n=1+1", "\n'=1+1"),
    ("\t+1", "'\t+1"),
    ("正常文本", "正常文本"),
])
def test_csv_newline_formula_escaped(value, expected):
    """多行单元格中换行后的行首也要防公式注入（审查 Major 7）。"""
    assert _csv_safe(value) == expected


@pytest.fixture()
def db(tmp_path, monkeypatch):
    manager = DatabaseManager(str(tmp_path / "review.db"))
    monkeypatch.setattr("modules.lottery.patrol.store.get_session", manager.get_session)
    return manager


def _scan_row(db):
    return store.create_scan("k", {"bvid": "BV1", "aid": 1, "title": "t"}, 7, {}, "v")


def test_unknown_result_requires_acknowledgement(db):
    """结果未知的评论默认跳过，只有单独确认才允许重试（审查 Major 4）。"""
    sid = _scan_row(db)
    store.upsert_comments(sid, [{"rpid": 1, "mid": 101, "uname": "bot", "content": "x",
                                 "tier": "bot", "score_source": "rule+llm", "final_score": 90,
                                 "report_status": "unknown"}])
    store.set_comment_report_status(sid, 1, "unknown")
    scan = store.get_scan(sid)
    comments = store.all_comments(sid)
    s = build_settings({"report": {"proxy_enabled": True}})
    keep, skipped = reporter.validate_selection(scan, comments, [1], mode="proxy", s=s,
                                                prefs={}, account_mid=7)
    assert keep == [] and skipped[0]["reason_code"] == "unknown_result"
    keep2, _ = reporter.validate_selection(scan, comments, [1], mode="proxy", s=s, prefs={},
                                           account_mid=7, acknowledge_unknown_rpids=[1])
    assert len(keep2) == 1


def test_resume_merges_prior_comments(db, monkeypatch):
    """断点续采恢复原批次并合并中断前评论（审查 Major 2）。"""
    sid = _scan_row(db)
    store.upsert_comments(sid, [
        {"rpid": 1, "mid": 101, "uname": "阿杰的大肥鱼", "content": "本鲸记下了", "level": 2,
         "is_reply": False, "rule_score": 50, "final_score": 50, "tier": "suspect", "score_source": "hard_rule"},
    ])
    store.finish_scan(sid, "interrupted")
    # 模拟续采：collector 只返回新页（rpid=2），service 应合并 rpid=1。
    from modules.lottery.patrol import collector as collector_mod

    class _Meta:
        bvid = "BV1"
        aid = 1
        title = "t"
        owner_mid = 9
        owner_name = "UP"

        def to_dict(self):
            return {"bvid": "BV1", "aid": 1, "title": "t", "owner_mid": 9, "owner_name": "UP"}

    class _Collector:
        def __init__(self, api, settings):
            pass

        async def fetch_video_meta(self, target):
            return _Meta()

        async def collect(self, meta, **kw):
            return collector_mod.CollectResult(
                comments=[{"rpid": 2, "mid": 102, "uname": "普通观众", "content": "不错", "level": 5,
                           "is_reply": False, "at_users": []}],
                partial=False, partial_reason="", pages=1, failed_pages=0, reply_requests=0, cursor=None)

    monkeypatch.setattr(service, "PatrolCollector", _Collector)
    monkeypatch.setattr(reporter, "read_account_cookie", lambda: ("c", "j", 7))
    svc = service.PatrolService(api_factory=lambda c: object())
    out = asyncio.run(svc.run_scan("task-1", {"target": "BV1", "max_comments": 500, "include_replies": False},
                                   progress=lambda *a: None, is_cancelled=lambda: False,
                                   on_scan_created=lambda sid: None,
                                   resume_cursor="cursor", resume_scan_id=sid))
    assert out["scan_id"] == sid
    rpids = {c["rpid"] for c in store.all_comments(sid)}
    assert rpids == {1, 2}
    assert store.get_scan(sid)["status"] == "completed"
