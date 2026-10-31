"""真实桌面内核探针（由 tests/test_frontend_qtwebengine_compat.py 以子进程启动）。

不作为 pytest 用例：文件名不以 test_ 开头。

为什么是子进程、为什么不用 offscreen：
    QT_QPA_PLATFORM=offscreen 下构造 QWebEngineView 会 STATUS_ACCESS_VIOLATION
    （见 test_desktop_webengine_isolation.py），所以这里显式使用系统原生平台，
    窗口加 WA_DontShowOnScreen 不弹出。无图形环境时父进程按 skip 处理。

做法：本地起只读静态服务器托管 web/frontend（/api、/ws 一律 503，不依赖后端），
    1280x800 加载真实 index.html，输出一行 JSON 给父进程断言。
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import threading
from functools import partial
from pathlib import Path

# 必须在任何 Qt 对象之前设置：父进程（pytest）可能带着 offscreen。
if sys.platform == "win32":
    os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PyQt5.QtCore import Qt, QTimer, QUrl  # noqa: E402
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineScript, QWebEngineView  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

FRONTEND = Path(sys.argv[1]).resolve()


class _Handler(http.server.SimpleHTTPRequestHandler):
    """静态文件 + 后端接口一律 503：探针只关心前端在内核里的渲染与执行。"""

    def log_message(self, *args) -> None:  # 静默访问日志，避免污染 stdout
        return

    def do_GET(self) -> None:  # noqa: N802 - 标准库方法名
        if self.path.startswith(("/api/", "/ws")):
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"probe"}')
            return
        if self.path in ("/", "/index.html"):
            self.path = "/templates/index.html"
        super().do_GET()

    do_POST = do_GET


_COLLECT_ERRORS = r"""
window.__probeErrors = [];
window.addEventListener('error', e => window.__probeErrors.push(e.message + ' @' + (e.filename || '').split('/').pop() + ':' + e.lineno));
"""

_PROBE = r"""
(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const out = { ua: navigator.userAgent };
  navigateTo('hotspot'); await wait(80);
  const mask = document.getElementById('hotspot-drawer-mask');
  mask.classList.add('hotspot-drawer-mask-show'); await wait(30);
  const rect = mask.getBoundingClientRect();
  out.drawerMask = { w: Math.round(rect.width), h: Math.round(rect.height), vw: innerWidth, vh: innerHeight };
  mask.classList.remove('hotspot-drawer-mask-show');
  // :is() 展开后的卡片规则是否生效：取 isolation / overflow。
  // 不取 transition：系统开启“减少动态效果”时 reduced-motion 规则会把它置为 0s，结果随机器设置而变。
  const sectionCs = getComputedStyle(document.querySelector('#hotspot-page .section'));
  out.sectionIsolation = sectionCs.isolation;
  out.sectionOverflow = sectionCs.overflow;
  navigateTo('logs'); await wait(60);
  const origCreate = URL.createObjectURL, origRevoke = URL.revokeObjectURL, origClick = HTMLAnchorElement.prototype.click;
  let name = null, err = null;
  URL.createObjectURL = () => 'blob:probe'; URL.revokeObjectURL = () => {};
  HTMLAnchorElement.prototype.click = function () { name = this.download; };
  try { exportProblemLogs(); } catch (e) { err = e.name + ': ' + e.message; }
  URL.createObjectURL = origCreate; URL.revokeObjectURL = origRevoke; HTMLAnchorElement.prototype.click = origClick;
  out.exportError = err; out.exportName = name;
  out.errors = window.__probeErrors.slice();
  window.__probeResult = JSON.stringify(out);
})().catch(e => { window.__probeResult = JSON.stringify({ fatal: String(e && e.stack || e) }); });
true
"""


def main() -> int:
    """启动静态服务器与隐藏视图，跑探针并把结果 JSON 打到 stdout。

    Returns:
        int: 拿到结果返回 0，超时返回 2。
    """
    httpd = socketserver.TCPServer(("127.0.0.1", 0), partial(_Handler, directory=str(FRONTEND)))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    app = QApplication(sys.argv[:1])
    view = QWebEngineView()
    page = QWebEnginePage(view)
    view.setPage(page)
    script = QWebEngineScript()
    script.setSourceCode(_COLLECT_ERRORS)
    script.setInjectionPoint(QWebEngineScript.DocumentCreation)
    script.setWorldId(QWebEngineScript.MainWorld)
    page.scripts().insert(script)
    view.setAttribute(Qt.WA_DontShowOnScreen, True)
    view.resize(1280, 800)
    view.show()

    result: dict = {}

    def done(raw) -> None:
        result["raw"] = raw
        app.quit()

    def poll() -> None:
        page.runJavaScript("window.__probeResult || null", lambda r: done(r) if r else QTimer.singleShot(300, poll))

    def on_load(_ok: bool) -> None:
        QTimer.singleShot(1200, lambda: (page.runJavaScript(_PROBE), QTimer.singleShot(400, poll)))

    page.loadFinished.connect(on_load)
    page.setUrl(QUrl(f"http://127.0.0.1:{port}/"))
    QTimer.singleShot(60000, app.quit)
    app.exec_()
    httpd.shutdown()
    if "raw" not in result:
        print("probe timeout", file=sys.stderr)
        return 2
    print(result["raw"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
