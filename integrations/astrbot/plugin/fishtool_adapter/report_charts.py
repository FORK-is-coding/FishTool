"""自诊报告里的矢量图表：用 reportlab.graphics 根据 self_data 直接画，不依赖前端截图。

与前端 web/frontend/static/js/app.up.js 的四张图一一对应（renderDiagnosis*Chart / buildDiagnosisTagCloudImage），
配色改用 PDF 的“终端白”常量。

- ``chart_specs()`` 只算数据和标签，不画，便于单测；
- ``build_chart_flowables()`` 画成 reportlab flowables，交给 report_pdf.build(charts=...)。

不 import astrbot；logger 由调用方传入。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .report_pdf import FAINT, INK, MUTED, RULE, SIGNAL, Fonts, escape_text, section_label

#: 词云最多展示的标签数（前端同样取前 40 个）。
TAG_LIMIT = 40
#: 雷达图维度：(字段, 名称)。三项都是百分比，图表按 0~100% 封顶（app.up.js engagementScores）。
ENGAGEMENT_DIMS = (
    ("play_to_fans_ratio", "粉丝触达"),
    ("comment_to_play_ratio", "评论效率"),
    ("favorite_to_play_ratio", "收藏效率"),
)
#: 雷达图网格圈（百分比）；最外圈 100 同时把刻度固定为 0~100%。
RADAR_RINGS = (25.0, 50.0, 75.0, 100.0)
#: 词云：权重（词频 / 最高词频）达到这个值的标签加粗（前端 weight >= 0.55 用 600 字重）。
BOLD_WEIGHT = 0.55
#: 模拟粗体的描边宽度（pt）。
BOLD_STROKE_PT = 0.3
#: 词云 5 色轮换；黄色只给词频最高的那个词，避免满屏黄。
TAG_COLORS = (INK, MUTED, "#3a3d40", FAINT)


def _num(value) -> float | None:
    """可空数值：None / 布尔 / 非数字都算缺失（前端 diagnosisNullableNumber）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def fmt_count(value: float | None) -> str:
    """播放量等大数：万 / 亿 单位；缺失显示“暂无”。"""
    if value is None:
        return "暂无"
    if abs(value) >= 1e8:
        return f"{value / 1e8:.1f}亿"
    if abs(value) >= 1e4:
        return f"{value / 1e4:.1f}万"
    return f"{value:,.0f}"


def fmt_percent(value: float | None) -> str:
    if value is None:
        return "暂无数据"
    return f"{value:.2f}".rstrip("0").rstrip(".") + "%"


@dataclass
class ChartSpec:
    kind: str  # volume / engagement / benchmark / tag_cloud
    title: str
    labels: list[str] = field(default_factory=list)
    values: list[float | None] = field(default_factory=list)
    highlight: int | None = None  # 高亮的柱子下标（“我的均播”）
    note: str = ""


def chart_specs(self_data: dict, benchmark: dict | None = None) -> list[ChartSpec]:
    """根据 self_data 算出要画的图；没有数据的图不出现。"""
    self_data = self_data if isinstance(self_data, dict) else {}
    avail = self_data.get("data_availability") if isinstance(self_data.get("data_availability"), dict) else {}
    specs: list[ChartSpec] = []

    videos = self_data.get("video_stats") if isinstance(self_data.get("video_stats"), dict) else {}
    if avail.get("video_stats") and videos:
        values = [_num(videos.get(k)) for k in ("total_play", "total_comment", "total_favorite")]
        if any(v is not None for v in values):
            specs.append(ChartSpec("volume", "内容互动总量", ["累计播放", "累计评论", "累计收藏"], values))

    engagement = self_data.get("engagement_metrics") if isinstance(self_data.get("engagement_metrics"), dict) else {}
    if avail.get("engagement_metrics") and engagement:
        raws = [_num(engagement.get(key)) for key, _ in ENGAGEMENT_DIMS]
        if any(v is not None for v in raws):
            labels = [f"{name}\n{fmt_percent(raw)}" for (_, name), raw in zip(ENGAGEMENT_DIMS, raws)]
            specs.append(ChartSpec("engagement", "互动效率 · 三角维度", labels, raws))

    if isinstance(benchmark, dict) and benchmark.get("has_benchmark"):
        metrics = benchmark.get("category_metrics") if isinstance(benchmark.get("category_metrics"), dict) else {}
        mine = (benchmark.get("self_metrics") or {}).get("avg_play") if isinstance(benchmark.get("self_metrics"), dict) else None
        values = [_num(metrics.get(k)) for k in ("p25", "p50", "p75")] + [_num(mine)]
        if any(v is not None for v in values):
            category = str(benchmark.get("category") or "")
            specs.append(ChartSpec("benchmark", f"{category}分区播放基准" if category else "分区播放基准",
                                   ["P25", "P50", "P75", "我的均播"], values, highlight=3))

    cloud = self_data.get("tag_cloud") if isinstance(self_data.get("tag_cloud"), dict) else {}
    freq = cloud.get("word_frequency") if isinstance(cloud.get("word_frequency"), dict) else {}
    entries = [(str(tag), _num(count) or 1.0) for tag, count in list(freq.items())[:TAG_LIMIT] if str(tag).strip()]
    if entries:
        note = (f"已打标签视频 {cloud.get('tagged_video_count', '—')}/{cloud.get('video_count', '—')}"
                f" · 标签 {cloud.get('tag_count', len(entries))} 个")
        specs.append(ChartSpec("tag_cloud", "总体视频 Tag 词云",
                               [t for t, _ in entries], [c for _, c in entries], note=note))
    return specs

# ---------------------------------------------------------------- 画图
def _bar_drawing(spec: ChartSpec, width: float, height: float, fonts: Fonts):
    """柱状图：线性轴（与前端一致），柱顶标数值；缺失值画 0 高、标“暂无”。"""
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.colors import HexColor

    drawing = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x, chart.y = 40, 22
    chart.width, chart.height = width - 52, height - 40
    chart.data = [[v if v is not None else 0 for v in spec.values]]
    chart.categoryAxis.categoryNames = spec.labels
    chart.categoryAxis.labels.fontName = fonts.cjk
    chart.categoryAxis.labels.fontSize = 8
    chart.categoryAxis.labels.fillColor = HexColor(MUTED)
    chart.categoryAxis.strokeColor = HexColor(RULE)
    chart.valueAxis.valueMin = 0
    peak = max((v for v in spec.values if v is not None), default=0)
    if peak <= 0:
        chart.valueAxis.valueMax = 1
    # 刻度和柱顶标签含“万/亿/暂无”，必须用中文字体（Bahnschrift 没有中文字形，会显示方块）
    chart.valueAxis.labels.fontName = fonts.cjk
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.labels.fillColor = HexColor(MUTED)
    chart.valueAxis.labelTextFormat = lambda v: fmt_count(v)
    chart.valueAxis.strokeColor = HexColor(RULE)
    chart.valueAxis.visibleGrid = True
    chart.valueAxis.gridStrokeColor = HexColor(RULE)
    chart.valueAxis.gridStrokeDashArray = (2, 2)
    chart.valueAxis.gridStrokeWidth = 0.4
    chart.bars[0].fillColor = HexColor(INK)
    chart.bars[0].strokeColor = None
    if spec.highlight is not None:
        chart.bars[(0, spec.highlight)].fillColor = HexColor(SIGNAL)
    chart.barWidth = 18
    chart.groupSpacing = 14
    # 'values' + barLabelArray：每根柱子用自己的标签（缺失值显示“暂无”而不是 0）
    chart.barLabelFormat = "values"
    chart.barLabelArray = [[fmt_count(v) for v in spec.values]]
    chart.barLabels.fontName = fonts.cjk
    chart.barLabels.fontSize = 7
    chart.barLabels.fillColor = HexColor(INK)
    chart.barLabels.nudge = 6
    drawing.add(chart)
    return drawing


def _spider_drawing(spec: ChartSpec, width: float, height: float, fonts: Fonts):
    """雷达图：固定 0~100% 刻度，带 25/50/75/100 四圈网格（前端 splitNumber=4）。

    reportlab 的 SpiderChart 用所有数据的最大值当 100%，三个维度都只有百分之几时会被撑满、看起来像满分。
    额外加 25/50/75/100 四条网格线：100 那条把刻度钉住（实测 _norm 固定为 100），四条都只画浅灰描边、
    不填充、不画顶点。网格线会让 strandLabels 多画出数值，所以不用 strandLabels，百分比写在维度名里。
    SpiderChart 不接受 None：缺失维度按 0 画，维度名里写“暂无数据”。
    """
    from reportlab.graphics.charts.spider import SpiderChart
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.colors import Color, HexColor

    drawing = Drawing(width, height)
    chart = SpiderChart()
    size = min(width, height) - 70
    chart.x, chart.y = (width - size) / 2, (height - size) / 2 - 4
    chart.width = chart.height = size
    values = [min(100.0, max(0.0, v)) if v is not None else 0.0 for v in spec.values]
    chart.data = [values] + [[ring] * len(values) for ring in RADAR_RINGS]
    chart.labels = spec.labels
    chart.startAngle = 90
    chart.direction = "clockwise"
    chart.spokes.strokeColor = HexColor(RULE)
    chart.spokes.strokeWidth = 0.6
    chart.spokes.labelRadius = 1.22  # 默认 1.05 时两行维度名会压在外圈网格上
    chart.spokeLabels.fontName = fonts.cjk
    chart.spokeLabels.fontSize = 8
    chart.spokeLabels.fillColor = HexColor(INK)
    chart.spokeLabels.leading = 10
    # 标签框朝外贴住顶点：正上方的贴底边，左下贴右上角，右下贴左上角（startAngle=90、顺时针三等分）
    for index, anchor in enumerate(("s", "nw", "ne")[: len(values)] if len(values) == 3 else ()):
        chart.spokeLabels[index].boxAnchor = anchor
    signal = HexColor(SIGNAL)
    chart.strands[0].strokeColor = HexColor(INK)
    chart.strands[0].strokeWidth = 1.5
    chart.strands[0].fillColor = Color(signal.red, signal.green, signal.blue, alpha=0.28)
    for index in range(1, len(RADAR_RINGS) + 1):
        chart.strands[index].strokeColor = HexColor(RULE)
        chart.strands[index].strokeWidth = 0.5
        chart.strands[index].fillColor = None
        chart.strands[index].symbol = None
    drawing.add(chart)
    return drawing


def _tag_cloud_drawing(spec: ChartSpec, width: float, fonts: Fonts):
    """词云：按词频线性映射字号、按行居中排布（与前端 buildDiagnosisTagCloudImage 同一确定性算法）。

    标签文字来自 B 站用户输入；String 是纯文本绘制，不经过任何标记解析。
    """
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase.pdfmetrics import stringWidth

    padding, gap, line_gap = 10.0, 9.0, 4.0
    usable = width - padding * 2
    peak = max(spec.values) if spec.values else 1.0
    items = []
    for index, (tag, count) in enumerate(zip(spec.labels, spec.values)):
        weight = count / peak if peak else 0
        size = 9 + weight * 13
        text_w = stringWidth(tag, fonts.cjk_bold, size)
        while text_w > usable and size > 8:
            size -= 0.5
            text_w = stringWidth(tag, fonts.cjk_bold, size)
        color = SIGNAL if index == 0 else TAG_COLORS[index % len(TAG_COLORS)]
        items.append((tag, size, text_w, color, weight >= BOLD_WEIGHT))

    lines, line, line_w = [], [], 0.0
    for item in items:
        nxt = line_w + gap + item[2] if line else item[2]
        if line and nxt > usable:
            lines.append(line)
            line, nxt = [], item[2]
        line.append(item)
        line_w = nxt
    if line:
        lines.append(line)

    heights = [max(i[1] for i in ln) * 1.35 for ln in lines]
    total_h = sum(heights) + line_gap * max(0, len(lines) - 1) + padding * 2
    drawing = Drawing(width, total_h)
    y = total_h - padding
    for ln, row_h in zip(lines, heights):
        row_w = sum(i[2] for i in ln) + gap * max(0, len(ln) - 1)
        x = (width - row_w) / 2
        baseline = y - row_h / 2 - max(i[1] for i in ln) * 0.35
        for tag, size, text_w, color, bold in ln:
            drawing.add(_tag_string(x, baseline, tag, fonts.cjk_bold, size, HexColor(color), bold))
            x += text_w + gap
        y -= row_h + line_gap
    return drawing


def _tag_string(x: float, y: float, text: str, font: str, size: float, color, bold: bool):
    """词云里的一个标签。高频词（权重 ≥ 0.55，前端用 600 字重）要加粗。

    插件只注册了一套字形（reportlab 不能合成 TTF 粗体），方案 9.7.4 定的做法是加一圈 0.3 pt 同色描边模拟粗体：
    PDF 文字渲染模式 2（填充 + 描边）。graphics 的 String / Group 都没有描边属性，renderPDF 画 String 时
    描边颜色取自渲染状态，而状态只能由带 strokeColor 的图形（Path 等）设置——所以粗体标签前面放一条
    零长度、带同色描边的 Path，把描边状态设好，再画 textRenderMode=2 的 String。文字只画一遍，
    pypdf 抽取不会重复；零长度路径不可见。
    """
    from reportlab.graphics.shapes import Group, Path, String

    label = String(x, y, text, fontName=font, fontSize=size, fillColor=color)
    if not bold:
        return label
    label.textRenderMode = 2
    # Group 结束时 renderPDF 会恢复外层状态，primer 的描边不会影响后续图形。
    # 颜色同时设置：String 的 fillColor 已经是 color，描边色由 primer 写入状态，两者一致。
    primer = Path(strokeColor=color, strokeWidth=BOLD_STROKE_PT, fillColor=None, strokeLineJoin=1)
    primer.moveTo(x, y)
    primer.lineTo(x, y)
    return Group(primer, label)


def _panel(title: str, drawing, width: float, fonts: Fonts, note: str = ""):
    """图表面板：白底、0.3 mm 浅灰描边、左上角图名（前端 drawReportChartPanel，直角）。"""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    head_style = ParagraphStyle("ft_chart_head", fontName=fonts.cjk_bold, fontSize=9.5, leading=13,
                                textColor=HexColor(INK))
    note_style = ParagraphStyle("ft_chart_note", fontName=fonts.cjk, fontSize=7.5, leading=10,
                                textColor=HexColor(MUTED), alignment=2)
    head = Table([[Paragraph(escape_text(title), head_style), Paragraph(escape_text(note), note_style) if note else ""]],
                 colWidths=[width * 0.55, width * 0.45 - 6 * mm])
    head.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                              ("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    panel = Table([[head], [drawing]], colWidths=[width])
    panel.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.3 * mm, HexColor(RULE)),
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#ffffff")),
        ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("TOPPADDING", (0, 0), (0, 0), 2.5 * mm), ("BOTTOMPADDING", (0, -1), (-1, -1), 2.5 * mm),
        ("ALIGN", (0, 1), (-1, 1), "CENTER"),
    ]))
    return panel


def build_chart_flowables(self_data: dict, benchmark: dict | None, width: float, fonts: Fonts, logger) -> list:
    """“数据可视化”章节的全部 flowables；没有可画的数据时返回空列表（不出这一章）。

    单张图画失败只跳过那一张并记 warning，不影响整份 PDF。
    """
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Spacer, Table, TableStyle

    specs = chart_specs(self_data, benchmark)
    gap = 4 * mm
    half = (width - gap) / 2
    panels: dict[str, object] = {}
    for spec in specs:
        try:
            if spec.kind in ("volume", "engagement"):
                inner_w = half - 6 * mm
                drawing = (_bar_drawing(spec, inner_w, 150, fonts) if spec.kind == "volume"
                           else _spider_drawing(spec, inner_w, 150, fonts))
                panels[spec.kind] = _panel(spec.title, drawing, half, fonts)
            elif spec.kind == "benchmark":
                panels[spec.kind] = _panel(spec.title, _bar_drawing(spec, width - 6 * mm, 140, fonts), width, fonts)
            else:
                drawing = _tag_cloud_drawing(spec, width - 6 * mm, fonts)
                panels[spec.kind] = _panel(spec.title, drawing, width, fonts, note=spec.note)
        except Exception as exc:  # noqa: BLE001 - 单张图失败只跳过这一张
            logger.warning(f"[fishtool] 自诊报告图表 {spec.kind} 绘制失败，已跳过: {exc!r}")
    if not panels:
        return []

    out: list = [section_label("数据可视化", ai=False, fonts=fonts)]
    top = [panels[k] for k in ("volume", "engagement") if k in panels]
    if len(top) == 2:
        row = Table([top], colWidths=[half + gap, half])
        row.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, 0), gap),
                                 ("RIGHTPADDING", (1, 0), (1, 0), 0), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        out.append(KeepTogether([out.pop(), row]))
    elif top:
        out.append(KeepTogether([out.pop(), top[0]]))
    for key in ("benchmark", "tag_cloud"):
        if key in panels:
            out += [Spacer(1, gap), KeepTogether([panels[key]])]
    out.append(Spacer(1, 2 * mm))
    return out
