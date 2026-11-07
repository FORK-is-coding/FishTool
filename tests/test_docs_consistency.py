"""文档与配置的一致性：目录树、日志路径、端口（#123）。

这类断言只读文本，不 import 被测模块，避免文档和代码各自漂移后没人发现。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text("utf-8")


def test_structure_tree_lists_real_module_dirs():
    text = _read("STRUCTURE.md")
    dirs = sorted(p.name for p in (ROOT / "modules").iterdir() if p.is_dir() and p.name != "__pycache__")
    assert dirs, "modules/ 下应该至少有一个功能模块"
    for name in dirs:
        assert f"{name}/" in text, f"STRUCTURE.md 目录树缺少 modules/{name}/"


def test_structure_tree_drops_stale_dirs():
    """曾经写进树里、后来被删掉的目录不能再出现在文档里。"""
    text = _read("STRUCTURE.md")
    for stale in ("gui/", "modules/analysis", "modules/diagnosis", "modules/filter", "web/api/", "web/templates/", "PROGRESS.md"):
        assert stale not in text, f"STRUCTURE.md 仍提到已不存在的 {stale}"


def test_readme_points_logs_to_data_dir():
    """日志实际写在 data/logs（core/logger.py 的默认 log_dir）。"""
    text = _read("README.md")
    assert "`data/logs/`" in text
    assert not re.search(r"日志在 `logs/`", text)


def test_config_yaml_has_no_dead_server_block():
    """监听端口来自命令行参数（app.py 的 --port，默认 8000），yaml 里的 server 段没人读。"""
    text = _read("config/config.yaml")
    assert not re.search(r"(?m)^server:", text), "config/config.yaml 的 server 段是死配置"
    assert "port: 8080" not in text


def test_docs_and_entrypoint_agree_on_default_port():
    """文档说的 8000 必须和入口的默认值一致（曾经出现文档 8000 / 配置 8080 的错位）。"""
    app = (ROOT / "app.py").read_text("utf-8")
    assert 'default=8000' in app
    for name in ("README.md", "QUICKSTART.md", "STRUCTURE.md"):
        text = _read(name)
        assert "8080" not in text, f"{name} 仍在写 8080"
