"""core.config 底座测试（第1批补齐 · core 段）。

覆盖范围：
- ConfigManager.__init__ / _init_cipher / _load_config / _get_default_config
- ConfigManager._deep_merge / get / set / save_config / save_secret / get_secret
- ConfigManager.reload / export_config / all

测试策略：
- 每个用例使用 tmp_path 下独立的 config 目录，绝不触碰仓库真实 config/。
- 加解密走真实 Fernet，验证"落盘为密文、可原样回解"。
- 敏感文件损坏、解密失败等异常分支用真实坏字节驱动，不用 Mock。
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
import yaml

from core.config import ConfigManager


def _make_manager(tmp_path, sub: str = "cfg") -> ConfigManager:
    """在 tmp_path 下创建独立配置目录的 ConfigManager。"""
    return ConfigManager(config_dir=str(tmp_path / sub))


# ---------------------------------------------------------------------------
# 初始化与默认配置
# ---------------------------------------------------------------------------


def test_init_creates_dir_and_default_config(tmp_path):
    """首次初始化应创建目录并落盘默认配置。"""
    manager = _make_manager(tmp_path)
    assert manager.config_dir.exists()
    assert manager.main_config_path.exists()
    assert manager.get("app.name") == "B站运营工具箱"
    assert manager.get("server.port") == 8080
    assert manager.get("database.echo") is False
    # 未设置过任何 secret 时返回默认值
    assert manager.get_secret("llm_api_key") is None
    assert manager.get_secret("llm_api_key", "d") == "d"


def test_default_config_returns_fresh_copy(tmp_path):
    """_get_default_config 每次返回全新字典，修改不影响下一次。"""
    manager = _make_manager(tmp_path)
    first = manager._get_default_config()
    first["app"]["name"] = "被篡改"
    second = manager._get_default_config()
    assert second["app"]["name"] == "B站运营工具箱"


def test_init_cipher_generates_then_reuses_key(tmp_path):
    """密钥文件首次生成、再次初始化复用，且加解密可互操作。"""
    manager = _make_manager(tmp_path)
    assert manager.key_path.exists()
    key_bytes = manager.key_path.read_bytes()

    manager_again = ConfigManager(config_dir=str(tmp_path / "cfg"))
    assert manager_again.key_path.read_bytes() == key_bytes

    token = manager_again._cipher.encrypt(b"hello")
    assert manager._cipher.decrypt(token) == b"hello"


# ---------------------------------------------------------------------------
# 深度合并
# ---------------------------------------------------------------------------


def test_deep_merge_recurses_nested_dicts(tmp_path):
    """嵌套字典应递归合并，而非整块覆盖。"""
    manager = _make_manager(tmp_path)
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    override = {"a": {"c": 9, "e": 5}, "f": 6}
    manager._deep_merge(base, override)
    assert base == {"a": {"b": 1, "c": 9, "e": 5}, "d": 3, "f": 6}


# ---------------------------------------------------------------------------
# get / set
# ---------------------------------------------------------------------------


def test_get_supports_dotted_path_and_default(tmp_path):
    """点号路径逐级下钻，路径断裂时返回默认值。"""
    manager = _make_manager(tmp_path)
    manager.set("bilibili.rate_limit.normal", 3.5)
    assert manager.get("bilibili.rate_limit.normal") == 3.5
    # 顶级标量
    assert manager.get("app.name") == "B站运营工具箱"
    # 键不存在
    assert manager.get("not.exist.key", "D") == "D"
    # 路径穿过非字典值（app.name 是字符串）→ 返回默认值
    assert manager.get("app.name.deep", "D") == "D"


def test_set_auto_creates_intermediate_dicts(tmp_path):
    """set 遇到不存在的中间层级会自动建字典。"""
    manager = _make_manager(tmp_path)
    manager.set("brand.new.leaf", 42)
    assert manager.get("brand.new.leaf") == 42


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


def test_save_config_roundtrip_and_excludes_secrets(tmp_path):
    """save_config 落盘不含 secrets，重新加载后普通配置与密钥都在。"""
    manager = _make_manager(tmp_path)
    manager.set("app.debug", True)
    manager.save_secret("llm_api_key", "sk-secret-value")
    manager.save_config()

    raw = manager.main_config_path.read_text(encoding="utf-8")
    assert "sk-secret-value" not in raw
    assert "secrets:" not in raw
    assert "debug: true" in raw

    reloaded = ConfigManager(config_dir=str(tmp_path / "cfg"))
    assert reloaded.get("app.debug") is True
    assert reloaded.get_secret("llm_api_key") == "sk-secret-value"


def test_secret_is_encrypted_on_disk_and_multi_key(tmp_path):
    """敏感信息以密文落盘，且可保存/读取多个键。"""
    manager = _make_manager(tmp_path)
    manager.save_secret("a_key", "PLAINTEXT-A")
    manager.save_secret("b_key", "PLAINTEXT-B")

    on_disk = manager.secrets_path.read_bytes()
    assert b"PLAINTEXT-A" not in on_disk
    assert b"PLAINTEXT-B" not in on_disk
    assert manager.get_secret("a_key") == "PLAINTEXT-A"
    assert manager.get_secret("b_key") == "PLAINTEXT-B"


def test_corrupted_secrets_file_falls_back_to_empty(tmp_path):
    """解密失败时 secrets 置空，不抛出、不崩溃。"""
    manager = _make_manager(tmp_path)
    manager.save_secret("k", "v")
    # 写入无法被 Fernet 解密的坏字节
    manager.secrets_path.write_bytes(b"not-a-valid-fernet-token")

    reloaded = ConfigManager(config_dir=str(tmp_path / "cfg"))
    assert reloaded.get_secret("k") is None
    assert reloaded.get_secret("k", "fallback") == "fallback"
    # 解不开的 .secrets 先改名备份，下一次 save_secret 不会把它覆盖掉
    backups = list(manager.config_dir.glob(".secrets.bad-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"not-a-valid-fernet-token"


def test_user_config_deep_merges_over_main(tmp_path):
    """user_config.yaml 只覆盖指定子键，其余保留默认值。"""
    _make_manager(tmp_path)  # 生成默认 config.yaml
    cfg_dir = tmp_path / "cfg"
    (cfg_dir / "user_config.yaml").write_text(
        yaml.dump({"server": {"port": 9999}, "app": {"debug": True}}, allow_unicode=True),
        encoding="utf-8",
    )
    manager = ConfigManager(config_dir=str(cfg_dir))
    assert manager.get("server.port") == 9999
    assert manager.get("server.host") == "127.0.0.1"  # 深度合并保留
    assert manager.get("app.debug") is True


def test_reload_discards_unsaved_changes(tmp_path):
    """reload 会丢弃内存中未保存的 set 修改。"""
    manager = _make_manager(tmp_path)
    manager.set("app.version", "1.0.0")
    manager.save_config()

    manager.set("app.version", "9.9.9")  # 未落盘
    manager.reload()
    assert manager.get("app.version") == "1.0.0"


def test_reload_picks_up_external_file_edit(tmp_path):
    """外部修改 config.yaml 后 reload 应读到新值。"""
    manager = _make_manager(tmp_path)
    raw = yaml.safe_load(manager.main_config_path.read_text(encoding="utf-8"))
    raw["app"]["version"] = "2.2.2"
    manager.main_config_path.write_text(
        yaml.dump(raw, allow_unicode=True), encoding="utf-8"
    )

    manager.reload()
    assert manager.get("app.version") == "2.2.2"


# ---------------------------------------------------------------------------
# 导出与只读快照
# ---------------------------------------------------------------------------


def test_export_config_writes_without_secrets(tmp_path):
    """export_config 输出文件不含 secrets 字段。"""
    manager = _make_manager(tmp_path)
    manager.save_secret("token", "should-not-leak")
    out_path = tmp_path / "exported.yaml"
    manager.export_config(str(out_path))

    raw = out_path.read_text(encoding="utf-8")
    assert "should-not-leak" not in raw
    assert "secrets" not in raw
    data = yaml.safe_load(raw)
    assert data["app"]["name"] == "B站运营工具箱"


def test_all_property_snapshot_excludes_secrets(tmp_path):
    """all 快照过滤 secrets，且顶层修改不回写内部状态。"""
    manager = _make_manager(tmp_path)
    manager.save_secret("k", "v")
    snapshot = manager.all
    assert "secrets" not in snapshot
    snapshot["injected"] = True
    assert manager.get("injected") is None
    assert manager.get_secret("k") == "v"


def test_all_property_returns_deep_copy(tmp_path):
    """all 返回深拷贝：改写嵌套节点不得污染活配置，也不得随落盘写出（#88）。"""
    manager = _make_manager(tmp_path)
    snapshot = manager.all
    # 模拟 web 层往响应副本里塞展示字段
    snapshot["llm"]["api_key_masked"] = "sk-1234***cdef"
    snapshot["server"]["port"] = 1

    assert "api_key_masked" not in manager.get("llm")
    assert manager.get("server.port") == 8080

    manager.save_config()
    assert "api_key_masked" not in manager.main_config_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 明文密钥迁移（#88）
# ---------------------------------------------------------------------------


def test_legacy_plaintext_api_key_is_migrated_into_secrets(tmp_path):
    """历史明文 llm.api_key 应搬进加密 secrets 并从普通配置里消失。"""
    _make_manager(tmp_path)  # 生成默认 config.yaml
    cfg_dir = tmp_path / "cfg"
    raw = yaml.safe_load((cfg_dir / "config.yaml").read_text(encoding="utf-8"))
    raw["llm"]["api_key"] = "sk-legacy-plain"
    (cfg_dir / "config.yaml").write_text(
        yaml.dump(raw, allow_unicode=True), encoding="utf-8"
    )

    manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get_secret("llm_api_key") == "sk-legacy-plain"
    assert manager.get("llm.api_key") is None
    assert "sk-legacy-plain" not in (cfg_dir / "config.yaml").read_text(encoding="utf-8")
    assert b"sk-legacy-plain" not in manager.secrets_path.read_bytes()


# ---------------------------------------------------------------------------
# 坏文件与原子写盘（#124）
# ---------------------------------------------------------------------------


def test_broken_yaml_falls_back_to_defaults_and_backs_up(tmp_path):
    """config.yaml 解析失败时构造不抛异常，回退默认值并备份坏文件。"""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True)
    broken = "server: [unclosed\n  bad: : :\n"
    (cfg_dir / "config.yaml").write_text(broken, encoding="utf-8")

    manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get("server.port") == 8080
    backups = list(cfg_dir.glob("config.yaml.bad-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == broken


def test_broken_user_config_is_ignored(tmp_path):
    """user_config.yaml 解析失败时按“没有覆盖项”处理，主配置照常生效。"""
    _make_manager(tmp_path)
    cfg_dir = tmp_path / "cfg"
    (cfg_dir / "user_config.yaml").write_text("app: {broken\n", encoding="utf-8")

    manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get("server.port") == 8080
    assert list(cfg_dir.glob("user_config.yaml.bad-*"))


def test_non_mapping_yaml_falls_back_to_defaults(tmp_path):
    """顶层不是字典的 YAML 同样视为坏文件，而不是让后续 .get 崩掉。"""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text("just-a-string\n", encoding="utf-8")

    manager = ConfigManager(config_dir=str(cfg_dir))

    assert manager.get("server.port") == 8080
    assert list(cfg_dir.glob("config.yaml.bad-*"))


def test_save_config_failure_keeps_original_file(tmp_path, monkeypatch):
    """序列化失败时原 config.yaml 字节不变（旧实现会先截断成半截文件）。"""
    manager = _make_manager(tmp_path)
    original = manager.main_config_path.read_bytes()
    manager.set("app.version", "9.9.9")

    def _boom(*args, **kwargs):
        raise RuntimeError("dump failed")

    monkeypatch.setattr(
        sys.modules["core.config"], "yaml", SimpleNamespace(dump=_boom)
    )
    with pytest.raises(RuntimeError):
        manager.save_config()

    assert manager.main_config_path.read_bytes() == original
    assert "9.9.9" not in manager.main_config_path.read_text(encoding="utf-8")
    # 不留临时文件
    assert not list(manager.config_dir.glob(".config.yaml.*.tmp"))


def test_truncated_key_file_is_backed_up_and_rebuilt(tmp_path):
    """截断的 .key 不再抛 ValueError：备份旧文件并重建可用密钥。"""
    manager = _make_manager(tmp_path)
    manager.save_secret("llm_api_key", "sk-old")
    manager.key_path.write_bytes(b"short")

    rebuilt = ConfigManager(config_dir=str(tmp_path / "cfg"))

    assert len(rebuilt.key_path.read_bytes()) > 8
    assert rebuilt.get("server.port") == 8080  # 配置本身仍可加载
    backups = list(manager.config_dir.glob(".key.bad-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"short"
    # 新密钥可正常加解密
    rebuilt.save_secret("k2", "v2")
    assert rebuilt.get_secret("k2") == "v2"


def test_truncated_key_also_backs_up_secrets_so_old_values_stay_recoverable(tmp_path):
    """.key 损坏重建时必须先备份 .secrets：否则下一次保存会用新密钥覆盖，旧 Cookie 永久丢失。"""
    from cryptography.fernet import Fernet
    import json

    manager = _make_manager(tmp_path)
    manager.save_secret("bilibili_cookie", "SESSDATA=OLD")
    old_key = manager.key_path.read_bytes()
    manager.key_path.write_bytes(old_key[:10])

    rebuilt = ConfigManager(config_dir=str(tmp_path / "cfg"))
    rebuilt.save_secret("llm_api_key", "sk-new")

    backups = list(manager.config_dir.glob(".secrets.bad-*"))
    assert len(backups) == 1
    restored = json.loads(Fernet(old_key).decrypt(backups[0].read_bytes()))
    assert restored["bilibili_cookie"] == "SESSDATA=OLD"


def test_key_read_error_is_not_treated_as_corruption(tmp_path, monkeypatch):
    """读 .key 时的 OSError（占用、权限）不是密钥损坏：不得改名重建 .key / .secrets。"""
    from pathlib import Path

    manager = _make_manager(tmp_path)
    manager.save_secret("bilibili_cookie", "SESSDATA=KEEP")
    key_path = manager.key_path
    real_read = Path.read_bytes

    def _flaky(self):
        if self == key_path:
            raise PermissionError("locked")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", _flaky)
    # 与 main 一致：读不到密钥就让构造失败，而不是悄悄换一把新密钥
    with pytest.raises(PermissionError):
        ConfigManager(config_dir=str(tmp_path / "cfg"))
    monkeypatch.setattr(Path, "read_bytes", real_read)

    assert not list(manager.config_dir.glob(".key.bad-*"))
    assert not list(manager.config_dir.glob(".secrets.bad-*"))
    assert ConfigManager(config_dir=str(tmp_path / "cfg")).get_secret("bilibili_cookie") == "SESSDATA=KEEP"


def test_legacy_plaintext_key_in_user_config_is_migrated_once(tmp_path):
    """明文 key 写在 user_config.yaml：迁入 secrets、从该文件删除，且不再反复重写 config.yaml。"""
    manager = _make_manager(tmp_path)
    manager.user_config_path.write_text("llm:\n  api_key: sk-user-plain\n", encoding="utf-8")

    first = ConfigManager(config_dir=str(tmp_path / "cfg"))
    assert first.get_secret("llm_api_key") == "sk-user-plain"
    assert "sk-user-plain" not in manager.user_config_path.read_text(encoding="utf-8")
    assert "sk-user-plain" not in manager.main_config_path.read_text(encoding="utf-8")

    before = manager.main_config_path.stat().st_mtime_ns
    ConfigManager(config_dir=str(tmp_path / "cfg"))
    assert manager.main_config_path.stat().st_mtime_ns == before


def test_atomic_replace_retries_when_target_is_briefly_locked(tmp_path, monkeypatch):
    """Windows 上目标文件被别的句柄打开时 os.replace 抛 PermissionError：短暂重试后成功。"""
    import os as os_module

    # core/__init__.py 用同名属性导出了 ConfigManager 实例，必须从 sys.modules 取子模块本身
    core_config_module = sys.modules["core.config"]
    manager = _make_manager(tmp_path)
    real_replace = os_module.replace
    failures = {"left": 2}

    def _locked_then_ok(src, dst):
        if failures["left"]:
            failures["left"] -= 1
            raise PermissionError("[WinError 5] 拒绝访问")
        return real_replace(src, dst)

    monkeypatch.setattr(os_module, "replace", _locked_then_ok)
    monkeypatch.setattr(os_module, "name", "nt")
    monkeypatch.setattr(core_config_module.time, "sleep", lambda _s: None)
    manager.set("app.version", "9.9.9")
    manager.save_config()

    assert failures["left"] == 0
    assert "9.9.9" in manager.main_config_path.read_text(encoding="utf-8")


def test_init_survives_unexpected_load_error(tmp_path, monkeypatch):
    """_load_config 抛出意料之外的异常时，构造仍成功并落到默认值（导入期不崩）。"""
    def _boom(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(ConfigManager, "_load_config", _boom)
    manager = ConfigManager(config_dir=str(tmp_path / "cfg"))

    assert manager.get("server.port") == 8080
    assert manager.all["app"]["name"] == "B站运营工具箱"
