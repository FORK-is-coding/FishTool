"""WebEngine 子进程探针（由 tests/test_desktop_webengine_isolation.py 启动）。

为什么必须是子进程：
    QT_QPA_PLATFORM=offscreen 下 `class DesktopWebPage(QWebEnginePage)`（main_window.py:65）
    与 `class MainWindow(QMainWindow)`（main_window.py:80）一构造就触发
    STATUS_ACCESS_VIOLATION（0xC0000005，exit=3221225477），Python 层
    try/except BaseException 完全拦不住，会把整个 pytest 进程带走。
    因此构造面只在这里跑，父进程只读 exit code。

不作为 pytest 用例：
    文件名不以 test_ 开头、也不以 _test 结尾，pytest 默认 python_files 不会收集。

硬约束 #5（落盘纪律）：
    子进程不继承 pytest 的 monkeypatch 隔离。裸 import desktop 会懒构造真
    LoggerManager 并按 cwd 相对路径重建 data/logs，所以父进程必须以
    cwd=<临时目录> 启动本脚本。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 无显示器环境：必须在任何 Qt 对象之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import_surface() -> int:
    """只 import 桌面端导入面，不构造任何 WebEngine 对象。

    Returns:
        int: 成功返回 0。
    """
    import desktop  # noqa: F401  -> desktop/__init__ -> main_window -> QtWebEngineWidgets
    from desktop import icon_utils  # noqa: F401
    from desktop.main_window import DesktopWebPage, MainWindow  # noqa: F401
    from desktop.pet_window import ws_mixin, ws_thread  # noqa: F401

    print("IMPORT-SURFACE OK", flush=True)
    return 0


def _construct_page() -> int:
    """构造 DesktopWebPage（QWebEnginePage 子类）。

    Returns:
        int: 若能构造成功返回 0（正常环境下会被访问违规打断，走不到这里）。
    """
    # QtWebEngineWidgets 必须先于 QApplication import
    from PyQt5.QtWebEngineWidgets import QWebEngineView
    from desktop.main_window import DesktopWebPage
    from PyQt5.QtWidgets import QApplication

    app = QApplication([])  # noqa: F841
    view = QWebEngineView()
    page = DesktopWebPage(view)
    print(f"CONSTRUCT-PAGE OK {page!r}", flush=True)
    return 0


def _construct_mainwindow() -> int:
    """构造 MainWindow。

    刻意屏蔽 start_web_service：本探针只验证 WebEngine 构造面，
    不应真的 Popen 起 Web 服务或轮询 /health。

    Returns:
        int: 若能构造成功返回 0（正常环境下会被访问违规打断，走不到这里）。
    """
    from desktop.main_window import MainWindow
    from PyQt5.QtWidgets import QApplication

    # 只测 WebEngine 构造面，屏蔽真实启动 Web 服务的副作用
    MainWindow.start_web_service = lambda self: None

    app = QApplication([])  # noqa: F841
    window = MainWindow()
    print(f"CONSTRUCT-MAINWINDOW OK {window!r}", flush=True)
    return 0


MODES = {
    "import-surface": _import_surface,
    "construct-page": _construct_page,
    "construct-mainwindow": _construct_mainwindow,
}


def main() -> int:
    """按命令行 mode 分派。

    Returns:
        int: 未知/缺失 mode 返回 2，否则返回对应探针的退出码。
    """
    if len(sys.argv) < 2 or sys.argv[1] not in MODES:
        print("usage: _desktop_webengine_child.py <%s>" % "|".join(MODES), flush=True)
        return 2
    return MODES[sys.argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main())
