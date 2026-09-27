"""
桌宠悬浮窗 - WebSocket通信的可爱桌面宠物
支持拖拽、状态切换、消息推送

本模块实现桌面宠物悬浮窗，作为运营工具的可爱交互界面：

一、WebSocketThread 通信线程
- 独立 QThread 运行 WebSocket 客户端
- 连接后端 /ws 端点，接收实时预警推送
- 30 秒心跳保活
- 信号：message_received/connected/disconnected/error

二、PetWindow 桌宠窗口
- 无边框 + 置顶 + 透明背景悬浮窗
- 四种状态：待机/拖拽/点击/通知（对应不同颜色占位图）
- 拖拽移动：鼠标按下记录偏移，移动实时更新位置
# 用新值覆盖旧值，保持数据一致
- 通知气泡：收到 alert 消息显示 5 秒后自动隐藏
# 将内容呈现到界面上
- 双击触发功能菜单（预留扩展点）

三、消息协议
- 收到 {type: "alert", data: {title, message}} 显示通知
# 将内容呈现到界面上
- 收到 {type: "ping"/"pong"} 心跳保活

启动方式：
# 触发服务/线程开始运行
- 独立进程：python pet_window.py
- 也可集成进主应用（run_pet_window 入口）

注意：
- 图片为占位符（彩色圆形），实际接入美术素材时
  替换 load_placeholder_images 即可
- WebSocket 未连接时保持待机状态，不阻塞界面
"""
import sys
# 导入模块
import asyncio
# 从 typing 导入符号
from typing import Optional
# 从 PyQt5.QtWidgets 导入符号
from PyQt5.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QFrame, QPushButton, QApplication,
    QToolTip
)
# 从 PyQt5.QtCore 导入窗口、动画、配置和信号相关类型。
from PyQt5.QtCore import (
    Qt, QPoint, QSize, QTimer, QEvent, pyqtSignal, QThread, pyqtProperty,
    QPropertyAnimation, QParallelAnimationGroup, QEasingCurve, QSettings
)
# 从 PyQt5.QtGui 导入图像、绘制和鼠标相关类型。
from PyQt5.QtGui import QPixmap, QPainter, QCursor, QColor

from desktop.icon_utils import load_app_icon
# 导入模块
import websocket
# 导入模块
import json
# 导入模块
import threading
# 导入模块
import logging
from pathlib import Path

from .assets import _asset_roots, PET_ASSET_DIRS, logger
from .ws_thread import WebSocketThread
from .image_label import PetImageLabel
from .menu_panel import PetMenuPanel
from .base import PetWindowBase
from .ws_mixin import WsMixin
from .notify_mixin import NotifyMixin
from .menu_mixin import MenuMixin
from .drag_mixin import DragMixin
from .lifecycle_mixin import LifecycleMixin


class PetWindow(PetWindowBase, WsMixin, NotifyMixin, MenuMixin, DragMixin, LifecycleMixin, QWidget):
    """桌宠悬浮窗
    
    无边框置顶透明窗口，支持拖拽与消息通知展示。
    # 将内容呈现到界面上
    
    状态机：
    - STATE_IDLE: 待机（蓝色）
    - STATE_DRAGGING: 拖拽中（橙色）
    - STATE_CLICKED: 点击（浅蓝）
    - STATE_NOTIFY: 通知（粉色）
    """
    pass


from .runner import run_pet_window

__all__ = [
    "PetWindow",
    "WebSocketThread",
    "PetImageLabel",
    "PetMenuPanel",
    "run_pet_window",
    "PET_ASSET_DIRS",
    "_asset_roots",
    "logger",
]
