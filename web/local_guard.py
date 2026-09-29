"""本机单用户防护：session token + 同源校验 + 写请求 CSRF 依赖（规格 §10.2）。

背景：本轮新增的长采集 / 取消 / 重试写端点只面向「本机单用户」，仓库暂无认证体系，
因此这里补一层最小的本机防护，**不虚构 owner 身份**：

1. **session token**：进程内生成，只通过响应体发给同源页面 / CLI；
   放在请求头 ``X-Local-Token``，**不进 URL、不进日志**；
2. **同源校验**：Host 默认只允许环回地址；带 Origin 时必须是同源环回；
3. **写请求 CSRF 依赖**：写端点挂 ``require_local_write``，缺 token / 跨站一律 403。

其它模块（如 02 watch 写端点）可复用同一依赖，不复制第二套鉴权系统。
生产网络部署、多账号权限另立需求，不在此处假装实现。
"""
from __future__ import annotations

import hmac
import os
import secrets
from typing import Optional, Set

from fastapi import Header, HTTPException, Request

#: 请求头名（token 不进 URL / 日志）
LOCAL_TOKEN_HEADER = 'X-Local-Token'

#: 默认只允许环回 Host
_DEFAULT_ALLOWED_HOSTS = {'localhost', '127.0.0.1', '::1', '[::1]'}


def _allowed_hosts() -> Set[str]:
    """返回允许的 Host 集合（可用 ``FISHTOOL_ALLOWED_HOSTS`` 覆盖）。

    Returns:
        Set[str]: 小写 Host 集合。
    """
    raw = os.environ.get('FISHTOOL_ALLOWED_HOSTS', '').strip()
    if raw:
        return {item.strip().lower() for item in raw.split(',') if item.strip()}
    return set(_DEFAULT_ALLOWED_HOSTS)


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
    if text.startswith('['):
        return text.split(']', 1)[0] + ']'
    return text.split(':', 1)[0]


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

    def check_host(self, request: Request) -> None:
        """校验 Host 属于允许集合。

        Args:
            request: FastAPI 请求对象。

        Raises:
            HTTPException: 403 跨站 / 非本机 Host。
        """
        host = _host_only(request.headers.get('host') or '')
        if host not in _allowed_hosts():
            raise HTTPException(status_code=403, detail='local_host_required')

    def check_origin(self, request: Request) -> None:
        """校验 Origin（若存在）与 Host 同源。

        Args:
            request: FastAPI 请求对象。

        Raises:
            HTTPException: 403 跨站 Origin。
        """
        origin = request.headers.get('origin')
        if not origin:
            return
        if _host_only(origin) not in _allowed_hosts():
            raise HTTPException(status_code=403, detail='cross_site_request_blocked')


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
