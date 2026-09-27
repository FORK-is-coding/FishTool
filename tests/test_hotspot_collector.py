"""热点采集链路回归测试：榜单 -> 快照 -> 信号落库 -> 进度状态流转。

覆盖本次新增的 P0 采集链路：
- 完整链路成功路径（含热评/弹幕采样接线，复用 CommentCollector + SentimentAnalyzer）
- 单视频失败不中断整轮
- 空榜单边界
- 进度状态流转与风控预算计数
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modules.hotspot.collector import HotspotCollector, get_progress


class MockBudget:
    """不等待的预算器，只记录 acquire 次数。"""

    def __init__(self):
        self.calls = 0

    async def acquire(self, *args, **kwargs):
        self.calls += 1


class MockStore:
    """内存信号存储，记录 save_many 写入。"""

    def __init__(self):
        self.records = []

    def save_many(self, rows):
        self.records.extend(rows)


class FakeResp:
    """支持 async context manager 的 HTTP 响应替身，模拟弹幕 XML。"""

    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return '<d p="1,1,25,16777215,0,0,0">前方高能</d><d p="2,1,25,16777215,0,0,0">泪目</d>'


def make_api():
    """构造热点采集器所需的 API 替身。

    覆盖：榜单、视频详情、弹幕 XML；评论采集器独立 mock。
    """
    now = int(time.time())
    api = MagicMock()
    api.BASE_URL = "https://api.bilibili.com"
    api.get_ranking = AsyncMock(return_value={
        "data": {"list": [
            {"bvid": "BV1", "aid": 101, "pubdate": now},
            {"bvid": "BV2", "aid": 102, "pubdate": now},
        ]}
    })
    api.get = AsyncMock(return_value={
        "bvid": "BV1",
        "aid": 101,
        "title": "测试视频",
        "desc": "描述",
        "duration": 120,
        "tid": 4,
        "tname": "游戏",
        "owner": {"mid": 1001, "name": "UP主"},
        "stat": {"view": 1000, "danmaku": 10, "reply": 5, "favorite": 2, "coin": 3, "share": 1, "like": 8},
    })
    api.init_session = AsyncMock()
    api.session = SimpleNamespace(get=MagicMock(return_value=FakeResp()))
    return api


def build_collector(api=None):
    """构造带替身依赖的采集器，并 mock 掉写库的 _save_snapshot。"""
    api = api or make_api()
    budget = MockBudget()
    store = MockStore()
    collector = HotspotCollector(api=api, budget=budget, store=store)
    # 快照写库走真实 Video/VideoStats 表会污染全局库，统一替换为计数替身。
    collector._save_snapshot = AsyncMock(return_value=1)
    # 评论采样复用 CommentCollector，这里替换其采集入口，只验证关键词提取接线。
    collector.comment_collector.collect_video_comments = AsyncMock(return_value=[
        {"rpid": 1, "content": "这个视频真的好看"},
        {"rpid": 2, "content": "真的好看，爱了"},
    ])
    return collector, budget, store


def test_collect_runs_full_pipeline_and_saves_signals():
    """完整链路应采集 2 个视频，落 title 信号 2 条，并采样评论/弹幕关键词。"""
    collector, budget, store = build_collector()

    result = asyncio.run(collector.collect(tid=4))

    assert result["total"] == 2
    assert result["ok"] == 2
    assert result["failed"] == 0
    # 2 条 mock 快照信号 + comment 信号 + danmaku 信号
    assert result["signals"] >= 4
    sources = {row["source"] for row in store.records}
    # title 信号在 _save_snapshot 内部落库，此处被 mock 替换，只验证采样信号接线。
    assert {"comment", "danmaku"} <= sources
    # 榜单 1 次 + 视频详情 2 次 + 评论采样 2 次 + 弹幕采样 2 次 = 7
    assert budget.calls == 7
    snapshot_call = collector._save_snapshot.await_args_list[0]
    assert snapshot_call.args[0]["bvid"] == "BV1"
    assert snapshot_call.kwargs["source"] == "ranking"
    assert snapshot_call.kwargs["collection_tid"] == 4
    # run_id 为本轮采集批次标识，同轮所有视频共用，格式 <时间戳>_<来源>_<分区>
    assert snapshot_call.kwargs["run_id"].endswith("_ranking_4")
    comment_payload = next(row["value"] for row in store.records if row["source"] == "comment")
    assert "好看" in comment_payload["keywords"]
    # 弹幕关键字必须非空兜底：session.get 若被 AsyncMock 包成协程，async with 抛错会被
    # except 静默吞掉，此时 danmaku 信号仍会落库但 keywords 为空，只断言信号存在测不出来。
    danmaku_payload = next(row["value"] for row in store.records if row["source"] == "danmaku")
    assert "前方高能" in danmaku_payload["keywords"]


def test_collect_keeps_going_when_single_video_fails():
    """单个视频详情失败不应中断整轮采集。"""
    api = make_api()
    api.get = AsyncMock(side_effect=[RuntimeError("接口异常"), {
        "bvid": "BV2",
        "aid": 102,
        "title": "第二个视频",
        "tid": 4,
        "owner": {"mid": 1002, "name": "UP2"},
        "stat": {"view": 500},
    }])
    collector, budget, store = build_collector(api)

    result = asyncio.run(collector.collect(tid=4, sample_comments=False, sample_danmaku=False))

    assert result["total"] == 2
    assert result["ok"] == 1
    assert result["failed"] == 1
    assert result["signals"] >= 1
    assert get_progress()["status"] == "completed"


def test_collect_empty_ranking_short_circuits():
    """榜单为空时应直接完成，不发起任何视频请求。"""
    api = make_api()
    api.get_ranking = AsyncMock(return_value={"data": {"list": []}})
    collector, budget, store = build_collector(api)

    result = asyncio.run(collector.collect(tid=4))

    assert result["total"] == 0
    assert result["ok"] == 0
    assert budget.calls == 1  # 只消耗榜单请求
    assert get_progress()["status"] == "completed"
    assert get_progress()["progress"] == 100


def test_collect_progress_transitions_through_states():
    """进度应从 running 走到 completed，且前端可读取快照。"""
    collector, budget, store = build_collector()

    asyncio.run(collector.collect(tid=4, limit=2))

    progress = get_progress()
    assert progress["status"] == "completed"
    assert progress["progress"] == 100
    assert progress["updated_at"] is not None
    assert "采集完成" in progress["message"]
