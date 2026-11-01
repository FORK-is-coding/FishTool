"""WebEngine 红线的 subprocess 隔离用例（批5 · A 组）。

历史：PyQt5（Chromium 83）时代，QT_QPA_PLATFORM=offscreen 下构造 DesktopWebPage / MainWindow
    一构造即 STATUS_ACCESS_VIOLATION（0xC0000005），try/except 拦不住，裸收进全量会带走整个 pytest 进程。
    迁到 PySide6 6.12 后实测 offscreen 下可以正常构造（exit=0）。

收编策略（按「碰不碰 WebEngine」切刀，不按「是不是 desktop」切）：
    - 导入面（import QtWebEngineWidgets / import desktop）在 offscreen 下安全，
      可放进主套件；
    - 构造面仍放子进程，父进程只读 exit code 与输出：WebEngine 不在主 pytest 进程里启动，
      避免拖慢全量，也避免没有 GPU 的机器上出问题。

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


def test_webengine_construction_runs_in_subprocess(tmp_path):
    """构造 DesktopWebPage / MainWindow 在子进程里完成，正常退出，且没有写到 cwd 之外。

    PyQt5（Chromium 83）时代 offscreen 下构造必然 0xC0000005；PySide6 6.12 实测可以正常构造。
    仍放在子进程：WebEngine 在主 pytest 进程里启动会拖慢全量，也可能在没有 GPU 的机器上出问题。
    """
    for mode, marker in (("construct-page", "CONSTRUCT-PAGE OK"), ("construct-mainwindow", "CONSTRUCT-MAINWINDOW OK")):
        proc = _run_child(mode, tmp_path)
        assert proc.returncode == 0, (
            f"{mode} 退出码 {proc.returncode}（0x{proc.returncode & 0xFFFFFFFF:X}）\n"
            f"STDOUT={proc.stdout}\nSTDERR={proc.stderr[-2000:]}"
        )
        assert marker in proc.stdout, proc.stdout
    # MainWindow 的持久化 profile 落在 cwd（临时目录）下，不写真实用户目录。
    assert (tmp_path / "webengine" / "Default").is_dir(), sorted(p.name for p in tmp_path.iterdir())
