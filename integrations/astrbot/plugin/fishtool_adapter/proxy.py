"""bridge → 引擎 HTTP 转发与响应信封。

AstrBot bridge 的实际行为（dashboard/src/views/PluginViewPage.vue:378-400）：

1. 响应 JSON 只要 ``status === "error"`` 就 reject —— FishTool ``/api/auth/poll`` 会返回
   ``{"success":true,"status":"error"}``，会被误判。
2. 成功时返回 ``response.data?.data ?? response.data`` —— 无条件剥掉 ``data`` 字段。
3. 只有 GET/POST，不能自定义请求头，POST 不能带 query。
4. HTTP 状态码与 FastAPI ``detail`` 不会传到 iframe。

对策：对所有引擎响应（含 4xx/5xx）都回 HTTP 200 +
``{"status":"ok","data":{"http_status":…, "body":…}}``；bridge 剥一层 data 后 iframe 拿到
``{http_status, body}``，bridge-shim.js 再还原成标准 Response。
插件 handler 绝不返回 HTTP 401：父页面 axios 遇到 401 会清掉面板 token 并跳登录页。
"""
from __future__ import annotations

import re

from .client import EngineUnavailable

SAFE_PATH = re.compile(r"^[A-Za-z0-9_\-./]{1,300}$")
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

#: 同步长请求（引擎在请求内完成全部工作才返回）。前缀匹配也会命中对应的异步 */tasks，长超时对异步接口无害。
LONG_PREFIXES = (
    "comment/monitor",
    "comment/collect",
    "analysis/self-diagnosis",
    "analysis/report-html",
    "analysis/export-report",
    "analysis/analyze-up",
    "analysis/category-top-ups",
    "analysis/benchmark/candidates",
    "lottery/quick-filter",
    "lottery/verify-winners",
    "hotspot/tag-cloud",
    "hotspot/topics/generate",
    "hotspot/activities",
)

#: 前端先请求这两个接口拿 token 再带 X-Local-Token 写请求；真实 token 不交给 iframe，转发时由 client 注入。
TOKEN_PATHS = {"analysis/benchmark/local-token", "comment/semantic/local-token"}

#: block_api_key_write 打开时，API Key 调用这些前缀的任意方法都会被拒。
CREDENTIAL_PREFIXES = ("auth/", "config/", "config", "analysis/benchmark/", "comment/semantic/")


def envelope(http_status: int, body) -> dict:
    """统一信封（handler 直接返回 dict，AstrBot 转成 HTTP 200 JSON）。"""
    return {"status": "ok", "data": {"http_status": int(http_status), "body": body}}


def is_safe_path(path: str) -> bool:
    if not SAFE_PATH.fullmatch(path or ""):
        return False
    parts = path.split("/")
    return not any(part in ("", ".", "..") for part in parts[:-1]) and parts[-1] not in (".", "..")


def is_long_request(path: str) -> bool:
    return path.startswith(LONG_PREFIXES)


def api_key_blocked(username: str | None, method: str, path: str) -> bool:
    """API Key 写保护：写方法、凭据类路径、local-token。"""
    if not (username or "").startswith("api_key:"):
        return False
    if method != "GET":
        return True
    return path.endswith("local-token") or path.startswith(CREDENTIAL_PREFIXES)


def _clean_query(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        if value is None:
            continue
        out[str(key)] = value if isinstance(value, (str, int, float, bool)) else str(value)
    return out


class EngineProxy:
    def __init__(self, supervisor, client, cfg: dict, logger):
        self.sup = supervisor
        self.client = client
        self.cfg = cfg
        self.log = logger
        self.closed = False

    async def handle(self, sub: str, *, method: str, query: dict, body_json, username: str | None) -> dict:
        """转发一次 bridge 请求。

        Args:
            sub: ``api/`` 之后的引擎路径（不含 ``/api/`` 前缀）。
            method: 浏览器侧 HTTP 方法（GET，或 POST 隧道 / 直接 PUT 等）。
            query: GET 的 query；POST 隧道里的 query。
            body_json: 请求体。
            username: AstrBot 认证主体（``api_key:<id>`` 表示 Open API Key）。
        """
        if self.closed:
            return envelope(503, {"detail": "plugin unloaded", "error_code": "plugin_unloaded"})
        if not is_safe_path(sub):
            return envelope(400, {"detail": "invalid path"})
        method = (method or "GET").upper()
        if method not in ALLOWED_METHODS:
            return envelope(405, {"detail": "method not allowed"})
        if self.cfg.get("block_api_key_write") and api_key_blocked(username, method, sub):
            return envelope(403, {"detail": "API Key 不允许调用此接口", "error_code": "api_key_write_blocked"})
        if self.sup.state != "running":
            return envelope(503, {
                "detail": f"FishTool 引擎未运行（{self.sup.state}）",
                "error_code": "engine_not_running",
                "engine_state": self.sup.state,
                "engine_error": self.sup.last_error[-500:] if self.sup.last_error else "",
            })
        if sub in TOKEN_PATHS and method == "GET":
            return envelope(200, {"success": True, "data": {"token": "proxy-managed", "header": "X-Local-Token"}})

        timeout = float(self.cfg["long_request_timeout_s"] if is_long_request(sub) else self.cfg["request_timeout_s"])
        try:
            status, payload = await self.client.call(method, f"/api/{sub}", query=_clean_query(query), json=body_json, timeout=timeout)
        except EngineUnavailable as exc:
            self.log.warning(f"[fishtool] proxy {method} {sub} failed: {exc}")
            return envelope(504 if "timeout" in str(exc) else 502, {"detail": str(exc), "error_code": "engine_unavailable"})
        return envelope(status, payload)
