"""charge_count 功能回归测试。"""

import asyncio
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from sqlalchemy import inspect

from bilibili.api import BilibiliAPI
from bilibili.rate_limiter import RateLimiter
from core.database.manager import DatabaseManager
from modules.self_diagnosis.report_generator import ReportGenerator


def test_charge_count_parser_supports_compatible_payloads() -> None:
    """验证充电人数解析兼容 count、total_count 和嵌套 data。"""
    assert BilibiliAPI.extract_charge_count({'count': 7}) == 7
    assert BilibiliAPI.extract_charge_count({'data': {'total_count': 9}}) == 9
    assert BilibiliAPI.extract_charge_count({'battery_list': []}) is None


def test_old_database_is_migrated() -> None:
    """验证旧库初始化后自动补充 up_masters.charge_count。"""
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / 'legacy.db'
        manager = DatabaseManager(str(path))
        columns = {
            column['name'] for column in inspect(manager.engine).get_columns('up_masters')
        }
        assert 'charge_count' in columns
        manager.engine.dispose()


def test_report_contains_charge_count() -> None:
    """验证自诊 Markdown 报告输出充电人数。"""
    report = ReportGenerator().generate_markdown_report({
        'uid': 42,
        'basic_info': {'name': '测试账号', 'level': 6},
        'fan_stats': {'follower': 100, 'following': 3, 'charge_count': 8},
        'video_stats': {},
        'post_rhythm': {},
        'engagement_metrics': {},
        'data_availability': {},
    })
    assert '充电人数' in report
    assert '8' in report


def test_charge_budget_waits_after_twenty_requests(monkeypatch) -> None:
    """验证单 Cookie 预算超过 20 次后进入滑动窗口等待。"""
    limiter = RateLimiter(rate=0)
    sleeps = []

    async def fake_sleep(seconds: float) -> None:
        """记录等待秒数，不在测试中真实等待。"""
        sleeps.append(seconds)

    monkeypatch.setattr('bilibili.rate_limiter.asyncio.sleep', fake_sleep)

    async def run() -> None:
        """连续提交 21 次同 Cookie 预算请求。"""
        for _ in range(21):
            await limiter.acquire_cookie_budget('digest', budget_key='charge')

    asyncio.run(run())
    assert len(sleeps) == 1
