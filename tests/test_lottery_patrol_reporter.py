"""AI 伪人巡检 · 举报：参数、错误码分流、台账、校验、可用性、Cookie 不外泄。"""
import asyncio
import inspect
import logging
from datetime import datetime, timedelta

import pytest

from core.database import DatabaseManager
from core.exceptions import BilibiliAPIError, CookieExpiredError, NetworkError, RateLimitError
from core.request_budget import RequestQuotaExceeded
from modules.lottery.patrol import reporter, store
from modules.lottery.patrol.contracts import PROXY_CONSENT_VERSION, build_settings

S = build_settings({})
COOKIE = "SESSDATA=secret-sess; bili_jct=csrf-token-123; DedeUserID=7"
META = {"bvid": "BV1xx411c7mD", "aid": 123, "title": "T", "owner_mid": 9, "owner_name": "UP"}


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    manager = DatabaseManager(str(tmp_path / "reporter.db"))
    monkeypatch.setattr("modules.lottery.patrol.store.get_session", manager.get_session)
    monkeypatch.setattr(reporter, "read_account_cookie", lambda: (COOKIE, "csrf-token-123", 7))
    monkeypatch.setattr(reporter, "emit_toast", lambda *a, **k: 0)
    reporter._REASON_CACHE.clear()
    return manager


def _scan():
    sid = store.create_scan("k", META, 7, {}, "v")
    store.finish_scan(sid, "completed")
    return store.get_scan(sid)


def _c(rpid, mid=None, *, tier="bot", source="rule+llm", score=90, disagreement=False):
    return {"rpid": rpid, "mid": mid or rpid + 100, "uname": f"u{rpid}", "content": "x", "final_score": score,
            "tier": tier, "score_source": source, "disagreement": disagreement, "report_status": "none"}


class _Api:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def post(self, url, data=None, json=None, **kw):
        self.calls.append({"url": url, "data": dict(data or {}), "kw": kw})
        step = self.outcomes.pop(0) if self.outcomes else {}
        if isinstance(step, Exception):
            raise step
        return step

    async def get(self, url, params=None, **kw):
        step = self.outcomes.pop(0) if self.outcomes else {}
        if isinstance(step, Exception):
            raise step
        return step


ACCOUNT = reporter.Account(cookie=COOKIE, bili_jct="csrf-token-123", mid=7, uname="me", level=5)


def _run(api, scan, items, **kw):
    kw.setdefault("reason", 0)
    kw.setdefault("content", "疑似AI自动生成评论的机器人账号，请核实")
    kw.setdefault("request_key", "rk-1")
    kw.setdefault("dry_run", False)
    return asyncio.run(reporter.run_proxy_batch(api, ACCOUNT, scan, items, s=S, sleep=lambda _x: asyncio.sleep(0), **kw))


# ---------- 参数 ----------

def test_post_params_and_no_retry():
    api = _Api({})
    scan = _scan()
    _run(api, scan, [_c(1)])
    call = api.calls[0]
    assert call["url"] == reporter.REPORT_URL
    assert call["data"] == {"type": 1, "oid": 123, "rpid": 1, "reason": 0, "csrf": "csrf-token-123",
                            "content": "疑似AI自动生成评论的机器人账号，请核实"}
    assert call["kw"]["retry_times"] == 1


def test_non_zero_reason_has_no_content():
    api = _Api({})
    _run(api, _scan(), [_c(1)], reason=3)
    assert "content" not in api.calls[0]["data"] and api.calls[0]["data"]["reason"] == 3


def test_source_never_sends_blacklist_or_delete():
    src = inspect.getsource(reporter)
    assert "add_blacklist" not in src
    assert '"delete"' not in src


# ---------- 错误码分流 ----------

@pytest.mark.parametrize("code,status", [(12008, "duplicate"), (12006, "deleted"), (12022, "deleted"), (-400, "failed")])
def test_continue_codes(code, status):
    api = _Api(BilibiliAPIError("API错误", bili_code=code), {})
    out = _run(api, _scan(), [_c(1), _c(2)])
    assert out["counts"].get(status) == 1 and out["counts"].get("reported") == 1 and not out["stopped"]


@pytest.mark.parametrize("exc,cooldown", [
    (BilibiliAPIError("API错误 [12019]", bili_code=12019), True),
    (BilibiliAPIError("风控", bili_code=-352), True),
    (RateLimitError(60), True),
    (BilibiliAPIError("API错误 [12002]", bili_code=12002), False),
    (BilibiliAPIError("API错误 [-111]", bili_code=-111), False),
    (CookieExpiredError("x"), False),
    (RequestQuotaExceeded("domain:cooling"), False),
])
def test_stop_codes(exc, cooldown):
    api = _Api(exc, {})
    scan = _scan()
    out = _run(api, scan, [_c(1), _c(2), _c(3)])
    assert out["stopped"] and len(api.calls) == 1
    assert out["counts"].get("failed") == 1
    rest_status = "skipped_rate_limited" if cooldown else "cancelled"
    assert out["counts"].get(rest_status) == 2
    assert (store.get_cooldown() is not None) is cooldown


def test_cooldown_survives_reload():
    _run(_Api(BilibiliAPIError("x", bili_code=12019)), _scan(), [_c(1)])
    assert store.load_preferences()["report_cooldown_until"] > datetime.now()


def test_network_timeout_is_unknown_not_retried():
    api = _Api(NetworkError("timeout"))
    out = _run(api, _scan(), [_c(1)])
    assert out["counts"] == {"unknown": 1} and len(api.calls) == 1
    assert store.has_active_report(7, 123, 1) is False


def test_high_priority_codes_report(monkeypatch):
    seen = []
    monkeypatch.setattr(reporter, "report_high_priority", lambda exc, **kw: seen.append(kw.get("toast_message")))
    _run(_Api(BilibiliAPIError("x", bili_code=12009)), _scan(), [_c(1)])
    assert seen and "参数" in seen[0]


# ---------- 台账 ----------

def test_ledger_and_comment_status():
    scan = _scan()
    store.upsert_comments(scan["scan_id"], [{**_c(1), "content": "x", "rule_score": 80}])
    _run(_Api({}), scan, [_c(1)])
    assert store.has_active_report(7, 123, 1)
    assert store.all_comments(scan["scan_id"])[0]["report_status"] == "reported"
    reports = store.list_reports()
    assert reports[0]["status"] == "reported" and reports[0]["mode"] == "proxy"


def test_concurrent_duplicate_skipped():
    scan = _scan()
    store.insert_report_sending({"request_key": "other", "mode": "proxy", "reporter_mid": 7, "scan_id": scan["scan_id"],
                                 "oid": 123, "rpid": 1})
    api = _Api({})
    out = _run(api, scan, [_c(1)])
    assert out["counts"] == {"skipped": 1} and api.calls == []


def test_dry_run_sends_nothing_and_does_not_block():
    api = _Api()
    out = _run(api, _scan(), [_c(1), _c(2)], dry_run=True)
    assert out["counts"] == {"dry_run": 2} and api.calls == []
    assert store.has_active_report(7, 123, 1) is False
    assert store.count_report_attempts(7, datetime.now() - timedelta(hours=1)) == 0


def test_account_change_stops(monkeypatch):
    seq = iter([(COOKIE, "csrf-token-123", 7), (COOKIE, "x", 8)])
    monkeypatch.setattr(reporter, "read_account_cookie", lambda: next(seq))
    api = _Api({}, {})
    out = _run(api, _scan(), [_c(1), _c(2)])
    assert len(api.calls) == 1 and "账号已变化" in out["stopped"] and out["counts"].get("cancelled") == 1


def test_logout_mid_batch_stops(monkeypatch):
    seq = iter([(COOKIE, "csrf-token-123", 7), None])
    monkeypatch.setattr(reporter, "read_account_cookie", lambda: next(seq))
    api = _Api({}, {})
    out = _run(api, _scan(), [_c(1), _c(2)])
    assert len(api.calls) == 1 and out["stopped"]


def test_cancel_marks_rest():
    flags = iter([False, True])
    api = _Api({}, {})
    out = _run(api, _scan(), [_c(1), _c(2)], is_cancelled=lambda: next(flags))
    assert len(api.calls) == 1 and out["counts"].get("cancelled") == 1


# ---------- 校验 ----------

def _validate(items, rpids=None, **kw):
    scan = kw.pop("scan", None) or _scan()
    prefs = kw.pop("prefs", {"whitelist_mids": []})
    return reporter.validate_selection(scan, items, rpids or [c["rpid"] for c in items], mode=kw.pop("mode", "proxy"),
                                       s=kw.pop("s", S), prefs=prefs, account_mid=7, **kw)


def test_validate_requires_bot_and_ai():
    items = [_c(1), _c(2, tier="suspect"), _c(3, source="rule"), _c(4, disagreement=True)]
    keep, skipped = _validate(items)
    assert [c["rpid"] for c in keep] == [1]
    codes = {s["rpid"]: s["reason_code"] for s in skipped}
    assert codes == {2: "not_bot", 3: "need_ai", 4: "need_confirm"}


def test_validate_confirmed_overrides():
    items = [_c(3, source="rule", tier="suspect"), _c(4, disagreement=True)]
    keep, _ = _validate(items, confirmed_rpids=[3, 4])
    assert {c["rpid"] for c in keep} == {3, 4}


def test_validate_same_account_keeps_highest():
    items = [_c(1, mid=50, score=80), _c(2, mid=50, score=95), _c(3, mid=50, score=90)]
    keep, skipped = _validate(items)
    assert [c["rpid"] for c in keep] == [2] and len([s for s in skipped if s["reason_code"] == "same_account"]) == 2


def test_validate_whitelist_owner_self():
    items = [_c(1, mid=9), _c(2, mid=7), _c(3, mid=55)]
    keep, skipped = _validate(items, prefs={"whitelist_mids": [55]})
    assert keep == [] and all(s["reason_code"] == "whitelist" for s in skipped)


def test_validate_not_in_scan_and_dedupe():
    keep, skipped = _validate([_c(1)], rpids=[1, 1, 999])
    assert [c["rpid"] for c in keep] == [1] and skipped[0]["reason_code"] == "not_in_scan"


def test_validate_per_task_limit():
    items = [_c(i) for i in range(1, 9)]
    keep, skipped = _validate(items)
    assert len(keep) == 5 and len([s for s in skipped if s["reason_code"] == "per_task_limit"]) == 3


def test_validate_daily_limit():
    for i in range(8):
        rid = store.insert_report_sending({"request_key": f"old{i}", "mode": "proxy", "reporter_mid": 7, "scan_id": None,
                                           "oid": 999, "rpid": 1000 + i})
        store.finalize_report(rid, "reported", 0, "")
    keep, skipped = _validate([_c(i) for i in range(1, 5)])
    assert len(keep) == 2 and len([s for s in skipped if s["reason_code"] == "daily_limit"]) == 2


def test_validate_stale_scan_blocks_proxy():
    scan = _scan()
    scan["completed_at_dt"] = datetime.now() - timedelta(hours=80)
    keep, skipped = _validate([_c(1)], scan=scan)
    assert keep == [] and skipped[0]["reason_code"] == "stale"


def test_validate_native_ignores_thresholds():
    items = [_c(1, tier="suspect", source="rule"), _c(2, tier="human", score=10)]
    keep, _ = _validate(items, mode="native")
    assert {c["rpid"] for c in keep} == {1, 2}


def test_validate_already_reported():
    scan = _scan()
    rid = store.insert_report_sending({"request_key": "x", "mode": "proxy", "reporter_mid": 7, "scan_id": scan["scan_id"],
                                       "oid": 123, "rpid": 1})
    store.finalize_report(rid, "reported", 0, "")
    keep, skipped = _validate([_c(1)], scan=scan)
    assert keep == [] and skipped[0]["reason_code"] == "already"


# ---------- 可用性 ----------

PREFS_OK = {"proxy_consent_version": PROXY_CONSENT_VERSION, "proxy_enabled": True}


@pytest.fixture()
def clean_env(monkeypatch):
    for key in ("FISHTOOL_EMBEDDED_BY", "FISHTOOL_ASTRBOT_ALLOW_PROXY_REPORT", "FISHTOOL_LISTEN_HOST"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(reporter, "_ip_circuit_open", lambda: False)
    monkeypatch.setattr(reporter, "_domain_cooling", lambda: False)
    return monkeypatch


def test_available_when_all_ok(clean_env):
    assert reporter.proxy_report_available(S, PREFS_OK, "127.0.0.1") == (True, "")


def test_needs_consent(clean_env):
    ok, why = reporter.proxy_report_available(S, {"proxy_enabled": True}, "127.0.0.1")
    assert not ok and "同意" in why


def test_unavailable_reasons(clean_env):
    assert not reporter.proxy_report_available(S, {**PREFS_OK, "proxy_enabled": False}, "127.0.0.1")[0]
    assert not reporter.proxy_report_available(S, PREFS_OK, "192.168.1.5")[0]
    assert not reporter.proxy_report_available(build_settings({"report": {"proxy_enabled": False}}), PREFS_OK,
                                               "127.0.0.1")[0]
    clean_env.setenv("FISHTOOL_LISTEN_HOST", "0.0.0.0")
    assert "本机" in reporter.proxy_report_available(S, PREFS_OK, "127.0.0.1")[1]


def test_astrbot_switch(clean_env):
    clean_env.setenv("FISHTOOL_EMBEDDED_BY", "astrbot")
    ok, why = reporter.proxy_report_available(S, PREFS_OK, "127.0.0.1")
    assert not ok and "AstrBot" in why
    clean_env.setenv("FISHTOOL_ASTRBOT_ALLOW_PROXY_REPORT", "1")
    assert reporter.proxy_report_available(S, PREFS_OK, "127.0.0.1") == (True, "")


def test_cooldown_and_circuit_block(clean_env):
    store.set_cooldown(datetime.now() + timedelta(minutes=10))
    assert "冷却" in reporter.proxy_report_available(S, PREFS_OK, "127.0.0.1")[1]
    store.set_cooldown(datetime.now() - timedelta(minutes=1))
    clean_env.setattr(reporter, "_ip_circuit_open", lambda: True)
    assert "熔断" in reporter.proxy_report_available(S, PREFS_OK, "127.0.0.1")[1]


# ---------- 理由表 / 原生链接 ----------

def test_fetch_reasons_fallback_and_remote():
    fallback = asyncio.run(reporter.fetch_reasons(_Api(BilibiliAPIError("x", bili_code=-101)), 123))
    assert fallback["source"] == "fallback" and 0 in reporter.reason_ids(fallback)
    remote = asyncio.run(reporter.fetch_reasons(_Api({"report_groups": [
        {"group_name": "社区", "reason_list": [{"tag_id": 3, "tag_name": "刷屏"}, {"tag_id": 0, "tag_name": "其他",
                                                                                 "content_required": True}]}]}), 456))
    assert remote["source"] == "remote" and reporter.reason_ids(remote) == {3: False, 0: True}


def test_native_links_and_ledger():
    scan = _scan()
    links = reporter.native_links(scan, [_c(5)], request_key="nk", reporter_mid=7)
    assert links[0]["url"] == "https://www.bilibili.com/video/BV1xx411c7mD#reply5"
    assert store.list_reports()[0]["status"] == "native_opened"


# ---------- Cookie 不外泄 ----------

def test_account_repr_hides_cookie():
    assert "secret" not in repr(ACCOUNT) and "csrf" not in repr(ACCOUNT)


def test_logs_do_not_contain_cookie(caplog):
    caplog.set_level(logging.DEBUG)
    _run(_Api(NetworkError("timeout"), BilibiliAPIError("x", bili_code=12019)), _scan(), [_c(1), _c(2)])
    text = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    assert "secret-sess" not in text and "csrf-token-123" not in text
    for row in store.list_reports():
        assert "csrf" not in str(row)
