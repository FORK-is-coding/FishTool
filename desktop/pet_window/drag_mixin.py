"""桌宠拖拽与悬停交互。

拆分自原 pet_window.py 的 PetWindow 交互部分：
支持长按/拖拽移动、悬停缩放与单击菜单触发。
"""
from PyQt5.QtCore import QEasingCurve, QPropertyAnimation, QTimer, Qt
from PyQt5.QtWidgets import QApplication

from .assets import logger


class DragMixin:
    """拖拽混入：悬停动画与鼠标事件处理。"""

    #: 长按超过该毫秒数即进入拖拽态（即使鼠标还没移动，也给用户确认感）。
    DRAG_LONG_PRESS_MS = 100

    #: 快速拖动时的位移兜底阈值（像素）。
    DRAG_START_DISTANCE = 6

    def _drag_threshold(self) -> int:
        """返回拖拽判定阈值。

        Returns:
            int: 系统拖拽距离与本地下限的较大值。
        """
        return max(self.DRAG_START_DISTANCE, QApplication.startDragDistance())

    def _drag_distance(self, global_pos) -> int:
        """计算当前鼠标位置相对按下起点的曼哈顿距离。

        Args:
            global_pos: 当前鼠标全局坐标。

        Returns:
            int: 曼哈顿距离（像素）。
        """
        return (global_pos - self._drag_start_position).manhattanLength()

    def _ensure_drag_press_timer(self) -> QTimer:
        """懒创建长按计时器。

        Returns:
            QTimer: 单次触发的长按判定计时器。
        """
        timer = getattr(self, "_drag_press_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._begin_drag_after_long_press)
            self._drag_press_timer = timer
        return timer

    def _begin_drag_after_long_press(self) -> None:
        """长按计时到期：鼠标仍按着时切到拖拽态，提供视觉确认。"""
        if not getattr(self, "_left_button_pressed", False):
            return
        if getattr(self, "_is_dragging", False):
            return
        self._is_dragging = True
        self.set_state(self.STATE_DRAGGING)

    def enterEvent(self, event):
        """鼠标进入桌宠区域时播放轻微放大动画。"""
        self._hover_animation = QPropertyAnimation(self.image_label, b"scale_factor", self)
        self._hover_animation.setDuration(160)
        self._hover_animation.setStartValue(self.image_label.scale_factor)
        self._hover_animation.setEndValue(1.06)
        self._hover_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._hover_animation.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开桌宠区域时平滑恢复原始大小。"""
        self._hover_animation = QPropertyAnimation(self.image_label, b"scale_factor", self)
        self._hover_animation.setDuration(180)
        self._hover_animation.setStartValue(self.image_label.scale_factor)
        self._hover_animation.setEndValue(1.0)
        self._hover_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._hover_animation.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        """鼠标按下时记录起点并启动长按计时；超过 100ms 才进入拖拽态。"""
        try:
            if event.button() == Qt.LeftButton:
                self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
                self._drag_start_position = event.globalPos()
                # 关键：按下瞬间不能切 STATE_DRAGGING，否则单击会闪拖拽素材。
                self._is_dragging = False
                self._left_button_pressed = True
                self._menu_hide_timer.stop()
                self._ensure_drag_press_timer().start(self.DRAG_LONG_PRESS_MS)
                event.accept()
            else:
                super().mousePressEvent(event)
        except Exception as exc:
            self._left_button_pressed = False
            timer = getattr(self, "_drag_press_timer", None)
            if timer is not None:
                timer.stop()
            logger.error(f"[桌宠] 鼠标按下处理失败: {exc}", exc_info=True)
            event.ignore()

    def mouseMoveEvent(self, event):
        """鼠标移动事件（拖拽）

        长按计时已切拖拽态，或位移超过快速拖拽阈值时移动窗口；
        阈值内的轻微抖动保持单击语义，不移动、不变形。

        Args:
            event: 鼠标事件对象
        """
        if event.buttons() & Qt.LeftButton:
            if not getattr(self, "_is_dragging", False):
                if self._drag_distance(event.globalPos()) < self._drag_threshold():
                    # 还没达到拖拽阈值：吞掉事件，防止窗口被 1~2px 抖动带偏。
                    event.accept()
                    return
                # 快速拖动：不等长按计时，直接进入拖拽态。
                timer = getattr(self, "_drag_press_timer", None)
                if timer is not None:
                    timer.stop()
                self._is_dragging = True
                self.set_state(self.STATE_DRAGGING)
            # 先移动桌宠窗口，再按新位置同步快捷卡片；后续菜单功能改动不能省略 move。
            self.move(event.globalPos() - self.drag_position)
            self._sync_menu_position()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """鼠标释放事件

        长按/拖动过的释放按拖拽结束处理，不展开菜单；
        快速单击未触发长按计时也没有位移时，展开/收起菜单。

        Args:
            event: 鼠标事件对象
        """
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return

        try:
            timer = getattr(self, "_drag_press_timer", None)
            if timer is not None:
                timer.stop()
            self._left_button_pressed = False
            moved_enough = self._drag_distance(event.globalPos()) >= self._drag_threshold()
            was_dragging = getattr(self, "_is_dragging", False) or moved_enough
            self._is_dragging = False
            self.set_state(self.STATE_IDLE)
            # 保存位置到本地配置，下一次启动可恢复桌宠位置。
            self.settings.setValue("position", self.pos())
            # 拖拽释放后再同步一次，消除最后一帧位置误差。
            self._sync_menu_position()
            if not was_dragging:
                self.toggle_menu()
            event.accept()
        except Exception as exc:
            # Qt 事件回调中的未捕获异常可能直接终止窗口进程。
            self._is_dragging = False
            self._left_button_pressed = False
            self.set_state(self.STATE_IDLE)
            logger.exception("[桌宠] 鼠标释放处理失败: %s", exc)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        """鼠标双击事件处理

        双击桌宠不额外改变状态，菜单仍由单击释放逻辑负责。

        Args:
            event: 鼠标事件对象
        """
        if event.button() == Qt.LeftButton:
            logger.info("[桌宠] 双击")
            timer = getattr(self, "_drag_press_timer", None)
            if timer is not None:
                timer.stop()
            self._left_button_pressed = False
            self.set_state(self.STATE_IDLE)
