"""
数据库管理器 DatabaseManager

拆分自 database.py 原始 L693-L814。
封装 SQLite + SQLAlchemy 的初始化与会话管理。
"""
from datetime import datetime
from pathlib import Path
import logging
from typing import Dict

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
    """每条 SQLite 连接建立时开启外键约束、WAL 与 busy_timeout（规格 §2.6）。

    SQLite 默认 ``foreign_keys=OFF``，模型里声明的 ``ForeignKey`` 只是注释，
    孤儿行可以照常写入。外键开关是连接级设置，必须在建连 hook 里打开，
    且要在任何事务开始之前执行（事务内设置会被 SQLite 静默忽略）。

    Args:
        dbapi_connection: sqlite3 原生连接。
        _connection_record: SQLAlchemy 连接池记录（未使用）。

    Returns:
        无；PRAGMA 失败只记日志，不让连接建立直接失败。
    """
    cursor = dbapi_connection.cursor()
    try:
        # 外键强制：父行被引用时拒绝删除，声明了 ON DELETE 的按 schema 级联。
        cursor.execute("PRAGMA foreign_keys=ON")
        # 回读确认真的生效，避免驱动层静默忽略后仍然以为约束已开。
        cursor.execute("PRAGMA foreign_keys")
        enabled = cursor.fetchone()
        if not enabled or int(enabled[0]) != 1:
            logger.error("SQLite 外键约束未生效（PRAGMA foreign_keys=%s），孤儿行可能仍可写入", enabled)
        # WAL 是库文件级设置，开启后读不阻塞写、写不阻塞读。
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    except Exception:
        logger.exception("SQLite PRAGMA（foreign_keys / WAL / busy_timeout）设置失败，回退默认设置")
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
        # tasks 表迁移与其余迁移互不依赖，放在最前，避免与其他分支在序列末尾追加的迁移相邻冲突
        self._migrate_task_key_column()
        self._migrate_comment_member_columns()
        self._migrate_comment_context_columns()
        self._migrate_up_master_charge_count()
        self._migrate_video_stats_columns()
        self._migrate_hotspot_watch_event_columns()
        self._migrate_cookie_pool_plaintext_credentials()
        self._migrate_semantic_run_budget_columns()
        # 外键开启后，历史库可能存在父行已删除的孤儿行；启动时体检并告警，不删数据。
        self._audit_orphan_rows()
        logger.info(f"数据库表创建完成: {self.db_path}")

    def _migrate_task_key_column(self) -> None:
        """为旧数据库幂等补充 ``tasks.task_key`` 列及其唯一索引。

        抽奖后台任务用随机 UUID 作对外句柄，持久化后需按它查询；
        已有的评论采集断点记录 ``task_key`` 为 NULL，SQLite 唯一索引允许多个 NULL，互不影响。

        Args:
            无。

        Returns:
            无；缺失列通过 SQLite ALTER TABLE 原位新增，索引用 IF NOT EXISTS 创建。
        """
        try:
            existing = {column["name"] for column in inspect(self.engine).get_columns("tasks")}
            with self.engine.begin() as connection:
                if "task_key" not in existing:
                    connection.execute(text("ALTER TABLE tasks ADD COLUMN task_key VARCHAR(64)"))
                    logger.info("数据库迁移完成: tasks.task_key")
                connection.execute(
                    text("CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_task_key ON tasks (task_key)")
                )
        except Exception:
            logger.exception("任务句柄字段迁移失败")
            raise

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
    
    def _migrate_comment_context_columns(self) -> None:
        """为旧数据库幂等补充评论上下文列（语义折叠首版）。

        新增 ``root_rpid`` / ``parent_rpid`` / ``context_status`` 三列，均可空。
        旧行补列后默认 NULL，语义层一律视为 ``unknown`` 上下文，不冒充顶层；
        重新采集相同 rpid 时由 storage 层原位补齐，不破坏既有数据。

        Args:
            无。

        Returns:
            无；缺失列通过 SQLite ``ALTER TABLE`` 原位新增，重复启动跳过已存在列。
        """
        try:
            existing = {column["name"] for column in inspect(self.engine).get_columns("comments")}
            statements = {
                "root_rpid": "ALTER TABLE comments ADD COLUMN root_rpid VARCHAR(50)",
                "parent_rpid": "ALTER TABLE comments ADD COLUMN parent_rpid VARCHAR(50)",
                "context_status": "ALTER TABLE comments ADD COLUMN context_status VARCHAR(20)",
            }
            with self.engine.begin() as connection:
                for column_name, statement in statements.items():
                    if column_name not in existing:
                        connection.execute(text(statement))
                        logger.info("数据库迁移完成: comments.%s", column_name)
        except Exception:
            logger.exception("评论上下文字段迁移失败")
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
                "pubdate_epoch_s": "ALTER TABLE video_stats ADD COLUMN pubdate_epoch_s INTEGER",
                "pubdate_status": "ALTER TABLE video_stats ADD COLUMN pubdate_status VARCHAR(20)",
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

    def _migrate_hotspot_watch_event_columns(self) -> None:
        """为旧数据库幂等补充 ``hotspot_watch`` 的 04 事件列。

        补列（照 04 §3.4）：``sample_interval_s`` / ``fast_until_s`` / ``manual_pinned`` /
        ``source_demands``。其中 ``sample_interval_s`` 已由 02 批 2 的模型定义，检测到即跳过，
        **不重复 ALTER**；其余三列由本迁移补。

        幂等要点：
        - 表不存在时先确保 02 模型注册建表，**绝不对不存在的表盲 ALTER**；
        - 已存在的列一律跳过，重复启动不报错、不重复加列；
        - 旧记录默认普通节奏（``manual_pinned`` 落 0、``fast_until_s`` / ``source_demands``
          为 NULL），**不**推断旧 watch 的来源、不臆造 ``source_demands`` 内容。

        Args:
            无。

        Returns:
            无；缺失列通过 SQLite ``ALTER TABLE`` 原位新增。
        """
        try:
            inspector = inspect(self.engine)
            if not inspector.has_table("hotspot_watch"):
                # 02 模型可能尚未注册：显式导入并补建该表，绝不盲 ALTER 一个不存在的表。
                from .models_hotspot_watch import HotspotWatch

                HotspotWatch.__table__.create(bind=self.engine, checkfirst=True)
                inspector = inspect(self.engine)
                if not inspector.has_table("hotspot_watch"):
                    logger.warning("hotspot_watch 表不存在，跳过 04 事件列迁移")
                    return
            existing = {column["name"] for column in inspector.get_columns("hotspot_watch")}
            statements = {
                "sample_interval_s": (
                    "ALTER TABLE hotspot_watch "
                    "ADD COLUMN sample_interval_s INTEGER NOT NULL DEFAULT 3600"
                ),
                "fast_until_s": "ALTER TABLE hotspot_watch ADD COLUMN fast_until_s INTEGER",
                "manual_pinned": (
                    "ALTER TABLE hotspot_watch "
                    "ADD COLUMN manual_pinned BOOLEAN NOT NULL DEFAULT 0"
                ),
                "source_demands": "ALTER TABLE hotspot_watch ADD COLUMN source_demands JSON",
            }
            with self.engine.begin() as connection:
                for column_name, statement in statements.items():
                    if column_name not in existing:
                        connection.execute(text(statement))
                        logger.info("数据库迁移完成: hotspot_watch.%s", column_name)
        except Exception:
            logger.exception("hotspot_watch 事件字段迁移失败")
            raise

    def _migrate_semantic_run_budget_columns(self) -> None:
        """为旧数据库幂等补充 ``semantic_runs`` 的预算模式与耗时统计四列。

        新增 ``budget_mode`` / ``stop_seq`` / ``elapsed_call_ms`` / ``timed_calls``，
        全部可原位 ALTER ADD COLUMN；旧行取列默认值（budget_mode='default'，
        其余为 0），与旧任务的实际行为一致（旧任务都在默认档预算下运行）。

        Args:
            无。

        Returns:
            无；不存在的列逐个补齐，已存在则跳过，可重复执行。
        """
        try:
            inspector = inspect(self.engine)
            if not inspector.has_table("semantic_runs"):
                return
            existing = {column["name"] for column in inspector.get_columns("semantic_runs")}
            statements = {
                "budget_mode": "ALTER TABLE semantic_runs ADD COLUMN budget_mode VARCHAR(16) NOT NULL DEFAULT 'default'",
                "stop_seq": "ALTER TABLE semantic_runs ADD COLUMN stop_seq INTEGER NOT NULL DEFAULT 0",
                "elapsed_call_ms": "ALTER TABLE semantic_runs ADD COLUMN elapsed_call_ms INTEGER NOT NULL DEFAULT 0",
                "timed_calls": "ALTER TABLE semantic_runs ADD COLUMN timed_calls INTEGER NOT NULL DEFAULT 0",
            }
            with self.engine.begin() as connection:
                for column_name, statement in statements.items():
                    if column_name not in existing:
                        connection.execute(text(statement))
                        logger.info("数据库迁移完成: semantic_runs.%s", column_name)
        except Exception:
            logger.exception("语义任务预算列迁移失败")
            raise

    def _migrate_cookie_pool_plaintext_credentials(self) -> None:
        """幂等清理 ``cookie_pool`` 表中历史遗留的明文凭据列（issue #5 第 3 点）。

        旧版本把 SESSDATA / bili_jct 明文写进独立列，只拿到库文件即可读取会话凭据。
        本迁移：
        - ``sessdata`` 明文改写为 ``sha256:<hex>`` 摘要（仍可用于去重）；
          已带 ``sha256:`` 前缀的行跳过，重复启动不会二次摘要；
        - ``bili_jct`` 非空值一律置为空串；
        - 单行无法计算摘要时清空该列，宁可失去去重也不保留明文。

        完整 Cookie 只保存在加密的 ``cookie_data`` 中，运行时字段由 Cookie 池解密后解析，
        因此改写这两列不影响加载。注意：UPDATE 后旧明文仍可能残留在 SQLite 空闲页与备份文件中，
        需要用户自行执行 VACUUM 并处理备份（见 CHANGELOG 发布说明）。

        Args:
            无。

        Returns:
            无；明文列原位改写，表不存在时直接跳过。
        """
        from .models_account import SESSDATA_DIGEST_PREFIX, sessdata_digest

        try:
            inspector = inspect(self.engine)
            if not inspector.has_table("cookie_pool"):
                return
            existing = {column["name"] for column in inspector.get_columns("cookie_pool")}
            migrated = 0
            with self.engine.begin() as connection:
                if "sessdata" in existing:
                    rows = connection.execute(text(
                        "SELECT id, sessdata FROM cookie_pool "
                        "WHERE sessdata IS NOT NULL AND sessdata != ''"
                    )).fetchall()
                    for row_id, value in rows:
                        if isinstance(value, str) and value.startswith(SESSDATA_DIGEST_PREFIX):
                            continue
                        try:
                            if isinstance(value, bytes):
                                value = value.decode("utf-8")
                            digest = sessdata_digest(str(value).strip())
                        except Exception:
                            # 无法解析的值不能原样保留，直接清空
                            logger.warning("cookie_pool id=%s 的 sessdata 无法计算摘要，已清空", row_id)
                            digest = ""
                        connection.execute(
                            text("UPDATE cookie_pool SET sessdata = :digest WHERE id = :row_id"),
                            {"digest": digest, "row_id": row_id},
                        )
                        migrated += 1
                if "bili_jct" in existing:
                    result = connection.execute(text(
                        "UPDATE cookie_pool SET bili_jct = '' "
                        "WHERE bili_jct IS NOT NULL AND bili_jct != ''"
                    ))
                    migrated += result.rowcount or 0
            if migrated:
                logger.info("数据库迁移完成: cookie_pool 明文凭据列已清理 %s 处", migrated)
        except Exception:
            logger.exception("cookie_pool 明文凭据列清理失败")
            raise

    @staticmethod
    def _orphan_count_statement(table, constraint):
        """为一条外键约束生成"统计孤儿行"的 SQL。

        Args:
            table: 子表（``sqlalchemy.Table``）。
            constraint: 该表上的 ``ForeignKeyConstraint``。

        Returns:
            ``SELECT COUNT(*)`` 语句；约束不属于该表时返回 ``None``。
        """
        child_columns = []
        parent_columns = []
        for element in constraint.elements:
            # 只处理以本表为子端的约束（同一约束对象可能被两侧共享）。
            if element.parent.table is not table:
                continue
            child_columns.append(element.parent.name)
            parent_columns.append(element.column.name)
        if not child_columns:
            return None
        parent_table = constraint.elements[0].column.table.name
        # NULL 外键在 SQLite 语义里不算违例，只统计非 NULL 且父行不存在的行。
        conditions = " AND ".join(
            f"({table.name}.{child} IS NOT NULL AND NOT EXISTS ("
            f"SELECT 1 FROM {parent_table} "
            f"WHERE {parent_table}.{parent} = {table.name}.{child}))"
            for child, parent in zip(child_columns, parent_columns)
        )
        return f"SELECT COUNT(*) FROM {table.name} WHERE {conditions}"

    def _audit_orphan_rows(self) -> None:
        """体检历史遗留的孤儿外键行并告警，不删除任何业务数据。

        外键在本版本才开启，旧库里可能存在父行已删除、子行仍残留的记录。
        SQLite 开启外键后不会回溯校验存量行，这些行对新写入没有影响，因此这里
        只统计并告警：静默删除会误伤仍然有效的子行（例如 accounts 行被清理后
        cookie_pool 里仍可用的凭据），数据修复交给人工按告警处理。体检失败只记
        日志，绝不让启动失败。

        Args:
            无。

        Returns:
            无；重复启动结果一致。
        """
        orphaned: Dict[str, int] = {}
        try:
            with self.engine.connect() as connection:
                # 依赖倒序：先统计子表，父表计数不受子行影响。
                for table in reversed(Base.metadata.sorted_tables):
                    for constraint in table.foreign_key_constraints:
                        count = self._count_orphan_rows(connection, table, constraint)
                        if count:
                            orphaned[table.name] = orphaned.get(table.name, 0) + count
        except Exception:
            # 体检失败不影响启动：外键仍对新写入生效。
            logger.exception("历史孤儿外键行体检失败（继续启动）")
            return
        if orphaned:
            detail = ", ".join(f"{name}={count}" for name, count in sorted(orphaned.items()))
            logger.warning(
                "检测到历史孤儿外键行（外键已开启，新写入不会再产生），请人工确认后清理: %s", detail
            )

    @staticmethod
    def _count_orphan_rows(connection, table, constraint) -> int:
        """统计单条外键约束下的孤儿行数，查询失败只记警告。

        Args:
            connection: 当前连接。
            table: 子表。
            constraint: 外键约束。

        Returns:
            孤儿行条数；查询失败返回 0。
        """
        statement = DatabaseManager._orphan_count_statement(table, constraint)
        if not statement:
            return 0
        try:
            result = connection.execute(text(statement)).scalar()
        except Exception:
            logger.warning("跳过 %s 的孤儿行体检", table.name, exc_info=True)
            return 0
        return int(result or 0)

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
