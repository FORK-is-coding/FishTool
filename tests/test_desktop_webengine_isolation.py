"""WebEngine 红线的 subprocess 隔离用例（批5 · A 组）。

红线事实（实测两次稳定复现）：
    QT_QPA_PLATFORM=offscreen 下
      - desktop/main_window.py:65  DesktopWebPage(QWebEnginePage)
      - desktop/main_window.py:80  MainWindow(QMainWindow)
    一构造即 STATUS_ACCESS_VIOLATION（0xC0000005，进程退出码 3221225477）。
    这是访问违规而非 Python 异常，try/except BaseException 拦不住，裸收进全量
    会让整个 pytest 进程 crash、把 1394 全绿打没。

收编策略（按「碰不碰 WebEngine」切刀，不按「是不是 desktop」切）：
    - 导入面（import QtWebEngineWidgets / import desktop）在 offscreen 下安全，
      可放进主套件；
    - 构造面必须放子进程，父进程只读 exit code，崩溃不影响主套件。

硬约束 #5：子进程不继承 pytest 的落盘隔离，因此一律以 cwd=tmp_path 启动，
    避免裸 import desktop 按 cwd 重建仓库 data/logs。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

#: 子进程探针（文件名不匹配 pytest 的 python_files，不会被当作用例收集）
CHILD = Path(__file__).resolve().parent / "_desktop_webengine_child.py"

#: Windows 访问违规：STATUS_ACCESS_VIOLATION
STATUS_ACCESS_VIOLATION = 0xC0000005  # == 3221225477


def _run_child(mode: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """在隔离的临时工作目录里运行 WebEngine 探针，只返回进程级结果。

    Args:
        mode: 探针模式（import-surface / construct-page / construct-mainwindow）。
        tmp_path: pytest 临时目录，作为子进程 cwd，承接其可能的 data/logs 落盘。

    Returns:
        subprocess.CompletedProcess: returncode/stdout/stderr 可直接断言。
    """
    return subprocess.run(
        [sys.executable, str(CHILD), mode],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def test_webengine_import_surface_is_process_safe(tmp_path):
    """只 import 桌面端导入面（含 QtWebEngineWidgets）不应崩溃，只有构造面危险。"""
    proc = _run_child("import-surface", tmp_path)

    assert proc.returncode == 0, (
        f"import 面异常退出 rc={proc.returncode}\nSTDOUT={proc.stdout}\nSTDERR={proc.stderr}"
    )
    assert "IMPORT-SURFACE OK" in proc.stdout


def test_webengine_construction_crash_is_contained_in_subprocess(tmp_path):
    """构造 DesktopWebPage / MainWindow 必须崩在子进程，父进程存活并读到退出码。"""
    outcomes: dict = {}

    for mode in ("construct-page", "construct-mainwindow"):
        proc = _run_child(mode, tmp_path)
        outcomes[mode] = proc.returncode
        assert proc.returncode != 0, (
            f"{mode} 竟然构造成功（rc=0）；说明 offscreen 红线上移，"
            f"需要重新评估是否可裸收编\nSTDOUT={proc.stdout}"
        )

    if sys.platform == "win32":
        for mode, returncode in outcomes.items():
            assert returncode == STATUS_ACCESS_VIOLATION, (
                f"{mode} 退出码 {returncode} != 0x{STATUS_ACCESS_VIOLATION:X}（访问违规）"
            )

    # 能执行到这里即证明：子进程的访问违规没有带走父进程（pytest 主进程）。
    # 这正是「按 WebEngine 切刀 + subprocess 隔离」要达到的效果。
