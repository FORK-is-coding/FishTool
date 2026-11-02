"""数据根布局、默认配置。"""
from __future__ import annotations

from pathlib import Path

from fishtool_adapter.constants import DEFAULT_ENGINE_CFG, engine_cfg
from fishtool_adapter.paths import DataLayout


def test_layout(tmp_path: Path):
    layout = DataLayout(root=tmp_path / "plugin_data" / "astrbot_plugin_fishtool", venv_root=tmp_path / "fishtool_runtime")
    layout.ensure()
    assert layout.config_dir == layout.root / "config"
    assert layout.runtime_dir == layout.root / "runtime"
    assert layout.engine_log.parent == layout.runtime_dir / "data" / "logs"
    assert layout.config_dir.is_dir() and layout.logs_dir.is_dir() and layout.venv_root.is_dir()
    # venv 不在 plugin_data 内（不进 AstrBot 备份）
    assert layout.root not in layout.venv_dir.parents
    assert layout.venv_python.parent.parent == layout.venv_dir


def test_engine_cfg_defaults():
    assert engine_cfg(None) == DEFAULT_ENGINE_CFG
    merged = engine_cfg({"port": 9000, "python": None})
    assert merged["port"] == 9000
    assert merged["python"] == ""
    assert merged["long_request_timeout_s"] == 1800
