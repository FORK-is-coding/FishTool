"""tests 目录的公共 pytest 配置。"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 桌面测试在无显示器环境运行，避免 Qt 尝试连接真实桌面会话。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ===========================================================================
# Web 应用生命周期隔离
# ===========================================================================
# 背景：web/main.py 的 lifespan 在 `with TestClient(app)` 期间会被执行，默认会
#   ① ConfigManager()                       -> 读写真实 config/（含 .key/.secrets）
#   ② init_database()                       -> 打开相对路径 "data/bili_ops.db" 真库
#   ③ ResidentCommentMonitor(...).start()   -> 起常驻监控，写 monitor_state/
#                                              operation_logs，并可能发真实网络请求
# 以上三处任意一处都会污染真实数据，因此凡是走真实 lifespan 的用例必须先隔离。
#
# 隔离方式：仅在测试侧用 monkeypatch 替换 web.main 命名空间里的三个符号，
# 不修改任何生产代码。注意 web/main.py 是 `from core.database import init_database`
# 的模块级导入，补丁必须打在 `web.main.init_database` 上；打在 core.database.api 上无效。
#
# 作用域：autouse，对本目录及其子目录下的所有用例生效；对仓库根目录下的同源老测试
# （尚未搬运时）需配合 `-p tests.conftest` 或以根 conftest 暴露（见交付说明）。
#
# 防御：web.main 导入失败（环境缺依赖）时只打印中文日志并跳过，不影响其余用例。
# ===========================================================================


class _NoopResidentMonitor:
    """常驻评论监控的 no-op 替身。

    只记录"被实例化 / 被 start / 被 shutdown"，绝不创建后台任务、定时器或网络请求。
    真实对象对应 core.monitor_service.ResidentCommentMonitor。
    """

    #: 便于用例断言"确实被创建过，但没有真起常驻任务"
    instances: list = []

    def __init__(self, *args, **kwargs) -> None:
        """接收与真实类一致的任意入参并忽略之。

        Args:
            *args: 真实类的位置参数（monitor_factory）。
            **kwargs: 真实类的关键字参数（config）。

        Returns:
            无。
        """
        type(self).instances.append(self)
        self.args = args
        self.kwargs = kwargs
        self.started = False
        self.shutdown_called = False

    async def start(self) -> None:
        """空实现的启动：只置位标记，不真正启动常驻监控循环。"""
        self.started = True

    async def shutdown(self) -> None:
        """空实现的关闭：与 start 对称，不触碰任何外部资源。"""
        self.shutdown_called = True


@pytest.fixture(autouse=True)
def _isolate_web_lifespan(tmp_path, monkeypatch):
    """把 web.main 的 lifespan 三个污染点全部重定向到临时目录 / 假对象。

    隔离点（全部打在 web.main 模块对象上）：
        1. web.main.init_database         -> 临时库 tmp_path/"bili_ops.db"
        2. web.main.ConfigManager         -> 指向临时 config 目录的工厂
        3. web.main.ResidentCommentMonitor-> _NoopResidentMonitor（不真起常驻监控）

    额外保护：lifespan 会改写 web.main.config_manager / monitor_service 两个模块级
    全局变量，这里先做快照，pytest 的 monkeypatch 会在用例结束时自动还原，避免
    真实配置对象或假监控实例泄漏到后续用例。

    DB 真隔离（关键）：core.database.api.get_session()/get_db() 在 db_manager 为 None
    时会自动 init_database("data/bili_ops.db")——那是相对 CWD 的真库。因此这里必须把
    core.database.api.db_manager 显式替换为指向 tmp 库的 DatabaseManager 实例；
    若只做同值快照（setattr 成它自己），等于没隔离，storage_mixin / comment collector
    等直接调用 core.database.get_session() 的路径仍会写脏真库。

    Args:
        tmp_path: pytest 提供的用例级临时目录。
        monkeypatch: pytest 提供的临时替换工具，负责用例结束后逐条还原。

    Returns:
        无（生成器 fixture，yield 之后交给 monkeypatch 做回滚）。
    """
    import importlib

    try:
        web_main = importlib.import_module("web.main")
    except Exception as exc:  # noqa: BLE001 - 环境缺依赖时不应连坐其他用例
        print(f"[conftest] 跳过 web 生命周期隔离（导入 web.main 失败）：{exc!r}")
        yield
        return

    # 延迟导入：避免在不需要隔离的用例上增加额外导入成本
    # 注意：core/__init__.py 里 `from .config import config` 会把包属性 core.config
    # 覆盖成 ConfigManager 单例实例（core.database 同理导出 db_manager），因此必须用
    # importlib 显式取子模块对象，不能用 `from core import config as xxx` 的写法。
    config_module = importlib.import_module("core.config")
    database_api = importlib.import_module("core.database.api")

    isolated_db_path = tmp_path / "bili_ops.db"
    isolated_config_dir = tmp_path / "config"

    # -------------------------------------------------------------------
    # 快照：lifespan 会写这三个全局状态，先记录当前值，用例结束由 monkeypatch 还原
    # -------------------------------------------------------------------
    # 真隔离：挂一个指向 tmp 库的 DatabaseManager 实例作为模块级单例。
    # 顺序要点：monkeypatch.setattr 会在调用时记录“当时的旧值”以便用例结束还原，
    # 所以必须直接 setattr 传入新构造的 tmp 管理器；若先 init_database(tmp) 再 setattr，
    # 快照到的会是新值，用例结束无法还原回真实 None。
    # get_session()/get_db() 运行时都读取 core.database.api.db_manager 这个模块全局，
    # 只替换这一处即可覆盖 modules/comment/collector/storage_mixin.py 等全部落库路径。
    monkeypatch.setattr(
        database_api,
        "db_manager",
        database_api.DatabaseManager(str(isolated_db_path)),
    )
    monkeypatch.setattr(web_main, "config_manager", web_main.config_manager, raising=False)
    monkeypatch.setattr(web_main, "monitor_service", web_main.monitor_service, raising=False)

    # -------------------------------------------------------------------
    # 隔离点 1：数据库重定向到 tmp 库（必须打在 web.main 上）
    # -------------------------------------------------------------------
    def _isolated_init_database(db_path=None):
        """忽略调用方传入的路径，一律初始化到临时库。

        Args:
            db_path: 真实实现的位置参数；此处刻意忽略。

        Returns:
            core.database.DatabaseManager: 指向临时库的管理器实例。
        """
        return database_api.init_database(str(isolated_db_path))

    monkeypatch.setattr(web_main, "init_database", _isolated_init_database)

    # -------------------------------------------------------------------
    # 隔离点 2：配置管理器落到临时 config 目录，避免读写真实 config/
    # -------------------------------------------------------------------
    real_config_manager = config_module.ConfigManager

    def _isolated_config_manager(*args, **kwargs):
        """构造真实 ConfigManager，但目录强制指向临时目录。

        Args:
            *args: 透传给真实 ConfigManager 的位置参数。
            **kwargs: 透传的关键字参数；未显式指定 config_dir 时使用临时目录。

        Returns:
            ConfigManager: 基于临时配置目录的实例。
        """
        kwargs.setdefault("config_dir", str(isolated_config_dir))
        return real_config_manager(*args, **kwargs)

    monkeypatch.setattr(web_main, "ConfigManager", _isolated_config_manager)

    # -------------------------------------------------------------------
    # 隔离点 3：常驻监控换成 no-op 假对象，绝不真起常驻任务
    # -------------------------------------------------------------------
    monkeypatch.setattr(web_main, "ResidentCommentMonitor", _NoopResidentMonitor)

    yield


# ===========================================================================
# 缺口 A/B/C 落盘隔离：data/logs · data/lottery_cache · data 下的测试库文件
# ===========================================================================
# 目标：把测试期间的「真目录落盘」收敛到会话级临时沙箱，跑完全量 pytest 后
#   data/logs/*.log、data/lottery_cache/*.json、data 下的测试 .db 的
#   (size, mtime) 全部保持不变。
#
# LoggerManager 创建时机判定（关键结论）：
#   core/logger.py 底部 `logger_manager: Optional[LoggerManager] = None` 仅为占位；
#   模块内与 core/__init__.py 均无 import 期实例化；唯一构造入口是 init_logger()，
#   而 init_logger() 只在 get_logger() 发现全局为 None 时被调用（core/logger.py
#   :723-726）。→ 结论：**懒创建（lazy）**。首次构造发生在某个被 import 的模块执行
#   `logger = get_logger(__name__)` 时（例如 modules/lottery/cache.py:10），即 test
#   模块 import 阶段。因此隔离补丁必须打在 **本 conftest 顶层（import 期）**，早于
#   任何 test 模块 import；放进 session fixture 已晚——那时单例早把真实 data/logs
#   的文件 handler 绑死了，事后改属性无效。
#
# 三个落盘点与补丁目标：
#   1) 日志       -> patch core.logger.LoggerManager.__init__（按路径判定重定向）
#        覆盖两条来源：logger.py 默认值 "data/logs" 与 main.py:66
#        `Path(config['database.path']).parent / 'logs'`（同样解析到 data/logs）。
#   2) 抽奖缓存   -> patch modules.lottery.cache.LotteryCache._write_json（全类唯一写盘点）
#        只重定向「落在 data/lottery_cache 下的 target」，不改 self.cache_dir 语义，
#        因而 tests/test_lottery_service.py::test_init_defaults_cache_dir_under_data
#        断言的 cache_dir == Path("data") / "lottery_cache" 照样成立。
#   3) 测试库文件 -> patch core.database.manager.DatabaseManager.__init__（按路径判定）
#        覆盖 tests/test_hotspot_rev3_import.py:11 的硬编码
#        DatabaseManager("data/test_hotspot_rev3.db")。
#
# 说明：仅做运行时 monkeypatch，不改任何生产源码；不做 os.chdir（避免打断
#   test_lottery_frontend_paths_match_backend_contract 的相对路径读 JS）。
# ===========================================================================
_ISOLATED_ROOT = Path(tempfile.mkdtemp(prefix="bili_ops_test_isolation_"))
_ISOLATED_LOGS_DIR = _ISOLATED_ROOT / "logs"
_ISOLATED_LOTTERY_DIR = _ISOLATED_ROOT / "lottery_cache"
_ISOLATED_DATA_DIR = _ISOLATED_ROOT / "data"

_REPO_DATA_DIR = PROJECT_ROOT / "data"
_REPO_LOGS_DIR = _REPO_DATA_DIR / "logs"
_REPO_LOTTERY_DIR = _REPO_DATA_DIR / "lottery_cache"


def _resolves_under(path_like, root: Path) -> bool:
    """判断路径解析后的绝对位置是否落在 root 之下。

    Args:
        path_like: 待判断路径（str/Path），允许不存在。
        root: 目标根目录（仓库内真实目录）。

    Returns:
        bool: True 表示解析后位于 root 之下。
    """
    try:
        candidate = Path(path_like).resolve()
        base = Path(root).resolve()
    except (TypeError, OSError, ValueError):
        return False
    try:
        candidate.relative_to(base)
        return True
    except ValueError:
        return False


def _points_into_repo_dir(path_like, root: Path, raw_prefix: str) -> bool:
    """双判据识别「指向仓库内目录」：绝对解析 + 相对前缀（不依赖 CWD）。

    Args:
        path_like: 待判断路径（str/Path）。
        root: 仓库内目标目录（用于绝对解析比对）。
        raw_prefix: 形如 "data/logs" 的相对前缀（兜底，防止 CWD 非仓库根）。

    Returns:
        bool: 命中任一条判据即返回 True。
    """
    if _resolves_under(path_like, root):
        return True
    try:
        raw = str(path_like).replace("\\", "/")
    except Exception:  # noqa: BLE001 - 极端情况下不因判定失败而中断隔离
        return False
    normalized = raw_prefix.replace("\\", "/").rstrip("/")
    return raw == normalized or raw.startswith(normalized + "/")


def _install_import_time_isolation() -> None:
    """在 import 期安装三个落盘点的重定向补丁（幂等，可被重复调用）。

    子步骤各自 try-except：缺依赖 / 导入失败时只打印中文日志并跳过，
    绝不影响其余用例。每个补丁以 ``_bili_ops_isolated`` 属性做幂等标记，
    避免 conftest 被同时当作目录 conftest 与插件（tests.conftest）重复导入时
    二次包装（二次包装会用不同临时目录，导致行为漂移）。

    Returns:
        无。
    """
    # ---- 点 1：日志目录（LoggerManager 懒创建，此处早于首个 get_logger 调用）----
    try:
        logger_module = importlib.import_module("core.logger")
    except Exception as exc:  # noqa: BLE001 - 环境缺依赖不应连坐其他用例
        print(f"[conftest] 跳过日志隔离（导入 core.logger 失败）：{exc!r}")
    else:
        real_logger_init = logger_module.LoggerManager.__init__
        if not getattr(real_logger_init, "_bili_ops_isolated", False):

            def _isolated_logger_init(self, log_dir="data/logs", log_level="INFO"):
                """把落在仓库 data/logs 的日志目录重定向到会话沙箱，其余原样透传。

                Args:
                    self: LoggerManager 实例。
                    log_dir: 日志目录（默认值保持与生产一致，便于识别）。
                    log_level: 日志级别。

                Returns:
                    无（调用真实 __init__ 完成初始化）。
                """
                if _points_into_repo_dir(log_dir, _REPO_LOGS_DIR, "data/logs"):
                    log_dir = str(_ISOLATED_LOGS_DIR)
                real_logger_init(self, log_dir, log_level)

            _isolated_logger_init._bili_ops_isolated = True
            logger_module.LoggerManager.__init__ = _isolated_logger_init

    # ---- 点 2：抽奖缓存 JSON（仅在落盘层拦截，保持 cache_dir 语义不变）----
    try:
        cache_module = importlib.import_module("modules.lottery.cache")
    except Exception as exc:  # noqa: BLE001
        print(f"[conftest] 跳过抽奖缓存隔离（导入 modules.lottery.cache 失败）：{exc!r}")
    else:
        real_write_json = cache_module.LotteryCache._write_json
        if not getattr(real_write_json, "_bili_ops_isolated", False):

            def _isolated_write_json(self, target, payload, label):
                """把落在仓库 data/lottery_cache 的写盘目标重定向到会话沙箱。

                Args:
                    self: LotteryCache 实例。
                    target: 最终文件路径（可能为相对 Path）。
                    payload: 可 JSON 序列化数据。
                    label: 日志中的缓存名称。

                Returns:
                    无（委托真实 _write_json 完成原子写入）。
                """
                target = Path(target)
                if _points_into_repo_dir(target, _REPO_LOTTERY_DIR, "data/lottery_cache"):
                    redirected = _ISOLATED_LOTTERY_DIR / target.name
                    redirected.parent.mkdir(parents=True, exist_ok=True)
                    target = redirected
                return real_write_json(self, target, payload, label)

            _isolated_write_json._bili_ops_isolated = True
            cache_module.LotteryCache._write_json = _isolated_write_json

    # ---- 点 3：data 下的测试库文件（硬编码 data/test_hotspot_rev3.db 等）----
    try:
        manager_module = importlib.import_module("core.database.manager")
    except Exception as exc:  # noqa: BLE001
        print(f"[conftest] 跳过测试库隔离（导入 core.database.manager 失败）：{exc!r}")
    else:
        real_db_init = manager_module.DatabaseManager.__init__
        if not getattr(real_db_init, "_bili_ops_isolated", False):

            def _isolated_db_init(self, db_path="data/bili_ops.db"):
                """把落在仓库 data/ 下的 sqlite 路径重定向到会话沙箱（保留文件名）。

                Args:
                    self: DatabaseManager 实例。
                    db_path: 数据库文件路径（默认值保持与生产一致）。

                Returns:
                    无（调用真实 __init__ 完成建表与引擎创建）。
                """
                if _points_into_repo_dir(db_path, _REPO_DATA_DIR, "data"):
                    redirected = _ISOLATED_DATA_DIR / Path(db_path).name
                    redirected.parent.mkdir(parents=True, exist_ok=True)
                    db_path = str(redirected)
                real_db_init(self, db_path)

            _isolated_db_init._bili_ops_isolated = True
            manager_module.DatabaseManager.__init__ = _isolated_db_init


_install_import_time_isolation()
print(f"[conftest] 测试落盘隔离已启用 -> {_ISOLATED_ROOT}")
