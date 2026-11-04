"""聊天指令的业务逻辑：调用引擎并把结果格式化成文本。

不 import astrbot：logger 由 main.py 传入，保持可在测试里直接构造。
只依赖 ``errors``，不依赖 httpx，单测可用假 client。
"""
from __future__ import annotations

import re
import time

from .constants import ALERT_LEVELS
from .errors import CommandError, EngineUnavailable

BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
UID_RE = re.compile(r"^\d{1,20}$")
#: 告警查询的级别参数；all 表示不过滤。
LEVEL_CHOICES = ("all",) + ALERT_LEVELS
#: /auth/status 每次都用 Cookie 实测请求 B 站，结果缓存这么久。
LOGIN_CACHE_S = 60
#: 单条评论原文在消息里的最大长度。
TEXT_LIMIT = 200
#: 舆情摘要里最多列出的告警条数。
MAX_ALERT_LINES = 5
#: 采集策略（web/routers/comment.py 只接受 normal/full）。
STRATEGIES = ("normal", "full")
#: 常驻监控的操作。
RESIDENT_ACTIONS = ("状态", "开启", "暂停", "停止")
#: 一次指令最多指定的常驻监控目标数。
MAX_RESIDENT_TARGETS = 20

_LEVEL_LABEL = {"high": "高", "medium": "中", "low": "低"}
_STATE_LABEL = {
    "stopped": "已停止",
    "preparing": "准备运行环境",
    "starting": "启动中",
    "running": "运行中",
    "crashed": "已崩溃",
    "error": "启动失败",
}


def clamp(value, low: int, high: int, default: int) -> int:
    """把数量参数收敛到 [low, high]；非法值用 default。"""
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def shorten(text, limit: int = TEXT_LIMIT) -> str:
    """压成一行并截断，避免长评论刷屏。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _short_time(value) -> str:
    """ISO 时间串截到分钟：``2026-11-04T10:20:30`` → ``2026-11-04 10:20``；空值返回空串。"""
    return str(value or "")[:16].replace("T", " ")


def _data(body):
    """取引擎统一响应里的 data；非 dict 时返回空 dict。"""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return {}


class FishToolCommands:
    """聊天指令的只读查询与格式化。写操作在后续步骤加入。"""

    def __init__(self, supervisor, client, ecfg: dict, logger, *, clock=time.monotonic):
        self.sup = supervisor
        self.client = client
        self.ecfg = ecfg  # engine 段：超时只从这里读
        self.log = logger
        self._clock = clock
        self._login: tuple[str, float, dict] | None = None  # (sup.token, 取到的时间, 结果)

    # ------------------------------------------------------------------ engine access
    def _ensure_running(self) -> None:
        state = getattr(self.sup, "state", "stopped")
        if state == "running":
            return
        label = _STATE_LABEL.get(state, state)
        msg = f"FishTool 引擎未运行（当前：{label}）"
        if state == "crashed":
            msg += "，正在自动重启，请稍后再试"
        elif state in ("preparing", "starting"):
            msg += "，请稍后再试"
        else:
            msg += "，请到 AstrBot 面板 → 插件 → FishTool 启动引擎"
        raise CommandError(msg)

    async def _call(self, method: str, path: str, *, long: bool = False, **kw):
        self._ensure_running()
        key = "long_request_timeout_s" if long else "request_timeout_s"
        try:
            timeout = float(self.ecfg.get(key) or 120)
        except (TypeError, ValueError):
            timeout = 120.0
        try:
            status, body = await self.client.call(method, path, timeout=timeout, **kw)
        except EngineUnavailable as exc:
            raise CommandError(f"引擎不可用：{exc}") from exc
        if status >= 400:
            detail = body.get("detail") if isinstance(body, dict) else body
            raise CommandError(f"引擎返回 {status}：{shorten(detail, 300) or '无详情'}")
        return body

    async def login_status(self) -> dict:
        """B 站登录态；60 秒内复用结果，引擎重启（token 变化）后作废。"""
        now = self._clock()
        token = getattr(self.sup, "token", "")
        cached = self._login
        if cached and cached[0] == token and now - cached[1] < LOGIN_CACHE_S:
            return cached[2]
        body = await self._call("GET", "/api/auth/status")
        body = body if isinstance(body, dict) else {}
        self._login = (token, now, body)
        return body

    # ------------------------------------------------------------------ validation
    @staticmethod
    def check_bvid(bvid: str) -> str:
        bvid = (bvid or "").strip()
        if not BVID_RE.match(bvid):
            raise CommandError(f"BV 号格式不对：{shorten(bvid, 40) or '（空）'}")
        return bvid

    @staticmethod
    def check_uid(uid: str) -> str:
        uid = (uid or "").strip()
        if not UID_RE.match(uid):
            raise CommandError(f"UID 应为纯数字：{shorten(uid, 40) or '（空）'}")
        return uid

    @staticmethod
    def check_level(level: str) -> str:
        level = (level or "all").strip().lower()
        if level not in LEVEL_CHOICES:
            raise CommandError(f"告警级别只能是 {'/'.join(LEVEL_CHOICES)}：{shorten(level, 20)}")
        return level

    # ------------------------------------------------------------------ read-only commands
    async def status_text(self, font_label: str) -> str:
        lines = ["【FishTool 状态】"]
        state = getattr(self.sup, "state", "stopped")
        lines.append(f"引擎：{_STATE_LABEL.get(state, state)}")
        if state != "running":
            err = getattr(self.sup, "last_error", "")
            if err:
                lines.append(f"最近错误：{shorten(err, 120)}")
            lines.append(f"自诊报告字体：{font_label}")
            return "\n".join(lines)

        lines.append(f"B站登录：{await self._login_line()}")
        lines.append(f"常驻监控：{await self._resident_line()}")
        lines.append(f"自诊报告字体：{font_label}")
        return "\n".join(lines)

    async def _login_line(self) -> str:
        try:
            body = await self.login_status()
        except CommandError as exc:
            return f"查询失败（{exc}）"
        if body.get("logged_in"):
            user = body.get("user") if isinstance(body.get("user"), dict) else {}
            name = shorten(user.get("username") or "", 30)
            uid = user.get("uid")
            who = f"{name}（UID {uid}）" if name else f"UID {uid}"
            return f"已登录 {who}"
        if body.get("invalid"):
            return "Cookie 已失效，请到面板扫码重新登录"
        return "未登录，请到面板扫码登录"

    async def _resident_line(self) -> str:
        try:
            snap = _data(await self._call("GET", "/api/comment/resident/status"))
        except CommandError as exc:
            return f"查询失败（{exc}）"
        status = snap.get("status") or "stopped"
        label = {"running": "运行中", "paused": "已暂停", "stopped": "已停止"}.get(status, status)
        targets = snap.get("target_bvids") or []
        parts = [label, f"目标 {len(targets)} 个"]
        if snap.get("last_collect_at"):
            parts.append(f"最近采集 {_short_time(snap['last_collect_at'])}")
        if snap.get("last_error"):
            parts.append(f"最近错误：{shorten(snap['last_error'], 80)}")
        return "，".join(parts)

    async def alerts_text(self, limit, level: str = "all") -> str:
        level = self.check_level(level)
        limit = clamp(limit, 1, 20, 5)
        query = {"is_read": "false", "limit": limit}
        if level != "all":
            query["level"] = level
        body = await self._call("GET", "/api/comment/alerts", query=query)
        alerts = body.get("alerts") if isinstance(body, dict) else None
        alerts = [a for a in (alerts or []) if isinstance(a, dict)]
        scope = "" if level == "all" else f"（{_LEVEL_LABEL[level]}级）"
        if not alerts:
            return f"暂无未读告警{scope}"
        lines = [f"【未读告警{scope}】最近 {len(alerts)} 条"]
        for index, alert in enumerate(alerts, 1):
            lines.append(f"{index}. {self.format_alert(alert)}")
        return "\n".join(lines)

    async def hotspot_text(self, limit) -> str:
        limit = clamp(limit, 1, 30, 10)
        snap = _data(await self._call("GET", "/api/hotspot/discovery/latest")).get("snapshot")
        if not isinstance(snap, dict):
            return "尚未跑过热点发现轮，请先在面板开启热点发现或等待下一轮采样"
        keywords = [k for k in (snap.get("keywords") or []) if isinstance(k, dict)]
        videos = [v for v in (snap.get("videos") or []) if isinstance(v, dict)]
        keywords.sort(key=lambda k: (k.get("rank") is None, k.get("rank") or 0))
        lines = ["【最近一轮热点】"]
        captured = snap.get("captured_epoch_s")
        if isinstance(captured, (int, float)) and captured > 0:
            lines[0] += " " + time.strftime("%m-%d %H:%M", time.localtime(captured))
        if keywords:
            lines.append("热搜词：")
            for index, item in enumerate(keywords[:limit], 1):
                name = shorten(item.get("show_name") or item.get("keyword") or "", 30)
                heat = item.get("heat_score")
                lines.append(f"{index}. {name}" + (f"（热度 {heat}）" if isinstance(heat, int) else ""))
        if videos:
            lines.append("热门视频：")
            for index, item in enumerate(videos[:limit], 1):
                lines.append(f"{index}. {self._video_line(item)}")
        if len(lines) == 1:
            lines.append("本轮没有可展示的热搜词或视频")
        return "\n".join(lines)

    # ------------------------------------------------------------------ write commands
    async def monitor_video_text(self, bvid: str, strategy: str = "normal") -> str:
        """跑一次单视频评论舆情（/comment/monitor，长请求），返回摘要。"""
        bvid = self.check_bvid(bvid)
        strategy = self.check_strategy(strategy)
        body = await self._call(
            "POST", "/api/comment/monitor", long=True,
            json={"bvid": bvid, "strategy": strategy},
        )
        data = _data(body)
        lines = [f"【舆情分析】{bvid}（{'全量' if strategy == 'full' else '快速'}）"]
        if not data.get("success", True):
            lines.append(f"未完成：{shorten(data.get('error') or '未采集到评论', 160)}")
            return "\n".join(lines)
        collected = data.get("collected_count")
        processed = data.get("processed_count")
        lines.append(f"采集 {collected} 条，去重后 {processed} 条")
        sentiment = data.get("sentiment_result") if isinstance(data.get("sentiment_result"), dict) else None
        if sentiment:
            parts = []
            for key, label in (("positive_ratio", "正面"), ("negative_ratio", "负面"),
                               ("neutral_ratio", "中性"), ("risk_ratio", "风险")):
                value = sentiment.get(key)
                if isinstance(value, (int, float)):
                    parts.append(f"{label} {value:.0%}")
            if parts:
                lines.append("情感：" + "，".join(parts))
        alerts = [a for a in (data.get("alerts") or []) if isinstance(a, dict)]
        if alerts:
            lines.append(f"触发 {len(alerts)} 条告警：")
            for alert in alerts[:MAX_ALERT_LINES]:
                lines.append(f"- {self.format_alert({**alert, 'bvid': ''})}")
            if len(alerts) > MAX_ALERT_LINES:
                lines.append(f"……另有 {len(alerts) - MAX_ALERT_LINES} 条，见面板")
        else:
            lines.append("未触发告警")
        if data.get("warning"):
            lines.append(f"注意：{shorten(data['warning'], 160)}")
        return "\n".join(lines)

    async def resident_text(self, action: str, bvids: list[str] | None = None) -> str:
        """常驻监控：状态 / 开启 [BV 列表] / 暂停 / 停止。"""
        action = (action or "状态").strip()
        if action not in RESIDENT_ACTIONS:
            raise CommandError(f"监控操作只能是 {'/'.join(RESIDENT_ACTIONS)}：{shorten(action, 20)}")
        if action == "状态":
            return f"常驻监控：{await self._resident_line()}"
        if action == "开启":
            payload = {"bvids": [self.check_bvid(b) for b in bvids]} if bvids else {}
            snap = _data(await self._call("POST", "/api/comment/resident/enable", json=payload))
        else:
            path = "pause" if action == "暂停" else "stop"
            snap = _data(await self._call("POST", f"/api/comment/resident/{path}"))
        targets = snap.get("target_bvids") or []
        text = f"常驻监控已{action}，目标 {len(targets)} 个"
        if action == "开启" and not targets:
            text += "（没有目标视频，请用 /fishtool 监控 开启 BV号,BV号 指定）"
        return text

    @staticmethod
    def parse_bvid_list(raw: str) -> list[str]:
        """``BV1,BV2`` / ``BV1，BV2`` → 去重后的列表；空串返回空列表。"""
        parts = re.split(r"[,，;；\s]+", raw or "")
        result: list[str] = []
        for part in parts:
            if part and part not in result:
                result.append(part)
        if len(result) > MAX_RESIDENT_TARGETS:
            raise CommandError(f"一次最多指定 {MAX_RESIDENT_TARGETS} 个视频")
        return result

    @staticmethod
    def check_strategy(strategy: str) -> str:
        strategy = (strategy or "normal").strip().lower()
        if strategy not in STRATEGIES:
            raise CommandError(f"采集策略只能是 {'/'.join(STRATEGIES)}：{shorten(strategy, 20)}")
        return strategy

    @staticmethod
    def _video_line(item: dict) -> str:
        bvid = item.get("bvid") or ""
        details = item.get("source_details") if isinstance(item.get("source_details"), list) else []
        view = next(
            (d.get("view") for d in details if isinstance(d, dict) and isinstance(d.get("view"), int)),
            None,
        )
        return f"{bvid}" + (f"（播放 {view:,}）" if view is not None else "")

    @staticmethod
    def format_alert(data: dict) -> str:
        """把一条告警（引擎推送或 /comment/alerts 返回）格式化成一行。"""
        level = _LEVEL_LABEL.get(str(data.get("level") or ""), str(data.get("level") or "?"))
        # 推送的告警带 bvid（modules/comment/_alerts.py 回调）；/comment/alerts 查询结果只有 video_id
        video = data.get("bvid") or (f"视频#{data['video_id']}" if data.get("video_id") else "")
        when = _short_time(data.get("created_at"))
        head = f"[{level}]"
        if video:
            head += f" {video}"
        if when:
            head += f" {when}"
        return f"{head} {shorten(data.get('message'))}"
