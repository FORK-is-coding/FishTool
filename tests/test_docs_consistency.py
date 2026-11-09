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


def test_docs_and_entrypoint_agree_on_default_port():
    """文档说的默认端口必须和入口一致（曾经出现文档 8000 / 配置 8080 的错位）。

    只检查“把 8080 写成默认访问地址”的说法；文档可以合法地提到 8080（例如 CORS
    同时放行 8000/8080），不能用“全文不许出现 8080”来卡。
    """
    app = (ROOT / "app.py").read_text("utf-8")
    assert re.search(r"default\s*=\s*8000", app), "app.py 的默认端口不再是 8000，请同步文档"
    for name in ("README.md", "QUICKSTART.md", "STRUCTURE.md"):
        text = _read(name)
        assert not re.search(r"https?://(?:localhost|127\.0\.0\.1):8080", text), f"{name} 把 8080 写成了访问地址"
