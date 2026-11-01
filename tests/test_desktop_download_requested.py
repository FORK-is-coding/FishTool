"""桌面端网页下载回归（#14 下载入口）。

覆盖对象：
- desktop.main_window._default_download_path：保存对话框默认路径（纯函数）
- MainWindow._on_download_requested：保存对话框确认后 accept，取消 / 异常则 cancel
- init_ui 必须连接 profile.downloadRequested（否则 QWebEngine 静默丢弃下载）

测试策略：
- 不构造 MainWindow / QWebEngineView（WebEngine 只在子进程里构造，见 tests/test_desktop_webengine_isolation.py），
  用替身 download 对象调用未绑定方法。
- QFileDialog 用 monkeypatch 替换，不弹真实对话框。
"""
from __future__ import annotations

from pathlib import Path

import pytest

# 模块级 import（PySide6 下导入顺序没有要求；保持在模块级只是为了收集期就暴露导入错误）
from desktop import main_window as main_window_module
from desktop.main_window import MainWindow

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _default_download_path(suggested_name, downloads_dir=None):
    """运行期查找被测纯函数，便于在旧代码上逐条报告失败。"""
    return main_window_module._default_download_path(suggested_name, downloads_dir)


# ---------------------------------------------------------------------------
# 纯函数 _default_download_path
# ---------------------------------------------------------------------------


def test_default_download_path_joins_downloads_dir(tmp_path):
    """建议文件名拼到下载目录下。"""
    result = _default_download_path("bili_ops_errors_2026-10-27.txt", tmp_path)
    assert Path(result) == tmp_path / "bili_ops_errors_2026-10-27.txt"


@pytest.mark.parametrize(
    ("suggested", "expected"),
    [
        ("../../evil.txt", "evil.txt"),
        ("..\\..\\evil.txt", "evil.txt"),
        ("C:\\Windows\\evil.txt", "evil.txt"),
        ("  spaced.txt  ", "spaced.txt"),
        ("", "download"),
        (None, "download"),
        ("..", "download"),
        (".", "download"),
    ],
)
def test_default_download_path_strips_directories(tmp_path, suggested, expected):
    """网页给出的建议名只取最后一段，空名回退为 download。"""
    assert Path(_default_download_path(suggested, tmp_path)) == tmp_path / expected


def test_default_download_path_missing_dir_returns_bare_name(tmp_path):
    """下载目录不存在时只返回文件名，与报告导出的写法一致。"""
    assert _default_download_path("a.txt", tmp_path / "missing") == "a.txt"


def test_default_download_path_defaults_to_home_downloads(tmp_path, monkeypatch):
    """省略目录参数时取 ~/Downloads。"""
    (tmp_path / "Downloads").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert Path(_default_download_path("a.txt")) == tmp_path / "Downloads" / "a.txt"


# ---------------------------------------------------------------------------
# MainWindow._on_download_requested
# ---------------------------------------------------------------------------


class _FakeDownload:
    """QWebEngineDownloadRequest 替身（Qt5 时为 QWebEngineDownloadItem），记录 accept / cancel 与目标路径。"""

    def __init__(self, suggested: str) -> None:
        """保存建议文件名。"""
        self._suggested = suggested
        self.directory = None
        self.file_name = None
        self.accepted = False
        self.cancelled = False

    def suggestedFileName(self) -> str:  # noqa: N802 - 与 Qt API 同名
        """返回建议文件名。"""
        return self._suggested

    def setDownloadDirectory(self, directory: str) -> None:  # noqa: N802
        """记录下载目录。"""
        self.directory = directory

    def setDownloadFileName(self, name: str) -> None:  # noqa: N802
        """记录下载文件名。"""
        self.file_name = name

    def accept(self) -> None:
        """记录接受下载。"""
        self.accepted = True

    def cancel(self) -> None:
        """记录取消下载。"""
        self.cancelled = True


def _patch_save_dialog(monkeypatch, tmp_path, chosen=None, error=None):
    """替换 QFileDialog 与下载目录，返回对话框收到的默认路径列表。"""
    calls = []
    (tmp_path / "Downloads").mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    class _Dialog:
        @staticmethod
        def getSaveFileName(parent, caption, default_path, *args):  # noqa: N802
            calls.append(default_path)
            if error is not None:
                raise error
            return chosen, ""

    monkeypatch.setattr(main_window_module, "QFileDialog", _Dialog)
    return calls


def test_download_requested_accepts_chosen_path(monkeypatch, tmp_path):
    """用户选定路径后设置目录与文件名并 accept。"""
    chosen = tmp_path / "picked" / "my_logs.txt"
    calls = _patch_save_dialog(monkeypatch, tmp_path, str(chosen))
    download = _FakeDownload("bili_ops_errors.txt")

    MainWindow._on_download_requested(object(), download)

    assert calls == [str(tmp_path / "Downloads" / "bili_ops_errors.txt")]
    assert download.accepted is True
    assert download.cancelled is False
    assert Path(download.directory) == chosen.parent
    assert download.file_name == "my_logs.txt"


def test_download_requested_cancel_dialog_cancels(monkeypatch, tmp_path):
    """用户取消保存对话框时取消下载，不 accept。"""
    _patch_save_dialog(monkeypatch, tmp_path, "")
    download = _FakeDownload("bili_ops_errors.txt")

    MainWindow._on_download_requested(object(), download)

    assert download.accepted is False
    assert download.cancelled is True
    assert download.directory is None


def test_download_requested_dialog_error_cancels(monkeypatch, tmp_path):
    """保存对话框抛异常时取消下载，不向 Qt 信号外抛。"""
    _patch_save_dialog(monkeypatch, tmp_path, error=RuntimeError("boom"))
    download = _FakeDownload("bili_ops_errors.txt")

    MainWindow._on_download_requested(object(), download)

    assert download.accepted is False
    assert download.cancelled is True


def test_profile_download_signal_is_connected():
    """init_ui 必须直连 profile.downloadRequested（accept 需在处理函数内同步完成）。"""
    source = (PROJECT_ROOT / "desktop" / "main_window.py").read_text(encoding="utf-8")
    assert "profile.downloadRequested.connect(self._on_download_requested)" in source
