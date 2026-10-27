"""主窗口 loadFinished 回调不得在生产页面注入弹窗验收探针（#10）。

旧实现每次业务页加载完成后都会注入一段脚本：点击「加入群聊」「校验中奖名单」
按钮并删除 ``.app-modal-backdrop``，既会误删加载时真实弹出的提示，也让每次
加载都走一遍中奖名单校验入口。回调应只保留 ``__FISHTOOL_DESKTOP__`` 标记注入。

为什么不构造真实 MainWindow：
    offscreen 下构造 QWebEnginePage / MainWindow 会触发 0xC0000005
    （见 tests/test_desktop_webengine_isolation.py），因此这里用最小替身
    直接调用未绑定方法，只验证回调本身的行为。
"""
from __future__ import annotations

import pytest

# 模块级 import：QtWebEngineWidgets 必须早于任何 QApplication 创建
from desktop.main_window import MainWindow

WEB_URL = "http://localhost:8000"
DESKTOP_FLAG_SCRIPT = "window.__FISHTOOL_DESKTOP__ = true;"


class _Url:
    """QUrl 替身，仅提供 toString。"""

    def __init__(self, value: str) -> None:
        """保存 URL 文本。"""
        self._value = value

    def toString(self) -> str:  # noqa: N802 - 与 Qt API 同名
        """返回 URL 文本。"""
        return self._value


class _Page:
    """QWebEnginePage 替身，记录被注入的脚本。"""

    def __init__(self) -> None:
        """初始化脚本记录。"""
        self.scripts: list[str] = []

    def runJavaScript(self, script, *callback) -> None:  # noqa: N802 - 与 Qt API 同名
        """记录注入脚本，忽略回调。"""
        self.scripts.append(script)


class _View:
    """QWebEngineView 替身。"""

    def __init__(self, url: str) -> None:
        """保存当前 URL 与页面替身。"""
        self._url = url
        self._page = _Page()

    def url(self) -> _Url:
        """返回当前 URL。"""
        return _Url(self._url)

    def page(self) -> _Page:
        """返回页面替身。"""
        return self._page


class _Host:
    """承载 _on_web_load_finished 所需属性的最小宿主。"""

    web_url = WEB_URL

    def __init__(self, url: str) -> None:
        """构造指向给定 URL 的视图替身。"""
        self.web_view = _View(url)


def test_business_page_load_only_injects_desktop_flag():
    """业务页加载成功后只注入桌面标记，不再点击按钮或删除弹窗遮罩。"""
    host = _Host(f"{WEB_URL}/?desktop_start=1")

    MainWindow._on_web_load_finished(host, True)

    scripts = host.web_view.page().scripts
    assert scripts == [DESKTOP_FLAG_SCRIPT]
    for script in scripts:
        assert ".click()" not in script
        assert "app-modal-backdrop" not in script
        assert "lottery-verify-winners-button" not in script


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        (f"{WEB_URL}/?desktop_start=1", False),
        ("about:blank", True),
        ("data:text/html,loading", True),
    ],
)
def test_failed_or_placeholder_load_injects_nothing(url, ok):
    """加载失败或内嵌加载页（非 web_url）不注入任何脚本。"""
    host = _Host(url)

    MainWindow._on_web_load_finished(host, ok)

    assert host.web_view.page().scripts == []
