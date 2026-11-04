"""自诊报告矢量图表（report_charts.py）：数据规格、缺失值、封顶、单位换算、端到端可抽取文字。"""
from __future__ import annotations

import io
import logging

import pytest

pytest.importorskip("reportlab")
pypdf = pytest.importorskip("pypdf")

from fishtool_adapter import report_charts as rc  # noqa: E402
from fishtool_adapter import report_pdf as rp  # noqa: E402

log = logging.getLogger("test")
AVAIL = {"video_stats": True, "engagement_metrics": True, "tag_cloud": True}


def full_data(**over):
    data = {
        "video_stats": {"total_play": 1234567, "total_comment": 3456, "total_favorite": 7890},
        "engagement_metrics": {"play_to_fans_ratio": 42.1, "comment_to_play_ratio": 3.1, "favorite_to_play_ratio": 6.4},
        "tag_cloud": {"word_frequency": {"游戏": 12, "攻略": 8}, "video_count": 42, "tagged_video_count": 40, "tag_count": 2},
        "data_availability": dict(AVAIL),
    }
    data.update(over)
    return data


BENCH = {"has_benchmark": True, "category": "游戏", "category_metrics": {"p25": 1000, "p50": 5000, "p75": 20000},
         "self_metrics": {"avg_play": 29394}}


def kinds(specs):
    return [s.kind for s in specs]


# ---------------------------------------------------------------- 规格


def test_all_charts_present():
    specs = rc.chart_specs(full_data(), BENCH)
    assert kinds(specs) == ["volume", "engagement", "benchmark", "tag_cloud"]
    bench = specs[2]
    assert bench.title == "游戏分区播放基准" and bench.highlight == 3


def test_benchmark_disabled_hides_chart():
    # 引擎当前固定返回 has_benchmark=False（self_analyzer.benchmark_with_category）
    assert "benchmark" not in kinds(rc.chart_specs(full_data(), {"has_benchmark": False, "status": "disabled"}))
    assert "benchmark" not in kinds(rc.chart_specs(full_data(), None))


def test_availability_flags_respected():
    data = full_data(data_availability={"tag_cloud": True})
    assert kinds(rc.chart_specs(data)) == ["tag_cloud"]


def test_all_missing_returns_empty():
    data = {"video_stats": {"total_play": None}, "engagement_metrics": {}, "tag_cloud": {"word_frequency": {}},
            "data_availability": dict(AVAIL)}
    assert rc.chart_specs(data) == []
    assert rc.chart_specs(None) == []


def test_missing_values_kept_as_none_and_labelled():
    data = full_data(engagement_metrics={"play_to_fans_ratio": 150.0, "comment_to_play_ratio": None,
                                         "favorite_to_play_ratio": True})
    eng = next(s for s in rc.chart_specs(data) if s.kind == "engagement")
    assert eng.values == [150.0, None, None]
    assert eng.labels == ["粉丝触达\n150%", "评论效率\n暂无数据", "收藏效率\n暂无数据"]


def test_tag_cloud_limit_and_note():
    freq = {f"t{i}": 100 - i for i in range(60)}
    cloud = next(s for s in rc.chart_specs(full_data(tag_cloud={"word_frequency": freq, "video_count": 9,
                                                                "tagged_video_count": 8, "tag_count": 60}))
                 if s.kind == "tag_cloud")
    assert len(cloud.labels) == rc.TAG_LIMIT
    assert cloud.note == "已打标签视频 8/9 · 标签 60 个"


@pytest.mark.parametrize("value,text", [
    (None, "暂无"), (999, "999"), (12345, "1.2万"), (1234567, "123.5万"), (2.5e8, "2.5亿"), (0, "0"),
])
def test_fmt_count(value, text):
    assert rc.fmt_count(value) == text


# ---------------------------------------------------------------- 绘制


@pytest.fixture(scope="module")
def fonts():
    rp._FONT_CACHE.clear()
    return rp.resolve_fonts({}, log)


def test_radar_scale_fixed_to_100(fonts):
    spec = rc.ChartSpec("engagement", "x", ["a\n1%", "b\n2%", "c\n3%"], [1.0, 2.0, 3.0])
    chart = rc._spider_drawing(spec, 240, 150, fonts).contents[0]
    chart.normalizeData()
    assert chart._norm == 100.0  # 没有参考圈时会是 3.0，图被撑满


def test_radar_values_capped_and_none_drawn_as_zero(fonts):
    spec = rc.ChartSpec("engagement", "x", ["a", "b", "c"], [150.0, None, -5.0])
    chart = rc._spider_drawing(spec, 240, 150, fonts).contents[0]
    assert chart.data[0] == [100.0, 0.0, 0.0]


def test_build_flowables_empty_when_no_data(fonts):
    assert rc.build_chart_flowables({}, None, rp.body_width_pt(), fonts, log) == []


def test_single_chart_failure_is_skipped(fonts, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("draw failed")

    monkeypatch.setattr(rc, "_spider_drawing", boom)
    with caplog.at_level(logging.WARNING):
        out = rc.build_chart_flowables(full_data(), None, rp.body_width_pt(), fonts, log)
    assert out  # 其他图照常
    assert "engagement 绘制失败" in caplog.text


def test_end_to_end_chart_text_extractable(fonts):
    from pathlib import Path

    html = (Path(__file__).parent / "fixtures" / "self_diagnosis_report.html").read_text("utf-8")
    data = full_data(video_stats={"total_play": 1234567, "total_comment": 3456, "total_favorite": None})
    charts = rc.build_chart_flowables(data, BENCH, rp.body_width_pt(), fonts, log)
    pdf = rp.build(html, fonts, charts=charts)
    text = "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages)
    for needle in ("数据可视化", "内容互动总量", "互动效率", "粉丝触达", "游戏分区播放基准", "总体视频 Tag 词云"):
        assert needle in text, needle
    if not fonts.degraded:
        assert "123.5万" in text and "暂无" in text
    # 参考圈不带出数值标签（不用 strandLabels）
    assert "\n100\n" not in f"\n{text}\n"
    assert "已打标签视频 40/42" in text
