"""桌面端诊断报告 PDF 导出。

前端只负责把后端生成的打印 HTML 交给桌面壳；这里负责渲染成 PDF。
不依赖 wkhtmltopdf / pdfkit，避免用户机器缺少外部二进制时降级成 Markdown。

渲染方式（方案 A「终端白」）：
- 封面、每页左侧档案脊、页眉页脚由 QPainter 直接绘制（不依赖 HTML/CSS 能力）；
- 正文由 QTextDocument 排版，章节标题 / 引用块转换为 QTextDocument 支持的表格写法。

为什么不用 Chromium 打印（QWebEnginePage.printToPdf）：QtWebEngine 5.15 的 PDF 后端不做字体子集化，
静态中文字体会整套嵌入（微软雅黑单字重约 12MB），一份报告 20MB+；Qt 自身的 PDF 引擎会子集化，
同一份报告在百 KB 量级。浏览器端仍走 Chrome/Edge 自带打印，使用 report_theme 的完整 CSS 版本。
"""
from __future__ import annotations

import html as html_lib
import re
from pathlib import Path
from typing import Dict

from PyQt5.QtCore import QMarginsF, QPointF, QRectF, QSizeF, Qt
from PyQt5.QtGui import (
    QColor, QFont, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen, QTextDocument,
)

from core.logger import get_logger

logger = get_logger(__name__)

#: 方案 A 配色（与 modules/self_diagnosis/report_theme.py 保持一致）。
INK = QColor("#1d1f21")
PAPER = QColor("#f3f4f2")
SIGNAL = QColor("#f5c400")
MUTED = QColor("#6b6f72")
FAINT = QColor("#8a8e91")
RULE = QColor("#c9ccc8")
GHOST = QColor("#e3e5e1")

#: 无衬线字体：数字/英文 Bahnschrift，中文微软雅黑（均为 Windows 自带）。
LATIN_FAMILY = "Bahnschrift"
CJK_FAMILY = "Microsoft YaHei"

#: 页面几何（毫米）：左侧档案脊宽 13；正文区 左 26 / 上 22 / 右 16 / 下 20。
SPINE_MM = 13.0
BODY_LEFT_MM, BODY_TOP_MM, BODY_RIGHT_MM, BODY_BOTTOM_MM = 26.0, 22.0, 16.0, 20.0
#: PDF 分辨率：文字为矢量，分辨率只影响排版坐标精度。
PDF_DPI = 300

#: QTextDocument 能识别的正文样式子集（不支持定位、伪元素、border-left 等，章节标题改由表格单元格着色）。
_QT_BODY_CSS = """
p { margin-top: 3px; margin-bottom: 3px; }
h3 { font-size: 11pt; font-weight: 600; margin-top: 14px; margin-bottom: 4px; }
ul, ol { margin-top: 2px; margin-bottom: 8px; }
li { margin-bottom: 5px; }
td.sec { background-color: #1d1f21; color: #f3f4f2; font-weight: 600; font-size: 11pt; }
td.sec-ai { background-color: #f5c400; color: #1d1f21; font-weight: 600; font-size: 11pt; }
td.bar { background-color: #f5c400; }
td.quote { color: #3a3d40; }
th { color: #6b6f72; font-weight: 600; }
"""

_META = re.compile(r'<meta\s+name="report-([a-z-]+)"\s+content="([^"]*)"\s*/?>', re.IGNORECASE)
_MAIN = re.compile(r'<main\b[^>]*class="report"[^>]*>(.*)</main>', re.IGNORECASE | re.S)


def _px(value_mm: float) -> float:
    """毫米换算为 PDF_DPI 下的设备像素。"""
    return value_mm / 25.4 * PDF_DPI


def parse_report_meta(html_content: str) -> Dict[str, str]:
    """读取主题写入的 ``<meta name="report-*">``（账号、UID、采集/生成时间），值反转义为纯文本。

    这些值只交给 QPainter 当作文字绘制（不是 HTML），反转义后不会产生标签注入。
    """
    return {key: html_lib.unescape(value) for key, value in _META.findall(html_content or "")}


def to_qt_body(html_content: str) -> str:
    """把主题 HTML 的正文转换为 QTextDocument 可排版的写法。

    只改写主题自身产生的结构标签（``<h2>`` / ``<blockquote>`` / ``<hr>`` / ``<table>``），
    不引入任何来自数据的新标签；正文已在后端完成 HTML 转义与图片剥离。
    找不到 ``<main class="report">`` 时（旧版 HTML）原样返回，保持向后兼容。
    """
    match = _MAIN.search(html_content or "")
    if not match:
        return html_content or ""
    body = match.group(1)

    def _section(m: "re.Match[str]") -> str:
        css = "sec-ai" if "ai-section" in (m.group(1) or "") else "sec"
        return (
            '<table cellspacing="0" cellpadding="7" style="margin-top:18px;margin-bottom:8px"><tr>'
            f'<td class="{css}">&nbsp;{m.group(2)}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</td></tr></table>'
        )

    # 报告标题已绘制在封面；QTextDocument 不支持 display:none，这里直接去掉正文里的 <h1>
    body = re.sub(r"<h1\b[^>]*>.*?</h1>", "", body, count=1, flags=re.S)
    body = re.sub(r"<h2([^>]*)>(.*?)</h2>", _section, body, flags=re.S)
    body = re.sub(
        r"<blockquote>(.*?)</blockquote>",
        r'<table cellspacing="0" cellpadding="5" width="100%" style="margin-top:6px;margin-bottom:6px"><tr>'
        r'<td class="bar" width="4"></td><td class="quote">\1</td></tr></table>',
        body,
        flags=re.S,
    )
    body = re.sub(r"<hr\s*/?>", "", body)
    body = body.replace(
        "<table>", '<table width="100%" cellspacing="0" cellpadding="5" border="0" style="margin-top:6px">'
    )
    return body


def _font(family: str, point_size: float, weight: int = QFont.Normal, spacing: float = 0.0) -> QFont:
    """构造字体；spacing 为字距百分比增量（0 表示原始字距）。"""
    font = QFont(family)
    font.setPointSizeF(point_size)
    font.setWeight(weight)
    if spacing:
        font.setLetterSpacing(QFont.PercentageSpacing, 100 + spacing)
    return font


def _draw_cover(painter: QPainter, page: QRectF, meta: Dict[str, str]) -> None:
    """绘制封面：浅灰纸面、左侧档案脊、标题块、巨型序号与底部账号信息。"""
    painter.fillRect(page, PAPER)
    painter.fillRect(QRectF(0, 0, _px(15), page.height()), INK)
    painter.fillRect(QRectF(0, _px(17), _px(15), _px(1.6)), SIGNAL)

    painter.save()
    painter.setPen(PAPER)
    painter.setFont(_font(LATIN_FAMILY, 7.5, QFont.Normal, 60))
    painter.translate(_px(6), _px(30))
    painter.rotate(90)
    painter.drawText(QPointF(0, 0), "RHODES · CREATOR · DOSSIER")
    painter.restore()

    left, right = _px(30), page.width() - _px(15)
    top_band = QRectF(left, 0, right - left, _px(17))
    painter.setPen(MUTED)
    painter.setFont(_font(LATIN_FAMILY, 7.5, QFont.Normal, 50))
    painter.drawText(top_band, Qt.AlignLeft | Qt.AlignVCenter, "FISHTOOL / SELF-DIAGNOSIS")
    painter.drawText(top_band, Qt.AlignRight | Qt.AlignVCenter, f"UID {meta.get('uid', '—')}")
    painter.setPen(QPen(RULE, _px(0.3)))
    painter.drawLine(QPointF(left, _px(17)), QPointF(right, _px(17)))

    # 巨型序号作为底纹，先画
    painter.setPen(GHOST)
    painter.setFont(_font(LATIN_FAMILY, 190, QFont.Bold))
    painter.drawText(QRectF(left, _px(140), right - left, _px(95)), Qt.AlignRight | Qt.AlignVCenter, "01")

    painter.setPen(MUTED)
    painter.setFont(_font(CJK_FAMILY, 10, QFont.Normal, 60))
    painter.drawText(QPointF(left, _px(64)), "B站账号诊断报告")
    painter.setPen(INK)
    painter.setFont(_font(CJK_FAMILY, 48, QFont.Bold))
    painter.drawText(QPointF(left - _px(1), _px(86)), "创作者")
    painter.drawText(QPointF(left - _px(1), _px(106)), "档案")
    painter.setPen(MUTED)
    painter.setFont(_font(LATIN_FAMILY, 10, QFont.Normal, 70))
    painter.drawText(QPointF(left, _px(118)), "CREATOR DIAGNOSIS REPORT")
    painter.fillRect(QRectF(left, _px(128), _px(32), _px(1.1)), SIGNAL)

    top = page.height() - _px(52)
    painter.setPen(QPen(INK, _px(0.6)))
    painter.drawLine(QPointF(left, top), QPointF(right, top))
    column = (right - left) / 3
    items = (
        ("账号", meta.get("account") or "未获取账号名", CJK_FAMILY),
        ("UID", meta.get("uid") or "—", LATIN_FAMILY),
        ("数据采集", meta.get("fetched-at") or "未记录", LATIN_FAMILY),
    )
    for index, (label, value, family) in enumerate(items):
        x = left + column * index
        painter.setPen(MUTED)
        painter.setFont(_font(CJK_FAMILY, 7.5, QFont.Normal, 30))
        painter.drawText(QPointF(x, top + _px(8)), label)
        painter.setPen(INK)
        painter.setFont(_font(family, 14, QFont.DemiBold))
        painter.drawText(
            QRectF(x, top + _px(10), column - _px(4), _px(10)),
            Qt.AlignLeft | Qt.AlignTop | Qt.TextSingleLine,
            value,
        )


def _draw_frame(painter: QPainter, page: QRectF, meta: Dict[str, str], page_no: int, total: int) -> None:
    """绘制正文页的档案脊、页眉与页脚。"""
    painter.fillRect(QRectF(0, 0, _px(SPINE_MM), page.height()), INK)
    painter.fillRect(QRectF(0, _px(14), _px(SPINE_MM), _px(1.6)), SIGNAL)
    x0, x1 = _px(BODY_LEFT_MM), page.width() - _px(BODY_RIGHT_MM)

    account = meta.get("account") or ""
    head = f"{account} · UID {meta.get('uid', '—')}" if account else f"UID {meta.get('uid', '—')}"
    band = QRectF(x0, 0, x1 - x0, _px(14))
    painter.setPen(MUTED)
    painter.setFont(_font(CJK_FAMILY, 7.5, QFont.Normal, 25))
    painter.drawText(band, Qt.AlignLeft | Qt.AlignVCenter, head)
    painter.setFont(_font(LATIN_FAMILY, 7.5, QFont.Normal, 25))
    painter.drawText(band, Qt.AlignRight | Qt.AlignVCenter, "SELF-DIAGNOSIS DOSSIER")
    painter.setPen(QPen(RULE, _px(0.3)))
    painter.drawLine(QPointF(x0, _px(14)), QPointF(x1, _px(14)))

    foot_top = page.height() - _px(12)
    painter.drawLine(QPointF(x0, foot_top), QPointF(x1, foot_top))
    foot = QRectF(x0, foot_top, x1 - x0, _px(12))
    painter.setPen(FAINT)
    painter.setFont(_font(CJK_FAMILY, 7, QFont.Normal, 25))
    painter.drawText(foot, Qt.AlignLeft | Qt.AlignVCenter, f"FishTool · 生成于 {meta.get('generated-at', '')}")
    painter.setFont(_font(LATIN_FAMILY, 7.5, QFont.DemiBold))
    painter.drawText(foot, Qt.AlignRight | Qt.AlignVCenter, f"{page_no:02d} / {total:02d}")


def save_html_as_pdf(html_content: str, output_path: str) -> tuple[bool, str]:
    """把打印 HTML 渲染为 PDF 文件（方案 A 主题，Qt 原生 PDF 引擎，字体自动子集化）。

    Args:
        html_content: 后端生成的完整 HTML 文档。
        output_path: 用户选择的保存路径。

    Returns:
        tuple[bool, str]: ``(是否成功, 成功时为路径 / 失败时为错误信息)``。
    """
    if not html_content or not str(html_content).strip():
        return False, "报告内容为空，无法生成 PDF。"

    painter = None
    try:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        writer = QPdfWriter(str(path))
        writer.setTitle("B站账号诊断报告")
        writer.setCreator("FishTool")
        writer.setResolution(PDF_DPI)
        writer.setPageLayout(QPageLayout(
            QPageSize(QPageSize.A4), QPageLayout.Portrait, QMarginsF(0, 0, 0, 0), QPageLayout.Millimeter
        ))
        page = QRectF(0, 0, _px(210), _px(297))
        body = QRectF(
            _px(BODY_LEFT_MM), _px(BODY_TOP_MM),
            page.width() - _px(BODY_LEFT_MM + BODY_RIGHT_MM),
            page.height() - _px(BODY_TOP_MM + BODY_BOTTOM_MM),
        )

        meta = parse_report_meta(str(html_content))
        document = QTextDocument()
        document.setDocumentMargin(0)
        # 显式指定字体，避免不同机器回退到缺字字体：Latin 用 Bahnschrift，中文回退微软雅黑。
        base_font = QFont(LATIN_FAMILY)
        base_font.setFamilies([LATIN_FAMILY, CJK_FAMILY])
        base_font.setPointSizeF(10)
        document.setDefaultFont(base_font)
        document.setDefaultStyleSheet(_QT_BODY_CSS)
        # 按 PDF 设备排版，页高即正文区高度，pageCount 即正文页数。
        document.documentLayout().setPaintDevice(writer)
        document.setPageSize(QSizeF(body.width(), body.height()))
        document.setHtml(to_qt_body(str(html_content)))
        body_pages = max(1, document.pageCount())

        painter = QPainter(writer)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        _draw_cover(painter, page, meta)
        for index in range(body_pages):
            writer.newPage()
            _draw_frame(painter, page, meta, index + 2, body_pages + 1)
            painter.save()
            painter.translate(body.left(), body.top() - index * body.height())
            document.drawContents(painter, QRectF(0, index * body.height(), body.width(), body.height()))
            painter.restore()
        painter.end()
        painter = None

        if not path.exists() or path.stat().st_size <= 0:
            return False, "PDF 文件未生成，请检查目标目录是否可写。"
        logger.info("[报告导出] PDF 已保存: %s", path)
        return True, str(path)
    except Exception as exc:  # noqa: BLE001 - 导出失败必须回传错误文案而不是崩溃
        logger.exception("[报告导出] PDF 生成失败: %s", exc)
        return False, f"PDF 生成失败: {exc}"
    finally:
        if painter is not None and painter.isActive():
            painter.end()
