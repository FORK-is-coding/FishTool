"""桌宠拖拽与悬停交互。

拆分自原 pet_window.py 的 PetWindow 交互部分：
支持长按/拖拽移动、悬停缩放与单击菜单触发。
"""
from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from .assets import logger
from .geometry import clamp_to_desktop, drag_target, drag_target_across_screens


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
                cursor = event.globalPosition().toPoint()
                self.drag_position = cursor - self.frameGeometry().topLeft()
                # 抓取偏移换算成物理像素保存：跨到 DPR 不同的屏幕后按当前 DPR 折回，
                # 否则偏移会被放大/缩小，表现为「桌宠不在鼠标下面」。
                dpr = self._screen_dpr_at(cursor)
                self._drag_offset_physical = (self.drag_position.x() * dpr, self.drag_position.y() * dpr)
                self._drag_start_position = cursor
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
                if self._drag_distance(event.globalPosition().toPoint()) < self._drag_threshold():
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
            cursor = event.globalPosition().toPoint()
            offset_phys = getattr(self, "_drag_offset_physical", None)
            if offset_phys is None:
                target = cursor - self.drag_position
            else:
                target, exact = self._drag_target_at(cursor, offset_phys)
                if not exact:
                    target = self._fit_target_to_cursor_screen(target, cursor)
            self.move(target)
            # 跨 DPI 边界时 Qt 会重算窗口尺寸，这里每帧纠正一次，避免累积漂移。
            self._keep_pet_size_after_move()
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
            moved_enough = self._drag_distance(event.globalPosition().toPoint()) >= self._drag_threshold()
            was_dragging = getattr(self, "_is_dragging", False) or moved_enough
            self._is_dragging = False
            self.set_state(self.STATE_IDLE)
            # 保存位置前先确保窗口落在可见区内，避免坏坐标被持久化。
            self._save_position_within_desktop()
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

    def _drag_target_at(self, cursor, offset_phys):
        """算出拖动到 ``cursor`` 时窗口应处的逻辑坐标。

        优先走跨屏物理换算（避免落进异构 DPI 的逻辑空隙），拿不到屏幕信息时
        退回「鼠标逻辑坐标 - 物理偏移 / 当前 DPR」的近似算法。

        Args:
            cursor: 鼠标当前全局坐标（``QPoint``）。
            offset_phys: 按下时记录的物理偏移 ``(x, y)``。

        Returns:
            tuple[QPoint, bool]: 窗口左上角逻辑坐标，以及是否由跨屏物理换算得出
            （``True`` 表示结果已落在某块屏的逻辑空间内，无需再做可见性兜底）。
        """
        screens = QGuiApplication.screens()
        primary = QGuiApplication.primaryScreen()
        snapshot = [
            (s.geometry().x(), s.geometry().y(), s.geometry().width(), s.geometry().height(), float(s.devicePixelRatio()) or 1.0)
            for s in screens
        ]
        primary_dpr = float(primary.devicePixelRatio()) if primary is not None else 1.0
        exact = drag_target_across_screens(
            cursor.x(), cursor.y(), offset_phys[0], offset_phys[1], snapshot, primary_dpr or 1.0
        )
        if exact is not None:
            return QPoint(*exact), True
        dpr = self._screen_dpr_at(cursor)
        approx = drag_target(cursor.x(), cursor.y(), offset_phys[0], offset_phys[1], dpr)
        return QPoint(*approx), False

    def _fit_target_to_cursor_screen(self, target, cursor):
        """把拖动目标夹进鼠标所在屏幕，避免落进多屏逻辑空隙。

        异构 DPI 下 Qt 的逻辑坐标空间在屏幕之间可能不连续（主屏 100% + 副屏
        150% 时存在 853px 的空隙）。目标中心一旦落进空隙，``move()`` 的落点就
        不可预测（实测逻辑 -812 会被送到物理 62、-612 送到物理 362，直接被丢到
        另一块屏）。这里把目标中心夹进鼠标所在屏幕的可用区：代价是交界处最多
        贴边一个抓取偏移，但位置始终可预测、始终跟手。

        目标中心仍在某块屏内时原样返回（允许半挂屏幕边缘）。

        Args:
            target: 期望的窗口左上角（``QPoint``）。
            cursor: 鼠标当前全局坐标（``QPoint``）。

        Returns:
            QPoint: 夹紧后的窗口左上角。
        """
        width, height = self.width(), self.height()
        center = QPoint(target.x() + width // 2, target.y() + height // 2)
        if QGuiApplication.screenAt(center) is not None:
            return target
        screen = QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()
        if screen is None:
            return target
        area = screen.availableGeometry()
        if width > area.width():
            next_x = area.x()
        else:
            next_x = min(max(target.x(), area.x()), area.x() + area.width() - width)
        if height > area.height():
            next_y = area.y()
        else:
            next_y = min(max(target.y(), area.y()), area.y() + area.height() - height)
        return QPoint(next_x, next_y)

    def _screen_dpr_at(self, point) -> float:
        """返回指定逻辑坐标所在屏幕的设备像素比。

        跨 DPI 拖动时用「鼠标当前所在屏幕」而不是「窗口当前所在屏幕」：窗口还
        停在旧屏时，旧屏的 DPR 会把偏移折算错。

        Args:
            point: 全局逻辑坐标（``QPoint``）。

        Returns:
            float: 设备像素比；找不到屏幕时按 1.0 处理。
        """
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        if screen is None:
            return 1.0
        return float(screen.devicePixelRatio()) or 1.0

    def _keep_pet_size_after_move(self) -> None:
        """窗口移动后纠正跨屏尺寸漂移。

        真实窗口由 :meth:`PetWindowBase._keep_pet_size` 实现；测试替身没有窗口
        尺寸概念，直接跳过，不影响事件流程。
        """
        keeper = getattr(self, "_keep_pet_size", None)
        if keeper is not None:
            keeper()

    def _save_position_within_desktop(self) -> None:
        """把窗口拉回可见区后保存位置。

        跨不同分辨率的屏幕拖动时，Qt 的逻辑坐标在屏幕之间并不连续，窗口可能落到
        任何屏幕之外；这里在松手（低频时机）做一次兜底，避免坏坐标被写进 QSettings
        后重启依然「消失」。拖动过程中不做约束，以免与用户手势对抗。
        """
        # 先恢复逻辑尺寸，否则跨屏漂移后的宽高会让夹紧算错目标矩形。
        keeper = getattr(self, "_keep_pet_size", None)
        if keeper is not None:
            keeper()
        frame = self.frameGeometry()
        rect = (self.pos().x(), self.pos().y(), frame.width(), frame.height())
        fitted = rect
        try:
            fitted = clamp_to_desktop(rect)
        except Exception as exc:  # noqa: BLE001 - 校正失败不能打断松手流程
            logger.error(f"[桌宠] 可见性校正失败，保留原位置: {exc}", exc_info=True)
        if (fitted[0], fitted[1]) != (rect[0], rect[1]):
            logger.info(f"[桌宠] 位置不在可见区，已拉回: {rect} -> {fitted}")
            self.move(QPoint(fitted[0], fitted[1]))
        # 保存校正后的坐标：即使窗口因 DPR 换算被 Qt 微调，下次启动也会再校正一次。
        self.settings.setValue("position", QPoint(fitted[0], fitted[1]))

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
