"""真实桌面内核探针：首次启动开场动画（由 tests/test_frontend_intro.py 以子进程启动）。

不作为 pytest 用例：文件名不以 test_ 开头。
静态服务与 Qt 初始化照抄 _flex_gap_child.py：系统原生平台 + WA_DontShowOnScreen，
本地静态服务器托管 web/frontend，``/`` 映射到 templates/index.html，/api、/ws 一律 503。
每个场景用独立的无痕 profile（QWebEngineProfile 不传名称），localStorage 互不影响。
不注入 window.__FISHTOOL_DESKTOP__：场景 idle 验证普通浏览器默认不播。
输出一行 JSON：各场景结束时的开场状态、耗时与 JS 报错。
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

from PyQt5.QtCore import Qt, QTimer, QUrl  # noqa: E402
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineView  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

FRONTEND = Path(sys.argv[1]).resolve()


class _Handler(http.server.SimpleHTTPRequestHandler):
    """静态文件 + 后端接口一律 503：探针只关心开场动画。"""

    def log_message(self, *args) -> None:  # 静默访问日志，避免污染 stdout
        return

    def do_GET(self) -> None:  # noqa: N802 - 标准库方法名
        if self.path.startswith(("/api/", "/ws")):
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"probe"}')
            return
        # 场景 URL 带 ?intro=full 等查询参数，按去掉查询串后的路径映射首页
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            self.path = "/templates/index.html"
        super().do_GET()

    do_POST = do_GET


# 文档创建时注入：收集未捕获异常，并每 16ms 记录开场状态首次变为 playing / done 的时刻
_INSTRUMENT = r"""
window.__probeErrors = [];
window.addEventListener('error', function (e) {
  window.__probeErrors.push(e.message + ' @' + (e.filename || '').split('/').pop() + ':' + e.lineno);
});
window.__intro = { playAt: null, doneAt: null };
(function () {
  var timer = setInterval(function () {
    var st = window.FishIntro ? window.FishIntro._state() : 'missing';
    if (st === 'playing' && window.__intro.playAt === null) window.__intro.playAt = performance.now();
    if (st === 'done' && window.__intro.doneAt === null) { window.__intro.doneAt = performance.now(); clearInterval(timer); }
  }, 16);
})();
"""

_REPORT = r"""
JSON.stringify({
  ua: navigator.userAgent,
  state: window.FishIntro ? window.FishIntro._state() : 'missing',
  playAt: window.__intro.playAt,
  doneAt: window.__intro.doneAt,
  skipAt: window.__skipAt === undefined ? null : window.__skipAt,
  pending: document.documentElement.classList.contains('intro-pending'),
  overlay: !!document.getElementById('fish-intro'),
  desktopFlag: !!window.__FISHTOOL_DESKTOP__,
  threeLoaded: typeof THREE !== 'undefined',
  replayButton: !!document.getElementById('intro-replay-btn'),
  sidebarVisibility: getComputedStyle(document.querySelector('.sidebar')).visibility,
  errors: window.__probeErrors.slice()
})
"""

_ESCAPE = r"""
window.__skipAt = performance.now();
window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
true
"""

# report_ms：页面加载完成后多久取结果。full 留足 10s 上限之外的余量；skip 在 1.5s 派发 Escape。
_SCENARIOS = [
    {"name": "full", "query": "?intro=full", "report_ms": 12500},
    {"name": "skip", "query": "?intro=full", "report_ms": 4500, "escape_ms": 1500},
    {"name": "idle", "query": "", "report_ms": 3000},
]


class _Page(QWebEnginePage):
    """只收集开场相关脚本的 console 报错；其余页面在 /api 503 下的报错与本探针无关。"""

    _SOURCES = ("app.intro.js", "intro-gate.js", "three.min.js", "logo-mark.js")

    def __init__(self, profile, parent, sink) -> None:
        super().__init__(profile, parent)
        self._sink = sink

    def javaScriptConsoleMessage(self, level, message, line, source) -> None:  # noqa: N802 - Qt 方法名
        if level < QWebEnginePage.ErrorMessageLevel:
            return
        name = (source or "").split("?", 1)[0].split("/")[-1]
        if name in self._SOURCES or "FishIntro" in message:
            self._sink.append(f"console: {message} @{name}:{line}")


def main() -> int:
    """依次跑三个场景，把结果 JSON 打到 stdout。

    Returns:
        int: 全部场景拿到结果返回 0，超时返回 2。
    """
    httpd = socketserver.TCPServer(("127.0.0.1", 0), partial(_Handler, directory=str(FRONTEND)))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    app = QApplication(sys.argv[:1])
    # 场景之间关窗再开新窗，不能让第一个窗口关掉就退出
    app.setQuitOnLastWindowClosed(False)
    results: dict = {}
    queue = list(_SCENARIOS)
    keep: dict = {}

    def run_next() -> None:
        if not queue:
            app.quit()
            return
        scenario = queue.pop(0)
        console_errors: list = []
        profile = QWebEngineProfile(app)  # 不传名称即无痕 profile，localStorage 只在内存里
        view = QWebEngineView()
        page = _Page(profile, view, console_errors)
        view.setPage(page)
        script = QWebEngineScript()
        script.setSourceCode(_INSTRUMENT)
        script.setInjectionPoint(QWebEngineScript.DocumentCreation)
        script.setWorldId(QWebEngineScript.MainWorld)
        page.scripts().insert(script)
        view.setAttribute(Qt.WA_DontShowOnScreen, True)
        # 与 desktop/main_window.py 的 INITIAL_WINDOW_WIDTH 一致
        view.resize(1440, 900)
        view.show()
        keep.update(view=view, page=page, profile=profile)
        loaded = {"done": False}

        def got(raw) -> None:
            data = json.loads(raw) if raw else {"fatal": "no report"}
            data["errors"] = list(data.get("errors") or []) + console_errors
            results[scenario["name"]] = data
            view.close()
            page.deleteLater()
            view.deleteLater()
            QTimer.singleShot(300, run_next)

        def on_load(_ok: bool) -> None:
            if loaded["done"]:
                return
            loaded["done"] = True
            if scenario.get("escape_ms"):
                QTimer.singleShot(scenario["escape_ms"], lambda: page.runJavaScript(_ESCAPE))
            QTimer.singleShot(scenario["report_ms"], lambda: page.runJavaScript(_REPORT, got))

        page.loadFinished.connect(on_load)
        page.setUrl(QUrl(f"http://127.0.0.1:{port}/{scenario['query']}"))

    QTimer.singleShot(0, run_next)
    QTimer.singleShot(90000, app.quit)
    app.exec_()
    httpd.shutdown()
    if len(results) != len(_SCENARIOS):
        print(f"probe timeout: {sorted(results)}", file=sys.stderr)
        return 2
    print(json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
