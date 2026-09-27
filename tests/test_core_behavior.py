"""核心纯逻辑行为测试，作为后续结构重构的回归基线。"""

from modules.lottery.analyzer import _clean_analysis_text, heuristic_classify
from modules.lottery.target import parse_target_input
from modules.self_diagnosis.report_generator import ReportGenerator


def test_parse_target_input_supports_video_and_dynamic_urls() -> None:
    """视频 BV 号和动态链接应解析为稳定的类型与 ID。"""
    assert parse_target_input("BV1xx411c7mD") == ("video", "BV1xx411c7mD")
    assert parse_target_input("https://t.bilibili.com/opus/123456") == ("dynamic", "123456")
    assert parse_target_input("https://www.bilibili.com/opus/987654") == ("dynamic", "987654")


def test_parse_target_input_rejects_ambiguous_or_foreign_values() -> None:
    """纯数字和非 B 站链接不能被误识别为动态。"""
    for value in ("123456", "https://example.com/opus/123456", ""):
        try:
            parse_target_input(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"输入应被拒绝: {value!r}")


def test_heuristic_classify_preserves_conservative_rules() -> None:
    """高风险特征组合应判定可疑，正常投稿账号应保持真人判定。"""
    suspicious = heuristic_classify(
        {
            "uid": 1,
            "level": 1,
            "recent_activity_count": 5,
            "lottery_repost_ratio": 0.9,
            "observable_account_days": 10,
            "video_count": 0,
        }
    )
    assert suspicious["classification"] == "suspicious"
    assert 0 <= suspicious["confidence"] <= 1
    assert suspicious["reasons"]

    normal = heuristic_classify(
        {
            "uid": 2,
            "level": 5,
            "recent_activity_count": 10,
            "lottery_repost_ratio": 0.0,
            "observable_account_days": 365,
            "video_count": 3,
        }
    )
    assert normal["classification"] == "real"


def test_clean_analysis_text_removes_technical_payloads() -> None:
    """模型返回 JSON 或技术字段时应回退为可读规则文本。"""
    result = _clean_analysis_text(
        ['{"uid": 1, "classification": "real"}'],
        ["存在公开投稿"],
    )
    assert result == "存在公开投稿"
    assert "classification" not in result
    assert len(result) <= 100


def test_markdown_report_handles_sparse_data_and_keeps_sections(tmp_path) -> None:
    """稀疏账号数据也应生成完整报告骨架，不抛出 KeyError。"""
    report = ReportGenerator(str(tmp_path)).generate_markdown_report(
        {
            "uid": 100,
            "basic_info": {},
            "fan_stats": {},
            "video_stats": {},
            "post_rhythm": {},
            "engagement_metrics": {},
            "data_availability": {},
        }
    )
    for section in ("账号概览", "投稿数据分析", "投稿节奏", "互动指标", "改进建议"):
        assert section in report
    assert "账号UID" in report
    assert "100" in report
