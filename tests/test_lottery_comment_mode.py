"""评论采集方式（comment_mode）分流与接口契约测试。

覆盖：
- 服务层：standard 走公共评论采集器与本地快照，up_fulllist 走 UP 主评论管理接口；
  动态目标忽略 comment_mode；默认值是 standard（与改动前行为一致）。
- draw / filter_real_users 把 comment_mode 透传给 get_comments。
- 路由层：抽奖与真人筛选两个任务接口都接受 comment_mode，默认 standard，非法取值 422；
  任务指纹包含 comment_mode，两种方式不会互相复用运行中的任务。
- up_fulllist 进度文案带剩余耗时估算，间隔取评论采集器的限频间隔。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bilibili.api import BilibiliAPI
from modules.lottery.service import LotteryService
from modules.lottery.target import LotteryTarget
from web.routers import lottery as lottery_module


def _video_target() -> LotteryTarget:
    return LotteryTarget("video", "BV1xx411c7mD", 170001, 1, "????", "??UP")


def _dynamic_target() -> LotteryTarget:
    return LotteryTarget("dynamic", "123", 123, 17, "????", "??UP")


@pytest.fixture()
def service(tmp_path: Path) -> LotteryService:
    return LotteryService(BilibiliAPI(), cache_dir=tmp_path / "lottery_cache")


class _Recorder:
    """记录 standard / up_fulllist / dynamic 三条采集链路各被调用几次。"""

    def __init__(self, service: LotteryService, monkeypatch) -> None:
        self.calls: list[str] = []

        async def fake_fulllist(target, progress=None):
            self.calls.append("up_fulllist")
            return [{"rpid": "m1", "uid": 1}]

        async def fake_dynamic(target, progress=None, max_count=None):
            self.calls.append("dynamic")
            return [{"rpid": "d1", "uid": 2}]

        class _Collector:
            last_save_result = {"success": True}
            rate_limiter = service.collector.rate_limiter

            async def collect_video_comments(inner, bvid, strategy=None, max_count=None):
                self.calls.append("standard")
                return [{"rpid": "s1", "uid": 3}]

        monkeypatch.setattr(service, "_collect_video_comments", fake_fulllist)
        monkeypatch.setattr(service, "_collect_dynamic_comments", fake_dynamic)
        service.collector = _Collector()


def test_get_comments_defaults_to_standard_for_video(service, monkeypatch):
    rec = _Recorder(service, monkeypatch)
    comments, source = asyncio.run(service.get_comments(_video_target()))
    assert rec.calls == ["standard"]
    assert source == "crawler"
    assert [c["rpid"] for c in comments] == ["s1"]


def test_get_comments_up_fulllist_uses_management_list(service, monkeypatch):
    rec = _Recorder(service, monkeypatch)
    comments, source = asyncio.run(service.get_comments(_video_target(), comment_mode="up_fulllist"))
    assert rec.calls == ["up_fulllist"]
    assert source == "crawler"
    assert [c["rpid"] for c in comments] == ["m1"]


def test_up_fulllist_never_reuses_video_snapshot(service, monkeypatch):
    """楼中楼模式每次实时采集：即使存在 TTL 内的完整视频快照也不复用。"""
    rec = _Recorder(service, monkeypatch)
    service._cache.mark_video_snapshot("BV1xx411c7mD", 1, complete=True)
    monkeypatch.setattr(service, "_load_video_comments", lambda bvid: [{"rpid": "local", "uid": 9}])
    _, source = asyncio.run(service.get_comments(_video_target(), comment_mode="up_fulllist"))
    assert rec.calls == ["up_fulllist"]
    assert source == "crawler"


@pytest.mark.parametrize("mode", ["standard", "up_fulllist"])
def test_dynamic_target_ignores_comment_mode(service, monkeypatch, mode):
    rec = _Recorder(service, monkeypatch)
    _, source = asyncio.run(service.get_comments(_dynamic_target(), comment_mode=mode))
    assert rec.calls == ["dynamic"]
    assert source == "crawler"


@pytest.mark.parametrize("mode", ["standard", "up_fulllist"])
def test_draw_and_filter_forward_comment_mode(service, monkeypatch, mode):
    seen: list[str] = []

    async def fake_get_comments(target, progress=None, comment_mode="standard"):
        seen.append(comment_mode)
        return [], "crawler"

    monkeypatch.setattr(service, "get_comments", fake_get_comments)

    async def fake_profile_and_classify(*args, **kwargs):
        return [], []

    monkeypatch.setattr(service, "_profile_and_classify", fake_profile_and_classify)
    asyncio.run(service.filter_real_users(_video_target(), None, comment_mode=mode))
    with pytest.raises(ValueError):
        # 空候选池会被拒绝，这里只关心 comment_mode 是否透传到了 get_comments
        asyncio.run(service.draw(_video_target(), 1, True, comment_mode=mode))
    assert seen == [mode, mode]


def test_fulllist_eta_suffix_uses_collector_interval(service):
    service.collector.rate_limiter.rate = 4.0
    # 10_050 条 / 50 每页 = 202 页；读完第 1 页还剩 201 页 × 4 秒 ≈ 13.4 分钟
    assert service._fulllist_eta_suffix(10_050, 1) == "，预计还需约 14 分钟"
    assert service._fulllist_eta_suffix(120, 1) == "，预计还需约 8 秒"
    assert service._fulllist_eta_suffix(50, 1) == ""
    assert service._fulllist_eta_suffix(0, 1) == ""


# ------------------------------------------------------------------ 路由层


class _ModeService:
    """只记录路由层传给服务层的 comment_mode。"""

    calls: list = []
    api = None  # ?????????? fetch_target_metadata?????

    async def filter_real_users(self, target, focus_template, progress_callback=None, **kwargs):
        type(self).calls.append(("filter", kwargs.get("comment_mode", "standard")))
        return {"real_users": [], "suspicious_users": [], "indeterminate_users": []}

    async def draw(self, target, winner_count, unique_users, progress_callback=None, **kwargs):
        type(self).calls.append(("draw", kwargs.get("comment_mode", "standard")))
        return {"winners": []}


@pytest.fixture()
def client(monkeypatch):
    _ModeService.calls = []
    spawned: list = []

    def fake_spawn(task_id, coro):
        spawned.append(coro)

    async def fake_fetch(api, raw):
        return _video_target()

    monkeypatch.setattr(lottery_module, "get_service", lambda: _ModeService())
    monkeypatch.setattr(lottery_module, "fetch_target_metadata", fake_fetch)
    monkeypatch.setattr(lottery_module, "_tasks", {})
    monkeypatch.setattr(lottery_module, "_spawn", fake_spawn)
    app = FastAPI()
    app.include_router(lottery_module.router, prefix="/api")
    test_client = TestClient(app)
    test_client.spawned = spawned
    return test_client


@pytest.mark.parametrize("path", ["/api/lottery/draw/tasks", "/api/lottery/filter/tasks"])
def test_task_endpoints_reject_unknown_comment_mode(client, path):
    response = client.post(path, json={"target": "BV1xx411c7mD", "comment_mode": "nested"})
    assert response.status_code == 422


@pytest.mark.parametrize("kind", ["draw", "filter"])
def test_request_models_default_to_standard(kind):
    model = lottery_module.DrawTaskRequest if kind == "draw" else lottery_module.FilterTaskRequest
    assert model(target="BV1xx411c7mD").comment_mode == "standard"
    assert model(target="BV1xx411c7mD", comment_mode="up_fulllist").comment_mode == "up_fulllist"


def test_fingerprint_distinguishes_comment_mode():
    standard = lottery_module.DrawTaskRequest(target="BV1xx411c7mD")
    fulllist = lottery_module.DrawTaskRequest(target="BV1xx411c7mD", comment_mode="up_fulllist")
    assert lottery_module._request_fingerprint("draw", standard) != lottery_module._request_fingerprint("draw", fulllist)


@pytest.mark.parametrize("mode", ["standard", "up_fulllist"])
def test_task_runners_forward_comment_mode(monkeypatch, mode):
    _ModeService.calls = []

    async def fake_fetch(api, raw):
        return _video_target()

    monkeypatch.setattr(lottery_module, "get_service", lambda: _ModeService())
    monkeypatch.setattr(lottery_module, "fetch_target_metadata", fake_fetch)
    monkeypatch.setattr(lottery_module, "_tasks", {})
    draw_id = lottery_module._create_task("draw")
    filter_id = lottery_module._create_task("filter")
    asyncio.run(lottery_module._run_draw_task(draw_id, lottery_module.DrawTaskRequest(target="BV1xx411c7mD", comment_mode=mode)))
    asyncio.run(lottery_module._run_filter_task(filter_id, lottery_module.FilterTaskRequest(target="BV1xx411c7mD", comment_mode=mode)))
    assert _ModeService.calls == [("draw", mode), ("filter", mode)]


def test_task_runners_accept_requests_without_comment_mode(monkeypatch):
    """旧调用方传来的请求对象没有 comment_mode 时按 standard 处理，且不向服务层传该参数。"""
    _ModeService.calls = []

    async def fake_fetch(api, raw):
        return _video_target()

    monkeypatch.setattr(lottery_module, "get_service", lambda: _ModeService())
    monkeypatch.setattr(lottery_module, "fetch_target_metadata", fake_fetch)
    monkeypatch.setattr(lottery_module, "_tasks", {})
    request = type("Req", (), {
        "target": "BV1xx411c7mD", "winner_count": 1, "unique_users": True, "vip_only": False,
        "min_level": None, "real_only": False, "include_indeterminate": False,
        "focus_template": None, "date_start": None, "date_end": None,
    })()
    asyncio.run(lottery_module._run_draw_task(lottery_module._create_task("draw"), request))
    assert _ModeService.calls == [("draw", "standard")]
