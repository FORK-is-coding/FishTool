"""报告「兜底结论必须有证据」与「覆盖度行不得造伪 0」契约测试（v0.2.3 收口）。

被测对象：
    ``modules/self_diagnosis/report_generator.ReportGenerator.generate_markdown_report``

两处收口：

1. 「整体表现良好」必须有证据
   四条阈值建议分属两个互相独立的评价维度——「投稿节奏」（videos_per_week /
   longest_gap_days）与「互动表现」（comment_to_play_ratio / play_to_fans_ratio）。
   只有**两个维度各自至少有 1 个明确数值**参与过阈值判断、且都没越界时，才允许
   下「整体表现良好」；任一维度全为 None（含全未知）时必须改为「指标不足」，
   禁止在没有任何证据时下良好结论。

2. 覆盖度行不得造伪 0
   coverage 条目缺 ``valid_count`` / ``missing_count`` 键时，必须渲染为「未知」，
   绝不渲染成「有效 0 / 缺失 0」；真实 0（键存在且值为 0）仍照常显示 0。

说明：本文件只补断言，不改任何生产代码；全程不触网、只写 pytest 临时目录。
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
# 一、兜底结论：证据不足不得说「良好」
# --------------------------------------------------------------------------- #
def test_all_unknown_yields_insufficient_not_good(tmp_path: Path) -> None:
    """四条指标全为 None（全未知）-> 只能「指标不足」，绝不说「整体表现良好」。"""
    report = _render(
        tmp_path,
        post_rhythm={"videos_per_week": None, "longest_gap_days": None},
        engagement={"comment_to_play_ratio": None, "play_to_fans_ratio": None},
    )

    assert "整体表现良好" not in report
    assert "指标不足" in report


def test_single_dimension_only_yields_insufficient(tmp_path: Path) -> None:
    """只有「投稿节奏」维度有有效指标、互动维度全未知 -> 仍然证据不足。"""
    report = _render(
        tmp_path,
        post_rhythm={"videos_per_week": 2, "longest_gap_days": 5},  # 均未越界
        engagement={},  # comment / touch 均缺失
    )

    # 没有触发任何阈值建议……
    assert "提升投稿频率" not in report
    assert "避免长期断更" not in report
    # ……但只有单维度有证据，不足以支撑「整体」结论。
    assert "整体表现良好" not in report
    assert "指标不足" in report


def test_both_dimensions_in_range_yields_good(tmp_path: Path) -> None:
    """两个维度各有明确数值且都没越界 -> 证据充分，才允许说「整体表现良好」。"""
    report = _render(
        tmp_path,
        post_rhythm={"videos_per_week": 2, "longest_gap_days": 5},
        engagement={"comment_to_play_ratio": 1.0, "play_to_fans_ratio": 50},
    )

    # 四条阈值均未触发……
    assert "提升投稿频率" not in report
    assert "增强互动引导" not in report
    assert "提升粉丝触达" not in report
    assert "避免长期断更" not in report
    # ……且两个维度都有效 -> 允许良好。
    assert "整体表现良好" in report
    assert "指标不足" not in report


def test_out_of_range_still_suggests_not_good(tmp_path: Path) -> None:
    """越界时照常触发建议，绝不落入「良好」（反向证明「证据」判定不是空转）。"""
    report = _render(
        tmp_path,
        post_rhythm={"videos_per_week": 0.5, "longest_gap_days": 40},
        engagement={"comment_to_play_ratio": 0.1, "play_to_fans_ratio": 5},
    )

    assert "提升投稿频率" in report
    assert "避免长期断更" in report
    assert "整体表现良好" not in report
    assert "指标不足" not in report


# --------------------------------------------------------------------------- #
# 二、覆盖度行：缺键渲染「未知」，真实 0 照常显示 0
# --------------------------------------------------------------------------- #
def test_coverage_missing_keys_render_unknown_not_zero(tmp_path: Path) -> None:
    """coverage 条目缺 valid_count / missing_count 键时渲染「未知」，不得造伪 0。"""
    report = _render(tmp_path, video_stats={
        "coverage": {
            "play": {"valid_count": 1, "missing_count": 1},
            "favorite": {},  # 两个键都缺
        },
    })

    assert "play 有效 1/缺失 1" in report
    assert "favorite 有效 未知/缺失 未知" in report
    # 缺键绝不能渲染成 0
    assert "favorite 有效 0" not in report
    assert "favorite 缺失 0" not in report


def test_coverage_partial_key_renders_unknown_for_missing_side(tmp_path: Path) -> None:
    """只缺一个键时，仅缺失的那一侧渲染「未知」，已存在的数值照常显示。"""
    report = _render(tmp_path, video_stats={
        "coverage": {"comment": {"valid_count": 3}},  # 缺 missing_count
    })

    assert "comment 有效 3/缺失 未知" in report
    assert "missing_count" not in report  # 不得把键名直接印到报告里


def test_coverage_real_zero_is_preserved(tmp_path: Path) -> None:
    """键存在且值为 0 是「真实 0」，必须照常显示 0（不得被过度纠正为未知）。"""
    report = _render(tmp_path, video_stats={
        "coverage": {"play": {"valid_count": 0, "missing_count": 0}},
    })

    assert "play 有效 0/缺失 0" in report
    assert "未知" not in report


def test_coverage_non_int_value_renders_unknown(tmp_path: Path) -> None:
    """bool / 字符串等非整数计数一律渲染「未知」，不得冒充成 0 或 1。"""
    report = _render(tmp_path, video_stats={
        "coverage": {"play": {"valid_count": True, "missing_count": "3"}},
    })

    assert "play 有效 未知/缺失 未知" in report
    assert "play 有效 1" not in report
    assert "play 缺失 3" not in report
