"""引擎子进程守护：拉起、健康检查、退避重启、优雅停止、孤儿清理。

- 退出通道用 stdin 管道：关闭 stdin → launcher 设 ``server.should_exit``，执行 FastAPI lifespan
  关闭阶段。AstrBot 进程崩溃时管道断开，引擎同样会退出。Windows 上不用 CTRL_BREAK（与
  CREATE_NO_WINDOW 组合时无效）。
- 单写者：launcher 对数据根持有 OS 级独占锁；pid 文件只用于清理孤儿进程。
- 引擎每次启动会对 Cookie 池逐个做 nav 校验，看门狗退避（2 s → 300 s）也用于限制反复重启。
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path

import httpx

from .constants import EXIT_LOCK_CONFLICT


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EngineSupervisor:
    """管理一个 FishTool 引擎子进程。"""

    def __init__(self, layout, plugin_dir: Path, cfg: dict, logger):
        self.layout = layout
        self.plugin_dir = plugin_dir
        self.cfg = cfg
        self.log = logger
        self.proc: asyncio.subprocess.Process | None = None
        self.port = 0
        self.token = ""
        self.state = "stopped"  # stopped|preparing|starting|running|crashed|error
        self.last_error = ""
        self.progress = ""
        self.started_at: float | None = None
        self.restarts = 0
        self.final = False  # 永久关闭标志，只能由 stop(final=True) 设置
        self._python = ""
        self._stopping = False
        self._watch: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def launcher_path(self) -> Path:
        return self.plugin_dir / "engine_launcher" / "launcher.py"

    @property
    def engine_root(self) -> Path:
        return self.plugin_dir / "engine" / "fishtool"

    def snapshot(self) -> dict:
        """供 _status 接口返回的状态（不含 token）。"""
        proc = self.proc
        return {
            "state": self.state,
            "last_error": self.last_error,
            "progress": self.progress,
            "pid": proc.pid if proc and proc.returncode is None else None,
            "port": self.port or None,
            "started_at": self.started_at,
            "restarts": self.restarts,
        }

    async def start(self, python: str) -> None:
        """启动引擎（已在运行则直接返回）。"""
        async with self._lock:
            if self.final:
                raise RuntimeError("supervisor closed")
            if self.proc and self.proc.returncode is None:
                return
            self._stopping = False
            self._python = python
            await self._spawn_once_locked()
            if self._watch is None or self._watch.done():
                self._watch = asyncio.create_task(self._watchdog(), name="fishtool-engine-watchdog")

    async def _spawn_once_locked(self) -> None:
        """拉起一次并等待健康；锁冲突时清理孤儿后重试一次。调用方须持有 _lock。"""
        await self.kill_orphan()
        for attempt in (1, 2):
            if self.final or self._stopping:
                raise RuntimeError("supervisor stopping")
            rc = await self._spawn_and_wait()
            if rc is None:
                return
            if rc == EXIT_LOCK_CONFLICT and attempt == 1:
                self.log.warning("[fishtool] 数据根被另一个引擎占用，清理后重试")
                await self.kill_orphan(force_any=True)
                await asyncio.sleep(1.0)
                continue
            tail = self._boot_log_tail()
            raise RuntimeError(f"engine exited during startup: code {rc}" + (f"\n{tail}" if tail else ""))

    async def _spawn_and_wait(self) -> int | None:
        """返回 None 表示已健康运行；否则返回启动期退出码。"""
        self.port = int(self.cfg.get("port") or 0) or free_port()
        self.token = secrets.token_urlsafe(32)  # 每次启动轮换
        env = {
            **os.environ,
            "FISHTOOL_LOCAL_SESSION_TOKEN": self.token,  # web/local_guard.py:69
            "FISHTOOL_ALLOWED_HOSTS": "127.0.0.1,localhost",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_PROXY": "127.0.0.1,localhost",
        }
        for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            env.pop(key, None)
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        self.layout.logs_dir.mkdir(parents=True, exist_ok=True)
        boot_log = self.layout.logs_dir / "engine_boot.log"
        self.state = "starting"
        self.progress = "启动引擎"
        with open(boot_log, "wb") as boot_fh:
            self.proc = await asyncio.create_subprocess_exec(
                self._python,
                str(self.launcher_path),
                "--engine-root", str(self.engine_root),
                "--data-root", str(self.layout.root),
                "--port", str(self.port),
                "--stdout-log", str(self.layout.engine_log),
                stdin=asyncio.subprocess.PIPE,
                stdout=boot_fh,
                stderr=boot_fh,
                env=env,
                cwd=str(self.layout.runtime_dir),
                **kwargs,
            )
        self.layout.pid_file.write_text(
            json.dumps({"pid": self.proc.pid, "port": self.port, "data_root": str(self.layout.root)}), "utf-8"
        )
        rc = await self._wait_healthy(timeout=90)
        if rc is not None:
            return rc
        self.state = "running"
        self.progress = ""
        self.last_error = ""
        self.started_at = time.time()
        self.log.info(f"[fishtool] engine running pid={self.proc.pid} port={self.port}")
        return None

    async def _wait_healthy(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            while time.monotonic() < deadline:
                if self.proc.returncode is not None:
                    return self.proc.returncode
                try:
                    resp = await client.get(f"{self.base_url}/health")
                    if resp.status_code == 200:
                        return None
                except httpx.HTTPError as exc:  # 引擎尚未监听，继续等待
                    self.log.debug(f"[fishtool] health probe: {exc!r}")
                await asyncio.sleep(0.5)
        await self._terminate_proc(self.proc, grace=5)
        tail = self._boot_log_tail()
        raise RuntimeError("engine health check timeout" + (f"\n{tail}" if tail else ""))

    def _boot_log_tail(self, limit: int = 1500) -> str:
        path = self.layout.logs_dir / "engine_boot.log"
        try:
            data = path.read_bytes()[-limit:]
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace").strip()

    async def _watchdog(self) -> None:
        backoff = 2
        while not self._stopping and not self.final:
            proc = self.proc
            if proc is None:
                return
            rc = await proc.wait()
            if self._stopping or self.final:
                return
            self.state, self.last_error = "crashed", f"引擎意外退出（code {rc}）"
            self.log.warning(f"[fishtool] engine exited {rc}, restart in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
            async with self._lock:
                if self._stopping or self.final:
                    return
                try:
                    await self._spawn_once_locked()
                    self.restarts += 1
                    backoff = 2
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 重启失败继续退避重试
                    self.state, self.last_error = "crashed", str(exc)
                    self.log.warning(f"[fishtool] engine restart failed: {exc}")

    async def _terminate_proc(self, proc, grace: float) -> None:
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception as exc:  # noqa: BLE001 - 管道已断开时只能继续强制结束
            self.log.warning(f"[fishtool] close engine stdin failed: {exc}")
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except asyncio.TimeoutError:
            self.log.warning("[fishtool] engine did not exit gracefully, killing")
            try:
                proc.kill()
            except ProcessLookupError:
                return
            await proc.wait()

    async def stop(self, grace: float = 15, final: bool = False) -> None:
        """停止引擎。final=True 表示插件实例被 terminate，之后拒绝再启动。"""
        if final:
            self.final = True
        self._stopping = True
        watch = self._watch
        if watch and watch is not asyncio.current_task() and not watch.done():
            watch.cancel()
            try:
                await watch
            except asyncio.CancelledError:
                self.log.debug("[fishtool] watchdog cancelled")
        async with self._lock:
            await self._terminate_proc(self.proc, grace)
            self.layout.pid_file.unlink(missing_ok=True)
            self.state = "stopped"
            self.progress = ""
            self.started_at = None

    async def kill_orphan(self, force_any: bool = False) -> None:
        """结束上次 AstrBot 异常退出遗留的引擎（在线程中执行，不阻塞事件循环）。

        只结束命令行包含 launcher.py 且 --data-root 指向本数据根的进程。
        force_any=True 时额外扫描所有进程（锁冲突但 pid 文件已丢失的情况）。
        """
        own_pid = self.proc.pid if self.proc is not None and self.proc.returncode is None else None
        await asyncio.to_thread(self._kill_orphan_sync, force_any, own_pid)

    def _kill_orphan_sync(self, force_any: bool, own_pid: int | None) -> None:
        try:
            import psutil
        except ImportError:
            self.log.warning("[fishtool] psutil unavailable, skip orphan cleanup")
            return
        root = str(self.layout.root)
        candidates: list = []
        if self.layout.pid_file.is_file():
            try:
                pid = int(json.loads(self.layout.pid_file.read_text("utf-8")).get("pid") or 0)
            except (OSError, ValueError) as exc:
                self.log.warning(f"[fishtool] pid file unreadable: {exc}")
                pid = 0
            if pid and pid != own_pid:
                try:
                    candidates.append(psutil.Process(pid))
                except psutil.Error:
                    self.log.debug(f"[fishtool] orphan pid {pid} not alive")
        if force_any:
            for proc in psutil.process_iter(["pid", "cmdline"]):
                if proc.pid == own_pid:
                    continue
                candidates.append(proc)
        for proc in candidates:
            try:
                cmdline = proc.cmdline()
            except psutil.Error as exc:  # 进程已退出或无权限读取命令行
                self.log.debug(f"[fishtool] skip pid {proc.pid}: {exc!r}")
                continue
            joined = " ".join(cmdline)
            if "launcher.py" not in joined or "--data-root" not in cmdline:
                continue
            idx = cmdline.index("--data-root")
            if idx + 1 >= len(cmdline) or os.path.normcase(os.path.abspath(cmdline[idx + 1])) != os.path.normcase(root):
                continue
            self.log.warning(f"[fishtool] killing orphan engine pid={proc.pid}")
            try:
                proc.kill()
                proc.wait(timeout=10)
            except psutil.Error as exc:
                self.log.warning(f"[fishtool] kill orphan {proc.pid} failed: {exc}")
        self.layout.pid_file.unlink(missing_ok=True)
