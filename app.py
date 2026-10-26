"""FishTool 统一启动入口。

只保留这一个启动脚本：

    python app.py                 # 桌面版（默认）：主窗口 + 桌宠，同时启动 8000 端口
    python app.py desktop         # 显式启动桌面版
    python app.py web             # 仅启动 Web 服务，浏览器访问 http://localhost:8000
    python app.py web --port 9000 # 指定 Web 端口（桌面内部也支持 --port）
    python app.py init            # 只初始化配置/数据库/目录，不启动界面

桌面版会自动在后台启动同一个 ``app.py web`` 进程，因此运行桌面版后，
浏览器访问 ``http://localhost:8000`` 也能使用同一套 Web 界面/API。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: 项目根目录；以绝对路径插入 sys.path，保证从任意工作目录启动都能 import。
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _runtime_root() -> Path:
    """返回运行期持久化根目录。

    Returns:
        Path: 源码环境为仓库根；PyInstaller 冻结环境为 exe 所在目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


def init_project() -> bool:
    """初始化配置、数据库和运行目录。

    Returns:
        bool: 初始化成功返回 True；配置或数据库失败返回 False。
    """
    from core.config import ConfigManager
    from core.database import init_database
    from core.logger import get_logger

    logger = get_logger(__name__)
    logger.info("初始化B站运营工具箱...")

    try:
        ConfigManager()
        logger.info("配置管理器初始化成功")
    except Exception as exc:  # noqa: BLE001 - 初始化阶段需要把原因写日志并退出
        logger.error("配置初始化失败: %s", exc, exc_info=True)
        return False

    try:
        init_database()
        logger.info("数据库初始化成功")
    except Exception as exc:  # noqa: BLE001 - 初始化阶段需要把原因写日志并退出
        logger.error("数据库初始化失败: %s", exc, exc_info=True)
        return False

    for dir_name in ("logs", "data", "backups"):
        (_runtime_root() / dir_name).mkdir(parents=True, exist_ok=True)

    logger.info("项目初始化完成")
    return True


def run_web(host: str, port: int, reload: bool) -> int:
    """启动 FastAPI Web 服务。

    Args:
        host: 监听地址。
        port: 监听端口。
        reload: 是否启用 uvicorn 热重载（开发模式）。

    Returns:
        int: 进程退出码。
    """
    import uvicorn

    from core.logger import get_logger

    logger = get_logger(__name__)
    logger.info("启动Web服务器: http://%s:%s", host, port)
    logger.info("开发模式: %s", "开启" if reload else "关闭")

    try:
        uvicorn.run(
            "web.main:app",
            host=host,
            port=port,
            reload=reload,
            log_level="info",
        )
    except KeyboardInterrupt:
        logger.info("服务器已停止")
        return 0
    except Exception as exc:  # noqa: BLE001 - 启动失败要给出非零退出码
        logger.error("服务器启动失败: %s", exc, exc_info=True)
        return 1
    return 0


def run_desktop(port: int) -> int:
    """启动桌面版主窗口与桌宠；Web 服务由 MainWindow 自动拉起。

    Args:
        port: 桌面内嵌 Web 服务端口，同时在浏览器可访问。

    Returns:
        int: Qt 事件循环退出码。
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    from core.logger import get_logger
    from desktop.icon_utils import load_app_icon, set_windows_app_user_model_id
    from desktop.main_window import MainWindow
    from desktop.pet_window import PetWindow

    logger = get_logger(__name__)

    # 在创建 QApplication 前启用 Qt 高 DPI，保证 Windows 缩放下 CSS 像素与浏览器一致。
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    set_windows_app_user_model_id()

    # 只把程序名传给 Qt，避免 argparse 的 desktop/--port 参数被 Qt 当成未知选项。
    app = QApplication([sys.argv[0]])
    app.setWindowIcon(load_app_icon())
    app.setApplicationName("B站运营工具箱")
    app.setOrganizationName("BiliOpsToolbox")

    try:
        logger.info("===== 桌面应用启动 =====")
        logger.info("[启动] 创建主窗口（内嵌 Web 服务端口 %s）", port)
        main_window = MainWindow(web_port=port)
        main_window.show()

        # 桌宠连接同一个端口的 WebSocket。
        logger.info("[启动] 创建桌宠窗口")
        pet_window = PetWindow(ws_port=port)
        pet_window.show()

        logger.info("===== 桌面应用启动完成 =====")
        return app.exec_()
    except Exception as exc:  # noqa: BLE001 - 桌面入口最后兜底，记录完整堆栈
        logger.error("[启动] 应用启动失败: %s", exc, exc_info=True)
        return 1


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。

    Returns:
        argparse.ArgumentParser: 统一入口参数解析器。
    """
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="FishTool 统一启动入口（默认桌面版，同时提供 localhost 上的 Web 服务）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python app.py                 # 桌面版 + http://localhost:8000\n"
            "  python app.py desktop         # 显式桌面版\n"
            "  python app.py web             # 仅 Web 版\n"
            "  python app.py web --port 9000 # Web 版换端口\n"
            "  python app.py init            # 仅初始化\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    desktop_parser = subparsers.add_parser("desktop", help="启动桌面版（默认）")
    desktop_parser.add_argument("--port", type=int, default=8000, help="内嵌 Web 端口（默认 8000）")

    web_parser = subparsers.add_parser("web", help="仅启动 Web 服务")
    web_parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    web_parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    web_parser.add_argument("--reload", action="store_true", help="开启开发热重载")
    web_parser.add_argument("--init-only", action="store_true", help="仅初始化，不启动服务")

    subparsers.add_parser("init", help="仅初始化配置/数据库/目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    """统一入口主函数。

    Args:
        argv: 命令行参数；None 表示使用 sys.argv[1:]。

    Returns:
        int: 进程退出码。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # 不带子命令时默认桌面版，保持「双击/直接运行 app.py 就能用」的体验。
    command = args.command or "desktop"

    if command == "init":
        return 0 if init_project() else 1

    if command == "web":
        if not init_project():
            return 1
        if args.init_only:
            return 0
        return run_web(host=args.host, port=args.port, reload=args.reload)

    if command == "desktop":
        return run_desktop(port=getattr(args, "port", 8000))

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
