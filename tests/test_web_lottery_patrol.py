"""AI 伪人巡检路由：守卫、校验、任务生命周期、举报入口、CSV、总开关。"""
import asyncio
from typing import Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import DatabaseManager
from modules.lottery.patrol import judge as judge_module
from modules.lottery.patrol import reporter, store
from modules.lottery.patrol.contracts import PROXY_CONSENT_VERSION, build_settings
from web.local_guard import get_local_guard
from web.routers import lottery as lottery_router
from web.routers import lottery_patrol as router_module
from web.routers.lottery_patrol_tasks import PatrolTaskRegistry

META = {"bvid": "BV1xx411c7mD", "aid": 123, "title": "T", "owner_mid": 9, "owner_name": "UP"}


class _FakeService:
    """不触网的服务替身；run_* 由各测试按需替换。"""

    def __init__(self):
        self.native_calls = []

    async def preflight(self, target, client_host=""):
        return {"login": {"ok": True}, "video": None}

    async def run_scan(self, task_id, req, *, progress, is_cancelled, on_scan_created, resume_cursor=None,
                       resume_scan_id=None):
        sid = store.create_scan(task_id, META, 7, req, "v")
        on_scan_created(sid)
        store.upsert_comments(sid, [{"rpid": 1, "mid": 101, "uname": "=HYPERLINK(1)", "content": "@阿杰 看这个",
                                     "is_reply": False, "rule_score": 80, "final_score": 80, "tier": "bot",
                                     "score_source": "rule", "level": 2}])
        store.finish_scan(sid, "completed")
        return {"scan_id": sid}

    async def run_judge(self, task_id, scan_id, req, *, progress, is_cancelled):
        return {"judged": 0}

    async def report_native(self, scan_id, req):
        self.native_calls.append(req)
        return {"links": [{"rpid": r, "url": f"https://www.bilibili.com/video/x#reply{r}"} for r in req["rpids"]],
                "skipped": []}

    async def run_report(self, task_id, scan_id, req, *, client_host, progress, is_cancelled):
        return {"counts": {"reported": 1}, "client_host": client_host}

    def startup(self):
        return {"ok": True}

    def _api_factory(self, cookie):
        class _A:
            async def get(self, *a, **k):
                return {}

            async def close(self):
                return None
        return _A()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    manager = DatabaseManager(str(tmp_path / "web_patrol.db"))
    monkeypatch.setattr("modules.lottery.patrol.store.get_session", manager.get_session)
    monkeypatch.setattr(router_module, "registry", PatrolTaskRegistry())
    monkeypatch.setattr(judge_module, "model_identity", lambda: {"api_base": "https://x.test", "model": "m1"})
    monkeypatch.setattr(reporter, "read_account_cookie", lambda: ("c", "j", 7))
    monkeypatch.setattr(reporter, "_ip_circuit_open", lambda: False)
    monkeypatch.setattr(reporter, "_domain_cooling", lambda: False)
    for key in ("FISHTOOL_EMBEDDED_BY", "FISHTOOL_ASTRBOT_ALLOW_PROXY_REPORT", "FISHTOOL_LISTEN_HOST"):
        monkeypatch.delenv(key, raising=False)
    svc = _FakeService()
    router_module.set_service(svc)
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api")
    app.include_router(lottery_router.router, prefix="/api")
    client = TestClient(app, base_url="http://127.0.0.1")
    yield client, svc, manager, monkeypatch
    router_module.set_service(None)


def _wh() -> Dict[str, str]:
    return {"X-Local-Token": get_local_guard().issue_token()}


def _wait(client, task_id, rounds=50):
    for _ in range(rounds):
        data = client.get(f"/api/lottery/patrol/tasks/{task_id}").json()["data"]
        if data["status"] != "running":
            return data
    return data


def _scan(client):
    r = client.post("/api/lottery/patrol/scans", json={"target": "BV1xx411c7mD", "include_replies": False},
                    headers=_wh())
    assert r.status_code == 202, r.text
    task = _wait(client, r.json()["task_id"])
    assert task["status"] == "completed"
    return task["result"]["scan_id"]


# ---------- 守卫与校验 ----------

def test_write_requires_token(env):
    client, *_ = env
    r = client.post("/api/lottery/patrol/scans", json={"target": "BV1xx411c7mD"})
    assert r.status_code == 403


def test_cross_site_read_blocked(env):
    client, *_ = env
    r = client.get("/api/lottery/patrol/reports", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize("body", [
    {"target": "BV1xx411c7mD", "extra": 1},
    {"target": "BV1xx411c7mD", "max_comments": 2001},
    {"target": "BV1xx411c7mD", "include_replies": "yes"},
    {"target": "x"},
])
def test_scan_validation(env, body):
    client, *_ = env
    assert client.post("/api/lottery/patrol/scans", json=body, headers=_wh()).status_code == 422


def test_report_rpids_limit(env):
    client, *_ = env
    sid = _scan(client)
    r = client.post(f"/api/lottery/patrol/scans/{sid}/report",
                    json={"rpids": list(range(1, 52)), "mode": "native", "request_key": "rk-12345"}, headers=_wh())
    assert r.status_code == 422


# ---------- 任务生命周期 ----------

def test_scan_then_list_and_get(env):
    client, *_ = env
    sid = _scan(client)
    data = client.get(f"/api/lottery/patrol/scans/{sid}/comments").json()["data"]
    assert data["total"] == 1 and data["items"][0]["rpid"] == 1
    assert "completed_at_dt" not in client.get(f"/api/lottery/patrol/scans/{sid}").json()["data"]


def test_registry_reuse_conflict_and_cancel(env):
    """TestClient 每个请求用独立事件循环，长任务无法跨请求存活，这里直接测注册表。"""
    reg = PatrolTaskRegistry()

    async def scenario():
        gate = asyncio.Event()

        async def slow():
            await gate.wait()

        tid = reg.create("scan", {"target": "x"}, fingerprint="fp1")
        reg.spawn(tid, slow())
        assert reg.find_by_fingerprint("fp1") == tid
        assert reg.running_of("scan") == tid
        assert reg.request_cancel(tid) == "cancelled"
        await asyncio.sleep(0)
        assert reg.public(tid)["status"] == "cancelled"
        rid = reg.create("report", {"rpids": [1]})
        assert reg.request_cancel(rid) == "cancelling" and reg.is_cancelled(rid)
        jid = reg.create("judge", {})
        assert reg.cancel_all("已退出登录") == 2  # report 仍是 running（等当前条完成），judge 被取消
        assert reg.get(jid)["status"] == "cancelled"

    asyncio.run(scenario())


def test_scan_conflict_via_http(env):
    client, *_ = env
    router_module.registry.create("scan", {"target": "other"}, fingerprint="other")
    r = client.post("/api/lottery/patrol/scans", json={"target": "BV1xx411c7mD"}, headers=_wh())
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "scan_running"


def test_task_not_found(env):
    client, *_ = env
    assert client.get("/api/lottery/patrol/tasks/nope").status_code == 404


def test_lottery_routes_cannot_see_patrol_tasks(env):
    client, *_ = env
    r = client.post("/api/lottery/patrol/scans", json={"target": "BV1xx411c7mD"}, headers=_wh())
    tid = r.json()["task_id"]
    _wait(client, tid)
    assert client.get(f"/api/lottery/tasks/{tid}").status_code == 404


def test_resume_rules(env):
    client, *_ = env
    store.task_create("rep1", "report", {"rpids": [1]}, scan_id=1)
    store.task_create("scan1", "scan", {"target": "BV1xx411c7mD", "max_comments": 100, "include_replies": False,
                                        "mode": "time"})
    store.recover_on_startup(7)
    assert client.post("/api/lottery/patrol/tasks/rep1/resume", headers=_wh()).json()["detail"]["error_code"] == \
        "report_not_resumable"
    r = client.post("/api/lottery/patrol/tasks/scan1/resume", headers=_wh())
    assert r.status_code == 202 and r.json()["resumed"] is True


# ---------- AI 甄别入口 ----------

def test_judge_requires_llm_and_consent(env):
    client, _svc, _m, mp = env
    sid = _scan(client)
    body = {"scope": "gray", "request_key": "jk-123456"}
    mp.setattr("llm.client.is_llm_available", lambda: False)
    assert client.post(f"/api/lottery/patrol/scans/{sid}/judge", json=body, headers=_wh()).json()["detail"][
        "error_code"] == "llm_not_configured"
    mp.setattr("llm.client.is_llm_available", lambda: True)
    assert client.post(f"/api/lottery/patrol/scans/{sid}/judge", json=body, headers=_wh()).json()["detail"][
        "error_code"] == "consent_required"
    client.put("/api/lottery/patrol/preferences", json={"expected_revision": store.load_preferences()["revision"],
                                                        "ai_consent": "agree"}, headers=_wh())
    assert client.post(f"/api/lottery/patrol/scans/{sid}/judge", json=body, headers=_wh()).status_code == 202


# ---------- 举报入口 ----------

def test_native_report(env):
    client, svc, *_ = env
    sid = _scan(client)
    r = client.post(f"/api/lottery/patrol/scans/{sid}/report",
                    json={"rpids": [1], "mode": "native", "request_key": "rk-123456"}, headers=_wh())
    assert r.status_code == 200 and r.json()["data"]["links"][0]["url"].endswith("#reply1")


def _enable_proxy(client):
    rev = store.load_preferences()["revision"]
    r = client.put("/api/lottery/patrol/preferences", json={"expected_revision": rev, "proxy_consent": "agree",
                                                            "proxy_enabled": True}, headers=_wh())
    assert r.status_code == 200, r.text


def test_proxy_requires_consent_before_enable(env):
    client, *_ = env
    r = client.put("/api/lottery/patrol/preferences", json={"expected_revision": 0, "proxy_enabled": True},
                   headers=_wh())
    assert r.json()["detail"]["error_code"] == "proxy_consent_required"


def test_proxy_blocked_for_non_loopback_client(env):
    client, _svc, _m, mp = env
    sid = _scan(client)
    _enable_proxy(client)
    mp.setattr(router_module, "load_settings", lambda: build_settings({"report": {"proxy_enabled": True}}))
    r = client.post(f"/api/lottery/patrol/scans/{sid}/report",
                    json={"rpids": [1], "mode": "proxy", "reason": 3, "request_key": "rk-123456"}, headers=_wh())
    # TestClient 的 client.host 是 "testclient"，不是回环地址
    assert r.status_code == 403 and r.json()["detail"]["error_code"] == "proxy_unavailable"


def test_proxy_allowed_from_loopback_and_astrbot_switch(env):
    client, _svc, _m, mp = env
    sid = _scan(client)
    _enable_proxy(client)
    mp.setattr(router_module, "load_settings", lambda: build_settings({"report": {"proxy_enabled": True}}))
    mp.setattr(router_module, "_client_host", lambda request: "127.0.0.1")
    body = {"rpids": [1], "mode": "proxy", "reason": 3, "request_key": "rk-123456"}
    r = client.post(f"/api/lottery/patrol/scans/{sid}/report", json=body, headers=_wh())
    assert r.status_code == 202
    assert _wait(client, r.json()["task_id"])["status"] == "completed"
    mp.setenv("FISHTOOL_EMBEDDED_BY", "astrbot")
    r = client.post(f"/api/lottery/patrol/scans/{sid}/report", json={**body, "request_key": "rk-234567"},
                    headers=_wh())
    assert r.status_code == 403 and "AstrBot" in r.json()["detail"]["message"]
    mp.setenv("FISHTOOL_ASTRBOT_ALLOW_PROXY_REPORT", "1")
    r = client.post(f"/api/lottery/patrol/scans/{sid}/report", json={**body, "request_key": "rk-345678"},
                    headers=_wh())
    assert r.status_code == 202


def test_proxy_account_changed(env):
    client, _svc, _m, mp = env
    sid = _scan(client)
    _enable_proxy(client)
    mp.setattr(router_module, "load_settings", lambda: build_settings({"report": {"proxy_enabled": True}}))
    mp.setattr(router_module, "_client_host", lambda request: "127.0.0.1")
    mp.setattr(reporter, "read_account_cookie", lambda: ("c", "j", 8))
    r = client.post(f"/api/lottery/patrol/scans/{sid}/report",
                    json={"rpids": [1], "mode": "proxy", "reason": 3, "request_key": "rk-123456"}, headers=_wh())
    assert r.json()["detail"]["error_code"] == "account_changed"


# ---------- 偏好 ----------

def test_preferences_cas_and_server_side_identity(env):
    client, *_ = env
    r = client.put("/api/lottery/patrol/preferences", json={"expected_revision": 0, "ai_consent": "agree",
                                                            "include_replies": False, "whitelist_mids": [5, 5, 3]},
                   headers=_wh())
    data = r.json()["data"]
    assert data["ai_model_identity"] == {"api_base": "https://x.test", "model": "m1"}
    assert data["whitelist_mids"] == [3, 5] and data["include_replies"] is False
    stale = client.put("/api/lottery/patrol/preferences", json={"expected_revision": 0, "include_replies": True},
                       headers=_wh())
    assert stale.json()["detail"]["error_code"] == "revision_conflict"
    got = client.get("/api/lottery/patrol/preferences").json()["data"]
    assert got["ai_consent_ok"] is True and "report_cooldown_until" not in got


def test_user_keyword_validation(env):
    client, *_ = env
    r = client.put("/api/lottery/patrol/preferences", json={"expected_revision": 0, "user_keywords": [
        {"word": "x", "field": "text", "weight": 40}]}, headers=_wh())
    assert r.status_code == 422


# ---------- CSV / 清除 / 总开关 ----------

def test_csv_escapes_formulas_and_hides_ids(env):
    client, *_ = env
    sid = _scan(client)
    r = client.get(f"/api/lottery/patrol/scans/{sid}/export")
    text = r.content.decode("utf-8-sig")
    assert "'=HYPERLINK(1)" in text and "'@阿杰 看这个" in text
    assert "mid" not in text.splitlines()[0]
    with_ids = client.get(f"/api/lottery/patrol/scans/{sid}/export?include_ids=true").content.decode("utf-8-sig")
    assert "mid" in with_ids.splitlines()[0]


def test_clear_data(env):
    client, *_ = env
    sid = _scan(client)
    r = client.delete("/api/lottery/patrol/data", headers=_wh())
    assert r.status_code == 200 and r.json()["data"]["scans"] == 1
    assert client.get(f"/api/lottery/patrol/scans/{sid}").status_code == 404


def test_disabled_returns_404(env):
    client, _svc, _m, mp = env
    mp.setattr(router_module, "load_settings", lambda: build_settings({"enabled": False}))
    assert client.get("/api/lottery/patrol/reports").status_code == 404
    assert client.post("/api/lottery/patrol/scans", json={"target": "BV1xx411c7mD"}, headers=_wh()).status_code == 404


def test_main_app_mounts_patrol_routes():
    from web.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/lottery/patrol/scans" in paths and "/api/lottery/patrol/scans/{scan_id}/report" in paths


def test_logout_cancels_patrol_tasks(env, monkeypatch):
    from web.routers import auth as auth_router
    called = []
    monkeypatch.setattr(router_module, "cancel_all_tasks", lambda reason: called.append(reason) or 0)

    class _Cfg:
        def get_secret(self, key, default=None):
            return ""

        def save_secret(self, key, value):
            return None

    monkeypatch.setattr(auth_router, "ConfigManager", lambda: _Cfg())
    asyncio.run(auth_router.logout())
    assert called == ["已退出登录"]
