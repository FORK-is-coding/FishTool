"""发布包校验：插件包必须随附 AGPL-3.0 LICENSE，且适配器模块与视图资源齐全。

插件包含 FishTool 引擎快照，需要随附仓库许可证；发布到插件仓库是整树替换，
包里没有 LICENSE 会把目标仓库已有的许可证删掉。
适配器模块由构建期 ``copytree`` 整目录复制，缺文件只可能来自源树不完整——
旧版校验只要求 ``fishtool_adapter/__init__.py``，少了 commands 之类模块也能通过。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from release_tools.verify_package import REQUIRED_ADAPTER_MODULES, verify

ASTRBOT_DIR = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ASTRBOT_DIR / "plugin"
REPO_ROOT = ASTRBOT_DIR.parents[1]

AGPL_HEAD = "                    GNU AFFERO GENERAL PUBLIC LICENSE\n                       Version 3, 19 November 2007\n"

#: build_plugin.py 生成、源树里并不存在的产物（只校验存在性，内容用占位）。
_GENERATED = {
    "engine/fishtool/web/main.py": "# stub\n",
    "engine/fishtool/core/config.py": "# stub\n",
    "engine/fishtool/config/config.yaml": "{}\n",
    "engine/fishtool/config/budget.yaml": "{}\n",
    "engine_requirements.txt": "uvicorn[standard]\n",
    "views/console/index.html": "<html></html>\n",
    "views/console/bridge-shim.js": "// stub\n",
    "views/console/shim.css": "/* stub */\n",
    "views/console/astrbot-overrides.js": "// stub\n",
}


def _license_errors(root: Path) -> list[str]:
    return [e for e in verify(root) if "LICENSE" in e]


def _make_package(root: Path) -> Path:
    """按 build_plugin.py 的形态组装一个能通过校验的发布树。"""
    shutil.copytree(PLUGIN_SRC, root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(REPO_ROOT / "LICENSE", root / "LICENSE")
    (root / "engine").mkdir(parents=True, exist_ok=True)
    (root / "engine" / "VERSION").write_text("deadbeef v0.1.0\n", "utf-8")
    for rel, body in _GENERATED.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, "utf-8")
    return root


def test_missing_license_rejected(tmp_path: Path):
    errors = _license_errors(tmp_path)
    assert "missing: LICENSE" in errors
    assert any("AGPL-3.0" in e for e in errors)


def test_mit_license_rejected(tmp_path: Path):
    (tmp_path / "LICENSE").write_text("MIT License\n\nCopyright (c) 2026 FORK\n", "utf-8")
    assert any("AGPL-3.0" in e for e in _license_errors(tmp_path))


def test_agpl_license_accepted(tmp_path: Path):
    (tmp_path / "LICENSE").write_text(AGPL_HEAD + "\n Preamble\n", "utf-8")
    assert _license_errors(tmp_path) == []


def test_adapter_module_list_matches_source_tree():
    """REQUIRED_ADAPTER_MODULES 必须与源树里的适配器模块一一对应（增删模块时同步）。"""
    source = {p.relative_to(PLUGIN_SRC).as_posix() for p in (PLUGIN_SRC / "fishtool_adapter").rglob("*.py")}
    assert set(REQUIRED_ADAPTER_MODULES) == source


def test_complete_package_passes(tmp_path: Path):
    root = _make_package(tmp_path / "plugin")
    assert verify(root) == []


def test_missing_adapter_module_fails(tmp_path: Path):
    root = _make_package(tmp_path / "plugin")
    (root / "fishtool_adapter" / "commands.py").unlink()
    assert "missing: fishtool_adapter/commands.py" in verify(root)


def test_missing_view_shim_css_fails(tmp_path: Path):
    root = _make_package(tmp_path / "plugin")
    (root / "views" / "console" / "shim.css").unlink()
    assert "missing: views/console/shim.css" in verify(root)
