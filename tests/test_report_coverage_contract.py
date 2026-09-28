"""报告「指标覆盖度」行与「阈值建议跳过未知指标」契约测试（验收 #7）。

被测对象：``modules/self_diagnosis/report_generator.ReportGenerator``
``generate_markdown_report`` 在 v0.2.2 新增/强化的两处行为：

1. 指标覆盖度行
   ``video_stats.coverage`` 为非空 dict 且含合法条目时，输出
   ``- **指标覆盖度**: {指标} 有效 {n}/缺失 {m}；...`` 行；
   coverage 缺失、空 dict、条目非 dict（无有效条目）时不得输出该行。

2. 阈值建议跳过未知指标
   「改进建议」中的四条阈值判断（投稿频率 / 评论率 / 触达率 / 断更）在指标为
   ``None``（unknown）时必须整条跳过；四条全为 None（全未知）时兜底结论必须是
   「指标不足」而**不是**「整体表现良好」（v0.2.3 收口：证据不足不得说良好）。
   反过来，已知数值越界时四条建议必须照常触发，证明「跳过」断言不是空转。
   「节奏评价 / 触达评价」同属阈值判断，未知时也必须改为缺失提示而不评分。

说明：本文件只补断言，不改任何生产代码；每个测试族都写成「越界必触发 +
未知必跳过」的双向断言，避免只写单边导致假覆盖。
"""
from __future__ import annotations

from pathlib import Path

from modules.self_diagnosis.report_generator import ReportGenerator


def _render(
    tmp_path: Path,
    *,
    video_stats: dict | None = None,
    post_rhythm: dict | None = None,
    engagement: dict | None = None,
) -> str:
    """用最小账号数据渲染 Markdown 报告，全程不触网、不写仓库目录。

    Args:
        tmp_path: pytest 临时目录，作为报告输出目录，避免污染仓库 ``reports/``。
        video_stats: 投稿统计维度，缺省为空 dict。
        post_rhythm: 投稿节奏维度，缺省为空 dict。
        engagement: 互动指标维度，缺省为空 dict。

    Returns:
        生成的 Markdown 报告文本。
    """
    generator = ReportGenerator(output_dir=str(tmp_path))
    return generator.generate_markdown_report({
        "uid": 42,
        "basic_info": {"name": "测试账号", "level": 6},
        "fan_stats": {"follower": 100, "following": 3},
        "video_stats": video_stats or {},
        "post_rhythm": post_rhythm or {},
        "engagement_metrics": engagement or {},
        "data_availability": {},
    })


# --------------------------------------------------------------------------- #
# 一、指标覆盖度行
# --------------------------------------------------------------------------- #
def test_report_renders_metric_coverage_row(tmp_path: Path) -> None:
    """coverage 非空时应输出「指标覆盖度」行，并逐项渲染有效/缺失数。"""
    report = _render(tmp_path, video_stats={
        "coverage": {
            "play": {"valid_count": 1, "missing_count": 1},
            "favorite": {"valid_count": 2, "missing_count": 0},
        },
    })

    assert "- **指标覆盖度**:" in report
    assert "play 有效 1/缺失 1" in report
    assert "favorite 有效 2/缺失 0" in report


def test_report_omits_metric_coverage_row_without_effective_entries(tmp_path: Path) -> None:
    """coverage 缺失 / 空 dict / 无合法条目时不得输出「指标覆盖度」行。"""
    assert "指标覆盖度" not in _render(tmp_path, video_stats={})
    assert "指标覆盖度" not in _render(tmp_path, video_stats={"coverage": {}})
    assert "指标覆盖度" not in _render(tmp_path, video_stats={"coverage": {"play": "1/1"}})


# --------------------------------------------------------------------------- #
# 二、阈值建议跳过未知指标
# --------------------------------------------------------------------------- #
def test_threshold_suggestions_skip_unknown_metrics(tmp_path: Path) -> None:
    """四项指标均为 None（unknown）时，四条阈值建议必须整条跳过。"""
    report = _render(
        tmp_path,
        post_rhythm={"videos_per_week": None, "longest_gap_days": None},
        engagement={"comment_to_play_ratio": None, "play_to_fans_ratio": None},
    )

    assert "提升投稿频率" not in report
    assert "增强互动引导" not in report
    assert "提升粉丝触达" not in report
    assert "避免长期断更" not in report
    # 全未知时两个维度都没有效指标 -> 兜底结论必须是「指标不足」，不得说良好。
    # 见 v0.2.3 收口：兜底结论必须区分「指标正常」与「指标不可得」。
    assert "整体表现良好" not in report
    assert "指标不足" in report


def test_threshold_suggestions_fire_for_known_out_of_range_metrics(tmp_path: Path) -> None:
    """已知数值越界时四条阈值建议必须触发，反向证明「跳过」断言非空转。"""
    report = _render(
        tmp_path,
        post_rhythm={"videos_per_week": 0.5, "longest_gap_days": 40},
        engagement={"comment_to_play_ratio": 0.1, "play_to_fans_ratio": 5},
    )

    assert "提升投稿频率" in report
    assert "增强互动引导" in report
    assert "提升粉丝触达" in report
    assert "避免长期断更" in report
    assert "整体表现良好" not in report


def test_threshold_evaluations_skip_unknown_metrics(tmp_path: Path) -> None:
    """节奏/触达评价同属阈值判断：指标为 None 时不得评分，改为缺失提示。"""
    report = _render(
        tmp_path,
        post_rhythm={"videos_per_week": None},
        engagement={"play_to_fans_ratio": None},
    )

    assert "投稿频率数据缺失，暂不评分" in report
    assert "触达率数据缺失，暂不评分" in report
    # 不得出现基于阈值的分级评价
    assert "✅ **评价**" not in report
    assert "✅ **触达评价**" not in report
