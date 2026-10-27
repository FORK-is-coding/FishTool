"""前端 XSS 静态契约测试（Refs #5 第 2 条）。

前端没有 JS 测试框架，按仓库惯例读取 JS/HTML 源码做静态断言：

1. 内联事件属性里不得用引号包裹 ``${escapeHtml(...)}`` 当 JS 字符串参数：
   浏览器先解 HTML 实体再解析 JS，``&#39;`` 会还原成 ``'``，可跳出 JS 字符串；
   应改用 ``jsArg``（JSON 字面量 + HTML 属性转义）。
2. ``escapeHtml`` / ``jsArg`` 只在 app.core.js 定义一次，且 app.core.js 先于其他脚本加载。
3. 已知未转义直插字段（分区名、采集失败原因、账号 mid）已收敛。
4. 有 node 时，实际执行 ``escapeHtml + jsArg`` 并模拟浏览器实体解码，确认参数原样还原。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_FRONTEND_DIR = Path(__file__).resolve().parents[1] / "web" / "frontend"
_JS_DIR = _FRONTEND_DIR / "static" / "js"
_INDEX_HTML = _FRONTEND_DIR / "templates" / "index.html"

# 内联事件属性（onclick="..." 等）的完整属性值。
_INLINE_HANDLER_RE = re.compile(r'\bon[a-z]+="([^"]*)"')
# 属性值中用单引号包裹插值：'${...'（数字收敛 '${Number(...)' 除外）。
_QUOTED_INTERPOLATION_RE = re.compile(r"'\$\{(?!Number\()")
# 直接命中 issue 写法：on*="...'${escapeHtml(..."。
_QUOTED_ESCAPE_HTML_RE = re.compile(r"""\bon[a-z]+="[^"]*'\$\{escapeHtml\(""")


def _frontend_sources() -> dict[str, str]:
    files = sorted(_JS_DIR.glob("*.js")) + [_INDEX_HTML]
    return {path.name: path.read_text(encoding="utf-8") for path in files}


def _line_of(source: str, index: int) -> int:
    return source.count("\n", 0, index) + 1


def test_inline_handler_does_not_quote_escape_html() -> None:
    """内联事件属性中不得出现 '${escapeHtml(...)}' 形式的 JS 字符串参数。"""
    hits = [
        f"{name}:{_line_of(source, m.start())}"
        for name, source in _frontend_sources().items()
        for m in _QUOTED_ESCAPE_HTML_RE.finditer(source)
    ]
    assert hits == [], f"内联事件参数应改用 jsArg：{hits}"


def test_inline_handler_has_no_quoted_string_interpolation() -> None:
    """内联事件属性中所有字符串插值都走 jsArg；仅 '${Number(...)}' 数字收敛允许带引号。"""
    hits = []
    for name, source in _frontend_sources().items():
        for attr in _INLINE_HANDLER_RE.finditer(source):
            if _QUOTED_INTERPOLATION_RE.search(attr.group(1)):
                hits.append(f"{name}:{_line_of(source, attr.start())}")
    assert hits == [], f"内联事件中存在引号包裹的字符串插值：{hits}"


def test_escape_html_and_js_arg_defined_once_in_core() -> None:
    """escapeHtml / jsArg 各只定义一次，且都位于 app.core.js。"""
    for func in ("escapeHtml", "jsArg"):
        pattern = re.compile(rf"\bfunction {func}\s*\(")
        owners = [
            (name, len(pattern.findall(source)))
            for name, source in _frontend_sources().items()
            if pattern.search(source)
        ]
        assert owners == [("app.core.js", 1)], f"{func} 定义位置异常：{owners}"


def test_core_script_loads_before_other_app_scripts() -> None:
    """app.core.js 必须先于其他 app.*.js 及内联脚本加载，共享工具函数才可用。"""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="/static/js/(app\.[a-z]+\.js)', html)
    assert scripts and scripts[0] == "app.core.js"
    core_pos = html.index("/static/js/app.core.js")
    inline_positions = [m.start() for m in re.finditer(r"<script>\s", html)]
    assert inline_positions, "index.html 应包含内联脚本（watch / 发现区块）"
    assert all(core_pos < pos for pos in inline_positions)


def test_known_raw_fields_are_escaped() -> None:
    """分区名、采集失败明细、账号 mid 不再裸插 HTML。"""
    up_js = (_JS_DIR / "app.up.js").read_text(encoding="utf-8")
    hotspot_js = (_JS_DIR / "app.hotspot.js").read_text(encoding="utf-8")
    assert "${z.name}" not in up_js
    assert "${result.category}" not in up_js
    assert "${f.error}" not in hotspot_js
    assert "${f.bvid}" not in hotspot_js
    assert "${d.mid ??" not in hotspot_js
    # safeUrl 为空时不渲染空 href 的 a 标签。
    assert '<a href="${safeUrl(act.link)}"' in hotspot_js
    assert "${safeUrl(act.link) ?" in hotspot_js


def _extract_core_helpers() -> str:
    core = (_JS_DIR / "app.core.js").read_text(encoding="utf-8")
    escape = re.search(r"function escapeHtml\(value\) \{.*?\n\}", core, re.S)
    js_arg = re.search(r"function jsArg\(value\) \{[^\n]*\}", core)
    assert escape and js_arg, "未能从 app.core.js 截取 escapeHtml / jsArg"
    return escape.group(0) + "\n" + js_arg.group(0) + "\n"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node")
def test_js_arg_survives_browser_entity_decoding() -> None:
    """jsArg 输出经浏览器实体解码后仍是合法 JS 字符串字面量，且值原样还原。"""
    payloads = ["');alert(1)//\"<x>&", "", "BV1xx411c7mD", "a\\b\n'\"</script>"]
    script = _extract_core_helpers() + f"""
const payloads = {json.dumps(payloads)};
// 模拟浏览器解析属性值：先解 HTML 实体（&amp; 最后解，避免二次解码）。
const decode = s => s.replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
const out = payloads.map(p => {{
    const attr = jsArg(p);
    const jsSource = decode(attr);
    return {{
        attrHasRawQuote: /["']/.test(attr),
        parsed: JSON.parse(jsSource),
        evaluated: new Function('return ' + jsSource)(),
    }};
}});
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run(
        ["node", "-"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    for payload, item in zip(payloads, json.loads(result.stdout)):
        assert item["attrHasRawQuote"] is False
        assert item["parsed"] == payload
        assert item["evaluated"] == payload
