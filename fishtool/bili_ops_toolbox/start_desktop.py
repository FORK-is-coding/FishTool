"""
B站运营工具箱 - 桌面应用启动脚本

桌面端统一启动入口，直接创建主窗口与桌宠悬浮窗。
Web 服务由 MainWindow 自动管理，桌面端不再显示首次启动导航向导。
"""
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from core.logger import get_logger
from desktop.icon_utils import load_app_icon, set_windows_app_user_model_id
from desktop.main_window import MainWindow
from desktop.pet_window import PetWindow

logger = get_logger(__name__)


def main() -> None:
    """创建桌面应用、主窗口和桌宠，并进入 Qt 事件循环。"""
    # 在创建 QApplication 前启用 Qt 高 DPI，保证 Windows 缩放下 CSS 像素与浏览器一致。
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    # QApplication 必须先于所有 Qt 窗口创建，负责初始化桌面事件循环。
    set_windows_app_user_model_id()
    app = QApplication(sys.argv)
    app.setWindowIcon(load_app_icon())
    # 设置应用名称，供系统任务栏、配置目录和窗口管理器识别。
    app.setApplicationName("B站运营工具箱")
    # 设置组织名称，避免 Qt 将本应用数据写入其他应用的配置空间。
    app.setOrganizationName("BiliOpsToolbox")

    try:
        # 启动日志标记桌面模式已开始，便于和 Web 模式启动记录区分。
        logger.info("===== 桌面应用启动 =====")
        # 主窗口负责承载主要业务页面，并自动管理内部 Web 服务。
        logger.info("[启动] 创建主窗口")
        main_window = MainWindow(web_port=8000)
        # 只有显示主窗口后用户才能进入运营工具主界面。
        main_window.show()

        # 桌宠使用独立窗口显示快捷状态，同时连接同一个 WebSocket 端口。
        logger.info("[启动] 创建桌宠窗口")
        pet_window = PetWindow(ws_port=8000)
        # 单独显示桌宠，避免它被主窗口的布局管理器隐藏。
        pet_window.show()

        # 所有窗口创建成功后再记录完成，便于定位启动阶段失败位置。
        logger.info("===== 桌面应用启动完成 =====")
        # 将控制权交给 Qt，直到用户退出应用才返回。
        sys.exit(app.exec_())
    except Exception as exc:
        # 启动异常写入完整堆栈，避免桌面程序静默退出导致无法排障。
        logger.error(f"[启动] 应用启动失败: {exc}", exc_info=True)
        # 向上抛出异常，让外层启动器能够感知失败并返回非零状态。
        raise


if __name__ == "__main__":
    # 仅在直接运行脚本时启动桌面程序，被导入时不产生副作用。
    main()

