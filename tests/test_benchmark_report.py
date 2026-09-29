"""01 正确排名 · 报告章节契约测试（规格 §10.5 / §11）。

覆盖用例：
- 排名真实显示：真名次、并列范围、参照样本百分位标注、peer 来源、指标口径；
- partial 提示：标题必须写「已成功取得的 X 个参评账号」，不声称请求的所有账号均已比较；
- 无虚假全区 / 全站文案：只说「不代表全站排名」，不编造全区 / 全站名次；
- rank=null 显示「无法计算」而不是 0；
- keyword-only 参数不破坏旧位置参数调用；非 schema=3 结果不渲染。

测试策略：只渲染 Markdown，不触网、只写 pytest 临时目录。
"""
from __future__ import annotations

from pathlib import Path

from modules.self_diagnosis.report_generator import ReportGenerator

SELF_DATA = {
    "uid": 42,
    "basic_info": {"name": "测试账号", "level": 6},
    "fan_stats": {"follower": 100, "following": 3},
    "video_stats": {},
    "post_rhythm": {},
    "engagement_metrics": {},
    "data_availability": {},
}


def _render(tmp_path: Path, creator_ranking) -> str:
    """渲染带冻结排名的 Markdown 报告（临时目录，不污染仓库 reports/）。"""
    generator = ReportGenerator(output_dir=str(tmp_path))
    return generator.generate_markdown_report(SELF_DATA, None, creator_ranking=creator_ranking)


def _result(*, rank=4, rank_end=5, total=6, percentile=30, state='complete',
            valid=5, requested=5, excluded=None, target=None, warnings=None) -> dict:
    """构造冻结排名结果（schema=3 / unit=creator）。"""
    target_card = target if target is not None else {
        'uid': 42, 'metric_value': 200.0, 'score_twice': 400,
        'rank': rank, 'rank_end': rank_end, 'total': total,
        'reference_count': valid, 'percentile': percentile,
        'percentile_method': 'peer_midrank_excluding_target_v1',
        'selected_count': 10, 'status': 'valid', 'is_target': True,
    }
    leaderboard = [target_card]
    for index, uid in enumerate(range(100, 100 + valid), start=1):
        leaderboard.append({'uid': uid, 'metric_value': 100.0 * index, 'score_twice': 200 * index,
                            'rank': index, 'rank_end': index, 'total': total,
                            'selected_count': 10, 'is_target': False})
    return {
        'schema_version': 3,
        'run_id': 'run-abc',
        'target_uid': 42,
        'policy': {'peer_source': 'manual_peer_set', 'window_days': 30,
                   'minimum_age_days': 7, 'max_videos': 10},
        'selection_as_of_s': 1_700_000_000,
        'comparison_state': state,
        'unit': 'creator',
        'metric': 'median_cumulative_views',
        'requested_peer_count': requested,
        'valid_peer_count': valid,
        'ranked_count': valid + 1,
        'reference_count': valid,
        'excluded_peers': excluded or [],
        'target': target_card,
        'reference_distribution': {'p25': 150.0, 'p50': 250.0, 'p75': 400.0, 'count': valid},
        'leaderboard': leaderboard,
        'warnings': warnings or [],
        'snapshot_hash': 'deadbeef',
    }


# --------------------------------------------------------------------------- #
# 真实显示
# --------------------------------------------------------------------------- #
def test_ranking_shows_real_rank_and_percentile(tmp_path) -> None:
    """真实名次 / 并列范围 / 百分位标注 / peer 来源 / 指标口径都要显示。"""
    report = _render(tmp_path, _result())

    assert '第 4–5 名 / 共 6 个参评账号' in report
    assert '参照样本百分位，越高越靠前' in report
    assert '30%' in report
    assert '手动指定同行名单' in report
    assert '中位累计播放' in report
    assert '一个账号一票' in report
    # 报告中允许出现真实新名次，不永久禁用「排名」二字
    assert '同行排名' in report


def test_ranking_marks_peer_source_when_discovered(tmp_path) -> None:
    """来源为榜单发现时，标签必须是「热门作品作者参评集合」。"""
    result = _result()
    result['policy'] = dict(result['policy'], peer_source='ranking_discovered_peer_set')
    report = _render(tmp_path, result)
    assert '热门作品作者参评集合' in report


def test_partial_mentions_successful_subset(tmp_path) -> None:
    """partial：标题写「已成功取得的 X 个参评账号」，并列出请求/有效/排除。"""
    excluded = [{'uid': 201, 'reason': 'error'}, {'uid': 202, 'reason': 'insufficient_posts'}]
    report = _render(tmp_path, _result(state='partial', valid=7, requested=10, excluded=excluded))

    assert '已成功取得的 7 个参评账号' in report
    assert '部分比较' in report
    assert '请求 10 个，仅 7 个成功参评' in report
    assert '排除账号' in report and '2 个' in report
    assert 'UID 201' in report


def test_no_false_whole_site_claim(tmp_path) -> None:
    """不得出现虚假全区 / 全站排名文案，只允许「不代表全站排名」的免责声明。"""
    report = _render(tmp_path, _result())

    assert '不代表全站排名' in report
    for forbidden in ('全区排名第', '全站第', '超过全站', '全站名次第', '分区排名第'):
        assert forbidden not in report


# --------------------------------------------------------------------------- #
# rank=null 必须显示「无法计算」
# --------------------------------------------------------------------------- #
def test_null_rank_shows_unavailable_not_zero(tmp_path) -> None:
    """目标 rank=null -> 显示「无法计算」，绝不显示 0。"""
    target = {'uid': 42, 'metric_value': None, 'score_twice': None,
              'rank': None, 'rank_end': None, 'total': 3, 'reference_count': 3,
              'percentile': None, 'percentile_method': None,
              'selected_count': 2, 'status': 'insufficient_posts', 'is_target': True}
    report = _render(tmp_path, _result(state='target_unavailable', rank=None, target=target,
                                       excluded=[], valid=3, requested=3))

    assert '无法计算' in report
    assert '第 0' not in report
    assert '0 名' not in report
    # 原因要写清楚，而不是空着
    assert '窗口内有效稿件不足最少条数' in report


def test_null_metric_in_leaderboard_shows_unavailable(tmp_path) -> None:
    """榜单行 metric=null 也必须显示「无法计算」，不显示 0。"""
    result = _result()
    result['leaderboard'] = [{'uid': 999, 'metric_value': None, 'score_twice': None,
                              'rank': None, 'rank_end': None, 'total': 1,
                              'selected_count': 0, 'is_target': False}]
    report = _render(tmp_path, result)
    assert '无法计算' in report
    assert '| 999 | 0 ' not in report


def test_warning_span_exceeded_is_rendered(tmp_path) -> None:
    """观测跨度超限等警告必须出现在报告里。"""
    report = _render(tmp_path, _result(
        rank=None,
        target={'uid': 42, 'metric_value': None, 'score_twice': None, 'rank': None, 'rank_end': None,
                'total': 6, 'reference_count': 5, 'percentile': None, 'percentile_method': None,
                'selected_count': 10, 'status': 'valid', 'is_target': True},
        warnings=[{'code': 'observation_span_exceeded', 'message': '采集跨度超过 2 小时，该组不排名；仅展示账号事实'}],
    ))
    assert '采集跨度超过 2 小时' in report


# --------------------------------------------------------------------------- #
# 兼容性
# --------------------------------------------------------------------------- #
def test_keyword_only_param_keeps_old_positional_call(tmp_path) -> None:
    """不传 creator_ranking 时旧调用行为不变；传 None 也不报错。"""
    generator = ReportGenerator(output_dir=str(tmp_path))
    first = generator.generate_markdown_report(SELF_DATA, None)
    second = generator.generate_markdown_report(SELF_DATA, None, creator_ranking=None)
    assert '同行排名' not in first
    assert first == second


def test_non_schema3_result_is_ignored(tmp_path) -> None:
    """非 schema=3 / 非 creator 单位的结果不渲染，避免错误排名复活。"""
    legacy = _result()
    legacy['schema_version'] = 2
    assert '同行排名' not in _render(tmp_path, legacy)

    not_creator = _result()
    not_creator['unit'] = 'video'
    assert '同行排名' not in _render(tmp_path, not_creator)


def test_save_markdown_report_accepts_creator_ranking(tmp_path) -> None:
    """save_markdown_report 的 keyword-only 参数可用，文件确实落盘。"""
    generator = ReportGenerator(output_dir=str(tmp_path))
    path = generator.save_markdown_report(SELF_DATA, None, 'rank_report.md',
                                          creator_ranking=_result())
    content = Path(path).read_text(encoding='utf-8')
    assert '同行排名' in content
    assert Path(path).name == 'rank_report.md'
