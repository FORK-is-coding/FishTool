"""全局每日 token 上限（llm.daily_token_limit）默认值与旧默认值迁移测试。

迁移规则（core.config.ConfigManager._migrate_llm_daily_limit）：
- config.yaml 中仍为旧默认值 100000、没有迁移标记、user_config.yaml 未覆盖该键时，
  启动时改为新默认值并写入标记，落盘一次；
- 其他情况一律不动（用户改过的值、user_config 覆盖、已有标记）。

每个用例在 tmp_path 下建独立配置目录，不触碰仓库 config/。
"""
from __future__ import annotations

import logging

import pytest
import yaml

from core.config import (
    ConfigManager,
    LLM_DAILY_TOKEN_LIMIT_DEFAULT,
    LLM_DAILY_TOKEN_LIMIT_LEGACY,
    LLM_DAILY_TOKEN_LIMIT_MIGRATED_KEY,
)


def _write_main(cfg_dir, llm: dict) -> None:
    """写一份只含 llm 段的 config.yaml（模拟老用户已落盘的配置）。"""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        yaml.dump({"app": {"name": "x"}, "llm": llm}, allow_unicode=True), encoding="utf-8")


def _read_main(cfg_dir) -> dict:
    return yaml.safe_load((cfg_dir / "config.yaml").read_text(encoding="utf-8"))


def test_fresh_install_uses_new_default_with_marker(tmp_path):
    """没有 config.yaml 时，生成的默认配置直接是新默认值并带迁移标记。"""
    manager = ConfigManager(config_dir=str(tmp_path / "cfg"))
    assert manager.get("llm.daily_token_limit") == LLM_DAILY_TOKEN_LIMIT_DEFAULT
    on_disk = _read_main(tmp_path / "cfg")["llm"]
    assert on_disk["daily_token_limit"] == LLM_DAILY_TOKEN_LIMIT_DEFAULT
    assert on_disk[LLM_DAILY_TOKEN_LIMIT_MIGRATED_KEY] == 1


def test_legacy_default_is_migrated_once_and_persisted(tmp_path):
    """老配置停在 100000 且无标记：迁移为新默认值并写回磁盘。"""
    cfg_dir = tmp_path / "cfg"
    _write_main(cfg_dir, {"model": "m", "daily_token_limit": LLM_DAILY_TOKEN_LIMIT_LEGACY})

    manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get("llm.daily_token_limit") == LLM_DAILY_TOKEN_LIMIT_DEFAULT
    on_disk = _read_main(cfg_dir)["llm"]
    assert on_disk["daily_token_limit"] == LLM_DAILY_TOKEN_LIMIT_DEFAULT
    assert on_disk[LLM_DAILY_TOKEN_LIMIT_MIGRATED_KEY] == 1
    # 同段其他键原样保留
    assert on_disk["model"] == "m"


def test_migration_is_idempotent_across_restart_and_reload(tmp_path, monkeypatch):
    """迁移只写一次盘：再次启动、reload 都不再调用 save_config。"""
    cfg_dir = tmp_path / "cfg"
    _write_main(cfg_dir, {"daily_token_limit": LLM_DAILY_TOKEN_LIMIT_LEGACY})
    saves = {"n": 0}
    original = ConfigManager.save_config

    def counting_save(self):
        saves["n"] += 1
        original(self)

    monkeypatch.setattr(ConfigManager, "save_config", counting_save)
    ConfigManager(config_dir=str(cfg_dir))
    assert saves["n"] == 1

    manager = ConfigManager(config_dir=str(cfg_dir))
    manager.reload()

    assert saves["n"] == 1
    assert manager.get("llm.daily_token_limit") == LLM_DAILY_TOKEN_LIMIT_DEFAULT


@pytest.mark.parametrize("value", [300000, 0, 1, 99999, 100001])
def test_user_customized_value_is_kept(tmp_path, value):
    """用户自己改过的值（含 0=不限制）不迁移，也不写标记。"""
    cfg_dir = tmp_path / "cfg"
    _write_main(cfg_dir, {"daily_token_limit": value})

    manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get("llm.daily_token_limit") == value
    assert LLM_DAILY_TOKEN_LIMIT_MIGRATED_KEY not in _read_main(cfg_dir)["llm"]


def test_marker_present_keeps_legacy_value(tmp_path):
    """已迁移过、用户之后又主动改回 10 万：保留，不再迁移。"""
    cfg_dir = tmp_path / "cfg"
    _write_main(cfg_dir, {"daily_token_limit": LLM_DAILY_TOKEN_LIMIT_LEGACY,
                          LLM_DAILY_TOKEN_LIMIT_MIGRATED_KEY: 1})

    manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get("llm.daily_token_limit") == LLM_DAILY_TOKEN_LIMIT_LEGACY


@pytest.mark.parametrize("user_value", [LLM_DAILY_TOKEN_LIMIT_LEGACY, 5000])
def test_user_config_override_blocks_migration(tmp_path, user_value):
    """user_config.yaml 覆盖了该键：视为用户主动设置，主配置也不迁移，生效值取覆盖值。"""
    cfg_dir = tmp_path / "cfg"
    _write_main(cfg_dir, {"daily_token_limit": LLM_DAILY_TOKEN_LIMIT_LEGACY})
    (cfg_dir / "user_config.yaml").write_text(
        yaml.dump({"llm": {"daily_token_limit": user_value}}), encoding="utf-8")

    manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get("llm.daily_token_limit") == user_value
    on_disk = _read_main(cfg_dir)["llm"]
    assert on_disk["daily_token_limit"] == LLM_DAILY_TOKEN_LIMIT_LEGACY
    assert LLM_DAILY_TOKEN_LIMIT_MIGRATED_KEY not in on_disk


def test_user_config_other_llm_keys_do_not_block_migration(tmp_path):
    """user_config.yaml 只覆盖 llm 段的其他键时，上限仍按规则迁移，且覆盖照常生效。"""
    cfg_dir = tmp_path / "cfg"
    _write_main(cfg_dir, {"model": "a", "daily_token_limit": LLM_DAILY_TOKEN_LIMIT_LEGACY})
    (cfg_dir / "user_config.yaml").write_text(
        yaml.dump({"llm": {"model": "b"}}), encoding="utf-8")

    manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get("llm.daily_token_limit") == LLM_DAILY_TOKEN_LIMIT_DEFAULT
    assert manager.get("llm.model") == "b"
    # user_config 的覆盖不会被写进 config.yaml
    assert _read_main(cfg_dir)["llm"]["model"] == "a"


@pytest.mark.parametrize("raw", [True, "100000", 100000.0])
def test_non_int_legacy_lookalikes_are_not_migrated(tmp_path, raw):
    """布尔、字符串、浮点不算旧默认值（只认整数 100000）。"""
    cfg_dir = tmp_path / "cfg"
    _write_main(cfg_dir, {"daily_token_limit": raw})

    manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get("llm.daily_token_limit") == raw


def test_missing_llm_section_is_left_alone(tmp_path):
    """config.yaml 没有 llm 段（或不是字典）时不迁移、不报错。"""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text(yaml.dump({"app": {"name": "x"}, "llm": None}),
                                         encoding="utf-8")

    manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get("llm") is None


def test_write_failure_is_logged_and_load_continues(tmp_path, monkeypatch, caplog):
    """迁移写盘失败：只记日志，加载继续，内存中按新值生效（下次启动会再试）。"""
    cfg_dir = tmp_path / "cfg"
    _write_main(cfg_dir, {"daily_token_limit": LLM_DAILY_TOKEN_LIMIT_LEGACY})

    def _boom(self):
        raise PermissionError("read-only")

    monkeypatch.setattr(ConfigManager, "save_config", _boom)
    with caplog.at_level(logging.WARNING, logger="core.config"):
        manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get("llm.daily_token_limit") == LLM_DAILY_TOKEN_LIMIT_DEFAULT
    assert _read_main(cfg_dir)["llm"]["daily_token_limit"] == LLM_DAILY_TOKEN_LIMIT_LEGACY
    records = [r for r in caplog.records if "迁移写盘失败" in r.getMessage()]
    assert records and all(r.levelno == logging.WARNING and r.exc_info is None for r in records)


def test_repo_config_yaml_ships_new_default_without_marker():
    """仓库跟踪的 config/config.yaml 与代码默认值一致，且**不带**迁移标记。

    AstrBot 启动时 seed_defaults 会把引擎快照里 config.yaml 有、用户配置缺的键补进数据根。
    若仓库模板带着标记，老用户（仍是 100000、没有标记）升级后会先被补上标记，迁移随即跳过，
    上限永远停在 10 万。新安装不需要标记：值本身已是新默认值，不满足迁移条件。
    """
    from pathlib import Path
    repo_cfg = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    llm = yaml.safe_load(repo_cfg.read_text(encoding="utf-8"))["llm"]
    assert llm["daily_token_limit"] == LLM_DAILY_TOKEN_LIMIT_DEFAULT
    assert LLM_DAILY_TOKEN_LIMIT_MIGRATED_KEY not in llm


def test_yaml_dump_error_during_migration_does_not_break_load(tmp_path, monkeypatch):
    """迁移写盘抛非 OSError（如 YAMLError）也不能让 ConfigManager 构造失败。"""
    cfg_dir = tmp_path / "cfg"
    _write_main(cfg_dir, {"daily_token_limit": LLM_DAILY_TOKEN_LIMIT_LEGACY})

    def _boom(self):
        raise yaml.YAMLError("cannot represent")

    monkeypatch.setattr(ConfigManager, "save_config", _boom)
    manager = ConfigManager(config_dir=str(cfg_dir))
    assert manager.get("llm.daily_token_limit") == LLM_DAILY_TOKEN_LIMIT_DEFAULT
