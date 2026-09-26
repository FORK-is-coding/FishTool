"""桌宠快捷菜单面板。

拆分自原 pet_window.py 的 PetMenuPanel：
仅负责三个动作卡片的视觉承载。
"""
from PyQt5.QtWidgets import QFrame, QPushButton, QVBoxLayout
from PyQt5.QtCore import Qt

class PetMenuPanel(QFrame):
    """桌宠快捷菜单面板，仅负责三个动作卡片的视觉承载。"""

    def __init__(self, parent=None):
        """创建无边框菜单面板和三个快捷按钮。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(220, 156)
        self.setStyleSheet("""
            # 桌宠快捷卡片使用统一的莫兰迪暖色，保证背景、字体与主界面风格一致。
            QFrame {
                background: #ffffff;
                border: 1px solid #d4a373;
                border-radius: 22px;
                color: #5c5148;
            }
            QPushButton {
                border: 0;
                border-radius: 14px;
                padding: 8px 10px;
                text-align: left;
                color: #5c5148;
                background: transparent;
                font-size: 12px;
            }
            QPushButton:hover {
                background: #faf7f2;
                color: #9b7653;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        self.open_button = QPushButton("打开主面板")
        self.live_button = QPushButton("舆情预警实况（待更新）")
        self.quit_button = QPushButton("关闭程序")
        for button in (self.open_button, self.live_button, self.quit_button):
            layout.addWidget(button)
