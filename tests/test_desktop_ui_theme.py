"""桌面端原生控件与 Web 面板风格一致、侧栏文字标与 v0.2.9 版本号契约。

1. desktop/ui_theme.py 的颜色常量与 style.base.css :root 设计令牌逐一相等；
2. 桌宠菜单、预警气泡、托盘菜单、退出确认 / 错误对话框、启动加载页、悬停提示都从 ui_theme 取样式，
   不再写旧的暖白 / 粉色 / 紫色渐变配色；
3. 前端不再调用原生 confirm()（桌面端会弹 Qt 系统对话框），改用 showAppConfirm；
4. 侧栏顶部是「FISH / TOOL」文字标，双端可见版本号统一为 v0.2.9。

为什么不构造真实 MainWindow：offscreen 下构造 QWebEngineView 会 0xC0000005
（见 tests/test_desktop_webengine_isolation.py）；这里只构造不碰 WebEngine 的控件，其余用源码断言。
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

# 与 test_desktop_pet_ui.py 一致：QtWebEngineWidgets 必须早于 QApplication 导入；desktop 包会导入 main_window。
os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM", "").strip() or "offscreen"
import desktop.main_window  # noqa: E402,F401
from desktop import ui_theme  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_CSS = _ROOT / "web" / "frontend" / "static" / "css" / "style.base.css"
_JS = _ROOT / "web" / "frontend" / "static" / "js"
_INDEX = _ROOT / "web" / "frontend" / "templates" / "index.html"
_DESKTOP = _ROOT / "desktop"

#: 旧版桌面端配色：暖白卡片、棕色按钮、粉色气泡、紫色渐变加载页。迁移后不应再出现在桌面壳源码里。
_LEGACY_COLORS = ("#fffdf9", "#d4a373", "#c48f61", "#faf7f2", "#fb7299", "#667eea", "#764ba2")


@pytest.fixture(scope="module")
def qapp():
    """离屏 QApplication（只构造不碰 WebEngine 的控件）。"""
    from PyQt5.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _css_tokens() -> dict:
    root = re.search(r":root\s*\{(.*?)\n\}", _CSS.read_text(encoding="utf-8"), re.S)
    assert root, "style.base.css 缺少 :root"
    return dict(re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", root.group(1)))


def test_theme_tokens_match_web_design_tokens() -> None:
    """ui_theme.TOKENS 的每个颜色都与 Web 面板同名 CSS 变量相等。"""
    css = _css_tokens()
    for name, value in ui_theme.TOKENS.items():
        assert name in css, f"style.base.css 没有 --{name}"
        assert css[name].lower() == value.lower(), f"--{name}: CSS={css[name]} ui_theme={value}"
    radius = _CSS.read_text(encoding="utf-8")
    assert f"--r-btn: {ui_theme.RADIUS_BTN}px" in radius
    assert f"--r-card: {ui_theme.RADIUS_CARD}px" in radius


def test_desktop_sources_drop_legacy_colors() -> None:
    """桌面壳源码里不再出现旧版暖白 / 棕 / 粉 / 紫渐变配色。"""
    for path in _DESKTOP.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for color in _LEGACY_COLORS:
            assert color not in text, f"{path.relative_to(_ROOT)} 仍使用旧配色 {color}"


def test_stylesheets_use_theme_colors_and_signal_only_as_thin_line() -> None:
    """各段样式表只用设计令牌里的颜色；黄色只做细线 / 短条（与 Web 端 .btn-primary::after 的 6px 短条同一规则）。

    - Qt 样式表：黄色只出现在 border 声明里，不做背景；
    - 启动加载页 HTML：黄色背景只允许出现在宽度不超过 10px 的短条规则里。
    """
    allowed = {v.lower() for v in ui_theme.TOKENS.values()} | {"#ffffff", "#eeeeee", "#2a2a2a", "#c42a21"}
    qt_sheets = (ui_theme.pet_menu_style(), ui_theme.pet_notify_style(), ui_theme.tooltip_style(),
                 ui_theme.menu_style(), ui_theme.dialog_style())
    signal = ui_theme.TOKENS["signal"].lower()
    for sheet in qt_sheets + (ui_theme.LOADING_HTML,):
        for color in re.findall(r"#[0-9a-fA-F]{6}\b", sheet):
            assert color.lower() in allowed, f"样式表使用了设计令牌之外的颜色 {color}"
    for sheet in qt_sheets:
        for decl in re.findall(r"background(?:-color)?\s*:\s*([^;]+);", sheet):
            assert signal not in decl.lower(), f"Qt 样式表把黄色用作了背景: {decl}"
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", ui_theme.LOADING_HTML):
        if re.search(r"background(?:-color)?\s*:[^;]*" + re.escape(signal), body.lower()):
            width = re.search(r"(?<![-\w])width\s*:\s*(\d+)px", body)
            assert width and int(width.group(1)) <= 10, f"加载页黄色背景不是短条: {selector.strip()}"


def test_pet_menu_panel_uses_theme(qapp) -> None:
    """桌宠菜单：尺寸与按钮角色不变，样式表来自 ui_theme。"""
    from desktop.pet_window.menu_panel import PetMenuPanel

    panel = PetMenuPanel()
    assert (panel.width(), panel.height()) == (236, 176)
    assert panel.styleSheet() == ui_theme.pet_menu_style()
    assert panel.open_button.objectName() == "petMenuPrimary"
    assert panel.quit_button.objectName() == "petMenuDanger"
    panel.deleteLater()


def test_pet_notify_label_and_app_level_styles_are_wired() -> None:
    """预警气泡、托盘菜单、全局悬停提示、退出确认与错误框、启动页都从 ui_theme 取样式。"""
    base = (_DESKTOP / "pet_window" / "base.py").read_text(encoding="utf-8")
    assert "self.notify_label.setStyleSheet(pet_notify_style())" in base
    main = (_DESKTOP / "main_window.py").read_text(encoding="utf-8")
    assert "tray_menu.setStyleSheet(menu_style())" in main
    assert "self.web_view.setHtml(LOADING_HTML)" in main
    assert "QMessageBox" not in main, "主窗口仍直接使用 QMessageBox 默认样式"
    tree = ast.parse(main)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "show_message"]
    kinds = {kw.value.value for c in calls for kw in c.keywords if kw.arg == "kind"}
    assert kinds == {"error", "confirm"}, kinds
    app_py = (_ROOT / "app.py").read_text(encoding="utf-8")
    runner = (_DESKTOP / "pet_window" / "runner.py").read_text(encoding="utf-8")
    for src in (app_py, runner):
        assert "app.setStyleSheet(app_style())" in src


def test_show_message_confirm_defaults_to_cancel(qapp, monkeypatch) -> None:
    """退出确认：默认按钮与 Esc 都是「取消」，确认按钮用危险样式；只有点确认才返回 True。"""
    from PyQt5.QtWidgets import QMessageBox

    seen = {}

    def fake_exec(self):
        buttons = {b.text(): b for b in self.buttons()}
        seen["texts"] = sorted(buttons)
        seen["default"] = self.defaultButton().text()
        seen["escape"] = self.escapeButton().text()
        seen["variant"] = buttons["退出"].property("variant")
        seen["sheet"] = self.styleSheet()
        self._clicked = buttons[seen.pop("click")]
        return 0

    monkeypatch.setattr(QMessageBox, "exec_", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self._clicked)

    seen["click"] = "取消"
    assert ui_theme.show_message(None, "确认退出", "x", kind="confirm", confirm_text="退出", danger=True) is False
    assert seen["texts"] == ["取消", "退出"]
    assert seen["default"] == "取消" and seen["escape"] == "取消"
    assert seen["variant"] == "danger"
    assert seen["sheet"] == ui_theme.dialog_style()

    seen["click"] = "退出"
    assert ui_theme.show_message(None, "确认退出", "x", kind="confirm", confirm_text="退出", danger=True) is True


def test_frontend_has_no_native_confirm() -> None:
    """前端不调用原生 confirm / alert / prompt；确认操作走 showAppConfirm。"""
    for path in _JS.glob("app.*.js"):
        # 去掉块注释和行注释再查：JSDoc 里会提到「替代原生 confirm()」。
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        text = re.sub(r"(?<!:)//[^\n]*", "", text)
        assert not re.search(r"(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\s*\(", text), path.name
    auth = (_JS / "app.auth.js").read_text(encoding="utf-8")
    lottery = (_JS / "app.lottery.js").read_text(encoding="utf-8")
    assert "await showAppConfirm(" in auth[auth.index("async function logoutBili("):]
    assert "await showAppConfirm(" in lottery[lottery.index("async function cancelLotteryTask("):]


def test_sidebar_wordmark_and_version() -> None:
    """侧栏顶部是 FISH / TOOL 文字标（不再是 图标.png），可见版本号统一为 v0.2.9。"""
    html = _INDEX.read_text(encoding="utf-8")
    logo = re.search(r'<div class="logo">(.*?)</div>\s*<nav', html, re.S)
    assert logo, "找不到侧栏 .logo"
    assert '<span class="logo-mark" aria-hidden="true">FISH<br>TOOL</span>' in logo.group(1)
    assert "logo-badge" not in logo.group(1) and "图标.png" not in logo.group(1)
    assert '<span class="logo-version">v0.2.9测试版</span>' in html
    assert '<span class="side-ver">v0.2.9</span>' in html
    assert '<span class="meta-r">BUILD 0.2.9</span>' in html
    assert "BUILD 0.2.9</div>" in (_JS / "app.intro.js").read_text(encoding="utf-8")
    for path in (_INDEX, _JS / "app.intro.js"):
        assert "0.2.5" not in path.read_text(encoding="utf-8"), path.name
    assert re.search(r"\.logo-mark\s*\{[^}]*var\(--f-wide\)", _CSS.read_text(encoding="utf-8"))
