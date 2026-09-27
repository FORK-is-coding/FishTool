"""
B站API封装 - Cookie池管理
支持多账号轮换、自动检查有效性、失效提醒

本模块是爬虫身份管理的核心组件，负责：
1. Cookie 加载：从数据库加载加密存储的 Cookie，
# 从存储/网络读入数据
   兼容旧版本明文数据（自动解密失败后回退明文并加密迁移）
2. 数据库加载：从加密存储读取已有 Cookie 并兼容旧版明文记录
3. 轮换策略：get_cookie() 按轮转下标从有效 Cookie 中
   选取，避免单账号频繁请求触发风控
4. 有效性检查：调用 B站 nav 接口验证登录态，
   支持手动全量检查与后台自动循环检查
5. 失效处理：mark_invalid 同步更新数据库并记录风控事件

设计要点：
- 线程安全：内存池操作统一走 asyncio.Lock
- 加密存储：cookie_data 字段用 Fernet 加密后入库
- 惰性初始化：get_cookie_pool() 首次调用时才建池并加载
- 兼容旧数据：解密失败的记录按明文处理并回写加密
# 对数据进行加工/分发

依赖：
- core.database: Account/CookiePool 模型与 db_manager
- core.config.ConfigManager: 密钥管理与加密器
- core.exceptions: CookieExpiredError/AuthenticationError
- bilibili.api.BilibiliAPI: 登录态验证
"""
import asyncio
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from cryptography.fernet import Fernet
import logging

from core.database import get_session, Account, CookiePool as CookiePoolModel
from core.exceptions import CookieExpiredError, AuthenticationError
from core.logger import get_logger
from bilibili.api import BilibiliAPI

logger = get_logger(__name__)


class Cookie:
    """Cookie对象

    内存中的 Cookie 表示，对应数据库 CookiePool 表的一行。
    包含解密后的完整 cookie_data 字符串与拆分的
    sessdata/bili_jct/buvid3 字段，以及有效性状态、
    最后使用时间、失败计数等运行时信息。

    字段说明：
    - id: 数据库主键，用于回写状态
    - account_id: 所属账号
    - cookie_data: 解密后的完整 Cookie 串
    - sessdata/bili_jct/buvid3: 请求头组装关键字段
    - is_valid: 有效性标记
    - last_used: 最后使用时间（轮换时更新）
    # 用新值覆盖旧值，保持数据一致
    - fail_count: 连续失败次数
    """
    
    def __init__(self, 
                 id: int,
                 account_id: int,
                 cookie_data: str,
                 sessdata: str,
                 bili_jct: str,
                 buvid3: str,
                 is_valid: bool = True):
        """初始化一条 Cookie 记录

        Args:
            id: 数据库主键
            account_id: 所属账号 ID
            cookie_data: 完整 Cookie 串
            sessdata/bili_jct/buvid3: 请求头关键字段
            is_valid: 初始有效性标记
        """
        # 数据库主键 ID，用于回写状态
        self.id = id
        # 所属账号 ID
        self.account_id = account_id
        # 解密后的完整 Cookie 字符串
        self.cookie_data = cookie_data
        # 拆分出的关键字段，供请求头组装使用
        self.sessdata = sessdata
        self.bili_jct = bili_jct
        self.buvid3 = buvid3
        # 有效性标记，失效后不会被轮换选中
        self.is_valid = is_valid
        # 最后使用时间（轮换时更新）
        self.last_used: Optional[datetime] = None
        # 连续失败次数，用于失效判定
        self.fail_count = 0


class CookiePool:
    """Cookie池 - 管理多个Cookie的轮换和有效性检查
    
    核心能力：
    - 多 Cookie 轮换：游标取模，分散请求压力
    - 有效性检查：nav 接口验证 + 自动循环
    - 失效处理：内存 + 数据库同步更新
    # 对数据进行加工/分发
    - 旧数据迁移：明文自动加密回写
    """
    
    def __init__(self, check_interval: int = 1800):
        """初始化Cookie池
        
        Args:
            check_interval: Cookie有效性检查间隔(秒)
        """
        # 内存中的 Cookie 列表
        self.cookies: List[Cookie] = []
        # 轮换游标，get_cookie 时自增取模
        self.current_index = 0
        # 自动检查间隔
        self.check_interval = check_interval
        # 上次全量检查时间
        self.last_check_time: Optional[datetime] = None
        # 异步锁，保护内存池并发访问
        self._lock = asyncio.Lock()
        
        # 初始化加密器（用于 cookie 加密存储）
        # 复用 ConfigManager 的 Fernet 实例，保证密钥一致
        from core.config import ConfigManager
        config_mgr = ConfigManager()
        self._cipher = config_mgr._cipher
    
    def load_from_db(self, db: Session):
        """从数据库加载Cookie
        # 从存储/网络读入数据
        
        从 CookiePool 表读取所有有效记录，逐条解密。
        解密失败的记录视为旧版明文，直接使用并加密回写
        （自动迁移），同时支持从向导加密存储导入。
        
        Args:
            db: 数据库会话
        """
        # 全量加载 Cookie，包含历史上标记为失效的记录。
        # 失效记录不会被 get_cookie() 选中，但必须留在内存中供巡检恢复。
        cookie_models = db.query(CookiePoolModel).all()
        
        self.cookies = []
        # 遍历 cookie_models 逐项处理
        for c in cookie_models:
            try:
                # 尝试解密 cookie_data
                try:
                    # 解码数据
                    decrypted_cookie = self._cipher.decrypt(c.cookie_data.encode('utf-8')).decode('utf-8')
                except Exception as decrypt_error:
                    # 解密失败，可能是旧版本的明文数据，直接使用并加密回写
                    logger.warning(f"Cookie ID={c.id} 解密失败，尝试作为明文处理并迁移: {decrypt_error}")
                    # 兼容 bytes 类型（老版本 BLOB 存储）：统一转 str，避免后续 encode 再炸
                    if isinstance(c.cookie_data, bytes):
                        decrypted_cookie = c.cookie_data.decode('utf-8', errors='replace')
                    else:
                        decrypted_cookie = c.cookie_data
                    
                    # 加密并回写数据库，完成迁移
                    # 保证后续读取走统一解密路径
                    try:
                        # 加密数据
                        encrypted_cookie = self._cipher.encrypt(decrypted_cookie.encode('utf-8'))
                        c.cookie_data = encrypted_cookie.decode('utf-8')
                        # 提交事务
                        db.commit()
                        logger.info(f"Cookie ID={c.id} 已完成加密迁移")
                    except Exception as migrate_error:
                        logger.error(f"Cookie ID={c.id} 加密迁移失败: {migrate_error}")
                        # 回滚事务
                        db.rollback()
                
                # 构造内存对象并加入池
                cookie = Cookie(
                    id=c.id,
                    account_id=c.account_id,
                    cookie_data=decrypted_cookie,
                    sessdata=c.sessdata,
                    bili_jct=c.bili_jct,
                    buvid3=c.buvid3,
                    is_valid=c.is_valid
                )
                cookie.fail_count = c.fail_count or 0
                cookie.last_used = c.last_used
                self.cookies.append(cookie)
            except Exception as e:
                logger.error(f"加载 Cookie ID={c.id} 失败: {e}")
                # 跳过本轮继续循环
                continue
        
        logger.info(f"从数据库加载了 {len(self.cookies)} 个Cookie")
    
    def add_cookie(self, cookie_str: str, persist: bool = True) -> Optional['Cookie']:
        """新增/去重 Cookie 到内存池，可选持久化到数据库。

        扫码登录或手动粘贴 Cookie 后调用，保证爬虫侧
        get_cookie_pool() 能立刻拿到登录态，不用等表里先有记录。
        同一 SESSDATA 重复调用直接返回已有对象，不重复入库。

        Args:
            cookie_str: 完整 Cookie 字符串（需含 SESSDATA）。
            persist: 是否同步写入 cookie_pool 表，默认 True。

        Returns:
            新增或已存在的 Cookie 对象；cookie 无效时返回 None。
        """
        # 空值/关键字段保护：没有 SESSDATA 的 Cookie 没有登录态意义
        if not cookie_str or 'SESSDATA=' not in cookie_str:
            logger.warning("add_cookie 跳过：Cookie 为空或缺少 SESSDATA")
            return None

        # 解析拆分字段，供内存对象与数据库行共用
        cookie_dict = self._parse_cookie(cookie_str)
        sessdata = cookie_dict.get('SESSDATA', '')

        # 去重：同 SESSDATA 已存在则直接复用
        for c in self.cookies:
            if c.sessdata == sessdata:
                return c

        # 构造内存 Cookie（临时 id=-1，持久化成功后回填真实主键）
        cookie = Cookie(
            id=-1,
            account_id=1,
            cookie_data=cookie_str,
            sessdata=sessdata,
            bili_jct=cookie_dict.get('bili_jct', ''),
            buvid3=cookie_dict.get('buvid3', ''),
            is_valid=True,
        )
        self.cookies.append(cookie)

        # 可选持久化：写 cookie_pool 表，重启后仍能从表加载
        if persist:
            try:
                db = get_session()
                try:
                    existing = db.query(CookiePoolModel).filter(
                        CookiePoolModel.sessdata == sessdata
                    ).first()
                    if existing is None:
                        # 关联默认账号；表里无账号时兜底 account_id=1
                        account = db.query(Account).first()
                        account_id = account.id if account else 1
                        # 加密后入库，与 load_from_db 的解密路径保持一致
                        try:
                            encrypted = self._cipher.encrypt(cookie_str.encode('utf-8')).decode('utf-8')
                        except Exception as _enc_err:
                            # 加密失败降级明文，load_from_db 会走迁移路径再加密
                            logger.warning(f"Cookie 加密失败，降级明文存储: {_enc_err}")
                            encrypted = cookie_str
                        model = CookiePoolModel(
                            account_id=account_id,
                            cookie_data=encrypted,
                            sessdata=sessdata,
                            bili_jct=cookie_dict.get('bili_jct', ''),
                            buvid3=cookie_dict.get('buvid3', ''),
                            is_valid=True,
                        )
                        db.add(model)
                        db.commit()
                        cookie.id = model.id
                        logger.info(f"Cookie 已持久化到 cookie_pool 表 id={cookie.id}")
                    else:
                        # 已有记录但内存池没有（例如热加载），直接回填主键
                        cookie.id = existing.id
                finally:
                    db.close()
            except Exception as e:
                # 持久化失败不影响内存池使用，降级为仅内存
                logger.warning(f"Cookie 持久化失败（内存池仍可用）: {e}")

        return cookie

    def load_from_secrets(self) -> int:
        """回退加载：从 ConfigManager secrets 读取扫码登录保存的 Cookie。

        修复场景：web 扫码登录成功后只写 secrets（bilibili.cookie），
        从未写入 cookie_pool 表，导致爬虫侧 get_cookie_pool() 拿到空池、
        无登录态请求被 B 站风控。此方法在数据库加载后调用，
        若池仍为空则把 secrets 中的登录态补进内存池并持久化。

        Returns:
            本次回退加载的 Cookie 数量。
        """
        # 池里已有记录就不需要回退
        if self.cookies:
            return 0
        try:
            from core.config import ConfigManager
            secret_cookie = ConfigManager().get_secret('bilibili.cookie', '')
            if not secret_cookie:
                return 0
            cookie = self.add_cookie(secret_cookie, persist=True)
            if cookie is not None:
                logger.info("已从 secrets 回退加载登录态 Cookie 到池")
                return 1
        except Exception as e:
            logger.warning(f"从 secrets 回退加载 Cookie 失败: {e}")
        return 0
    
    
    async def get_cookie(self) -> Optional[Cookie]:
        """获取下一个可用的Cookie（轮换策略）
        
        从有效 Cookie 中按游标轮换选取，返回前更新
        # 用新值覆盖旧值，保持数据一致
        last_used。全部失效时返回 None。
        
        Returns:
            Cookie对象，如果没有可用Cookie则返回None
        """
        async with self._lock:
            if not self.cookies:
                logger.warning("Cookie池为空")
                return None
            
            # 过滤有效的Cookie
            # 失效 Cookie 不参与轮换，避免请求 401
            valid_cookies = [c for c in self.cookies if c.is_valid]
            
            if not valid_cookies:
                logger.error("没有有效的Cookie")
                return None
            
            # 轮换选择
            # 游标取模实现循环轮换，分散请求压力
            cookie = valid_cookies[self.current_index % len(valid_cookies)]
            self.current_index = (self.current_index + 1) % len(valid_cookies)
            
            cookie.last_used = datetime.now()
            return cookie
    
    async def mark_invalid(self, cookie: Cookie, db: Session, reason: str = ""):
        """标记Cookie为无效
        
        同步更新内存池与数据库，并记录风控事件。
        # 用新值覆盖旧值，保持数据一致
        
        Args:
            cookie: Cookie对象
            db: 数据库会话
            reason: 失效原因
        """
        async with self._lock:
            cookie.is_valid = False
            cookie.fail_count += 1
        
        # 更新数据库
        cookie_model = db.query(CookiePoolModel).filter(
            CookiePoolModel.id == cookie.id
        ).first()
        
        if cookie_model:
            cookie_model.is_valid = False
            cookie_model.fail_count = cookie.fail_count
            # 提交事务
            db.commit()
        
        logger.warning(f"Cookie已标记为无效: ID={cookie.id}, 原因={reason}")
        
        # 记录风控事件
        # 复用 LoggerManager 的风控日志，便于统一查询
        from core.logger import logger_manager
        if logger_manager:
            logger_manager.risk_logger.log_cookie_expired(
                cookie_name=f"账号{cookie.account_id}"
            )
    
    async def check_all_cookies(self, db: Session):
        """检查所有Cookie的有效性
        
        逐条调用 nav 接口验证：
        - 有效 -> 失效：标记无效
        - 失效 -> 有效：恢复有效并清零失败计数
        最后更新 last_check_time 并输出统计。
        # 用新值覆盖旧值，保持数据一致
        
        Args:
            db: 数据库会话
        """
        logger.info("开始检查所有Cookie有效性")
        
        # 遍历 cookies 逐项处理
        for cookie in self.cookies:
            try:
                validity = await self._check_cookie_validity(cookie.cookie_data)
                if validity is None:
                    logger.warning(f"Cookie状态暂不可判定，保留原状态: ID={cookie.id}")
                    continue
                is_valid = validity
                
                # 多条件判断
                if not is_valid and cookie.is_valid:
                    # 状态翻转：有效变失效
                    await self.mark_invalid(cookie, db, "有效性检查失败")
                # 多条件判断
                elif is_valid and not cookie.is_valid:
                    # Cookie恢复有效
                    # 状态翻转：失效变有效，清零失败计数
                    async with self._lock:
                        cookie.is_valid = True
                        cookie.fail_count = 0
                    
                    # 更新数据库
                    cookie_model = db.query(CookiePoolModel).filter(
                        CookiePoolModel.id == cookie.id
                    ).first()
                    if cookie_model:
                        cookie_model.is_valid = True
                        cookie_model.fail_count = 0
                        cookie_model.last_check = datetime.now()
                        # 提交事务
                        db.commit()
                    
                    logger.info(f"Cookie恢复有效: ID={cookie.id}")
                
                # 更新最后检查时间
                # 无论结果如何都记录检查时间
                cookie_model = db.query(CookiePoolModel).filter(
                    CookiePoolModel.id == cookie.id
                ).first()
                if cookie_model:
                    cookie_model.last_check = datetime.now()
                    # 提交事务
                    db.commit()
                
            except Exception as e:
                logger.error(f"检查Cookie失败: ID={cookie.id}, 错误={e}")
        
        self.last_check_time = datetime.now()
        
        # 统计
        valid_count = sum(1 for c in self.cookies if c.is_valid)
        logger.info(f"Cookie有效性检查完成: {valid_count}/{len(self.cookies)} 有效")
    
    def _parse_cookie(self, cookie_str: str) -> Dict[str, str]:
        """解析Cookie字符串
        
        将 'key=value; key2=value2' 格式的 Cookie 字符串
        解析为字典，自动处理空格与空项。
        # 对数据进行加工/分发
        
        Args:
            cookie_str: Cookie字符串
            
        Returns:
            Cookie字典
        """
        cookie_dict = {}
        for item in cookie_str.split(';'):
            # 去除首尾空白
            item = item.strip()
            if '=' in item:
                key, value = item.split('=', 1)
                cookie_dict[key.strip()] = value.strip()
        return cookie_dict
    
    async def _check_cookie_validity(self, cookie_data: str) -> Optional[bool]:
        """检查 Cookie 登录态。

        Args:
            cookie_data: 完整 Cookie 字符串。

        Returns:
            ``True`` 表示已登录，``False`` 表示接口明确确认未登录，
            ``None`` 表示网络、限流或风控导致本次无法判定。
        """
        try:
            async with BilibiliAPI(cookie=cookie_data) as api:
                data = await api.get(
                    "https://api.bilibili.com/x/web-interface/nav",
                    retry_times=1,
                )
                return bool(data.get("isLogin", False))
        except CookieExpiredError:
            return False
        except Exception as exc:
            # 临时请求异常不能等同于 Cookie 过期，否则一次风控就会永久清空池。
            logger.warning(f"Cookie有效性暂不可判定: {exc}")
            return None
    
    def get_stats(self) -> Dict:
        """获取Cookie池统计信息
        
        返回总数/有效数/失效数与最后检查时间，
        供管理界面展示。
        
        Returns:
            统计数据
        """
        valid_count = sum(1 for c in self.cookies if c.is_valid)
        
        return {
            'total': len(self.cookies),
            'valid': valid_count,
            'invalid': len(self.cookies) - valid_count,
            'last_check': self.last_check_time.isoformat() if self.last_check_time else None
        }
    

# 全局Cookie池实例
# 惰性初始化：首次 get_cookie_pool() 时创建并加载
_global_cookie_pool: Optional[CookiePool] = None


def get_cookie_pool() -> CookiePool:
    """获取全局Cookie池实例
    
    单例模式：首次调用时创建 CookiePool，
    # 实例化对象并准备使用
    从配置读取检查间隔并从数据库加载 Cookie。
    # 从存储/网络读入数据
    
    Returns:
        CookiePool实例
    """
    global _global_cookie_pool
    if _global_cookie_pool is None:
        from core.config import config
        check_interval = config.get('bilibili.cookie_check_interval', 1800)
        _global_cookie_pool = CookiePool(check_interval=check_interval)
        
        # 通过模块级 get_session 获取会话，避免导入时捕获 None 的 db_manager
        try:
            db = get_session()
            _global_cookie_pool.load_from_db(db)
        except Exception as e:
            logger.error(f"从数据库加载Cookie失败: {e}")
        finally:
            if 'db' in locals():
                db.close()

        # 回退加载：数据库 cookie_pool 表为空时，从 secrets 读取扫码登录保存的 Cookie。
        # 修复 2026-08-22 空池风控问题：web 登录只写 secrets 不写表，导致爬虫无登录态。
        _global_cookie_pool.load_from_secrets()
    
    return _global_cookie_pool


# 别名，兼容旧代码中的 CookiePoolManager 引用
CookiePoolManager = CookiePool
