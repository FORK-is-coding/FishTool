"""桌面端 flex gap 兜底契约（Chromium 83 不支持 flex 容器 gap，84 起支持）。

1. 静态断言：每条「display:flex/inline-flex + gap」规则的每个选择器，都在 style.flexgap.css
   的 .no-flex-gap 规则中出现；兜底文件的每条规则都挂在 .no-flex-gap 下（不影响支持 gap 的浏览器）；
   兜底样式表最后加载；检测逻辑在 app.core.js 中且只在不支持时加类。
2. 有 Qt 时，在子进程里用真实 QtWebEngine 加载 index.html：确认加了 no-flex-gap，
   且抽样容器相邻子项的实测间距等于原 gap 声明（不再是 0 或负值）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CSS_DIR = _ROOT / "web" / "frontend" / "static" / "css"
_FALLBACK = _CSS_DIR / "style.flexgap.css"
_INDEX = _ROOT / "web" / "frontend" / "templates" / "index.html"
_CORE = _ROOT / "web" / "frontend" / "static" / "js" / "app.core.js"
_CHILD = Path(__file__).resolve().parent / "_flex_gap_child.py"


def _blocks(text: str):
    """扁平解析 CSS，返回 (选择器列表, 声明字典)；@media 内的规则同样展开。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"@media[^{]*\{", "", text)
    for prelude, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        selectors = [" ".join(s.split()) for s in prelude.split(",") if s.strip()]
        decl = {k.strip().lower(): v.strip() for k, v in (d.split(":", 1) for d in body.split(";") if ":" in d)}
        yield selectors, decl


def _flex_gap_selectors() -> set[str]:
    """源样式（不含兜底文件）里声明了 gap 的 flex 容器选择器。

    display 与 gap 可能分写在不同规则里（如断点内只改 gap），所以先汇总每个选择器的 display。
    """
    display: dict[str, str] = {}
    gap_selectors: set[str] = set()
    for path in sorted(_CSS_DIR.glob("*.css")):
        if path == _FALLBACK:
            continue
        for selectors, decl in _blocks(path.read_text(encoding="utf-8")):
            for sel in selectors:
                if "display" in decl:
                    display.setdefault(sel, decl["display"])
                if any(k in decl for k in ("gap", "row-gap", "column-gap")):
                    gap_selectors.add(sel)
    return {sel for sel in gap_selectors if "flex" in display.get(sel, "")}


def test_every_flex_gap_rule_has_fallback() -> None:
    """新增 flex gap 时必须同步补兜底，否则桌面端该处间距为 0。"""
    fallback = _FALLBACK.read_text(encoding="utf-8")
    covered = set()
    for selectors, _decl in _blocks(fallback):
        for sel in selectors:
            body = sel.replace(".no-flex-gap ", "", 1)
            covered.add(re.split(r"\s*>\s*", body)[0].strip())
    missing = sorted(s for s in _flex_gap_selectors() if s not in covered)
    assert missing == [], f"以下 flex gap 容器缺少 .no-flex-gap 兜底：{missing}"


def test_fallback_rules_are_scoped_to_no_flex_gap() -> None:
    """兜底文件每条规则都以 .no-flex-gap 开头，支持 gap 的浏览器不受任何影响。"""
    unscoped = [
        sel for selectors, _ in _blocks(_FALLBACK.read_text(encoding="utf-8"))
        for sel in selectors if not sel.startswith(".no-flex-gap ")
    ]
    assert unscoped == []


def test_fallback_stylesheet_loaded_last() -> None:
    """兜底样式表必须在所有样式表之后，才能以同等优先级覆盖原有 margin。"""
    links = re.findall(r'<link rel="stylesheet" href="/static/css/([\w.]+\.css)', _INDEX.read_text(encoding="utf-8"))
    assert links and links[-1] == "style.flexgap.css", links


def test_detection_only_adds_class_when_unsupported() -> None:
    """检测在 app.core.js 中实测 row-gap，只有不支持时才给 <html> 加 no-flex-gap。"""
    core = _CORE.read_text(encoding="utf-8")
    start = core.index("function detectFlexGapSupport()")
    body = core[start:core.index("})();", start)]
    assert "row-gap:1px" in body
    assert "if (!supported) root.classList.add('no-flex-gap');" in body


def _qt_available() -> bool:
    try:
        import PyQt5.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        # 没装 PyQtWebEngine：真实内核用例跳过，静态断言仍然覆盖。
        return False
    return True


@pytest.mark.skipif(not _qt_available(), reason="未安装 PyQtWebEngine")
def test_real_qtwebengine_restores_flex_spacing(tmp_path) -> None:
    """真实桌面内核：加了 no-flex-gap，抽样容器的相邻间距等于原 gap 声明。"""
    proc = subprocess.run(
        [sys.executable, str(_CHILD), str(_ROOT / "web" / "frontend")],
        cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    if proc.returncode != 0 or not proc.stdout.strip().startswith("{"):
        pytest.skip(f"QtWebEngine 子进程不可用 rc={proc.returncode}: {proc.stderr[-400:]}")
    result = json.loads(proc.stdout)
    assert "Chrome/83." in result["ua"], result["ua"]
    assert result["errors"] == [], result["errors"]
    assert result["noFlexGapClass"] is True
    for name, sample in result["samples"].items():
        assert sample["spacing"], f"{name} 未找到可见子项：{sample}"
        assert all(abs(s - sample["expected"]) <= 1 for s in sample["spacing"]), f"{name}: {sample}"
