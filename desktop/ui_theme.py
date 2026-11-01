"""桌面端原生控件的统一外观：与 Web 面板「白色档案」设计令牌保持一致。

Web 面板的颜色、圆角、按钮写在 ``web/frontend/static/css/style.base.css`` 的 ``:root`` 变量里；
桌面壳的 Qt 原生控件（桌宠菜单、预警气泡、悬停提示、托盘菜单、退出确认与错误对话框、启动加载页）
无法引用 CSS 变量，这里把同一组数值写成 Python 常量，再生成 Qt 样式表。

改 Web 面板的设计令牌时，同步改这里；``tests/test_desktop_ui_theme.py`` 会比对两边的取值。

设计约束（与 Web 端一致）：
- 白底、墨色字，圆角只用 2px（按钮）和 4px（卡片 / 弹窗）；
- 主按钮深灰底、浅色字，左侧一条黄色短条；黄色只用于细线、短条，不做大面积底色；
- 危险操作用白底 + 危险色细框，悬停时才变实底。
"""
from __future__ import annotations

from PyQt5.QtWidgets import QMessageBox, QWidget

#: 与 style.base.css :root 同名变量一一对应（去掉前缀 ``--``）。
TOKENS = {
    "paper": "#fafafa",
    "paper-2": "#f2f2f2",
    "paper-3": "#e6e6e6",
    "rule": "#d9d9d9",
    "rule-2": "#bfbfbf",
    "ink": "#191919",
    "ink-2": "#3c3c3c",
    "ink-3": "#8a8a8a",
    "ink-4": "#b3b3b3",
    "btn": "#383838",
    "signal": "#fffa00",
    "danger": "#e0362c",
    "warn": "#e08a00",
}

#: 按钮 2px、卡片与弹窗 4px，对应 --r-btn / --r-card。
RADIUS_BTN = 2
RADIUS_CARD = 4

#: 正文字体：与 --f-body 同序；Qt 找不到前面的字体时按顺序回退。
FONT_BODY = '"HarmonyOS Sans SC", "MiSans", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif'
#: 等宽字体：与 --f-mono 同序，用于加载页的英文编号。
FONT_MONO = '"JetBrains Mono", "Cascadia Mono", Consolas, monospace'


def _t(name: str) -> str:
    """取设计令牌颜色。"""
    return TOKENS[name]


def button_rules(scope: str = "") -> str:
    """生成 QPushButton 的三种外观：默认（次按钮）、主按钮、危险按钮。

    Args:
        scope: 选择器前缀，例如 ``"QFrame#petMenuPanel "``，用于把规则限定在某个容器里。

    Returns:
        str: Qt 样式表片段。
    """
    s = scope
    return f"""
            {s}QPushButton {{
                min-height: 30px;
                padding: 6px 14px;
                text-align: left;
                color: {_t('ink-2')};
                background: {_t('paper-2')};
                border: 1px solid {_t('rule')};
                border-radius: {RADIUS_BTN}px;
                font-family: {FONT_BODY};
                font-size: 13px;
                font-weight: 500;
            }}
            {s}QPushButton:hover {{
                color: {_t('ink')};
                border-color: {_t('ink-3')};
            }}
            {s}QPushButton:pressed {{
                background: {_t('paper-3')};
            }}
            {s}QPushButton:focus {{
                border-color: {_t('ink-2')};
            }}
            {s}QPushButton#petMenuPrimary, {s}QPushButton[variant="primary"] {{
                color: #eeeeee;
                background: {_t('btn')};
                border: 1px solid {_t('btn')};
                border-left: 4px solid {_t('signal')};
                padding-left: 11px;
            }}
            {s}QPushButton#petMenuPrimary:hover, {s}QPushButton[variant="primary"]:hover {{
                color: #ffffff;
                background: #2a2a2a;
                border-color: #2a2a2a;
                border-left-color: {_t('signal')};
            }}
            {s}QPushButton#petMenuPrimary:pressed, {s}QPushButton[variant="primary"]:pressed {{
                background: {_t('ink')};
            }}
            {s}QPushButton#petMenuDanger, {s}QPushButton[variant="danger"] {{
                color: {_t('danger')};
                background: #ffffff;
                border: 1px solid rgba(224, 54, 44, 115);
            }}
            {s}QPushButton#petMenuDanger:hover, {s}QPushButton[variant="danger"]:hover {{
                color: #ffffff;
                background: {_t('danger')};
                border-color: {_t('danger')};
            }}
            {s}QPushButton#petMenuDanger:pressed, {s}QPushButton[variant="danger"]:pressed {{
                background: #c42a21;
                border-color: #c42a21;
            }}"""


def pet_menu_style() -> str:
    """桌宠点击后弹出的快捷菜单：白底卡片 + 4px 圆角 + 细描边，顶边一条黄色短线。"""
    return f"""
            QFrame#petMenuPanel {{
                background: #ffffff;
                border: 1px solid {_t('rule')};
                border-top: 2px solid {_t('signal')};
                border-radius: {RADIUS_CARD}px;
            }}""" + button_rules("QFrame#petMenuPanel ")


def pet_notify_style() -> str:
    """桌宠预警气泡：白底、左侧 4px 危险色短条、墨色正文。"""
    return f"""
            QLabel {{
                background-color: #ffffff;
                border: 1px solid {_t('rule')};
                border-left: 4px solid {_t('danger')};
                border-radius: {RADIUS_CARD}px;
                padding: 10px 12px;
                color: {_t('ink-2')};
                font-family: {FONT_BODY};
                font-size: 12px;
            }}"""


def tooltip_style() -> str:
    """悬停提示：与侧栏导航悬停标签一致的深灰底浅色字。"""
    return f"""
            QToolTip {{
                color: #eeeeee;
                background-color: {_t('btn')};
                border: 0;
                border-left: 3px solid {_t('signal')};
                border-radius: {RADIUS_BTN}px;
                padding: 6px 10px;
                font-family: {FONT_BODY};
                font-size: 12px;
            }}"""


def menu_style() -> str:
    """托盘右键菜单：白底、细描边、选中项浅灰底 + 左侧黄色短条。"""
    return f"""
            QMenu {{
                background: #ffffff;
                border: 1px solid {_t('rule')};
                border-radius: {RADIUS_CARD}px;
                padding: 4px 0;
                font-family: {FONT_BODY};
                font-size: 13px;
                color: {_t('ink-2')};
            }}
            QMenu::item {{
                padding: 7px 22px 7px 16px;
                border-left: 3px solid transparent;
            }}
            QMenu::item:selected {{
                color: {_t('ink')};
                background: {_t('paper-2')};
                border-left: 3px solid {_t('signal')};
            }}
            QMenu::separator {{
                height: 1px;
                margin: 4px 10px;
                background: {_t('paper-3')};
            }}"""


def dialog_style() -> str:
    """退出确认、错误提示等 QMessageBox：白底弹窗，按钮沿用主按钮 / 次按钮外观。"""
    return f"""
            QMessageBox {{
                background: #ffffff;
                border-top: 2px solid {_t('signal')};
            }}
            QMessageBox QLabel {{
                color: {_t('ink-2')};
                font-family: {FONT_BODY};
                font-size: 13px;
            }}""" + button_rules("QMessageBox ")


def app_style() -> str:
    """应用级样式表：只包含悬停提示与菜单，两者在 Qt 里只能全局设置。"""
    return tooltip_style() + menu_style()


def show_message(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    kind: str = "info",
    confirm_text: str | None = None,
    cancel_text: str | None = None,
    danger: bool = False,
) -> bool:
    """弹出与面板风格一致的消息框，替代 ``QMessageBox.question`` / ``critical``。

    Args:
        parent: 父窗口。
        title: 窗口标题。
        text: 正文。
        kind: ``"info"``、``"error"`` 或 ``"confirm"``；confirm 有确认 / 取消两个按钮。
        confirm_text: 确认按钮文案，默认「确定」。
        cancel_text: 取消按钮文案，默认「取消」；仅 confirm 使用。
        danger: 确认按钮是否使用危险样式（例如「退出」）。

    Returns:
        bool: confirm 时用户点了确认返回 True；其余情况恒为 True。
    """
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.NoIcon)
    box.setStyleSheet(dialog_style())
    confirm_button = box.addButton(confirm_text or "确定", QMessageBox.AcceptRole)
    confirm_button.setProperty("variant", "danger" if danger else "primary")
    if kind == "confirm":
        cancel_button = box.addButton(cancel_text or "取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel_button)
        box.setEscapeButton(cancel_button)
    else:
        box.setDefaultButton(confirm_button)
    box.exec_()
    return kind != "confirm" or box.clickedButton() is confirm_button


#: 启动加载页（Web 服务就绪前显示）：白底墨字，与欢迎页一致的细线进度条和等宽编号。
LOADING_HTML = f"""
<html>
<head>
<meta charset="utf-8">
<style>
    html, body {{ height: 100%; margin: 0; }}
    body {{
        display: flex;
        align-items: center;
        justify-content: center;
        background: {_t('paper')};
        color: {_t('ink')};
        font-family: {FONT_BODY};
    }}
    .loading {{ width: 280px; }}
    .mark {{ margin: 0 0 18px; font: 800 14px/1.05 "Archivo Expanded", "Outfit", sans-serif; letter-spacing: -.02em; }}
    h2 {{ margin: 0 0 6px; font-size: 16px; font-weight: 700; letter-spacing: .02em; }}
    p {{ margin: 0 0 16px; color: {_t('ink-3')}; font-size: 12px; }}
    .bar {{ position: relative; height: 2px; overflow: hidden; background: {_t('paper-3')}; }}
    .bar i {{
        position: absolute; top: 0; bottom: 0; left: -40%; width: 40%;
        background: {_t('btn')};
        animation: run 1.4s cubic-bezier(.7, 0, .2, 1) infinite;
    }}
    .bar i::after {{ position: absolute; top: 0; right: 0; bottom: 0; width: 8px; content: ""; background: {_t('signal')}; }}
    .code {{ margin-top: 10px; color: {_t('ink-4')}; font: 500 10px/1 {FONT_MONO}; letter-spacing: .08em; }}
    @keyframes run {{ to {{ left: 100%; }} }}
    @media (prefers-reduced-motion: reduce) {{ .bar i {{ left: 0; width: 30%; animation: none; }} }}
</style>
</head>
<body>
    <div class="loading" role="status">
        <div class="mark">FISH<br>TOOL</div>
        <h2>正在启动</h2>
        <p>正在初始化 Web 服务，请稍候</p>
        <div class="bar"><i></i></div>
        <div class="code">BOOT // LOCAL SERVICE</div>
    </div>
</body>
</html>
"""
