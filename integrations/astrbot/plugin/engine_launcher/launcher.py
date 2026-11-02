"""FishTool 引擎启动器：零改源码的路径重定向，然后启动 uvicorn。

由引擎 venv 的 python 直接执行，不被 AstrBot import。

FishTool 运行时代码定位持久化路径的方式（origin/main f5500f2）：

- ``config/{config.yaml,user_config.yaml,.secrets,.key,budget.yaml}``：frozen 时取
  ``Path(sys.executable).resolve().parent / "config"``（core/config.py:53-70、
  core/quota_store.py:58-68、core/request_budget.py:171-181、modules/hotspot/risk_control.py:335-346）。
- ``data/bili_ops.db``、``data/logs``、``data/hotspot``、``data/lottery_cache``、``reports/``：相对 cwd。
- 静态资源：有 ``sys._MEIPASS`` 时取 ``_MEIPASS/web``，否则按 ``__file__``（web/main.py:565-588）。

因此在 import FishTool 任何模块之前：设 ``sys.frozen=True``、把 ``sys.executable`` 指到
``<data_root>/fishtool-engine``（config 目录 = ``<data_root>/config``）、不设 ``_MEIPASS``、
``chdir(<data_root>/runtime)``。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

#: 与 fishtool_adapter.constants.EXIT_LOCK_CONFLICT 保持一致（launcher 不能 import 适配层）。
EXIT_LOCK_CONFLICT = 3

#: 必须保持 open 的锁文件句柄；进程退出时由操作系统释放锁。
_LOCK_HANDLE = None


def _atomic_write(dst: Path, data: bytes) -> None:
    """先写临时文件再替换，避免中途失败留下半个配置文件。"""
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def _fill_missing(dst: dict, src: dict) -> bool:
    """把 src 中 dst 缺失的键递归补进 dst，不覆盖已有值。

    Returns:
        bool: dst 是否被修改。
    """
    changed = False
    for key, value in src.items():
        if key not in dst:
            dst[key] = value
            changed = True
        elif isinstance(value, dict) and isinstance(dst[key], dict):
            changed |= _fill_missing(dst[key], value)
    return changed


def seed_defaults(engine_root: Path, config_dir: Path) -> None:
    """把快照内默认配置合并到数据根。

    - ``budget.yaml``：不存在才复制（账本由用户维护）。
    - ``config.yaml``：不存在则整份复制；存在则只补缺失的键（插件升级新增的配置段能进入用户配置）。

    Args:
        engine_root: 引擎快照根目录（含 ``config/`` 默认模板）。
        config_dir: 数据根的 config 目录。
    """
    import yaml

    config_dir.mkdir(parents=True, exist_ok=True)
    src_budget = engine_root / "config" / "budget.yaml"
    dst_budget = config_dir / "budget.yaml"
    if src_budget.is_file() and not dst_budget.exists():
        _atomic_write(dst_budget, src_budget.read_bytes())

    src_cfg = engine_root / "config" / "config.yaml"
    dst_cfg = config_dir / "config.yaml"
    if not src_cfg.is_file():
        return
    if not dst_cfg.exists():
        _atomic_write(dst_cfg, src_cfg.read_bytes())
        return
    current = yaml.safe_load(dst_cfg.read_text("utf-8")) or {}
    defaults = yaml.safe_load(src_cfg.read_text("utf-8")) or {}
    if isinstance(current, dict) and isinstance(defaults, dict) and _fill_missing(current, defaults):
        _atomic_write(dst_cfg, yaml.safe_dump(current, allow_unicode=True, sort_keys=False).encode("utf-8"))


def apply_redirect(engine_root: Path, data_root: Path) -> Path:
    """路径重定向。必须在 import FishTool 任意模块之前调用。

    Args:
        engine_root: 引擎快照根目录（``<plugin>/engine/fishtool``）。
        data_root: 插件数据根（``data/plugin_data/astrbot_plugin_fishtool``）。

    Returns:
        Path: 引擎运行目录（cwd）。
    """
    engine_root = engine_root.resolve()
    data_root = data_root.resolve()
    runtime_dir = data_root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    # 子进程若需要真实解释器路径可从这里取（FishTool 运行时代码目前没有 subprocess 调用）。
    os.environ["FISHTOOL_ASTRBOT_REAL_PYTHON"] = sys.executable
    os.environ["FISHTOOL_ASTRBOT"] = "1"
    # config_dir = Path(sys.executable).resolve().parent / "config" → sys.executable 必须直接位于 data_root 下。
    sys.frozen = True  # type: ignore[attr-defined]
    sys.executable = str(data_root / "fishtool-engine")
    if hasattr(sys, "_MEIPASS"):
        raise RuntimeError("sys._MEIPASS must not be set: static assets are located via __file__")

    seed_defaults(engine_root, data_root / "config")
    os.chdir(runtime_dir)
    if str(engine_root) not in sys.path:
        sys.path.insert(0, str(engine_root))
    return runtime_dir


def acquire_data_root_lock(data_root: Path) -> bool:
    """对数据根加 OS 级独占锁（单写者），进程退出自动释放。

    Returns:
        bool: True 表示拿到锁；False 表示已有引擎在使用这个数据根。
    """
    global _LOCK_HANDLE
    handle = open(data_root / "engine.lock", "a+b")  # noqa: SIM115 - 句柄需要在进程生命周期内保持打开
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _LOCK_HANDLE = handle
    return True


class RotatingStream:
    """按大小轮转的文本流，替换 sys.stdout / sys.stderr。

    FishTool 的控制台 handler 会把与 app.log 相同的内容写到 stdout；引擎长期运行时
    不能让它无限增长。超过 ``max_bytes`` 时把当前文件改名为 ``.1`` 再新开。
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, path: Path, max_bytes: int = 5 * 1024 * 1024):
        self._path = path
        self._max = max_bytes
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115

    def _rotate(self) -> None:
        self._fh.close()
        backup = self._path.with_name(self._path.name + ".1")
        try:
            os.replace(self._path, backup)
        except OSError as exc:
            sys.__stderr__ and sys.__stderr__.write(f"[launcher] rotate failed: {exc}\n")
        self._fh = open(self._path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115

    def write(self, text: str) -> int:
        with self._lock:
            self._fh.write(text)
            self._fh.flush()
            if self._fh.tell() > self._max:
                self._rotate()
        return len(text)

    def flush(self) -> None:
        with self._lock:
            self._fh.flush()

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._fh.fileno()


def _stdin_watch(server) -> None:
    """父进程关闭 stdin 或崩溃导致管道断开 → 请求 uvicorn 优雅退出（执行 FastAPI lifespan 关闭阶段）。"""
    try:
        while sys.stdin.buffer.read(1):
            continue
    except (OSError, ValueError) as exc:
        print(f"[launcher] stdin watch stopped: {exc}", file=sys.stderr)
    finally:
        server.should_exit = True


def main(argv: list[str] | None = None) -> int:
    """启动引擎。

    Returns:
        int: 进程退出码。
    """
    parser = argparse.ArgumentParser(description="FishTool engine launcher for AstrBot")
    parser.add_argument("--engine-root", required=True, help="<plugin>/engine/fishtool")
    parser.add_argument("--data-root", required=True, help="data/plugin_data/astrbot_plugin_fishtool")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--stdout-log", default="", help="轮转写入的 stdout/stderr 日志文件")
    args = parser.parse_args(argv)

    if not os.environ.get("FISHTOOL_LOCAL_SESSION_TOKEN"):
        print("[launcher] FISHTOOL_LOCAL_SESSION_TOKEN is required", file=sys.stderr)
        return 2

    data_root = Path(args.data_root).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    if args.stdout_log:
        # 早期崩溃信息已经写进父进程给的 engine_boot.log；之后的输出改走轮转文件。
        stream = RotatingStream(Path(args.stdout_log))
        sys.stdout = stream  # type: ignore[assignment]
        sys.stderr = stream  # type: ignore[assignment]
    if not acquire_data_root_lock(data_root):
        print("[launcher] another engine holds engine.lock", file=sys.stderr)
        return EXIT_LOCK_CONFLICT

    apply_redirect(Path(args.engine_root), data_root)

    import uvicorn

    config = uvicorn.Config(
        "web.main:app",
        host="127.0.0.1",
        port=args.port,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    threading.Thread(target=_stdin_watch, args=(server,), daemon=True, name="stdin-watch").start()
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
