"""桌宠悬浮窗「不碰 WebEngine」部分的 offscreen 用例（批5 · A 组）。

覆盖 desktop/pet_window 下此前无覆盖的 8 个模块：
    assets / menu_panel / image_label / drag_mixin / notify_mixin /
    menu_mixin / lifecycle_mixin / base

为什么这些能直接收编（不 subprocess 隔离）：
    实测 QT_QPA_PLATFORM=offscreen 下 PetMenuPanel(220x156)、PetImageLabel、
    QPropertyAnimation 均可正常构造。它们不构造 QWebEnginePage / QWebEngineView。

为什么不直接构造真 PetWindow：
    PetWindowBase.__init__ 会 setup_websocket() 真起 WebSocketThread 连
    ws://localhost:8000/ws，并读写 QSettings 注册表；closeEvent 还会 wait(5000)。
    为了不引入真实网络/注册表副作用，这里用「最小 QObject 宿主 + 真实 mixin
    方法」驱动被测逻辑。

Qt 导入顺序：
    PyQt5 时代要求 QtWebEngineWidgets 在 QApplication 之前 import（否则报 AA_ShareOpenGLContexts）。
    PySide6 6.12 实测没有这个要求；desktop.* 的 import 仍留在模块级，收集期就能暴露导入错误。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# ---- 环境自愈门（必须在任何 Qt 对象构造之前执行）----
# 实测根因：cmd 里 `set QT_QPA_PLATFORM=offscreen && ...` 会把值设成带**尾空格**的
# "offscreen "，Qt 解析平台插件名失败 → 进程 qFatal/abort，退出码 0xC0000409
# (3221226505)。这是**解释器级崩溃**，pytest 的 try/except 拦不住，会把整个全量进程
# 带走（后面的用例全丢）。这里在 import 期把该变量规整为合法的 offscreen，保证即使调用方
# 环境被污染，本文件也不会把全量跑崩（可接受本文件跑不成，绝不可带走全量）。
_qt_platform = os.environ.get("QT_QPA_PLATFORM")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
if _qt_platform and _qt_platform.strip() != "offscreen":
    print(
        f"[test_desktop_pet_ui] 已覆写 QT_QPA_PLATFORM={_qt_platform!r} -> 'offscreen'"
        "（本文件硬约束：必须 offscreen；防解释器级崩溃污染全量）"
    )

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QPropertyAnimation, QRect, QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QWidget

# 模块级 import（见模块 docstring 的「Qt 导入顺序」）
from desktop.pet_window import assets as pet_assets
from desktop.pet_window.base import PetWindowBase
from desktop.pet_window.drag_mixin import DragMixin
from desktop.pet_window.image_label import PetImageLabel
from desktop.pet_window.inertia_mixin import InertiaMixin
from desktop.pet_window.lifecycle_mixin import LifecycleMixin
from desktop.pet_window.menu_mixin import MenuMixin
from desktop.pet_window.menu_panel import PetMenuPanel
from desktop.pet_window.notify_mixin import NotifyMixin


# ===========================================================================
# fixture
# ===========================================================================
@pytest.fixture(scope="module")
def qt_app():
    """提供进程内唯一的 QApplication（offscreen），供桌宠控件用例复用。

    Returns:
        QApplication: 已存在或新建的应用实例。
    """
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeMainWindow(QWidget):
    """带 show_window / quit_application 的顶层窗口替身。

    menu_mixin.open_main_panel 与 lifecycle_mixin.close_application 都是靠
    QApplication.topLevelWidgets() 按这两个方法名做鸭子匹配，因此这里只需要
    存在一个真实的顶层 QWidget 就足够驱动成功分支。
    """

    def __init__(self) -> None:
        """记录被调次数。"""
        super().__init__()
        self.show_count = 0
        self.quit_count = 0

    def show_window(self) -> None:
        """模拟主窗口显示。"""
        self.show_count += 1

    def quit_application(self) -> None:
        """模拟主窗口退出。"""
        self.quit_count += 1


@pytest.fixture(scope="module")
def fake_main_window(qt_app):
    """模块级唯一的主窗口替身，保证 topLevelWidgets 匹配结果确定。"""
    return _FakeMainWindow()


# ===========================================================================
# 测试替身
# ===========================================================================
class _FakeMouseEvent:
    """最小鼠标事件替身：只实现 mixin 真正读取的接口。"""

    def __init__(self, *, event_type=QEvent.MouseButtonPress,
                 button=Qt.LeftButton, buttons=Qt.NoButton, global_pos=None) -> None:
        """初始化事件类型、按键与全局坐标。"""
        self._type = event_type
        self._button = button
        self._buttons = buttons
        self._global_pos = QPoint(global_pos) if global_pos is not None else QPoint(0, 0)
        self.accepted = False
        self.ignored = False

    def type(self):
        """返回事件类型。"""
        return self._type

    def button(self):
        """返回触发按键。"""
        return self._button

    def buttons(self):
        """返回当前按住的按键集合。"""
        return self._buttons

    def globalPos(self):
        """返回全局坐标（真实 QPoint，供减法/曼哈顿距离使用）。"""
        return QPoint(self._global_pos)

    def globalPosition(self):
        """Qt6 接口：返回 QPointF 全局坐标（生产代码调用 .toPoint()）。"""
        return QPointF(self._global_pos)

    def accept(self) -> None:
        """标记事件已处理。"""
        self.accepted = True

    def ignore(self) -> None:
        """标记事件被忽略。"""
        self.ignored = True


class _FakeCloseEvent:
    """最小窗口关闭事件替身。"""

    def __init__(self) -> None:
        """初始化接受标记。"""
        self.accepted = False

    def accept(self) -> None:
        """标记事件已接受。"""
        self.accepted = True


class _TimerStub:
    """_menu_hide_timer 替身（真 QTimer 需事件循环才触发，测试里只关心调用）。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.stop_count = 0
        self.started: list = []

    def stop(self) -> None:
        """记录一次 stop。"""
        self.stop_count += 1

    def start(self, ms: int) -> None:
        """记录一次 start 的间隔。"""
        self.started.append(ms)


class _SettingsStub:
    """QSettings 替身，避免测试写 Windows 注册表。"""

    def __init__(self) -> None:
        """初始化键值存储。"""
        self.values: dict = {}

    def setValue(self, key, value) -> None:  # noqa: N802 - 对齐 QSettings API
        """记录写入。"""
        self.values[key] = value

    def value(self, key, default=None):  # noqa: N802 - 对齐 QSettings API
        """读取写入值。"""
        return self.values.get(key, default)


class _WsThreadStub:
    """WebSocketThread 替身，只记录 stop/wait。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.stop_count = 0
        self.waits: list = []

    def stop(self) -> None:
        """记录一次 stop。"""
        self.stop_count += 1

    def wait(self, ms: int) -> bool:
        """记录一次 wait 的超时毫秒并立即返回。"""
        self.waits.append(ms)
        return True


class _MixinHostBase(QObject):
    """drag/notify/menu/lifecycle 混入的最小 QObject 宿主。

    继承 QObject 是因为 mixin 内部会 `QTimer(self)` / `QPropertyAnimation(..., self)`，
    都要求 parent 是 QObject。
    """

    def __init__(self) -> None:
        """初始化调用记录与 super() 落点。"""
        super().__init__()
        self.calls: list = []
        self.moved: list = []

    def enterEvent(self, event) -> None:  # noqa: N802 - 对齐 Qt 事件名
        """记录 super() 落入的进入事件。"""
        self.calls.append("enterEvent")

    def leaveEvent(self, event) -> None:  # noqa: N802 - 对齐 Qt 事件名
        """记录 super() 落入的离开事件。"""
        self.calls.append("leaveEvent")

    def mousePressEvent(self, event) -> None:  # noqa: N802 - 对齐 Qt 事件名
        """记录 super() 落入的按下事件。"""
        self.calls.append("mousePressEvent")

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - 对齐 Qt 事件名
        """记录 super() 落入的移动事件。"""
        self.calls.append("mouseMoveEvent")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - 对齐 Qt 事件名
        """记录 super() 落入的释放事件。"""
        self.calls.append("mouseReleaseEvent")

    def eventFilter(self, watched, event):  # noqa: N802 - 对齐 Qt API
        """不拦截任何事件。"""
        return False

    def hide(self) -> None:
        """记录一次 hide。"""
        self.calls.append("hide")

    def move(self, pos) -> None:
        """记录一次 move 目标。"""
        self.moved.append(QPoint(pos))

    def pos(self):
        """返回固定位置，供 QSettings 持久化断言。"""
        return QPoint(10, 10)

    def width(self):  # noqa: N802 - 对齐 Qt API
        """返回固定宽度。"""
        return 150

    def geometry(self):
        """返回固定几何，供事件过滤命中判定。"""
        return QRect(0, 0, 150, 150)

    def frameGeometry(self):  # noqa: N802 - 对齐 Qt API
        """返回固定外框几何。"""
        return QRect(0, 0, 150, 150)

    def mapToGlobal(self, point):  # noqa: N802 - 对齐 Qt API
        """原样返回全局坐标。"""
        return QPoint(point)

    def mapFromGlobal(self, point):  # noqa: N802 - 对齐 Qt API
        """原样返回局部坐标。"""
        return QPoint(point)


class _PetHost(DragMixin, NotifyMixin, MenuMixin, LifecycleMixin, InertiaMixin, _MixinHostBase):
    """把五个混入接到最小宿主上，并补齐 mixin 依赖的属性。"""

    def __init__(self) -> None:
        """初始化状态机替身、控件与各桩对象。"""
        super().__init__()
        self.STATE_IDLE = PetWindowBase.STATE_IDLE
        self.STATE_DRAGGING = PetWindowBase.STATE_DRAGGING
        self.STATE_CLICKED = PetWindowBase.STATE_CLICKED
        self.STATE_NOTIFY = PetWindowBase.STATE_NOTIFY
        self.STATE_FLYING = PetWindowBase.STATE_FLYING
        self.current_state = self.STATE_IDLE
        self.state_history: list = []
        self.image_label = PetImageLabel()
        self.notify_label = QLabel()
        self._menu_hide_timer = _TimerStub()
        self._hover_animation = None
        self._menu_animation = None
        self._drag_start_position = QPoint(0, 0)
        self.drag_position = QPoint(0, 0)
        self.settings = _SettingsStub()
        self.ws_thread = _WsThreadStub()
        self.menu_toggles = 0

    def set_state(self, state) -> None:
        """记录状态切换（不真正换图）。

        与真实控件一致地同步 ``current_state``：inertia 收尾要靠它判断「当前是不是
        飞行态」，只记 history 会让通知态被飞行收尾误判成待机。
        """
        self.current_state = state
        self.state_history.append(state)

    def toggle_menu(self) -> None:
        """记录菜单切换次数。

        警告（实测踩过）：
            本方法会「遮蔽」MenuMixin.toggle_menu —— MRO 里 _PetHost 自己的定义优先，
            因此 host.toggle_menu() 只会自增 menu_toggles，**不会真正显示/隐藏菜单**。

            背景：DragMixin 内部会调 self.toggle_menu()，这里覆写是为了统计
            「拖拽触发了几次菜单切换」，属于替身的刻意行为，不要删。

            后果：任何想验证菜单显隐逻辑的用例，调 host.toggle_menu() 测到的都是本
            替身，show_menu/hide_menu 根本不会执行，panel.isVisible() 会一直是 False，
            看起来像 Qt/offscreen 的问题，其实是测错了对象。
            正确写法：显式指定基类方法 —— MenuMixin.toggle_menu(host)。
        """
        self.menu_toggles += 1


# ===========================================================================
# 1. assets
# ===========================================================================
def test_pet_asset_roots_are_unique_and_include_repo_assets(qt_app):
    """桌宠资源候选目录应去重，且包含源码仓库的 assets 目录。"""
    roots = pet_assets._asset_roots()

    assert len(roots) == len(set(roots)), "候选资源目录不应重复"
    repo_assets = Path(pet_assets.__file__).resolve().parents[2] / "assets"
    assert repo_assets in roots
    assert pet_assets.PET_ASSET_DIRS == roots


# ===========================================================================
# 2. menu_panel
# ===========================================================================
def test_pet_menu_panel_is_fixed_size_frameless_and_has_three_actions(qt_app):
    """PetMenuPanel 应固定 236x176、无边框置顶透明，并承载三个动作按钮。"""
    panel = PetMenuPanel()

    assert (panel.width(), panel.height()) == (236, 176)
    assert panel.windowFlags() & Qt.FramelessWindowHint
    assert panel.windowFlags() & Qt.Tool
    assert panel.testAttribute(Qt.WA_TranslucentBackground)
    assert [
        panel.open_button.text(),
        panel.live_button.text(),
        panel.quit_button.text(),
    ] == ["打开主面板", "舆情预警实况（待更新）", "关闭程序"]
    assert panel.open_button.objectName() == "petMenuPrimary"
    assert panel.quit_button.objectName() == "petMenuDanger"
    assert panel.layout().count() == 3
    assert panel.open_button.parent() is panel


# ===========================================================================
# 3. image_label
# ===========================================================================
def test_pet_image_label_clamps_scale_and_paints_both_branches(qt_app):
    """scale_factor 应 clamp 到 [1.0, 1.08]，且 paintEvent 两条分支都不崩。"""
    label = PetImageLabel()
    label.resize(60, 60)

    assert label.scale_factor == 1.0

    # 空 pixmap -> paintEvent 提前 return 分支
    label.repaint()

    label.set_scale_factor(0.2)
    assert label.scale_factor == 1.0, "低于下限应被夹到 1.0"
    label.set_scale_factor(1.5)
    assert label.scale_factor == 1.08, "高于上限应被夹到 1.08"

    # 非空 pixmap -> 绘制分支
    label.set_pet_pixmap(QPixmap(8, 8))
    label.repaint()

    animation = QPropertyAnimation(label, b"scale_factor")
    animation.setDuration(1)
    animation.setStartValue(1.0)
    animation.setEndValue(1.06)
    animation.start()
    assert animation.state() == QPropertyAnimation.Running, "scale_factor 应可作为动画属性驱动"
    animation.stop()


# ===========================================================================
# 4. drag_mixin
# ===========================================================================
def test_drag_mixin_hover_drag_and_release_flow(qt_app):
    """悬停放大/还原、拖拽阈值、单击菜单与拖拽结束持久化位置。"""
    host = _PetHost()

    # ---- 悬停进入：真实 QPropertyAnimation 指向 scale_factor，终点 1.06 ----
    host.enterEvent(QEvent(QEvent.Enter))
    hover_in = host._hover_animation
    assert isinstance(hover_in, QPropertyAnimation)
    assert hover_in.endValue() == 1.06
    assert hover_in.duration() == 160
    assert "enterEvent" in host.calls

    # ---- 悬停离开：终点恢复到 1.0 ----
    host.leaveEvent(QEvent(QEvent.Leave))
    hover_out = host._hover_animation
    assert hover_out.endValue() == 1.0
    assert hover_out.duration() == 180
    assert "leaveEvent" in host.calls

    # ---- 按下：只记录偏移，不立即切拖拽态（防止单击闪拖拽素材）----
    press = _FakeMouseEvent(button=Qt.LeftButton, global_pos=QPoint(500, 500))
    host.mousePressEvent(press)
    assert press.accepted is True
    assert host.state_history == []
    assert host._is_dragging is False
    assert host.drag_position == QPoint(500, 500) - QRect(0, 0, 150, 150).topLeft()
    assert host._drag_start_position == QPoint(500, 500)

    # ---- 阈值内抖动：不移动窗口、不切拖拽态 ----
    jitter = _FakeMouseEvent(buttons=Qt.LeftButton, global_pos=QPoint(503, 503))
    host.mouseMoveEvent(jitter)
    assert jitter.accepted is True
    assert host.state_history == []
    assert host.moved == []

    # ---- 移动超过阈值：切拖拽态并按新全局坐标搬窗 ----
    move = _FakeMouseEvent(buttons=Qt.LeftButton, global_pos=QPoint(520, 520))
    host.mouseMoveEvent(move)
    assert move.accepted is True
    assert host.state_history[-1] == host.STATE_DRAGGING
    assert host.moved[-1] == QPoint(520, 520) - host.drag_position

    # ---- 拖拽释放：回待机 + 存位置 + 不触发菜单 ----
    release = _FakeMouseEvent(global_pos=QPoint(520, 520))
    host.mouseReleaseEvent(release)
    assert release.accepted is True
    assert host.state_history[-1] == host.STATE_IDLE
    assert host.settings.values["position"] == host.pos()
    assert host.menu_toggles == 0

    # ---- 单击（阈值内微抖动）：不出现拖拽态，释放时展开菜单 ----
    click_host = _PetHost()
    click_host.mousePressEvent(_FakeMouseEvent(button=Qt.LeftButton, global_pos=QPoint(500, 500)))
    click_host.mouseReleaseEvent(_FakeMouseEvent(global_pos=QPoint(502, 502)))
    assert click_host.STATE_DRAGGING not in click_host.state_history
    assert click_host.menu_toggles == 1

    # ---- 长按 100ms（无位移）：计时到期后切拖拽态，释放不弹菜单 ----
    long_host = _PetHost()
    long_host.mousePressEvent(_FakeMouseEvent(button=Qt.LeftButton, global_pos=QPoint(500, 500)))
    assert long_host._drag_press_timer.isActive()
    assert long_host._drag_press_timer.interval() == long_host.DRAG_LONG_PRESS_MS
    long_host._begin_drag_after_long_press()
    assert long_host.state_history[-1] == long_host.STATE_DRAGGING
    long_host.mouseReleaseEvent(_FakeMouseEvent(global_pos=QPoint(500, 500)))
    assert long_host.state_history[-1] == long_host.STATE_IDLE
    assert long_host.menu_toggles == 0

    # ---- 非左键释放：落回 super() ----
    host.mouseReleaseEvent(_FakeMouseEvent(button=Qt.RightButton))
    assert "mouseReleaseEvent" in host.calls

    # ---- 防御分支：按下处理内部异常 -> 吞异常 + ignore（防 Qt 回调异常杀进程）----
    broken = _PetHost()

    def _boom() -> None:
        """故意抛错，驱动 drag_mixin 的 except 分支。"""
        raise RuntimeError("timer stop failed")

    broken._menu_hide_timer.stop = _boom
    broken_event = _FakeMouseEvent()
    broken.mousePressEvent(broken_event)
    assert broken_event.ignored is True, "异常路径必须 ignore 而不是向外抛"
    assert broken.state_history == [], "异常时不应切到拖拽态"


# ===========================================================================
# 5. notify_mixin
# ===========================================================================
def test_notify_mixin_show_hide_and_event_filter(qt_app):
    """通知气泡展示/隐藏切换状态、复用单一单次定时器，事件过滤决定是否收起菜单。"""
    host = _PetHost()

    host.show_notification({"title": "风险预警", "message": "命中阈值"})
    assert host.state_history[-1] == host.STATE_NOTIFY
    assert "风险预警" in host.notify_label.text()
    assert "命中阈值" in host.notify_label.text()

    timer = host._notification_timer
    assert timer.isSingleShot() is True
    assert timer.isActive() is True

    # 连续通知复用同一个定时器，不会互相提前隐藏
    host.show_notification({"title": "第二次", "message": ""})
    assert host._notification_timer is timer

    # 缺省标题回退
    host.show_notification({})
    assert "新通知" in host.notify_label.text()

    host.hide_notification()
    assert host.state_history[-1] == host.STATE_IDLE

    # ---- 事件过滤：点击桌宠与菜单之外 -> 起 120ms 收起定时器 ----
    class _PanelStub:
        """菜单面板替身。"""

        @staticmethod
        def isVisible() -> bool:  # noqa: N802 - 对齐 Qt API
            """菜单视为可见。"""
            return True

        @staticmethod
        def geometry():
            """菜单几何（远离测试点击点）。"""
            return QRect(0, 0, 10, 10)

    host.menu_panel = _PanelStub()

    outside = _FakeMouseEvent(global_pos=QPoint(5000, 5000))
    assert host.eventFilter(host, outside) is False
    assert host._menu_hide_timer.started == [120]

    # 桌宠内部点击不收起
    inside = _FakeMouseEvent(global_pos=QPoint(5, 5))
    host.eventFilter(host, inside)
    assert host._menu_hide_timer.started == [120]

    # 非鼠标按下事件直接放行
    other = _FakeMouseEvent(event_type=QEvent.KeyPress, global_pos=QPoint(5000, 5000))
    host.eventFilter(host, other)
    assert host._menu_hide_timer.started == [120]


# ===========================================================================
# 6. menu_mixin
# ===========================================================================
def test_menu_mixin_show_hide_sync_and_actions(qt_app, fake_main_window):
    """菜单显隐/定位、三个动作回调接线，以及复用已有主窗口接口。"""
    fake_main_window.show_count = 0
    fake_main_window.quit_count = 0
    host = _PetHost()

    assert not hasattr(host, "menu_panel")

    host.show_menu()
    panel = host.menu_panel
    assert isinstance(panel, PetMenuPanel)
    assert host._menu_hide_timer.stop_count >= 1
    assert host._menu_animation is not None
    assert panel.isVisible() is True

    # 动作接线：打开主面板 / 关闭程序 复用主窗口接口
    panel.open_button.click()
    assert fake_main_window.show_count == 1
    panel.quit_button.click()
    assert fake_main_window.quit_count == 1

    # 待更新提示：仅弹 tooltip 并收起菜单，不触发业务
    panel.live_button.click()
    assert panel.isVisible() is False, "待更新提示应收起菜单"

    # hide_menu 清理动画引用
    host.show_menu()
    host.hide_menu()
    assert panel.isVisible() is False
    assert host._menu_animation is None

    # toggle_menu：先开后关
    # 注意：_PetHost 为统计「拖拽触发的菜单切换次数」覆写了 toggle_menu（只自增
    # menu_toggles），会遮蔽 MenuMixin 的实现。本用例测的是 MenuMixin，必须显式
    # 调基类方法，否则测到的是替身、show/hide 根本没被执行。
    MenuMixin.toggle_menu(host)
    assert panel.isVisible() is True
    MenuMixin.toggle_menu(host)
    assert panel.isVisible() is False

    # _sync_menu_position 在无 panel 时是 no-op
    bare = _PetHost()
    bare._sync_menu_position()  # 不应抛异常


# ===========================================================================
# 7. lifecycle_mixin
# ===========================================================================
def test_lifecycle_mixin_close_event_stops_timers_and_ws_thread(qt_app, fake_main_window):
    """关闭事件应停定时器、摘事件过滤器、停 WebSocket 线程并接受事件。"""
    fake_main_window.quit_count = 0
    host = _PetHost()

    # close_application 找到带 quit_application 的顶层窗口后请求退出
    host.close_application()
    assert fake_main_window.quit_count == 1

    host.show_menu()
    host.show_notification({"title": "t", "message": "m"})
    host._menu_hide_timer.stop_count = 0

    close_event = _FakeCloseEvent()
    host.closeEvent(close_event)

    assert close_event.accepted is True
    assert host.menu_panel.isVisible() is False, "关闭事件应收起菜单"
    assert host._menu_hide_timer.stop_count == 1
    assert host._notification_timer.isActive() is False
    assert host.ws_thread.stop_count == 1
    assert host.ws_thread.waits == [5000]


# ===========================================================================
# 8. base.set_state
# ===========================================================================
class _PixmapSpy:
    """记录 set_pet_pixmap 调用。"""

    def __init__(self) -> None:
        """初始化接收列表。"""
        self.pixmaps: list = []

    def set_pet_pixmap(self, pixmap) -> None:
        """记录一次图片下发。"""
        self.pixmaps.append(pixmap)


def test_pet_window_base_set_state_ignores_unknown_and_delegates_pixmap(qt_app):
    """set_state 应忽略未知状态，且仅在对应状态有图时下发图片。"""
    # 刻意不跑 __init__：PetWindowBase.__init__ 会起真实 WebSocket + QSettings
    host = PetWindowBase.__new__(PetWindowBase)
    host.current_state = PetWindowBase.STATE_IDLE
    host.images = {
        PetWindowBase.STATE_IDLE: QPixmap(2, 2),
        PetWindowBase.STATE_DRAGGING: None,
    }
    spy = _PixmapSpy()
    host.image_label = spy

    # 已知状态但图为 None -> 切状态、不下发
    host.set_state(PetWindowBase.STATE_DRAGGING)
    assert host.current_state == PetWindowBase.STATE_DRAGGING
    assert spy.pixmaps == []

    # 已知状态且有图 -> 切状态并下发
    host.set_state(PetWindowBase.STATE_IDLE)
    assert host.current_state == PetWindowBase.STATE_IDLE
    assert len(spy.pixmaps) == 1

    # 未知状态 -> 直接返回，状态不变
    host.set_state(999)
    assert host.current_state == PetWindowBase.STATE_IDLE
    assert len(spy.pixmaps) == 1


class _FakeScreen:
    """QScreen 替身：只提供 availableGeometry。"""

    def __init__(self, rect: QRect) -> None:
        """保存可用区域。"""
        self._rect = rect

    def availableGeometry(self) -> QRect:  # noqa: N802 - 对齐 Qt API
        """返回可用区域。"""
        return self._rect


def test_default_pet_position_uses_available_area_corner():
    """首次启动位置：主屏可用区域右下角（右 50px、下 100px），原点不为 0 时也落在可用区域内。

    Qt6 删除了 QApplication.desktop()，旧代码按整屏宽高计算，会压到任务栏下面。
    """
    from desktop.pet_window.base import default_pet_position

    pos = default_pet_position(_FakeScreen(QRect(0, 0, 1707, 912)), 150, 200)
    assert (pos.x(), pos.y()) == (1707 - 150 - 50, 912 - 200 - 100)
    # 任务栏在左边 / 上边：可用区域原点不为 0
    pos = default_pet_position(_FakeScreen(QRect(60, 40, 1647, 920)), 150, 200)
    assert (pos.x(), pos.y()) == (60 + 1647 - 150 - 50, 40 + 920 - 200 - 100)
    # 屏幕比桌宠还小：不跑到可用区域左上角之外
    pos = default_pet_position(_FakeScreen(QRect(10, 20, 100, 100)), 150, 200)
    assert (pos.x(), pos.y()) == (10, 20)
    # 拿不到主屏
    pos = default_pet_position(None, 150, 200)
    assert (pos.x(), pos.y()) == (100, 100)


# ===========================================================================
# 9. inertia_mixin（投掷惯性）
# ===========================================================================
class _MovingPetHost(_PetHost):
    """位置可变的宿主：pos() 跟随 move()，才能采样出真实速度。"""

    def __init__(self, start=QPoint(150, 150)) -> None:
        """记录初始位置。"""
        super().__init__()
        self._pos = QPoint(start)

    def pos(self):
        """返回当前窗口左上角。"""
        return QPoint(self._pos)

    def move(self, pos) -> None:
        """记录并应用位置。"""
        self.moved.append(QPoint(pos))
        self._pos = QPoint(pos)

    def frameGeometry(self):  # noqa: N802 - 对齐 Qt API
        """返回跟随位置的窗口几何。

        基类替身把 frameGeometry 钉在原点，会让 DragMixin 算出的抓取偏移与真实
        位置不一致（实测拖到 (600,500) 时偏移被算成 (500,500) 而不是 (350,350)），
        投掷测速也就跟着偏。这里按 ``pos()`` 返回，语义与真实无边框窗口一致。
        """
        return QRect(QPoint(self._pos), QSize(150, 150))


def _inertia_host(
    monkeypatch,
    *,
    bounds=(0.0, 0.0, 600.0, 400.0),
    start=QPoint(150, 150),
):
    """构造时钟可控、边界固定、拖拽目标不依赖真实屏幕的宿主。

    Args:
        monkeypatch: pytest monkeypatch。
        bounds: 允许窗口左上角活动的范围。
        start: 初始位置。

    Returns:
        tuple[_MovingPetHost, list]: 宿主与可写时钟（毫秒）。
    """
    host = _MovingPetHost(start)
    clock = [0.0]
    monkeypatch.setattr(host, "_inertia_now_ms", lambda: clock[0])
    monkeypatch.setattr(host, "_inertia_bounds", lambda: bounds)
    # 拖动目标一律按「光标 - 抓取偏移」换算，避免用例依赖真实屏幕几何。
    monkeypatch.setattr(
        host,
        "_drag_target_at",
        lambda cursor, offset: (cursor - host.drag_position, True),
    )
    return host, clock


def _drag_to(host, clock, x: int, y: int, *, at_ms: float) -> None:
    """把桌宠拖到指定全局坐标，并在该时刻写一帧采样。

    Args:
        host: 宿主。
        clock: 可写时钟。
        x: 鼠标全局 x。
        y: 鼠标全局 y。
        at_ms: 该帧时间戳（毫秒）。
    """
    clock[0] = float(at_ms)
    host.mouseMoveEvent(
        _FakeMouseEvent(buttons=Qt.LeftButton, global_pos=QPoint(x, y))
    )


def _throw_to_the_right(host, clock) -> None:
    """模拟一次向右的快速甩动：两帧共 200px / 100ms = 2000px/s。

    Args:
        host: 宿主。
        clock: 可写时钟。
    """
    clock[0] = 0.0
    host.mousePressEvent(_FakeMouseEvent(button=Qt.LeftButton, global_pos=QPoint(500, 500)))
    _drag_to(host, clock, 600, 500, at_ms=50)
    _drag_to(host, clock, 700, 500, at_ms=100)
    clock[0] = 110.0
    host.mouseReleaseEvent(_FakeMouseEvent(global_pos=QPoint(700, 500)))


def test_inertia_fast_flick_starts_flight_and_ticks_window(qt_app, monkeypatch):
    """快速拖动后松手：进入飞行态、启动计时器，逐帧继续移动窗口。"""
    host, clock = _inertia_host(monkeypatch)

    _throw_to_the_right(host, clock)

    assert host.state_history[-1] == host.STATE_FLYING
    assert host.flight_active is True
    assert host._inertia_timer.isActive() is True

    start_x = host.pos().x()
    moved_before = len(host.moved)
    host._inertia_tick(0.016)
    assert len(host.moved) == moved_before + 1, "每帧必须移动一次窗口"
    assert host.moved[-1].x() > start_x, "向右甩应继续向右飞"

    host._inertia_stop(save_position=False)
    assert host.flight_active is False
    assert host._inertia_timer.isActive() is False


def test_inertia_slow_drag_does_not_throw(qt_app, monkeypatch):
    """缓慢拖动（100px/s）：不投掷，也不创建计时器。"""
    host, clock = _inertia_host(monkeypatch)

    clock[0] = 0.0
    host.mousePressEvent(_FakeMouseEvent(button=Qt.LeftButton, global_pos=QPoint(500, 500)))
    _drag_to(host, clock, 540, 500, at_ms=400)  # 40px / 400ms = 100px/s
    clock[0] = 420.0
    host.mouseReleaseEvent(_FakeMouseEvent(global_pos=QPoint(540, 500)))

    assert host.flight_active is False
    assert host.STATE_FLYING not in host.state_history
    assert getattr(host, "_inertia_timer", None) is None, "未投掷不该建计时器"


def test_inertia_stale_release_after_pause_does_not_throw(qt_app, monkeypatch):
    """快拖后停住再松手：末次采样已过期，按静止处理。"""
    host, clock = _inertia_host(monkeypatch)

    clock[0] = 0.0
    host.mousePressEvent(_FakeMouseEvent(button=Qt.LeftButton, global_pos=QPoint(500, 500)))
    _drag_to(host, clock, 600, 500, at_ms=50)
    _drag_to(host, clock, 700, 500, at_ms=100)
    clock[0] = 400.0  # 停手 300ms 后才松手
    host.mouseReleaseEvent(_FakeMouseEvent(global_pos=QPoint(700, 500)))

    assert host.flight_active is False
    assert host.STATE_FLYING not in host.state_history


def test_inertia_click_without_drag_does_not_throw(qt_app, monkeypatch):
    """按下即松开（单击）：不采样不投掷，仍走菜单逻辑。"""
    host, clock = _inertia_host(monkeypatch)

    clock[0] = 0.0
    host.mousePressEvent(_FakeMouseEvent(button=Qt.LeftButton, global_pos=QPoint(500, 500)))
    clock[0] = 20.0
    host.mouseReleaseEvent(_FakeMouseEvent(global_pos=QPoint(500, 500)))

    assert host.flight_active is False
    assert host.menu_toggles == 1, "单击仍应展开菜单"


def test_inertia_flight_stays_inside_release_screen(qt_app, monkeypatch):
    """整段飞行都留在松手所在屏的可用区内（不跨屏）。"""
    bounds = (0.0, 0.0, 150.0, 100.0)
    host, clock = _inertia_host(monkeypatch, bounds=bounds)

    _throw_to_the_right(host, clock)
    assert host.flight_active is True, "起点被夹进小屏后仍应起飞"

    flight_start = len(host.moved)
    for _ in range(600):
        if not host.flight_active:
            break
        host._inertia_tick(0.016)

    assert host.flight_active is False, "飞行必须在有限帧内结束"
    assert host.moved[flight_start:], "飞行期间应有移动"
    for point in host.moved[flight_start:]:
        assert bounds[0] <= point.x() <= bounds[2]
        assert bounds[1] <= point.y() <= bounds[3]


def test_inertia_press_catches_flight_and_suppresses_menu(qt_app, monkeypatch):
    """飞行中按下：立即停飞、位置不变，且这次松手不弹菜单。"""
    host, clock = _inertia_host(monkeypatch)
    _throw_to_the_right(host, clock)
    assert host.flight_active is True
    caught_pos = host.pos()

    clock[0] = 200.0
    host.mousePressEvent(_FakeMouseEvent(button=Qt.LeftButton, global_pos=QPoint(500, 500)))
    assert host.flight_active is False, "按下即接住"
    assert host._inertia_timer.isActive() is False
    assert host.pos() == caught_pos, "接住不能改变位置"
    assert host._suppress_click_menu is True

    clock[0] = 210.0
    host.mouseReleaseEvent(_FakeMouseEvent(global_pos=QPoint(500, 500)))
    assert host.menu_toggles == 0, "接住那一下不应弹菜单"
    assert host.state_history[-1] == host.STATE_IDLE


def test_inertia_right_press_catches_without_eating_next_click(qt_app, monkeypatch):
    """右键按下也接住，但不会吞掉随后一次正常左键单击的菜单。"""
    host, clock = _inertia_host(monkeypatch)
    _throw_to_the_right(host, clock)
    assert host.flight_active is True

    clock[0] = 200.0
    host.mousePressEvent(_FakeMouseEvent(button=Qt.RightButton, global_pos=QPoint(500, 500)))
    assert host.flight_active is False, "任意键按下都应接住"
    assert host._suppress_click_menu is False, "右键不该设置「不弹菜单」标记"
    clock[0] = 205.0
    host.mouseReleaseEvent(_FakeMouseEvent(button=Qt.RightButton, global_pos=QPoint(500, 500)))

    # 随后一次左键单击应照常展开菜单（右键那次不能污染标记）
    clock[0] = 260.0
    host.mousePressEvent(_FakeMouseEvent(button=Qt.LeftButton, global_pos=QPoint(500, 500)))
    clock[0] = 270.0
    host.mouseReleaseEvent(_FakeMouseEvent(global_pos=QPoint(500, 500)))
    assert host.menu_toggles == 1


def test_inertia_tick_swallows_errors_and_stops(qt_app, monkeypatch):
    """飞行推进内部异常：不冒泡、停飞、不再每帧刷错误。"""
    host, clock = _inertia_host(monkeypatch)
    _throw_to_the_right(host, clock)

    def _boom(*args, **kwargs):
        """故意抛错，驱动 tick 的 except 分支。"""
        raise RuntimeError("physics failed")

    monkeypatch.setattr("desktop.pet_window.inertia_mixin.step_flight", _boom)
    host._inertia_tick(0.016)  # 不应向外抛异常

    assert host.flight_active is False
    assert host._inertia_timer.isActive() is False


def test_inertia_finish_keeps_notification_state(qt_app, monkeypatch):
    """飞行途中收到通知：收尾只清飞行态，不覆盖通知态。"""
    host, clock = _inertia_host(monkeypatch)
    _throw_to_the_right(host, clock)

    host.show_notification({"title": "预警", "message": "命中"})
    for _ in range(600):
        if not host.flight_active:
            break
        host._inertia_tick(0.016)

    assert host.flight_active is False
    assert host.current_state == host.STATE_NOTIFY
    host._notification_timer.stop()


def test_close_event_stops_flight(qt_app, monkeypatch):
    """关闭窗口时必须停掉飞行计时器，避免销毁后仍收到 tick。"""
    host, clock = _inertia_host(monkeypatch)
    _throw_to_the_right(host, clock)
    assert host._inertia_timer.isActive() is True

    host.closeEvent(_FakeCloseEvent())

    assert host._inertia_timer.isActive() is False
    assert host.flight_active is False

