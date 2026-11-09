"""桌面壳外链：target=_blank / window.open 交给系统浏览器，不渲染外站。"""
import pytest

QtWebEngineCore = pytest.importorskip("PySide6.QtWebEngineCore")
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication


@pytest.fixture()
def app():
    instance = QApplication.instance() or QApplication(["-platform", "offscreen"])
    yield instance


def test_external_link_opens_in_system_browser(app, monkeypatch):
    from desktop.main_window import _ExternalLinkPage

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    page = _ExternalLinkPage()
    allowed = page.acceptNavigationRequest(QUrl("https://www.bilibili.com/video/BV1xx#reply1"),
                                           QtWebEngineCore.QWebEnginePage.NavigationTypeLinkClicked, True)
    assert allowed is False
    assert opened == ["https://www.bilibili.com/video/BV1xx#reply1"]


def test_non_http_scheme_is_not_opened(app, monkeypatch):
    from desktop.main_window import _ExternalLinkPage

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    page = _ExternalLinkPage()
    assert page.acceptNavigationRequest(QUrl("file:///c:/x"),
                                        QtWebEngineCore.QWebEnginePage.NavigationTypeLinkClicked, True) is False
    assert opened == []


def test_desktop_page_returns_external_link_page(app):
    from desktop.main_window import DesktopWebPage, _ExternalLinkPage

    page = DesktopWebPage()
    child = page.createWindow(QtWebEngineCore.QWebEnginePage.WebBrowserTab)
    assert isinstance(child, _ExternalLinkPage)
