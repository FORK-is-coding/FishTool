"""自诊报告 PDF（report_pdf.py）：HTML 解析、字体回退、版式常量与桌面版一致、端到端生成。

需要 reportlab（AstrBot 自带）与 pypdf；缺失时跳过。样例 HTML 由引擎 ReportGenerator.generate_html_report 生成。
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path

import pytest

pytest.importorskip("reportlab")
pypdf = pytest.importorskip("pypdf")

from conftest import REPO_ROOT  # noqa: E402
from fishtool_adapter import report_pdf as rp  # noqa: E402

log = logging.getLogger("test")
FIXTURE = Path(__file__).parent / "fixtures" / "self_diagnosis_report.html"


@pytest.fixture(scope="module")
def html_text() -> str:
    return FIXTURE.read_text("utf-8")


@pytest.fixture(autouse=True)
def _clear_font_cache():
    rp._FONT_CACHE.clear()
    yield
    rp._FONT_CACHE.clear()


def pdf_text(data: bytes) -> list[str]:
    return [page.extract_text() for page in pypdf.PdfReader(io.BytesIO(data)).pages]


# ---------------------------------------------------------------- 版式常量与桌面版一致


def test_layout_constants_match_desktop():
    src = REPO_ROOT / "desktop" / "report_export.py"
    if not src.is_file():
        pytest.skip("desktop/report_export.py 不在当前检出中（例如发布仓库）")
    text = src.read_text("utf-8")
    colors = dict(re.findall(r'^(INK|PAPER|SIGNAL|MUTED|FAINT|RULE|GHOST) = QColor\("(#[0-9a-fA-F]{6})"\)', text, re.M))
    assert colors, "desktop 常量格式变了，更新这个测试"
    for name, value in colors.items():
        assert getattr(rp, name).lower() == value.lower(), name
    spine = re.search(r"^SPINE_MM = ([\d.]+)", text, re.M)
    body = re.search(r"^BODY_LEFT_MM, BODY_TOP_MM, BODY_RIGHT_MM, BODY_BOTTOM_MM = (.+)$", text, re.M)
    assert float(spine.group(1)) == rp.SPINE_MM
    assert tuple(float(x) for x in body.group(1).split(",")) == (
        rp.BODY_LEFT_MM, rp.BODY_TOP_MM, rp.BODY_RIGHT_MM, rp.BODY_BOTTOM_MM)


# ---------------------------------------------------------------- 解析


def test_parse_meta(html_text):
    meta = rp.parse_meta(html_text)
    assert meta["uid"] == "123456"
    assert meta["account"] == "测试UP<script>"  # 反转义后的纯文本；绘制时不经过标记解析
    assert meta["fetched-at"] == "2026-11-04 10:00:00"


def test_parse_blocks_structure(html_text):
    blocks = rp.parse_blocks(html_text)
    kinds = [b.kind for b in blocks]
    assert "h1" not in kinds  # 封面已有标题
    labels = [("".join(t for t, _ in b.runs), b.kind) for b in blocks if b.kind in ("h2", "h2ai")]
    assert ("AI 调研报告", "h2ai") in labels and ("账号概览", "h2") in labels
    assert "quote" in kinds and "li" in kinds and "hr" in kinds
    first = blocks[0]
    assert first.kind == "p" and ("\n", False) in first.runs  # <br> 保留为段内换行
    assert any(bold for _, bold in first.runs)


def test_parse_table_and_ordered_list():
    html = (
        '<main class="report"><ol><li>一</li><li>二</li></ol>'
        "<table><thead><tr><th>UID</th><th>名次</th></tr></thead>"
        "<tbody><tr><td>1 &lt;me&gt;</td><td>3</td></tr></tbody></table></main>"
    )
    blocks = rp.parse_blocks(html)
    assert [(b.kind, b.index) for b in blocks[:2]] == [("oli", 1), ("oli", 2)]
    assert blocks[2].kind == "table" and blocks[2].rows == [["UID", "名次"], ["1 <me>", "3"]]


def test_loose_and_nested_lists_keep_list_items():
    """markdown2 的“松散列表”会生成 <li><p>…</p></li>；嵌套列表的父项在子列表后还有尾文字。都不能丢、也不能变成普通段落。"""
    blocks = rp.parse_blocks(
        '<main class="report"><ul><li><p>一段</p><p>二段</p></li></ul>'
        "<ul><li>父<ul><li>子</li></ul>尾</li></ul></main>"
    )
    assert [(b.kind, "".join(t for t, _ in b.runs)) for b in blocks] == [
        ("li", "一段"), ("li", "二段"), ("li", "父"), ("li", "子"), ("li", "尾"),
    ]


def test_unknown_tags_keep_text():
    blocks = rp.parse_blocks('<main class="report"><div><span>游离文字</span></div><p>a<code>b</code>c</p></main>')
    assert ["".join(t for t, _ in b.runs) for b in blocks] == ["游离文字", "abc"]


def test_markup_escapes_external_text():
    markup = rp._runs_markup([("A <b>& ", False), ("粗", True)])
    assert "&lt;b&gt;&amp;" in markup and "<b>" not in markup


# ---------------------------------------------------------------- 字体


def test_fonts_fallback_to_cid_when_nothing_found(monkeypatch, caplog):
    monkeypatch.setattr(rp, "_CJK_CANDIDATES", {})
    monkeypatch.setattr(rp, "_LATIN_CANDIDATES", {})
    with caplog.at_level(logging.WARNING):
        fonts = rp.resolve_fonts({}, log)
    assert fonts.degraded and fonts.cjk == rp.FALLBACK_CJK and fonts.latin == rp.FALLBACK_CJK
    assert "PDF 版式已降级" in caplog.text
    assert rp.font_label({}, log).startswith("已降级")


def test_missing_configured_font_falls_back(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(rp, "_CJK_CANDIDATES", {})
    with caplog.at_level(logging.WARNING):
        fonts = rp.resolve_fonts({"report_font_cjk": str(tmp_path / "nope.ttf")}, log)
    assert fonts.degraded
    assert "字体文件不存在" in caplog.text


def test_non_font_file_falls_back(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(rp, "_CJK_CANDIDATES", {})
    bogus = tmp_path / "secret.txt"
    bogus.write_text("password=hunter2", "utf-8")
    with caplog.at_level(logging.WARNING):
        fonts = rp.resolve_fonts({"report_font_cjk": str(bogus)}, log)
    assert fonts.degraded
    assert "hunter2" not in caplog.text  # 只记路径，不回显文件内容


def test_cff_font_reports_clear_reason(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(rp, "_CJK_CANDIDATES", {})
    otf = tmp_path / "NotoSansCJK.otf"
    # 最小的 CFF 轮廓 OpenType 头：reportlab 读到 'OTTO' 就会拒绝
    otf.write_bytes(b"OTTO" + b"\x00" * 64)
    with caplog.at_level(logging.WARNING):
        rp.resolve_fonts({"report_font_cjk": str(otf)}, log)
    assert "OpenType/CFF" in caplog.text or "不是可用的 TrueType 字体" in caplog.text


def test_system_font_used_when_available():
    fonts = rp.resolve_fonts({}, log)
    if fonts.degraded:
        pytest.skip("当前机器没有可用的 TrueType 中文字体")
    assert fonts.cjk.startswith("FT_CJK_")


def test_font_registration_name_changes_with_file(tmp_path):
    src = next((p for p in rp._CJK_CANDIDATES.get(rp._platform_key(), []) if Path(p).is_file()), None)
    if src is None:
        pytest.skip("当前机器没有可用的 TrueType 中文字体")
    copy = tmp_path / ("a" + Path(src).suffix)
    copy.write_bytes(Path(src).read_bytes())
    first = rp.resolve_fonts({"report_font_cjk": str(copy)}, log).cjk
    rp._FONT_CACHE.clear()
    copy.write_bytes(Path(src).read_bytes() + b"\x00")  # 换了文件内容（大小变化）
    second = rp.resolve_fonts({"report_font_cjk": str(copy)}, log).cjk
    assert first != second


# ---------------------------------------------------------------- 端到端


def test_build_pdf_end_to_end(html_text):
    fonts = rp.resolve_fonts({}, log)
    data = rp.build(html_text, fonts)
    assert data.startswith(b"%PDF")
    pages = pdf_text(data)
    assert len(pages) >= 2
    cover = pages[0]
    assert "CREATOR DIAGNOSIS REPORT" in cover and "123456" in cover
    body = "\n".join(pages[1:])
    assert "账号概览" in body and "AI 调研报告" in body and "粉丝数: 12,345" in body
    assert f"02 / {len(pages):02d}" in pages[1]
    assert "01 /" not in cover  # 封面不画页脚
    if not fonts.degraded:
        assert "测试UP<script>" in cover  # 外部文本按纯文本绘制，没被当成标记吞掉


def test_build_wraps_layout_error(html_text):
    """reportlab 排版失败要转成可读的 ReportPdfError，不能以 LayoutError 抛穿成“内部错误”。"""
    from reportlab.platypus import Spacer

    fonts = rp.resolve_fonts({}, log)
    with pytest.raises(rp.ReportPdfError) as excinfo:
        rp.build(html_text, fonts, charts=[Spacer(1, 5000)])
    message = str(excinfo.value)
    assert "报告排版失败" in message and "LayoutError" in message
    assert excinfo.value.__cause__ is not None  # 原始异常保留在链上，便于排查


def test_build_rejects_empty():
    fonts = rp.resolve_fonts({}, log)
    with pytest.raises(rp.ReportPdfError):
        rp.build("", fonts)
    with pytest.raises(rp.ReportPdfError):
        rp.build('<main class="report"><h1>只有标题</h1></main>', fonts)


def test_build_with_degraded_font(monkeypatch, html_text):
    monkeypatch.setattr(rp, "_CJK_CANDIDATES", {})
    monkeypatch.setattr(rp, "_LATIN_CANDIDATES", {})
    fonts = rp.resolve_fonts({}, log)
    assert rp.build(html_text, fonts).startswith(b"%PDF")


def test_report_filename():
    assert rp.report_filename({"uid": "123"}, "20261104") == "FishTool自诊_123_20261104.pdf"
    assert rp.report_filename({"uid": "../x"}, "20261104") == "FishTool自诊_unknown_20261104.pdf"
