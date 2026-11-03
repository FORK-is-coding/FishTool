"""tests/conftest.py 的落盘隔离在 Windows 受限令牌下也能用。

背景见 conftest 中 ``_relax_private_mkdir_on_windows`` 的说明（gh-118486 / CVE-2024-4030）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="只在 Windows 上放宽 0o700")
@pytest.mark.skipif(
    os.environ.get("BILI_TEST_RELAX_TMP_ACL", "").strip() == "0", reason="已用 BILI_TEST_RELAX_TMP_ACL=0 关闭"
)
def test_mkdir_0o700_relaxed_on_windows():
    """测试进程里 os.mkdir 已包装，mkdtemp 建的目录继承父目录 ACL。"""
    assert getattr(os.mkdir, "_bili_ops_relaxed", False)


def test_isolation_root_is_usable(tmp_path):
    """隔离根与 tmp_path 下都能建子目录、写文件。"""
    roots = {
        Path(m._ISOLATED_ROOT)
        for m in list(sys.modules.values())
        if isinstance(getattr(m, "_ISOLATED_ROOT", None), Path)
    }
    assert roots, "没有找到 conftest 的隔离根目录"
    for base in (*roots, tmp_path):
        sub = base / "probe_child" / "nested"
        sub.mkdir(parents=True)
        (sub / "f.txt").write_text("ok", encoding="utf-8")


def _run_pytest(env_extra: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra, "PYTHONUTF8": "1"}
    env.pop("PYTEST_XDIST_WORKER", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )


def test_unwritable_iso_root_gives_single_usage_error(tmp_path):
    """BILI_TEST_ISO_ROOT 不可用时只报一条明确错误，而不是每个文件一个收集错误。"""
    bad = tmp_path / "not_a_dir"
    bad.write_text("x", encoding="utf-8")  # 文件占位：在它下面建目录必然失败
    proc = _run_pytest({"BILI_TEST_ISO_ROOT": str(bad / "iso")}, "tests/test_charge_count.py")
    output = proc.stdout + proc.stderr
    assert proc.returncode == 4, output[-2000:]
    assert "BILI_TEST_ISO_ROOT" in output
    assert "ERROR collecting" not in output


def test_isolation_root_removed_after_session(tmp_path):
    """会话结束删除隔离根目录（BILI_TEST_ISO_ROOT 指定的父目录本身保留）。"""
    parent = tmp_path / "iso_parent"
    proc = _run_pytest({"BILI_TEST_ISO_ROOT": str(parent)}, "tests/test_charge_count.py")
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    assert parent.is_dir()
    assert list(parent.iterdir()) == []

    keep = tmp_path / "iso_keep"
    proc = _run_pytest({"BILI_TEST_ISO_ROOT": str(keep), "BILI_TEST_KEEP_ISO": "1"}, "tests/test_charge_count.py")
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    assert any(p.name.startswith("bili_ops_test_isolation_") for p in keep.iterdir())


def test_mkdtemp_children_ok_under_relaxed_mkdir():
    """放宽后 mkdtemp 目录可建子目录（普通令牌下也应成立，确保包装没有破坏 mkdtemp）。"""
    d = Path(tempfile.mkdtemp(prefix="bili_ops_relax_check_"))
    try:
        (d / "a" / "b").mkdir(parents=True)
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)
