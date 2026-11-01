"""桌宠快捷菜单面板。

拆分自原 pet_window.py 的 PetMenuPanel：
仅负责三个动作卡片的视觉承载。
"""
from PySide6.QtWidgets import QFrame, QPushButton, QVBoxLayout
from PySide6.QtCore import Qt

from desktop.ui_theme import pet_menu_style

class PetMenuPanel(QFrame):
    """桌宠快捷菜单面板，仅负责三个动作卡片的视觉承载。"""

    def __init__(self, parent=None):
        """创建无边框菜单面板和三个快捷按钮。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setObjectName("petMenuPanel")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(236, 176)
        # 与 Web 面板一致的白底卡片和按钮外观，配色来自 desktop.ui_theme（对应 style.base.css 的设计令牌）。
        self.setStyleSheet(pet_menu_style())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignTop)
        self.open_button = QPushButton("打开主面板")
        self.open_button.setObjectName("petMenuPrimary")
        self.live_button = QPushButton("舆情预警实况（待更新）")
        self.quit_button = QPushButton("关闭程序")
        self.quit_button.setObjectName("petMenuDanger")
        for button in (self.open_button, self.live_button, self.quit_button):
            layout.addWidget(button)
