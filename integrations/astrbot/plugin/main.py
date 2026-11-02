"""FishTool AstrBot 插件入口。

插件只做三件事：管理 FishTool 引擎子进程、把插件视图的请求代理到引擎、中继引擎事件。
不 import FishTool 的任何模块（引擎在独立 venv 的子进程中运行，见 engine_launcher/launcher.py）。
"""
from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, file_response, json_response, request, stream_response
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .fishtool_adapter.client import EngineClient
from .fishtool_adapter.constants import PLUGIN_NAME, engine_cfg
from .fishtool_adapter.events import EventRelay
from .fishtool_adapter.exports import ExportStore, safe_filename
from .fishtool_adapter.paths import DataLayout
from .fishtool_adapter.proxy import EngineProxy, envelope
from .fishtool_adapter.runtime_env import EngineEnvError, ensure_engine_env, remove_engine_env
from .fishtool_adapter.supervisor import EngineSupervisor

# 视图资源带 X-Content-Type-Options: nosniff，MIME 由 mimetypes 判断；部分 Windows 注册表把 .js
# 映射成 text/plain，浏览器会拒绝执行脚本导致白屏。
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")

PLUGIN_DIR = Path(__file__).resolve().parent
#: POST 隧道标记：bridge 只有 GET/POST 且 POST 不能带 query，bridge-shim.js 用它还原 PUT/PATCH/DELETE。
TUNNEL_KEY = "__fishtool_tunnel"


def _unloaded() -> dict:
    return envelope(503, {"detail": "plugin unloaded", "error_code": "plugin_unloaded"})


class FishToolPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.cfg = config if config is not None else {}
        self.ecfg = engine_cfg(self.cfg.get("engine") if hasattr(self.cfg, "get") else None)
        self._closed = False
        self._boot_task: asyncio.Task | None = None
        self.layout = DataLayout(
            root=Path(StarTools.get_data_dir(PLUGIN_NAME)),
            venv_root=Path(get_astrbot_data_path()) / "fishtool_runtime",
        )
        self.layout.ensure()
        self.sup = EngineSupervisor(self.layout, PLUGIN_DIR, self.ecfg, logger)
        self.client = EngineClient(self.sup)
        self.proxy = EngineProxy(self.sup, self.client, self.ecfg, logger)
        self.relay = EventRelay(self.sup, logger)
        self.exports = ExportStore(self.layout.exports_dir, logger)

        p = f"/{PLUGIN_NAME}"  # 路由必须带插件名前缀（plugin-pages.md）
        reg = context.register_web_api
        reg(f"{p}/_status", self.api_status, ["GET"], "FishTool 引擎状态")
        reg(f"{p}/_engine/start", self.api_start, ["POST"], "启动 FishTool 引擎")
        reg(f"{p}/_engine/stop", self.api_stop, ["POST"], "停止 FishTool 引擎")
        reg(f"{p}/_engine/restart", self.api_restart, ["POST"], "重启 FishTool 引擎")
        reg(f"{p}/_engine/clean-venv", self.api_clean_venv, ["POST"], "清理 FishTool 引擎运行环境")
        reg(f"{p}/_engine/log", self.api_engine_log, ["GET"], "FishTool 引擎启动日志")
        reg(f"{p}/_events", self.api_events, ["GET"], "FishTool 事件 SSE")
        reg(f"{p}/_export/report", self.api_export_report, ["POST"], "保存自诊报告 HTML")
        reg(f"{p}/_export/text", self.api_export_text, ["POST"], "保存文本导出")
        reg(f"{p}/_export/file", self.api_export_file, ["GET"], "下载导出文件")
        reg(f"{p}/api/<path:sub>", self.api_proxy, ["GET", "POST", "PUT", "PATCH", "DELETE"], "FishTool 引擎 API 代理")

    # ------------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.relay.start()
        if self.ecfg.get("autostart", True):
            self._spawn_boot()

    def _spawn_boot(self) -> bool:
        """后台启动引擎；重载/更新/保存配置都会 terminate 当前实例，所以任务必须被跟踪。"""
        if self._closed or self.sup.final:
            return False
        if self._boot_task and not self._boot_task.done():
            return False
        self._boot_task = asyncio.create_task(self._boot(), name="fishtool-engine-boot")
        return True

    def _set_progress(self, msg: str) -> None:
        self.sup.progress = msg

    async def _boot(self) -> None:
        try:
            self.sup.state = "preparing"
            self.sup.last_error = ""
            py = await ensure_engine_env(
                self.layout,
                PLUGIN_DIR,
                str(self.ecfg.get("python") or ""),
                str(self.ecfg.get("pip_index_url") or ""),
                on_progress=self._set_progress,
            )
            await self.sup.start(str(py))
        except asyncio.CancelledError:
            raise
        except EngineEnvError as exc:
            self.sup.state, self.sup.last_error, self.sup.progress = "error", str(exc), ""
            logger.error(f"[fishtool] engine env failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - 启动失败要在面板显示原因
            self.sup.state, self.sup.last_error, self.sup.progress = "error", str(exc), ""
            logger.error(f"[fishtool] engine boot failed: {exc}", exc_info=True)

    async def _cancel_boot(self) -> None:
        task = self._boot_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.debug("[fishtool] boot task cancelled")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[fishtool] boot task ended with error: {exc}")

    async def terminate(self) -> None:
        """停用/重载/卸载/AstrBot 正常停止时调用。"""
        self._closed = True
        self.proxy.closed = True
        await self._cancel_boot()
        await self.relay.stop()
        await self.sup.stop(final=True)
        await self.client.aclose()

    # ------------------------------------------------------------------ helpers
    def _engine_version(self) -> str:
        path = PLUGIN_DIR / "engine" / "VERSION"
        try:
            return path.read_text("utf-8").strip()
        except OSError:
            return "unknown"

    # ------------------------------------------------------------------ web api
    async def api_status(self):
        if self._closed:
            return _unloaded()
        return json_response({
            **self.sup.snapshot(),
            "events_connected": self.relay.connected,
            "engine_version": self._engine_version(),
            "data_root": str(self.layout.root),
            "venv": str(self.layout.venv_dir),
        })

    async def api_start(self):
        if self._closed:
            return _unloaded()
        if self.sup.state == "running":
            return json_response({"accepted": False, "state": "running"})
        return json_response({"accepted": self._spawn_boot(), "state": self.sup.state})

    async def api_stop(self):
        if self._closed:
            return _unloaded()
        await self._cancel_boot()
        await self.sup.stop()
        return json_response({"stopped": True})

    async def api_restart(self):
        if self._closed:
            return _unloaded()
        await self._cancel_boot()
        await self.sup.stop()
        return json_response({"accepted": self._spawn_boot()})

    async def api_clean_venv(self):
        if self._closed:
            return _unloaded()
        await self._cancel_boot()
        await self.sup.stop()
        removed = await asyncio.to_thread(remove_engine_env, self.layout)
        return json_response({"removed": removed})

    async def api_engine_log(self):
        if self._closed:
            return _unloaded()
        parts = []
        for path in (self.layout.logs_dir / "engine_boot.log", self.layout.engine_log):
            try:
                data = path.read_bytes()[-20000:]
            except OSError:
                continue
            parts.append(f"===== {path.name} =====\n" + data.decode("utf-8", errors="replace"))
        return json_response({"text": "\n".join(parts)})

    async def api_events(self):
        if self._closed:
            return error_response("plugin unloaded", status_code=503)
        return stream_response(
            self.relay.sse(),
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def api_export_report(self):
        if self._closed:
            return _unloaded()
        body = await request.json(default={}) or {}
        html = body.get("html")
        if not isinstance(html, str) or not html.strip():
            return json_response({"detail": "报告内容为空"})
        uid = safe_filename(str(body.get("uid") or ""), "report")
        name = f"diagnosis_report_{uid}.html"
        try:
            file_id, name = self.exports.save(html.encode("utf-8"), name, ".html")
        except ValueError as exc:
            return json_response({"detail": str(exc)})
        return json_response({"file_id": file_id, "filename": name})

    async def api_export_text(self):
        if self._closed:
            return _unloaded()
        body = await request.json(default={}) or {}
        text = body.get("text")
        if not isinstance(text, str):
            return json_response({"detail": "导出内容为空"})
        name = safe_filename(str(body.get("filename") or ""), "export.txt")
        if not name.endswith(".txt"):
            name += ".txt"
        try:
            file_id, name = self.exports.save(text.encode("utf-8"), name, ".txt")
        except ValueError as exc:
            return json_response({"detail": str(exc)})
        return json_response({"file_id": file_id, "filename": name})

    async def api_export_file(self):
        if self._closed:
            return error_response("plugin unloaded", status_code=503)
        found = self.exports.resolve(str(request.query.get("id") or ""))
        if not found:
            return error_response("文件不存在或已过期", status_code=404)
        path, name = found
        ctype = "text/html; charset=utf-8" if path.suffix == ".html" else "text/plain; charset=utf-8"
        return file_response(path, filename=name, content_type=ctype)

    async def api_proxy(self, sub: str):
        if self._closed:
            return _unloaded()
        method = request.method.upper()
        query = dict(request.query.items())
        body = None
        if method != "GET":
            body = await request.json(default=None)
            if method == "POST" and isinstance(body, dict) and body.get(TUNNEL_KEY):
                method = str(body.get("method") or "POST").upper()
                query = body.get("query") if isinstance(body.get("query"), dict) else {}
                body = body.get("body")
        return await self.proxy.handle(sub, method=method, query=query, body_json=body, username=request.username)
