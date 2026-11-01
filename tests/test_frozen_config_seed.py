"""冻结环境（PyInstaller exe）首次启动复制随包 budget.yaml 的回归测试。

背景：request_budget / quota_store / risk_control 在冻结环境只读 exe 同级的
config/budget.yaml，build.spec 却只把它打进 _MEIPASS。exe 旁边没有这个文件时，
配额维护每轮报 FileNotFoundError，配额总闸不拦截，热点发现返回 budget_unavailable。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import app as entry
from core import quota_store, request_budget
from modules.hotspot import risk_control

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_BUDGET = REPO_ROOT / "config" / "budget.yaml"


def _make_bundle(tmp_path: Path) -> Path:
    """模拟 _MEIPASS：放一份仓库里的 budget.yaml。"""
    bundle = tmp_path / "bundle"
    (bundle / "config").mkdir(parents=True)
    shutil.copyfile(REPO_BUDGET, bundle / "config" / "budget.yaml")
    return bundle


def _fake_frozen(monkeypatch, bundle: Path, exe_dir: Path) -> None:
    """把当前进程伪装成 exe：sys.frozen / sys._MEIPASS / sys.executable。"""
    exe_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "FishTool.exe"))


@pytest.fixture
def clear_budget_caches():
    """账本读取函数带进程内缓存，测试前后都清掉，避免串到其它用例。"""
    caches = (request_budget.load_quota_limits, request_budget.load_cooldown_limits, quota_store._window_params)
    for fn in caches:
        fn.cache_clear()
    yield
    for fn in caches:
        fn.cache_clear()


def test_build_spec_bundles_config_yaml():
    """复制的前提：build.spec 把 config/*.yaml 打进包内 config/。"""
    spec = (REPO_ROOT / "build.spec").read_text(encoding="utf-8")
    assert "(os.path.join(project_root, 'config', '*.yaml'), 'config')" in spec
    assert REPO_BUDGET.is_file()


def test_first_frozen_launch_copies_budget(monkeypatch, tmp_path):
    """exe 旁边没有 budget.yaml 时，从包内复制一份，内容逐字节一致。"""
    bundle, exe_dir = _make_bundle(tmp_path), tmp_path / "exe"
    _fake_frozen(monkeypatch, bundle, exe_dir)

    copied = entry.seed_bundled_config()

    dst = exe_dir / "config" / "budget.yaml"
    assert copied == [dst]
    assert dst.read_bytes() == REPO_BUDGET.read_bytes()
    assert not (exe_dir / "config" / "budget.yaml.tmp").exists()


def test_existing_budget_is_not_overwritten(monkeypatch, tmp_path):
    """用户改过的账本保持原样，第二次启动也不再复制。"""
    bundle, exe_dir = _make_bundle(tmp_path), tmp_path / "exe"
    _fake_frozen(monkeypatch, bundle, exe_dir)
    dst = exe_dir / "config" / "budget.yaml"
    dst.parent.mkdir(parents=True)
    dst.write_text("# user edited\n", encoding="utf-8")

    assert entry.seed_bundled_config() == []
    assert dst.read_text(encoding="utf-8") == "# user edited\n"


def test_source_run_does_nothing(monkeypatch, tmp_path):
    """源码环境（非 frozen）不复制任何文件，仓库 config/ 不被写入。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(_make_bundle(tmp_path)), raising=False)
    before = sorted(p.name for p in (REPO_ROOT / "config").iterdir())

    assert entry.seed_bundled_config() == []
    assert sorted(p.name for p in (REPO_ROOT / "config").iterdir()) == before


def test_missing_bundle_file_is_skipped(monkeypatch, tmp_path):
    """包内没有 budget.yaml 时只记日志，不抛异常、不建空文件。"""
    bundle, exe_dir = tmp_path / "bundle", tmp_path / "exe"
    bundle.mkdir()
    _fake_frozen(monkeypatch, bundle, exe_dir)

    assert entry.seed_bundled_config() == []
    assert not (exe_dir / "config" / "budget.yaml").exists()


def test_copy_failure_leaves_no_partial_file(monkeypatch, tmp_path):
    """复制失败（如目录只读）时不抛异常，也不留下临时文件或半个账本。"""
    bundle, exe_dir = _make_bundle(tmp_path), tmp_path / "exe"
    _fake_frozen(monkeypatch, bundle, exe_dir)

    def _boom(src, dst):
        Path(dst).write_bytes(b"partial")
        raise PermissionError("read-only")

    monkeypatch.setattr(entry.shutil, "copyfile", _boom)

    assert entry.seed_bundled_config() == []
    assert not (exe_dir / "config" / "budget.yaml").exists()
    assert not (exe_dir / "config" / "budget.yaml.tmp").exists()


def test_main_seeds_before_dispatch(monkeypatch):
    """所有子命令进入前都会先补齐配置（桌面版的 Web 服务在同进程内启动）。"""
    order = []
    monkeypatch.setattr(entry, "seed_bundled_config", lambda: order.append("seed") or [])
    monkeypatch.setattr(entry, "run_desktop", lambda port: order.append("desktop") or 0)

    assert entry.main([]) == 0
    assert order == ["seed", "desktop"]


def test_readers_find_seeded_budget(monkeypatch, tmp_path, clear_budget_caches):
    """复制之后，三个读账本的模块在冻结环境都能读到同一份 budget.yaml。"""
    bundle, exe_dir = _make_bundle(tmp_path), tmp_path / "exe"
    _fake_frozen(monkeypatch, bundle, exe_dir)
    dst = exe_dir / "config" / "budget.yaml"

    # 复制前：与 exe 中观察到的故障一致
    assert request_budget.load_quota_limits() is None
    with pytest.raises(FileNotFoundError):
        quota_store._window_params()
    request_budget.load_quota_limits.cache_clear()
    quota_store._window_params.cache_clear()

    entry.seed_bundled_config()

    assert request_budget._budget_config_path() == dst
    assert quota_store._budget_config_path() == dst
    assert risk_control._watch_scheduler_config_path() == dst
    assert request_budget.load_quota_limits() is not None
    params = quota_store._window_params()
    assert params["retention_hours"] >= params["window_hours"] >= 1
    risk_control.load_watch_scheduler_policy()
