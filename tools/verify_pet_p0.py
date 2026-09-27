"""FishTool 桌宠 P0 交互回归验证。

使用离线桌宠模拟 hover、拖拽、单击和三卡片操作，避免依赖 WebSocket 服务。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from PyQt5.QtCore import QEvent, QPoint, QSettings, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from desktop.pet_window import PetWindow  # noqa: E402


class OfflinePetWindow(PetWindow):
    """禁用 WebSocket 的测试桌宠，确保交互测试不受网络状态影响。"""

    def setup_websocket(self) -> None:
        """跳过通信线程初始化，无输入参数和返回值。"""


class MainWindowProbe(QWidget):
    """模拟主窗口的显示接口，用于验证三卡片中的主面板动作。"""

    def __init__(self) -> None:
        """初始化调用计数器，无输入参数和返回值。"""
        super().__init__()
        self.show_window_calls = 0

    def show_window(self) -> None:
        """记录主界面显示请求，无输入参数和返回值。"""
        self.show_window_calls += 1
        self.show()


def create_mouse_event(
    event_type: QEvent.Type,
    local_position: QPoint,
    global_position: QPoint,
    button: Qt.MouseButton,
    buttons: Qt.MouseButtons,
) -> QMouseEvent:
    """创建 Qt 鼠标事件。

    Args:
        event_type: 鼠标事件类型。
        local_position: 窗口内坐标。
        global_position: 屏幕坐标。
        button: 当前触发按键。
        buttons: 当前按下按键集合。

    Returns:
        QMouseEvent: 可直接传给桌宠事件处理器的事件。
    """
    return QMouseEvent(
        event_type,
        local_position,
        global_position,
        button,
        buttons,
        Qt.NoModifier,
    )


def verify_pet_interactions(app: QApplication) -> dict[str, bool]:
    """执行桌宠完整交互回归。

    Args:
        app: 当前 Qt 应用实例。

    Returns:
        dict[str, bool]: 每个验收项的布尔结果。
    """
    pet = OfflinePetWindow()
    main_probe = MainWindowProbe()
    with tempfile.TemporaryDirectory(prefix="fishtool-pet-p0-") as temp_dir:
        pet.settings = QSettings(str(Path(temp_dir) / "pet.ini"), QSettings.IniFormat)
        pet.show()
        app.processEvents()

        pet.enterEvent(QEvent(QEvent.Enter))
        QTest.qWait(220)
        hover_in = pet.image_label.scale_factor > 1.0
        pet.leaveEvent(QEvent(QEvent.Leave))
        QTest.qWait(240)
        hover_out = abs(pet.image_label.scale_factor - 1.0) < 0.01

        local_position = QPoint(pet.width() // 2, pet.height() // 2)
        drag_start = pet.mapToGlobal(local_position)
        drag_target = drag_start + QPoint(60, 35)
        old_position = pet.pos()
        pet.mousePressEvent(create_mouse_event(
            QEvent.MouseButtonPress, local_position, drag_start,
            Qt.LeftButton, Qt.LeftButton,
        ))
        pet.mouseMoveEvent(create_mouse_event(
            QEvent.MouseMove, pet.mapFromGlobal(drag_target), drag_target,
            Qt.NoButton, Qt.LeftButton,
        ))
        pet.mouseReleaseEvent(create_mouse_event(
            QEvent.MouseButtonRelease, pet.mapFromGlobal(drag_target), drag_target,
            Qt.LeftButton, Qt.NoButton,
        ))
        app.processEvents()
        drag_ok = pet.pos() == old_position + QPoint(60, 35)
        drag_idle = pet.current_state == pet.STATE_IDLE

        click_local = QPoint(pet.width() // 2, pet.height() // 2)
        click_global = pet.mapToGlobal(click_local)
        pet.mousePressEvent(create_mouse_event(
            QEvent.MouseButtonPress, click_local, click_global,
            Qt.LeftButton, Qt.LeftButton,
        ))
        pet.mouseReleaseEvent(create_mouse_event(
            QEvent.MouseButtonRelease, click_local, click_global,
            Qt.LeftButton, Qt.NoButton,
        ))
        QTest.qWait(240)
        menu_visible = hasattr(pet, "menu_panel") and pet.menu_panel.isVisible()
        three_cards = menu_visible and all(
            button is not None
            for button in (
                pet.menu_panel.open_button,
                pet.menu_panel.live_button,
                pet.menu_panel.quit_button,
            )
        )

        pet.menu_panel.open_button.click()
        app.processEvents()
        main_window_ok = main_probe.show_window_calls == 1 and main_probe.isVisible()

        pet.close()
        main_probe.close()
        app.processEvents()

    return {
        "hoverIn": hover_in,
        "hoverOut": hover_out,
        "dragMoved": drag_ok,
        "dragIdleAfterRelease": drag_idle,
        "clickMenuVisible": menu_visible,
        "threeCardsPresent": three_cards,
        "mainWindowAction": main_window_ok,
    }


def main() -> int:
    """运行回归验证并通过退出码表示成败，无输入参数。

    Returns:
        int: 全部通过返回 0，否则返回 1。
    """
    app = QApplication.instance() or QApplication(sys.argv)
    result = verify_pet_interactions(app)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
