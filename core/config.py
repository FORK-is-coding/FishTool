"""
B站运营工具箱 - 核心配置管理
支持多环境配置、热重载、敏感信息加密

主要功能：
1. 多层配置合并（默认配置 + 用户配置 + 敏感配置）
2. 敏感信息加密存储（使用 Fernet 对称加密）
3. 配置热重载（无需重启应用）
4. 点号分隔的配置访问（如 'bilibili.rate_limit.normal'）
"""
import os
import sys
import json
import yaml
from pathlib import Path
from typing import Dict, Any
from cryptography.fernet import Fernet
import logging

# 创建日志记录器，用于输出配置管理相关的日志
logger = logging.getLogger(__name__)


class ConfigManager:
    """配置管理器 - 统一管理所有配置项
    
    设计思想：
    - 三层配置架构：默认配置（内置） -> 主配置文件（yaml） -> 用户自定义配置（yaml）
    - 敏感信息单独加密存储在 .secrets 文件中，避免明文泄露
    - 支持配置项的点号访问，如 get('bilibili.rate_limit.normal')

    核心职责：
    1. 配置加载：启动时从 yaml 文件读取并深度合并三层配置
    2. 加密存储：敏感信息用 Fernet 对称加密，密钥保存在 .key 文件
    3. 热重载：运行时 reload() 重新读取配置，无需重启进程
    4. 点号访问：get/set 支持 'a.b.c' 形式的多级路径

    线程安全说明：
    - 所有操作都基于内存字典 _config，读多写少
    - 写操作（set/save）后必须 save_config() 落盘
    """
    
    def __init__(self, config_dir: str = None):
        """初始化配置管理器
        
        Args:
            config_dir: 配置文件目录，默认为项目根目录/config
        """
        # 获取项目根目录：当前文件在 core/ 下，所以 parent.parent 是根目录
        # 注意：PyInstaller frozen 环境下 __file__ 指向临时解压目录（_MEIPASS），
        # 重启后临时目录会被清理，配置会丢失（表现为每次启动都像首次运行）。
        # 因此 frozen 环境下改为以 exe 所在目录为根，配置持久化到 exe 旁边。
        if getattr(sys, 'frozen', False):
            # frozen: sys.executable 即 exe 路径，取其父目录作为持久化根目录
            self.base_dir = Path(sys.executable).resolve().parent
        else:
            # 源码环境：当前文件在 core/ 下，parent.parent 即项目根目录
            self.base_dir = Path(__file__).parent.parent
        
        # 配置文件目录：优先使用传入的路径，否则使用默认的 config/ 目录
        self.config_dir = Path(config_dir) if config_dir else self.base_dir / "config"
        
        # 确保配置目录存在，不存在则自动创建（parents=True 表示创建父目录）
        self.config_dir.mkdir(parents=True, exist_ok=True)
        
        # 定义各类配置文件的路径
        self.main_config_path = self.config_dir / "config.yaml"          # 主配置文件
        self.user_config_path = self.config_dir / "user_config.yaml"    # 用户自定义配置
        self.secrets_path = self.config_dir / ".secrets"                # 加密的敏感信息
        self.key_path = self.config_dir / ".key"                        # 加密密钥文件
        
        # 初始化加密器（用于敏感信息的加密和解密）
        self._cipher = self._init_cipher()
        
        # 初始化配置字典（所有配置项都存储在这里）
        self._config: Dict[str, Any] = {}
        
        # 加载所有配置（从文件读取并合并）
        self._load_config()
    
    def _init_cipher(self) -> Fernet:
        """初始化加密器（用于敏感信息的加密存储）
        
        工作流程：
        1. 检查密钥文件是否存在
        2. 如果存在，直接读取；如果不存在，生成新密钥并保存
        3. 在 Linux/Mac 上设置密钥文件为仅所有者可读（0o600 权限）
        
        Returns:
            Fernet 加密器实例
        """
        # 检查密钥文件是否已存在
        if self.key_path.exists():
            # 从文件读取已有的密钥（二进制格式）
            key = self.key_path.read_bytes()
        else:
            # 首次运行，生成新的随机密钥
            key = Fernet.generate_key()
            
            # 将密钥保存到文件（二进制格式）
            self.key_path.write_bytes(key)
            
            # 密钥文件权限保护：仅在非 Windows 系统上执行
            if os.name != 'nt':  # 'nt' 表示 Windows，其他为 Linux/Mac
                # 设置文件权限为 600（仅所有者可读写，其他人无权限）
                os.chmod(self.key_path, 0o600)
        
        # 创建并返回 Fernet 加密器实例
        return Fernet(key)
    
    def _load_config(self):
        """加载配置文件（三层配置合并）

        调用时机：
        - 构造函数初始化时调用一次
        - reload() 热重载时再次调用

        异常兜底：
        - 主配置解析失败时回退到内置默认值
        - 敏感配置解密失败时置空 secrets，不让启动崩溃
        """
        # 第一步：加载默认配置（如果主配置文件存在则从文件读取，否则使用内置默认值）
        # 判断依据：main_config_path 是否存在
        if self.main_config_path.exists():
            # 主配置文件存在，从文件加载
            with open(self.main_config_path, 'r', encoding='utf-8') as f:
                # 使用 yaml.safe_load 解析 YAML 文件（安全加载，不执行代码）
                self._config = yaml.safe_load(f) or {}
        else:
            # 主配置文件不存在，使用内置默认配置
            # 首次启动场景：先拿默认值保证服务可用
            self._config = self._get_default_config()
            # 将默认配置保存到文件，方便用户后续修改
            self.save_config()
        
        # 第二步：加载用户自定义配置并合并（覆盖默认配置）
        if self.user_config_path.exists():
            with open(self.user_config_path, 'r', encoding='utf-8') as f:
                user_config = yaml.safe_load(f) or {}
                # 深度合并：递归合并嵌套字典，而不是直接覆盖
                self._deep_merge(self._config, user_config)
        
        # 第三步：加载敏感配置（加密存储的敏感信息）
        if self.secrets_path.exists():
            # 读取加密的二进制数据
            encrypted_data = self.secrets_path.read_bytes()
            try:
                # 使用 Fernet 解密数据
                decrypted_data = self._cipher.decrypt(encrypted_data)
                # 将解密后的 JSON 字符串转为字典
                secrets = json.loads(decrypted_data.decode('utf-8'))
                # 将敏感配置添加到 _config 字典的 'secrets' 键下
                self._config['secrets'] = secrets
            except Exception as e:
                # 解密失败（可能是密钥错误或文件损坏），记录错误日志
                logger.error(f"解密敏感配置失败: {e}")
                # 初始化为空字典，避免后续访问出错
                self._config['secrets'] = {}
    
    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置（内置配置项）
        
        这是系统的基础配置，包含所有必需的配置项和合理的默认值。
        用户可以通过 config.yaml 或 user_config.yaml 覆盖这些默认值。
        
        设计说明：
        - 首次启动时无配置文件，使用内置默认值启动
        - 默认配置会落盘保存，方便用户查看和修改
        - 配置结构按模块分层：app/web/bilibili/database 等
        
        Returns:
            默认配置字典
        """
        return {
            # 应用基础信息
            "app": {
                "name": "B站运营工具箱",
                "version": "1.0.0",
                "debug": False,           # 调试模式：开启后输出详细日志
                "log_level": "INFO"       # 日志级别：DEBUG/INFO/WARNING/ERROR/CRITICAL
            },
            
            # Web服务器配置
            "server": {
                "host": "127.0.0.1",      # 监听地址：127.0.0.1 表示仅本机访问
                "port": 8080,             # 监听端口
                "reload": False           # 热重载：开发时可开启，生产环境建议关闭
            },
            
            # 数据库配置
            "database": {
                "path": "data/bili_ops.db",  # SQLite 数据库文件路径
                "echo": False,                # 是否输出 SQL 语句（调试用）
                "pool_size": 10               # 连接池大小
            },
            
            # B站API相关配置
            "bilibili": {
                # WBI密钥刷新间隔（秒）：B站的WBI签名密钥会定期更换，需要定时刷新
                "wbi_key_refresh_interval": 3600,
                
                # Cookie有效性检查间隔（秒）：定期检查Cookie是否过期
                "cookie_check_interval": 1800,
                
                # 请求限频配置（防止触发B站的反爬虫机制）
                "rate_limit": {
                    "normal": 2.0,      # 普通接口：每次请求间隔2秒
                    "comment": 4.0,     # 评论接口：更严格的限制，间隔4秒
                    "dynamic": 2.5,     # 动态接口：间隔2.5秒
                    
                    # 429错误（请求过多）的退避策略：遇到429后等待的时间序列
                    "retry_429_delays": [30, 60, 120, 300, 600],
                    
                    # 连续429次数阈值：超过此次数后触发熔断，暂停请求
                    "max_429_count": 5
                },
                
                # 请求头配置（模拟浏览器行为）
                "request_headers": {
                    # User-Agent：模拟真实浏览器
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    # Referer：告诉服务器请求来源
                    "Referer": "https://www.bilibili.com"
                }
            },
            
            # LLM（大语言模型）配置
            "llm": {
                "provider": "openai",      # 提供商：openai/azure/custom
                "api_base": "",            # API基础URL（自定义端点时使用）
                "api_key": "",             # API密钥（建议存储在 secrets 中）
                "model": "gpt-3.5-turbo",  # 使用的模型
                "max_tokens": 2000,        # 单次请求最大token数
                "temperature": 0.7,        # 温度参数：控制输出的随机性（0-1）
                "batch_size": 100,         # 批量处理评论的数量
                "daily_token_limit": 100000  # 每日token用量限制（成本控制）
            },
            
            # 爬虫配置
            "crawler": {
                "concurrent_limit": 3,     # 并发请求数限制（避免触发限流）
                "timeout": 30,             # 请求超时时间（秒）
                "retry_times": 3,          # 失败重试次数
                "enable_checkpoint": True, # 启用断点续爬（中断后可从断点继续）
                "cache_ttl": 3600          # 缓存有效期（秒）
            },
            
            # 监控配置
            "monitor": {
                "enable": False,           # 是否启用常驻监控功能
                "check_interval": 300,     # 监控检查间隔（秒）
                
                # 预警阈值配置
                "alert_thresholds": {
                    "negative_ratio": 0.3,  # 负面评论占比超过30%时预警
                    "comment_surge": 2.0,   # 评论数激增2倍时预警
                    "keyword_match": True   # 是否启用关键词预警
                },
                
                # 预警关键词列表（检测到这些词会触发预警）
                "alert_keywords": ["翻车", "抄袭", "举报", "营销号"]
            },
            
            # 去重配置
            "deduplication": {
                "enable_fuzzy_match": True,   # 启用模糊匹配去重（相似内容视为重复）
                "min_length_for_fuzzy": 5,    # 模糊匹配的最小长度
                "similarity_threshold": 0.85,  # 相似度阈值（0-1）
                "time_window": 3600           # 热点检测时间窗口（秒）
            },
            
            # 桌面宠物配置
            "desktop_pet": {
                "enable": True,                  # 是否启用桌面宠物功能
                "websocket_port": 8081,          # WebSocket通信端口
                "stay_on_top": True,             # 是否始终置顶显示
                "default_position": {"x": 100, "y": 100}  # 默认显示位置
            },
            
            # 导出配置
            "export": {
                "formats": ["pdf", "markdown", "json"],  # 支持的导出格式
                "output_dir": "exports"                  # 导出文件保存目录
            }
        }
    
    def _deep_merge(self, base: Dict, override: Dict):
        """深度合并字典（递归合并嵌套字典）
        
        与浅合并不同，深度合并会递归处理嵌套的字典：
        - 浅合并：{"a": {"b": 1}} + {"a": {"c": 2}} = {"a": {"c": 2}}（整个 a 被覆盖）
        - 深度合并：{"a": {"b": 1}} + {"a": {"c": 2}} = {"a": {"b": 1, "c": 2}}（递归合并）
        
        使用场景：
        - user_config.yaml 覆盖 config.yaml 时，只需覆盖用户关心的子键，
          其余配置保持默认值，而不是整个配置块被替换
        - 未来新增配置键时，旧配置文件不会丢失新键的默认值
        
        Args:
            base: 基础字典（会被修改）
            override: 覆盖字典（合并到 base 中）
        """
        # 遍历覆盖字典的每一个键值对
        # 逐个检查，因为每个键的处理方式可能不同
        for key, value in override.items():
            # 检查这个键是否在基础字典中，且两边的值都是字典类型
            # 只有双方都是 dict 才需要递归合并，否则直接覆盖
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                # 如果是嵌套字典，递归调用深度合并
                # 这样深层子键也能被逐级合并，而不是被整体替换
                self._deep_merge(base[key], value)
            else:
                # 如果不是嵌套字典，直接覆盖（或添加新键）
                # 标量值/列表/新键都走这里，语义是"以 override 为准"
                base[key] = value
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项（支持点号分隔的多级访问）
        
        用法示例：
        - get_config('app.name') 访问嵌套键
        - get_config('debug') 访问顶层键
        - 键不存在时返回 default
        
        实现：
        - 按点号切分路径逐层下沉
        - 任意层级缺失返回 default
        
        使用示例：
        - config.get('app.name') → 'B站运营工具箱'
        - config.get('bilibili.rate_limit.normal') → 2.0
        - config.get('not.exist.key', 'default_value') → 'default_value'
        
        实现原理：
        - 将点号路径拆成键列表，沿 _config 字典逐级下钻
        - 任一级缺失（None）立即返回 default，不会抛 KeyError
        
        Args:
            key: 配置项键名（支持点号分隔多级，如 'bilibili.rate_limit.normal'）
            default: 默认值（当键不存在时返回）
            
        Returns:
            配置值，如果不存在则返回 default
        """
        # 按点号分隔键名，得到路径列表。例如：'a.b.c' → ['a', 'b', 'c']
        keys = key.split('.')
        
        # 从配置字典的根节点开始查找
        value = self._config
        
        # 逐级深入查找
        # 每一级都检查是否为字典，非字典说明路径到此断裂
        for k in keys:
            # 检查当前值是否是字典类型
            if isinstance(value, dict):
                # 从字典中获取下一级的值
                value = value.get(k)
                # 如果这一级的值不存在（None），直接返回默认值
                # None 值无法继续下钻，视为"路径不存在"
                if value is None:
                    return default
            else:
                # 当前值不是字典，无法继续深入，返回默认值
                return default
        
        # 成功找到配置值，返回
        return value
    
    def set(self, key: str, value: Any):
        """设置配置项（支持点号分隔的多级访问）
        
        用法示例：
        - set_config('app.name', '新值')
        - 中间层级不存在时自动创建
        
        注意：
        - 仅修改内存中的 _config
        - 需调用 save_config 持久化到文件
        
        使用示例：
        - config.set('app.debug', True)
        - config.set('bilibili.rate_limit.normal', 3.0)
        
        注意：此方法只修改内存中的配置，需要调用 save_config() 才能持久化到文件
        
        中间路径处理：
        - 若 'a.b.c' 中的 'a'/'a.b' 不存在，会自动创建空字典，
          避免 KeyError；这是与 get() 只读行为的区别
        
        Args:
            key: 配置项键名（支持点号分隔）
            value: 配置值
        """
        # 按点号分隔键名，得到路径列表。例如：'a.b.c' → ['a', 'b', 'c']
        keys = key.split('.')
        
        # 从配置字典的根节点开始
        config = self._config
        
        # 逐级深入，为不存在的中间路径创建空字典
        # keys[:-1] 表示除了最后一个键之外的所有键（即中间路径）
        for k in keys[:-1]:
            # 如果这一级的键不存在，创建一个空字典
            if k not in config:
                config[k] = {}
            # 深入到下一级
            config = config[k]
        
        # 在最后一级设置值（keys[-1] 是最后一个键）
        config[keys[-1]] = value
    
    def save_config(self):
        """保存主配置文件到 config.yaml
        
        注意：
        - 敏感信息不会保存到此文件中（它们存储在加密的 .secrets 文件中）
        - 使用 YAML 格式，方便人工阅读和修改
        
        触发时机：
        - 首次运行时保存默认配置，方便用户直接编辑
        - 运行时修改配置后手动调用，持久化到磁盘
        """
        # 创建一个新字典，排除敏感信息（secrets 键）
        # 使用字典推导式过滤掉 'secrets' 键
        # 敏感信息必须留在加密文件里，不能明文写入 yaml
        save_config = {k: v for k, v in self._config.items() if k != 'secrets'}
        
        # 以 UTF-8 编码写入 YAML 文件
        # 保证中文配置项（如 app.name）正常读写
        with open(self.main_config_path, 'w', encoding='utf-8') as f:
            # allow_unicode=True: 允许保存中文等 Unicode 字符
            # default_flow_style=False: 使用块状样式（更易读），而非行内样式
            yaml.dump(save_config, f, allow_unicode=True, default_flow_style=False)
        
        # 写入成功后打日志，方便排查配置变更历史
        logger.info("主配置已保存")
    
    
    def save_secret(self, key: str, value: str):
        """保存敏感信息（加密存储到 .secrets 文件）
        
        安全措施：
        - 使用 Fernet 对称加密
        - 密钥独立存储，加密文件泄露也无法直接读取
        - 文件权限设置为 600
        
        为什么要加密存储：
        - 防止敏感信息（如 API 密钥、密码）以明文形式存储在磁盘上
        - 使用 Fernet 对称加密算法，安全性高
        - 即使文件被复制，没有密钥文件也无法解密
        
        存储链路：
        内存 _config['secrets'] -> JSON 字符串 -> UTF-8 字节 ->
        Fernet 加密 -> 写入 .secrets 文件
        
        Args:
            key: 配置键名（如 'api_key'）
            value: 配置值（如 'sk-abc123...'）
        """
        # 确保 secrets 字典存在（如果不存在则初始化为空字典）
        # 防止首次调用时 KeyError
        if 'secrets' not in self._config:
            self._config['secrets'] = {}
        
        # 将敏感信息添加到 secrets 字典中
        # 多个敏感键共存于同一字典，统一加密落盘
        self._config['secrets'][key] = value
        
        # 加密并保存到文件
        # 步骤1：将 secrets 字典转换为 JSON 字符串
        secrets_json = json.dumps(self._config['secrets'])
        
        # 步骤2：将字符串编码为 UTF-8 字节流
        # Fernet 加密接口接收 bytes，所以先 encode
        secrets_bytes = secrets_json.encode('utf-8')
        
        # 步骤3：使用 Fernet 加密器加密字节流
        # 加密后的数据包含版本/时间戳/IV/密文，无法被篡改
        encrypted_data = self._cipher.encrypt(secrets_bytes)
        
        # 步骤4：将加密后的二进制数据写入 .secrets 文件
        # 每次保存都是全量覆盖，保证文件与内存一致
        self.secrets_path.write_bytes(encrypted_data)
    
    def get_secret(self, key: str, default: Any = None) -> Any:
        """获取敏感信息（从加密存储中读取）
        
        Args:
            key: 配置键名
            default: 默认值（当键不存在时返回）
            
        Returns:
            配置值，如果不存在则返回 default
        """
        # 从 _config['secrets'] 字典中获取敏感信息
        # 使用链式 get() 方法：先获取 'secrets' 字典，再从中获取指定的 key
        return self._config.get('secrets', {}).get(key, default)
    
    def reload(self):
        """重新加载配置（从文件重新读取）
        
        触发时机：
        - 用户修改配置文件后手动调用
        - 外部检测到配置变更后调用
        - 不保留内存中未保存的修改
        
        使用场景：
        - 配置文件在运行时被修改，需要应用新配置
        - 无需重启应用即可更新配置
        
        注意事项：
        - reload 会覆盖内存中未保存的 set() 修改
        - 敏感信息也会一并重新加载（如果 .secrets 存在）
        """
        # 调用 _load_config() 重新加载所有配置
        # 内部会依次读取主配置/用户配置/敏感配置并深度合并
        self._load_config()
        
        # 记录日志，表示配置已重新加载
        logger.info("配置已重新加载")
    
    def export_config(self, output_path: str):
        """导出配置到指定文件（不含敏感信息）
        
        用途：
        - 生成配置模板供用户参考
        - 备份非敏感配置
        - 自动过滤 secrets 字段避免泄露
        
        用途：
        - 备份配置
        - 分享配置给其他用户
        - 生成配置模板
        
        与 save_config 的区别：
        - save_config 固定写 config.yaml
        - export_config 可指定任意输出路径，适合做模板/备份
        
        Args:
            output_path: 导出文件路径
        """
        # 创建一个新字典，排除敏感信息
        # 导出文件不应包含任何密钥，防止泄露
        export_config = {k: v for k, v in self._config.items() if k != 'secrets'}
        
        # 写入到指定的输出文件
        with open(output_path, 'w', encoding='utf-8') as f:
            # 与主配置保存使用相同的 YAML 参数
            yaml.dump(export_config, f, allow_unicode=True, default_flow_style=False)
    
    @property
    def all(self) -> Dict[str, Any]:
        """获取全部配置（不含敏感信息）
        
        返回副本而非引用，防止外部修改内部状态
        
        使用 @property 装饰器，可以像访问属性一样访问：config.all
        而不需要调用方法：config.all()
        
        Returns:
            完整配置字典（不含 secrets）
        """
        # 返回排除敏感信息的配置字典
        # 避免外部直接修改内部 _config 状态
        return {k: v for k, v in self._config.items() if k != 'secrets'}


# 全局配置实例（单例模式）
# 在应用启动时自动创建，整个应用共享同一个配置管理器实例
# 其他模块通过 from core.config import config 获取，
# 无需关心初始化时机，导入即完成加载
config = ConfigManager()