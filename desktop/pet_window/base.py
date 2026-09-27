"""桌宠窗口基础：初始化、UI 构建、占位图加载与状态管理。

拆分自原 pet_window.py 的 PetWindow 基础部分。
"""
from PyQt5.QtWidgets import QApplication, QLabel, QVBoxLayout
from PyQt5.QtCore import Qt, QPoint, QSettings, QTimer
from PyQt5.QtGui import QColor, QPainter, QPixmap

from desktop.icon_utils import load_app_icon
from .assets import PET_ASSET_DIRS, logger
from .image_label import PetImageLabel
from .ws_thread import WebSocketThread


class PetWindowBase:
    """桌宠窗口基础部分（原 PetWindow 的初始化、UI 构建与状态管理逻辑）。"""

    # 状态枚举
    STATE_IDLE = 0  # 待机
    STATE_DRAGGING = 1  # 拖拽
    STATE_CLICKED = 2  # 点击
    STATE_NOTIFY = 3  # 通知

    def __init__(self, ws_port: int = 8000):
        """初始化桌宠窗口
        # 设置初始值/默认状态，避免后续空引用
        
        Args:
            ws_port: WebSocket服务端口
        """
        # 调用父类QWidget的构造函数
        super().__init__()
        
        # 桌宠是独立顶层窗口，同样显式设置图标以覆盖 Alt+Tab 和窗口标题栏。
        self.setWindowIcon(load_app_icon())
        
        # 构造WebSocket连接URL地址
        self.ws_url = f"ws://localhost:{ws_port}/ws"
        # 初始化当前状态为待机状态
        self.current_state = self.STATE_IDLE
        # 初始化拖拽位置记录点。
        self.drag_position = QPoint()
        # 本地配置只保存桌宠位置，不改变主程序或通信配置。
        self.settings = QSettings("BiliOpsToolbox", "DesktopPet")
        # 菜单动画和收起定时器独立管理，避免与通知定时器互相覆盖。
        self._menu_animation = None
        self._menu_hide_timer = QTimer(self)
        self._menu_hide_timer.setSingleShot(True)
        self._menu_hide_timer.timeout.connect(self.hide_menu)
        self._hover_animation = None
        self._drag_start_position = QPoint()
        # 监听应用级鼠标按下事件，用于点击其他区域收起菜单。
        QApplication.instance().installEventFilter(self)

        # 占位符图片（实际应替换为美术素材）
        self.images = {
            self.STATE_IDLE: None,  # 待机图
            self.STATE_DRAGGING: None,  # 拖拽图
            self.STATE_CLICKED: None,  # 点击图
            self.STATE_NOTIFY: None  # 通知图
        }
        
        self.init_ui()
        self.setup_websocket()

    def init_ui(self):
        """初始化UI界面
        # 设置初始值/默认状态，避免后续空引用
        
        创建无边框、置顶、透明背景的悬浮窗，显示桌宠图片和通知气泡。
        # 实例化对象并准备使用
        默认定位到屏幕右下角。
        """
        # 无边框、始终置顶、透明背景
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        # 设置属性
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # 窗口大小
        self.resize(150, 150)
        
        # 布局
        layout = QVBoxLayout(self)
        # 设置布局边距
        layout.setContentsMargins(0, 0, 0, 0)
        
        # 图片标签
        # 显示桌宠形象图
        # 将内容呈现到界面上
        # 图片控件按比例绘制素材，并允许父窗口接收鼠标交互。
        self.image_label = PetImageLabel(self)
        self.image_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.image_label)
        
        # 通知气泡标签
        # 粉白配色气泡，显示预警消息
        # 将内容呈现到界面上
        self.notify_label = QLabel()
        self.notify_label.setStyleSheet("""
            QLabel {
                background-color: rgba(255, 255, 255, 0.95);
                border: 2px solid #fb7299;
                border-radius: 10px;
                padding: 10px;
                color: #333;
                font-size: 12px;
            }
        """)
        self.notify_label.setWordWrap(True)
        # 隐藏窗口/内容
        self.notify_label.hide()
        # 添加控件到布局
        # 将元素加入容器/布局
        layout.addWidget(self.notify_label)
        
        # 加载占位符图片
        # 从存储/网络读入数据
        self.load_placeholder_images()
        
        # 初始状态
        self.set_state(self.STATE_IDLE)
        
        # 定位到屏幕右下角
        # 留出 50px 边距
        screen = QApplication.desktop().screenGeometry()
        # 移动窗口位置
        saved_position = self.settings.value("position")
        if isinstance(saved_position, QPoint):
            self.move(saved_position)
        else:
            self.move(screen.width() - self.width() - 50, screen.height() - self.height() - 100)
        
        logger.info("[桌宠] UI初始化完成")

    def load_placeholder_images(self):
        """加载占位符图片（纯色方块）
        # 从存储/网络读入数据
        
        生成不同颜色的圆形占位符，分别代表不同状态：
        - 蓝色：待机
        - 橙色：拖拽中
        - 浅蓝：点击
        - 粉色：通知状态
        """
        # 从项目资源目录读取素材；加载失败时保留同尺寸占位图。
        asset_paths = {
            self.STATE_IDLE: next((root / "待机.png" for root in PET_ASSET_DIRS if (root / "待机.png").is_file()), PET_ASSET_DIRS[-1] / "待机.png"),
            self.STATE_DRAGGING: next((root / "拖拽.png" for root in PET_ASSET_DIRS if (root / "拖拽.png").is_file()), PET_ASSET_DIRS[-1] / "拖拽.png"),
        }
        colors = {
            self.STATE_IDLE: QColor(100, 181, 246),
            self.STATE_DRAGGING: QColor(255, 183, 77),
            self.STATE_CLICKED: QColor(129, 212, 250),
            self.STATE_NOTIFY: QColor(251, 114, 153),
        }

        
        # 创建占位符（不同颜色的圆形）
        colors = {
            self.STATE_IDLE: QColor(100, 181, 246),  # 蓝色 - 待机
            self.STATE_DRAGGING: QColor(255, 183, 77),  # 橙色 - 拖拽
            self.STATE_CLICKED: QColor(129, 212, 250),  # 浅蓝 - 点击
            self.STATE_NOTIFY: QColor(251, 114, 153)  # 粉色 - 通知
        }
        
        # 为每个状态绘制圆形占位图
        for state, color in colors.items():
            # 创建透明画布
            pixmap = QPixmap(150, 150)
            pixmap.fill(Qt.transparent)
            
            # 绘制圆形
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            # 设置画刷
            painter.setBrush(color)
            # 设置画笔
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(10, 10, 130, 130)
            painter.end()
            
            # 保存到状态图片字典
            self.images[state] = pixmap

        # 读取用户桌面素材，保持占位图作为缺失文件时的兜底。
        for state, path in asset_paths.items():
            try:
                if path.is_file():
                    loaded = QPixmap(str(path))
                    if not loaded.isNull():
                        self.images[state] = loaded
                        logger.info(f"[桌宠] 已加载素材: {path}")
                    else:
                        logger.warning(f"[桌宠] 素材无法解析，使用占位图: {path}")
                else:
                    logger.warning(f"[桌宠] 素材不存在，使用占位图: {path}")
            except Exception as exc:
                logger.error(f"[桌宠] 加载素材失败 {path}: {exc}")

    def set_state(self, state: int):
        """切换桌宠状态
        
        更新当前状态并切换对应图片。
        # 用新值覆盖旧值，保持数据一致
        
        Args:
            state: 状态枚举值
        """
        if state not in self.images:
            return
        
        self.current_state = state
        # 图片控件负责保持宽高比，状态切换只提供图片数据。
        if self.images[state]:
            self.image_label.set_pet_pixmap(self.images[state])
        
        logger.debug(f"[桌宠] 状态切换: {state}")
