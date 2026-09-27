"""评论采集示例入口测试（modules/comment/collector/runner.py）。

覆盖 runner.py 的唯一协程 demo_collect_comments：
- 依赖链装配（Cookie 池 -> API -> 采集器）
- 打印首 5 条评论的展示逻辑
- 固化现状缺陷：runner 命名空间没有导入 CommentCollector，直接调用必抛 NameError

测试策略：
- 只替换 Cookie 池与 BilibiliAPI 为契约级假对象，采集器被显式注入到 runner 命名空间；
- 使用 capsys 断言控制台输出，不触网、不写库。
"""
import asyncio
import inspect

import pytest

import bilibili.cookie_pool as cookie_pool_module
from modules.comment.collector import runner as runner_module


def run(coro):
    """同步测试内执行单次协程，并加 5 秒超时双保险。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=5))


class FakeCollector:
    """契约级采集器替身：记录调用并返回真实结构的评论列表。"""

    STRATEGY_NORMAL = "normal"
    instances = []

    def __init__(self, api):
        self.api = api
        self.calls = []
        FakeCollector.instances.append(self)

    async def collect_video_comments(self, bvid, strategy=None):
        """返回 6 条评论，用于验证只打印前 5 条。"""
        self.calls.append({"bvid": bvid, "strategy": strategy})
        return [
            {"uname": f"用户{i}", "content": "x" * 60, "like": i, "ctime": f"2026-08-2{i}"}
            for i in range(6)
        ]


@pytest.fixture()
def patched_env(monkeypatch):
    """替换 Cookie 池与 API 构造，并清空替身调用记录。"""
    FakeCollector.instances = []
    sentinel_pool = object()
    monkeypatch.setattr(cookie_pool_module, "get_cookie_pool", lambda: sentinel_pool)

    class FakeAPI:
        """契约级 BilibiliAPI：只记录 cookie_pool。"""

        def __init__(self, cookie_pool=None):
            self.cookie_pool = cookie_pool

    monkeypatch.setattr(runner_module, "BilibiliAPI", FakeAPI)
    return sentinel_pool


def test_runner_exposes_coroutine_entry():
    """示例入口必须是协程函数，且模块可被正常导入。"""
    assert inspect.iscoroutinefunction(runner_module.demo_collect_comments)
    assert callable(runner_module.demo_collect_comments)


def test_runner_demo_fails_because_collector_is_not_imported(patched_env):
    """固化现状缺陷：runner 未导入 CommentCollector，示例入口调用即 NameError。"""
    with pytest.raises(NameError, match="CommentCollector"):
        run(runner_module.demo_collect_comments())


def test_runner_demo_prints_first_five_comments(patched_env, monkeypatch, capsys):
    """注入 CommentCollector 后示例可跑通，并按契约打印首 5 条评论。"""
    monkeypatch.setattr(runner_module, "CommentCollector", FakeCollector, raising=False)

    run(runner_module.demo_collect_comments())

    collector = FakeCollector.instances[0]
    assert collector.api.cookie_pool is patched_env
    assert collector.calls == [{"bvid": "BV1xx411c7XZ", "strategy": FakeCollector.STRATEGY_NORMAL}]

    out = capsys.readouterr().out
    assert "采集到 6 条评论" in out
    assert "1. 用户0" in out
    assert "5. 用户4" in out
    # 第 6 条不打印。
    assert "6. 用户5" not in out
    # 正文被截断为 50 字符展示。
    assert "x" * 50 in out
    assert "点赞: 0 | 时间: 2026-08-20" in out


def test_runner_demo_handles_empty_comment_list(patched_env, monkeypatch, capsys):
    """无评论时示例只打印 0 条，不应抛异常。"""
    class EmptyCollector(FakeCollector):
        """返回空列表的采集器替身。"""

        async def collect_video_comments(self, bvid, strategy=None):
            """空结果边界。"""
            self.calls.append({"bvid": bvid, "strategy": strategy})
            return []

    monkeypatch.setattr(runner_module, "CommentCollector", EmptyCollector, raising=False)

    run(runner_module.demo_collect_comments())

    out = capsys.readouterr().out
    assert "采集到 0 条评论" in out
