"""抽奖工具核心回归测试。"""

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from core.database import Comment, DatabaseManager, Video

from modules.lottery.analyzer import heuristic_classify
from modules.lottery.service import LotteryService
from modules.lottery.target import LotteryTarget, parse_target_input


def test_comment_database_migration_and_local_metadata(tmp_path: Path) -> None:
    """旧库迁移后应能持久化并读取评论等级与年度大会员字段。"""
    manager = DatabaseManager(str(tmp_path / "lottery.db"))
    columns = {column["name"] for column in __import__("sqlalchemy").inspect(manager.engine).get_columns("comments")}
    assert {"level_info", "vip"}.issubset(columns)

    session = manager.get_session()
    video = Video(bvid="BV1xx411c7mD", title="测试")
    session.add(video)
    session.flush()
    session.add(Comment(
        rpid="7", video_id=video.id, uid=42, uname="年度会员", content="参与抽奖",
        ctime=datetime(2026, 8, 10, 12), level_info={"current_level": 6},
        vip={"vipStatus": 1, "vipType": 2},
    ))
    session.commit()
    row = session.query(Comment).one()
    candidate = LotteryService._comment_row_to_dict(row)
    session.close()

    assert candidate["level"] == 6
    assert candidate["vip_type"] == 2
    assert candidate["vip_label"] == "年度大会员"
    assert candidate["ctime"] == "2026-08-10T12:00:00"


def test_parse_video_and_dynamic_target() -> None:
    """BV号和动态完整链接应被稳定识别，纯数字动态应拒绝。"""
    assert parse_target_input("BV1xx411c7mD") == ("video", "BV1xx411c7mD")
    assert parse_target_input("https://www.bilibili.com/opus/123456") == ("dynamic", "123456")
    with pytest.raises(ValueError, match="完整链接"):
        parse_target_input("123456")


def test_heuristic_lottery_account_classification() -> None:
    """全是抽奖转发且低活跃质量的账号应进入疑似分类。"""
    result = heuristic_classify({
        "uid": 42,
        "level": 1,
        "recent_activity_count": 6,
        "lottery_repost_ratio": 1.0,
        "observable_account_days": 10,
        "video_count": 0,
    })
    assert result["classification"] == "suspicious"
    assert result["source"] == "heuristic"


def test_parse_reply_preserves_annual_vip_metadata() -> None:
    """评论清洗必须保留年度大会员等级，供前端渲染三级会员徽标。"""
    reply = {
        "rpid": 7,
        "ctime": 1786324800,
        "member": {
            "mid": "42",
            "uname": "年度会员",
            "level_info": {"current_level": 6},
            "vip": {"vipStatus": 1, "vipType": 2},
        },
        "content": {"message": "参与抽奖"},
    }

    parsed = LotteryService._parse_reply(reply)
    assert parsed is not None
    assert parsed["level"] == 6
    assert parsed["level_info"] == {"current_level": 6}
    assert parsed["vip"] == {"vipStatus": 1, "vipType": 2}
    assert parsed["is_vip"] is True
    assert parsed["vip_type"] == 2
    assert parsed["vip_label"] == "年度大会员"
    assert parsed["ctime"]


def test_draw_deduplicates_users_and_reports_source() -> None:
    """开启UID去重后抽奖池应按用户去重并保留数据来源。"""
    service = LotteryService(api=object())
    target = LotteryTarget("video", "BV1xx411c7mD", 1, 1, "标题", "发布者")

    async def fake_get_comments(_target, _progress=None):
        return ([
            {"uid": 1, "uname": "甲", "content": "A", "ctime": "2026-08-10T12:00:00", "level": 3, "is_vip": False, "vip_type": 0, "vip_label": "非会员"},
            {"uid": 1, "uname": "甲", "content": "B", "ctime": "2026-08-10T12:01:00", "level": 3, "is_vip": False, "vip_type": 0, "vip_label": "非会员"},
            {"uid": 2, "uname": "乙", "content": "C", "ctime": "2026-08-10T12:02:00", "level": 5, "is_vip": True, "vip_type": 1, "vip_label": "大会员"},
        ], "local")

    service.get_comments = fake_get_comments
    result = asyncio.run(service.draw(target, winner_count=2, unique_users=True))
    assert result["candidate_count"] == 2
    assert result["data_source"] == "local"
    assert {item["uid"] for item in result["winners"]} == {1, 2}


def test_draw_filters_vip_level_and_comment_date() -> None:
    """抽奖池应同时执行大会员、最低等级和评论日期闭区间筛选。"""
    service = LotteryService(api=object())
    target = LotteryTarget("video", "BV1xx411c7mD", 1, 1, "标题", "发布者")

    async def fake_get_comments(_target, _progress=None):
        return [
            {"uid": 1, "uname": "符合", "content": "A", "ctime": "2026-08-10T12:00:00", "level": 5, "is_vip": True},
            {"uid": 2, "uname": "等级低", "content": "B", "ctime": "2026-08-10T12:00:00", "level": 2, "is_vip": True},
            {"uid": 3, "uname": "非会员", "content": "C", "ctime": "2026-08-10T12:00:00", "level": 6, "is_vip": False},
            {"uid": 4, "uname": "日期早", "content": "D", "ctime": "2026-07-01T12:00:00", "level": 6, "is_vip": True},
        ], "local"

    service.get_comments = fake_get_comments
    result = asyncio.run(service.draw(
        target,
        winner_count=1,
        unique_users=True,
        vip_only=True,
        min_level=4,
        date_start=datetime(2026, 8, 1),
        date_end=datetime(2026, 8, 31, 23, 59, 59),
    ))

    assert result["candidate_count"] == 1
    assert result["candidates"][0]["uid"] == 1
    assert result["excluded"] == {"vip": 1, "level": 1, "date": 1, "duplicate": 0}
    assert result["data_source"] == "local"


def test_lottery_button_endpoints_return_non_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """逐按钮通过 ASGI 发起真实 HTTP 请求，确保前后端接线不再返回 404。

    Args:
        monkeypatch: pytest 注入的临时替换工具。

    Returns:
        无；任一按钮接口为 404 时测试失败。
    """
    from fastapi.testclient import TestClient
    from web.main import app
    from web.routers import lottery

    class FakeLotteryService:
        """隔离 B 站网络请求，仅验证 Web 路由接线。"""

        async def preview(self, target: str) -> dict:
            """返回固定目标预览数据。"""
            return {"target_id": target, "title": "接线测试", "author": "可乐"}

        async def quick_filter(self, uid: int, focus_template: str | None) -> dict:
            """返回固定单 UID 快筛数据。"""
            return {"profile": {"uid": uid}, "assessment": {"classification": "real"}}

        async def verify_winners(self, winners: list[dict], focus_template: str | None = None) -> dict:
            """返回固定中奖名单校验结果，避免调用真实 LLM 和网络。"""
            return {
                "winner_count": len(winners), "local_count": len(winners), "fetched_count": 0,
                "real_count": len(winners), "suspicious_count": 0, "results": [],
            }

    async def fake_filter_task(task_id, request):
        """隔离后台筛选协程，避免测试假服务泄漏到真实任务逻辑。"""
        lottery._tasks[task_id].update(status="completed", progress=100, result={})

    async def fake_draw_task(task_id, request):
        """隔离后台抽奖协程，避免访问 B 站或污染生产服务状态。"""
        lottery._tasks[task_id].update(status="completed", progress=100, result={"winners": []})

    fake_service = FakeLotteryService()
    monkeypatch.setattr(lottery, "get_service", lambda: fake_service)
    monkeypatch.setattr(lottery, "_run_filter_task", fake_filter_task)
    monkeypatch.setattr(lottery, "_run_draw_task", fake_draw_task)
    lottery._tasks["route-proof"] = {
        "task_id": "route-proof", "kind": "draw", "status": "completed",
        "stage": "completed", "progress": 100, "message": "ok",
        "estimated_seconds": 0, "result": {}, "started_monotonic": 0,
    }

    with TestClient(app) as client:
        checks = [
            ("内容预览", client.post("/api/lottery/preview", json={"target": "BV1xx411c7mD"}), 200),
            ("真人筛选", client.post("/api/lottery/filter/tasks", json={"target": "BV1xx411c7mD"}), 202),
            ("单UID快筛", client.post("/api/lottery/quick-filter", json={"uid": 42}), 200),
            ("中奖名单校验", client.post("/api/lottery/verify-winners", json={"winners": [{"uid": 42}]}), 200),
            ("随机抽奖", client.post("/api/lottery/draw/tasks", json={"target": "BV1xx411c7mD"}), 202),
            ("任务轮询", client.get("/api/lottery/tasks/route-proof"), 200),
        ]

    for button_name, response, expected_status in checks:
        assert response.status_code != 404, f"{button_name}仍返回 not found"
        assert response.status_code == expected_status, response.text


def test_lottery_frontend_paths_match_backend_contract() -> None:
    """前端五条抽奖请求路径必须与后端 /api/lottery 路由完全一致。"""
    from pathlib import Path

    # 拆分后抽奖路径集中在 app.lottery.js，API_BASE 常量在 app.core.js。
    lottery_js = Path("web/frontend/static/js/app.lottery.js").read_text(encoding="utf-8")
    core_js = Path("web/frontend/static/js/app.core.js").read_text(encoding="utf-8")
    expected_paths = {
        "/lottery/preview",
        "/lottery/filter/tasks",
        "/lottery/quick-filter",
        "/lottery/verify-winners",
        "/lottery/draw/tasks",
        "/lottery/tasks/",
    }
    for path in expected_paths:
        assert path in lottery_js, f"前端缺少抽奖请求路径: {path}"
    assert "API_BASE = '/api'" in core_js


def test_lottery_routes_are_mounted() -> None:
    """主应用必须挂载预览、筛选、抽奖和任务查询路由。"""
    from web.main import app
    from web.routers import lottery

    router_paths = {route.path for route in lottery.router.routes}
    included = [route for route in app.routes if getattr(route, "original_router", None) is lottery.router]
    assert included and included[0].include_context.prefix == "/api"
    assert "/lottery/preview" in router_paths
    assert "/lottery/quick-filter" in router_paths
    assert "/lottery/verify-winners" in router_paths
    assert "/lottery/filter/tasks" in router_paths
    assert "/lottery/draw/tasks" in router_paths
    assert "/lottery/tasks/{task_id}" in router_paths


def test_verify_winners_without_draw_returns_product_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端没有中奖名单时必须返回前端可直接展示的产品提示。"""
    from fastapi.testclient import TestClient
    from web.main import app
    from web.routers import lottery

    monkeypatch.setattr(lottery, "_latest_winners", [])
    with TestClient(app) as client:
        response = client.post("/api/lottery/verify-winners", json={})

    assert response.status_code == 400
    assert response.json()["detail"] == "请先进行抽奖！"
