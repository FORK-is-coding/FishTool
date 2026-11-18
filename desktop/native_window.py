"""Optional Windows system-control UI backed by the existing HTTP API."""
from __future__ import annotations

import base64
import io
import json
import threading
import webbrowser

import wx

from desktop.native_backend import NativeBackend
from core.logger import get_logger


logger = get_logger(__name__)


class NativeWindow(wx.Frame):
    def __init__(self, backend):
        super().__init__(None, title="FishTool — Windows 系统控件", size=(1060, 760))
        self.backend = backend
        self.closing = False
        self.qr_key = None
        self.qr_pending = False
        self.tasks = {}
        panel = wx.Panel(self)
        layout = wx.BoxSizer(wx.VERTICAL)
        toolbar = wx.BoxSizer(wx.HORIZONTAL)
        toolbar.Add(wx.StaticText(panel, label=f"本机服务：{backend.url}"), 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 8)
        full = wx.Button(panel, label="打开完整版网页")
        full.Bind(wx.EVT_BUTTON, lambda event: webbrowser.open(backend.url))
        toolbar.Add(full, 0, wx.ALL, 8)
        layout.Add(toolbar, 0, wx.EXPAND)
        self.book = wx.Notebook(panel)
        layout.Add(self.book, 1, wx.EXPAND | wx.ALL, 8)
        panel.SetSizer(layout)
        self.CreateStatusBar()
        self.SetStatusText("就绪；高级流程可打开完整版网页")
        self.build_pages()
        for index in range(self.book.GetPageCount()):
            self.book.GetPage(index).Layout()
        panel.Layout()
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.poll_qr, self.timer)
        self.timer.Start(3000)
        self.Centre()

    def page(self, title, description):
        panel = wx.Panel(self.book)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(panel, label=description), 0, wx.ALL, 10)
        panel.SetSizer(layout)
        self.book.AddPage(panel, title)
        return panel

    def field(self, panel, label, value="", password=False):
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(panel, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        control = wx.TextCtrl(panel, value=str(value), style=wx.TE_PASSWORD if password else 0)
        row.Add(control, 1)
        panel.GetSizer().Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        return control

    def output(self, panel):
        control = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        panel.GetSizer().Add(control, 1, wx.EXPAND | wx.ALL, 10)
        return control

    def button(self, panel, label, action):
        control = wx.Button(panel, label=label)
        def clicked(event):
            try:
                action(control)
            except (ValueError, TypeError) as exc:
                wx.MessageBox(str(exc), "请检查输入", wx.OK | wx.ICON_INFORMATION, self)
        control.Bind(wx.EVT_BUTTON, clicked)
        panel.GetSizer().Add(control, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        return control

    def submit(self, button, output, method, path, payload=None, callback=None):
        button.Disable()
        self.SetStatusText("正在处理…")
        def worker():
            try:
                result = self.backend.request(method, path, payload)
                error = None
            except Exception as exc:
                logger.exception("原生界面 API 请求失败：%s %s", method, path)
                result, error = None, str(exc)
            def finish():
                if self.closing:
                    return
                button.Enable()
                self.SetStatusText("请求失败" if error else "完成")
                if error:
                    output.SetValue("请求失败：" + error + "\n可在日志页查看详细原因。")
                else:
                    output.SetValue(json.dumps(result, ensure_ascii=False, indent=2))
                    if callback:
                        callback(result)
            wx.CallAfter(finish)
        threading.Thread(target=worker, daemon=True).start()

    def build_pages(self):
        p = self.page("扫码登录", "使用哔哩哔哩手机客户端扫码；二维码生成后自动查询登录结果。")
        self.qr_image = wx.StaticBitmap(p, bitmap=wx.Bitmap.FromBuffer(220, 220, b"\xff" * (220 * 220 * 3)))
        p.GetSizer().Add(self.qr_image, 0, wx.ALL, 10)
        self.qr_output = self.output(p)
        self.qr_button = self.button(p, "生成登录二维码", lambda b: self.submit(
            b, self.qr_output, "GET", "/api/auth/qrcode", callback=self.show_qr))
        self.button(p, "刷新登录状态", lambda b: self.submit(b, self.qr_output, "GET", "/api/auth/status"))

        p = self.page("热点采集", "采集所选分区的热点视频；查看进度可读取后台采集状态。")
        tid = self.field(p, "分区 ID", 4)
        limit = self.field(p, "视频数量（1–200）", 20)
        out = self.output(p)
        self.button(p, "开始采集", lambda b, out=out: self.submit(b, out, "POST",
            f"/api/hotspot/collect?tid={int(tid.GetValue())}&limit={int(limit.GetValue())}"))
        self.button(p, "查看采集进度", lambda b, out=out: self.submit(b, out, "GET", "/api/hotspot/collect/progress"))

        p = self.page("评论监控", "输入视频 BV 号或链接，执行评论监控；常驻策略可在完整版设置。")
        target = self.field(p, "视频 BV 号或链接")
        out = self.output(p)
        self.button(p, "开始评论监控", lambda b, out=out: self.submit(b, out, "POST", "/api/comment/monitor",
            {"bvid": target.GetValue()}))
        self.button(p, "查看监控进度", lambda b, out=out: self.submit(b, out, "GET", "/api/comment/monitor/progress"))
        self.button(p, "查看预警", lambda b, out=out: self.submit(b, out, "GET", "/api/comment/alerts"))

        p = self.page("UP 主分析", "输入 UP 主 UID 或主页链接，提交分析后可查看任务结果。")
        uid = self.field(p, "UID 或主页链接")
        out = self.output(p)
        self.button(p, "开始分析", lambda b, out=out: self.submit(b, out, "POST", "/api/analysis/analyze-up/tasks",
            {"uid_or_url": uid.GetValue()}, lambda r: self.remember_task("analysis", r)))
        self.button(p, "查看分析结果", lambda b, out=out: self.task_result(b, out, "analysis", "/api/analysis/analyze-up/tasks/"))

        p = self.page("账号自诊", "输入自己的 B 站 UID，生成账号诊断。AI 分析需要先配置模型。")
        own_uid = self.field(p, "自己的 UID")
        out = self.output(p)
        self.button(p, "开始账号自诊", lambda b, out=out: self.submit(b, out, "POST", "/api/analysis/self-diagnosis",
            {"uid": int(own_uid.GetValue())}))

        p = self.page("评论区抽奖", "从视频或动态评论区随机抽取；同一 UID 默认只保留一次机会。")
        lottery_target = self.field(p, "BV 号或动态完整链接")
        count = self.field(p, "中奖人数（1–100）", 1)
        out = self.output(p)
        self.button(p, "开始抽奖", lambda b, out=out: self.submit(b, out, "POST", "/api/lottery/draw/tasks",
            {"target": lottery_target.GetValue(), "winner_count": int(count.GetValue())},
            lambda r: self.remember_task("lottery", r)))
        self.button(p, "查看抽奖结果", lambda b, out=out: self.task_result(b, out, "lottery", "/api/lottery/tasks/"))

        p = self.page("AI 配置", "密钥由现有服务加密保存在本地。留空的地址和模型不会修改已有设置。")
        api_base = self.field(p, "API 地址")
        model = self.field(p, "模型名称")
        key = self.field(p, "API Key", password=True)
        out = self.output(p)
        def save(b, out=out):
            payload = {"api_key": key.GetValue()}
            for name, ctrl in (("base_url", api_base), ("model", model)):
                if ctrl.GetValue().strip():
                    payload[name] = ctrl.GetValue().strip()
            self.submit(b, out, "POST", "/api/config/llm", payload, lambda r: key.SetValue(""))
        self.button(p, "保存 AI 配置", save)
        self.button(p, "查看 AI 配置状态", lambda b, out=out: self.submit(b, out, "GET", "/api/config/llm"))

        p = self.page("日志", "读取现有服务日志；日志、配置和数据库保存在程序所在目录。")
        out = self.output(p)
        self.button(p, "刷新日志", lambda b, out=out: self.submit(b, out, "GET", "/api/logs/?limit=100"))

    def remember_task(self, name, result):
        task_id = result.get("task_id") or (result.get("data") or {}).get("task_id")
        if task_id:
            self.tasks[name] = task_id

    def task_result(self, button, output, name, prefix):
        if name not in self.tasks:
            raise ValueError("请先提交任务")
        self.submit(button, output, "GET", prefix + self.tasks[name])

    def show_qr(self, result):
        raw = result.get("qrcode_base64", "").split(",")[-1]
        image = wx.Image(io.BytesIO(base64.b64decode(raw)), wx.BITMAP_TYPE_PNG)
        self.qr_image.SetBitmap(wx.Bitmap(image.Scale(220, 220)))
        self.qr_key = result.get("qrcode_key")
        self.qr_output.SetValue("请使用哔哩哔哩客户端扫码，等待手机确认。")

    def poll_qr(self, event):
        if not self.qr_key or self.qr_pending:
            return
        self.qr_pending = True
        key = self.qr_key
        def worker():
            try:
                result = self.backend.request("POST", "/api/auth/poll", {"qrcode_key": key})
            except Exception as exc:
                logger.exception("原生界面登录状态轮询失败")
                result = {"status": "error", "message": str(exc)}
            def finish():
                self.qr_pending = False
                if self.closing or self.qr_key != key:
                    return
                self.qr_output.SetValue(result.get("message") or result.get("status", "等待扫码"))
                if result.get("status") in ("confirmed", "expired", "error"):
                    self.qr_key = None
            wx.CallAfter(finish)
        threading.Thread(target=worker, daemon=True).start()

    def on_close(self, event):
        self.closing = True
        self.timer.Stop()
        self.Hide()
        self.Destroy()


def run_native(port=8000):
    app = wx.App(False)
    backend = NativeBackend(port)
    progress = wx.ProgressDialog("FishTool", "正在启动本机服务…", style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE)
    errors = []
    ready = threading.Event()
    def start():
        try:
            backend.start()
        except BaseException as exc:
            logger.exception("原生界面本机服务启动失败")
            errors.append(str(exc))
        finally:
            ready.set()
    threading.Thread(target=start, daemon=True).start()
    try:
        while not ready.wait(0.05):
            progress.Pulse()
            wx.Yield()
        progress.Destroy()
        if errors:
            wx.MessageBox("启动失败：" + errors[0], "FishTool", wx.OK | wx.ICON_ERROR)
            return 1
        window = NativeWindow(backend)
        window.Show()
        app.MainLoop()
        return 0
    finally:
        backend.stop()
