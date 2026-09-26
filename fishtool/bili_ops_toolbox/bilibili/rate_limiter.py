"""
B站API封装 - 限频器
实现智能限频、429退避、熔断保护、随机抖动

本模块是爬虫风控防护的核心组件，功能特性：
- 令牌桶算法：平滑限频，按时间间隔匀速补充令牌
- 429 自动退避：遭遇限流后按指数序列延迟重试
- 熔断保护：连续失败超过阈值自动熔断，暂停请求
- 随机抖动：在基础延迟上添加 ±20% 随机波动，
# 将元素加入容器/布局
  避免固定频率请求被反爬机制识别
- 请求历史统计：记录最近请求端点与时间，便于诊断
- 多端点隔离：MultiEndpointRateLimiter 为不同
  API 端点（普通/评论/动态）配置独立限频策略

核心类：
- RateLimiter: 单端点限频器（令牌桶 + 429退避 + 熔断）
- MultiEndpointRateLimiter: 多端点管理器，内部持有
  多个 RateLimiter 实例，按端点类型路由

使用方式：
    limiter = RateLimiter(rate=2.0)      # 2秒一个请求
    await limiter.acquire('/api/xxx')     # 等待令牌
    # 阻塞直到条件满足或超时
    limiter.report_429('/api/xxx')        # 遭遇限流
    limiter.report_success()              # 恢复成功

全局入口：
    get_rate_limiter(): 从配置读取限频参数，
    返回全局 MultiEndpointRateLimiter 单例
    # 将结果交回调用方
"""
import asyncio
# 导入模块
import time
# 导入模块
import random
# 从 typing 导入符号
from typing import Dict, Optional
# 从 collections 导入符号
from collections import deque
# 从 datetime 导入符号
from datetime import datetime, timedelta
# 导入模块
import logging

# 从 core.exceptions 导入符号
from core.exceptions import RateLimitError, CircuitBreakerError
# 从 core.logger 导入符号
from core.logger import get_logger, RiskControlLevel

logger = get_logger(__name__)


class RateLimiter:
    """限频器 - 实现令牌桶算法和429退避策略
    
    核心功能：
    1. 令牌桶限频：控制平均请求速率
    2. 随机抖动：在基础延迟上添加 ±20% 随机波动，避免请求模式被识别
    # 将元素加入容器/布局
    3. 429 退避：指数级延迟重试
    4. 熔断保护：连续失败自动熔断
    """
    
    def __init__(self, 
                 # 赋值并准备后续使用
                 rate: float = 2.0,
                 # 赋值并准备后续使用
                 retry_delays: list = None,
                 # 赋值并准备后续使用
                 max_429_count: int = 5,
                 # 赋值并准备后续使用
                 jitter_factor: float = 0.2):
        """初始化限频器
        # 设置初始值/默认状态，避免后续空引用
        # 写入配置/属性，影响后续行为
        
        Args:
            rate: 基础请求间隔(秒)
            retry_delays: 429退避延迟序列(秒)
            max_429_count: 连续429次数阈值，超过则熔断
            jitter_factor: 随机抖动因子，范围 [0, 1]，表示在基础延迟上的波动比例
                          例如 0.2 表示 ±20% 的随机波动
        """
        self.rate = rate
        self.retry_delays = retry_delays or [30, 60, 120, 300, 600]
        self.max_429_count = max_429_count
        self.jitter_factor = jitter_factor  # 新增：随机抖动因子
        
        # 令牌桶
        # 初始满令牌，避免启动后第一个请求立即被限
        # 触发服务/线程开始运行
        self.tokens = 1.0
        self.max_tokens = 1.0
        self.last_update = time.time()
        
        # 429计数
        self.status_429_count = 0
        self.last_429_time: Optional[datetime] = None
        
        # 熔断状态
        self.is_circuit_open = False
        self.circuit_open_time: Optional[datetime] = None
        self.circuit_reset_timeout = timedelta(minutes=10)
        
        # 请求历史（用于统计）
        # 定长队列，只保留最近100条
        self.request_history = deque(maxlen=100)

        # 充电人数接口按 Cookie 隔离的滑动窗口预算。
        self.cookie_request_history: Dict[str, deque] = {}
        self.cookie_budget_limit = 20
        self.cookie_budget_window = 60.0
        
        # 并发安全锁
        self._lock = asyncio.Lock()
    
    def _add_jitter(self, delay: float) -> float:
        """为延迟添加随机抖动，避免请求模式被识别
        # 将元素加入容器/布局
        
        Args:
            delay: 基础延迟时间(秒)
            
        Returns:
            添加随机抖动后的延迟时间(秒)
            # 将元素加入容器/布局
            
        Example:
            基础延迟 2 秒，抖动因子 0.2：
            - 最小延迟：2 * (1 - 0.2) = 1.6 秒
            - 最大延迟：2 * (1 + 0.2) = 2.4 秒
            - 实际延迟：在 [1.6, 2.4] 之间均匀随机
        """
        if self.jitter_factor <= 0:
            return delay
        
        # 计算抖动范围：[delay * (1 - factor), delay * (1 + factor)]
        # 对输入做运算得到结果
        min_delay = delay * (1 - self.jitter_factor)
        # 计算结果存入 max_delay
        # 对输入做运算得到结果
        max_delay = delay * (1 + self.jitter_factor)
        
        # 在范围内均匀随机
        jittered_delay = random.uniform(min_delay, max_delay)
        
        return jittered_delay
    
    def _refill_tokens(self):
        """补充令牌
        
        按经过的时间线性补充令牌：
        elapsed / rate 秒产生一个令牌，
        上限为 max_tokens，不会无限累积。
        """
        now = time.time()
        # 计算结果存入 elapsed
        # 对输入做运算得到结果
        elapsed = now - self.last_update
        
        # 根据时间间隔补充令牌
        tokens_to_add = elapsed / self.rate
        self.tokens = min(self.max_tokens, self.tokens + tokens_to_add)
        self.last_update = now
    
    async def acquire(self, endpoint: str = "unknown"):
        """获取令牌（等待直到可以发送请求）
        # 读取数据并赋值给当前作用域变量
        
        流程：
        1. 检查熔断状态，熔断中直接抛异常
        # 验证状态/条件，决定下一步分支
        2. 补充令牌（按时间线性累积）
        3. 令牌不足则等待（带随机抖动）
        # 阻塞直到条件满足或超时
        4. 消耗令牌并记录请求历史
        
        Args:
            endpoint: API端点（用于日志）
            
        Note:
            此方法会自动添加随机抖动，避免请求模式被识别
            # 将元素加入容器/布局
        """
        async with self._lock:
            # 检查熔断状态
            # 熔断中直接拒绝，避免继续打爆接口
            if self.is_circuit_open:
                # 计算结果存入 elapsed
                # 对输入做运算得到结果
                elapsed = datetime.now() - self.circuit_open_time
                # 边界/有效性检查
                if elapsed < self.circuit_reset_timeout:
                    # 赋值并准备后续使用
                    remaining = (self.circuit_reset_timeout - elapsed).total_seconds()
                    # 抛出异常中断流程
                    raise CircuitBreakerError(
                        f"熔断保护中，请{int(remaining)}秒后重试"
                    )
                # 分支判断
                else:
                    # 熔断超时，重置状态
                    # 熔断窗口结束，允许重新尝试
                    self._reset_circuit()
            
            # 补充令牌
            # 先按时间补充，再判断是否足够
            # 根据条件走向不同处理分支
            self._refill_tokens()
            
            # 等待令牌
            # 令牌不足 1 个时计算等待时间并睡眠
            # 对输入做运算得到结果
            if self.tokens < 1.0:
                # 计算结果存入 base_wait_time
                # 对输入做运算得到结果
                base_wait_time = (1.0 - self.tokens) * self.rate
                # 添加随机抖动
                # 将元素加入容器/布局
                wait_time = self._add_jitter(base_wait_time)
                logger.debug(f"限频等待 {wait_time:.2f}秒 (基础:{base_wait_time:.2f}秒): {endpoint}")
                # 异步等待结果
                await asyncio.sleep(wait_time)
                # 等待结束后令牌已补满
                # 阻塞直到条件满足或超时
                self.tokens = 1.0
            
            # 消耗令牌
            self.tokens -= 1.0
            
            # 记录请求
            # 追加到定长队列，保留最近100条
            self.request_history.append({
                'endpoint': endpoint,
                'timestamp': datetime.now()
            })
    
    async def acquire_cookie_budget(
        self,
        cookie_key: str,
        budget_key: str = 'charge',
        limit: int = 20,
        window: float = 60.0,
    ) -> None:
        """按 Cookie 对指定业务施加滑动窗口请求预算。

        Args:
            cookie_key: Cookie 的不可逆摘要，不得传入明文 Cookie。
            budget_key: 预算业务名称，当前用于区分充电接口。
            limit: 窗口内最大请求数，默认 20 次。
            window: 滑动窗口秒数，默认 60 秒。

        Returns:
            无；预算耗尽时异步等待最早请求离开窗口。
        """
        try:
            key = f'{budget_key}:{cookie_key}'
            async with self._lock:
                history = self.cookie_request_history.setdefault(key, deque())
                now = time.monotonic()
                while history and now - history[0] >= window:
                    history.popleft()
                if len(history) >= min(limit, self.cookie_budget_limit):
                    wait_seconds = max(window - (now - history[0]), 0.0)
                    logger.info("%s接口触发单Cookie滑动窗口，等待%.2f秒", budget_key, wait_seconds)
                    await asyncio.sleep(wait_seconds)
                    now = time.monotonic()
                    while history and now - history[0] >= window:
                        history.popleft()
                history.append(now)
        except Exception as exc:
            logger.error("%s接口滑动窗口限频失败: %s", budget_key, exc)
            raise

    def report_429(self, endpoint: str) -> int:
        """报告429错误，返回建议的等待时间
        # 将结果交回调用方
        
        递增连续429计数，按计数在退避序列中取
        延迟值，并检查是否触发熔断。
        # 验证状态/条件，决定下一步分支
        
        Args:
            endpoint: API端点
            
        Returns:
            建议等待时间(秒)
            # 阻塞直到条件满足或超时
        """
        self.status_429_count += 1
        self.last_429_time = datetime.now()
        
        # 获取退避延迟
        # 超出序列长度时取最后一位（最长的延迟）
        delay_index = min(self.status_429_count - 1, len(self.retry_delays) - 1)
        # 赋值并准备后续使用
        retry_after = self.retry_delays[delay_index]
        
        logger.warning(
            f"遭遇429限流 (第{self.status_429_count}次): {endpoint}, "
            f"等待{retry_after}秒后重试"
        )
        
        # 记录风控事件
        # 写入 risk_control.log，供后续分析
        from core.logger import logger_manager
        # 判断 logger_manager
        # 根据条件走向不同处理分支
        if logger_manager:
            logger_manager.risk_logger.log_429(
                endpoint=endpoint,
                retry_after=retry_after,
                count=self.status_429_count
            )
        
        # 检查是否需要熔断
        # 连续超过阈值说明接口持续限流，暂停请求
        if self.status_429_count >= self.max_429_count:
            self._trigger_circuit_breaker(
                f"连续{self.status_429_count}次遭遇429"
            )
        
        return retry_after
    
    def report_success(self):
        """报告请求成功（重置429计数）"""
        if self.status_429_count > 0:
            logger.info(f"请求恢复正常，重置429计数: {self.status_429_count} -> 0")
            self.status_429_count = 0
            self.last_429_time = None
    
    def _trigger_circuit_breaker(self, reason: str):
        """触发熔断
        
        置熔断标志并记录风控事件，
        熔断期间 acquire() 会直接抛异常。
        
        Args:
            reason: 熔断原因
        """
        self.is_circuit_open = True
        self.circuit_open_time = datetime.now()
        
        logger.error(f"触发熔断保护: {reason}")
        
        # 记录风控事件
        from core.logger import logger_manager
        # 判断 logger_manager
        # 根据条件走向不同处理分支
        if logger_manager:
            logger_manager.risk_logger.log_circuit_break(reason)
        
        # 抛出异常中断流程
        raise CircuitBreakerError(reason)
    
    def _reset_circuit(self):
        """重置熔断状态
        
        熔断超时后调用，恢复请求并清零计数。
        """
        self.is_circuit_open = False
        self.circuit_open_time = None
        self.status_429_count = 0
        logger.info("熔断状态已重置")
    
    def get_stats(self) -> Dict:
        """获取统计信息
        # 读取数据并赋值给当前作用域变量
        
        Returns:
            统计数据
        """
        now = datetime.now()
        
        # 最近1分钟的请求数
        # 用于监控面板展示实时请求频率
        # 将内容呈现到界面上
        recent_requests = [
            r for r in self.request_history
            # 分支判断
            if (now - r['timestamp']).total_seconds() < 60
        ]
        
        return {
            'rate': self.rate,
            'tokens': self.tokens,
            'status_429_count': self.status_429_count,
            'is_circuit_open': self.is_circuit_open,
            'requests_last_minute': len(recent_requests),
            'total_requests': len(self.request_history)
        }


class MultiEndpointRateLimiter:
    """多端点限频器 - 为不同API端点设置不同的限频策略
    # 写入配置/属性，影响后续行为
    
    内部为每种端点类型维护独立的 RateLimiter，
    acquire/report_429 按端点类型路由到对应实例。
    默认配置：
    - normal: 2.0s（普通接口）
    - comment: 4.0s（评论接口，更严格）
    - dynamic: 2.5s（动态接口）
    """
    
    def __init__(self, config: Dict[str, Dict] = None):
        """初始化多端点限频器
        # 设置初始值/默认状态，避免后续空引用
        # 写入配置/属性，影响后续行为
        
        Args:
            config: 端点配置字典
                {
                    'normal': {'rate': 2.0},
                    'comment': {'rate': 4.0},
                    'dynamic': {'rate': 2.5}
                }
        """
        self.config = config or {
            'normal': {'rate': 2.0},
            'comment': {'rate': 4.0},
            'dynamic': {'rate': 2.5}
        }
        
        # 创建限频器实例
        # 每个端点类型一个独立 RateLimiter
        self.limiters: Dict[str, RateLimiter] = {}
        # 循环遍历处理
        # 对集合内每个元素执行相同处理
        for endpoint_type, settings in self.config.items():
            self.limiters[endpoint_type] = RateLimiter(
                rate=settings.get('rate', 2.0),
                retry_delays=settings.get('retry_delays', [30, 60, 120, 300, 600]),
                max_429_count=settings.get('max_429_count', 5)
            )
        
        # 默认限频器
        # 未识别的端点类型回退到 normal
        self.default_limiter = self.limiters.get('normal')
    
    def get_limiter(self, endpoint_type: str = 'normal') -> RateLimiter:
        """获取指定类型的限频器
        # 读取数据并赋值给当前作用域变量
        
        Args:
            endpoint_type: 端点类型
            
        Returns:
            RateLimiter实例
        """
        return self.limiters.get(endpoint_type, self.default_limiter)
    
    async def acquire(self, endpoint: str, endpoint_type: str = 'normal'):
        """获取令牌
        # 读取数据并赋值给当前作用域变量
        
        Args:
            endpoint: API端点
            endpoint_type: 端点类型
        """
        limiter = self.get_limiter(endpoint_type)
        # 异步等待结果
        await limiter.acquire(endpoint)
    
    def report_429(self, endpoint: str, endpoint_type: str = 'normal') -> int:
        """报告429错误
        
        Args:
            endpoint: API端点
            endpoint_type: 端点类型
            
        Returns:
            建议等待时间(秒)
            # 阻塞直到条件满足或超时
        """
        limiter = self.get_limiter(endpoint_type)
        return limiter.report_429(endpoint)
    
    def report_success(self, endpoint_type: str = 'normal'):
        """报告请求成功
        
        Args:
            endpoint_type: 端点类型
        """
        limiter = self.get_limiter(endpoint_type)
        limiter.report_success()
    
    


# 全局限频器实例
# 惰性初始化，首次 get_rate_limiter() 时创建
_global_rate_limiter: Optional[MultiEndpointRateLimiter] = None


def get_rate_limiter() -> MultiEndpointRateLimiter:
    """获取全局限频器实例
    # 读取数据并赋值给当前作用域变量
    
    从配置读取各端点限频参数并创建
    # 实例化对象并准备使用
    MultiEndpointRateLimiter 单例。
    
    Returns:
        MultiEndpointRateLimiter实例
    """
    global _global_rate_limiter
    # 边界/有效性检查
    if _global_rate_limiter is None:
        # 从 core.config 导入符号
        from core.config import config
        
        # 从配置读取限频设置
        # 各端点独立配置 rate/retry_delays/max_429_count
        rate_limit_config = {
            'normal': {
                'rate': config.get('bilibili.rate_limit.normal', 2.0),
                'retry_delays': config.get('bilibili.rate_limit.retry_429_delays', [30, 60, 120, 300, 600]),
                'max_429_count': config.get('bilibili.rate_limit.max_429_count', 5)
            },
            'comment': {
                'rate': config.get('bilibili.rate_limit.comment', 4.0),
                'retry_delays': config.get('bilibili.rate_limit.retry_429_delays', [30, 60, 120, 300, 600]),
                'max_429_count': config.get('bilibili.rate_limit.max_429_count', 5)
            },
            'dynamic': {
                'rate': config.get('bilibili.rate_limit.dynamic', 2.5),
                'retry_delays': config.get('bilibili.rate_limit.retry_429_delays', [30, 60, 120, 300, 600]),
                'max_429_count': config.get('bilibili.rate_limit.max_429_count', 5)
            }
        }
        
        _global_rate_limiter = MultiEndpointRateLimiter(rate_limit_config)
    
    return _global_rate_limiter