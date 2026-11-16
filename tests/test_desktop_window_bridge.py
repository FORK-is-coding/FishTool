"""无边框 TUI 窗口命中测试与窗口桥的纯逻辑回归。"""
from __future__ import annotations

import sys

from desktop.window_bridge import (
    HTCAPTION,
    HTBOTTOM,
    HTBOTTOMLEFT,
    HTBOTTOMRIGHT,
    HTCLIENT,
    HTLEFT,
    HTRIGHT,
    HTTOP,
    HTTOPLEFT,
    HTTOPRIGHT,
    WindowBridge,
    tui_hit_test,
)


def test_titlebar_is_caption_and_controls_are_client():
    """标题栏左侧可拖动；右侧窗口按钮区域必须留给页面点击。"""
    assert tui_hit_test(20, 10, 1440, 900, 1.0, False) == HTCAPTION
    assert tui_hit_test(720, 10, 1440, 900, 1.0, False) == HTCAPTION
    assert tui_hit_test(1400, 10, 1440, 900, 1.0, False) == HTCLIENT
    assert tui_hit_test(720, 40, 1440, 900, 1.0, False) == HTCLIENT


def test_resize_edges_and_corners():
    """边缘和四角返回 Windows 的缩放命中码。"""
    assert tui_hit_test(2, 100, 1440, 900, 1.0, False) == HTLEFT
    assert tui_hit_test(1438, 100, 1440, 900, 1.0, False) == HTRIGHT
    assert tui_hit_test(720, 2, 1440, 900, 1.0, False) == HTTOP
    assert tui_hit_test(720, 898, 1440, 900, 1.0, False) == HTBOTTOM
    assert tui_hit_test(2, 2, 1440, 900, 1.0, False) == HTTOPLEFT
    assert tui_hit_test(1438, 2, 1440, 900, 1.0, False) == HTTOPRIGHT
    assert tui_hit_test(2, 898, 1440, 900, 1.0, False) == HTBOTTOMLEFT
    assert tui_hit_test(1438, 898, 1440, 900, 1.0, False) == HTBOTTOMRIGHT


def test_hidpi_scales_titlebar_and_edges():
    """150% 缩放下按物理像素换算标题栏和边缘。"""
    assert tui_hit_test(5, 5, 2160, 1350, 1.5, False) == HTTOPLEFT
    assert tui_hit_test(200, 30, 2160, 1350, 1.5, False) == HTCAPTION
    assert tui_hit_test(200, 70, 2160, 1350, 1.5, False) == HTCLIENT


def test_maximized_uses_caption_but_no_resize_edges():
    """最大化时只保留标题栏拖动，不再允许边缘缩放。"""
    assert tui_hit_test(2, 100, 1440, 900, 1.0, True) == HTCLIENT
    assert tui_hit_test(720, 2, 1440, 900, 1.0, True) == HTCAPTION
    assert tui_hit_test(720, 500, 1440, 900, 1.0, True) == HTCLIENT


class _FakeWindow:
    def __init__(self):
        self.visible = False
        self.maximized = False
        self.closed = 0
        self.normal = 0
        self.minimized = 0

    def showMinimized(self):
        self.minimized += 1

    def isMaximized(self):
        return self.maximized

    def showMaximized(self):
        self.maximized = True

    def showNormal(self):
        self.maximized = False
        self.normal += 1

    def close(self):
        self.closed += 1


def test_window_bridge_controls():
    """最大化/还原、最小化与关闭到托盘的桥接行为。"""
    window = _FakeWindow()
    bridge = WindowBridge(window)
    assert bridge.isMaximized() is False
    bridge.toggleMaximize()
    assert window.maximized is True
    bridge.toggleMaximize()
    assert window.maximized is False
    bridge.minimize()
    assert window.minimized == 1
    bridge.close()
    assert window.closed == 1


def test_window_bridge_native_hit_test_matches_platform():
    """Windows 使用原生命中测试；其他平台交给页面回退。"""
    bridge = WindowBridge(_FakeWindow())
    assert bridge.nativeHitTest() is (sys.platform == "win32")
