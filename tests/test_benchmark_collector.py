"""01 正确排名 · 采集器契约测试（规格 §8 / §11）。

覆盖用例（派单要求）：
- wrapper：消费 03 的 ``_meta.field_status``，meta 标 missing 时不得当成真实粉丝数；
- 7 / 30 日边界：``<= newest`` 与 ``>= oldest`` 才算有效范围内；
- 10 稿选择证明：有 100 条稿件时只取最新 10 条且不再向下扫描；
- 原始 tid：``exact_raw_tid`` 必须读详情里的 raw_tid，不能凭榜单 / 列表 ID；
- 缺选中指标不替补：选中稿缺播放 -> missing_selected_metrics，且不从后面补更好稿件；
- 分页失败：列表页异常 -> 不得排名；
- 身份不符：详情 owner.mid 与目标不符 -> error。

测试策略：全部用 stub API，不触网；用 ``asyncio.run`` 驱动（仓库未装 pytest-asyncio）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from modules.self_diagnosis.benchmark.collector import (
    RankingProfileCollector,
    discover_candidates,
)
from modules.self_diagnosis.benchmark.contracts import BenchmarkPolicy

DAY_S = 86400
AS_OF = 1_700_000_000


def run(coro):
    """用独立事件循环驱动协程（仓库未安装 pytest-asyncio）。"""
    return asyncio.run(coro)


class StubAPI:
    """契约级 stub API：只实现采集器会调用的 4 个入口。"""

    def __init__(
        self,
        *,
        user_info: Optional[Dict[str, Any]] = None,
        relation: Optional[Dict[str, Any]] = None,
        pages: Optional[Dict[int, List[Dict[str, Any]]]] = None,
        views: Optional[Dict[str, Dict[str, Any]]] = None,
        page_errors: Optional[Dict[int, Exception]] = None,
        view_errors: Optional[Dict[str, Exception]] = None,
        ranking_pages: Optional[Dict[int, List[Dict[str, Any]]]] = None,
    ) -> None:
        self.user_info = user_info if user_info is not None else {'data': {'name': '账号'}, '_meta': {}}
        self.relation = relation if relation is not None else {'data': {}}
        self.pages = pages or {}
        self.views = views or {}
        self.page_errors = page_errors or {}
        self.view_errors = view_errors or {}
        self.ranking_pages = ranking_pages or {}
        self.calls: List[tuple] = []

    async def get_user_info(self, uid: int) -> Dict[str, Any]:
        """返回预置用户资料。"""
        self.calls.append(('user_info', uid))
        return self.user_info

    async def get_user_relation_stat(self, uid: int) -> Dict[str, Any]:
        """返回预置关系统计。"""
        self.calls.append(('relation', uid))
        return self.relation

    async def get_user_videos(self, uid: int, page: int = 1, page_size: int = 30) -> Dict[str, Any]:
        """返回预置投稿列表页。"""
        self.calls.append(('videos', uid, page))
        if page in self.page_errors:
            raise self.page_errors[page]
        vlist = list(self.pages.get(page, []))
        return {'data': {'list': {'vlist': vlist}, 'page': {'pn': page, 'ps': page_size, 'count': len(vlist)}}}

    async def get(self, url: str, params: Optional[Dict[str, Any]] = None, need_sign: bool = False, **kwargs):
        """返回预置详情（已解包 data，无第二层 data）。"""
        bvid = (params or {}).get('bvid')
        self.calls.append(('view', bvid))
        if bvid in self.view_errors:
            raise self.view_errors[bvid]
        return self.views.get(bvid)

    async def get_ranking(self, rid: int, day: int = 7, original: int = 0, page: int = 1):
        """返回预置榜单页。"""
        self.calls.append(('ranking', rid, page))
        items = list(self.ranking_pages.get(page, []))
        return {'data': {'list': items}}


def entry(bvid: str, created: int, tid: int = 4, play: int = 0) -> Dict[str, Any]:
    """构造一条投稿列表条目。"""
    return {'bvid': bvid, 'created': created, 'tid': tid, 'play': play}


def view(
    bvid: str,
    uid: int,
    pubdate: int,
    tid: int = 4,
    view_count: Optional[int] = 1000,
) -> Dict[str, Any]:
    """构造一条详情响应（stat.view 可缺省以模拟 missing）。"""
    stat: Dict[str, Any] = {}
    if view_count is not None:
        stat['view'] = view_count
    return {'bvid': bvid, 'pubdate': pubdate, 'tid': tid, 'owner': {'mid': uid}, 'stat': stat}


def policy(**overrides) -> BenchmarkPolicy:
    """构造测试用策略。"""
    base = {'min_videos': 3}
    base.update(overrides)
    return BenchmarkPolicy(**base)


# --------------------------------------------------------------------------- #
# wrapper / 粉丝来源
# --------------------------------------------------------------------------- #
def test_meta_missing_follower_is_not_treated_as_real_zero() -> None:
    """03 meta 标 follower=missing 时，兼容 data 的 0 不得当成真实粉丝数。"""
    api = StubAPI(
        user_info={'data': {'name': 'A', 'follower': 0}, '_meta': {'source': 'public_card', 'field_status': {'follower': 'missing'}}},
        relation={'data': {}},
        pages={1: [entry('BV1', AS_OF - 10 * DAY_S)]},
        views={'BV1': view('BV1', 1, AS_OF - 10 * DAY_S)},
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.follower_count is None
    assert sample.follower_status == 'missing'


def test_relation_follower_is_preferred() -> None:
    """排名粉丝优先取 relation 接口明确返回字段。"""
    api = StubAPI(
        user_info={'data': {'name': 'A', 'follower': 5}, '_meta': {'field_status': {'follower': 'ok'}}},
        relation={'data': {'follower': 12345}},
        pages={1: [entry('BV1', AS_OF - 10 * DAY_S)]},
        views={'BV1': view('BV1', 1, AS_OF - 10 * DAY_S)},
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.follower_count == 12345
    assert sample.follower_status == 'ok'


def test_meta_ok_real_zero_is_accepted() -> None:
    """meta 明确 ok 且值为 0（真实 0）时，必须接受为 0 而不是 missing。"""
    api = StubAPI(
        user_info={'data': {'name': 'A', 'follower': 0}, '_meta': {'field_status': {'follower': 'ok'}}},
        relation={'data': {}},
        pages={1: [entry('BV1', AS_OF - 10 * DAY_S)]},
        views={'BV1': view('BV1', 1, AS_OF - 10 * DAY_S)},
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.follower_count == 0
    assert sample.follower_status == 'ok'


# --------------------------------------------------------------------------- #
# 7 / 30 日边界
# --------------------------------------------------------------------------- #
def test_age_window_boundaries_inclusive() -> None:
    """``created == as_of-7d`` 与 ``created == as_of-30d`` 都算在窗口内；越界 1 秒排除。"""
    newest = AS_OF - 7 * DAY_S
    oldest = AS_OF - 30 * DAY_S
    api = StubAPI(
        pages={1: [
            entry('BV_NEW', newest + 1),        # 太新 1 秒
            entry('BV_A', newest),              # 边界内
            entry('BV_B', oldest),              # 边界内
            entry('BV_OLD', oldest - 1),        # 太旧 1 秒
        ]},
        views={
            'BV_A': view('BV_A', 1, newest),
            'BV_B': view('BV_B', 1, oldest),
        },
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(min_videos=2), AS_OF))
    picked = [item['bvid'] for item in sample.selected_videos]
    assert picked == ['BV_A', 'BV_B']
    assert sample.fetch_complete is True
    assert sample.status == 'valid'
    # 太新的稿件连详情都不该请求（列表层即可判定）
    assert ('view', 'BV_NEW') not in api.calls
    # 太旧的稿件只用于证明旧边界，不入参评
    assert ('view', 'BV_OLD') not in api.calls


def test_in_window_but_too_few_becomes_insufficient_posts() -> None:
    """窗口内完整扫描但不足 min_videos -> insufficient_posts（不排名）。"""
    api = StubAPI(
        pages={1: [entry('BV1', AS_OF - 10 * DAY_S), entry('BV2', AS_OF - 11 * DAY_S)]},
        views={
            'BV1': view('BV1', 1, AS_OF - 10 * DAY_S),
            'BV2': view('BV2', 1, AS_OF - 11 * DAY_S),
        },
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(min_videos=3), AS_OF))
    assert sample.status == 'insufficient_posts'
    assert sample.score_twice is None
    assert sample.fetch_complete is True


# --------------------------------------------------------------------------- #
# 10 稿选择证明
# --------------------------------------------------------------------------- #
def test_selects_newest_ten_and_stops_scanning() -> None:
    """100 条稿件中只取最新 10 条，并在取满后停止继续扫描。"""
    entries = [entry(f'BV{i:03d}', AS_OF - (10 + i) * DAY_S) for i in range(30)]
    api = StubAPI(
        pages={1: entries, 2: entries, 3: entries, 4: entries},
        views={f'BV{i:03d}': view(f'BV{i:03d}', 1, AS_OF - (10 + i) * DAY_S, view_count=1000 + i) for i in range(30)},
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.status == 'valid'
    assert sample.selected_count == 10
    assert [item['bvid'] for item in sample.selected_videos] == [f'BV{i:03d}' for i in range(10)]
    # 取满即停：只扫了第 1 页
    pages_scanned = {call[2] for call in api.calls if call[0] == 'videos'}
    assert pages_scanned == {1}


def test_three_videos_author_is_still_one_vote() -> None:
    """3 条稿件（刚好达到 min_videos）也能形成一条有效样本，不按条数加权。"""
    api = StubAPI(
        pages={1: [
            entry('BV1', AS_OF - 8 * DAY_S),
            entry('BV2', AS_OF - 9 * DAY_S),
            entry('BV3', AS_OF - 10 * DAY_S),
        ]},
        views={
            'BV1': view('BV1', 1, AS_OF - 8 * DAY_S, view_count=300),
            'BV2': view('BV2', 1, AS_OF - 9 * DAY_S, view_count=100),
            'BV3': view('BV3', 1, AS_OF - 10 * DAY_S, view_count=200),
        },
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.status == 'valid'
    assert sample.selected_count == 3
    assert sample.metric_value == 200.0  # 中位 200，不是平均 200 之外的任何加权


def test_pagination_exhausted_proves_completeness() -> None:
    """列表遍历结束（下一页为空）也算证明完整。"""
    api = StubAPI(
        pages={1: [entry('BV1', AS_OF - 8 * DAY_S), entry('BV2', AS_OF - 9 * DAY_S), entry('BV3', AS_OF - 10 * DAY_S)]},
        views={
            'BV1': view('BV1', 1, AS_OF - 8 * DAY_S),
            'BV2': view('BV2', 1, AS_OF - 9 * DAY_S),
            'BV3': view('BV3', 1, AS_OF - 10 * DAY_S),
        },
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.fetch_complete is True
    assert sample.stop_reason == 'exhausted'
    assert sample.status == 'valid'


# --------------------------------------------------------------------------- #
# 原始 tid
# --------------------------------------------------------------------------- #
def test_exact_raw_tid_uses_detail_tid_only() -> None:
    """exact_raw_tid 必须读详情 raw_tid；列表 tid 与详情冲突时以详情为准。"""
    entries = [
        entry('BV1', AS_OF - 8 * DAY_S, tid=4),    # 列表说 4，详情说 21 -> 排除
        entry('BV2', AS_OF - 9 * DAY_S, tid=21),   # 列表说 21，详情说 4 -> 入选
        entry('BV3', AS_OF - 10 * DAY_S, tid=21),  # 详情 4 -> 入选
    ]
    api = StubAPI(
        pages={1: entries},
        views={
            'BV1': view('BV1', 1, AS_OF - 8 * DAY_S, tid=21, view_count=999999),
            'BV2': view('BV2', 1, AS_OF - 9 * DAY_S, tid=4, view_count=100),
            'BV3': view('BV3', 1, AS_OF - 10 * DAY_S, tid=4, view_count=200),
        },
    )
    sample = run(RankingProfileCollector(api).collect_creator(
        1, policy(content_scope='exact_raw_tid', raw_tid=4, min_videos=2), AS_OF
    ))
    picked = [item['bvid'] for item in sample.selected_videos]
    assert picked == ['BV2', 'BV3']
    assert all(item['raw_tid'] == 4 for item in sample.selected_videos)


# --------------------------------------------------------------------------- #
# 缺选中指标不替补
# --------------------------------------------------------------------------- #
def test_missing_selected_metric_does_not_substitute() -> None:
    """选中稿缺播放 -> missing_selected_metrics；不从后面补更老的可用稿。"""
    api = StubAPI(
        pages={1: [
            entry('BV1', AS_OF - 8 * DAY_S),
            entry('BV2', AS_OF - 9 * DAY_S),
            entry('BV3', AS_OF - 10 * DAY_S),
            entry('BV4', AS_OF - 11 * DAY_S),     # 缺播放
            entry('BV_OLD', AS_OF - 40 * DAY_S),  # 窗口外，绝不能被用来替补
        ]},
        views={
            'BV1': view('BV1', 1, AS_OF - 8 * DAY_S, view_count=100),
            'BV2': view('BV2', 1, AS_OF - 9 * DAY_S, view_count=200),
            'BV3': view('BV3', 1, AS_OF - 10 * DAY_S, view_count=300),
            'BV4': view('BV4', 1, AS_OF - 11 * DAY_S, view_count=None),
        },
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.status == 'missing_selected_metrics'
    assert sample.score_twice is None
    picked = [item['bvid'] for item in sample.selected_videos]
    assert 'BV4' in picked  # 明细保留，不假装没选它
    assert 'BV_OLD' not in picked
    assert ('view', 'BV_OLD') not in api.calls


def test_real_zero_view_is_valid_not_missing() -> None:
    """真实 0 播放有效；legacy 数据库 0 不参与（本采集器从不读 DB）。"""
    api = StubAPI(
        pages={1: [
            entry('BV1', AS_OF - 8 * DAY_S),
            entry('BV2', AS_OF - 9 * DAY_S),
            entry('BV3', AS_OF - 10 * DAY_S),
        ]},
        views={
            'BV1': view('BV1', 1, AS_OF - 8 * DAY_S, view_count=0),
            'BV2': view('BV2', 1, AS_OF - 9 * DAY_S, view_count=0),
            'BV3': view('BV3', 1, AS_OF - 10 * DAY_S, view_count=0),
        },
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.status == 'valid'
    assert sample.metric_value == 0.0
    assert all(item['view_status'] == 'ok' for item in sample.selected_videos)


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #
def test_page_failure_is_not_ranked() -> None:
    """列表页失败 -> 不能证明覆盖 -> error / incomplete，且无成绩。"""
    api = StubAPI(
        pages={1: [entry('BV1', AS_OF - 8 * DAY_S)]},
        page_errors={1: RuntimeError('network down')},
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.status == 'error'
    assert sample.fetch_complete is False
    assert sample.score_twice is None


def test_duplicate_page_is_not_ranked() -> None:
    """重复页 = 假覆盖：不得排名。"""
    same = [entry('BV1', AS_OF - 8 * DAY_S)]
    api = StubAPI(
        pages={1: same, 2: same, 3: same},
        views={'BV1': view('BV1', 1, AS_OF - 8 * DAY_S)},
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.fetch_complete is False
    assert sample.stop_reason == 'duplicate_page'
    assert sample.status == 'incomplete_selection'


def test_detail_failure_leaves_selection_incomplete() -> None:
    """详情请求失败 -> 无法证明选稿完整 -> incomplete_selection。"""
    api = StubAPI(
        pages={1: [entry('BV1', AS_OF - 8 * DAY_S)]},
        view_errors={'BV1': RuntimeError('boom')},
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.fetch_complete is False
    assert sample.status == 'incomplete_selection'


def test_owner_mismatch_is_error() -> None:
    """详情 owner.mid 与目标 UID 不符 -> error（不能算成该账号成绩）。"""
    api = StubAPI(
        pages={1: [entry('BV1', AS_OF - 8 * DAY_S)]},
        views={'BV1': view('BV1', 999, AS_OF - 8 * DAY_S)},
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.status == 'error'
    assert sample.stop_reason == 'owner_mismatch'


def test_bvid_mismatch_is_rejected() -> None:
    """详情返回的 bvid 与请求不符 -> 拒绝该详情。"""
    api = StubAPI(
        pages={1: [entry('BV1', AS_OF - 8 * DAY_S)]},
        views={'BV1': view('BV_OTHER', 1, AS_OF - 8 * DAY_S)},
    )
    sample = run(RankingProfileCollector(api).collect_creator(1, policy(), AS_OF))
    assert sample.status == 'incomplete_selection'
    assert sample.selected_count == 0


# --------------------------------------------------------------------------- #
# 候选发现
# --------------------------------------------------------------------------- #
def test_discover_candidates_dedups_authors_and_reports_evidence() -> None:
    """候选发现只从 owner.mid 提取唯一作者，并记录端点 / 截断 / 重复页证据。"""
    page1 = [
        {'bvid': 'BV1', 'owner': {'mid': 10, 'name': '甲'}},
        {'bvid': 'BV2', 'owner': {'mid': 10, 'name': '甲'}},
        {'bvid': 'BV3', 'owner': {'mid': 11, 'name': '乙'}},
    ]
    page2 = [
        {'bvid': 'BV4', 'owner': {'mid': 12, 'name': '丙'}},
        {'bvid': 'BV5', 'owner': {'mid': 11, 'name': '乙'}},
    ]
    api = StubAPI(ranking_pages={1: page1, 2: page2})
    result = run(discover_candidates(api, {'taxonomy': 'pid_v2', 'rid': 4, 'day': 7}, limit=10))
    assert [c['uid'] for c in result['candidates']] == [10, 11, 12]
    assert result['endpoint'] == '/x/web-interface/ranking/v2'
    assert result['source_changed'] is False
    assert 'discovered_s' in result


def test_discover_candidates_stops_on_duplicate_page() -> None:
    """重复页检测：服务端未真正分页时必须标记截断而不是假装覆盖。"""
    same = [{'bvid': 'BV1', 'owner': {'mid': 10}}]
    api = StubAPI(ranking_pages={1: same, 2: same, 3: same})
    result = run(discover_candidates(api, {'rid': 4}, limit=10))
    assert result['truncated'] is True
    assert any(f.get('message') == 'duplicate_page' for f in result['failures'])


def test_discover_candidates_flags_source_changed_on_bad_structure() -> None:
    """结构异常时显式返回 source_changed，不静默把降级来源叫 ranking。"""
    class BadAPI(StubAPI):
        async def get_ranking(self, rid, day=7, original=0, page=1):
            return {'data': {'newlist': []}}

    result = run(discover_candidates(BadAPI(), {'rid': 4}, limit=10))
    assert result['source_changed'] is True
    assert result['candidates'] == []


def test_discover_candidates_respects_author_limit() -> None:
    """遍历到作者上限即停止并标记截断。"""
    page1 = [{'bvid': f'BV{i}', 'owner': {'mid': i}} for i in range(5)]
    api = StubAPI(ranking_pages={1: page1, 2: page1})
    result = run(discover_candidates(api, {'rid': 4}, limit=3))
    assert len(result['candidates']) == 3
    assert result['truncated'] is True
