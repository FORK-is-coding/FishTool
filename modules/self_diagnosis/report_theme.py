"""账号自诊报告的打印主题（方案 A「终端白」）。

视觉规则：
- 冷白纸面 + 炭黑文字 + 单一警示黄强调；全部使用无衬线字体
  （数字/英文 Bahnschrift，中文 Microsoft YaHei，均为 Windows 自带）。
- 签名元素：每页左侧黑色「档案脊」竖条；章节标题为黑底白字斜切角标签；
  关键段落左侧黄色竖线。
- 只用 Chromium 打印（QWebEngine printToPdf / 浏览器另存为 PDF）可稳定渲染的 CSS：
  分页用 ``@page`` 与 ``break-*``，每页重复元素用 ``position: fixed``。

本模块只负责「样式 + 封面/页眉页脚外壳」。正文仍由 Markdown 渲染，
由 ``ReportGenerator.generate_html_report`` 负责 HTML 转义与图片剥离（安全边界不在此处）。
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, Optional

#: 主题名，写入 HTML 便于排查是哪一版样式。
THEME_NAME = "terminal-white"

#: 页面尺寸与边距：左侧留出档案脊的位置。
_PAGE_MARGIN_MM = {"top": 20, "right": 16, "bottom": 18, "left": 26}

REPORT_PRINT_CSS = """
@page { size: A4; margin: 0; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: #ffffff; }
body {
    font-family: "Bahnschrift", "Microsoft YaHei", "DengXian", "Noto Sans SC", "Segoe UI", sans-serif;
    color: #1d1f21;
    font-size: 10.5pt;
    line-height: 1.7;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
}

/* ---------- 每页重复：档案脊、页眉、页脚（@page 无边距，fixed 以纸张为参照逐页重复） ---------- */
.spine { position: fixed; left: 0; top: 0; width: 13mm; height: 297mm; background: #1d1f21; }
.spine i { position: absolute; left: 0; right: 0; top: 14mm; height: 1.6mm; background: #f5c400; }
.running-head {
    position: fixed; top: 0; left: 26mm; right: 16mm; height: 14mm; line-height: 14mm;
    border-bottom: 0.3mm solid #c9ccc8; color: #6b6f72; background: #ffffff;
    font-size: 7.5pt; letter-spacing: 0.25em; text-transform: uppercase;
}
.running-head .l { float: left; }
.running-head .r { float: right; }
.running-foot {
    position: fixed; bottom: 0; left: 26mm; right: 16mm; height: 12mm; line-height: 12mm;
    border-top: 0.3mm solid #c9ccc8; color: #8a8e91; background: #ffffff;
    font-size: 7pt; letter-spacing: 0.25em; text-transform: uppercase;
}

/* ---------- 封面：独占第一页，满版 ---------- */
.cover {
    position: relative; z-index: 10; width: 210mm; height: 297mm; background: #f3f4f2;
    break-after: page; page-break-after: always; overflow: hidden;
}
.cover .c-spine { position: absolute; left: 0; top: 0; bottom: 0; width: 15mm; background: #1d1f21; }
.cover .c-spine i { position: absolute; left: 0; right: 0; top: 17mm; height: 1.6mm; background: #f5c400; }
.cover .c-spine .vt {
    position: absolute; left: 4.6mm; top: 30mm; width: 6mm; color: #f3f4f2;
    font-size: 7.5pt; letter-spacing: 0.35em; writing-mode: vertical-rl; text-transform: uppercase;
}
.cover .c-body { position: absolute; left: 15mm; right: 0; top: 0; bottom: 0; padding: 0 15mm; }
.cover .c-top {
    height: 17mm; line-height: 17mm; border-bottom: 0.3mm solid #c9ccc8;
    color: #6b6f72; font-size: 7.5pt; letter-spacing: 0.3em; text-transform: uppercase;
}
.cover .c-top .r { float: right; }
.cover .kicker { margin-top: 42mm; color: #6b6f72; font-size: 10pt; letter-spacing: 0.6em; }
.cover h1.c-title {
    margin: 5mm 0 0; padding: 0; border: 0; color: #1d1f21;
    font-size: 50pt; font-weight: 700; line-height: 1.04; letter-spacing: -0.02em;
}
.cover .c-en { margin-top: 5mm; color: #6b6f72; font-size: 10pt; letter-spacing: 0.7em; text-transform: uppercase; }
.cover .c-rule { margin-top: 12mm; width: 32mm; height: 1.1mm; background: #f5c400; }
.cover .c-big {
    position: absolute; right: 12mm; top: 150mm; color: #e3e5e1;
    font-size: 200pt; font-weight: 700; line-height: 1; font-family: "Bahnschrift", "Segoe UI", sans-serif;
}
.cover .c-meta { position: absolute; left: 30mm; right: 15mm; bottom: 30mm; border-top: 0.6mm solid #1d1f21; }
.cover .c-meta table { width: 100%; border-collapse: collapse; margin: 0; page-break-inside: auto; }
.cover .c-meta td {
    width: 33.3%; padding: 4mm 0 0; border: 0; background: transparent; vertical-align: top;
}
.cover .c-meta .k { color: #6b6f72; font-size: 7.5pt; letter-spacing: 0.3em; }
.cover .c-meta .v { margin-top: 1.5mm; font-size: 15pt; font-weight: 600; font-family: "Bahnschrift", "Microsoft YaHei", sans-serif; }

/* ---------- 正文 ---------- */
.report {
    width: 100%; margin: 0; padding: 20mm 16mm 18mm 26mm;
    -webkit-box-decoration-break: clone; box-decoration-break: clone;
}
.report h1 { display: none; }  /* 标题已在封面呈现，正文不重复 */
.report > p:first-of-type { color: #6b6f72; font-size: 9pt; }
h2 {
    position: relative; margin: 9mm 0 4mm; padding: 1.6mm 9mm 1.6mm 3.5mm;
    display: inline-block; min-width: 48mm;
    background: #1d1f21; color: #f3f4f2; font-size: 11pt; font-weight: 600; letter-spacing: 0.2em;
    break-after: avoid; page-break-after: avoid;
}
h2::after {
    content: ""; position: absolute; top: 0; right: -4mm; width: 0; height: 0;
    border-top: 4.4mm solid transparent; border-bottom: 4.4mm solid transparent; border-left: 4mm solid #1d1f21;
}
h2.ai-section { background: #f5c400; color: #1d1f21; }
h2.ai-section::after { border-left-color: #f5c400; }
h3 {
    margin: 5mm 0 2mm; color: #1d1f21; font-size: 10.5pt; font-weight: 600; letter-spacing: 0.08em;
    break-after: avoid; page-break-after: avoid;
}
p { margin: 1.5mm 0; }
strong { color: #1d1f21; font-weight: 600; }
ul, ol { margin: 1.5mm 0 3mm; padding-left: 0; list-style: none; }
li {
    margin: 0; padding: 1.6mm 0; border-bottom: 0.25mm solid #e3e5e1;
    break-inside: avoid; page-break-inside: avoid;
}
li:last-child { border-bottom: 0; }
ol { counter-reset: n; }
ol > li { counter-increment: n; padding-left: 8mm; position: relative; }
ol > li::before {
    content: counter(n, decimal-leading-zero); position: absolute; left: 0; top: 1.6mm;
    color: #9a6b00; font-family: "Bahnschrift", "Segoe UI", sans-serif; font-weight: 600;
}
hr { border: 0; height: 0; margin: 3mm 0; }
blockquote {
    margin: 3mm 0; padding: 1.5mm 0 1.5mm 4mm; color: #3a3d40;
    border-left: 1mm solid #f5c400; background: transparent;
}
blockquote p { margin: 0; }
table {
    width: 100%; margin: 3mm 0 5mm; border-collapse: collapse; font-size: 9.5pt;
    break-inside: avoid; page-break-inside: avoid;
}
th {
    text-align: left; padding: 2mm 2.5mm; color: #6b6f72; font-size: 8pt; font-weight: 600;
    letter-spacing: 0.15em; border-bottom: 0.6mm solid #1d1f21; background: transparent;
}
td { padding: 2mm 2.5mm; border-bottom: 0.25mm solid #d9dcd8; vertical-align: top; }
tr:nth-child(even) td { background: transparent; }
code { padding: 0 1mm; background: #eceeea; font-family: "Bahnschrift", Consolas, monospace; font-size: 9pt; }
pre { padding: 3mm; background: #f3f4f2; border-left: 1mm solid #1d1f21; white-space: pre-wrap; }
a { color: #1d1f21; text-decoration: none; border-bottom: 0.3mm solid #f5c400; }

/* ---------- 图表（前端合成的 data URI 图片） ---------- */
.report-charts { margin: 4mm 0 2mm; }
.report-chart { margin: 0 0 5mm; text-align: center; break-inside: avoid; page-break-inside: avoid; }
.report-chart img { width: 100%; max-width: 168mm; height: auto; border: 0.3mm solid #c9ccc8; }
.report-chart-caption { margin: 1.5mm 0 0; color: #6b6f72; font-size: 8pt; letter-spacing: 0.2em; text-align: center; }

/* 屏幕预览（浏览器直接打开 HTML）时：纸张居中，逐页重复的档案脊/页眉页脚只在打印时出现 */
@media screen {
    body { background: #2b2b2b; }
    .cover, .report { background: #ffffff; margin: 0 auto 8mm; }
    .report { max-width: 210mm; }
    .spine, .running-head, .running-foot { display: none; }
}
"""

#: 装饰 emoji 与符号的码位范围（与 Markdown 报告共用文案；打印版统一去掉，方舟风格不用 emoji）：
#: 杂项技术符号（⏰⌛ 等）、箭头/几何/杂项符号与 Dingbats（✅⚠️❌ℹ️ 等）、补充符号区（🎉👍💪🏆 等），
#: 以及变体选择符 U+FE0F 与零宽连接符 U+200D。
_EMOJI_CHARS = (
    "⌀-⏿"
    "←-⇿①-➿⤀-⥿⬀-⯿"
    "\U0001F000-\U0001FAFF"
    "️‍"
)
_EMOJI_PREFIX = re.compile(f"^[\\s{_EMOJI_CHARS}]+")
#: 正文中的状态 emoji，替换为空，避免打印时缺字成方块。
_INLINE_EMOJI = re.compile(f"[{_EMOJI_CHARS}]")

#: AI 相关章节标题：用黄底标签区分「模型生成」与「数据统计」。
_AI_SECTION_TITLES = ("AI 调研报告", "运营点评")


#: 账号名等外部文本里的 Markdown 链接/图片语法与 URL：封面只展示可读名称，不回显这些片段。
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"\b(?:file|https?|data|javascript):\S*", re.IGNORECASE)


def _plain_label(value: Any, fallback: str, limit: int = 40) -> str:
    """把外部文本整理成封面/页眉用的纯文本标签（未转义，调用方负责 html.escape）。

    去掉 Markdown 链接与图片语法、URL 和控制字符，收拢空白并截断，
    避免账号名里的 ``![x](file:///...)`` 之类片段以原文出现在封面和每页页眉。
    """
    text = _MD_LINK.sub(lambda m: m.group(1), str(value or ""))
    text = _URL.sub("", text)
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return fallback
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strip_emoji(text: str) -> str:
    """去掉标题前缀与正文里的装饰 emoji，并收拢多余空白。"""
    text = _EMOJI_PREFIX.sub("", text)
    text = _INLINE_EMOJI.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def decorate_body(body_html: str) -> str:
    """给 Markdown 产出的正文 HTML 套主题：标题去 emoji、AI 章节加标记类。

    只处理 ``<h2>`` / ``<h3>`` 的文本内容与正文中的 emoji 字符，不引入任何来自数据的新标签；
    传入的 HTML 必须已经过 ``safe_mode='escape'`` 与图片剥离。

    Args:
        body_html: 已转义、已剥离图片的正文 HTML。

    Returns:
        套用主题后的正文 HTML。
    """

    def _h2(match: "re.Match[str]") -> str:
        title = _strip_emoji(match.group(1))
        css = ' class="ai-section"' if any(key in title for key in _AI_SECTION_TITLES) else ""
        return f"<h2{css}>{title}</h2>"

    def _h3(match: "re.Match[str]") -> str:
        return f"<h3>{_strip_emoji(match.group(1))}</h3>"

    body_html = re.sub(r"<h2>(.*?)</h2>", _h2, body_html, flags=re.S)
    body_html = re.sub(r"<h3>(.*?)</h3>", _h3, body_html, flags=re.S)
    return _INLINE_EMOJI.sub("", body_html)


def _cover_html(self_data: Dict[str, Any], generated_at: str, fetched_at: str) -> str:
    """封面：报告名、账号、UID、数据采集时间。所有数据字段一律 HTML 转义。"""
    basic = self_data.get("basic_info") if isinstance(self_data.get("basic_info"), dict) else {}
    name = html.escape(_plain_label(basic.get("name"), "未获取账号名"))
    uid = html.escape(_plain_label(self_data.get("uid"), "—", limit=20))
    return (
        '<section class="cover">'
        '<div class="c-spine"><i></i><div class="vt">Rhodes · Creator · Dossier</div></div>'
        '<div class="c-body">'
        f'<div class="c-top">FishTool / Self-Diagnosis<span class="r">UID {uid}</span></div>'
        '<div class="kicker">B站账号诊断报告</div>'
        '<h1 class="c-title">创作者<br>档案</h1>'
        '<div class="c-en">Creator Diagnosis Report</div>'
        '<div class="c-rule"></div>'
        '<div class="c-big">01</div>'
        '</div>'
        '<div class="c-meta"><table><tr>'
        f'<td><div class="k">账号</div><div class="v">{name}</div></td>'
        f'<td><div class="k">UID</div><div class="v">{uid}</div></td>'
        f'<td><div class="k">数据采集</div><div class="v">{html.escape(fetched_at)}</div></td>'
        '</tr></table></div>'
        '</section>'
    )


def render_document(body_html: str, self_data: Dict[str, Any], *,
                    generated_at: str, fetched_at: str,
                    title: Optional[str] = None) -> str:
    """组装完整的打印 HTML 文档：封面 + 每页档案脊/页眉页脚 + 正文。

    Args:
        body_html: 已转义、已剥离图片、可含图表章节的正文 HTML。
        self_data: 账号数据（只读取 uid 与 basic_info.name 用于封面与页眉，均会转义）。
        generated_at: 报告生成时间文本。
        fetched_at: 数据采集时间文本。
        title: 文档标题（浏览器标签 / PDF 元数据），默认「B站账号诊断报告」。

    Returns:
        完整 HTML 文档字符串。
    """
    basic = self_data.get("basic_info") if isinstance(self_data.get("basic_info"), dict) else {}
    name = html.escape(_plain_label(basic.get("name"), ""))
    uid = html.escape(_plain_label(self_data.get("uid"), "—", limit=20))
    doc_title = html.escape(title or "B站账号诊断报告")
    head_label = f"{name} · UID {uid}" if name else f"UID {uid}"
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        f"<meta name=\"report-theme\" content=\"{THEME_NAME}\">\n"
        # 供桌面端原生渲染（desktop/report_export.py）绘制封面与页眉页脚；内容均已转义
        f"<meta name=\"report-account\" content=\"{name or '未获取账号名'}\">\n"
        f"<meta name=\"report-uid\" content=\"{uid}\">\n"
        f"<meta name=\"report-fetched-at\" content=\"{html.escape(fetched_at)}\">\n"
        f"<meta name=\"report-generated-at\" content=\"{html.escape(generated_at)}\">\n"
        f"<title>{doc_title}</title>\n"
        f"<style>{REPORT_PRINT_CSS}</style>\n"
        "</head>\n<body>\n"
        f"{_cover_html(self_data, generated_at, fetched_at)}\n"
        "<div class=\"spine\"><i></i></div>\n"
        f"<div class=\"running-head\"><span class=\"l\">{head_label}</span>"
        "<span class=\"r\">Self-Diagnosis Dossier</span></div>\n"
        f"<div class=\"running-foot\">FishTool · 生成于 {html.escape(generated_at)}</div>\n"
        f"<main class=\"report\">{body_html}</main>\n"
        "</body>\n</html>"
    )
