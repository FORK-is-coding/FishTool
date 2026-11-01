"""欢迎页启动公告的真实桌面内核探针（由 tests/test_frontend_welcome_notice.py 以子进程启动）。

不作为 pytest 用例：文件名不以 test_ 开头。
与 _intro_child.py 同一做法：系统原生平台 + WA_DontShowOnScreen；本地静态服务器托管 web/frontend，
/api、/ws 一律 503。依次加载三个场景，每个场景输出页面上的公告状态，最后打印一行 JSON。
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

if sys.platform == "win32":
    os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

FRONTEND = Path(sys.argv[1]).resolve()
SHOT_PATH = sys.argv[2] if len(sys.argv) > 2 else ""


class _Handler(http.server.SimpleHTTPRequestHandler):
    """静态文件 + 后端接口一律 503：探针只关心前端弹窗。"""

    def log_message(self, *args) -> None:  # 静默访问日志，避免污染 stdout
        return

    def do_GET(self) -> None:  # noqa: N802 - 标准库方法名
        if self.path.startswith(("/api/", "/ws")):
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"probe"}')
            return
        # 场景 URL 带查询参数，按去掉查询串后的路径映射首页
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            self.path = "/templates/index.html"
        super().do_GET()

    do_POST = do_GET


_COLLECT_ERRORS = r"""
window.__probeErrors = [];
window.addEventListener('error', e => window.__probeErrors.push(e.message + ' @' + (e.filename || '').split('/').pop() + ':' + e.lineno));
"""

# 弹窗在视口内的几何、内容和焦点；再点「阅读使用说明」看是否切页并收起。
_PROBE = r"""
(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const q = s => document.querySelector(s);
  const out = { ua: navigator.userAgent, errors: [] };
  const bd = q('.welcome-notice-backdrop');
  out.shown = !!bd;
  out.backdropCount = document.querySelectorAll('.app-modal-backdrop').length;
  out.introPending = document.documentElement.classList.contains('intro-pending');
  out.introOverlay = !!document.getElementById('fish-intro');
  if (bd) {
    // 弹窗上部中心点是否被开场层等其他元素盖住
    const c = q('.welcome-notice-modal').getBoundingClientRect();
    const hit = document.elementFromPoint(c.left + c.width / 2, c.top + 40);
    out.centerInsideModal = !!(hit && hit.closest('.welcome-notice-modal'));
    const m =q('.welcome-notice-modal').getBoundingClientRect();
    const b = bd.getBoundingClientRect();
    out.modal = { x: Math.round(m.left), y: Math.round(m.top), w: Math.round(m.width), h: Math.round(m.height) };
    out.backdrop = { w: Math.round(b.width), h: Math.round(b.height), vw: innerWidth, vh: innerHeight };
    out.title = q('#welcome-notice-title').textContent;
    out.strong = [...document.querySelectorAll('.welcome-notice-body strong')].map(s => s.textContent);
    out.alertColor = getComputedStyle(q('.welcome-notice-alert strong')).color;
    out.alertBorder = getComputedStyle(q('.welcome-notice-alert')).borderLeftColor;
    out.mailText = q('.welcome-notice-mail').textContent;
    out.focus = document.activeElement ? document.activeElement.textContent : null;
    const btns = [...document.querySelectorAll('.welcome-notice-modal .app-modal-actions .btn')];
    out.buttons = btns.map(x => x.textContent);
    if (btns.length === 2) {
      const r0 = btns[0].getBoundingClientRect(), r1 = btns[1].getBoundingClientRect();
      out.buttonGap = Math.round(r1.left - r0.right);
    }
    if (window.__SHOT__) { window.__probeShotReady = true; await wait(window.__SHOT__); }
    btns[0].click(); await wait(120);
    out.afterUsage = {
      backdrops: document.querySelectorAll('.app-modal-backdrop').length,
      usageActive: q('#usage-page').classList.contains('active'),
    };
    navigateTo('welcome'); await wait(120);
    out.reopenedOnWelcome = !!q('.welcome-notice-backdrop');
  }
  out.sessionFlag = sessionStorage.getItem('fishtool.notice.v1');
  out.errors = window.__probeErrors.slice();
  window.__probeResult = JSON.stringify(out);
})().catch(e => { window.__probeResult = JSON.stringify({ fatal: String(e && e.stack || e) }); });
true
"""

# report_ms：页面加载完成后多久开始探测。intro 场景开场约 8s，留余量到 11s。
# reload 场景在同一页面会话里再加载一次，验证刷新不重复弹。
_SCENARIOS = [
    {"name": "plain", "query": "", "report_ms": 1500, "fresh": True},
    {"name": "reload", "query": "", "report_ms": 1500, "fresh": False},
    {"name": "shot", "query": "?shot=1", "report_ms": 1500, "fresh": True},
    {"name": "intro_playing", "query": "?intro=full", "report_ms": 1500, "fresh": True, "no_click": True},
    # 开场推出阶段（intro-pending 已去掉、开场层还在）：仍不应弹出
    {"name": "intro_leaving", "query": "?intro=full", "report_ms": 0, "fresh": True, "no_click": True,
     "wait_js": "!document.documentElement.classList.contains('intro-pending') && !!document.getElementById('fish-intro')"},
    {"name": "intro_done", "query": "?intro=full", "report_ms": 11000, "fresh": True},
]


def main() -> int:
    """依次跑各场景并把结果 JSON 打到 stdout。

    Returns:
        int: 全部场景都拿到结果返回 0，超时返回 2。
    """
    httpd = socketserver.TCPServer(("127.0.0.1", 0), partial(_Handler, directory=str(FRONTEND)))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    app = QApplication(sys.argv[:1])
    view = QWebEngineView()
    view.setAttribute(Qt.WA_DontShowOnScreen, True)
    # 与 desktop/main_window.py 的 INITIAL_WINDOW_WIDTH 一致，按桌面默认窗口宽度测布局。
    view.resize(1440, 900)
    view.show()

    results: dict = {}
    state = {"index": -1, "page": None}

    def new_page() -> QWebEnginePage:
        page = QWebEnginePage(view)
        script = QWebEngineScript()
        script.setSourceCode(_COLLECT_ERRORS)
        script.setInjectionPoint(QWebEngineScript.DocumentCreation)
        script.setWorldId(QWebEngineScript.MainWorld)
        page.scripts().insert(script)
        page.loadFinished.connect(on_load)
        view.setPage(page)
        return page

    def run_next() -> None:
        state["index"] += 1
        if state["index"] >= len(_SCENARIOS):
            app.quit()
            return
        sc = _SCENARIOS[state["index"]]
        # fresh：新建页面（新的 sessionStorage）；否则复用上一个页面，模拟同一标签页刷新。
        if sc["fresh"] or state["page"] is None:
            state["page"] = new_page()
        state["page"].setUrl(QUrl(f"http://127.0.0.1:{port}/{sc['query']}"))

    def poll(page: QWebEnginePage, name: str) -> None:
        def got(raw) -> None:
            if not raw:
                QTimer.singleShot(200, lambda: poll(page, name))
                return
            results[name] = json.loads(raw)
            QTimer.singleShot(200, run_next)
        page.runJavaScript("window.__probeResult || null", got)

    def grab_when_ready(page: QWebEnginePage) -> None:
        def got(ready) -> None:
            if ready:
                view.grab().save(SHOT_PATH)
                return
            QTimer.singleShot(100, lambda: grab_when_ready(page))
        page.runJavaScript("window.__probeShotReady === true", got)

    def on_load(_ok: bool) -> None:
        page = state["page"]
        sc = _SCENARIOS[state["index"]]
        probe = _PROBE
        if sc.get("no_click"):
            # 开场进行中只读状态，不点按钮、不切页，避免影响后续场景。
            probe = ("window.__probeResult = JSON.stringify({ shown: !!document.querySelector('.welcome-notice-backdrop'),"
                     " introPending: document.documentElement.classList.contains('intro-pending'),"
                     " introOverlay: !!document.getElementById('fish-intro'), errors: window.__probeErrors.slice() }); true")
        shot = SHOT_PATH and sc["name"] == "plain"

        def go() -> None:
            if shot:
                page.runJavaScript("window.__SHOT__ = 1500; true")
                grab_when_ready(page)
            page.runJavaScript(probe)
            QTimer.singleShot(300, lambda: poll(page, sc["name"]))

        def wait_then_go() -> None:
            # wait_js：等页面进入指定状态再探测（例如开场推出阶段）
            page.runJavaScript(sc["wait_js"], lambda ok: go() if ok else QTimer.singleShot(16, wait_then_go))

        QTimer.singleShot(sc["report_ms"], wait_then_go if sc.get("wait_js") else go)

    QTimer.singleShot(0, run_next)
    QTimer.singleShot(150000, app.quit)
    app.exec()
    httpd.shutdown()
    if len(results) != len(_SCENARIOS):
        print(f"probe timeout: got {sorted(results)}", file=sys.stderr)
        return 2
    # 只输出 ASCII：父进程按 utf-8 解码，Windows 控制台代码页不影响结果。
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
