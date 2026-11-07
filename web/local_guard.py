"""本机单用户防护：session token + 同源校验 + 写请求 CSRF 依赖（规格 §10.2）。

背景：本轮新增的长采集 / 取消 / 重试写端点只面向「本机单用户」，仓库暂无认证体系，
因此这里补一层最小的本机防护，**不虚构 owner 身份**：

1. **session token**：进程内生成，只通过响应体发给同源页面 / CLI；
   放在请求头 ``X-Local-Token``，**不进 URL、不进日志**；
2. **同源校验**：Host 默认只允许环回地址；带 Origin 时必须是同源（scheme + host + port
   三者都匹配），任意本机端口的页面不算同源；
3. **写请求 CSRF 依赖**：写端点挂 ``require_local_write``，缺 token / 跨站一律 403。

其它模块（如 02 watch 写端点）可复用同一依赖，不复制第二套鉴权系统。
生产网络部署、多账号权限另立需求，不在此处假装实现。
"""
from __future__ import annotations

import hmac
import os
import secrets
from typing import Optional, Set, Tuple

from fastapi import Header, HTTPException, Request

#: 请求头名（token 不进 URL / 日志）
LOCAL_TOKEN_HEADER = 'X-Local-Token'

#: 默认只允许环回 Host
_DEFAULT_ALLOWED_HOSTS = {'localhost', '127.0.0.1', '::1', '[::1]'}

#: 默认允许的本机端口：开发常用 8000/8080，加标准 Web 端口 80/443
#: （``FISHTOOL_ALLOWED_PORTS`` 可整体覆盖）。同源判定必须带上端口，
#: 否则 ``http://127.0.0.1:5173`` 这类任意本机页面都会被当成同源。
_DEFAULT_ALLOWED_PORTS = frozenset({80, 443, 8000, 8080})

#: 认可的 Origin scheme；``null``（file:// / 沙箱 iframe）与其它伪协议一律视为跨站。
_ALLOWED_SCHEMES = frozenset({'http', 'https'})

#: Origin / Host 省略端口时的 scheme 默认端口。
_DEFAULT_PORT_BY_SCHEME = {'http': 80, 'https': 443}


def _split_authority(authority: str) -> Tuple[str, Optional[int]]:
    """把 ``host[:port]`` / ``[ipv6][:port]`` 拆成主机与端口。

    Args:
        authority: 不含 scheme 与路径的权威段。

    Returns:
        Tuple[str, Optional[int]]: 小写主机（IPv6 保留方括号）与端口（缺省为 None）。
    """
    text = (authority or '').strip().lower()
    if not text:
        return '', None
    # IPv6 字面量：主机固定在方括号内，端口在右括号之后。
    if text.startswith('['):
        host, _, remainder = text.partition(']')
        port_text = remainder.lstrip(':')
        return host + ']', int(port_text) if port_text.isdigit() else None
    if ':' in text:
        host, _, port_text = text.rpartition(':')
        if host and port_text.isdigit():
            return host, int(port_text)
        # 形如 ``example.com:abc`` 的非法端口只保留主机，端口视为缺省。
        return text.split(':', 1)[0], None
    return text, None


def _allowed_hosts() -> Set[str]:
    """返回允许的 Host 集合（可用 ``FISHTOOL_ALLOWED_HOSTS`` 覆盖）。

    配置项既接受纯主机（``127.0.0.1``），也接受 ``主机:端口`` 写法：
    这里只取主机部分，端口由 :func:`_allowed_ports` 汇总。

    Returns:
        Set[str]: 小写 Host 集合。
    """
    raw = os.environ.get('FISHTOOL_ALLOWED_HOSTS', '').strip()
    if raw:
        hosts = {_split_authority(item)[0] for item in raw.split(',') if item.strip()}
        hosts.discard('')
        if hosts:
            return hosts
    return set(_DEFAULT_ALLOWED_HOSTS)


def _allowed_ports() -> Set[int]:
    """返回允许的 Origin 端口集合。

    ``FISHTOOL_ALLOWED_PORTS``（逗号分隔）整体覆盖默认值；未设置时用默认端口集，
    并把 ``FISHTOOL_ALLOWED_HOSTS`` 里显式写出的端口一并纳入。

    Returns:
        Set[int]: 允许端口集合。
    """
    raw_ports = os.environ.get('FISHTOOL_ALLOWED_PORTS', '').strip()
    if raw_ports:
        ports = {int(item.strip()) for item in raw_ports.split(',') if item.strip().isdigit()}
        if ports:
            return ports
        # 配置了但全是非法值：回退默认，避免把服务锁死在本机都访问不了的状态。
        return set(_DEFAULT_ALLOWED_PORTS)
    ports = set(_DEFAULT_ALLOWED_PORTS)
    raw_hosts = os.environ.get('FISHTOOL_ALLOWED_HOSTS', '').strip()
    if raw_hosts:
        for item in raw_hosts.split(','):
            _, port = _split_authority(item)
            if port is not None:
                ports.add(port)
    return ports


def _host_only(value: str) -> str:
    """从 Host / Origin 值中取出主机部分。

    Args:
        value: 形如 ``127.0.0.1:8000`` 或 ``http://127.0.0.1:8000``。

    Returns:
        str: 小写主机（含 IPv6 的方括号形式）。
    """
    text = (value or '').strip().lower()
    if '://' in text:
        text = text.split('://', 1)[1]
    text = text.split('/', 1)[0]
    return _split_authority(text)[0]


def _origin_parts(origin: str) -> Optional[Tuple[str, str, int]]:
    """把 Origin 拆成 ``(scheme, host, port)`` 三元组。

    Args:
        origin: 请求头 ``Origin`` 的值。

    Returns:
        Optional[Tuple[str, str, int]]: 合法 http/https Origin 的三元组；``null``、
        缺少 scheme、未知 scheme 等一律返回 None。
    """
    text = (origin or '').strip()
    if '://' not in text:
        return None
    scheme, _, rest = text.partition('://')
    scheme = scheme.strip().lower()
    host, port = _split_authority(rest.split('/', 1)[0])
    if not host:
        return None
    if port is None:
        port = _DEFAULT_PORT_BY_SCHEME.get(scheme)
        if port is None:
            return None
    return scheme, host, port


class LocalGuard:
    """本机 session token 与同源校验。"""

    def __init__(self, token: Optional[str] = None):
        """初始化。

        Args:
            token: 显式指定 token（测试用）；默认从环境变量读取，缺省则随机生成。
        """
        env_token = os.environ.get('FISHTOOL_LOCAL_SESSION_TOKEN')
        self._token = token or env_token or secrets.token_urlsafe(32)

    def issue_token(self) -> str:
        """返回本机 session token（仅供同源响应体下发）。

        Returns:
            str: session token。
        """
        return self._token

    def verify_token(self, token: Optional[str]) -> bool:
        """常量时间比较 token。

        Args:
            token: 请求头中的 token。

        Returns:
            bool: True 表示合法。
        """
        if not token or not self._token:
            return False
        return hmac.compare_digest(str(token), str(self._token))

    def is_local_host(self, host: Optional[str]) -> bool:
        """判断 Host 头是否属于允许集合（不抛异常，供中间件直接判定）。

        Args:
            host: 请求头 ``Host`` 的原始值（可能带端口）。

        Returns:
            bool: True 表示本机 / 显式放行的 Host。
        """
        return bool(host) and _host_only(host or '') in _allowed_hosts()

    def check_host(self, request: Request) -> None:
        """校验 Host 属于允许集合。

        Args:
            request: FastAPI 请求对象。

        Raises:
            HTTPException: 403 跨站 / 非本机 Host。
        """
        if not self.is_local_host(request.headers.get('host')):
            raise HTTPException(status_code=403, detail='local_host_required')

    def check_origin(self, request: Request) -> None:
        """校验 Origin（若存在）与请求同源（scheme / host / port）。

        Args:
            request: FastAPI 请求对象。

        Raises:
            HTTPException: 403 跨站 Origin。
        """
        if is_cross_site_origin(request.headers.get('origin'), request_port=request.url.port):
            raise HTTPException(status_code=403, detail='cross_site_request_blocked')


def is_cross_site_origin(origin: Optional[str], *, request_port: Optional[int] = None) -> bool:
    """判断 Origin 是否为跨站来源（按同源定义比较 scheme / host / port）。

    浏览器发起的跨站请求（含 ``mode: 'no-cors'`` 的 fetch 与 WebSocket 握手）一定携带 Origin；
    无 Origin 的请求（同源 GET、CLI、桌宠 websocket-client）视为非跨站。

    同源定义要求 scheme、host、port 三者全部匹配允许集合：只比主机名会把
    ``http://127.0.0.1:5173``（任意本机端口页面）误判成同源，从而绕过 CSRF 防线。

    Args:
        origin: 请求头 ``Origin`` 的值。
        request_port: 当前请求自身端口；服务的自定义端口（非 8000/8080）据此放行。

    Returns:
        bool: Origin 存在且不属于允许来源时为 True。
    """
    if not origin:
        return False
    parts = _origin_parts(origin)
    # ``null``、file://、chrome-extension:// 等无法构成同源来源，一律按跨站处理。
    if parts is None:
        return True
    scheme, host, port = parts
    if scheme not in _ALLOWED_SCHEMES:
        return True
    if host not in _allowed_hosts():
        return True
    if port in _allowed_ports():
        return False
    # 服务跑在自定义端口时，页面与请求同端口才算同源。
    return request_port is None or port != request_port


#: 进程内单例
_guard: Optional[LocalGuard] = None


def get_local_guard() -> LocalGuard:
    """返回进程内 LocalGuard 单例。

    Returns:
        LocalGuard: 单例。
    """
    global _guard
    if _guard is None:
        _guard = LocalGuard()
    return _guard


async def require_local_write(
    request: Request,
    x_local_token: Optional[str] = Header(default=None, alias=LOCAL_TOKEN_HEADER),
) -> None:
    """写请求依赖：token + 同源 Host / Origin 三重校验。

    Args:
        request: FastAPI 请求对象。
        x_local_token: ``X-Local-Token`` 请求头。

    Returns:
        无。

    Raises:
        HTTPException: 403 token 缺失 / 非法，或跨站 Host / Origin。
    """
    guard = get_local_guard()
    guard.check_host(request)
    guard.check_origin(request)
    if not guard.verify_token(x_local_token):
        # 无 Origin 的 CLI 请求同样必须带 token
        raise HTTPException(status_code=403, detail='local_token_required')


async def require_local_read(request: Request) -> None:
    """读请求依赖：只做同源校验，不强制 token。

    Args:
        request: FastAPI 请求对象。

    Returns:
        无。

    Raises:
        HTTPException: 403 跨站 Host / Origin。
    """
    guard = get_local_guard()
    guard.check_host(request)
    guard.check_origin(request)
