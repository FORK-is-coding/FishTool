"""
B站API封装 - WBI签名与基础API类
实现B站Web接口的签名认证和请求封装
参考: https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/misc/sign/wbi.md

本模块是访问 B站开放接口的基础层，包含：

一、WBISigner WBI签名器
B站部分接口（如投稿搜索）要求 WBI 签名：
- 从 nav 接口获取 img_key / sub_key 密钥对
- 用混淆表对密钥进行字符重排得到 mixin_key
- 参数排序 + 过滤特殊字符 + 拼时间戳 wts
- MD5(query + mixin_key) 得到 w_rid 签名
- 密钥每小时刷新一次（need_update 判断）

二、BilibiliAPI 基础请求封装
- 统一 aiohttp 会话管理与请求头
- 支持固定 Cookie 或 CookiePool 轮换取用
- 可选接入 RateLimiter 限频
- 重试机制：超时/网络错误/412/429/风控码自动重试，
  Cookie 失效码 -101 直接抛出不重试
- 业务码解析：成功剥离外层 data 返回
- 便捷方法：get/post + 业务方法
  （get_user_info/get_user_relation_stat/
   get_user_videos/get_ranking）

典型用法：
    async with BilibiliAPI(cookie=cookie) as api:
        data = await api.get('/x/web-interface/nav')

异常体系（core.exceptions）：
- BilibiliAPIError: 通用API错误
- CookieExpiredError: Cookie失效（-101）
- Status429Error/RateLimitError: 限流
- NetworkError/TimeoutError: 网络层

拆分说明（2026-08-22）：
原 api.py（882行）按职责拆分为：
- signer.py: WBISigner（原始 L72-L264）
- client.py: BilibiliAPICore 基础请求（原始 L279-L680）
- user.py: UserAPIMixin 用户与榜单接口（原始 L683-L882）
BilibiliAPI = BilibiliAPICore + UserAPIMixin 组合。
注释均原样保留，未删除未错位。
外部兼容：from bilibili.api import BilibiliAPI / WBISigner 全部可用。
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
from .client import BilibiliAPICore
from .user import UserAPIMixin

logger = get_logger(__name__)


class BilibiliAPI(BilibiliAPICore, UserAPIMixin):
    """B站API基础类 - 封装通用请求逻辑

    统一处理会话、Cookie、限频、签名、重试与业务码。
    Cookie 支持两种模式：
    - 固定 Cookie：构造时传入 cookie 字符串
    - Cookie 池：传入 CookiePool，每次请求轮换取用
    """


__all__ = ['BilibiliAPI', 'WBISigner']
