"""桌面端内核兼容契约：前端不得使用 QtWebEngine 5.15.2（Chromium 83）不支持的语法。

桌面主窗口用 PyQtWebEngine 加载同一套前端。requirements 固定 PyQtWebEngine==5.15.6，
其 Qt5WebEngineCore 为 5.15.2，UA 为 Chrome/83.0.4103.122。下列语法在 83 上会被整条丢弃或抛错：

- ``inset`` 简写（87 起）：声明失效，position:fixed 的遮罩宽高为 0；
- ``:is()`` / ``:where()``（88 起）：整组选择器无效，规则全部不生效；
- ``String.prototype.replaceAll``（85 起）：调用即 TypeError。

1. 静态断言：扫描 web/frontend 下的 CSS / JS / 模板，命中即失败，并给出 文件:行号；
2. 有 Qt 时，在子进程里用真实 QtWebEngine 加载 index.html，复核抽屉遮罩铺满视口、
   卡片规则（isolation / overflow）生效、导出问题日志不抛错（子进程隔离，与 test_desktop_webengine_isolation 同一策略）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND = _ROOT / "web" / "frontend"
_CHILD = Path(__file__).resolve().parent / "_qtwebengine_compat_child.py"

# CSS：inset 简写（排除 inset-inline 等逻辑属性名和 box-shadow 的 inset 关键字）、:is()/:where()。
_CSS_RULES = {
    "inset 简写": re.compile(r"(?<![-\w])inset\s*:"),
    ":is() 选择器": re.compile(r":is\("),
    ":where() 选择器": re.compile(r":where\("),
}
# JS（含模板内联脚本与内联样式）：replaceAll，以及内联 style 中的 inset 简写。
_JS_RULES = {
    "String.replaceAll": re.compile(r"\.replaceAll\("),
    "inset 简写": re.compile(r"(?<![-\w])inset\s*:"),
}


def _strip_css_comments(text: str) -> str:
    """去掉 CSS 注释但保留换行，行号与原文件一致（注释里提到 inset 不算违规）。"""
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def _strip_js_line_comments(text: str) -> str:
    """去掉 // 行注释（不处理字符串内的 //，前端源码中 URL 都在字符串里，仅影响注释误报）。"""
    return re.sub(r"(^|[^:'\"`\\])//[^\n]*", r"\1", text)


def _hits(files, rules, strip) -> list[str]:
    out = []
    for path in files:
        text = strip(path.read_text(encoding="utf-8"))
        for label, pattern in rules.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                out.append(f"{path.relative_to(_ROOT).as_posix()}:{line} {label}")
    return out


def test_css_has_no_chromium83_unsupported_syntax() -> None:
    """CSS 不使用 inset 简写与 :is()/:where()。"""
    files = sorted((_FRONTEND / "static" / "css").glob("*.css"))
    assert files
    assert _hits(files, _CSS_RULES, _strip_css_comments) == []


def test_js_and_template_have_no_chromium83_unsupported_api() -> None:
    """JS 与模板（含内联脚本、内联 style）不使用 replaceAll 与 inset 简写。"""
    files = sorted((_FRONTEND / "static" / "js").glob("*.js")) + [_FRONTEND / "templates" / "index.html"]
    assert _hits(files, _JS_RULES, _strip_js_line_comments) == []


def _qt_available() -> bool:
    try:
        import PyQt5.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        # 没装 PyQtWebEngine：真实内核用例跳过，静态断言仍然覆盖。
        return False
    return True


@pytest.mark.skipif(not _qt_available(), reason="未安装 PyQtWebEngine")
def test_real_qtwebengine_renders_fixed_features(tmp_path) -> None:
    """真实桌面内核：抽屉遮罩铺满视口、卡片规则（isolation / overflow）生效、导出问题日志不抛错。"""
    proc = subprocess.run(
        [sys.executable, str(_CHILD), str(_FRONTEND)],
        cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    if proc.returncode != 0 or not proc.stdout.strip().startswith("{"):
        # 无图形环境（CI 或 offscreen 下构造视图崩溃）时只能跳过，静态断言仍然覆盖。
        pytest.skip(f"QtWebEngine 子进程不可用 rc={proc.returncode}: {proc.stderr[-400:]}")
    result = json.loads(proc.stdout)
    assert "Chrome/83." in result["ua"], result["ua"]
    assert result["errors"] == [], result["errors"]
    mask = result["drawerMask"]
    assert mask["w"] >= mask["vw"] and mask["h"] >= mask["vh"], mask
    assert result["sectionIsolation"] == "isolate", result["sectionIsolation"]
    assert result["sectionOverflow"] == "hidden", result["sectionOverflow"]
    assert result["exportError"] is None, result["exportError"]
    assert result["exportName"] and ":" not in result["exportName"], result["exportName"]
