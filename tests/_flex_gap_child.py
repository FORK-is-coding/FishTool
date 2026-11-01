"""真实桌面内核探针（由 tests/test_frontend_flex_gap_fallback.py 以子进程启动）。

不作为 pytest 用例：文件名不以 test_ 开头。
与 _qtwebengine_compat_child.py 同一做法：offscreen 下构造 QWebEngineView 会访问违规，
因此显式使用系统原生平台 + WA_DontShowOnScreen；本地静态服务器托管 web/frontend，/api、/ws 一律 503。
输出一行 JSON：是否加了 no-flex-gap，以及抽样容器的相邻子项实测间距与期望值。
"""
from __future__ import annotations

import http.server
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


class _Handler(http.server.SimpleHTTPRequestHandler):
    """静态文件 + 后端接口一律 503：探针只关心前端布局。"""

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

# 抽样覆盖三种兜底写法：单行相邻 margin、可换行四周 margin + 负外边距、纵向 margin-top。
# 期望值取各容器原 gap 声明；同一行的相邻子项测水平间距，换行/纵向测垂直间距。
_PROBE = r"""
(async () => {
  const wait = ms => new Promise(r => setTimeout(r, ms));
  // 「#id!parent」表示取该 id 元素的父容器（没有独立类名的容器靠唯一子元素定位）。
  const samples = [
    ['hotspot', '#hotspot-collect-message!parent', 12, 'row'],      // 单行横向（可换行容器）
    ['hotspot', '#hotspot-watch-bvid!parent', 20, 'row'],           // 表单组：输入框/按钮同一行
    ['hotspot', '#hotspot-watch-bvid!parent', 20, 'column'],        // 表单组：label 独占一行后的行距
    ['comment', '.resident-monitor-controls', 12, 'row'],
    ['comment', '.resident-monitor-metrics', 12, 'row'],
    ['logs', '.logs-toolbar-row', 14, 'row'],
    ['usage', '.usage-card-stack', 20, 'column'],                   // 纵向容器
    ['welcome', '.logo', 10, 'row'],
  ];
  const out = { ua: navigator.userAgent, noFlexGapClass: document.documentElement.classList.contains('no-flex-gap'), samples: {} };
  for (const [page, selector, expected, axis] of samples) {
    navigateTo(page); await wait(80);
    const el = selector.endsWith('!parent')
      ? (document.querySelector(selector.slice(0, -7)) || {}).parentElement || null
      : document.querySelector(selector);
    const key = `${selector} ${axis}`;
    if (!el) { out.samples[key] = { expected, spacing: [], missing: true }; continue; }
    const kids = [...el.children].filter(k => k.offsetParent !== null && getComputedStyle(k).position !== 'absolute');
    // 按垂直区间重叠把子项分行（align-items:center 时同一行各子项顶边不同）。
    const rows = [];
    for (const k of kids) {
      const r = k.getBoundingClientRect();
      const row = rows[rows.length - 1];
      if (row && r.top < row.bottom && row.top < r.bottom) {
        row.items.push(r); row.top = Math.min(row.top, r.top); row.bottom = Math.max(row.bottom, r.bottom);
      } else {
        rows.push({ items: [r], top: r.top, bottom: r.bottom });
      }
    }
    const spacing = [];
    // 列距：同一行内相邻子项的水平间距。
    if (axis === 'row') rows.forEach(row => row.items.slice(1).forEach((b, i) => spacing.push(Math.round(b.left - row.items[i].right))));
    // 行距：上一行底边到下一行顶边（行内最高子项决定行盒，与原生 row-gap 的定义一致）。
    if (axis === 'column') rows.slice(1).forEach((row, i) => spacing.push(Math.round(row.top - rows[i].bottom)));
    out.samples[key] = { expected, spacing };
  }
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
    # 与 desktop/main_window.py 的 INITIAL_WINDOW_WIDTH 一致，按桌面默认窗口宽度测布局。
    view.resize(1440, 900)
    view.show()

    result: dict = {}

    def done(raw) -> None:
        result["raw"] = raw
        app.quit()

    def poll() -> None:
        page.runJavaScript("window.__probeResult || null", lambda r: done(r) if r else QTimer.singleShot(300, poll))

    def start_probe() -> None:
        page.runJavaScript(_PROBE)
        QTimer.singleShot(400, poll)

    def wait_ready() -> None:
        # 等前端脚本加载完（navigateTo 已定义）再跑探针；固定延时在新内核上偶发 ReferenceError。
        page.runJavaScript("typeof navigateTo === 'function'",
                           lambda ok: start_probe() if ok else QTimer.singleShot(200, wait_ready))

    def on_load(_ok: bool) -> None:
        QTimer.singleShot(300, wait_ready)

    page.loadFinished.connect(on_load)
    page.setUrl(QUrl(f"http://127.0.0.1:{port}/"))
    QTimer.singleShot(60000, app.quit)
    app.exec()
    httpd.shutdown()
    if "raw" not in result:
        print("probe timeout", file=sys.stderr)
        return 2
    print(result["raw"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
