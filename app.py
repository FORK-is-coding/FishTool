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
import os
import shutil
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


#: 冻结环境下要放到 exe 旁边 config/ 的随包配置文件。
#: config.yaml 不在此列：ConfigManager 首次启动时会自己生成。
BUNDLED_CONFIG_FILES = ("budget.yaml",)


def seed_bundled_config(
    bundle_dir: Path | None = None,
    runtime_dir: Path | None = None,
) -> list[Path]:
    """冻结环境首次启动时，把随包配置复制到 exe 旁边的 ``config/``。

    ``core/request_budget.py``、``core/quota_store.py`` 和
    ``modules/hotspot/risk_control.py`` 在冻结环境只读 exe 同级的
    ``config/budget.yaml``，而 build.spec 只把它打进包内（``_MEIPASS``）。
    不复制的话，exe 永远读不到配额账本。

    已存在的文件不覆盖，用户改过的账本保持原样。源码环境不做任何事。

    Args:
        bundle_dir: 随包资源根目录；默认取 ``sys._MEIPASS``。
        runtime_dir: 运行期持久化根目录；默认取 :func:`_runtime_root`。

    Returns:
        list[Path]: 本次新复制出的文件路径。
    """
    if bundle_dir is None:
        meipass = getattr(sys, "_MEIPASS", None)
        if not getattr(sys, "frozen", False) or not meipass:
            return []
        bundle_dir = Path(meipass)
    if runtime_dir is None:
        runtime_dir = _runtime_root()

    copied: list[Path] = []
    for name in BUNDLED_CONFIG_FILES:
        src = bundle_dir / "config" / name
        dst = runtime_dir / "config" / name
        if dst.exists():
            continue
        if not src.is_file():
            _entry_logger().warning("[启动] 包内缺少配置文件，无法复制: %s", src)
            continue
        tmp = dst.with_name(dst.name + ".tmp")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再改名：中途失败不会留下半个账本，下次启动还会重试。
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        except OSError as exc:
            _entry_logger().error("[启动] 复制配置文件失败: %s -> %s: %s", src, dst, exc)
            tmp.unlink(missing_ok=True)
            continue
        _entry_logger().info("[启动] 已复制默认配置文件: %s", dst)
        copied.append(dst)
    return copied


def _entry_logger():
    """延迟导入项目 logger，避免入口模块导入时就初始化日志目录。"""
    from core.logger import get_logger

    return get_logger(__name__)


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
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from core.logger import get_logger
    from desktop.icon_utils import load_app_icon, set_windows_app_user_model_id
    from desktop.main_window import MainWindow
    from desktop.pet_window import PetWindow

    logger = get_logger(__name__)

    # Qt6 默认开启高 DPI 缩放与高 DPI 图标，不再需要 AA_EnableHighDpiScaling / AA_UseHighDpiPixmaps。
    # 保留小数缩放倍率（150% 屏按 1.5 倍渲染），与浏览器的 devicePixelRatio 一致。
    # Qt6 默认已是 PassThrough，这里显式声明，避免将来默认值或环境变量变化时退回取整（Qt5 时 150% 被取整成 2）。
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    # WebEngine 合成改用 OpenGL：默认的 Direct3D 11 在双显卡机器上悬停时整页闪烁。必须在 QApplication 之前设置。
    from desktop.webengine_profile import select_graphics_api
    graphics_api = select_graphics_api()
    set_windows_app_user_model_id()

    # 只把程序名传给 Qt，避免 argparse 的 desktop/--port 参数被 Qt 当成未知选项。
    app = QApplication([sys.argv[0]])
    app.setWindowIcon(load_app_icon())
    # 悬停提示与菜单只能全局设置样式；与面板一致的深灰提示、白底菜单。
    from desktop.ui_theme import app_style
    app.setStyleSheet(app_style())
    app.setApplicationName("B站运营工具箱")
    app.setOrganizationName("BiliOpsToolbox")

    try:
        logger.info("===== 桌面应用启动 =====")
        logger.info("[启动] WebEngine 图形接口: %s", graphics_api)
        logger.info("[启动] 创建主窗口（内嵌 Web 服务端口 %s）", port)
        main_window = MainWindow(web_port=port)
        main_window.show()

        # 桌宠连接同一个端口的 WebSocket。
        logger.info("[启动] 创建桌宠窗口")
        pet_window = PetWindow(ws_port=port)
        pet_window.show()

        logger.info("===== 桌面应用启动完成 =====")
        return app.exec()
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
    # 默认只监听回环地址；局域网访问需显式 --host 0.0.0.0 并配置 FISHTOOL_ALLOWED_HOSTS。
    web_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址（默认 127.0.0.1，仅本机；局域网访问传 0.0.0.0 并配置 FISHTOOL_ALLOWED_HOSTS）",
    )
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

    # 冻结环境先补齐 exe 旁边的随包配置，后面所有子命令读到的都是同一份。
    seed_bundled_config()

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
