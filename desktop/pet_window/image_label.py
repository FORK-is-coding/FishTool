"""桌宠图片控件。

拆分自原 pet_window.py 的 PetImageLabel：
支持平滑缩放绘制的桌宠图片控件，独立于状态和通信逻辑。
"""
from PyQt5.QtWidgets import QLabel
from PyQt5.QtCore import QSize, Qt, pyqtProperty
from PyQt5.QtGui import QPainter, QPixmap

class PetImageLabel(QLabel):
    """支持平滑缩放绘制的桌宠图片控件，独立于状态和通信逻辑。"""

    def __init__(self, parent=None):
        """初始化图片控件。

        Args:
            parent: Qt 父控件。
        """
        super().__init__(parent)
        self._scale_factor = 1.0
        self._pixmap = QPixmap()
        self.setAlignment(Qt.AlignCenter)

    def set_pet_pixmap(self, pixmap: QPixmap):
        """设置桌宠图片并触发重绘。

        Args:
            pixmap: 待显示的桌宠图片。
        """
        self._pixmap = pixmap
        self.update()

    def get_scale_factor(self) -> float:
        """返回图片当前缩放比例。"""
        return self._scale_factor

    def set_scale_factor(self, value: float):
        """设置图片缩放比例并触发重绘。

        Args:
            value: 图片相对基础尺寸的缩放比例。
        """
        self._scale_factor = max(1.0, min(float(value), 1.08))
        self.update()

    scale_factor = pyqtProperty(float, get_scale_factor, set_scale_factor)

    def paintEvent(self, event):
        """按宽高比绘制图片，避免素材变形。

        Args:
            event: Qt 绘制事件。
        """
        del event
        if self._pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        scaled = self._pixmap.scaled(
            QSize(
                int(self.width() * self._scale_factor),
                int(self.height() * self._scale_factor),
            ),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
        painter.end()
