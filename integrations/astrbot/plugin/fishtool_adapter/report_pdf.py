"""自诊报告 PDF：把引擎 ``/api/analysis/report-html`` 返回的主题 HTML 用 reportlab 排成方案 A「终端白」版式。

桌面版用 PySide6（desktop/report_export.py）画同一份 HTML；插件进程里没有 PySide6，改用 AstrBot 自带的
reportlab。版式常量照抄桌面版，``tests/test_report_pdf.py`` 会和 desktop/report_export.py 比对，防止分叉。

只在插件进程内运行，不 import astrbot；logger 由调用方传入。``build()`` 是同步 CPU/磁盘操作，调用方要放进
``asyncio.to_thread``。
"""
from __future__ import annotations

import hashlib
import html as html_lib
import os
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

# ---------------------------------------------------------------- 版式常量（与 desktop/report_export.py 一致）
INK = "#1d1f21"
PAPER = "#f3f4f2"
SIGNAL = "#f5c400"
MUTED = "#6b6f72"
FAINT = "#8a8e91"
RULE = "#c9ccc8"
GHOST = "#e3e5e1"
#: 表格单元格底线与引用正文（report_theme.py CSS）。
CELL_RULE = "#d9dcd8"
QUOTE_TEXT = "#3a3d40"

SPINE_MM = 13.0
BODY_LEFT_MM, BODY_TOP_MM, BODY_RIGHT_MM, BODY_BOTTOM_MM = 26.0, 22.0, 16.0, 20.0

#: 系统字体候选（TrueType 轮廓；Noto Sans CJK 等 CFF 轮廓的 .otf reportlab 无法嵌入）。
_CJK_CANDIDATES = {
    "win32": ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simhei.ttf"],
    "darwin": ["/System/Library/Fonts/STHeiti Medium.ttc", "/Library/Fonts/Arial Unicode.ttf"],
    "linux": [
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    ],
}
_LATIN_CANDIDATES = {"win32": ["C:/Windows/Fonts/bahnschrift.ttf"]}
#: 都找不到时退回的 CID 字体（不嵌入，宋体）。
FALLBACK_CJK = "STSong-Light"

_META = re.compile(r'<meta\s+name="report-([a-z-]+)"\s+content="([^"]*)"\s*/?>', re.IGNORECASE)
_MAIN = re.compile(r'<main\b[^>]*class="report"[^>]*>(.*)</main>', re.IGNORECASE | re.S)


class ReportPdfError(RuntimeError):
    """PDF 生成失败（缺少 reportlab、内容为空等），消息可直接回复给用户。"""


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


# ---------------------------------------------------------------- 字体
@dataclass
class Fonts:
    """注册到 reportlab 的字体名。``degraded`` 为 True 表示退回了 STSong-Light。"""

    cjk: str
    cjk_bold: str
    latin: str
    latin_bold: str
    label: str
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)


_FONT_CACHE: dict[tuple, Fonts] = {}


def _font_key(path: str) -> tuple:
    try:
        stat = os.stat(path)
        return (os.path.abspath(path), int(stat.st_mtime), stat.st_size)
    except OSError:
        return (os.path.abspath(path), None, None)


def _register_ttf(path: str, prefix: str) -> tuple[str | None, str]:
    """注册一个 .ttf/.ttc（取第 0 个子字体）。成功返回 (注册名, 字体族名)，失败返回 (None, 原因)。

    注册名带“路径 + 修改时间 + 大小”的哈希：reportlab 的字体注册表是进程级的，插件重载不会清空，
    用户换了字体文件或路径时，固定名字会一直用到旧字体。
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFError, TTFont

    if not os.path.isfile(path):
        return None, f"字体文件不存在：{path}"
    digest = hashlib.sha1(repr(_font_key(path)).encode("utf-8")).hexdigest()[:8]
    name = f"{prefix}_{digest}"
    if name in pdfmetrics.getRegisteredFontNames():
        return name, name
    try:
        kwargs = {"subfontIndex": 0} if path.lower().endswith(".ttc") else {}
        font = TTFont(name, path, **kwargs)
    except TTFError as exc:
        if "postscript outlines" in str(exc).lower():
            return None, (f"{path} 是 OpenType/CFF 格式，reportlab 无法嵌入，"
                          "请换 .ttf 或 TrueType 轮廓的 .ttc（Noto Sans CJK、思源黑体的 .otf 都不行）")
        return None, f"{path} 不是可用的 TrueType 字体：{exc}"
    except (OSError, ValueError, KeyError, IndexError) as exc:
        return None, f"{path} 读取失败：{exc}"
    pdfmetrics.registerFont(font)
    family = getattr(getattr(font, "face", None), "name", b"")
    family = family.decode("utf-8", "replace") if isinstance(family, bytes) else str(family or name)
    return name, family


def resolve_fonts(chat: dict, logger) -> Fonts:
    """按“配置路径 → 系统字体 → STSong-Light”的顺序找字体，结果按配置与文件状态缓存。

    Raises:
        ReportPdfError: 当前 Python 环境没有 reportlab。
    """
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    except ImportError as exc:
        raise ReportPdfError("当前 AstrBot 环境缺少 reportlab，请执行 pip install reportlab") from exc

    cjk_path = (chat.get("report_font_cjk") or "").strip()
    latin_path = (chat.get("report_font_latin") or "").strip()
    plat = _platform_key()
    key = (plat, _font_key(cjk_path) if cjk_path else None, _font_key(latin_path) if latin_path else None)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached

    warnings: list[str] = []
    cjk = family = None
    for path in ([cjk_path] if cjk_path else []) + _CJK_CANDIDATES.get(plat, []):
        name, info = _register_ttf(path, "FT_CJK")
        if name:
            cjk, family = name, info
            break
        if path == cjk_path:
            warnings.append(info)
    degraded = cjk is None
    if degraded:
        if FALLBACK_CJK not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(FALLBACK_CJK))
        cjk, family = FALLBACK_CJK, "宋体（STSong-Light，未嵌入）"
        warnings.append("未找到可用的中文字体，PDF 版式已降级。请在插件配置的『自诊报告字体』里填写 .ttf / .ttc 路径。")

    latin = None
    for path in ([latin_path] if latin_path else []) + _LATIN_CANDIDATES.get(plat, []):
        name, info = _register_ttf(path, "FT_LAT")
        if name:
            latin = name
            break
        if path == latin_path:
            warnings.append(info)
    latin = latin or cjk

    for message in warnings:
        logger.warning(f"[fishtool] 自诊报告字体：{message}")
    # 只有一套字形：粗体用同一字体，标题靠字号和颜色区分（reportlab 不能合成粗体 TTF）
    fonts = Fonts(cjk=cjk, cjk_bold=cjk, latin=latin, latin_bold=latin,
                  label=family or cjk, degraded=degraded, warnings=warnings)
    _FONT_CACHE[key] = fonts
    return fonts


def font_label(chat: dict, logger) -> str:
    """`/fishtool 状态` 里显示的字体名；不抛异常。"""
    try:
        fonts = resolve_fonts(chat, logger)
    except ReportPdfError:
        return "不可用（缺少 reportlab）"
    return f"已降级为{fonts.label}" if fonts.degraded else fonts.label


# ---------------------------------------------------------------- HTML 解析
def parse_meta(html_text: str) -> dict[str, str]:
    """主题写入的 ``<meta name="report-*">``：account / uid / fetched-at / generated-at。"""
    return {key: html_lib.unescape(value) for key, value in _META.findall(html_text or "")}

#: <br> 的内部标记：和源码里的普通换行符区分开。
_BR = " "


@dataclass
class Block:
    """正文块。``runs`` 是 [(文本, 是否加粗)]，文本已反转义成纯文本。"""

    kind: str  # h2 / h2ai / h3 / p / li / oli / quote / pre / hr / table
    runs: list[tuple[str, bool]] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)  # table：第一行是表头
    index: int = 0  # oli 的序号


class _ReportParser(HTMLParser):
    """只认主题会产生的结构；不认识的标签只取文字。与桌面版 to_qt_body 的处理范围一致。"""

    _INLINE_BOLD = {"strong", "b"}
    _BLOCKS = {"h2", "h3", "p", "li", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._cur: Block | None = None
        self._bold = 0
        self._skip = 0  # h1（封面已有标题）
        self._quote = 0
        self._lists: list[list] = []  # [kind, counter]
        self._table: list[list[str]] | None = None
        self._cell: list[str] | None = None

    # -- helpers
    def _open(self, kind: str) -> None:
        self._close()
        block = Block(kind)
        if kind == "li" and self._lists and self._lists[-1][0] == "ol":
            self._lists[-1][1] += 1
            block.kind, block.index = "oli", self._lists[-1][1]
        self._cur = block

    def _close(self) -> None:
        block = self._cur
        self._cur = None
        if block is None:
            return
        text = "".join(t for t, _ in block.runs).strip()
        if not text:
            return
        if self._quote and block.kind in ("p", "li"):
            block.kind = "quote"
        self.blocks.append(block)

    # -- HTMLParser callbacks
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h1":
            self._close()
            self._skip += 1
        elif tag == "table":
            self._close()
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._table.append([])
        elif tag in ("td", "th") and self._table is not None:
            self._cell = []
        elif tag in ("ul", "ol"):
            self._close()
            self._lists.append([tag, 0])
        elif tag == "blockquote":
            self._close()
            self._quote += 1
        elif tag == "h2":
            self._open("h2ai" if "ai-section" in (attrs.get("class") or "") else "h2")
        elif tag == "p" and self._cur is not None and self._cur.kind in ("li", "oli"):
            # 松散列表 <li><p>…</p></li>：第一个 p 并入当前列表项；后续 p 另起一项（保持列表样式）
            if "".join(t for t, _ in self._cur.runs).strip():
                self._open("li")
        elif tag == "p" and self._lists and self._cur is None:
            self._open("li")
        elif tag in self._BLOCKS and tag != "blockquote":
            self._open(tag)
        elif tag == "hr":
            self._close()
            self.blocks.append(Block("hr"))
        elif tag == "br":
            self._text(_BR)
        elif tag in self._INLINE_BOLD:
            self._bold += 1

    def handle_endtag(self, tag):
        if tag == "h1":
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._table is not None and self._cell is not None:
            if self._table:
                self._table[-1].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "table" and self._table is not None:
            rows = [r for r in self._table if r]
            if rows:
                self.blocks.append(Block("table", rows=rows))
            self._table = None
        elif tag in ("ul", "ol"):
            self._close()
            if self._lists:
                self._lists.pop()
        elif tag == "blockquote":
            self._close()
            self._quote = max(0, self._quote - 1)
        elif tag == "p" and self._cur is not None and self._cur.kind in ("li", "oli"):
            self._close()
        elif tag in self._BLOCKS:
            self._close()
        elif tag in self._INLINE_BOLD:
            self._bold = max(0, self._bold - 1)

    def handle_data(self, data):
        self._text(data)

    def _text(self, data: str) -> None:
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
            return
        if self._cur is None:
            if not data.strip():
                return
            # 游离文本当段落；列表里的游离文本（如 </p> 之后、</li> 之前）仍归到列表项
            self._open("li" if self._lists else "p")
        if data == _BR:
            self._cur.runs.append(("\n", False))
            return
        if self._cur.kind != "pre":
            # 源码里 <br /> 后面紧跟的换行符是排版空白，不是换行
            data = re.sub(r"\s+", " ", data)
            if self._cur.runs and self._cur.runs[-1][0] == "\n":
                data = data.lstrip()
            if not data:
                return
        self._cur.runs.append((data, self._bold > 0))

    def close(self):
        super().close()
        self._close()


def parse_blocks(html_text: str) -> list[Block]:
    """取 ``<main class="report">`` 里的正文块；没有 main（旧版 HTML）时解析整个 body。"""
    match = _MAIN.search(html_text or "")
    body = match.group(1) if match else (html_text or "")
    parser = _ReportParser()
    parser.feed(body)
    parser.close()
    return parser.blocks

# ---------------------------------------------------------------- 渲染
def escape_text(text: str) -> str:
    """纯文本 → reportlab Paragraph 标记里的安全文本（外部文本里的 < & 不能被当成标记）。"""
    return html_lib.escape(text, quote=False)


_esc = escape_text


def _runs_markup(runs: list[tuple[str, bool]]) -> str:
    parts = []
    for text, bold in runs:
        safe = _esc(text).replace("\n", "<br/>")
        parts.append(f'<font color="{INK}">{safe}</font>' if bold else safe)
    markup = "".join(parts).strip()
    return re.sub(r"^(<br/>\s*)+|(\s*<br/>)+$", "", markup)


def _spaced(canvas, x: float, y: float, text: str, font: str, size: float, spacing_pct: float = 0.0,
            align: str = "left") -> None:
    """带字距的单行文字。``spacing_pct`` 对应 Qt 的百分比字距增量（桌面版 _font 的 spacing）。"""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    char_space = size * spacing_pct / 100.0
    width = stringWidth(text, font, size) + char_space * max(0, len(text) - 1)
    if align == "right":
        x -= width
    obj = canvas.beginText(x, y)
    obj.setFont(font, size)
    obj.setCharSpace(char_space)
    obj.textOut(text)
    canvas.drawText(obj)


def section_label(text: str, *, ai: bool, fonts: Fonts):
    """章节标签：黑底白字（AI 章节黄底黑字）+ 右侧斜切角，最小 48 mm（report_theme.py 的 h2）。"""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Flowable

    class SectionLabel(Flowable):
        size = 11.0
        pad_x, pad_y, tip = 3.5 * mm, 1.6 * mm, 4.0 * mm

        def __init__(self):
            super().__init__()
            self.spacing = self.size * 20 / 100.0
            text_w = stringWidth(text, fonts.cjk_bold, self.size) + self.spacing * max(0, len(text) - 1)
            self.box_w = max(48 * mm, text_w + self.pad_x + 9 * mm)
            self.box_h = self.size + 2 * self.pad_y
            self.spaceBefore, self.spaceAfter = 9 * mm, 4 * mm

        def wrap(self, avail_w, avail_h):
            self.width = min(self.box_w + self.tip, avail_w)
            self.height = self.box_h
            return self.width, self.height

        def draw(self):
            c = self.canv
            bg = HexColor(SIGNAL if ai else INK)
            fg = HexColor(INK if ai else PAPER)
            body_w = self.width - self.tip
            c.setFillColor(bg)
            c.rect(0, 0, body_w, self.box_h, stroke=0, fill=1)
            path = c.beginPath()
            path.moveTo(body_w, 0)
            path.lineTo(body_w + self.tip, self.box_h / 2)
            path.lineTo(body_w, self.box_h)
            path.close()
            c.drawPath(path, stroke=0, fill=1)
            c.setFillColor(fg)
            _spaced(c, self.pad_x, self.pad_y + self.size * 0.18, text, fonts.cjk_bold, self.size, 20)

    return SectionLabel()


def _styles(fonts: Fonts):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm

    base = ParagraphStyle("ft_body", fontName=fonts.cjk, fontSize=10, leading=17,
                          textColor=HexColor(INK), spaceBefore=1.5 * mm, spaceAfter=1.5 * mm, wordWrap="CJK")
    return {
        "p": base,
        "meta": ParagraphStyle("ft_meta", parent=base, fontSize=9, leading=15, textColor=HexColor(MUTED)),
        "h3": ParagraphStyle("ft_h3", parent=base, fontName=fonts.cjk_bold, fontSize=10.5, leading=16,
                             spaceBefore=5 * mm, spaceAfter=2 * mm),
        "li": ParagraphStyle("ft_li", parent=base, spaceBefore=0, spaceAfter=0, leftIndent=0),
        "quote": ParagraphStyle("ft_quote", parent=base, textColor=HexColor(QUOTE_TEXT), spaceBefore=0, spaceAfter=0),
        "pre": ParagraphStyle("ft_pre", parent=base, fontSize=9, leading=14),
        "th": ParagraphStyle("ft_th", parent=base, fontName=fonts.cjk_bold, fontSize=8, leading=11,
                             textColor=HexColor(MUTED), spaceBefore=0, spaceAfter=0),
        "td": ParagraphStyle("ft_td", parent=base, fontSize=9.5, leading=13, spaceBefore=0, spaceAfter=0),
    }


def _list_item(markup: str, styles, index: int | None, width: float, fonts: Fonts):
    """列表项：底部细线分隔；有序列表左侧黄褐色两位序号（report_theme.py 的 ol > li::before）。"""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    body = Paragraph(markup, styles["li"])
    if index is None:
        cells, widths = [[body]], [width]
    else:
        num_style = ParagraphStyle("ft_num", parent=styles["li"], fontName=fonts.latin_bold, textColor=HexColor("#9a6b00"))
        cells, widths = [[Paragraph(f"{index:02d}", num_style), body]], [8 * mm, width - 8 * mm]
    table = Table(cells, colWidths=widths)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1.6 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6 * mm),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25 * mm, HexColor(GHOST)),
    ]))
    return table


def _quote(markup: str, styles, width: float):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    table = Table([["", Paragraph(markup, styles["quote"])]], colWidths=[1 * mm, width - 1 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), HexColor(SIGNAL)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 4 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
    ]))
    table.spaceBefore = table.spaceAfter = 3 * mm
    return table


def _data_table(rows: list[list[str]], styles, width: float):
    """表格：表头灰色小字 + 0.6 mm 黑色底线，单元格 0.25 mm 浅灰底线（report_theme.py 的 th/td）。"""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    columns = max(len(r) for r in rows)
    cells = []
    for row_index, row in enumerate(rows):
        style = styles["th"] if row_index == 0 else styles["td"]
        cells.append([Paragraph(_esc(row[i]) if i < len(row) else "", style) for i in range(columns)])
    table = Table(cells, colWidths=[width / columns] * columns, repeatRows=1)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6 * mm, HexColor(INK)),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25 * mm, HexColor(CELL_RULE)),
    ]))
    table.spaceBefore, table.spaceAfter = 3 * mm, 5 * mm
    return table


def build_story(blocks: list[Block], fonts: Fonts, width: float, *, charts: list | None = None) -> list:
    """正文块 → reportlab flowables。``charts`` 插在第一个 hr 之后（与引擎插入图表章节的位置一致）。"""
    from reportlab.platypus import KeepTogether, Paragraph, Spacer

    styles = _styles(fonts)
    story: list = []
    charts_pending = list(charts or [])
    first_p = True
    for block in blocks:
        if block.kind == "hr":
            story.append(Spacer(1, 6))
            if charts_pending:
                story.extend(charts_pending)
                charts_pending = []
            continue
        if block.kind in ("h2", "h2ai"):
            text = "".join(t for t, _ in block.runs).strip()
            story.append(section_label(text, ai=block.kind == "h2ai", fonts=fonts))
            continue
        if block.kind == "table":
            story.append(_data_table(block.rows, styles, width))
            continue
        markup = _runs_markup(block.runs)
        if block.kind == "h3":
            story.append(Paragraph(markup, styles["h3"]))
        elif block.kind in ("li", "oli"):
            story.append(_list_item(markup, styles, block.index if block.kind == "oli" else None, width, fonts))
        elif block.kind == "quote":
            story.append(_quote(markup, styles, width))
        elif block.kind == "pre":
            story.append(Paragraph(markup, styles["pre"]))
        else:
            # 正文第一段是“生成时间 / 采集时间 / UID”元信息，按主题用小号灰字
            story.append(Paragraph(markup, styles["meta"] if first_p else styles["p"]))
            first_p = False
    if charts_pending:
        story.extend(charts_pending)
    # 标题不落在页尾：标签和后面第一块绑在一起
    merged: list = []
    for item in story:
        if merged and type(merged[-1]).__name__ == "SectionLabel":
            merged[-1] = KeepTogether([merged[-1], item])
        else:
            merged.append(item)
    return merged

# ---------------------------------------------------------------- 封面与页框（坐标照抄 desktop/report_export.py）
#: A4（毫米）。Qt 的 y 轴向下、reportlab 向上：桌面版的 y 换算为 ``PAGE_H - y``。
PAGE_W, PAGE_H = 210.0, 297.0


def _y(mm_from_top: float) -> float:
    from reportlab.lib.units import mm

    return (PAGE_H - mm_from_top) * mm


def _vcenter(top_mm: float, height_mm: float, size_pt: float) -> float:
    """Qt 在矩形内垂直居中的文字 → reportlab 基线（按字号的 0.35 倍估算基线偏移）。"""
    from reportlab.lib.units import mm

    return _y(top_mm + height_mm / 2) - size_pt * 0.35 + 0 * mm


def draw_cover(canvas, meta: dict[str, str], fonts: Fonts) -> None:
    """封面：浅灰纸面、左侧档案脊、标题块、巨型序号与底部账号信息（_draw_cover）。"""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm

    c = canvas
    c.saveState()
    c.setFillColor(HexColor(PAPER))
    c.rect(0, 0, PAGE_W * mm, PAGE_H * mm, stroke=0, fill=1)
    c.setFillColor(HexColor(INK))
    c.rect(0, 0, 15 * mm, PAGE_H * mm, stroke=0, fill=1)
    c.setFillColor(HexColor(SIGNAL))
    c.rect(0, _y(17 + 1.6), 15 * mm, 1.6 * mm, stroke=0, fill=1)

    # 竖排英文：Qt 先平移到 (6, 30) 再旋转 90°（顺时针），reportlab 的正角是逆时针，所以转 -90°
    c.saveState()
    c.translate(6 * mm, _y(30))
    c.rotate(-90)
    c.setFillColor(HexColor(PAPER))
    _spaced(c, 0, 0, "RHODES · CREATOR · DOSSIER", fonts.latin, 7.5, 60)
    c.restoreState()

    left, right = 30.0, PAGE_W - 15.0
    c.setFillColor(HexColor(MUTED))
    base = _vcenter(0, 17, 7.5)
    _spaced(c, left * mm, base, "FISHTOOL / SELF-DIAGNOSIS", fonts.latin, 7.5, 50)
    _spaced(c, right * mm, base, f"UID {meta.get('uid', '—')}", fonts.latin, 7.5, 50, align="right")
    c.setStrokeColor(HexColor(RULE))
    c.setLineWidth(0.3 * mm)
    c.line(left * mm, _y(17), right * mm, _y(17))

    # 巨型序号作底纹：右对齐、在 140~235 mm 的区域内垂直居中
    c.setFillColor(HexColor(GHOST))
    c.setFont(fonts.latin_bold, 190)
    c.drawRightString(right * mm, _vcenter(140, 95, 190), "01")

    c.setFillColor(HexColor(MUTED))
    _spaced(c, left * mm, _y(64), "B站账号诊断报告", fonts.cjk, 10, 60)
    c.setFillColor(HexColor(INK))
    c.setFont(fonts.cjk_bold, 48)
    c.drawString((left - 1) * mm, _y(86), "创作者")
    c.drawString((left - 1) * mm, _y(106), "档案")
    c.setFillColor(HexColor(MUTED))
    _spaced(c, left * mm, _y(118), "CREATOR DIAGNOSIS REPORT", fonts.latin, 10, 70)
    c.setFillColor(HexColor(SIGNAL))
    c.rect(left * mm, _y(128 + 1.1), 32 * mm, 1.1 * mm, stroke=0, fill=1)

    top = PAGE_H - 52
    c.setStrokeColor(HexColor(INK))
    c.setLineWidth(0.6 * mm)
    c.line(left * mm, _y(top), right * mm, _y(top))
    column = (right - left) / 3
    items = (
        ("账号", meta.get("account") or "未获取账号名", fonts.cjk_bold),
        ("UID", meta.get("uid") or "—", fonts.latin_bold),
        ("数据采集", meta.get("fetched-at") or "未记录", fonts.latin_bold),
    )
    from reportlab.pdfbase.pdfmetrics import stringWidth

    for index, (label, value, font) in enumerate(items):
        x = left + column * index
        c.setFillColor(HexColor(MUTED))
        _spaced(c, x * mm, _y(top + 8), label, fonts.cjk, 7.5, 30)
        c.setFillColor(HexColor(INK))
        size, limit = 14.0, (column - 4) * mm
        while size > 8 and stringWidth(value, font, size) > limit:
            size -= 0.5
        c.setFont(font, size)
        c.drawString(x * mm, _y(top + 10) - size, value)
    c.restoreState()


def draw_frame(canvas, meta: dict[str, str], fonts: Fonts, page_no: int, total: int) -> None:
    """正文页：档案脊、页眉、页脚与页码（_draw_frame）。"""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm

    c = canvas
    c.saveState()
    c.setFillColor(HexColor(INK))
    c.rect(0, 0, SPINE_MM * mm, PAGE_H * mm, stroke=0, fill=1)
    c.setFillColor(HexColor(SIGNAL))
    c.rect(0, _y(14 + 1.6), SPINE_MM * mm, 1.6 * mm, stroke=0, fill=1)
    x0, x1 = BODY_LEFT_MM, PAGE_W - BODY_RIGHT_MM

    account = meta.get("account") or ""
    uid = meta.get("uid", "—")
    head = f"{account} · UID {uid}" if account else f"UID {uid}"
    c.setFillColor(HexColor(MUTED))
    base = _vcenter(0, 14, 7.5)
    _spaced(c, x0 * mm, base, head, fonts.cjk, 7.5, 25)
    _spaced(c, x1 * mm, base, "SELF-DIAGNOSIS DOSSIER", fonts.latin, 7.5, 25, align="right")
    c.setStrokeColor(HexColor(RULE))
    c.setLineWidth(0.3 * mm)
    c.line(x0 * mm, _y(14), x1 * mm, _y(14))

    foot_top = PAGE_H - 12
    c.line(x0 * mm, _y(foot_top), x1 * mm, _y(foot_top))
    base = _vcenter(foot_top, 12, 7)
    c.setFillColor(HexColor(FAINT))
    _spaced(c, x0 * mm, base, f"FishTool · 生成于 {meta.get('generated-at', '')}", fonts.cjk, 7, 25)
    c.setFont(fonts.latin_bold, 7.5)
    c.drawRightString(x1 * mm, base, f"{page_no:02d} / {total:02d}")
    c.restoreState()

# ---------------------------------------------------------------- 入口
def body_width_pt() -> float:
    from reportlab.lib.units import mm

    return (PAGE_W - BODY_LEFT_MM - BODY_RIGHT_MM) * mm


def build(html_text: str, fonts: Fonts, *, charts: list | None = None, title: str = "B站账号诊断报告") -> bytes:
    """主题 HTML → PDF 字节。

    Args:
        html_text: ``/api/analysis/report-html`` 返回的 ``data.html``。
        fonts: ``resolve_fonts()`` 的结果。
        charts: 插在第一个分隔线后的图表 flowables（report_charts.build_chart_flowables），可为空。
        title: PDF 元数据标题。

    Raises:
        ReportPdfError: 内容为空或缺少 reportlab。
    """
    if not html_text or not html_text.strip():
        raise ReportPdfError("报告内容为空，无法生成 PDF")
    try:
        from io import BytesIO

        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.platypus import BaseDocTemplate, Frame, NextPageTemplate, PageBreak, PageTemplate
    except ImportError as exc:
        raise ReportPdfError("当前 AstrBot 环境缺少 reportlab，请执行 pip install reportlab") from exc

    meta = parse_meta(html_text)
    blocks = parse_blocks(html_text)
    if not blocks:
        raise ReportPdfError("报告正文为空，无法生成 PDF")

    class NumberedCanvas(Canvas):
        """先缓存每页状态，save() 时知道总页数再统一补画页框（只排版一次，不用 multiBuild）。"""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages: list[dict] = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for page_no, state in enumerate(self._pages, 1):
                self.__dict__.update(state)
                if page_no == 1:
                    draw_cover(self, meta, fonts)
                else:
                    # 封面算第 1 页但不画页脚；正文从 02 开始（desktop/report_export.py 的编号规则）
                    draw_frame(self, meta, fonts, page_no, total)
                super().showPage()
            super().save()

    buffer = BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=A4, title=title, author="FishTool", creator="FishTool",
        leftMargin=BODY_LEFT_MM * mm, rightMargin=BODY_RIGHT_MM * mm,
        topMargin=BODY_TOP_MM * mm, bottomMargin=BODY_BOTTOM_MM * mm,
    )
    full = Frame(0, 0, PAGE_W * mm, PAGE_H * mm, id="cover", leftPadding=0, rightPadding=0,
                 topPadding=0, bottomPadding=0)
    body = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body",
                 leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="cover", frames=[full]), PageTemplate(id="body", frames=[body])])
    story = [NextPageTemplate("body"), PageBreak()]
    story += build_story(blocks, fonts, doc.width, charts=charts)
    try:
        doc.build(story, canvasmaker=NumberedCanvas)
    except ReportPdfError:
        raise
    except Exception as exc:  # noqa: BLE001 - reportlab 的 LayoutError/TableTooLarge 等统一转成可读的导出错误
        raise ReportPdfError(f"报告排版失败：{type(exc).__name__}（内容块过大或表格过宽）") from exc
    return buffer.getvalue()


def report_filename(meta: dict[str, str], today: str) -> str:
    """``FishTool自诊_<UID>_<日期>.pdf``；UID 只保留数字。"""
    uid = re.sub(r"\D", "", meta.get("uid", "")) or "unknown"
    return f"FishTool自诊_{uid}_{today}.pdf"
