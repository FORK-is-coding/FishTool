"""用户报告的扫码、抽奖与日志页面回归测试。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from bilibili.api import BilibiliAPI
from bilibili.auth import QRCodeLogin
from bilibili.rate_limiter import RateLimiter
from modules.lottery.service import LotteryService
from web.routers import logs as logs_router
from web.routers import lottery as lottery_router


def test_qrcode_generation_has_initialized_session_contract(monkeypatch) -> None:
    """扫码客户端应具备完整构造属性并能生成真实 PNG 二维码。"""

    async def fake_init_session(self) -> None:
        """跳过测试中的真实网络会话初始化。"""

    async def fake_close(self) -> None:
        """跳过测试中的真实会话关闭。"""

    async def fake_get(self, url, **kwargs):
        """返回 B 站二维码生成接口的结构化测试响应。"""
        return {"url": "https://passport.bilibili.com/h5-app/passport/login/scan", "qrcode_key": "test-key"}

    monkeypatch.setattr(BilibiliAPI, "init_session", fake_init_session)
    monkeypatch.setattr(BilibiliAPI, "close", fake_close)
    monkeypatch.setattr(BilibiliAPI, "get", fake_get)

    client = BilibiliAPI()
    assert client.session is None
    assert client.headers["Referer"] == "https://www.bilibili.com/"
    qr = QRCodeLogin()
    qr_url, image = asyncio.run(qr.generate_qrcode())

    assert qr_url.startswith("https://passport.bilibili.com/")
    assert qr.qrcode_key == "test-key"
    assert image.startswith(b"\x89PNG\r\n\x1a\n")


def test_lottery_preview_and_uid_analysis_use_same_constructor_contract(monkeypatch) -> None:
    """抽奖预览与 UID 分析应共享可注入限频器的 API 构造契约。"""

    async def fake_get(self, url, params=None, **kwargs):
        """按 URL 返回抽奖目标或动态画像测试数据。"""
        if url.endswith("/x/web-interface/view"):
            return {"bvid": "BV1xx411c7mD", "aid": 170001, "title": "回归测试视频", "owner": {"name": "测试UP"}}
        if "feed/space" in url:
            return {"items": []}
        return {}

    async def fake_user_info(self, uid):
        """返回 UID 基础资料。"""
        return {"data": {"name": "测试用户", "level": 5}}

    async def fake_relation(self, uid):
        """返回 UID 关系数据。"""
        return {"data": {"follower": 120, "following": 30}}

    async def fake_videos(self, uid, page=1, page_size=10):
        """返回 UID 投稿统计。"""
        return {"data": {"page": {"count": 8}}}

    async def fake_classify(profiles, focus_template=None):
        """避免测试访问外部 LLM，保留业务结果结构。"""
        return [{"uid": profiles[0]["uid"], "classification": "real", "confidence": 0.9, "reasons": ["公开资料完整"]}]

    monkeypatch.setattr(BilibiliAPI, "get", fake_get)
    monkeypatch.setattr(BilibiliAPI, "get_user_info", fake_user_info)
    monkeypatch.setattr(BilibiliAPI, "get_user_relation_stat", fake_relation)
    monkeypatch.setattr(BilibiliAPI, "get_user_videos", fake_videos)
    monkeypatch.setattr("modules.lottery.service.classify_profiles", fake_classify)

    api = BilibiliAPI(rate_limiter=RateLimiter(), cookie_pool=SimpleNamespace())
    service = LotteryService(api)
    preview = asyncio.run(service.preview("BV1xx411c7mD"))
    analysis = asyncio.run(service.quick_filter(42, None))

    assert preview["title"] == "回归测试视频"
    assert preview["author"] == "测试UP"
    assert analysis["profile"]["name"] == "测试用户"
    assert analysis["assessment"]["classification"] == "real"


def test_user_info_falls_back_to_public_card_endpoint(monkeypatch) -> None:
    """空间 WBI 接口受风控时应从公开名片接口恢复昵称和等级。"""
    calls = []

    async def fake_get(self, url, params=None, need_sign=False, **kwargs):
        """主接口模拟风控，回退接口返回公开名片。"""
        calls.append((url, need_sign))
        if "wbi/acc/info" in url:
            raise RuntimeError("-352")
        return {
            "card": {
                "mid": "2",
                "name": "碧诗",
                "level_info": {"current_level": 6},
                "fans": 100,
                "attention": 10,
            },
            "follower": 100,
        }

    monkeypatch.setattr(BilibiliAPI, "get", fake_get)
    result = asyncio.run(BilibiliAPI().get_user_info(2))

    assert result["data"]["name"] == "碧诗"
    assert result["data"]["level"] == 6
    assert calls[0][1] is True
    assert calls[1][1] is False


def test_logs_query_follows_runtime_logger_directory(monkeypatch, tmp_path: Path) -> None:
    """日志查询应读取 LoggerManager 的实际写入目录并返回结构化记录。"""
    app_log = tmp_path / "app.log"
    app_log.write_text(
        "2026-08-22 18:30:00 [\x1b[32mINFO\x1b[0m] regression [test.py:1] - 日志页面实测记录\n",
        encoding="utf-8",
    )
    (tmp_path / "risk_control.log").write_text("", encoding="utf-8")
    monkeypatch.setattr(logs_router.logger_module, "logger_manager", SimpleNamespace(log_dir=tmp_path))

    response = asyncio.run(logs_router.get_logs(log_type="all", limit=20))

    assert response["returned"] == 1
    assert response["records"][0]["message"] == "日志页面实测记录"
    assert response["records"][0]["source"] == "regression"
