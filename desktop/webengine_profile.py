"""桌面端 WebEngine 持久化 profile 与 Qt5 旧数据迁移。

背景（PySide6 6.12 实测）：
- ``QWebEngineProfile.defaultProfile()`` 和 ``QWebEngineView()`` 自带的页面都是无痕 profile，
  localStorage 只在内存里，重启就丢（例如开场动画的「已播放」标记 ``fishtool.intro.v1``）；
- 必须用有名字的 profile 才会落盘。数据目录从 Qt5 的
  ``%LOCALAPPDATA%\\<组织>\\<应用>\\QtWebEngine\\Default`` 变成
  ``%APPDATA%\\<组织>\\<应用>\\QtWebEngine\\<名字>``（Roaming，不是 Local）。

迁移规则：
- 只复制旧目录下的 ``Local Storage``，不复制 GPUCache、Cache 等缓存；
- 只复制、不移动：Qt6 打开数据后会改写文件，旧路径要原样保留，回退到 Qt5 版时仍能用；
- 必须在这个 profile 构造第一个页面之前完成：任何页面加载后 Chromium 都会建一个空的
  ``Local Storage``，之后再复制就会覆盖真实数据或被跳过；
- 用标记文件判断是否已迁移，不看目录是否存在；先复制到临时目录再改名，不留下复制了一半的数据；
- 失败只记日志、不阻止启动，下次启动再试（标记只在成功后写入）。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from core.logger import get_logger

logger = get_logger(__name__)

#: 持久化 profile 名，决定新数据目录的最后一级。与 Qt5 旧目录同名；定下后不要再改，否则用户数据会「丢」。
PROFILE_NAME = "Default"

#: 迁移成功后写在新 profile 目录下的标记文件。
MIGRATION_MARKER = ".fishtool_migrated_from_qt5"

#: 只迁移这一个子目录。
LOCAL_STORAGE_DIR = "Local Storage"

#: 测试用环境变量：设了就把 profile 数据和缓存放到该目录下，并跳过 Qt5 旧数据迁移，不碰真实用户目录。
STORAGE_ROOT_ENV = "FISHTOOL_WEBENGINE_STORAGE"


def legacy_profile_dir(organization: str, application: str, local_appdata: Optional[str] = None) -> Path:
    """迁移前的 Qt5 版本（PyQt5 + PyQtWebEngine 5.15）默认 profile 的磁盘目录。

    Args:
        organization: ``QApplication.organizationName()``。
        application: ``QApplication.applicationName()``。
        local_appdata: ``%LOCALAPPDATA%``；省略时读环境变量。

    Returns:
        Path: ``<LOCALAPPDATA>/<组织>/<应用>/QtWebEngine/Default``。
    """
    base = local_appdata if local_appdata is not None else os.environ.get("LOCALAPPDATA", "")
    return Path(base) / organization / application / "QtWebEngine" / "Default"


def migrate_legacy_local_storage(old_profile_dir: Path, new_profile_dir: Path) -> bool:
    """把 Qt5 旧 profile 的 ``Local Storage`` 复制到 Qt6 新 profile 目录（只做一次）。

    Args:
        old_profile_dir: Qt5 旧 profile 目录（见 :func:`legacy_profile_dir`）。
        new_profile_dir: Qt6 有名 profile 的 ``persistentStoragePath()``。

    Returns:
        bool: 本次实际复制了数据返回 True；已迁移过、没有旧数据或失败时返回 False。
    """
    new_profile_dir = Path(new_profile_dir)
    old_storage = Path(old_profile_dir) / LOCAL_STORAGE_DIR
    marker = new_profile_dir / MIGRATION_MARKER
    target = new_profile_dir / LOCAL_STORAGE_DIR
    tmp = new_profile_dir / (LOCAL_STORAGE_DIR + ".migrating")

    if marker.exists():
        return False
    if not old_storage.is_dir():
        logger.info("[WebEngine] 没有 Qt5 旧 localStorage，跳过迁移: %s", old_storage)
        return False
    if target.exists():
        # 新目录已有数据（例如用户先用过 Qt6 版本），不覆盖；写标记，以后不再检查。
        logger.warning("[WebEngine] 新 profile 已有 Local Storage，不覆盖，跳过迁移: %s", target)
        _write_marker(marker, "skipped: target exists")
        return False

    try:
        new_profile_dir.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            shutil.rmtree(tmp)
        # 复制，不移动：旧目录原样保留给回退后的 Qt5 版本。
        shutil.copytree(old_storage, tmp)
        os.replace(tmp, target)
    except OSError as exc:
        logger.error("[WebEngine] 迁移 Qt5 localStorage 失败，下次启动重试: %s -> %s: %s", old_storage, target, exc)
        shutil.rmtree(tmp, ignore_errors=True)
        return False

    _write_marker(marker, f"copied from {old_storage}")
    logger.info("[WebEngine] 已迁移 Qt5 localStorage: %s -> %s", old_storage, target)
    return True


def _write_marker(marker: Path, text: str) -> None:
    """写迁移标记；写失败只记日志（下次启动会再判断一次）。"""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        logger.error("[WebEngine] 写迁移标记失败: %s: %s", marker, exc)


def create_persistent_profile(parent=None, storage_root: Optional[Path] = None):
    """创建落盘的有名 profile，并在它构造任何页面之前完成旧数据迁移。

    profile 的父对象默认是 ``QApplication``：主窗口的 closeEvent 只会隐藏到托盘，
    退出时不会走到；挂在应用上能保证页面先于 profile 销毁，退出时不会出现
    「Release of profile requested but WebEnginePage still not deleted」。

    Args:
        parent: profile 的父对象；省略时用 ``QApplication.instance()``。
        storage_root: 测试用：把 profile 数据与缓存都放到这个目录下，不碰真实用户目录。
            生产代码不传，使用 Qt 默认路径。

    Returns:
        QWebEngineProfile: 非无痕的有名 profile。
    """
    from PySide6.QtWebEngineCore import QWebEngineProfile
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    profile = QWebEngineProfile(PROFILE_NAME, parent if parent is not None else app)
    if storage_root is None and os.environ.get(STORAGE_ROOT_ENV):
        storage_root = Path(os.environ[STORAGE_ROOT_ENV])
    if storage_root is not None:
        root = Path(storage_root)
        profile.setPersistentStoragePath(str(root / PROFILE_NAME))
        profile.setCachePath(str(root / "cache" / PROFILE_NAME))
        return profile
    if app is not None:
        old_dir = legacy_profile_dir(app.organizationName(), app.applicationName())
        migrate_legacy_local_storage(old_dir, Path(profile.persistentStoragePath()))
    return profile


def select_graphics_api() -> str:
    """在创建 QApplication 之前选定 Qt Quick 的图形接口，默认 OpenGL。

    Qt6 在 Windows 上默认用 Direct3D 11 合成 QtWebEngine 的画面。实测在双显卡机器上
    （GTX 1650 + UHD 630），鼠标移到卡片、按钮上时整页闪烁；Edge 打开同一页面不闪，
    设置 ``QSG_RHI_BACKEND=opengl`` 后不再闪。Qt5 的 QtWebEngine 一直走 OpenGL，所以以前没有这个问题。

    用户或排障时显式设置了 ``QSG_RHI_BACKEND`` 就尊重它，不覆盖。

    Returns:
        str: 实际生效的选择（``"opengl"`` 或环境变量里的值）。
    """
    chosen = os.environ.get("QSG_RHI_BACKEND", "").strip()
    if chosen:
        logger.info("[WebEngine] 使用环境变量指定的图形接口: QSG_RHI_BACKEND=%s", chosen)
        return chosen
    from PySide6.QtQuick import QQuickWindow, QSGRendererInterface

    QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
    return "opengl"
