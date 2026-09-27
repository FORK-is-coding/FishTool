"""桌宠拖拽与悬停交互。

拆分自原 pet_window.py 的 PetWindow 交互部分：
支持鼠标拖拽移动、悬停缩放与双击菜单触发。
"""
from PyQt5.QtCore import QEasingCurve, QPropertyAnimation, Qt

from .assets import logger


class DragMixin:
    """拖拽混入：悬停动画与鼠标事件处理。"""

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
        """鼠标按下时记录拖拽偏移并切换拖拽素材。"""
        try:
            if event.button() == Qt.LeftButton:
                self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
                self._drag_start_position = event.globalPos()
                self._menu_hide_timer.stop()
                self.set_state(self.STATE_DRAGGING)
                event.accept()
            else:
                super().mousePressEvent(event)
        except Exception as exc:
            logger.error(f"[桌宠] 鼠标按下处理失败: {exc}")
            event.ignore()

    def mouseMoveEvent(self, event):
        """鼠标移动事件（拖拽）
        
        根据鼠标移动距离实时更新窗口位置。
        # 用新值覆盖旧值，保持数据一致
        
        Args:
            event: 鼠标事件对象
        """
        if event.buttons() & Qt.LeftButton:
            # 先移动桌宠窗口，再按新位置同步快捷卡片；后续菜单功能改动不能省略 move。
            self.move(event.globalPos() - self.drag_position)
            self._sync_menu_position()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """鼠标释放事件
        
        拖拽结束恢复待机状态。
        
        Args:
            event: 鼠标事件对象
        """
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return

        try:
            self.set_state(self.STATE_IDLE)
            # 保存位置到本地配置，下一次启动可恢复桌宠位置。
            self.settings.setValue("position", self.pos())
            # 拖拽释放后再同步一次，消除最后一帧位置误差。
            self._sync_menu_position()
            if (event.globalPos() - self._drag_start_position).manhattanLength() <= 4:
                self.toggle_menu()
            event.accept()
        except Exception as exc:
            # Qt 事件回调中的未捕获异常可能直接终止窗口进程。
            self.set_state(self.STATE_IDLE)
            logger.exception("[桌宠] 鼠标释放处理失败: %s", exc)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        """鼠标双击事件处理
        # 对数据进行加工/分发
        
        双击桌宠触发快捷功能菜单（待实现）。
        当前仅演示状态切换。
        
        Args:
            event: 鼠标事件对象
        """
        if event.button() == Qt.LeftButton:
            logger.info("[桌宠] 双击，触发功能弹出")
            # 点击状态仅作为瞬时反馈，菜单由独立展示逻辑负责。
            self.set_state(self.STATE_IDLE)
