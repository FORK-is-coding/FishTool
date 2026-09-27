"""FishTool 桌面图标资源解析与 Windows 任务栏标识。"""
from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

from PyQt5.QtGui import QIcon

logger = logging.getLogger(__name__)
APP_USER_MODEL_ID = "BiliOpsToolbox.FishTool"


def _icon_candidates() -> tuple[Path, ...]:
    """返回源码、PyInstaller 临时目录和 exe 同级目录中的图标候选路径。

    Returns:
        tuple[Path, ...]: 按优先级排列且已去重的图标路径。
    """
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / "图标.png")
    executable_dir = Path(sys.executable).resolve().parent
    candidates.extend(
        (
            executable_dir / "assets" / "图标.png",
            Path(__file__).resolve().parents[1] / "assets" / "图标.png",
            Path.cwd() / "assets" / "图标.png",
            Path(r"C:\Users\27418\Desktop\图标.png"),
        )
    )
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def icon_path() -> Path | None:
    """查找可用的 FishTool 图标文件。

    Returns:
        Path | None: 存在且非空的图标路径，找不到时返回 None。
    """
    try:
        for candidate in _icon_candidates():
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    except OSError:
        logger.exception("[图标] 检查图标文件失败")
    logger.error("[图标] 未找到图标.png，候选路径: %s", _icon_candidates())
    return None


def load_app_icon() -> QIcon:
    """加载统一应用图标，并记录资源加载结果。

    Returns:
        QIcon: 可直接用于 QApplication、窗口和托盘的图标对象。
    """
    path = icon_path()
    if path is None:
        return QIcon()
    icon = QIcon(str(path))
    if icon.isNull():
        logger.error("[图标] Qt 无法读取图标文件: %s", path)
    else:
        logger.info("[图标] 已加载: %s", path)
    return icon


def set_windows_app_user_model_id() -> None:
    """设置 Windows AppUserModelID，避免任务栏将窗口归入默认 Python 图标。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        logger.exception("[图标] 设置 Windows AppUserModelID 失败")
