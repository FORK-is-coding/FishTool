"""桌宠右键功能菜单。

拆分自原 pet_window.py 的 PetWindow 菜单部分：
双击触发功能菜单，支持动画弹出、自动收起与主面板跳转。
"""
from PyQt5.QtCore import QEasingCurve, QParallelAnimationGroup, QPoint, QPropertyAnimation
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import QApplication, QToolTip

from .assets import logger
from .menu_panel import PetMenuPanel


class MenuMixin:
    """菜单混入：菜单显示/隐藏/定位与主面板跳转。"""

    def toggle_menu(self):

        """显示或收起桌宠快捷菜单，并播放淡入位移动画。"""
        if hasattr(self, "menu_panel") and self.menu_panel.isVisible():
            self.hide_menu()
            return
        self.show_menu()

    def _sync_menu_position(self):
        """根据桌宠当前全局坐标同步快捷卡片位置。

        Returns:
            None: 仅更新已创建菜单面板的位置。
        """
        if not hasattr(self, "menu_panel"):
            return
        # 每次桌宠移动都重新计算右侧锚点，避免卡片停留在首次打开位置。
        target = self.mapToGlobal(QPoint(self.width() + 8, 0))
        self.menu_panel.move(target)

    def show_menu(self):
        """创建并显示三个菜单卡片，动作复用已有主窗口接口。"""
        if not hasattr(self, "menu_panel"):
            self.menu_panel = PetMenuPanel()
            self.menu_panel.open_button.clicked.connect(self.open_main_panel)
            self.menu_panel.live_button.clicked.connect(self.show_pending_message)
            self.menu_panel.quit_button.clicked.connect(self.close_application)
        self._menu_hide_timer.stop()
        # 先按桌宠当前位置定位，再播放菜单淡入动画。
        self._sync_menu_position()
        target = self.menu_panel.pos()
        self.menu_panel.move(target + QPoint(0, 8))
        self.menu_panel.setWindowOpacity(0.0)
        self.menu_panel.show()
        self.menu_panel.raise_()
        fade = QPropertyAnimation(self.menu_panel, b"windowOpacity")
        fade.setDuration(180)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.OutCubic)
        slide = QPropertyAnimation(self.menu_panel, b"pos")
        slide.setDuration(180)
        slide.setStartValue(target + QPoint(0, 8))
        slide.setEndValue(target)
        slide.setEasingCurve(QEasingCurve.OutCubic)
        animation = QParallelAnimationGroup(self)
        animation.addAnimation(fade)
        animation.addAnimation(slide)
        self._menu_animation = animation
        animation.start()

    def hide_menu(self):
        """立即收起菜单并清理动画引用。"""
        if hasattr(self, "menu_panel"):
            self.menu_panel.hide()
        self._menu_animation = None

    def open_main_panel(self):
        """查找现有主窗口并调用其显示逻辑。"""
        try:
            for widget in QApplication.topLevelWidgets():
                if hasattr(widget, "show_window"):
                    widget.show_window()
                    self.hide_menu()
                    return
            logger.warning("[桌宠] 未找到主窗口，无法打开主面板")
        except Exception as exc:
            logger.error(f"[桌宠] 打开主面板失败: {exc}")

    def show_pending_message(self):
        """显示待更新提示，不触发任何业务或网络操作。"""
        QToolTip.showText(QCursor.pos(), "舆情预警实况功能待更新")
        self.hide_menu()
