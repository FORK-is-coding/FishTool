"""桌面端 WebEngine 持久化 profile 与 Qt5 旧 localStorage 迁移（PySide6 迁移）。

背景：PySide6 6.12 的默认 profile 是无痕的，localStorage 重启就丢；桌面壳改用有名 profile，
数据目录从 %LOCALAPPDATA%\\...\\QtWebEngine\\Default 变成 %APPDATA%\\...\\QtWebEngine\\Default，
首次启动把 Qt5 时期的 Local Storage 复制过去（只复制、不移动，失败下次重试）。

这里不碰真实用户目录：迁移函数用 tmp_path，profile 用 FISHTOOL_WEBENGINE_STORAGE 指到 tmp_path。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from desktop import webengine_profile as wp

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_legacy(root: Path) -> Path:
    """造一个 Qt5 风格的旧 profile 目录（含 Local Storage/leveldb）。"""
    legacy = root / "legacy" / "Default"
    leveldb = legacy / "Local Storage" / "leveldb"
    leveldb.mkdir(parents=True)
    (leveldb / "000003.log").write_bytes(b"\x00fishtool.intro.v1\x001")
    (leveldb / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
    (legacy / "GPUCache").mkdir()
    (legacy / "GPUCache" / "data_0").write_bytes(b"cache")
    return legacy


def test_first_run_copies_local_storage_only(tmp_path):
    """首次迁移：只复制 Local Storage，内容一致，写标记，不留临时目录，旧目录原样保留。"""
    legacy = _make_legacy(tmp_path)
    new = tmp_path / "new" / "Default"
    before = {p.relative_to(legacy): p.read_bytes() for p in legacy.rglob("*") if p.is_file()}

    assert wp.migrate_legacy_local_storage(legacy, new) is True

    copied = new / "Local Storage" / "leveldb"
    assert (copied / "000003.log").read_bytes() == b"\x00fishtool.intro.v1\x001"
    assert (copied / "CURRENT").exists()
    assert not (new / "GPUCache").exists()
    assert (new / wp.MIGRATION_MARKER).exists()
    assert not (new / "Local Storage.migrating").exists()
    after = {p.relative_to(legacy): p.read_bytes() for p in legacy.rglob("*") if p.is_file()}
    assert after == before, "旧目录必须原样保留（只复制，不移动）"


def test_second_run_is_noop(tmp_path):
    """有标记后不再复制，即使旧数据后来变了。"""
    legacy = _make_legacy(tmp_path)
    new = tmp_path / "new" / "Default"
    assert wp.migrate_legacy_local_storage(legacy, new) is True
    (legacy / "Local Storage" / "leveldb" / "000003.log").write_bytes(b"changed")

    assert wp.migrate_legacy_local_storage(legacy, new) is False
    assert (new / "Local Storage" / "leveldb" / "000003.log").read_bytes() != b"changed"


def test_existing_target_is_not_overwritten(tmp_path):
    """新目录已有 Local Storage（例如先用过 Qt6 版）时不覆盖，写标记后不再检查。"""
    legacy = _make_legacy(tmp_path)
    new = tmp_path / "new" / "Default"
    (new / "Local Storage").mkdir(parents=True)
    (new / "Local Storage" / "mine").write_text("keep", encoding="utf-8")

    assert wp.migrate_legacy_local_storage(legacy, new) is False
    assert (new / "Local Storage" / "mine").read_text(encoding="utf-8") == "keep"
    assert (new / wp.MIGRATION_MARKER).exists()


def test_missing_legacy_is_skipped_without_marker(tmp_path):
    """没有旧数据：不报错、不建目录、不写标记。"""
    new = tmp_path / "new" / "Default"
    assert wp.migrate_legacy_local_storage(tmp_path / "nope", new) is False
    assert not new.exists()


def test_copy_failure_leaves_no_partial_data_and_retries(tmp_path, monkeypatch):
    """复制失败：不抛异常、不写标记、不留临时目录；下次启动能重试成功。"""
    legacy = _make_legacy(tmp_path)
    new = tmp_path / "new" / "Default"
    real_copytree = wp.shutil.copytree

    def boom(src, dst, *args, **kwargs):
        Path(dst).mkdir(parents=True)
        (Path(dst) / "half").write_text("x", encoding="utf-8")
        raise PermissionError("locked")

    monkeypatch.setattr(wp.shutil, "copytree", boom)
    assert wp.migrate_legacy_local_storage(legacy, new) is False
    assert not (new / wp.MIGRATION_MARKER).exists()
    assert not (new / "Local Storage").exists()
    assert not (new / "Local Storage.migrating").exists()

    monkeypatch.setattr(wp.shutil, "copytree", real_copytree)
    assert wp.migrate_legacy_local_storage(legacy, new) is True


def test_legacy_dir_matches_qt5_layout(tmp_path):
    """旧目录 = <LOCALAPPDATA>/<组织>/<应用>/QtWebEngine/Default（与 Qt5 实测路径一致）。"""
    got = wp.legacy_profile_dir("BiliOpsToolbox", "B站运营工具箱", local_appdata=str(tmp_path))
    assert got == tmp_path / "BiliOpsToolbox" / "B站运营工具箱" / "QtWebEngine" / "Default"


def test_main_window_uses_persistent_profile_before_first_page():
    """MainWindow：先建持久化 profile（内含迁移），再构造 DesktopWebPage；退出前释放页面。"""
    src = (REPO_ROOT / "desktop" / "main_window.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
    init_ui = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "init_ui")
    body = ast.get_source_segment(src, init_ui)
    assert "self._web_profile = create_persistent_profile()" in body
    assert body.index("create_persistent_profile()") < body.index("DesktopWebPage(self._web_profile")
    assert "app.aboutToQuit.connect(self._release_web_page)" in body
    assert "profile = self._web_profile" in body
    # 不能退回 QWebEngineView 自带的（Qt6 下无痕的）默认页面 profile
    assert "self.web_view.page().profile()" not in body


_PROFILE_PROBE = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
app.setOrganizationName("BiliOpsToolbox"); app.setApplicationName("B站运营工具箱")
from desktop.webengine_profile import create_persistent_profile
from desktop.main_window import DesktopWebPage
profile = create_persistent_profile()
page = DesktopWebPage(profile)
print(json.dumps({"otr": profile.isOffTheRecord(), "name": profile.storageName(),
                  "path": profile.persistentStoragePath(), "page_otr": page.profile().isOffTheRecord()}))
page.deleteLater()
"""


def test_persistent_profile_is_not_off_the_record(tmp_path):
    """真实 PySide6：有名 profile 不是无痕，页面用的就是它；数据落在指定目录，不碰真实用户目录。"""
    pytest.importorskip("PySide6.QtWebEngineCore")
    env = dict(os.environ, **{wp.STORAGE_ROOT_ENV: str(tmp_path / "webengine")})
    proc = subprocess.run([sys.executable, "-c", _PROFILE_PROBE, str(REPO_ROOT)], cwd=str(tmp_path),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=env)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("{")), None)
    assert proc.returncode == 0 and line, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr[-1500:]}"
    out = json.loads(line)
    assert out["otr"] is False and out["page_otr"] is False
    assert out["name"] == wp.PROFILE_NAME
    assert Path(out["path"]) == tmp_path / "webengine" / wp.PROFILE_NAME


def test_select_graphics_api_defaults_to_opengl(monkeypatch):
    """默认把 Qt Quick 图形接口设为 OpenGL（Direct3D 11 在双显卡机器上悬停时整页闪烁）。"""
    from PySide6.QtQuick import QQuickWindow, QSGRendererInterface

    calls = []
    monkeypatch.delenv("QSG_RHI_BACKEND", raising=False)
    monkeypatch.setattr(QQuickWindow, "setGraphicsApi", staticmethod(lambda api: calls.append(api)))
    assert wp.select_graphics_api() == "opengl"
    assert calls == [QSGRendererInterface.GraphicsApi.OpenGL]


def test_select_graphics_api_respects_env(monkeypatch):
    """排障时显式设置的 QSG_RHI_BACKEND 不被覆盖。"""
    from PySide6.QtQuick import QQuickWindow

    calls = []
    monkeypatch.setenv("QSG_RHI_BACKEND", "d3d11")
    monkeypatch.setattr(QQuickWindow, "setGraphicsApi", staticmethod(lambda api: calls.append(api)))
    assert wp.select_graphics_api() == "d3d11"
    assert calls == []


def test_run_desktop_selects_graphics_api_before_qapplication():
    """app.run_desktop 在创建 QApplication 之前选图形接口（之后设置无效）。"""
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    body = src[src.index("def run_desktop("):]
    body = body[:body.index("\ndef ", 1)]
    assert "select_graphics_api()" in body
    assert body.index("select_graphics_api()") < body.index("app = QApplication(")
