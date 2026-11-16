"""无边框 TUI 桌面的窗口桥与 Windows 原生命中测试。

TUI 页面在桌面模式下提供自己的标题栏和窗口按钮。窗口拖动 / 边缘缩放优先交给
Windows 原生 ``WM_NCHITTEST``，这样系统自带的拖动、双击最大化、Snap 和边缘缩放
都能保留；非 Windows 平台退化为 QWebChannel 的 ``startSystemMove`` /
``startSystemResize``。

纯函数 :func:`tui_hit_test` 独立于 Qt，便于测试；真正的原生过滤器只在 Windows 上安装。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
from typing import Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Qt, Signal, Slot

#: 与 tui.css 中 .titlebar 的高度、.win-controls 的宽度保持一致（逻辑像素）。
TUI_TITLEBAR_HEIGHT = 34
TUI_WINDOW_CONTROLS_WIDTH = 112
TUI_RESIZE_BORDER = 6

#: Windows WM_NCHITTEST 返回码。
HTCLIENT = 1
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17
WM_NCHITTEST = 0x0084


class WindowBridge(QObject):
    """暴露给 TUI 页面的 QWebChannel 对象。

    页面只拿到最小能力集合：最小化、最大化/还原、关闭到托盘、查询状态、拖动和缩放。
    关闭走 ``QMainWindow.close()``，因此仍保持“关闭到托盘”的既有桌面行为。
    """

    maximizedChanged = Signal(bool)

    def __init__(self, window):
        """保存宿主窗口引用。

        Args:
            window: ``QMainWindow`` 实例。
        """
        super().__init__(window if isinstance(window, QObject) else None)
        self._window = window

    @Slot()
    def minimize(self) -> None:
        """最小化窗口。"""
        self._window.showMinimized()

    @Slot()
    def toggleMaximize(self) -> None:
        """在最大化与还原之间切换。"""
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()
        self.notifyMaximized()

    @Slot()
    def close(self) -> None:
        """关闭窗口；主窗口的 closeEvent 会隐藏到系统托盘。"""
        self._window.close()

    @Slot()
    def openClassic(self) -> None:
        """切回正式 WebUI（占位页上的 OPEN CLASSIC UI 使用）。"""
        load = getattr(self._window, "load_web_ui", None)
        if callable(load):
            load(tui_preview=False)

    @Slot(result=bool)
    def isMaximized(self) -> bool:
        """返回当前是否最大化。"""
        return bool(self._window.isMaximized())

    @Slot(result=bool)
    def nativeHitTest(self) -> bool:
        """Windows 使用原生 WM_NCHITTEST 拖动/缩放，页面不再绑定回退事件。"""
        return sys.platform == "win32"

    @Slot()
    def startDrag(self) -> None:
        """非 Windows 回退：让 Qt 接管窗口移动。"""
        handle = self._window.windowHandle()
        if handle is not None:
            handle.startSystemMove()

    @Slot(str)
    def startResize(self, edge: str) -> None:
        """非 Windows 回退：让 Qt 按边缘开始缩放。

        Args:
            edge: ``n`` / ``s`` / ``e`` / ``w`` / ``ne`` / ``nw`` / ``se`` / ``sw``。
        """
        handle = self._window.windowHandle()
        if handle is None:
            return
        mapping = {
            "n": Qt.TopEdge,
            "s": Qt.BottomEdge,
            "e": Qt.RightEdge,
            "w": Qt.LeftEdge,
            "ne": Qt.TopEdge | Qt.RightEdge,
            "nw": Qt.TopEdge | Qt.LeftEdge,
            "se": Qt.BottomEdge | Qt.RightEdge,
            "sw": Qt.BottomEdge | Qt.LeftEdge,
        }
        edges = mapping.get(str(edge or "").lower())
        if edges is not None:
            handle.startSystemResize(edges)

    def notifyMaximized(self) -> None:
        """窗口状态变化时把最大化状态推给页面。"""
        self.maximizedChanged.emit(bool(self._window.isMaximized()))


def tui_hit_test(
    x: float,
    y: float,
    width: float,
    height: float,
    dpr: float = 1.0,
    maximized: bool = False,
) -> int:
    """计算无边框 TUI 窗口的 Windows 命中码（纯函数）。

    参数均为窗口客户区物理像素；``dpr`` 用于把 CSS 逻辑尺寸换算成物理尺寸。

    Args:
        x: 客户区 x。
        y: 客户区 y。
        width: 客户区宽度。
        height: 客户区高度。
        dpr: 设备像素比。
        maximized: 是否最大化。

    Returns:
        int: ``HT*`` 命中码。
    """
    scale = dpr if dpr and dpr > 0 else 1.0
    border = max(4.0, TUI_RESIZE_BORDER * scale)
    titlebar = TUI_TITLEBAR_HEIGHT * scale
    controls = TUI_WINDOW_CONTROLS_WIDTH * scale

    if width <= 0 or height <= 0:
        return HTCLIENT

    # 最大化时禁用边缘缩放，只保留标题栏拖动 / 双击还原。
    if not maximized:
        on_left = x < border
        on_right = x > width - border
        on_top = y < border
        on_bottom = y > height - border
        if on_top and on_left:
            return HTTOPLEFT
        if on_top and on_right:
            return HTTOPRIGHT
        if on_bottom and on_left:
            return HTBOTTOMLEFT
        if on_bottom and on_right:
            return HTBOTTOMRIGHT
        if on_left:
            return HTLEFT
        if on_right:
            return HTRIGHT
        if on_top:
            return HTTOP
        if on_bottom:
            return HTBOTTOM

    # 标题栏右侧留给页面内的窗口按钮；其余区域交给系统拖动。
    if y < titlebar and x < width - controls:
        return HTCAPTION
    return HTCLIENT


class WindowsHitTestFilter(QAbstractNativeEventFilter):
    """仅 Windows：把 TUI 标题栏 / 边缘映射到原生窗口拖动与缩放。"""

    def __init__(self, window):
        """保存宿主窗口。

        Args:
            window: ``QMainWindow`` 实例。
        """
        super().__init__()
        self._window = window
        # 显式声明 64 位 HWND 原型，避免 ctypes 默认把句柄截断成 32 位。
        try:
            user32 = ctypes.windll.user32
            user32.ScreenToClient.argtypes = [
                ctypes.wintypes.HWND,
                ctypes.POINTER(ctypes.wintypes.POINT),
            ]
            user32.ScreenToClient.restype = ctypes.wintypes.BOOL
            user32.GetClientRect.argtypes = [
                ctypes.wintypes.HWND,
                ctypes.POINTER(ctypes.wintypes.RECT),
            ]
            user32.GetClientRect.restype = ctypes.wintypes.BOOL
            user32.IsZoomed.argtypes = [ctypes.wintypes.HWND]
            user32.IsZoomed.restype = ctypes.wintypes.BOOL
        except Exception:  # noqa: BLE001 - 非 Windows 不会走到真实调用
            pass

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt 命名
        """处理 ``WM_NCHITTEST``。

        Args:
            event_type: Qt 原生事件类型，Windows 为 ``windows_generic_MSG``。
            message: 指向 MSG 的指针。

        Returns:
            tuple[bool, int]: 是否已处理及 LRESULT。
        """
        if not isinstance(event_type, (bytes, bytearray)):
            try:
                event_type = bytes(event_type.data()) if hasattr(event_type, "data") else bytes(event_type)
            except Exception:  # noqa: BLE001 - 无法识别的平台事件直接放行
                return False, 0
        if event_type != b"windows_generic_MSG":
            return False, 0
        window = self._window
        try:
            if not getattr(window, "_tui_preview", False) or not window.isVisible():
                return False, 0
        except RuntimeError:
            # 窗口已销毁但过滤器还没从 QApplication 上摘掉：直接交给 Qt。
            return False, 0
        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
        except Exception:  # noqa: BLE001 - 非 Windows / 指针不可用时交给 Qt
            return False, 0
        if msg.message != WM_NCHITTEST:
            return False, 0
        try:
            hwnd = int(window.winId())
            if int(msg.hWnd) != hwnd:
                return False, 0
            user32 = ctypes.windll.user32
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
            point = ctypes.wintypes.POINT(x, y)
            if not user32.ScreenToClient(msg.hWnd, ctypes.byref(point)):
                return False, 0
            rect = ctypes.wintypes.RECT()
            if not user32.GetClientRect(msg.hWnd, ctypes.byref(rect)):
                return False, 0
            dpr = float(window.devicePixelRatioF() or 1.0)
            hit = tui_hit_test(
                point.x,
                point.y,
                rect.right,
                rect.bottom,
                dpr,
                bool(user32.IsZoomed(msg.hWnd)),
            )
            return True, int(hit)
        except Exception:  # noqa: BLE001 - 命中测试失败不能影响窗口消息循环
            return False, 0


def install_windows_hit_test(window) -> Optional[WindowsHitTestFilter]:
    """在 Windows 上安装 TUI 窗口命中测试过滤器。

    Args:
        window: ``QMainWindow`` 实例。

    Returns:
        Optional[WindowsHitTestFilter]: 非 Windows 返回 ``None``。
    """
    if sys.platform != "win32":
        return None
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return None
    event_filter = WindowsHitTestFilter(window)
    app.installNativeEventFilter(event_filter)
    return event_filter
