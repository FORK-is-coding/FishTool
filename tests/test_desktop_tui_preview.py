"""桌面壳 TUI 核心前端的纯函数回归：URL 与窗口标题。"""
from __future__ import annotations

from desktop.main_window import (
    APP_WINDOW_TITLE,
    TUI_PREVIEW_PATH,
    TUI_PREVIEW_WINDOW_TITLE,
    MainWindow,
    build_desktop_page_url,
)


def test_default_page_url_stays_webui_root():
    """默认桌面版仍加载根路径，不改变正式 WebUI。"""
    assert build_desktop_page_url("http://localhost:8000", False, 123) == (
        "http://localhost:8000/?desktop_start=123"
    )


def test_tui_page_url_points_to_static_preview():
    """TUI 核心前端从 /tui 进入，并保留缓存戳。"""
    url = build_desktop_page_url("http://localhost:8000", True, 456)
    assert url == f"http://localhost:8000{TUI_PREVIEW_PATH}?desktop_start=456"
    assert url.startswith("http://localhost:8000/tui")


def test_page_url_normalizes_trailing_slash():
    """Web 根地址带尾斜杠时不会产生双斜杠。"""
    assert build_desktop_page_url("http://localhost:9000/", False, 1) == (
        "http://localhost:9000/?desktop_start=1"
    )
    assert build_desktop_page_url("http://localhost:9000/", True, 2) == (
        "http://localhost:9000/tui?desktop_start=2"
    )


def test_window_title_distinguishes_tui_preview():
    """TUI 窗口标题明确标注，不冒充正式桌面界面。"""

    class _Host:
        _tui_preview = False

    assert MainWindow._window_title(_Host()) == APP_WINDOW_TITLE
    _Host._tui_preview = True
    assert MainWindow._window_title(_Host()) == TUI_PREVIEW_WINDOW_TITLE


def test_load_web_ui_switches_between_webui_and_tui(monkeypatch):
    """托盘切换走同一个 load_web_ui：默认根路径，TUI 模式走静态预览页。"""

    class _Signal:
        def __init__(self):
            self.emitted = 0

        def emit(self):
            self.emitted += 1

    class _View:
        def __init__(self):
            self.urls = []

        def setUrl(self, url):  # noqa: N802 - 与 Qt API 同名
            self.urls.append(url.toString())

    class _Host:
        web_url = "http://localhost:8000"
        _tui_preview = False
        _window_title = MainWindow._window_title
        _update_tui_action = lambda self: None

        def __init__(self):
            self.web_view = _View()
            self.web_ready = _Signal()
            self.titles = []

        def setWindowTitle(self, title):  # noqa: N802 - 与 Qt API 同名
            self.titles.append(title)

    host = _Host()
    MainWindow.load_web_ui(host, tui_preview=True)
    assert host._tui_preview is True
    assert host.web_view.urls[-1].startswith(
        "http://localhost:8000/tui?desktop_start="
    )
    assert host.titles[-1] == TUI_PREVIEW_WINDOW_TITLE
    assert host.web_ready.emitted == 1

    # 省略参数时沿用当前 TUI 模式，不悄悄跳回正式页面。
    MainWindow.load_web_ui(host)
    assert host.web_view.urls[-1].startswith(
        "http://localhost:8000/tui?desktop_start="
    )

    MainWindow.load_web_ui(host, tui_preview=False)
    assert host._tui_preview is False
    assert host.web_view.urls[-1].startswith("http://localhost:8000/?desktop_start=")
    assert host.titles[-1] == APP_WINDOW_TITLE
    assert host.web_ready.emitted == 3


def test_tui_action_label_follows_mode():
    """托盘文案随模式变化：预览时提供返回正式界面的入口。"""

    class _Action:
        def __init__(self):
            self.text = ""

        def setText(self, text):  # noqa: N802 - 与 Qt API 同名
            self.text = text

    class _Host:
        _tui_preview = False
        tui_action = _Action()

    MainWindow._update_tui_action(_Host())
    assert _Host.tui_action.text == "打开 TUI"
    _Host._tui_preview = True
    MainWindow._update_tui_action(_Host())
    assert _Host.tui_action.text == "返回正式界面"
