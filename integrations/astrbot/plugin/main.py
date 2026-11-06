"""FishTool AstrBot 插件入口。

插件只做三件事：管理 FishTool 引擎子进程、把插件视图的请求代理到引擎、中继引擎事件。
不 import FishTool 的任何模块（引擎在独立 venv 的子进程中运行，见 engine_launcher/launcher.py）。
"""
from __future__ import annotations

import asyncio
import mimetypes
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Node, Nodes, Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, file_response, json_response, request, stream_response
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .fishtool_adapter import gate as G
from .fishtool_adapter.client import EngineClient
from .fishtool_adapter.commands import FishToolCommands
from .fishtool_adapter.constants import PLUGIN_NAME, chat_cfg, engine_cfg
from .fishtool_adapter.errors import CommandError
from .fishtool_adapter.events import EventRelay
from .fishtool_adapter.exports import ExportStore, content_type_for, safe_filename
from .fishtool_adapter.jobs import JobRunner
from .fishtool_adapter.notifier import SEND_GAP_S, AlertNotifier
from .fishtool_adapter.replies import Reply
from .fishtool_adapter.paths import DataLayout
from .fishtool_adapter.proxy import EngineProxy, envelope
from .fishtool_adapter.runtime_env import EngineEnvError, ensure_engine_env, remove_engine_env
from .fishtool_adapter.subscriptions import MAX_SUBSCRIPTIONS, SubscriptionStore
from .fishtool_adapter.supervisor import EngineSupervisor

# 视图资源带 X-Content-Type-Options: nosniff，MIME 由 mimetypes 判断；部分 Windows 注册表把 .js
# 映射成 text/plain，浏览器会拒绝执行脚本导致白屏。
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")

PLUGIN_DIR = Path(__file__).resolve().parent

#: 自诊摘要末尾固定附的一句：协议端先返回成功、再异步下载失败时插件感知不到，只能提前说明。
FILE_HINT = "PDF 报告随后发送；如果没收到文件，见插件 README『聊天指令 → 收不到文件』。"
#: callback_api_base 是 AstrBot 的系统设置，不在插件配置里；面板只显示中文名，所以提示里写出完整位置。
FILE_FALLBACK_TEXT = (
    "PDF 发送失败：协议端读不到插件生成的文件。AstrBot 与 NapCat 分开部署时，请在 AstrBot 面板 → "
    "系统设置 → 常规 → 基础运行 →「对外可达的回调接口地址」（callback_api_base，不在插件配置里）"
    "填写 NapCat 能访问到的 AstrBot 地址，或把 AstrBot 的 data 目录以相同路径挂载到 NapCat。"
    "报告已保存在插件的 exports 目录，24 小时内有效。"
)
FONT_DEGRADED_HINT = "注意：未找到可用的中文字体，PDF 版式已降级。请在插件配置的『自诊报告字体』里填写 .ttf / .ttc 路径。"
#: POST 隧道标记：bridge 只有 GET/POST 且 POST 不能带 query，bridge-shim.js 用它还原 PUT/PATCH/DELETE。
TUNNEL_KEY = "__fishtool_tunnel"


def _unloaded() -> dict:
    return envelope(503, {"detail": "plugin unloaded", "error_code": "plugin_unloaded"})


class FishToolPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.cfg = config if config is not None else {}
        self.ecfg = engine_cfg(self.cfg.get("engine") if hasattr(self.cfg, "get") else None)
        self.chat = chat_cfg(self.cfg.get("chat") if hasattr(self.cfg, "get") else None)
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
        self.subs = SubscriptionStore(self.layout.subscriptions_file, logger)
        self.cmds = FishToolCommands(self.sup, self.client, self.ecfg, logger)
        self.gate = G.Gate(self.chat, is_subscribed=self.subs.contains)
        self.jobs = JobRunner(self._send_text, logger)
        self.notifier = AlertNotifier(
            self.chat,
            sessions=self.subs.list,
            send_batch=self._send_alert_batch,
            fetch_unread=self.cmds.unread_alerts,
            logger=logger,
        )

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
        if self.chat["enable"]:
            self.relay.on_alert.append(self.notifier.on_alert)
            self.relay.on_connection.append(self.notifier.on_connection)
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
                logger,
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
        # 后台指令任务与告警推送必须在关闭 HTTP client 之前取消，否则还在跑的任务会撞上已关闭的 client
        await self.jobs.close()
        await self.notifier.close()
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
        return file_response(path, filename=name, content_type=content_type_for(path))

    # ------------------------------------------------------------------ chat commands
    def _gate(self, event: AstrMessageEvent, name: str, level: str) -> str:
        if self._closed:
            return G.OFF
        return self.gate.check(
            name=name,
            level=level,
            umo=event.unified_msg_origin,
            is_admin=event.is_admin(),
            sender=str(event.get_sender_id() or ""),
        )

    @staticmethod
    def _denied_text(event: AstrMessageEvent) -> str:
        return G.DENIED_TEXT.format(sid=event.get_sender_id())

    @staticmethod
    def _reply_chain(reply, self_id: str = "") -> MessageChain:
        """Reply → 消息链：条目多或全文长时改用合并转发（方案 6.5），否则普通文本。纯字符串原样发送。"""
        if not isinstance(reply, Reply) or not reply.use_forward():
            return MessageChain().message(str(reply))
        uin = self_id or "0"
        return MessageChain([Nodes([Node(content=[Plain(text)], name="FishTool", uin=uin) for text in reply.nodes()])])

    async def _send_text(self, umo: str, text) -> bool:
        """主动推送到会话（text 可以是 Reply）；失败只记日志，由调用方决定是否退回。"""
        try:
            await self.context.send_message(umo, self._reply_chain(text, self.subs.self_id_for(umo)))
            return True
        except Exception as exc:  # noqa: BLE001 - 推送失败（平台断开、会话失效）不能影响其他任务
            logger.warning(f"[fishtool] 推送到 {umo} 失败: {exc}")
            return False

    async def _send_alert_batch(self, sessions: list[dict], alerts: list[dict], overflow: int) -> None:
        """一批告警推给所有订阅会话。1 条发普通文本，多条发合并转发（每条一个节点）。"""
        head = f"【FishTool 舆情告警】{len(alerts) + overflow} 条"
        tail = [f"……另有 {overflow} 条，用 /fishtool 告警 查看"] if overflow else []
        reply = Reply(head, [self.cmds.format_alert(alert) for alert in alerts], tail)
        for index, sub in enumerate(sessions):
            if index:
                await asyncio.sleep(SEND_GAP_S)
            # 推送：1 条发普通文本，多条就用合并转发（比指令回复的阈值更低，避免个人号连发被风控）
            if len(alerts) + overflow > 1:
                uin = sub.get("self_id") or "0"
                chain = MessageChain([Nodes([Node(content=[Plain(t)], name="FishTool", uin=uin) for t in reply.nodes()])])
            else:
                chain = MessageChain().message(reply.as_text())
            try:
                await self.context.send_message(sub["umo"], chain)
            except Exception as exc:  # noqa: BLE001 - 某个会话失效不影响其他订阅
                logger.warning(f"[fishtool] 告警推送到 {sub['umo']} 失败: {exc}")

    def _result(self, event: AstrMessageEvent, reply):
        """handler 里回复：长内容用合并转发，节点头像用机器人自己的 QQ 号。"""
        return event.chain_result(self._reply_chain(reply, str(event.get_self_id() or "")).chain)

    async def _safe(self, coro):
        """只把 CommandError 转成用户可读文字；其他异常交给调用方或框架兜底。"""
        try:
            return await coro
        except CommandError as exc:
            return str(exc)

    def _font_label(self) -> str:
        """`状态` 里显示的自诊报告字体（有缓存，反复调用只检查一次文件）。"""
        from .fishtool_adapter.report_pdf import font_label

        return font_label(self.chat, logger)

    def _render_diagnosis_pdf(self, html_text: str, data: dict) -> tuple[bytes, list[str]]:
        """同步：字体 → 图表 → PDF。放在 asyncio.to_thread 里跑，排版和 .ttc 解析会占用几秒 CPU。"""
        from .fishtool_adapter import report_charts, report_pdf

        fonts = report_pdf.resolve_fonts(self.chat, logger)
        charts = None
        if self.chat["report_charts"]:
            charts = report_charts.build_chart_flowables(
                data.get("self_data") or {}, data.get("benchmark"), report_pdf.body_width_pt(), fonts, logger,
            )
        notes = [FONT_DEGRADED_HINT] if fonts.degraded else []
        return report_pdf.build(html_text, fonts, charts=charts), notes

    async def _run_diagnosis(self, key: str, umo: str, uid: str) -> None:
        """自诊后台任务：解析 UID → 自诊 → 主题 HTML → PDF → 发摘要 → 发文件（失败退回提示）。"""
        from .fishtool_adapter.report_pdf import ReportPdfError, parse_meta, report_filename

        try:
            try:
                resolved = await self.cmds.resolve_uid(uid)
                html_text, data = await self.cmds.diagnosis(resolved)
                pdf_bytes, notes = await asyncio.to_thread(self._render_diagnosis_pdf, html_text, data)
                name = report_filename(parse_meta(html_text), time.strftime("%Y%m%d"))
                try:
                    _, saved = await asyncio.to_thread(self.exports.save_and_path, pdf_bytes, name, ".pdf")
                except ValueError as exc:  # ExportStore：超过 32 MB 上限
                    raise CommandError(str(exc)) from exc
            except (CommandError, ReportPdfError) as exc:
                await self._send_text(umo, f"自诊失败：{exc}")
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务必须兜底，否则用户收不到任何回复
            logger.error(f"[fishtool] {key} 执行失败: {exc}", exc_info=True)
            await self._send_text(umo, f"自诊失败（内部错误），详情见 AstrBot 日志：{type(exc).__name__}")
            return
        finally:
            self.jobs.release(key)

        if self._closed:
            return
        try:
            head = self.cmds.diagnosis_summary(data, resolved)
        except Exception as exc:  # noqa: BLE001 - 摘要只是附带信息，出错不能挡住 PDF
            logger.error(f"[fishtool] {key} 生成摘要失败: {exc}", exc_info=True)
            head = f"【账号自诊】UID {resolved}"
        await self._send_text(umo, "\n".join([head, *notes, FILE_HINT]))
        try:
            await self.context.send_message(umo, MessageChain([File(name=name, file=str(saved))]))
        except Exception as exc:  # noqa: BLE001 - 协议端读不到文件时退回文字提示
            logger.warning(f"[fishtool] 自诊 PDF 发送到 {umo} 失败: {exc}")
            await self._send_text(umo, FILE_FALLBACK_TEXT)

    @filter.command_group("fishtool", alias={"ft"})
    def fishtool(self):
        """FishTool B站运营工具箱"""

    @fishtool.command("状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """引擎、B站登录态、常驻监控与报告字体状态"""
        if self._gate(event, "status", G.PUBLIC) != G.OK:
            return
        yield event.plain_result(await self._safe(self.cmds.status_text(self._font_label())))

    # 数量参数声明成 str：AstrBot 按默认值类型转换参数（core/star/filter/command.py），声明成 int 时
    # 用户输错会收到框架的“参数 limit 类型错误”；这里统一交给 commands.clamp 收敛到默认值。
    @fishtool.command("热点")
    async def cmd_hotspot(self, event: AstrMessageEvent, limit: str = "10"):
        """最近一轮热点发现的热搜词和视频（数量 1~30）"""
        if self._gate(event, "hotspot", G.PUBLIC) != G.OK:
            return
        yield self._result(event, await self._safe(self.cmds.hotspot_text(limit)))

    @fishtool.command("告警")
    async def cmd_alerts(self, event: AstrMessageEvent, limit: str = "5", level: str = "all"):
        """最近未读告警（数量 1~20，级别 all/high/medium/low）"""
        gate = self._gate(event, "alerts", G.RESTRICTED)
        if gate == G.DENIED:
            yield event.plain_result(self._denied_text(event))
        if gate != G.OK:
            return
        yield self._result(event, await self._safe(self.cmds.alerts_text(limit, level)))

    @fishtool.command("舆情")
    async def cmd_monitor(self, event: AstrMessageEvent, bvid: str = "", strategy: str = "normal"):
        """后台跑一次评论舆情分析，完成后推送到本会话（仅管理员；策略 normal/full）"""
        gate = self._gate(event, "monitor", G.ADMIN)
        if gate == G.DENIED:
            yield event.plain_result(self._denied_text(event))
        if gate != G.OK:
            return
        if not bvid:
            yield event.plain_result("用法：/fishtool 舆情 <BV号> [normal|full]")
            return
        try:
            bvid = self.cmds.check_bvid(bvid)
            strategy = self.cmds.check_strategy(strategy)
        except CommandError as exc:
            yield event.plain_result(str(exc))
            return
        key = f"monitor:{bvid}"
        if not self.jobs.claim(key):
            yield event.plain_result(f"{bvid} 正在分析中，完成后会发到发起的会话")
            return
        task = self.jobs.start_text_job(key, event.unified_msg_origin, self.cmds.monitor_video_text(bvid, strategy))
        if task is None:
            self.jobs.release(key)
            return
        mode = "全量，评论多时可能要很久" if strategy == "full" else "快速，约 1 分钟"
        yield event.plain_result(f"已开始分析 {bvid}（{mode}），完成后发到这里")

    @fishtool.command("监控")
    async def cmd_resident(self, event: AstrMessageEvent, action: str = "状态", bvids: str = ""):
        """常驻监控：状态 / 开启 [BV号,BV号] / 暂停 / 停止（状态以外仅管理员）"""
        action = (action or "状态").strip()
        level = G.RESTRICTED if action == "状态" else G.ADMIN
        gate = self._gate(event, f"resident:{action}", level)
        if gate == G.DENIED:
            yield event.plain_result(self._denied_text(event))
        if gate != G.OK:
            return
        try:
            targets = self.cmds.parse_bvid_list(bvids) if action == "开启" else None
        except CommandError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(await self._safe(self.cmds.resident_text(action, targets)))

    @fishtool.command("订阅")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """把当前会话加入评论舆情告警推送名单（仅管理员）"""
        gate = self._gate(event, "subscribe", G.ADMIN)
        if gate == G.DENIED:
            yield event.plain_result(self._denied_text(event))
        if gate != G.OK:
            return
        try:
            result = self.subs.add(event.unified_msg_origin, str(event.get_self_id() or ""))
        except (OSError, ValueError) as exc:
            logger.warning(f"[fishtool] 保存推送订阅失败: {exc}")
            yield event.plain_result(f"订阅失败：{exc}")
            return
        if result == "full":
            yield event.plain_result(f"订阅会话已达上限（{MAX_SUBSCRIPTIONS} 个），请先在其他会话 /fishtool 退订")
            return
        level = {"high": "高", "medium": "中及以上", "low": "全部"}[self.chat["notify_min_level"]]
        verb = "已更新" if result == "updated" else "已订阅"
        yield event.plain_result(
            f"{verb}：本会话会收到评论舆情告警（级别：{level}，每 {self.chat['notify_merge_seconds']} 秒合并发送一次）"
        )

    @fishtool.command("退订")
    async def cmd_unsubscribe(self, event: AstrMessageEvent):
        """把当前会话移出告警推送名单（仅管理员）"""
        gate = self._gate(event, "unsubscribe", G.ADMIN)
        if gate == G.DENIED:
            yield event.plain_result(self._denied_text(event))
        if gate != G.OK:
            return
        try:
            removed = self.subs.remove(event.unified_msg_origin)
        except OSError as exc:
            logger.warning(f"[fishtool] 保存推送订阅失败: {exc}")
            yield event.plain_result(f"退订失败：{exc}")
            return
        yield event.plain_result("已退订，本会话不再收到告警推送" if removed else "本会话未订阅告警推送")

    @fishtool.command("自诊")
    async def cmd_diagnosis(self, event: AstrMessageEvent, uid: str = ""):
        """生成账号自诊报告并以 PDF 发到本会话（仅管理员；不填 UID 用 B站登录账号）"""
        gate = self._gate(event, "diag", G.ADMIN)
        if gate == G.DENIED:
            yield event.plain_result(self._denied_text(event))
        if gate != G.OK:
            return
        if uid:
            try:
                uid = self.cmds.check_uid(uid)
            except CommandError as exc:
                yield event.plain_result(str(exc))
                return
        # 不填 UID 时登录态检查（会请求 B 站）放进后台任务，“正在生成”能马上回复
        key = f"diag:{uid or 'self'}"
        if not self.jobs.claim(key):
            yield event.plain_result("这个账号的报告正在生成，完成后会发到发起的会话")
            return
        task = self.jobs.spawn(self._run_diagnosis(key, event.unified_msg_origin, uid), name=f"fishtool-{key}")
        if task is None:
            self.jobs.release(key)
            return
        yield event.plain_result("正在生成自诊报告（抓取账号数据 + AI 调研），约需 1～3 分钟，完成后发到这里")

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
