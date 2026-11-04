"""守护：launcher 重定向后，FishTool 的全部持久化文件都落在数据根，快照目录不被写入。

首个版本发布前必须通过：发布后再改路径会导致 .key 换位置，已加密的 Cookie 与 API Key 无法解密。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import PLUGIN_DIR, REPO_ROOT

from release_tools.build_plugin import copy_engine_snapshot

LAUNCHER = PLUGIN_DIR / "engine_launcher" / "launcher.py"
PROBE = Path(__file__).with_name("_redirect_probe.py")


def _snapshot_files(root: Path) -> set[str]:
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


@pytest.fixture()
def engine_snapshot(tmp_path: Path) -> Path:
    dst = tmp_path / "plugin" / "engine" / "fishtool"
    copy_engine_snapshot(REPO_ROOT, dst)
    return dst


def test_redirect_puts_all_state_under_data_root(tmp_path: Path, engine_snapshot: Path, engine_python: str):
    data_root = tmp_path / "plugin_data" / "astrbot_plugin_fishtool"
    before = _snapshot_files(engine_snapshot)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [engine_python, str(PROBE), str(LAUNCHER), str(engine_snapshot), str(data_root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(tmp_path),
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith("PROBE_JSON="))
    out = json.loads(line[len("PROBE_JSON="):])

    config_dir = (data_root / "config").resolve()
    runtime_dir = (data_root / "runtime").resolve()
    assert Path(out["config_dir"]) == config_dir
    assert Path(out["key_path"]) == config_dir / ".key"
    for key in ("budget_request", "budget_quota", "budget_risk"):
        assert Path(out[key]) == config_dir / "budget.yaml", key
    assert out["quota_limits_loaded"] is True, "budget.yaml 未被读到，配额总闸会被 lru_cache 永久关闭"
    assert Path(out["cwd"]) == runtime_dir
    assert Path(out["log_dir"]) == runtime_dir / "data" / "logs"

    assert (config_dir / ".key").is_file()
    assert (config_dir / "budget.yaml").is_file()
    assert (config_dir / "config.yaml").is_file()
    assert (runtime_dir / "data" / "bili_ops.db").is_file()
    assert (runtime_dir / "data" / "logs" / "app.log").is_file()
    assert not (runtime_dir / "config").exists(), "config 目录误落在 runtime/ 下"

    assert _snapshot_files(engine_snapshot) == before, "引擎快照目录被写入了新文件"


def test_seed_defaults_fills_missing_keys_without_overwriting(tmp_path: Path, engine_python: str):
    engine_root = tmp_path / "engine"
    (engine_root / "config").mkdir(parents=True)
    (engine_root / "config" / "config.yaml").write_text("a: 1\nnested:\n  x: 1\n  y: 2\nnew_section:\n  k: v\n", "utf-8")
    (engine_root / "config" / "budget.yaml").write_text("default: true\n", "utf-8")
    config_dir = tmp_path / "data" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("a: 99\nnested:\n  x: 42\n", "utf-8")
    (config_dir / "budget.yaml").write_text("user: true\n", "utf-8")

    code = (
        "import importlib.util,sys,pathlib;"
        f"s=importlib.util.spec_from_file_location('l', r'{LAUNCHER}');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        f"m.seed_defaults(pathlib.Path(r'{engine_root}'), pathlib.Path(r'{config_dir}'))"
    )
    proc = subprocess.run([engine_python, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    import yaml

    merged = yaml.safe_load((config_dir / "config.yaml").read_text("utf-8"))
    assert merged["a"] == 99
    assert merged["nested"] == {"x": 42, "y": 2}
    assert merged["new_section"] == {"k": "v"}
    assert (config_dir / "budget.yaml").read_text("utf-8") == "user: true\n"


def test_seed_defaults_then_load_migrates_legacy_daily_token_limit(tmp_path: Path, engine_python: str):
    """老用户升级：数据根 config.yaml 仍是旧默认 100000。

    seed_defaults 用当前仓库的 config.yaml 补缺失键后，ConfigManager 加载必须把上限迁移成新默认值。
    仓库模板若带迁移标记，标记会先被补进用户配置，迁移随即跳过（曾经的缺陷）。
    """
    import shutil

    import yaml

    engine_root = tmp_path / "engine"
    (engine_root / "config").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "config" / "config.yaml", engine_root / "config" / "config.yaml")
    config_dir = tmp_path / "data" / "config"
    config_dir.mkdir(parents=True)
    legacy = yaml.safe_load((REPO_ROOT / "config" / "config.yaml").read_text("utf-8"))
    legacy["llm"]["daily_token_limit"] = 100000
    legacy["llm"].pop("daily_token_limit_migrated", None)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(legacy, allow_unicode=True), "utf-8")

    code = (
        "import importlib.util,sys,pathlib;"
        f"s=importlib.util.spec_from_file_location('l', r'{LAUNCHER}');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        f"m.seed_defaults(pathlib.Path(r'{engine_root}'), pathlib.Path(r'{config_dir}'));"
        f"sys.path.insert(0, r'{REPO_ROOT}');"
        "from core.config import ConfigManager;"
        f"print(ConfigManager(config_dir=r'{config_dir}').get('llm.daily_token_limit'))"
    )
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    env.pop("PYTHONPATH", None)
    proc = subprocess.run([engine_python, "-c", code], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "2000000"


def test_lock_rejects_second_holder(tmp_path: Path, engine_python: str):
    """同一数据根第二个 launcher 拿不到锁。"""
    code = (
        "import importlib.util,sys,pathlib,time;"
        f"s=importlib.util.spec_from_file_location('l', r'{LAUNCHER}');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        f"ok=m.acquire_data_root_lock(pathlib.Path(r'{tmp_path}'));print('LOCK', ok, flush=True);"
        "sys.stdin.read()"
    )
    holder = subprocess.Popen([engine_python, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "LOCK True"
        second = subprocess.run([engine_python, "-c", code], input="", capture_output=True, text=True, timeout=30)
        assert "LOCK False" in second.stdout
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
