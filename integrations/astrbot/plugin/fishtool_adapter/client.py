"""访问引擎的 HTTP 客户端。

- ``trust_env=False``：不读系统代理，避免回环请求被代理劫持。
- 跟随 307（``/api/logs``、``/api/config`` 无尾斜杠时引擎返回 307，保留方法与请求体）。
- 只带 ``X-Local-Token``，不转发浏览器的 Origin / Authorization / Cookie。``Origin: null``
  会被引擎 block_cross_site_writes 判为跨站返回 403。
- 引擎重启后 port/token 会变：每次请求都从 supervisor 读取，不缓存。
"""
from __future__ import annotations

import httpx

from .errors import EngineUnavailable

LOCAL_TOKEN_HEADER = "X-Local-Token"

__all__ = ["EngineClient", "EngineUnavailable", "LOCAL_TOKEN_HEADER", "parse_body"]


class EngineClient:
    def __init__(self, supervisor):
        self.sup = supervisor
        self._client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
            timeout=httpx.Timeout(120.0, connect=5.0),
        )

    def _headers(self) -> dict:
        return {LOCAL_TOKEN_HEADER: self.sup.token, "Accept": "application/json"}

    async def request(self, method: str, path: str, *, query: dict | None = None, json=None, timeout: float = 120) -> httpx.Response:
        """发请求到引擎，返回原始响应。

        Raises:
            EngineUnavailable: 连接失败或超时。
        """
        if not self.sup.port:
            raise EngineUnavailable("engine not started")
        url = f"{self.sup.base_url}{path}"
        kwargs: dict = {"params": query or None, "headers": self._headers(), "timeout": httpx.Timeout(timeout, connect=5.0)}
        if json is not None and method not in ("GET", "HEAD"):
            kwargs["json"] = json
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise EngineUnavailable(f"engine request timeout after {int(timeout)}s") from exc
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"engine unreachable: {exc!r}") from exc

    async def call(self, method: str, path: str, *, query: dict | None = None, json=None, timeout: float = 120) -> tuple[int, object]:
        """发请求并把响应体解析为 JSON（非 JSON 时返回 ``{"detail": 文本}``）。"""
        resp = await self.request(method, path, query=query, json=json, timeout=timeout)
        return resp.status_code, parse_body(resp)

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_body(resp: httpx.Response):
    if resp.status_code in (204, 205, 304) or not resp.content:
        return None
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype:
        try:
            return resp.json()
        except ValueError:
            return {"detail": resp.text[:2000]}
    return {"detail": resp.text[:2000], "content_type": ctype}
