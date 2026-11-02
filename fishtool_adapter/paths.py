"""数据根布局。

所有运行期数据都在 ``data/plugin_data/astrbot_plugin_fishtool/`` 下（AstrBot 官方规范，
更新插件时不会被删除，且随 AstrBot 备份）。引擎 venv 体积大，放在
``data/fishtool_runtime/``，不进 AstrBot 备份，可随时删除重建。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataLayout:
    """插件数据目录布局。

    Attributes:
        root: ``data/plugin_data/astrbot_plugin_fishtool``。
        venv_root: ``data/fishtool_runtime``。
    """

    root: Path
    venv_root: Path

    @property
    def config_dir(self) -> Path:
        """FishTool 的 config 目录：.key .secrets config.yaml budget.yaml。"""
        return self.root / "config"

    @property
    def runtime_dir(self) -> Path:
        """引擎 cwd：data/bili_ops.db、data/logs、reports 等相对路径都落在这里。"""
        return self.root / "runtime"

    @property
    def logs_dir(self) -> Path:
        """FishTool 日志目录。"""
        return self.runtime_dir / "data" / "logs"

    @property
    def engine_log(self) -> Path:
        """引擎子进程 stdout/stderr（只保留启动期崩溃信息）。"""
        return self.logs_dir / "engine_stdout.log"

    @property
    def engine_lock(self) -> Path:
        """引擎子进程持有的 OS 级独占锁文件。"""
        return self.root / "engine.lock"

    @property
    def pid_file(self) -> Path:
        """最近一次拉起的引擎 pid 与端口，用于清理孤儿进程。"""
        return self.root / "engine.pid.json"

    @property
    def remote_dir(self) -> Path:
        """二期机器人遥控的订阅与任务数据。"""
        return self.root / "remote"

    @property
    def exports_dir(self) -> Path:
        """视图下载用的临时导出文件。"""
        return self.root / "exports"

    @property
    def venv_dir(self) -> Path:
        """引擎独立 venv。"""
        return self.venv_root / "venv"

    @property
    def venv_lock(self) -> Path:
        """记录 venv 依赖哈希，哈希不变则跳过 pip。"""
        return self.venv_root / "venv.lock.json"

    @property
    def venv_python(self) -> Path:
        """venv 内的 python 可执行文件。"""
        sub = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
        return self.venv_dir.joinpath(*sub)

    def ensure(self) -> None:
        """创建必要目录（幂等）。"""
        for path in (self.config_dir, self.logs_dir, self.remote_dir, self.exports_dir, self.venv_root):
            path.mkdir(parents=True, exist_ok=True)
