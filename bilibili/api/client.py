"""
B站API封装 - BilibiliAPICore 基础请求封装

拆分自 api.py 原始 L279-L680。
统一 aiohttp 会话管理与请求头，支持固定 Cookie 或 CookiePool 轮换取用，
可选接入 RateLimiter 限频，含重试机制与业务码解析。
"""
import time
import hashlib
import urllib.parse

from typing import Dict, Any, Optional, List, Tuple
from functools import reduce
import aiohttp
import asyncio
from datetime import datetime, timedelta
import logging

from core.exceptions import (
    BilibiliAPIError, WBISignError, InvalidResponseError,
    RateLimitError, Status429Error, CookieExpiredError,
    NetworkError, TimeoutError as CustomTimeoutError,
    is_retryable_error
)
from core.logger import get_logger
from .signer import WBISigner

logger = get_logger(__name__)


class BilibiliAPICore:
    """B站API基础类，统一管理会话、登录态、限频与请求配置。"""

    BASE_URL = "https://api.bilibili.com"

    def __init__(
        self,
        cookie: Optional[str] = None,
        cookie_pool: Optional[Any] = None,
        rate_limiter: Optional[Any] = None,
    ) -> None:
        """初始化 API 客户端。

        Args:
            cookie: 固定 Cookie 字符串；未传入时使用空字符串。
            cookie_pool: 可选 Cookie 池，请求前从中轮换登录态。
            rate_limiter: 可选限频器，所有请求复用同一限频策略。

        Returns:
            无。
        """
        # 会话必须在构造阶段显式存在，异步上下文进入前也允许安全检查。
        self.session: Optional[aiohttp.ClientSession] = None
        self.cookie = cookie or ""
        self.cookie_pool = cookie_pool
        self.rate_limiter = rate_limiter
        self.headers: Dict[str, str] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }
        if self.cookie:
            self.headers["Cookie"] = self.cookie
        # WBI 状态由专用签名器持有，避免核心客户端与签名器字段重复或错位。
        self.wbi_signer = WBISigner()

    async def __aenter__(self):
        """异步上下文管理器入口
        
        支持 async with BilibiliAPI() as api 用法，
        进入时自动初始化会话。
        # 设置初始值/默认状态，避免后续空引用
        """
        await self.init_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出
        
        退出时自动关闭会话，释放连接。
        # 释放连接/窗口资源
        """
        await self.close()

    async def init_session(self):
        """初始化aiohttp会话
        # 设置初始值/默认状态，避免后续空引用
        
        会话为空或已关闭时创建新的 ClientSession，
        # 实例化对象并准备使用
        统一设置 30 秒超时。
        # 写入配置/属性，影响后续行为
        """
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=timeout
            )
            # 自动获取 buvid3/buvid4 指纹并写入统一请求头
            # B站动态/搜索等接口对无指纹的匿名请求风控严格（412），
            # 补上指纹可显著降低被拦概率；获取失败静默忽略不影响主流程
            try:
                async with self.session.get(
                    "https://api.bilibili.com/x/frontend/finger/spi",
                    timeout=10
                ) as resp:
                    if resp.status == 200:
                        spi = await resp.json()
                        # 保存 B 站接口响应，后续从中提取标题、作者和评论区标识。
                        data = spi.get('data') or {}
                        cookies = {}
                        if data.get('b_3'):
                            cookies['buvid3'] = data['b_3']
                        if data.get('b_4'):
                            cookies['buvid4'] = data['b_4']
                        if cookies:
                            # 同步写入显式 Cookie 头，确保 buvid 指纹与 SESSDATA 一起发送。
                            cookie_parts = [part.strip() for part in self.headers.get("Cookie", "").split(";") if part.strip()]
                            for name, value in cookies.items():
                                cookie_parts = [part for part in cookie_parts if not part.startswith(f"{name}=")]
                                cookie_parts.append(f"{name}={value}")
                            if cookie_parts:
                                # request() 每次从 self.headers 复制请求头，指纹会随 Cookie 发出。
                                self.headers["Cookie"] = "; ".join(cookie_parts)
                            logger.info(f"已获取B站指纹: buvid3={cookies.get('buvid3', '')[:16]}...")
            except Exception as e:
                # 指纹获取失败不阻塞主流程，仅调试日志记录
                logger.debug(f"获取B站指纹失败(忽略): {e}")

    async def close(self):
        """关闭会话
        # 释放连接/窗口资源
        
        释放底层连接池，应在退出时调用。
        """
        if self.session and not self.session.closed:
            await self.session.close()

    def set_cookie(self, cookie: str):
        """设置 Cookie。

        更新实例 Cookie 与统一请求头。后续请求会从统一请求头创建独立快照，
        避免依赖会话默认头的可变性。

        Args:
            cookie: Cookie 字符串。

        Returns:
            无。
        """
        self.cookie = cookie
        self.headers['Cookie'] = cookie

    async def request(self,
                     method: str,
                     url: str,
                     params: Optional[Dict[str, Any]] = None,
                     data: Optional[Dict[str, Any]] = None,
                     json: Optional[Dict[str, Any]] = None,
                     headers: Optional[Dict[str, str]] = None,
                     need_sign: bool = False,
                     retry_times: int = 3,
                     budget_key: Optional[str] = None) -> Dict[str, Any]:
        """通用请求方法
        
        统一请求入口，处理 Cookie 获取、限频、签名、
        # 读取数据并赋值给当前作用域变量
        重试、业务码解析等通用逻辑。
        # 将原始文本转为结构化数据
        
        Args:
            method: 请求方法
            url: 请求URL
            params: URL参数
            data: 表单数据
            json: JSON数据
            headers: 额外请求头
            need_sign: 是否需要WBI签名
            retry_times: 重试次数
            
        Returns:
            响应JSON数据（已剥离外层 data 字段）
        """
        await self.init_session()
        
        # 从 cookie_pool 获取 cookie（如果配置了）
        # 池模式：每次请求前轮换取一个有效 Cookie
        if self.cookie_pool is not None:
            cookie_obj = await self.cookie_pool.get_cookie()
            # 判断 cookie_obj
            # 根据条件走向不同处理分支
            if cookie_obj:
                # 设置cookie属性
                # 先更新当前请求使用的 Cookie，再计算预算标识；标识只保留哈希值。
                self.set_cookie(cookie_obj.cookie_data)

        # 充电接口按当前 Cookie 施加独立滑动窗口预算，避免与普通端点限频混用。
        if budget_key and self.rate_limiter is not None:
            try:
                cookie_digest = hashlib.sha256(self.cookie.encode('utf-8')).hexdigest()
                await self.rate_limiter.acquire_cookie_budget(cookie_digest, budget_key=budget_key)
            except AttributeError:
                logger.warning("当前限频器不支持充电接口预算，继续使用通用限频")

        # 限频控制（如果配置了 rate_limiter）
        # 等待令牌，避免请求频率超限
        # 阻塞直到条件满足或超时
        if self.rate_limiter is not None:
            await self.rate_limiter.acquire(endpoint=url)
        
        # 合并请求头
        request_headers = self.headers.copy()
        # 判断 headers
        # 根据条件走向不同处理分支
        if headers:
            # 更新记录
            request_headers.update(headers)
        
        # WBI签名
        # 需要签名的接口先做参数签名
        if need_sign and params:
            params = await self.wbi_signer.sign_params(params, self.session)
        
        # 重试逻辑
        # 最多重试 retry_times 次，指数退避
        last_exception = None
        # 循环遍历处理
        # 对集合内每个元素执行相同处理
        for attempt in range(retry_times):
            # 异常保护：局部失败不影响主流程
            try:
                # 上下文管理：确保资源自动释放
                async with self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    data=data,
                    json=json,
                    headers=request_headers
                ) as resp:
                    # 检查HTTP状态码
                    if resp.status == 412:
                        # HTTP 412 是反爬拦截（IP/UA），不是 Cookie 失效
                        # 412 是 IP/UA 级风控，短时间重试无法恢复，直接抛出避免拖慢采集流程
                        # （与 429 限流不同，429 等待后可能恢复，412 不会）
                        logger.warning(f"请求被反爬拦截: HTTP 412 - {url}")
                        # 抛出异常中断流程
                        raise BilibiliAPIError("请求被反爬拦截（IP/UA限制）")
                    
                    # 边界/有效性检查
                    if resp.status == 429:
                        # 429 退避处理
                        # 优先用限频器给的延迟，否则读响应头
                        if self.rate_limiter is not None:
                            retry_after = self.rate_limiter.report_429(url)
                        else:
                            # 数值转换存入 retry_after
                            # 将数据从一种形态映射为另一种
                            retry_after = int(resp.headers.get('Retry-After', 60))
                        logger.warning(f"收到 429 响应，等待 {retry_after}s 后重试")
                        await asyncio.sleep(retry_after)
                        # 跳过本轮继续循环
                        continue  # 继续重试
                    
                    # 边界/有效性检查
                    if resp.status >= 500:
                        # 抛出异常中断流程
                        raise NetworkError(f"服务器错误: HTTP {resp.status}")
                    
                    # 解析响应
                    try:
                        result = await resp.json()
                    except Exception:
                        text = await resp.text()
                        # 抛出异常中断流程
                        raise InvalidResponseError(f"响应不是有效的JSON: {text[:200]}")
                    
                    # 检查业务状态码
                    code = result.get('code', -1)
                    # 读取字典/配置项
                    message = result.get('message', '')
                    
                    # Cookie失效
                    # -101 表示未登录/Cookie失效，不重试直接抛出
                    if code == -101:
                        # 抛出异常中断流程
                        raise CookieExpiredError(message)
                    
                    # 请求被风控（区分于 Cookie 失效）
                    # -352 风控校验失败，-412 请求被拦截
                    # 均为 IP/UA 级风控，短时间重试无法恢复，直接抛出避免拖慢采集
                    if code == -352 or code == -412:
                        logger.warning(f"请求被风控: {message}")
                        # 抛出异常中断流程
                        raise BilibiliAPIError(f"请求被风控: {message}")
                    
                    # 请求过快
                    # -509 访问过于频繁，等待后重试
                    # 阻塞直到条件满足或超时
                    if code == -509:
                        # 边界/有效性检查
                        if self.rate_limiter is not None:
                            retry_after = self.rate_limiter.report_429(url)
                            await asyncio.sleep(retry_after)
                            # 跳过本轮继续循环
                            continue
                        # 抛出异常中断流程
                        raise RateLimitError(60)

                    # -799 是空间资料接口常见的频率风控码。相比通用 1/2/4 秒
                    # 重试，使用更长退避并将最终失败明确标为限流，避免误判为字段缺失。
                    if code == -799:
                        retry_after = min(30, 6 * (attempt + 1))
                        if attempt < retry_times - 1:
                            logger.warning(
                                "请求受限(-799)，等待 %ss 后重试 %s/%s: %s",
                                retry_after, attempt + 1, retry_times, url
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        raise RateLimitError(retry_after)
                    
                    # 其他错误
                    # 非 0 业务码视为调用失败
                    if code != 0:
                        # 抛出异常中断流程
                        raise BilibiliAPIError(f"API错误 [{code}]: {message}")
                    
                    # 成功时记录并返回 data 字段
                    # 成功后重置限频器的429计数
                    if self.rate_limiter is not None:
                        self.rate_limiter.report_success()
                    
                    return result.get('data', {})
                    
            except asyncio.TimeoutError:
                # 将 asyncio 超时转换为项目统一异常，再按统一规则判断是否重试。
                last_exception = CustomTimeoutError(30)
                if not is_retryable_error(last_exception):
                    raise last_exception
                logger.warning(f"请求超时，重试 {attempt + 1}/{retry_times}: {url}")
                
            except aiohttp.ClientError as e:
                # 将 aiohttp 网络异常转换为项目统一异常，保留原始错误信息。
                last_exception = NetworkError(f"网络请求失败: {e}")
                if not is_retryable_error(last_exception):
                    raise last_exception
                logger.warning(f"网络错误，重试 {attempt + 1}/{retry_times}: {e}")
            
            except (Status429Error, CookieExpiredError) as e:
                # 429 已在业务码分支处理；Cookie失效属于明确不可重试错误。
                if isinstance(e, CookieExpiredError):
                    raise
                if not is_retryable_error(e):
                    raise
                last_exception = e
                logger.warning(f"请求限流，重试 {attempt + 1}/{retry_times}: {e}")
            
            except Exception as e:
                # 只有异常体系明确标记为可重试时才继续，其他异常立即暴露。
                if not is_retryable_error(e):
                    raise
                last_exception = e
                logger.warning(f"请求失败，重试 {attempt + 1}/{retry_times}: {e}")
            
            # 重试前等待
            # 指数退避：1s, 2s, 4s...
            if attempt < retry_times - 1:
                await asyncio.sleep(2 ** attempt)  # 指数退避
        
        # 所有重试都失败
        if last_exception:
            # 抛出异常中断流程
            raise last_exception
        else:
            # 抛出异常中断流程
            raise NetworkError("请求失败")

    async def get(self, url: str, params: Optional[Dict[str, Any]] = None,
                  need_sign: bool = False, **kwargs) -> Dict[str, Any]:
        """GET请求
        
        Args:
            url: 请求URL
            params: URL参数
            need_sign: 是否需要WBI签名
            **kwargs: 其他参数
            
        Returns:
            响应数据
        """
        return await self.request('GET', url, params=params, need_sign=need_sign, **kwargs)

    async def post(self, url: str, data: Optional[Dict[str, Any]] = None,
                   json: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
        """POST请求
        
        Args:
            url: 请求URL
            data: 表单数据
            json: JSON数据
            **kwargs: 其他参数
            
        Returns:
            响应数据
        """
        return await self.request('POST', url, data=data, json=json, **kwargs)

    async def poll_qrcode(self, qrcode_key: str) -> Tuple[Dict[str, Any], List[str]]:
        """调用 B 站扫码轮询接口并保留响应头 Cookie。

        Args:
            qrcode_key: 二维码轮询 key。

        Returns:
            原始 JSON 响应与所有 Set-Cookie 响应头。
        """
        try:
            await self.init_session()
            async with self.session.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": qrcode_key},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    raise BilibiliAPIError(f"扫码轮询失败: HTTP {response.status}")
                return await response.json(), response.headers.getall("Set-Cookie", [])
        except Exception as exc:
            logger.error(f"扫码轮询请求失败: {exc}")
            if isinstance(exc, BilibiliAPIError):
                raise
            raise NetworkError(f"扫码轮询请求失败: {exc}") from exc
