"""桌宠生命周期：关闭程序与窗口关闭事件。

拆分自原 pet_window.py 的 PetWindow 生命周期部分。
"""
from PyQt5.QtWidgets import QApplication

from .assets import logger


class LifecycleMixin:
    """生命周期混入：关闭应用与窗口关闭清理。"""

    def close_application(self):
        """调用现有主窗口退出逻辑，确保程序优雅清理资源。"""
        try:
            for widget in QApplication.topLevelWidgets():
                if hasattr(widget, "quit_application"):
                    self.hide_menu()
                    widget.quit_application()
                    return
            logger.warning("[桌宠] 未找到主窗口，无法执行退出逻辑")
        except Exception as exc:
            logger.error(f"[桌宠] 请求退出失败: {exc}")

    def closeEvent(self, event):
        """窗口关闭事件
        # 释放连接/窗口资源
        
        停止 WebSocket 线程后关闭窗口。
        # 释放连接/窗口资源
        
        Args:
            event: 关闭事件对象
            # 释放连接/窗口资源
        """
        logger.info("[桌宠] 窗口关闭")
        self.hide_menu()
        self._menu_hide_timer.stop()
        if hasattr(self, "_notification_timer"):
            self._notification_timer.stop()
        QApplication.instance().removeEventFilter(self)

        if hasattr(self, 'ws_thread'):
            self.ws_thread.stop()
            # 等待一段时间
            # 阻塞直到条件满足或超时
            self.ws_thread.wait(5000)
        
        # 接受连接
        event.accept()
