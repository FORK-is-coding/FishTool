"""
B站API封装 - 模块初始化
本包封装了访问B站开放接口所需的全部基础设施：
- api: BilibiliAPI 基础请求封装 + WBISigner WBI签名器
- auth: QRCodeLogin 扫码登录 + CookieLoginHelper Cookie辅助
- cookie_pool: CookiePool 多Cookie轮换与有效性管理
- rate_limiter: RateLimiter 限频/退避/熔断/抖动
通过本 __init__ 统一导出常用符号，
外部只需 from bilibili import BilibiliAPI 即可使用。
"""
# API基础：请求封装 + WBI 签名，所有B站接口的统一入口
# 本包其余模块依赖 api 层提供的请求能力
from .api import BilibiliAPI, WBISigner
# 认证：扫码登录全流程 + Cookie 辅助工具
from .auth import QRCodeLogin, CookieLoginHelper, QRLoginStatus
# Cookie池：多Cookie轮换、有效性检查、加密存储
from .cookie_pool import CookiePool, CookiePoolManager, Cookie, get_cookie_pool
# 限频：单端点/多端点限频器 + 全局单例获取
from .rate_limiter import RateLimiter, MultiEndpointRateLimiter, get_rate_limiter

# 对外导出清单
# 按功能分组：API基础/认证/Cookie池/限频
# 外部只依赖 __all__ 中的符号，内部实现可自由调整
__all__ = [
    # API基础
    'BilibiliAPI',  # B站API请求封装
    'WBISigner',    # WBI签名器

    # 认证
    'QRCodeLogin',      # 扫码登录
    'CookieLoginHelper',  # Cookie登录辅助
    'QRLoginStatus',    # 扫码状态枚举

    # Cookie池
    'CookiePool',          # Cookie池
    'CookiePoolManager',   # Cookie池管理器
    'Cookie',              # Cookie模型
    'get_cookie_pool',     # 获取Cookie池单例

    # 限频
    'RateLimiter',             # 单端点限频器
    'MultiEndpointRateLimiter',  # 多端点限频器
    'get_rate_limiter',        # 获取限频器单例
]
