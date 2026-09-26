"""
B站运营工具箱 - 分级日志系统
支持多级日志、风控事件专用标记、日志导出、结构化日志

本模块提供统一的日志基础设施，包含：

一、日志级别与风控级别枚举
- LogLevel: DEBUG/INFO/WARNING/ERROR/CRITICAL 标准五级
- RiskControlLevel: 风控专用级别（限频/429/Cookie失效/IP封禁/
  账号风险/验证码/熔断），用于标记需要特别关注的爬虫风险事件

二、格式化器
- ColoredFormatter: 控制台彩色输出，Windows 通过 ANSI 转义
  序列实现（模块加载时全局启用）
- StructuredFormatter: JSON 结构化输出，写入 risk_control.log，
  便于机器解析与事后检索

三、风控日志记录器 RiskControlLogger
- 独立的 risk_control.log，按天轮转保留 30 天
- log_event 记录结构化风控事件
- 提供 log_429/log_cookie_expired/log_circuit_break 便捷方法

四、总管理器 LoggerManager
- 根 logger 统一配置：控制台 + app.log（10MB 轮转×5）
  + error.log（仅 ERROR 级，10MB 轮转×3）
- 爬虫专用 logger：crawler.log（10MB 轮转×5）
- 日志导出：按类型/日期区间导出合并文件
- 风控事件查询：按时间/级别过滤
- 旧日志清理：按保留天数自动删除

五、全局入口
- init_logger(): 初始化全局 LoggerManager
- get_logger(name): 获取命名 logger（未初始化时自动初始化）

线程与异步说明：
- 标准 logging 库是线程安全的，无需额外加锁
- 各模块通过 get_logger(__name__) 获得独立命名空间，
  日志中自带模块名便于定位

扩展指引：
- 新增风控事件类型：在 RiskControlLevel 枚举中添加，
  并在 RiskControlLogger 中补充便捷方法
- 新增日志文件：在 LoggerManager 中仿照 crawler logger 添加

典型用法：
    from core.logger import get_logger
    logger = get_logger(__name__)
    logger.info("...")
"""
import os
import sys
import logging
import json
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from enum import Enum


class LogLevel(str, Enum):
    """日志级别

    标准日志级别，用于控制各 handler 的过滤阈值。

    级别由低到高：
    - DEBUG: 调试信息，仅在开发阶段使用
    - INFO: 常规运行信息，默认级别
    - WARNING: 警告，不影响运行但需关注
    - ERROR: 错误，功能失败但进程存活
    - CRITICAL: 致命错误，需要立即处理

    用法：LoggerManager(log_level='DEBUG') 即输出全部级别。
    """
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class RiskControlLevel(str, Enum):
    """风控事件级别

    专门标记爬虫运行中的风险事件类型，
    便于后续统计与告警。

    覆盖的风控场景：
    - RATE_LIMIT: 通用限频，命中频控策略
    - STATUS_429: HTTP 429，B 站明确拒绝请求
    - COOKIE_EXPIRED: Cookie 失效，需要重新登录
    - IP_BANNED: IP 被封禁
    - ACCOUNT_RISK: 账号被风控标记
    - CAPTCHA: 出现验证码，说明触发风控
    - CIRCUIT_BREAK: 连续触发风控，熔断暂停请求

    各事件通过 RiskControlLogger.log_event 记录到独立文件。
    """
    RATE_LIMIT = "RATE_LIMIT"      # 限频
    STATUS_429 = "STATUS_429"       # 429错误
    COOKIE_EXPIRED = "COOKIE_EXPIRED"  # Cookie失效
    IP_BANNED = "IP_BANNED"         # IP被封
    ACCOUNT_RISK = "ACCOUNT_RISK"   # 账号风险
    CAPTCHA = "CAPTCHA"             # 验证码
    CIRCUIT_BREAK = "CIRCUIT_BREAK" # 熔断


# Windows ANSI 颜色支持全局初始化（仅执行一次）
# 在 Windows 上调用 os.system('') 会启用控制台的 ANSI 转义支持，
# 否则彩色日志会输出原始转义码
if sys.platform == 'win32':
    os.system('')  # 启用 ANSI 转义序列支持


class ColoredFormatter(logging.Formatter):
    """彩色日志格式化器（控制台输出）
    
    提供彩色日志输出，支持 Windows ANSI 转义序列。
    Windows ANSI 支持已在模块加载时全局初始化，避免每次格式化时重复调用。
    
    颜色映射：
    - DEBUG: 青色（36）
    - INFO: 绿色（32）
    - WARNING: 黄色（33）
    - ERROR: 红色（31）
    - CRITICAL: 红底白字（41;37）
    
    实现方式：
    - format() 中临时改写 record.levelname 为带色码的字符串
    - 格式化完成后调用 super().format() 输出
    - 只影响控制台 handler，文件 handler 用普通 Formatter
    """
    
    # 各日志级别对应的 ANSI 颜色码
    COLORS = {
        'DEBUG': '\033[36m',      # 青色
        'INFO': '\033[32m',       # 绿色
        'WARNING': '\033[33m',    # 黄色
        'ERROR': '\033[31m',      # 红色
        'CRITICAL': '\033[35m',   # 紫色
        'RESET': '\033[0m'
    }
    
    def format(self, record):
        """格式化日志记录，添加颜色代码
        
        临时修改 record.levelname 为带颜色的形式，
        格式化完成后通过 super().format 输出。
        
        实现说明：
        - 只对已知级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）着色
        - 修改的是 record 副本的 levelname，不影响原始数据
        - 颜色码只在控制台输出中出现，文件日志保持纯文本
        
        Args:
            record: 日志记录对象
            
        Returns:
            格式化后的彩色日志字符串
        """
        levelname = record.levelname
        if levelname in self.COLORS:
            # 用颜色码包裹级别名，例如 \033[32mINFO\033[0m
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.COLORS['RESET']}"
        
        return super().format(record)


class StructuredFormatter(logging.Formatter):
    """结构化日志格式化器（JSON格式）

    将日志记录序列化为 JSON 行，便于机器解析。
    额外支持：
    - record.extra_data 附加字段（风控事件详情）
    - exc_info 异常堆栈

    输出结构：
    {
      "timestamp": "2026-08-19T22:00:01.123456",
      "level": "WARNING",
      "logger": "risk_control",
      "message": "事件描述",
      "module": "cookie_pool",
      "function": "load",
      "line": 120,
      "extra": {...},      # 可选，风控结构化数据
      "exception": "..."   # 可选，异常堆栈
    }
    
    读取方：
    - get_risk_events() 逐行解析还原事件
    - 外部日志平台可直接消费 JSON 行
    """

    @staticmethod
    def _serialize(value: object) -> str:
        """将日志字段转换为稳定文本，避免字典等对象使用 Python 表示法。"""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)

    def format(self, record):
        """格式化日志记录为 JSON 行

        每次调用构建一条 JSON 日志，包含：
        - 基础字段：时间/级别/logger名/消息/位置
        - 可选 extra：风控事件结构化数据
        - 可选 exception：异常堆栈

        Returns:
            JSON 字符串（ensure_ascii=False 保留中文）
        """
        # 基础字段：时间/级别/logger名/消息/位置信息
        log_data = {
            'timestamp': datetime.fromtimestamp(record.created).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': self._serialize(record.getMessage()),
            'module': self._serialize(record.module),
            'function': self._serialize(record.funcName),
            'line': record.lineno
        }
        
        # 添加额外字段
        # 风控事件通过 LogRecord 的 extra_data 属性传递结构化数据
        if hasattr(record, 'extra_data'):
            log_data['extra'] = record.extra_data
        
        # 异常信息
        # 有异常时调用 formatException 获取完整堆栈
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)
        
        return json.dumps(log_data, ensure_ascii=False)


class RiskControlLogger:
    """风控事件专用日志记录器

    独立于主日志体系，专门记录风控事件。
    日志写入 risk_control.log（JSON 格式，按天轮转保留30天），
    方便后续用 get_risk_events 查询分析。

    设计要点：
    - 使用独立 logger 名 'risk_control'，propagate=False
      避免风控事件冒泡到根 logger 造成重复输出
    - 使用 StructuredFormatter 输出 JSON 行，便于机器解析
    - 文件按天轮转（TimedRotatingFileHandler），保留 30 天备份
    - 事件数据通过 LogRecord.extra_data 传递，
      StructuredFormatter 读取后写入 JSON 的 extra 字段

    便捷方法：
    - log_429: 记录 HTTP 429 限流
    - log_cookie_expired: 记录 Cookie 失效
    - log_circuit_break: 记录熔断事件
    """
    
    def __init__(self, log_dir: Path):
        """初始化风控日志记录器

        Args:
            log_dir: 日志输出目录
        """
        # 日志目录与文件路径
        self.log_dir = log_dir
        self.risk_log_path = log_dir / "risk_control.log"
        # 确保目录存在
        self.risk_log_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 创建风控专用logger
        # propagate=False 防止事件冒泡到根logger重复输出
        self.logger = logging.getLogger('risk_control')
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        
        # 文件处理器 - 按日期轮转
        # 每天午夜切换新文件，保留30个备份
        # 轮转按自然日对齐，便于按天检索风控记录
        file_handler = TimedRotatingFileHandler(
            self.risk_log_path,
            when='midnight',
            interval=1,
            backupCount=30,
            encoding='utf-8'
        )
        file_handler.setFormatter(StructuredFormatter())
        self.logger.addHandler(file_handler)
    
    def log_event(self, 
                  level: RiskControlLevel,
                  message: str,
                  endpoint: Optional[str] = None,
                  status_code: Optional[int] = None,
                  retry_after: Optional[int] = None,
                  extra: Optional[Dict[str, Any]] = None):
        """记录风控事件
        
        组装结构化事件数据，通过 LogRecord 的 extra_data
        属性传递给 StructuredFormatter。
        
        与标准 logger 的区别：
        - 不直接调用 logger.warning(...)，而是手动构造 LogRecord，
          以便挂载 extra_data 结构化字段
        - 事件以 JSON 字符串作为 message 存储，
          StructuredFormatter 读取 extra_data 还原结构化数据
        
        Args:
            level: 风控级别
            message: 事件描述
            endpoint: API端点
            status_code: HTTP状态码
            retry_after: 重试延迟(秒)
            extra: 额外信息
        """
        # 组装事件数据字典
        # 顶层字段用于快速检索，extra 用于业务详情
        # 时间戳统一用 ISO 格式便于外部系统解析
        event_data = {
            'risk_level': level.value,
            'message': message,
            'timestamp': datetime.now().isoformat(),
            'endpoint': endpoint,
            'status_code': status_code,
            'retry_after': retry_after
        }
        
        # 附加业务字段
        # extra 通常包含 cookie 名、熔断原因等上下文
        # 有额外信息才写入，避免事件数据冗余
        if extra:
            event_data['extra'] = extra
        
        # 事件数据组装完成，进入记录阶段
        # 使用LogRecord的extra属性传递结构化数据
        # makeRecord 手动构造记录，message 存 JSON 字符串
        log_record = self.logger.makeRecord(
            self.logger.name,
            logging.WARNING,
            "(unknown file)", 0,
            json.dumps(event_data, ensure_ascii=False),
            (), None
        )
        log_record.extra_data = event_data
        # Formatter 读取 extra_data 还原结构化字段
        # 最终以 JSON 行写入 risk_control.log
        self.logger.handle(log_record)
    
    def log_429(self, endpoint: str, retry_after: int, count: int = 1):
        # 429 限流是风控前兆，需记录接口与等待时间
        
        """记录429错误
        
        专门记录 HTTP 429 限流事件，带重试等待时间。
        

        应对策略：
        - 等待 retry_after 秒后重试
        - 连续多次触发时考虑降速或切换账号
        触发场景：
        - B 站接口返回 429 Too Many Requests
        - 连续请求速度超过频控阈值
        
        Args:
            endpoint: 被限流的接口
            retry_after: 建议等待秒数
            count: 当前连续429次数
        """
        self.log_event(
            RiskControlLevel.STATUS_429,
            f"遭遇429限流，第{count}次",
            endpoint=endpoint,
            status_code=429,
            retry_after=retry_after
        )
    
    def log_cookie_expired(self, cookie_name: str):
        """记录Cookie失效
        
        Args:
            cookie_name: 失效的Cookie名称
        
        触发场景：
        - 账号 Cookie 过期或失效
        - B 站端要求重新登录
        - 更换设备导致登录态失效
        """
        self.log_event(
            RiskControlLevel.COOKIE_EXPIRED,
            f"Cookie已失效: {cookie_name}",
            extra={'cookie': cookie_name}
        )
    
    def log_circuit_break(self, reason: str):
        """记录熔断事件
        
        Args:
            reason: 熔断触发原因
        
        触发场景：
        - 连续请求失败达到熔断阈值
        - 保护账号避免风控升级
        - 熔断期间接口直接短路返回
        """
        self.log_event(
            RiskControlLevel.CIRCUIT_BREAK,
            f"触发熔断: {reason}",
            extra={'reason': reason}
        )


class LoggerManager:
    """日志管理器 - 统一管理所有日志

    负责根 logger 的初始化（控制台+文件 handlers），
    提供命名 logger 获取、爬虫 logger、日志导出、
    风控事件查询、旧日志清理等能力。

    管理结构：
    - 根 logger（logging.getLogger() 无参）：全局默认，所有模块日志
      默认冒泡到这里，配置控制台 + app.log + error.log 三个 handler
    - 'crawler' logger：爬虫模块专用，独立写 crawler.log
    - 'risk_control' logger：风控事件专用，独立写 risk_control.log（JSON）

    Handler 规划：
    - 控制台 StreamHandler：彩色输出，级别 = log_level
    - app.log RotatingFileHandler：10MB 轮转保留 5 个，级别 = log_level
    - error.log RotatingFileHandler：只收 ERROR+，10MB 轮转保留 3 个
    - crawler.log / risk_control.log：子 logger 各自持有

    初始化幂等性：
    - _setup_root_logger 会先清空根 logger 已有 handlers，
      避免重复初始化导致日志重复输出
    - get_crawler_logger 判断 handlers 为空才添加，避免重复
    """

    def __init__(self, log_dir: str = "data/logs", log_level: str = "INFO"):
        """初始化日志管理器
        
        Args:
            log_dir: 日志目录
            log_level: 日志级别
        """
        self.log_dir = Path(log_dir)
        # 确保日志目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 字符串级别名转 logging 常量
        self.log_level = getattr(logging, log_level.upper())
        
        # 日志文件路径（必须在 _setup_root_logger 之前定义）
        # 各文件职责：
        # - app.log: 全量业务日志
        # - error.log: 仅错误日志，排查用
        # - crawler.log: 爬虫过程日志
        self.app_log_path = self.log_dir / "app.log"
        self.error_log_path = self.log_dir / "error.log"
        self.crawler_log_path = self.log_dir / "crawler.log"
        
        # 配置根logger
        # 内部会设置控制台 + app.log + error.log 三个 handler
        self._setup_root_logger()
        
        # 创建风控日志记录器
        # 独立文件 risk_control.log，JSON 格式
        self.risk_logger = RiskControlLogger(self.log_dir)
    
    def _setup_root_logger(self):
        """配置根logger

        配置根 logger 的三个 handler：
        1. 控制台：彩色输出，全级别
        2. app.log：按大小轮转（10MB×5），全级别
        3. error.log：仅 ERROR 及以上，按大小轮转（10MB×3）

        实现细节：
        - 必须先清空已有 handlers，防止重复初始化时堆积
        - 控制台与文件 handler 共享同一套格式化模板，
          但控制台额外套一层颜色
        - error.log 复用 app_formatter，保证文件格式统一
        """
        root_logger = logging.getLogger()
        root_logger.setLevel(self.log_level)
        
        # 清除已有处理器
        # 避免重复初始化时 handler 堆积导致重复输出
        root_logger.handlers.clear()
        
        # 控制台处理器 - 彩色输出
        # StreamHandler 默认输出到 stderr，这里显式指定 stdout
        # Bug2 辅助修复：PyInstaller windowed(console=False) 模式下
        # sys.stdout/sys.stderr 均为 None，直接创建 StreamHandler 会导致
        # 每次日志 emit 都抛 AttributeError（被 handleError 吞掉但浪费性能），
        # 因此无控制台流时跳过控制台 handler，只保留文件 handler
        console_handler = None
        if sys.stdout is not None:
            console_handler = logging.StreamHandler(sys.stdout)
        elif sys.stderr is not None:
            console_handler = logging.StreamHandler(sys.stderr)
        if console_handler is not None:
            console_handler.setLevel(self.log_level)
            console_formatter = ColoredFormatter(
                '%(asctime)s [%(levelname)s] %(name)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            console_handler.setFormatter(console_formatter)
            root_logger.addHandler(console_handler)
        
        # 应用日志文件处理器 - 按大小轮转
        # maxBytes=10MB，超过后滚动备份，保留5个
        app_file_handler = RotatingFileHandler(
            self.app_log_path,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        app_file_handler.setLevel(self.log_level)
        app_formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s [%(filename)s:%(lineno)d] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        app_file_handler.setFormatter(app_formatter)
        root_logger.addHandler(app_file_handler)
        
        # 错误日志文件处理器 - 只记录ERROR及以上
        # 独立文件方便快速排查错误
        # 与 app.log 共享同一格式化器，保持格式一致
        error_file_handler = RotatingFileHandler(
            self.error_log_path,
            maxBytes=10*1024*1024,
            backupCount=3,
            encoding='utf-8'
        )
        error_file_handler.setLevel(logging.ERROR)
        error_file_handler.setFormatter(app_formatter)
        root_logger.addHandler(error_file_handler)
    
    def get_logger(self, name: str) -> logging.Logger:
        # 按名字获取子 logger，自动继承根配置
        
        """获取命名logger
        
        Args:
            name: logger名称
            
        Returns:
            Logger实例
        """
        # 标准库按名缓存 logger，同名多次调用返回同一实例
        return logging.getLogger(name)
    
    def get_crawler_logger(self) -> logging.Logger:
        """获取爬虫专用logger
        
        爬虫日志单独落盘 crawler.log，
        与业务日志分离，便于排查采集问题。
        独立文件+独立轮转策略。
        
        爬虫日志写入独立文件 crawler.log，
        与业务日志分离，便于单独跟踪爬取过程。

        适用模块：
        - modules/comment/collector.py
        - modules/up_analyzer/data_fetcher.py
        - bilibili/* 的 API 请求层

        使用示例：
            logger = logger_manager.get_crawler_logger()
            logger.info(f"抓取视频 {bvid} 完成")
        """
        # 独立 logger 名 crawler，避免与业务日志混用
        crawler_logger = logging.getLogger('crawler')
        # 级别与主 logger 保持一致，便于统一调整
        crawler_logger.setLevel(self.log_level)
        
        # 避免重复添加处理器
        # 多次调用 get_crawler_logger 不会叠加 handler
        # 多次调用时只添加一次 handler
        # 否则每次调用都会新增一个文件 handler，导致日志重复写入
        if not crawler_logger.handlers:
            # 爬虫日志文件处理器
            crawler_handler = RotatingFileHandler(
                self.crawler_log_path,
                maxBytes=10*1024*1024,
                backupCount=5,
                encoding='utf-8'
            )
            crawler_handler.setLevel(self.log_level)
            formatter = logging.Formatter(
                '%(asctime)s [%(levelname)s] %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            crawler_handler.setFormatter(formatter)
            crawler_logger.addHandler(crawler_handler)
        
        # 调用方直接 logger.info/warning 即可，无需关心 handler
        return crawler_logger
    
    def export_logs(self, 
                    start_date: Optional[datetime] = None,
                    end_date: Optional[datetime] = None,
                    log_types: Optional[list] = None,
                    output_path: Optional[str] = None) -> str:
        """导出日志
        
        将指定类型的日志合并导出到单个文件，
        支持按日期区间过滤（按行首时间戳解析）。
        
        导出格式：
        - 每种日志类型前加分隔标题（==== 类型名 ====）
        - 默认导出全部四种日志（app/error/crawler/risk）
        - 输出文件默认放在 data/logs/export_时间戳.log
        
        Args:
            start_date: 起始日期
            end_date: 结束日期
            log_types: 日志类型列表 ['app', 'error', 'crawler', 'risk']
            output_path: 输出文件路径
            
        Returns:
            导出文件路径
        """
        # 默认导出全部类型
        # None 时兜底为四种日志，保证调用方不传参也能用
        if log_types is None:
            log_types = ['app', 'error', 'crawler', 'risk']
        
        # 未指定输出路径时按时间戳生成
        # 默认导出目录为 data/logs
        # 时间戳精确到秒，避免覆盖同名导出文件
        if output_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = self.log_dir / f"export_{timestamp}.log"
        
        # 统一转成 Path 对象，兼容 str 与 Path 入参
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 逐类型追加写入导出文件
        # 写模式打开：每次导出重新生成，不追加旧数据
        with open(output_path, 'w', encoding='utf-8') as outfile:
            for log_type in log_types:
                # 按类型逐个处理，保证导出文件顺序稳定
                log_file = self.log_dir / f"{log_type}.log"
                # 跳过不存在的日志文件
                if not log_file.exists():
                    continue
                
                # 每个类型加分隔标题
        # 分隔线 80 个等号，方便肉眼区分区块
                # 便于在导出文件中快速定位各类型日志
                outfile.write(f"\n{'='*80}\n")
                outfile.write(f"日志类型: {log_type.upper()}\n")
                outfile.write(f"{'='*80}\n\n")
                
                with open(log_file, 'r', encoding='utf-8') as infile:
                    # 按行读取避免大文件全量载入内存
                    for line in infile:
                        # 简单的日期过滤
                        # 按行首 'YYYY-MM-DD HH:MM:SS' 格式解析时间戳
                        if start_date or end_date:
                            try:
                                # 解析日志时间戳
                                # 标准格式形如 '2026-08-19 22:00:01 [INFO] ...'
                                timestamp_str = line.split('[')[0].strip()
                                log_time = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                                
                                if start_date and log_time < start_date:
                                    continue
                                if end_date and log_time > end_date:
                                    continue
                            except:
                                pass  # 解析失败，保留该行
        
                        outfile.write(line)
        
        return str(output_path)
    
    


# 全局日志管理器实例
# 通过 init_logger() 初始化，get_logger() 懒加载使用
# 模块级单例：整个进程共享同一个 LoggerManager，
# 避免重复创建文件 handler 导致日志重复
logger_manager: Optional[LoggerManager] = None


def init_logger(log_dir: str = "data/logs", log_level: str = "INFO") -> LoggerManager:
    """初始化全局日志管理器
    
    模块级单例入口，整个应用共享一个 LoggerManager。
    业务代码通过 get_logger() 获取子 logger，
    无需关心初始化细节。
    
    Args:
        log_dir: 日志目录
        log_level: 日志级别
        
    Returns:
        LoggerManager实例
    """
    global logger_manager
    # 创建 LoggerManager 实例并赋给全局变量
    # 之后 get_logger 可以直接复用，无需重复初始化
    logger_manager = LoggerManager(log_dir, log_level)
    return logger_manager


def get_logger(name: str) -> logging.Logger:
    """获取logger实例
    
    未初始化时自动调用 init_logger() 使用默认配置。
    
    Args:
        name: logger名称
        
    Returns:
        Logger实例
    """
    if logger_manager is None:
        # 懒初始化：首次调用时用默认配置创建
        # 后续调用直接复用全局实例
        init_logger()
    return logger_manager.get_logger(name)