"""发布包校验：插件包必须随附 AGPL-3.0 LICENSE。

插件包含 FishTool 引擎快照，需要随附仓库许可证；发布到插件仓库是整树替换，
包里没有 LICENSE 会把目标仓库已有的许可证删掉。
"""
from __future__ import annotations

from pathlib import Path

from release_tools.verify_package import verify

AGPL_HEAD = "                    GNU AFFERO GENERAL PUBLIC LICENSE\n                       Version 3, 19 November 2007\n"


def _license_errors(root: Path) -> list[str]:
    return [e for e in verify(root) if "LICENSE" in e]


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
