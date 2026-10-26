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
        self.setObjectName("petMenuPanel")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(236, 176)
        self.setStyleSheet("""
            /* 桌宠快捷卡片：与主界面一致的暖白底、细描边和大圆角。 */
            QFrame#petMenuPanel {
                background: #fffdf9;
                border: 1px solid #dfd0c0;
                border-radius: 18px;
            }
            QPushButton {
                min-height: 30px;
                padding: 7px 12px;
                text-align: left;
                color: #5c5148;
                background: #faf7f2;
                border: 1px solid #eadfd3;
                border-radius: 12px;
                font-family: "Microsoft YaHei", "SimHei", sans-serif;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton:hover {
                color: #9b7653;
                background: #f1e8dc;
                border-color: #d4a373;
            }
            QPushButton:pressed {
                background: #e8d9c8;
            }
            QPushButton:focus {
                border-color: #d4a373;
            }
            QPushButton#petMenuPrimary {
                color: #ffffff;
                background: #d4a373;
                border-color: #d4a373;
            }
            QPushButton#petMenuPrimary:hover {
                background: #c48f61;
                border-color: #c48f61;
            }
            QPushButton#petMenuPrimary:pressed {
                background: #b98255;
                border-color: #b98255;
            }
            QPushButton#petMenuDanger {
                color: #b46f69;
                background: #fff7f6;
                border-color: #e7c4c0;
            }
            QPushButton#petMenuDanger:hover {
                color: #8f4a45;
                background: #f6e3e1;
                border-color: #b46f69;
            }
            QPushButton#petMenuDanger:pressed {
                background: #efcfcb;
                border-color: #b46f69;
            }
        """)
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
