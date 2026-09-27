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
