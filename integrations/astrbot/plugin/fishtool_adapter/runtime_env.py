"""引擎独立 venv 的创建、校验与依赖安装。

为什么要独立 venv：FishTool 锁定 fastapi==0.108 / pydantic==2.5.2 等旧版本，AstrBot 要求
fastapi>=0.124 / pydantic>=2.12.5，同一解释器无法同时满足。AstrBot 安装插件时会把插件
requirements.txt 装进 AstrBot 自己的解释器，所以引擎依赖放在 engine_requirements.txt，
由本模块装进 ``data/fishtool_runtime/venv``。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from .constants import ENGINE_REQ_NAME, SUPPORTED_ENGINE_PYTHON

_log = logging.getLogger("astrbot")


class EngineEnvError(RuntimeError):
    """引擎运行环境不可用（版本不符、venv/pip 失败）。"""


def _subprocess_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def _clean_env() -> dict:
    env = {**os.environ, "PYTHONUTF8": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    # 不继承 AstrBot 的模块路径与 venv，避免 pip 装到错误位置。
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        env.pop(key, None)
    return env


async def run_cmd(*args: str, timeout: float) -> str:
    """运行子进程，返回合并后的输出；失败抛 EngineEnvError。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_clean_env(),
            **_subprocess_kwargs(),
        )
    except OSError as exc:
        raise EngineEnvError(f"无法执行 {args[0]}: {exc}") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise EngineEnvError(f"命令超时（{int(timeout)}s）: {' '.join(args[1:4])}") from exc
    except asyncio.CancelledError:
        proc.kill()
        await proc.communicate()
        raise
    text = out.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise EngineEnvError(f"命令失败（{proc.returncode}）: {' '.join(args[1:4])}\n{text[-2000:]}")
    return text


async def probe_version(python: str) -> tuple[int, int]:
    """返回解释器的 (major, minor)。"""
    out = await run_cmd(python, "-c", "import sys;print('%d.%d' % sys.version_info[:2])", timeout=60)
    major, minor = out.strip().splitlines()[-1].split(".")
    return int(major), int(minor)


def requirements_digest(req: Path) -> str:
    return hashlib.sha256(req.read_bytes()).hexdigest()


def _read_lock(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        _log.warning("[fishtool] venv.lock.json 无法读取，将重新安装依赖: %s", exc)
        return {}


async def ensure_engine_env(layout, plugin_dir: Path, base_python: str = "", index_url: str = "", on_progress=None) -> Path:
    """创建/复用引擎 venv；requirements 内容哈希不变则跳过安装。

    Args:
        layout: DataLayout。
        plugin_dir: 插件目录（含 engine_requirements.txt）。
        base_python: 用来创建 venv 的解释器；空 = AstrBot 当前解释器。
        index_url: pip 镜像地址。
        on_progress: 可选回调 ``(message: str) -> None``，用于面板显示进度。

    Returns:
        Path: venv 内 python 路径。
    """
    def progress(msg: str) -> None:
        _log.info("[fishtool] %s", msg)
        if on_progress:
            on_progress(msg)

    req = plugin_dir / ENGINE_REQ_NAME
    if not req.is_file():
        raise EngineEnvError(f"缺少 {ENGINE_REQ_NAME}，插件包不完整")
    digest = requirements_digest(req)
    lock = _read_lock(layout.venv_lock)
    py = layout.venv_python
    base = (base_python or "").strip() or sys.executable
    if py.exists() and lock.get("req_sha256") == digest and (not base_python or lock.get("base") == base):
        return py

    progress("检查引擎 Python 版本")
    version = await probe_version(base)
    if version not in SUPPORTED_ENGINE_PYTHON:
        raise EngineEnvError(
            f"引擎需要 Python 3.11 或 3.12，当前 {base} 是 {version[0]}.{version[1]}；"
            "请在插件配置 engine.python 填写 3.11/3.12 解释器路径"
        )

    if py.exists() and lock.get("base") and lock.get("base") != base:
        progress("引擎解释器已更换，重建运行环境")
        shutil.rmtree(layout.venv_dir, ignore_errors=True)
    if not py.exists():
        progress("创建引擎运行环境（venv）")
        try:
            await run_cmd(base, "-m", "venv", str(layout.venv_dir), timeout=600)
        except EngineEnvError as exc:
            raise EngineEnvError(
                f"无法用 {base} 创建 venv（AstrBot 桌面客户端的内置 Python 可能不带 venv）。"
                f"请在插件配置 engine.python 填写外部 Python 3.11/3.12。\n{exc}"
            ) from exc

    progress("安装引擎依赖（首次约需数分钟，需要联网）")
    pip = [str(py), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-r", str(req)]
    if index_url:
        pip += ["-i", index_url]
    await run_cmd(*pip, timeout=1800)
    layout.venv_lock.write_text(json.dumps({"req_sha256": digest, "base": base}), "utf-8")
    progress("引擎依赖已就绪")
    return py


def remove_engine_env(layout) -> bool:
    """删除引擎 venv（插件视图“清理运行环境”）。

    Returns:
        bool: 是否删除了任何内容。
    """
    removed = False
    if layout.venv_dir.exists():
        shutil.rmtree(layout.venv_dir)
        removed = True
    if layout.venv_lock.exists():
        layout.venv_lock.unlink()
        removed = True
    return removed
