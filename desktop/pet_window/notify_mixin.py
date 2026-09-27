"""桌宠通知气泡与事件过滤。

拆分自原 pet_window.py 的 PetWindow 通知部分：
收到 alert 消息显示通知气泡，5 秒后自动隐藏。
"""
from PyQt5.QtCore import QEvent, QTimer

from .assets import logger


class NotifyMixin:
    """通知混入：通知气泡展示/隐藏与应用级事件过滤。"""

    def show_notification(self, alert_data: dict):
        """显示通知气泡
        # 将内容呈现到界面上
        
        Args:
            alert_data: 预警数据
        """
        logger.info(f"[桌宠] 显示通知: {alert_data}")
        
        # 切换到通知状态
        self.set_state(self.STATE_NOTIFY)
        
        # 显示通知文本
        # 将内容呈现到界面上
        title = alert_data.get('title', '新通知')
        # 读取字典/配置项
        message = alert_data.get('message', '')
        
        # 设置文本
        self.notify_label.setText(f"<b>{title}</b><br>{message}")
        # 显示窗口/内容
        # 将内容呈现到界面上
        self.notify_label.show()
        
        # 单一可取消通知定时器，连续通知不会互相提前隐藏。
        if not hasattr(self, "_notification_timer"):
            self._notification_timer = QTimer(self)
            self._notification_timer.setSingleShot(True)
            self._notification_timer.timeout.connect(self.hide_notification)
        self._notification_timer.start(5000)

    def hide_notification(self):
        """隐藏通知气泡
        
        关闭通知显示，恢复待机状态。
        # 将内容呈现到界面上
        """
        self.notify_label.hide()
        # 设置state属性
        self.set_state(self.STATE_IDLE)

    def eventFilter(self, watched, event):
        """处理应用级点击事件，点击菜单和桌宠之外区域时收起菜单。"""
        if event.type() == QEvent.MouseButtonPress and hasattr(self, "menu_panel"):
            if self.menu_panel.isVisible():
                position = event.globalPos()
                inside_pet = self.geometry().contains(self.mapFromGlobal(position))
                inside_menu = self.menu_panel.geometry().contains(position)
                if not inside_pet and not inside_menu:
                    self._menu_hide_timer.start(120)
        return super().eventFilter(watched, event)
