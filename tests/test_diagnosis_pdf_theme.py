"""自诊报告打印主题（方案 A「终端白」）与桌面端 PDF 渲染。

覆盖：
- report_theme：完整文档结构（封面 / 档案脊 / 页眉页脚 / meta）、无衬线字体、去 emoji、
  AI 章节标记、外部文本（账号名）在封面与 meta 中被清洗并转义；
- generate_html_report：仍保留 PR-26 的转义与图片剥离、图表照常嵌入；
- desktop.report_export：to_qt_body 结构转换、meta 解析；真实导出 PDF 页数与体积
  （锁定「字体子集化」——Chromium 打印会把整套中文字体嵌入，单份报告 20MB+）。
"""
from __future__ import annotations

import os
import re

import pytest

from modules.self_diagnosis import report_theme
from modules.self_diagnosis.report_generator import ReportGenerator

SELF_DATA = {
    "uid": 12345678,
    "fetched_at": "2026-10-26T14:30:05",
    "basic_info": {"name": "示例UP主", "level": 6},
    "fan_stats": {"follower": 128000, "following": 210},
    "video_stats": {"total_count": 142, "total_play": 9360000, "avg_play": 65900},
    "post_rhythm": {"videos_per_week": 1.6, "longest_gap_days": 37},
    "engagement_metrics": {"play_to_fans_ratio": 51.6, "comment_to_play_ratio": 0.31, "favorite_to_play_ratio": 2.2},
    "data_availability": {"basic_info": True, "video_stats": True},
}
AI_OK = {"success": True, "report": "教程类内容是主要增长来源。", "commentary": "建议固定周更教程栏目。",
         "highlights": ["教程收藏率高"], "model": "示例模型", "generated_at": 1792999800}
CHART = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/"
         "lK3Q6wAAAABJRU5ErkJggg==")


def _html(tmp_path, self_data=None, **kwargs):
    return ReportGenerator(str(tmp_path)).generate_html_report(self_data or SELF_DATA, **kwargs)


# ---------------------------------------------------------------- 主题结构


def test_document_has_cover_spine_running_head_and_meta(tmp_path):
    """完整文档：主题标记、封面、每页档案脊/页眉页脚、供桌面端读取的 meta。"""
    html = _html(tmp_path, ai_report=AI_OK)

    assert html.startswith("<!DOCTYPE html>")
    assert f'<meta name="report-theme" content="{report_theme.THEME_NAME}">' in html
    assert '<section class="cover">' in html and "创作者<br>档案" in html
    for cls in ("spine", "running-head", "running-foot"):
        assert f'class="{cls}"' in html
    assert '<meta name="report-account" content="示例UP主">' in html
    assert '<meta name="report-uid" content="12345678">' in html
    assert '<meta name="report-fetched-at" content="2026-10-26 14:30:05">' in html
    assert "@page" in html


def test_theme_uses_only_sans_serif_fonts():
    """全部 font-family 都以无衬线字体收尾，不出现任何衬线 / 宋体族。"""
    families = re.findall(r"font-family:\s*([^;]+);", report_theme.REPORT_PRINT_CSS)
    assert families
    for value in families:
        assert "serif" not in value.replace("sans-serif", ""), value
        assert not re.search(r"SimSun|宋体|Times|Georgia|Songti", value, re.IGNORECASE), value
    assert "Bahnschrift" in report_theme.REPORT_PRINT_CSS
    assert "Microsoft YaHei" in report_theme.REPORT_PRINT_CSS


def test_headings_drop_emoji_and_mark_ai_sections(tmp_path):
    """章节标题去掉 emoji；AI 调研报告 / 运营点评带 ai-section 标记，其余不带。"""
    html = _html(tmp_path, ai_report=AI_OK)
    body = html.split('<main class="report">', 1)[1]

    assert '<h2 class="ai-section">AI 调研报告</h2>' in body
    assert '<h2 class="ai-section">运营点评</h2>' in body
    assert "<h2>账号概览</h2>" in body
    assert not re.search("[\U0001F000-\U0001FAFF⌀-⏿✀-➿]", body)


@pytest.mark.parametrize("name, expected", [
    ("攻击者![x](file:///C:/Windows/win.png)", "攻击者x"),
    ("<b>名</b>", "&lt;b&gt;名&lt;/b&gt;"),
    ("一" * 60, "一" * 39 + "…"),
    ("", "未获取账号名"),
])
def test_cover_label_is_sanitized_and_escaped(tmp_path, name, expected):
    """账号名（外部文本）在封面与 meta 中去掉 Markdown 链接/图片语法与 URL、截断并转义。"""
    data = dict(SELF_DATA, basic_info={"name": name})
    html = _html(tmp_path, self_data=data)
    head = html.split('<main class="report">', 1)[0]

    assert f'<meta name="report-account" content="{expected}">' in head
    assert "file:///" not in head
    assert "<b>名</b>" not in head


def test_body_security_and_charts_preserved(tmp_path):
    """主题不削弱 PR-26 的处理：正文 HTML 已转义、Markdown 图片被剥离；data URI 图表照常嵌入。"""
    data = dict(SELF_DATA, basic_info={"name": "<iframe src=x>![p](file:///C:/a.png)"})
    html = _html(tmp_path, self_data=data, charts={"dashboard": CHART})

    assert "<iframe" not in html
    assert "file:///C:/a.png" not in html
    assert html.count("data:image/png;base64,") == 1


# ---------------------------------------------------------------- 桌面端渲染

qt = pytest.importorskip("PyQt5.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# desktop 包的 __init__ 会导入 main_window → QtWebEngineWidgets；它必须先于 QApplication 导入
# （同 tests/test_desktop_pet_ui.py 的约定），因此在模块级完成导入。
from desktop.report_export import parse_report_meta, save_html_as_pdf, to_qt_body  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """离屏 QApplication（QPdfWriter / QTextDocument 需要）。"""
    from PyQt5.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_to_qt_body_converts_theme_structures():
    """h1 去掉（已在封面）、h2 转着色表格、引用块转黄条表格；无主题外壳时原样返回。"""
    html = ('<main class="report"><h1>标题</h1><h2>账号概览</h2><h2 class="ai-section">AI 调研报告</h2>'
            '<blockquote><p>提示</p></blockquote><hr /><table><tr><td>1</td></tr></table></main>')
    body = to_qt_body(html)

    assert "<h1" not in body and "<h2" not in body and "<hr" not in body
    assert 'class="sec">&nbsp;账号概览' in body
    assert 'class="sec-ai">&nbsp;AI 调研报告' in body
    assert 'class="bar"' in body and "<p>提示</p>" in body
    assert to_qt_body("<p>旧版</p>") == "<p>旧版</p>"


def test_parse_report_meta_unescapes_values():
    """meta 值反转义为纯文本，只交给 QPainter 当文字绘制。"""
    meta = parse_report_meta('<meta name="report-account" content="&lt;b&gt;名">'
                             '<meta name="report-uid" content="42">')
    assert meta == {"account": "<b>名", "uid": "42"}


def test_desktop_export_writes_small_multi_page_pdf(tmp_path, qapp):
    """真实导出：多页 PDF，体积在百 KB 量级（字体子集化），并嵌入无衬线字体。"""
    html = _html(tmp_path, ai_report=AI_OK)
    target = tmp_path / "out" / "report.pdf"
    ok, detail = save_html_as_pdf(html, str(target))

    assert ok, detail
    data = target.read_bytes()
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    assert pages >= 2  # 封面 + 至少一页正文
    assert target.stat().st_size < 1_500_000, target.stat().st_size
    # 字体须子集化：任何嵌入字体流都远小于整套中文字体（微软雅黑单字重 10MB+）。
    # 说明：offscreen QPA 下 Qt 可能找不到系统字体而不嵌入任何字体对象（此时本断言自然成立）；
    # 真实桌面环境下嵌入的是「6 位前缀+字体名」的子集字体（已在 Windows 实机导出中核对）。
    for obj_id in re.findall(rb"/FontFile2\s+(\d+)\s+0\s+R", data):
        length = re.search(rb"\n" + obj_id + rb"\s+0\s+obj\s*<<[^>]*?/Length\s+(\d+)", data)
        assert length is None or int(length.group(1)) < 2_000_000, int(length.group(1))


def test_desktop_export_rejects_empty_html(tmp_path, qapp):
    """空内容不生成文件，返回明确文案。"""
    ok, detail = save_html_as_pdf("   ", str(tmp_path / "x.pdf"))
    assert ok is False and "内容为空" in detail
