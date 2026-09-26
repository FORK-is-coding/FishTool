"""桌面客户端遗留问题回归验证。

仅验证桌面壳差异：QWebEngine 弹窗 DOM 与桌宠拖拽，不修改 Web 业务数据。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from PyQt5.QtCore import QEvent, QPoint, QSettings, Qt, QTimer, QUrl
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineProfile
from PyQt5.QtWidgets import QApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from desktop.pet_window import PetWindow  # noqa: E402


class OfflinePetWindow(PetWindow):
    """跳过 WebSocket 的测试桌宠，避免回归测试依赖网络时序。"""

    def setup_websocket(self) -> None:
        """测试中不创建通信线程，拖拽逻辑与正式窗口保持一致。"""


class WebModalProbe:
    """用真实 QWebEnginePage 验证客户端弹窗入口和 DOM。"""

    def __init__(self, app: QApplication, url: str) -> None:
        """初始化页面探针。

        Args:
            app: 当前 Qt 应用。
            url: 待检查的本地 Web 地址。
        """
        self.app = app
        self.url = url
        self.result: dict[str, object] = {}
        self.page = QWebEnginePage()
        profile = self.page.profile()
        profile.setHttpCacheType(QWebEngineProfile.NoCache)
        profile.clearHttpCache()
        self.page.loadFinished.connect(self._on_loaded)

    def run(self) -> dict[str, object]:
        """加载页面并等待异步 JavaScript 验证完成。

        Returns:
            dict: 弹窗与静态图片检查结果。
        """
        QTimer.singleShot(15000, self._on_timeout)
        self.page.load(QUrl(self.url))
        self.app.exec_()
        return self.result

    def _on_loaded(self, ok: bool) -> None:
        """页面加载后创建群聊和校验弹窗，并读取 DOM 结果。"""
        if not ok:
            self.result = {"pageLoaded": False}
            self.app.quit()
            return
        script = """
            (() => {
                const result = {
                    pageLoaded: true,
                    pageUrl: window.location.href,
                    groupEntry: typeof window.showGroupQrModal === 'function',
                    verifyEntry: typeof window.verifyLotteryWinners === 'function'
                };
                const isVisible = element => {
                    if (!element) return false;
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0 &&
                        Number(style.zIndex || 0) >= 1000;
                };
                try {
                    const groupButton = document.querySelector('button[onclick="showGroupQrModal()"]');
                    result.groupButton = Boolean(groupButton);
                    groupButton?.click();
                    result.groupModal = Boolean(document.querySelector('.group-qr-modal'));
                    const image = document.querySelector('.group-qr-image');
                    const groupBackdrop = document.querySelector('.app-modal-backdrop');
                    result.groupImageUrl = Boolean(image && image.src.includes('/static/'));
                    result.groupVisible = isVisible(groupBackdrop);
                    groupBackdrop?.remove();

                    const verifyButton = document.getElementById('lottery-verify-winners-button');
                    result.verifyButton = Boolean(verifyButton);
                    verifyButton?.click();
                    const verifyBackdrop = document.querySelector('.app-modal-backdrop');
                    result.verifyModal = Boolean(verifyBackdrop);
                    result.verifyVisible = isVisible(verifyBackdrop);
                } catch (error) {
                    result.error = String(error && error.stack ? error.stack : error);
                }
                return result;
            })();
        """
        self.page.runJavaScript(script, self._finish)

    def _finish(self, result: object) -> None:
        """保存 JavaScript 返回值并退出测试事件循环。"""
        self.result = result if isinstance(result, dict) else {"invalidResult": str(result)}
        self.app.quit()

    def _on_timeout(self) -> None:
        """页面或脚本超时时返回明确失败结果。"""
        if not self.result:
            self.result = {"timeout": True}
            self.app.quit()


def verify_pet_drag(app: QApplication) -> dict[str, object]:
    """模拟鼠标拖拽并断言窗口位移、素材切换和位置持久化。

    Args:
        app: 当前 Qt 应用。

    Returns:
        dict: 桌宠拖拽验证结果。
    """
    pet = OfflinePetWindow()
    with tempfile.TemporaryDirectory(prefix="fishtool-pet-test-") as temp_dir:
        pet.settings = QSettings(str(Path(temp_dir) / "pet.ini"), QSettings.IniFormat)
        pet.show()
        app.processEvents()
        old_pos = pet.pos()
        local_pos = QPoint(pet.width() // 2, pet.height() // 2)
        start_global = pet.mapToGlobal(local_pos)
        target_global = start_global + QPoint(80, 45)

        press = QMouseEvent(
            QEvent.MouseButtonPress, local_pos, start_global,
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        pet.mousePressEvent(press)
        dragging_state = pet.current_state == pet.STATE_DRAGGING
        dragging_asset = pet.image_label._pixmap.cacheKey() == pet.images[pet.STATE_DRAGGING].cacheKey()

        move = QMouseEvent(
            QEvent.MouseMove, pet.mapFromGlobal(target_global), target_global,
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
        )
        pet.mouseMoveEvent(move)
        app.processEvents()
        moved = pet.pos() == old_pos + QPoint(80, 45)

        release = QMouseEvent(
            QEvent.MouseButtonRelease, pet.mapFromGlobal(target_global), target_global,
            Qt.LeftButton, Qt.NoButton, Qt.NoModifier,
        )
        pet.mouseReleaseEvent(release)
        idle_state = pet.current_state == pet.STATE_IDLE
        persisted = pet.settings.value("position") == pet.pos()
        pet.close()

    return {
        "draggingState": dragging_state,
        "draggingAsset": dragging_asset,
        "windowMoved": moved,
        "idleAfterRelease": idle_state,
        "positionPersisted": persisted,
    }


def main() -> int:
    """执行桌面回归验证并以进程退出码表示结果。"""
    app = QApplication.instance() or QApplication(sys.argv)
    pet_result = verify_pet_drag(app)
    web_result = WebModalProbe(app, "http://127.0.0.1:8000/?desktop_test=1").run()
    result = {"pet": pet_result, "webEngine": web_result}
    print(json.dumps(result, ensure_ascii=False, indent=2))

    pet_ok = all(pet_result.values())
    web_ok = all(
        web_result.get(key) is True
        for key in (
            "pageLoaded", "groupEntry", "verifyEntry", "groupButton", "verifyButton",
            "groupModal", "groupImageUrl", "groupVisible", "verifyModal", "verifyVisible",
        )
    )
    return 0 if pet_ok and web_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
