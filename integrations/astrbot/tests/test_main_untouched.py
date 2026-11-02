"""守护：开发分支相对 main 只新增 integrations/astrbot/ 与 astrbot-* workflow，不改主线文件。

环境变量 ``FISHTOOL_BASE_REF`` 指定比较基准（默认 origin/main）。合回 main 后，主线功能 PR 可设
``FISHTOOL_SKIP_UNTOUCHED=1`` 跳过。
"""
from __future__ import annotations

import os
import subprocess

import pytest

from conftest import REPO_ROOT

ALLOWED_PREFIXES = ("integrations/astrbot/",)
ALLOWED_FILES_RE = ("astrbot-",)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


def _allowed(path: str) -> bool:
    if path.startswith(ALLOWED_PREFIXES):
        return True
    if path.startswith(".github/workflows/"):
        return path.rsplit("/", 1)[-1].startswith(ALLOWED_FILES_RE)
    return False


def test_branch_only_adds_adapter_files():
    if os.environ.get("FISHTOOL_SKIP_UNTOUCHED") == "1":
        pytest.skip("explicitly skipped")
    base = os.environ.get("FISHTOOL_BASE_REF", "origin/main")
    if _git("rev-parse", "--verify", base).returncode != 0:
        pytest.skip(f"base ref {base} not available")
    merge_base = _git("merge-base", base, "HEAD")
    if merge_base.returncode != 0:
        pytest.skip("no merge base")
    committed = _git("diff", "--name-only", f"{merge_base.stdout.strip()}...HEAD").stdout.split()
    working = _git("diff", "--name-only", "HEAD").stdout.split()
    untracked = _git("ls-files", "--others", "--exclude-standard").stdout.split()
    changed = sorted(set(committed + working + untracked))
    offenders = [p for p in changed if not _allowed(p)]
    assert not offenders, f"适配分支改动了主线文件: {offenders}"
