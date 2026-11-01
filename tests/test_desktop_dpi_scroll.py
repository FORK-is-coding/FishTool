"""桌面端与浏览器显示一致性：高 DPI 小数倍率、平滑滚动、启动窗口贴合屏幕。

背景（150% 缩放的 2560x1440 屏幕，exe 内实测）：
- Qt5 默认把缩放倍率取整为 2：devicePixelRatio=2，页面视口 1282x750，逻辑屏幕 1280x720，
  页面整体比浏览器大三分之一；改为 PassThrough 后 devicePixelRatio=1.5，视口 1440x900。
- QtWebEngine 的 ScrollAnimatorEnabled 默认 False，滚轮没有平滑滚动。
- 逻辑屏幕可用高度 912 时，900 高的客户区加标题栏从 y=100 放置，底部超出屏幕。

为什么不构造真实 MainWindow：WebEngine 只在子进程里构造（见 tests/test_desktop_webengine_isolation.py），
这里用替身和纯函数验证。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

# 模块级 import（PySide6 下导入顺序没有要求；保持在模块级只是为了收集期就暴露导入错误）
import desktop.main_window as mw
from desktop.main_window import MainWindow, fit_window_geometry

import app as entry

REPO_ROOT = Path(__file__).resolve().parent.parent
INITIAL = (MainWindow.INITIAL_WINDOW_WIDTH, MainWindow.INITIAL_WINDOW_HEIGHT)
MINIMUM = (MainWindow.MIN_WINDOW_WIDTH, MainWindow.MIN_WINDOW_HEIGHT)
FRAME = MainWindow.WINDOW_FRAME_EXTRA


# ---------- 高 DPI 倍率：run_desktop 在创建 QApplication 前设置 PassThrough ----------

class _FakeApp:
    """QApplication 替身：记录类方法调用顺序与实例化。"""

    calls: list = []

    def __init__(self, argv) -> None:
        """记录实例化。"""
        _FakeApp.calls.append(("init", None))

    @classmethod
    def setAttribute(cls, attr, on=True):  # noqa: N802 - 与 Qt API 同名
        """记录应用属性。"""
        cls.calls.append(("setAttribute", attr))

    @classmethod
    def setHighDpiScaleFactorRoundingPolicy(cls, policy):  # noqa: N802 - 与 Qt API 同名
        """记录缩放取整策略。"""
        cls.calls.append(("rounding", policy))

    def setWindowIcon(self, icon):  # noqa: N802
        """忽略。"""

    def setApplicationName(self, name):  # noqa: N802
        """忽略。"""

    def setOrganizationName(self, name):  # noqa: N802
        """忽略。"""

    def setStyleSheet(self, sheet):  # noqa: N802
        """记录应用级样式表（悬停提示与菜单，见 desktop.ui_theme.app_style）。"""
        _FakeApp.calls.append(("styleSheet", None))

    def exec(self):
        """不进入事件循环。"""
        return 0


class _FakeWindow:
    """MainWindow / PetWindow 替身。"""

    def __init__(self, *args, **kwargs) -> None:
        """忽略参数。"""

    def show(self) -> None:
        """忽略。"""


def test_run_desktop_sets_passthrough_before_qapplication(monkeypatch):
    """取整策略只在 QApplication 创建前设置才生效。"""
    import PySide6.QtWidgets as qtw
    from PySide6.QtCore import Qt

    import desktop.icon_utils as icon_utils
    import desktop.pet_window as pet

    _FakeApp.calls = []
    monkeypatch.setattr(qtw, "QApplication", _FakeApp)
    monkeypatch.setattr(mw, "MainWindow", _FakeWindow)
    monkeypatch.setattr(pet, "PetWindow", _FakeWindow)
    monkeypatch.setattr(icon_utils, "load_app_icon", lambda: None)
    monkeypatch.setattr(icon_utils, "set_windows_app_user_model_id", lambda: None)

    assert entry.run_desktop(port=8000) == 0

    names = [c[0] for c in _FakeApp.calls]
    assert ("rounding", Qt.HighDpiScaleFactorRoundingPolicy.PassThrough) in _FakeApp.calls
    assert names.index("rounding") < names.index("init")
    assert not any(c[0] == "setAttribute" for c in _FakeApp.calls), _FakeApp.calls  # Qt6 默认开启高 DPI


# ---------- 平滑滚动：init_ui 打开 ScrollAnimatorEnabled ----------

def _init_ui_calls() -> list[str]:
    """返回 MainWindow.init_ui 中所有 settings.setAttribute 的第一个参数（属性名）。"""
    tree = ast.parse((REPO_ROOT / "desktop" / "main_window.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "init_ui")
    attrs = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setAttribute"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value is True
        ):
            attrs.append(node.args[0].attr)
    return attrs


def test_init_ui_enables_scroll_animator():
    """QtWebEngine 默认关闭平滑滚动，桌面壳显式打开。"""
    assert "ScrollAnimatorEnabled" in _init_ui_calls()


_DEFAULT_PROBE = (
    "import sys\n"
    "from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings as S\n"
    "from PySide6.QtWidgets import QApplication\n"
    "app = QApplication(sys.argv[:1])\n"
    "print('SCROLL_ANIMATOR_DEFAULT', QWebEngineProfile.defaultProfile().settings().testAttribute(S.ScrollAnimatorEnabled))\n"
)


def test_scroll_animator_default_is_off():
    """对照：确认默认值确实是关闭的，否则这条设置没有意义。

    读取 WebEngine 默认设置会初始化 WebEngine，按约定放到子进程里用系统原生平台跑；
    子进程起不来（无桌面会话等）时跳过。
    """
    env = dict(os.environ, QT_QPA_PLATFORM="windows" if sys.platform == "win32" else os.environ.get("QT_QPA_PLATFORM", ""))
    proc = subprocess.run([sys.executable, "-c", _DEFAULT_PROBE], capture_output=True, text=True, timeout=60, env=env)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("SCROLL_ANIMATOR_DEFAULT")), None)
    if proc.returncode != 0 or line is None:
        pytest.skip(f"无法在子进程中初始化 QtWebEngine（returncode={proc.returncode}）")
    assert line == "SCROLL_ANIMATOR_DEFAULT False"


# ---------- 启动窗口贴合屏幕 ----------

def test_large_screen_keeps_constants_and_centers():
    """1920x1040 可用区域放得下：尺寸保持 1440x900，最小尺寸不变，居中。"""
    (x, y, w, h), mins = fit_window_geometry((0, 0, 1920, 1040), FRAME, INITIAL, MINIMUM)

    assert (w, h) == INITIAL
    assert mins == MINIMUM
    assert x == (1920 - 1440 - FRAME[0]) // 2
    assert y == (1040 - 900 - FRAME[1]) // 2


def test_150_percent_1440p_fits_inside_available_height():
    """2560x1440 @150%：逻辑可用区域 1707x912，窗口外框不能超出屏幕底部。"""
    (x, y, w, h), _ = fit_window_geometry((0, 0, 1707, 912), FRAME, INITIAL, MINIMUM)

    assert w == 1440
    assert h == 912 - FRAME[1]
    assert y >= 0 and y + h + FRAME[1] <= 912
    assert x >= 0 and x + w + FRAME[0] <= 1707


def test_small_screen_shrinks_minimum_too():
    """1366x728 可用区域比最小尺寸还小：最小尺寸跟着收缩，窗口仍完整可见。"""
    (x, y, w, h), (min_w, min_h) = fit_window_geometry((0, 0, 1366, 728), FRAME, INITIAL, MINIMUM)

    assert (w, h) == (1366 - FRAME[0], 728 - FRAME[1])
    assert (min_w, min_h) == (1200, 728 - FRAME[1])
    assert (x, y) == (0, 0)


def test_offset_screen_origin_is_respected():
    """副屏或任务栏在顶部时可用区域原点不为 0，居中要以该原点为基准。"""
    (x, y, w, h), _ = fit_window_geometry((1920, 40, 1920, 1040), FRAME, INITIAL, MINIMUM)

    assert x == 1920 + (1920 - 1440 - FRAME[0]) // 2
    assert y == 40 + (1040 - 900 - FRAME[1]) // 2


@pytest.mark.parametrize("available", [(0, 0, 800, 600), (0, 0, 1280, 680), (0, 0, 3840, 2100)])
def test_result_never_exceeds_available(available):
    """任何可用区域下，窗口外框都在屏幕内，最小尺寸不大于初始尺寸。"""
    ax, ay, aw, ah = available
    (x, y, w, h), (min_w, min_h) = fit_window_geometry(available, FRAME, INITIAL, MINIMUM)

    assert ax <= x and x + w + FRAME[0] <= ax + aw
    assert ay <= y and y + h + FRAME[1] <= ay + ah
    assert min_w <= w and min_h <= h


class _Rect:
    """QRect 替身。"""

    def __init__(self, x, y, w, h) -> None:
        """保存矩形。"""
        self._v = (x, y, w, h)

    def x(self):
        """左。"""
        return self._v[0]

    def y(self):
        """上。"""
        return self._v[1]

    def width(self):
        """宽。"""
        return self._v[2]

    def height(self):
        """高。"""
        return self._v[3]


class _Screen:
    """QScreen 替身。"""

    def __init__(self, rect) -> None:
        """保存可用区域。"""
        self._rect = rect

    def availableGeometry(self):  # noqa: N802 - 与 Qt API 同名
        """返回可用区域。"""
        return self._rect


class _Win:
    """MainWindow 替身：记录 setMinimumSize / resize / move / setGeometry。"""

    INITIAL_WINDOW_WIDTH, INITIAL_WINDOW_HEIGHT = INITIAL
    MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT = MINIMUM
    WINDOW_FRAME_EXTRA = FRAME

    def __init__(self) -> None:
        """初始化记录。"""
        self.calls = []

    def setMinimumSize(self, w, h):  # noqa: N802
        """记录。"""
        self.calls.append(("min", w, h))

    def resize(self, w, h):
        """记录。"""
        self.calls.append(("resize", w, h))

    def move(self, x, y):
        """记录。"""
        self.calls.append(("move", x, y))

    def setGeometry(self, x, y, w, h):  # noqa: N802
        """记录。"""
        self.calls.append(("geometry", x, y, w, h))


def test_apply_initial_geometry_uses_primary_screen(monkeypatch):
    """_apply_initial_geometry 读主屏可用区域并按 fit_window_geometry 的结果设置窗口。"""
    monkeypatch.setattr(mw.QApplication, "primaryScreen", staticmethod(lambda: _Screen(_Rect(0, 0, 1707, 912))))
    win = _Win()

    MainWindow._apply_initial_geometry(win)

    (x, y, w, h), (min_w, min_h) = fit_window_geometry((0, 0, 1707, 912), FRAME, INITIAL, MINIMUM)
    assert win.calls == [("min", min_w, min_h), ("resize", w, h), ("move", x, y)]


def test_apply_initial_geometry_without_screen_falls_back(monkeypatch):
    """拿不到主屏时保持原来的固定尺寸和位置。"""
    monkeypatch.setattr(mw.QApplication, "primaryScreen", staticmethod(lambda: None))
    win = _Win()

    MainWindow._apply_initial_geometry(win)

    assert win.calls == [("min", *MINIMUM), ("geometry", 100, 100, *INITIAL)]
