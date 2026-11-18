"""
PySide6 桌面客户端（Qt 6，WebEngine 内核 Chromium 140）
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
from importlib import import_module


def __getattr__(name):
    modules = {"MainWindow": "main_window", "PetWindow": "pet_window"}
    if name not in modules:
        raise AttributeError(name)
    value = getattr(import_module(f".{modules[name]}", __name__), name)
    globals()[name] = value
    return value

__all__ = ['MainWindow', 'PetWindow']

