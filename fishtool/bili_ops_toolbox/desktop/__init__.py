"""
PyQt桌面客户端
内嵌WebUI的桌面应用，包含桌宠悬浮窗

本包提供桌面端两个组件：
- main_window: MainWindow 主窗口（内嵌WebUI + 系统托盘）
- pet_window: PetWindow 桌宠悬浮窗（WebSocket通知）

架构说明：
桌面端是"壳"，业务全部由 Web 服务提供。
MainWindow 加载本地 FastAPI 服务页面；
PetWindow 通过 WebSocket 接收实时预警。

外部使用方式：
    from desktop import MainWindow, PetWindow
"""
from .main_window import MainWindow
from .pet_window import PetWindow

__all__ = ['MainWindow', 'PetWindow']

