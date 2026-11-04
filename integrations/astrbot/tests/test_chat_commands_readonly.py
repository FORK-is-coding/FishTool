"""聊天指令只读部分：配置收敛、引擎访问、登录态缓存、格式化。

只依赖 fishtool_adapter.{constants,errors,commands}，不需要 httpx / astrbot。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from fishtool_adapter.commands import FishToolCommands, clamp, shorten
from fishtool_adapter.constants import DEFAULT_CHAT_CFG, chat_cfg, engine_cfg
from fishtool_adapter.errors import CommandError, EngineUnavailable

log = logging.getLogger("test")


class FakeSup:
    def __init__(self, state="running", token="t1"):
        self.state = state
        self.token = token
        self.last_error = ""


class FakeClient:
    """按路径返回预设响应；未预设的路径返回 404。"""

    def __init__(self, routes=None, exc=None):
        self.routes = routes or {}
        self.exc = exc
        self.calls = []

    async def call(self, method, path, *, query=None, json=None, timeout=120):
        self.calls.append({"method": method, "path": path, "query": query, "json": json, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.routes.get(path, (404, {"detail": "not found"}))


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def run(coro):
    return asyncio.run(coro)


def make(state="running", routes=None, exc=None, ecfg=None, clock=None):
    sup = FakeSup(state)
    client = FakeClient(routes, exc)
    cmds = FishToolCommands(sup, client, engine_cfg(ecfg or {}), log, clock=clock or Clock())
    return cmds, sup, client


LOGGED_IN = (200, {"success": True, "logged_in": True, "user": {"uid": 123, "username": "鱼"}})


# ---------------------------------------------------------------- chat_cfg


def test_chat_cfg_defaults():
    assert chat_cfg(None) == DEFAULT_CHAT_CFG
    assert chat_cfg({}) == DEFAULT_CHAT_CFG
    assert chat_cfg("not a dict") == DEFAULT_CHAT_CFG


def test_chat_cfg_invalid_values_fall_back():
    cfg = chat_cfg({
        "enable": "yes",
        "restricted_scope": "root",
        "cooldown_seconds": -5,
        "notify_min_level": "critical",
        "notify_merge_seconds": 0,
        "report_charts": 1,
        "report_font_cjk": 42,
    })
    assert cfg["enable"] is True
    assert cfg["restricted_scope"] == "admin"
    assert cfg["cooldown_seconds"] == 10
    assert cfg["notify_min_level"] == "high"
    assert cfg["notify_merge_seconds"] == 120
    assert cfg["report_charts"] is True
    assert cfg["report_font_cjk"] == ""


def test_chat_cfg_keeps_valid_values():
    cfg = chat_cfg({
        "enable": False,
        "restricted_scope": "subscribed",
        "cooldown_seconds": "0",
        "notify_min_level": "low",
        "report_font_cjk": "  /fonts/a.ttf  ",
    })
    assert cfg["enable"] is False
    assert cfg["restricted_scope"] == "subscribed"
    assert cfg["cooldown_seconds"] == 0
    assert cfg["notify_min_level"] == "low"
    assert cfg["report_font_cjk"] == "/fonts/a.ttf"


@pytest.mark.parametrize("raw,expect", [
    (30.0, 30),
    ("10.5", 10),
    (10.5, 10),
    (99999, 3600),
    (None, 10),
])
def test_chat_cfg_int_bounds(raw, expect):
    assert chat_cfg({"cooldown_seconds": raw})["cooldown_seconds"] == expect


def test_chat_cfg_merge_seconds_bounds():
    assert chat_cfg({"notify_merge_seconds": 0})["notify_merge_seconds"] == 120
    assert chat_cfg({"notify_merge_seconds": 10**9})["notify_merge_seconds"] == 3600


def test_chat_cfg_returns_new_dict():
    cfg = chat_cfg(None)
    cfg["enable"] = False
    assert DEFAULT_CHAT_CFG["enable"] is True
    assert "notify_sessions" not in cfg  # 订阅不在配置里，见 subscriptions.py


# ---------------------------------------------------------------- engine access


def test_call_uses_engine_timeouts():
    cmds, _, client = make(routes={"/x": (200, {})}, ecfg={"request_timeout_s": 7, "long_request_timeout_s": 99})
    run(cmds._call("GET", "/x"))
    run(cmds._call("POST", "/x", long=True))
    assert client.calls[0]["timeout"] == 7
    assert client.calls[1]["timeout"] == 99


@pytest.mark.parametrize("state,hint", [
    ("crashed", "正在自动重启"),
    ("preparing", "稍后再试"),
    ("starting", "稍后再试"),
    ("stopped", "面板"),
    ("error", "面板"),
])
def test_engine_not_running(state, hint):
    cmds, _, client = make(state=state)
    with pytest.raises(CommandError) as err:
        run(cmds._call("GET", "/x"))
    assert "未运行" in str(err.value) and hint in str(err.value)
    assert not client.calls


def test_http_error_becomes_command_error():
    cmds, _, _ = make(routes={"/x": (500, {"detail": "boom"})})
    with pytest.raises(CommandError, match="500.*boom"):
        run(cmds._call("GET", "/x"))


def test_engine_unavailable_becomes_command_error():
    cmds, _, _ = make(exc=EngineUnavailable("engine unreachable"))
    with pytest.raises(CommandError, match="引擎不可用"):
        run(cmds._call("GET", "/x"))


def test_engine_unavailable_still_importable_from_client():
    pytest.importorskip("httpx")
    from fishtool_adapter import client

    assert client.EngineUnavailable is EngineUnavailable


# ---------------------------------------------------------------- login cache


def test_login_status_cached_for_60s():
    clock = Clock()
    cmds, _, client = make(routes={"/api/auth/status": LOGGED_IN}, clock=clock)
    run(cmds.login_status())
    clock.now += 59
    run(cmds.login_status())
    assert len(client.calls) == 1
    clock.now += 2
    run(cmds.login_status())
    assert len(client.calls) == 2


def test_login_cache_invalidated_on_engine_restart():
    cmds, sup, client = make(routes={"/api/auth/status": LOGGED_IN})
    run(cmds.login_status())
    sup.token = "t2"
    run(cmds.login_status())
    assert len(client.calls) == 2


def test_login_failure_not_cached():
    cmds, _, client = make(routes={"/api/auth/status": (500, {"detail": "x"})})
    with pytest.raises(CommandError):
        run(cmds.login_status())
    client.routes["/api/auth/status"] = LOGGED_IN
    assert run(cmds.login_status())["logged_in"] is True


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("bvid", ["BV1xx411c7mD", " BV1GJ411x7h7 "])
def test_check_bvid_ok(bvid):
    assert FishToolCommands.check_bvid(bvid) == bvid.strip()


@pytest.mark.parametrize("bvid", ["", "av123", "BV123", "BV1xx411c7mD1", "BV1xx411c7m!"])
def test_check_bvid_bad(bvid):
    with pytest.raises(CommandError):
        FishToolCommands.check_bvid(bvid)


def test_check_uid():
    assert FishToolCommands.check_uid("123") == "123"
    for bad in ("", "12a", "-1", "1" * 21):
        with pytest.raises(CommandError):
            FishToolCommands.check_uid(bad)


def test_clamp_and_shorten():
    assert clamp("3", 1, 20, 5) == 3
    assert clamp(99, 1, 20, 5) == 20
    assert clamp(0, 1, 20, 5) == 1
    assert clamp("x", 1, 20, 5) == 5
    assert clamp(True, 1, 20, 5) == 5
    assert shorten("a\n b  c") == "a b c"
    assert len(shorten("x" * 500)) == 200


# ---------------------------------------------------------------- status


def test_status_engine_not_running_does_not_call():
    cmds, sup, client = make(state="error")
    sup.last_error = "venv 创建失败"
    text = run(cmds.status_text("微软雅黑"))
    assert "启动失败" in text and "venv 创建失败" in text and "微软雅黑" in text
    assert not client.calls


def test_status_running():
    cmds, _, _ = make(routes={
        "/api/auth/status": LOGGED_IN,
        "/api/comment/resident/status": (200, {"success": True, "data": {
            "status": "running", "target_bvids": ["BV1", "BV2"], "last_collect_at": "2026-11-04T10:20:30",
        }}),
    })
    text = run(cmds.status_text("微软雅黑"))
    assert "运行中" in text
    assert "已登录 鱼（UID 123）" in text
    assert "目标 2 个" in text and "2026-11-04 10:20" in text
    assert "自诊报告字体：微软雅黑" in text


@pytest.mark.parametrize("body,expect", [
    ({"success": True, "logged_in": False, "invalid": True}, "Cookie 已失效"),
    ({"success": True, "logged_in": False}, "未登录"),
])
def test_status_login_states(body, expect):
    cmds, _, _ = make(routes={
        "/api/auth/status": (200, body),
        "/api/comment/resident/status": (200, {"success": True, "data": {"status": "stopped"}}),
    })
    assert expect in run(cmds.status_text("x"))


def test_status_tolerates_non_string_last_collect_at():
    cmds, _, _ = make(routes={
        "/api/auth/status": LOGGED_IN,
        "/api/comment/resident/status": (200, {"success": True, "data": {
            "status": "running", "target_bvids": None, "last_collect_at": 1730700000,
        }}),
    })
    text = run(cmds.status_text("x"))
    assert "目标 0 个" in text and "最近采集 1730700000" in text


def test_status_partial_failure_still_reports():
    cmds, _, _ = make(routes={"/api/auth/status": LOGGED_IN})
    text = run(cmds.status_text("x"))
    assert "已登录" in text and "常驻监控：查询失败" in text


# ---------------------------------------------------------------- alerts


def test_alerts_level_validation():
    cmds, _, client = make()
    with pytest.raises(CommandError, match="告警级别"):
        run(cmds.alerts_text(5, "critical"))
    assert not client.calls


def test_alerts_query_and_format():
    long_msg = "差评" * 200
    cmds, _, client = make(routes={"/api/comment/alerts": (200, {"success": True, "alerts": [
        {"id": 1, "level": "high", "message": long_msg, "created_at": "2026-11-04T09:00:00"},
    ], "count": 1})})
    reply = run(cmds.alerts_text("50", "HIGH"))
    text = reply.as_text()
    assert client.calls[0]["query"] == {"is_read": "false", "limit": 20, "level": "high"}
    assert "（高级）" in text and "[高] 2026-11-04 09:00" in text
    assert len(text.splitlines()[1]) < 260
    assert not reply.use_forward()  # 1 条、长度已截断：普通文本


def test_alerts_many_use_forward():
    alerts = [{"id": i, "level": "high", "message": f"m{i}"} for i in range(8)]
    cmds, _, _ = make(routes={"/api/comment/alerts": (200, {"success": True, "alerts": alerts, "count": 8})})
    reply = run(cmds.alerts_text(20))
    assert reply.use_forward()
    assert reply.nodes()[0] == "【未读告警】最近 8 条" and len(reply.nodes()) == 9


def test_alerts_empty():
    cmds, _, client = make(routes={"/api/comment/alerts": (200, {"success": True, "alerts": [], "count": 0})})
    assert run(cmds.alerts_text(5)).as_text() == "暂无未读告警"
    assert "level" not in client.calls[0]["query"]


def test_format_alert_with_bvid():
    line = FishToolCommands.format_alert({"bvid": "BV1xx411c7mD", "level": "medium", "message": "敏感词"})
    assert line == "[中] BV1xx411c7mD 敏感词"


def test_format_alert_query_result_uses_video_id():
    # /comment/alerts 返回的告警只有 video_id，没有 bvid（modules/comment/_alerts.py get_alerts）
    line = FishToolCommands.format_alert({"id": 3, "video_id": 7, "level": "low", "message": "疑似水军"})
    assert line == "[低] 视频#7 疑似水军"


def test_format_alert_tolerates_odd_types():
    line = FishToolCommands.format_alert({"level": None, "message": None, "created_at": 12345})
    assert line.startswith("[?] 12345")


# ---------------------------------------------------------------- hotspot


def test_hotspot_no_snapshot():
    cmds, _, _ = make(routes={"/api/hotspot/discovery/latest": (200, {"success": True, "data": {"snapshot": None}})})
    assert "尚未跑过" in run(cmds.hotspot_text(10)).as_text()


def test_hotspot_lists_keywords_and_videos():
    cmds, _, _ = make(routes={"/api/hotspot/discovery/latest": (200, {"success": True, "data": {"snapshot": {
        "captured_epoch_s": 0,
        "keywords": [
            {"keyword": "b", "rank": 2, "heat_score": 10},
            {"keyword": "a", "show_name": "甲", "rank": 1, "heat_score": None},
            {"keyword": "c", "rank": None},
        ],
        "videos": [{"bvid": "BV1xx411c7mD", "source_details": [{"view": None}, {"view": 12345}]}],
    }}})})
    reply = run(cmds.hotspot_text(2))
    lines = reply.as_text().splitlines()
    assert lines[2] == "1. 甲" and lines[3] == "2. b（热度 10）"
    assert "c" not in lines[2:4]
    assert "BV1xx411c7mD（播放 12,345）" in reply.as_text()
    assert not reply.use_forward()  # 4 条：普通文本


def test_hotspot_long_list_uses_forward():
    keywords = [{"keyword": f"k{i}", "rank": i} for i in range(1, 31)]
    cmds, _, _ = make(routes={"/api/hotspot/discovery/latest": (200, {"success": True, "data": {"snapshot": {
        "keywords": keywords, "videos": [],
    }}})})
    reply = run(cmds.hotspot_text(30))
    assert reply.use_forward()
    assert reply.nodes()[1] == "热搜词：" and reply.nodes()[2] == "1. k1"


MONITOR_OK = (200, {"success": True, "data": {
    "bvid": "BV1xx411c7mD",
    "success": True,
    "collected_count": 120,
    "processed_count": 98,
    "sentiment_result": {"positive_ratio": 0.5, "negative_ratio": 0.35, "neutral_ratio": 0.1, "risk_ratio": 0.05},
    "alerts": [{"type": "negative_surge", "level": "medium", "message": "负面评论占比过高: 35.0%"}] * 7,
    "warning": None,
}})


def test_monitor_video_summary_and_long_timeout():
    cmds, _, client = make(routes={"/api/comment/monitor": MONITOR_OK}, ecfg={"long_request_timeout_s": 1800})
    reply = run(cmds.monitor_video_text("BV1xx411c7mD", "FULL"))
    text = reply.as_text()
    call = client.calls[0]
    assert call["method"] == "POST" and call["timeout"] == 1800
    assert call["json"] == {"bvid": "BV1xx411c7mD", "strategy": "full"}
    assert "全量" in text and "采集 120 条，去重后 98 条" in text
    assert "负面 35%" in text and "触发 7 条告警" in text
    assert "BV1xx411c7mD [" not in text  # 摘要标题已有 BV 号，告警行不再重复
    # 7 条告警 > 5：改用合并转发，全部列出、不再截断成“另有 N 条”
    assert reply.use_forward() and "另有" not in text
    nodes = reply.nodes()
    assert nodes[0].startswith("【舆情分析】") and "触发 7 条告警" in nodes[0]
    assert len(nodes) == 1 + 7


def test_monitor_video_few_alerts_plain_text():
    data = dict(MONITOR_OK[1]["data"], alerts=MONITOR_OK[1]["data"]["alerts"][:2])
    cmds, _, _ = make(routes={"/api/comment/monitor": (200, {"success": True, "data": data})})
    reply = run(cmds.monitor_video_text("BV1xx411c7mD"))
    assert not reply.use_forward()
    assert "触发 2 条告警：" in reply.as_text()


def test_monitor_video_not_collected():
    cmds, _, _ = make(routes={"/api/comment/monitor": (200, {"success": True, "data": {
        "success": False, "error": "未采集到评论数据，请检查B站Cookie",
    }})})
    assert "未完成：未采集到评论数据" in run(cmds.monitor_video_text("BV1xx411c7mD")).as_text()


def test_monitor_video_validates_before_call():
    cmds, _, client = make()
    for bvid, strategy in (("bad", "normal"), ("BV1xx411c7mD", "fast")):
        try:
            run(cmds.monitor_video_text(bvid, strategy))
        except CommandError:
            pass
        else:
            raise AssertionError("expected CommandError")
    assert not client.calls


@pytest.mark.parametrize("action,path", [("暂停", "/api/comment/resident/pause"), ("停止", "/api/comment/resident/stop")])
def test_resident_pause_stop(action, path):
    cmds, _, client = make(routes={path: (200, {"success": True, "data": {"target_bvids": ["BV1"]}})})
    assert run(cmds.resident_text(action)) == f"常驻监控已{action}，目标 1 个"
    assert client.calls[0]["method"] == "POST"


def test_resident_enable_with_and_without_targets():
    path = "/api/comment/resident/enable"
    cmds, _, client = make(routes={path: (200, {"success": True, "data": {"target_bvids": []}})})
    assert "没有目标视频" in run(cmds.resident_text("开启"))
    assert client.calls[0]["json"] == {}
    run(cmds.resident_text("开启", ["BV1xx411c7mD"]))
    assert client.calls[1]["json"] == {"bvids": ["BV1xx411c7mD"]}
    with pytest.raises(CommandError):
        run(cmds.resident_text("开启", ["bad"]))


def test_resident_bad_action():
    cmds, _, _ = make()
    with pytest.raises(CommandError, match="监控操作"):
        run(cmds.resident_text("重启"))


def test_parse_bvid_list():
    assert FishToolCommands.parse_bvid_list("BV1, BV2，BV1;BV3") == ["BV1", "BV2", "BV3"]
    assert FishToolCommands.parse_bvid_list("") == []
    with pytest.raises(CommandError, match="最多"):
        FishToolCommands.parse_bvid_list(",".join(f"BV{i}" for i in range(21)))


DIAG = (200, {"success": True, "data": {
    "self_data": {"uid": 1, "basic_info": {"name": "鱼<b>"}, "fan_stats": {"follower": 12345},
                  "video_stats": {"total_count": 42, "avg_play": 29394}},
    "benchmark": {"has_benchmark": False}, "benchmark_status": "not_requested",
    "ai_report": {"success": True, "commentary": "建议稳定更新。"},
}})


def test_resolve_uid_explicit_and_from_login():
    cmds, _, client = make(routes={"/api/auth/status": LOGGED_IN})
    assert run(cmds.resolve_uid(" 42 ")) == "42"
    assert not client.calls
    assert run(cmds.resolve_uid("")) == "123"


def test_resolve_uid_requires_login():
    cmds, _, _ = make(routes={"/api/auth/status": (200, {"success": True, "logged_in": False})})
    with pytest.raises(CommandError, match="未登录"):
        run(cmds.resolve_uid(""))


def test_diagnosis_reuses_result_for_report_html():
    cmds, _, client = make(routes={
        "/api/analysis/self-diagnosis": DIAG,
        "/api/analysis/report-html": (200, {"success": True, "data": {"html": "<main class='report'>x</main>"}}),
    }, ecfg={"long_request_timeout_s": 1800, "request_timeout_s": 120})
    html, data = run(cmds.diagnosis("1"))
    diag_call, html_call = client.calls
    assert diag_call["json"] == {"uid": 1} and diag_call["timeout"] == 1800
    sent = html_call["json"]
    assert sent["self_data"] == DIAG[1]["data"]["self_data"]  # 原样传回：引擎不重抓、不再调 LLM
    assert sent["ai_report"]["commentary"] == "建议稳定更新。"
    assert "charts" not in sent  # 不传截图，引擎不生成截图图表章节
    assert html and data["benchmark_status"] == "not_requested"


@pytest.mark.parametrize("routes,msg", [
    ({"/api/analysis/self-diagnosis": (200, {"success": True, "data": {}})}, "没有返回账号数据"),
    ({"/api/analysis/self-diagnosis": DIAG,
      "/api/analysis/report-html": (200, {"success": True, "data": {"html": "  "}})}, "没有返回报告内容"),
    ({"/api/analysis/self-diagnosis": (500, {"detail": "LLM 超时"})}, "500"),
])
def test_diagnosis_errors(routes, msg):
    cmds, _, _ = make(routes=routes)
    with pytest.raises(CommandError, match=msg):
        run(cmds.diagnosis("1"))


def test_diagnosis_summary():
    text = FishToolCommands.diagnosis_summary(DIAG[1]["data"], "1")
    assert text.splitlines()[0] == "【账号自诊】鱼<b>（UID 1）"
    assert "粉丝 12,345，投稿 42，平均播放 29,394" in text and "运营点评：建议稳定更新。" in text
    failed = FishToolCommands.diagnosis_summary({"self_data": {}, "ai_report": {"success": False, "message": "未配置模型"}}, "2")
    assert "粉丝 暂无" in failed and "AI 调研未生成：未配置模型" in failed


def test_hotspot_empty_snapshot():
    cmds, _, _ = make(routes={"/api/hotspot/discovery/latest": (200, {"success": True, "data": {
        "snapshot": {"keywords": [], "videos": []},
    }})})
    assert "没有可展示" in run(cmds.hotspot_text(10)).as_text()
