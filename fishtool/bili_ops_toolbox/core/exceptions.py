"""
B站运营工具箱 - 自定义异常类
统一异常处理，便于错误追踪和用户友好提示

异常层次结构：
- BiliOpsException（根异常类）
  ├── ConfigError（配置相关）
  ├── DatabaseError（数据库相关）
  ├── BilibiliAPIError（B站API相关）
  │   ├── AuthenticationError（认证失败）
  │   │   └── CookieExpiredError（Cookie过期）
  │   ├── RateLimitError（限频）
  │   │   └── Status429Error（429错误）
  │   ├── IPBannedError（IP封禁）
  │   ├── AccountRiskError（账号风险）
  │   ├── CaptchaRequiredError（需要验证码）
  │   ├── CircuitBreakerError（熔断）
  │   ├── WBISignError（签名错误）
  │   └── InvalidResponseError（无效响应）
  ├── CrawlerError（爬虫相关）
  │   ├── NetworkError（网络错误）
  │   ├── TimeoutError（超时）
  │   └── ParseError（解析错误）
  ├── LLMError（大语言模型相关）
  │   ├── LLMNotConfiguredError（未配置）
  │   ├── LLMAPIError（API错误）
  │   └── TokenLimitExceededError（Token超限）
  └── 业务逻辑异常
      ├── ValidationError（验证错误）
      ├── ResourceNotFoundError（资源不存在）
      ├── PermissionDeniedError（权限不足）
      └── TaskError（任务错误）

使用场景：
1. 统一错误处理：所有异常继承自 BiliOpsException，便于统一捕获
2. 错误码标识：每个异常都有唯一的 code，方便日志记录和监控
3. 用户友好提示：异常消息使用中文，易于理解
4. 携带上下文：异常可携带额外信息（如 retry_after、cookie_name）
"""


class BiliOpsException(Exception):
    """基础异常类（所有自定义异常的根类）
    
    设计要点：
    - 统一异常接口：所有异常都包含 message 和 code
    - 便于统一捕获：可以用 except BiliOpsException 捕获所有自定义异常
    - 支持错误码：便于日志记录、监控告警、错误分类
    
    Attributes:
        message: 错误消息（中文描述）
        code: 错误码（大写字母+下划线，如 "AUTH_ERROR"）
    """
    def __init__(self, message: str, code: str = None):
        """初始化异常
        
        Args:
            message: 错误消息
            code: 错误码，默认为 "UNKNOWN_ERROR"
        """
        self.message = message  # 保存错误消息
        self.code = code or "UNKNOWN_ERROR"  # 保存错误码，如果未提供则使用默认值
        super().__init__(self.message)  # 调用父类 Exception 的初始化方法


# ============ 配置相关异常 ============
# 用于配置文件读取、解析、验证等场景

class ConfigError(BiliOpsException):
    """配置错误（配置文件不存在、格式错误、参数非法等）
    
    使用场景：
    - 配置文件解析失败
    - 必需配置项缺失
    - 配置项类型错误
    """
    def __init__(self, message: str):
        """message: str 参数异常初始化，携带固定错误码"""
        # 使用固定错误码 "CONFIG_ERROR"
        super().__init__(message, "CONFIG_ERROR")


class ConfigNotFoundError(ConfigError):
    """配置项不存在（尝试访问不存在的配置键时抛出）
    
    使用场景：
    - 访问未定义的配置项
    - 配置键名拼写错误
    
    示例：
        config.get('not.exist.key')  # 抛出此异常
    """
    def __init__(self, key: str):
        """异常初始化"""
        # 自动构造错误消息，包含具体的配置键名
        super().__init__(f"配置项不存在: {key}", "CONFIG_NOT_FOUND")


# ============ 数据库相关异常 ============
# 用于数据库连接、查询、事务等场景

class DatabaseError(BiliOpsException):
    """数据库错误（连接失败、查询错误、事务失败等）
    
    使用场景：
    - 数据库连接失败
    - SQL 语句执行错误
    - 事务提交/回滚失败
    - 数据完整性约束违反
    """
    def __init__(self, message: str):
        """message: str 参数异常初始化，携带固定错误码"""
        super().__init__(message, "DATABASE_ERROR")


class RecordNotFoundError(DatabaseError):
    """记录不存在（查询数据库时未找到指定记录）
    
    使用场景：
    - 根据主键查询记录不存在
    - 外键引用的记录不存在
    
    Attributes:
        model: 模型名称（如 "Account", "Video"）
        id: 记录ID
    
    示例：
        db.query(Account).filter_by(id=999).first()  # 如果不存在则抛出此异常
    """
    def __init__(self, model: str, id: any):
        """异常初始化"""
        # 构造包含模型名和ID的错误消息
        super().__init__(f"{model} 记录不存在: {id}", "RECORD_NOT_FOUND")


# ============ B站API相关异常 ============
# 用于B站API调用、认证、限流等场景

class BilibiliAPIError(BiliOpsException):
    """B站API错误基类（所有B站API相关异常的父类）
    
    使用场景：
    - API返回错误码
    - 网络请求失败
    - 响应格式异常
    """
    def __init__(self, message: str, code: str = "BILIBILI_API_ERROR"):
        """异常初始化"""
        super().__init__(message, code)


class AuthenticationError(BilibiliAPIError):
    """认证失败（Cookie失效、登录态丢失等）
    
    使用场景：
    - Cookie 过期或无效
    - 未登录访问需要登录的接口
    - Token 失效
    
    处理建议：
    - 提示用户重新登录
    - 自动切换到备用账号
    - 刷新登录态
    """
    def __init__(self, message: str = "认证失败，请重新登录"):
        """异常初始化"""
        super().__init__(message, "AUTH_ERROR")


class CookieExpiredError(AuthenticationError):
    """Cookie已失效（特定的认证失败场景）
    
    使用场景：
    - SESSDATA 过期（B站登录态的核心Cookie）
    - bili_jct 失效（CSRF Token）
    - 账号在其他设备登录导致当前Cookie失效
    
    Attributes:
        cookie_name: 失效的Cookie名称（如 "SESSDATA"）
    
    处理建议：
    - 从Cookie池中移除失效Cookie
    - 标记账号状态为失效
    - 触发Cookie刷新流程
    """
    def __init__(self, cookie_name: str = None):
        """异常初始化"""
        # 根据是否提供 cookie_name 构造不同的错误消息
        msg = f"Cookie已失效: {cookie_name}" if cookie_name else "Cookie已失效"
        super().__init__(msg)
        self.cookie_name = cookie_name  # 保存具体的Cookie名称，便于后续处理


class RateLimitError(BilibiliAPIError):
    """限频错误（请求过于频繁，触发B站限流）
    
    使用场景：
    - 请求间隔过短
    - 单位时间内请求次数过多
    - 触发B站反爬虫机制
    
    Attributes:
        retry_after: 建议等待时间（秒）
    
    处理建议：
    - 等待 retry_after 秒后重试
    - 增加请求间隔
    - 启用限流器（RateLimiter）
    """
    def __init__(self, retry_after: int = None):
        """异常初始化"""
        # 构造包含等待时间的错误消息
        msg = f"请求过于频繁，请{retry_after}秒后重试" if retry_after else "请求过于频繁"
        super().__init__(msg, "RATE_LIMIT")
        self.retry_after = retry_after  # 保存建议等待时间


class Status429Error(RateLimitError):
    """429错误（HTTP状态码429：Too Many Requests）
    
    这是最严重的限流错误，说明已经触发了B站的硬性限流。
    
    使用场景：
    - 短时间内大量请求
    - 并发请求过多
    - 已被临时限流
    
    Attributes:
        endpoint: 触发429的API端点（如 "/x/web-interface/view"）
        retry_after: 建议等待时间（秒）
    
    处理建议：
    - 立即停止请求（触发熔断）
    - 等待更长时间（通常30秒以上）
    - 检查并调整请求策略
    - 记录日志便于后续分析
    """
    def __init__(self, endpoint: str = None, retry_after: int = None):
        """异常初始化"""
        super().__init__(retry_after)  # 调用父类初始化
        self.endpoint = endpoint  # 保存触发429的具体端点
        self.code = "STATUS_429"  # 使用更具体的错误码


class IPBannedError(BilibiliAPIError):
    """IP被封禁（最严重的限流：IP被B站拉黑）
    
    使用场景：
    - 持续大量异常请求
    - 触发B站风控系统
    - IP被列入黑名单
    
    处理建议：
    - 立即停止所有请求
    - 更换IP地址（使用代理）
    - 联系B站申诉（如果是误封）
    - 检查请求模式是否正常
    """
    def __init__(self, message: str = "IP已被封禁"):
        """异常初始化"""
        super().__init__(message, "IP_BANNED")


class AccountRiskError(BilibiliAPIError):
    """账号风险（账号被B站风控系统标记为异常）
    
    使用场景：
    - 账号行为异常（如短时间大量操作）
    - 触发人机验证
    - 账号被限制部分功能
    
    处理建议：
    - 暂停使用该账号
    - 完成人机验证
    - 降低操作频率
    - 模拟正常用户行为
    """
    def __init__(self, message: str = "账号存在风险，请通过人机验证"):
        """异常初始化"""
        super().__init__(message, "ACCOUNT_RISK")


class CaptchaRequiredError(BilibiliAPIError):
    """需要验证码（操作需要完成验证码验证）
    
    使用场景：
    - 登录时需要验证码
    - 发送评论需要验证码
    - 触发风控需要人机验证
    
    处理建议：
    - 提示用户完成验证码
    - 集成验证码识别服务（付费）
    - 降低操作频率避免触发验证码
    """
    def __init__(self, message: str = "需要完成验证码"):
        """异常初始化"""
        super().__init__(message, "CAPTCHA_REQUIRED")


class CircuitBreakerError(BilibiliAPIError):
    """熔断错误（系统触发熔断机制，暂停请求）
    
    熔断机制说明：
    当连续失败次数超过阈值时，系统会自动"熔断"（停止请求），
    等待一段时间后再尝试恢复，避免雪崩效应。
    
    使用场景：
    - 连续多次429错误
    - 连续多次网络超时
    - 连续多次API返回错误
    
    Attributes:
        reason: 触发熔断的原因
    
    处理建议：
    - 等待熔断器自动恢复
    - 检查并修复根本问题
    - 记录日志便于分析
    """
    def __init__(self, reason: str):
        """异常初始化"""
        super().__init__(f"触发熔断: {reason}", "CIRCUIT_BREAKER")
        self.reason = reason  # 保存触发熔断的具体原因


class WBISignError(BilibiliAPIError):
    """WBI签名错误（B站WBI签名算法失败）
    
    WBI签名说明：
    B站使用WBI签名算法对部分接口进行安全验证，
    签名失败会导致请求被拒绝。
    
    使用场景：
    - WBI密钥过期
    - 签名算法错误
    - 参数处理错误
    
    处理建议：
    - 刷新WBI密钥
    - 检查签名算法实现
    - 确认参数格式正确
    """
    def __init__(self, message: str = "WBI签名失败"):
        """异常初始化"""
        super().__init__(message, "WBI_SIGN_ERROR")


class InvalidResponseError(BilibiliAPIError):
    """无效响应（API返回的数据格式不符合预期）
    
    使用场景：
    - 返回的不是有效JSON
    - 缺少必需的字段
    - 数据类型不匹配
    - 接口结构变更
    
    处理建议：
    - 检查API是否有变更
    - 验证请求参数是否正确
    - 记录完整响应便于调试
    """
    def __init__(self, message: str = "API返回数据格式错误"):
        """异常初始化"""
        super().__init__(message, "INVALID_RESPONSE")


# ============ 爬虫相关异常 ============
# 用于网络请求、数据抓取、解析等场景

class CrawlerError(BiliOpsException):
    """爬虫错误基类（所有爬虫相关异常的父类）
    
    使用场景：
    - 网络连接失败
    - 请求超时
    - 响应解析错误
    - 反爬虫机制触发
    """
    def __init__(self, message: str, code: str = "CRAWLER_ERROR"):
        """异常初始化"""
        super().__init__(message, code)


class NetworkError(CrawlerError):
    """网络错误（网络连接、DNS解析等底层网络问题）
    
    使用场景：
    - 网络不可达
    - DNS解析失败
    - 连接被重置
    - SSL证书错误
    
    处理建议：
    - 检查网络连接
    - 重试请求
    - 使用备用域名/IP
    - 检查代理设置
    """
    def __init__(self, message: str = "网络请求失败"):
        """异常初始化"""
        super().__init__(message, "NETWORK_ERROR")


class TimeoutError(CrawlerError):
    """请求超时（请求等待时间超过设定阈值）
    
    使用场景：
    - 服务器响应慢
    - 网络延迟高
    - 大文件下载超时
    
    Attributes:
        timeout: 超时时间（秒）
    
    处理建议：
    - 增加超时时间
    - 重试请求
    - 检查网络质量
    - 优化请求参数
    """
    def __init__(self, timeout: int = None):
        """异常初始化"""
        # 构造包含超时时间的错误消息
        msg = f"请求超时({timeout}秒)" if timeout else "请求超时"
        super().__init__(msg, "TIMEOUT")


class ParseError(CrawlerError):
    """解析错误（HTML/JSON/XML等数据解析失败）
    
    使用场景：
    - HTML结构变更
    - JSON格式错误
    - 字段缺失
    - 编码问题
    
    处理建议：
    - 检查目标网站是否更新
    - 更新解析规则
    - 增加容错处理
    - 记录原始数据便于调试
    """
    def __init__(self, message: str = "数据解析失败"):
        """异常初始化"""
        super().__init__(message, "PARSE_ERROR")


# ============ LLM相关异常 ============
# 用于大语言模型API调用、Token管理等场景

class LLMError(BiliOpsException):
    """LLM错误基类（所有大语言模型相关异常的父类）
    
    使用场景：
    - LLM API调用失败
    - Token用量超限
    - 模型未配置
    - 响应格式错误
    """
    def __init__(self, message: str, code: str = "LLM_ERROR"):
        """异常初始化"""
        super().__init__(message, code)


class LLMNotConfiguredError(LLMError):
    """LLM未配置（用户未设置LLM API密钥或配置）
    
    使用场景：
    - 首次使用LLM功能
    - API密钥未设置
    - 配置信息缺失
    
    处理建议：
    - 引导用户到配置页面
    - 提供配置文档链接
    - 检查必需配置项
    """
    def __init__(self, message: str = "LLM未配置，请先在配置页面设置API"):
        """异常初始化"""
        super().__init__(message, "LLM_NOT_CONFIGURED")


class LLMAPIError(LLMError):
    """LLM API错误（调用LLM API时返回错误）
    
    使用场景：
    - API密钥无效
    - 请求格式错误
    - 模型不可用
    - 服务端错误
    
    处理建议：
    - 检查API密钥是否有效
    - 验证请求参数
    - 查看API文档确认接口变更
    - 联系服务提供商
    """
    def __init__(self, message: str):
        """message: str 参数异常初始化，携带固定错误码"""
        super().__init__(message, "LLM_API_ERROR")


class TokenLimitExceededError(LLMError):
    """Token用量超限（达到每日Token使用上限）
    
    Token说明：
    LLM按Token计费，1个Token约等于0.75个英文单词或1.5个中文字符。
    为了控制成本，系统设置了每日Token用量上限。
    
    使用场景：
    - 单日Token用量超过配置的限制
    - 批量处理任务消耗大量Token
    
    Attributes:
        used: 已使用的Token数
        limit: Token用量上限
    
    处理建议：
    - 等待第二天重置
    - 提高Token限制
    - 优化提示词减少Token消耗
    - 分批处理任务
    """
    def __init__(self, used: int, limit: int):
        """异常初始化"""
        # 构造包含用量和限制的错误消息
        super().__init__(f"Token用量超限: {used}/{limit}", "TOKEN_LIMIT_EXCEEDED")
        self.used = used  # 保存已使用量
        self.limit = limit  # 保存限制值


# ============ 业务逻辑异常 ============
# 用于应用层的业务逻辑验证和错误处理

class ValidationError(BiliOpsException):
    """数据验证错误（输入数据不符合业务规则）
    
    使用场景：
    - 表单字段验证失败
    - 参数类型错误
    - 参数范围超限
    - 必填字段缺失
    
    Attributes:
        field: 错误字段名
    
    示例：
        ValidationError("username", "用户名长度必须在3-20个字符之间")
    
    处理建议：
    - 在前端添加验证逻辑
    - 提供清晰的错误提示
    - 记录验证失败日志
    """
    def __init__(self, field: str, message: str):
        """异常初始化"""
        # 构造包含字段名和错误信息的消息
        super().__init__(f"{field}: {message}", "VALIDATION_ERROR")
        self.field = field  # 保存错误字段名，便于前端定位


class ResourceNotFoundError(BiliOpsException):
    """资源不存在（请求的资源未找到）
    
    使用场景：
    - 视频不存在或已删除
    - UP主不存在
    - 评论已被删除
    - 动态已失效
    
    Attributes:
        resource_type: 资源类型（如 "视频", "UP主", "评论"）
        resource_id: 资源ID（如 BV号、UID）
    
    处理建议：
    - 提示用户资源不存在
    - 从数据库中移除失效资源
    - 更新监控任务列表
    """
    def __init__(self, resource_type: str, resource_id: str):
        """异常初始化"""
        super().__init__(f"{resource_type}不存在: {resource_id}", "RESOURCE_NOT_FOUND")


class PermissionDeniedError(BiliOpsException):
    """权限不足（用户没有执行该操作的权限）
    
    使用场景：
    - 访问他人的私有数据
    - 执行管理员操作但非管理员
    - 账号权限受限
    
    处理建议：
    - 提示用户权限不足
    - 引导用户申请权限
    - 检查权限配置
    """
    def __init__(self, message: str = "权限不足"):
        """异常初始化"""
        super().__init__(message, "PERMISSION_DENIED")


class TaskError(BiliOpsException):
    """任务执行错误（异步任务或定时任务执行失败）
    
    使用场景：
    - 监控任务执行失败
    - 数据采集任务异常
    - 报告生成任务出错
    - 批量操作失败
    
    Attributes:
        task_name: 任务名称
    
    处理建议：
    - 记录详细错误日志
    - 保存任务执行状态
    - 支持任务重试
    - 发送失败通知
    """
    def __init__(self, task_name: str, message: str):
        """异常初始化"""
        super().__init__(f"任务[{task_name}]执行失败: {message}", "TASK_ERROR")
        self.task_name = task_name  # 保存任务名称，便于日志记录和监控

# ============ 工具函数 ============

def format_exception(exc: Exception) -> dict:
    """格式化异常信息为字典
    
    Args:
        exc: 异常对象
        
    Returns:
        异常信息字典
    """
    if isinstance(exc, BiliOpsException):
        return {
            "error": True,
            "code": exc.code,
            "message": exc.message,
            "type": exc.__class__.__name__
        }
    else:
        return {
            "error": True,
            "code": "UNKNOWN_ERROR",
            "message": str(exc),
            "type": exc.__class__.__name__
        }


def is_retryable_error(exc: Exception) -> bool:
    """判断异常是否可重试
    
    Args:
        exc: 异常对象
        
    Returns:
        是否可重试
    """
    retryable_types = (
        NetworkError,
        TimeoutError,
        RateLimitError,
        Status429Error
    )
    return isinstance(exc, retryable_types)