"""评论采集器示例入口的契约级测试。

覆盖 modules/comment/collector/runner.py 的 demo_collect_comments：
- 正常路径：注入假 cookie 池/API/采集器，验证调用参数与打印输出
- 缺陷固化：未注入 CommentCollector 时函数内全局名缺失，抛 NameError

示例函数只做演示编排，不落库；网络依赖全部替换为契约级假对象。
"""

from __future__ import annotations

import asyncio

import pytest

from modules.comment.collector import demo_collect_comments
from modules.comment.collector import runner as runner_module


class FakeCollector:
    """记录采集调用的假采集器，同时暴露策略常量。"""

    STRATEGY_NORMAL = "normal"

    created: list["FakeCollector"] = []

    def __init__(self, api) -> None:
        self.api = api
        self.calls: list[tuple] = []
        FakeCollector.created.append(self)

    async def collect_video_comments(self, bvid, strategy=None):
        """返回固定条数的假评论。"""
        self.calls.append((bvid, strategy))
        return [
            {"uname": f"u{index}", "content": "内容" * 40, "like": index, "ctime": "2026-01-01"}
            for index in range(3)
        ]


class FakeAPI:
    """记录 cookie_pool 入参的假 BilibiliAPI。"""

    def __init__(self, cookie_pool=None) -> None:
        self.cookie_pool = cookie_pool


@pytest.fixture()
def fake_deps(monkeypatch: pytest.MonkeyPatch):
    """注入假 cookie 池、假 API 与可记录的假采集器。"""
    import bilibili.cookie_pool as cookie_pool_module

    sentinel = object()
    monkeypatch.setattr(cookie_pool_module, "get_cookie_pool", lambda: sentinel)
    monkeypatch.setattr(runner_module, "BilibiliAPI", FakeAPI)
    # CommentCollector 并非 runner 模块的全局名，用 raising=False 临时注入。
    monkeypatch.setattr(runner_module, "CommentCollector", FakeCollector, raising=False)
    FakeCollector.created.clear()
    return sentinel


def test_demo_collect_comments_runs_happy_path(fake_deps, capsys) -> None:
    """示例入口应初始化依赖链、按普通策略采集并打印统计。"""
    asyncio.run(demo_collect_comments())

    out = capsys.readouterr().out
    assert "采集到 3 条评论" in out
    assert FakeCollector.created, "应创建采集器实例"
    collector = FakeCollector.created[0]
    assert isinstance(collector.api, FakeAPI)
    assert collector.api.cookie_pool is fake_deps
    assert collector.calls == [("BV1xx411c7XZ", "normal")]


def test_demo_collect_comments_truncates_preview_to_five(capsys, fake_deps) -> None:
    """预览打印最多展示 5 条评论。"""
    asyncio.run(demo_collect_comments())

    out = capsys.readouterr().out
    assert out.count("点赞:") == 3


def test_demo_collect_comments_name_error_without_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    """【缺陷固化】runner 模块未导入 CommentCollector，直接运行会抛 NameError。

    示例函数体内使用裸全局名 ``CommentCollector``，而该名字只在
    collector/__init__.py 中定义、并未注入 runner 模块命名空间。
    此处仅固化当前现状，未修改生产代码。
    """
    import bilibili.cookie_pool as cookie_pool_module

    monkeypatch.setattr(cookie_pool_module, "get_cookie_pool", lambda: object())
    monkeypatch.setattr(runner_module, "BilibiliAPI", FakeAPI)

    with pytest.raises(NameError):
        asyncio.run(demo_collect_comments())


def test_demo_collect_comments_is_exported() -> None:
    """示例入口应可从包入口导入。"""
    from modules.comment import collector as collector_package

    assert collector_package.demo_collect_comments is demo_collect_comments
