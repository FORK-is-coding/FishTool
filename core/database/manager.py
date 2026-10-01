"""
数据库管理器 DatabaseManager

拆分自 database.py 原始 L693-L814。
封装 SQLite + SQLAlchemy 的初始化与会话管理。
"""
from datetime import datetime
from pathlib import Path
import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from .base import Base, logger


# ============ SQLite 连接参数 ============

#: SQLite 忙等待超时（毫秒）。配额计数与业务写入并发时靠它排队，而不是立刻抛 "database is locked"。
SQLITE_BUSY_TIMEOUT_MS = 5000

#: 同上，换算成 sqlite3 驱动的 ``timeout`` 参数口径（秒）。
SQLITE_BUSY_TIMEOUT_S = SQLITE_BUSY_TIMEOUT_MS / 1000


def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """每条 SQLite 连接建立时开启 WAL 与 busy_timeout（规格 §2.6）。

    Args:
        dbapi_connection: sqlite3 原生连接。
        _connection_record: SQLAlchemy 连接池记录（未使用）。

    Returns:
        无；PRAGMA 失败只记日志，不让连接建立直接失败。
    """
    cursor = dbapi_connection.cursor()
    try:
        # WAL 是库文件级设置，开启后读不阻塞写、写不阻塞读。
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    except Exception:
        logger.exception("SQLite PRAGMA（WAL / busy_timeout）设置失败，回退默认日志模式")
    finally:
        cursor.close()


# ============ 数据库管理器 ============

class DatabaseManager:
    """数据库管理器

    封装 SQLite + SQLAlchemy 的初始化与会话管理：
    # 设置初始值/默认状态，避免后续空引用
    # 写入配置/属性，影响后续行为
    # 将数据持久化到表中
    - 自动创建数据库目录与所有表
    # 实例化对象并准备使用
    - StaticPool 固定连接池，配合 check_same_thread=False
      支持多线程/异步环境下共享引擎
    - 提供 get_session 获取独立会话（调用方负责关闭）
    # 读取数据并赋值给当前作用域变量
    - 提供 backup 备份与 drop_all_tables 清库（慎用）
    """
    
    def __init__(self, db_path: str = "data/bili_ops.db"):
        """初始化数据库管理器
        # 设置初始值/默认状态，避免后续空引用
        # 写入配置/属性，影响后续行为
        # 将数据持久化到表中
        
        Args:
            db_path: 数据库文件路径
        """
        self.db_path = Path(db_path)
        # 确保父目录存在，避免 SQLite 创建失败
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 创建引擎
        # StaticPool：固定单个连接，避免 SQLite 文件锁竞争
        # check_same_thread=False：允许跨线程使用同一连接
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            echo=False,
            # timeout 是 sqlite3 驱动级忙等待秒数，与 PRAGMA busy_timeout 同口径。
            connect_args={"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_S},
            poolclass=StaticPool
        )
        # 每条新连接都开 WAL + busy_timeout=5000，配额表与业务表共用同一库时不互相饿死。
        event.listen(self.engine, "connect", _apply_sqlite_pragmas)
        
        # 创建会话工厂
        # autocommit=False 手动管理事务，autoflush=False 延迟刷新
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        
        # 创建所有表
        self.create_tables()
    
    def create_tables(self):
        """创建所有表
        # 实例化对象并准备使用

        根据 Base.metadata 中注册的模型一次性建表，
        已存在的表自动跳过（SQLAlchemy 默认行为）。
        """
        Base.metadata.create_all(bind=self.engine)
        self._migrate_comment_member_columns()
        self._migrate_up_master_charge_count()
        self._migrate_video_stats_columns()
        logger.info(f"数据库表创建完成: {self.db_path}")

    def _migrate_comment_member_columns(self) -> None:
        """为旧数据库幂等补充评论用户等级和会员 JSON 列。

        Args:
            无。

        Returns:
            无；不存在的列会通过 SQLite ``ALTER TABLE`` 原位新增。
        """
        try:
            existing = {column["name"] for column in inspect(self.engine).get_columns("comments")}
            statements = {
                "level_info": "ALTER TABLE comments ADD COLUMN level_info JSON",
                "vip": "ALTER TABLE comments ADD COLUMN vip JSON",
            }
            with self.engine.begin() as connection:
                for column_name, statement in statements.items():
                    if column_name not in existing:
                        connection.execute(text(statement))
                        logger.info("数据库迁移完成: comments.%s", column_name)
        except Exception:
            logger.exception("评论用户字段迁移失败")
            raise
    
    def _migrate_up_master_charge_count(self) -> None:
        """为旧数据库幂等补充 ``up_masters.charge_count`` 列。

        Args:
            无。

        Returns:
            无；缺失列通过 SQLite ALTER TABLE 原位新增。
        """
        try:
            existing = {column["name"] for column in inspect(self.engine).get_columns("up_masters")}
            if "charge_count" not in existing:
                with self.engine.begin() as connection:
                    connection.execute(
                        text("ALTER TABLE up_masters ADD COLUMN charge_count INTEGER DEFAULT 0")
                    )
                logger.info("数据库迁移完成: up_masters.charge_count")
        except Exception:
            logger.exception("UP主充电人数字段迁移失败")
            raise

    def _migrate_video_stats_columns(self) -> None:
        """为旧数据库幂等补充 ``video_stats`` 的来源与质量列。

        Args:
            无。

        Returns:
            无；缺失列通过 SQLite ALTER TABLE 原位新增。
        """
        try:
            existing = {column["name"] for column in inspect(self.engine).get_columns("video_stats")}
            statements = {
                "source": "ALTER TABLE video_stats ADD COLUMN source VARCHAR(50)",
                "run_id": "ALTER TABLE video_stats ADD COLUMN run_id VARCHAR(64)",
                "view_status": "ALTER TABLE video_stats ADD COLUMN view_status VARCHAR(20)",
                "stat_status": "ALTER TABLE video_stats ADD COLUMN stat_status VARCHAR(20)",
                "captured_epoch_s": "ALTER TABLE video_stats ADD COLUMN captured_epoch_s INTEGER",
                "collection_tid": "ALTER TABLE video_stats ADD COLUMN collection_tid INTEGER",
                "raw_tid": "ALTER TABLE video_stats ADD COLUMN raw_tid INTEGER",
                "metric_status": "ALTER TABLE video_stats ADD COLUMN metric_status JSON",
            }
            with self.engine.begin() as connection:
                for column_name, statement in statements.items():
                    if column_name not in existing:
                        connection.execute(text(statement))
                        logger.info("数据库迁移完成: video_stats.%s", column_name)
                # 历史读取按 (video_id, captured_epoch_s) 提速；仅在两列都存在时幂等创建，
                # 避免对极端残缺旧表（缺 video_id）误建索引导致迁移失败。
                if {"video_id", "captured_epoch_s"} <= (existing | set(statements)):
                    connection.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_video_stats_video_captured "
                        "ON video_stats (video_id, captured_epoch_s)"
                    ))
        except Exception:
            logger.exception("视频统计来源字段迁移失败")
            raise

    def get_session(self) -> Session:
        """获取数据库会话
        # 读取数据并赋值给当前作用域变量
        
        Returns:
            Session对象
        """
        return self.SessionLocal()
    
    def drop_all_tables(self):
        """删除所有表（慎用）
        # 移除不再需要的数据/对象

        危险操作：会清空全部数据。
        # 重置容器状态，释放引用
        仅用于测试或重建场景。
        """
        Base.metadata.drop_all(bind=self.engine)
        logger.warning("已删除所有数据库表")
    
    def backup(self, backup_path: str = None):
        """备份数据库
        
        Args:
            backup_path: 备份文件路径
            
        Returns:
            备份文件路径
        """
        if backup_path is None:
            # 未指定路径时按时间戳生成备份名
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            # 计算结果存入 backup_path
            # 对输入做运算得到结果
            backup_path = self.db_path.parent / f"backup_{timestamp}.db"
        
        # 用 shutil 复制数据库文件，简单可靠
        import shutil
        shutil.copy2(self.db_path, backup_path)
        logger.info(f"数据库已备份到: {backup_path}")
        return backup_path
