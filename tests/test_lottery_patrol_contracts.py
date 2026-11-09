"""AI 伪人巡检配置加载：默认值、硬上限、阈值一致性、AstrBot 环境变量。"""
import pytest

from modules.lottery.patrol import contracts
from modules.lottery.patrol.contracts import (
    HARD_REPORT_DAILY,
    HARD_REPORT_PER_TASK,
    astrbot_allows_proxy_report,
    build_settings,
    is_embedded_by_astrbot,
    load_settings,
)


def test_empty_config_uses_defaults():
    s = build_settings({})
    assert s.enabled is True
    assert s.max_comments_default == 500
    assert (s.bot_threshold, s.suspect_threshold, s.gray_low, s.gray_high) == (75, 45, 20, 85)
    assert s.soft_cap == 70 and s.hard_hit_floor == 60
    assert s.report_default_mode == "native"
    assert s.report_max_per_task == 5 and s.report_daily_limit == 10
    assert s.report_interval_seconds == (3.0, 6.0)
    assert s.report_require_ai is True and s.report_preselect is False


def test_none_and_garbage_config_fall_back():
    assert build_settings(None).max_comments_default == 500
    assert build_settings("oops").bot_threshold == 75


def test_hard_limits_cannot_be_raised():
    s = build_settings({"report": {"daily_limit": 999, "max_per_task": 99, "per_account_limit": 9},
                        "max_comments_hard": 99999, "reply_requests_hard": 500,
                        "judge": {"max_items_hard": 9999, "max_items": 9999}})
    assert s.report_daily_limit == HARD_REPORT_DAILY
    assert s.report_max_per_task == HARD_REPORT_PER_TASK
    assert s.report_per_account_limit == 2
    assert s.max_comments_hard == 2000
    assert s.reply_requests_hard == 60
    assert s.judge_max_items_hard == 500 and s.judge_max_items == 500


def test_wrong_types_are_dropped():
    s = build_settings({"report": {"max_per_task": "abc", "dry_run": "yes"}, "enabled": 1})
    assert s.report_max_per_task == 5
    assert s.report_dry_run is False
    assert s.enabled is True


@pytest.mark.parametrize("patch", [
    {"soft_cap": 80},
    {"hard_hit_floor": 90},
    {"thresholds": {"bot": 40, "suspect": 45}},
    {"thresholds": {"gray_low": 90, "gray_high": 50}},
    {"fusion_weight_llm": 1.5},
])
def test_inconsistent_thresholds_reset_group(patch):
    s = build_settings(patch)
    assert (s.bot_threshold, s.suspect_threshold, s.soft_cap, s.hard_hit_floor) == (75, 45, 70, 60)
    assert s.fusion_weight_llm == 0.55


def test_interval_is_sorted_and_floored():
    s = build_settings({"report": {"interval_seconds": [9, 1]}})
    assert s.report_interval_seconds == (2.0, 9.0)


def test_unknown_endpoint_and_mode_fall_back():
    s = build_settings({"comment_endpoint": "evil", "report": {"default_mode": "auto"}})
    assert s.comment_endpoint == "main" and s.report_default_mode == "native"


def test_load_settings_reads_fresh_config(monkeypatch):
    import importlib

    # core/__init__.py 把包属性 core.config 覆盖成了单例实例，必须按模块名导入。
    config_module = importlib.import_module("core.config")
    monkeypatch.setattr(config_module.ConfigManager, "get",
                        lambda self, key, default=None: {"max_comments_default": 200} if key == "patrol" else default)
    assert load_settings().max_comments_default == 200


def test_astrbot_env_flags(monkeypatch):
    monkeypatch.delenv("FISHTOOL_EMBEDDED_BY", raising=False)
    monkeypatch.delenv("FISHTOOL_ASTRBOT_ALLOW_PROXY_REPORT", raising=False)
    assert is_embedded_by_astrbot() is False
    assert astrbot_allows_proxy_report() is False
    monkeypatch.setenv("FISHTOOL_EMBEDDED_BY", "astrbot")
    assert is_embedded_by_astrbot() is True
    for value, expected in (("1", True), ("0", False), ("true", False), ("", False)):
        monkeypatch.setenv("FISHTOOL_ASTRBOT_ALLOW_PROXY_REPORT", value)
        assert astrbot_allows_proxy_report() is expected


def test_stop_batch_carries_flags():
    err = contracts.StopBatch("x", cooldown=True, high=True, bili_code=12019)
    assert err.cooldown and err.high and err.bili_code == 12019 and err.message == "x"
