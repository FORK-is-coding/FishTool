"""配置一致性：代码默认值、_conf_schema.json、两份 i18n、README 配置表必须同步；插件进程内不得 import logging。"""
from __future__ import annotations

import json
import re

import pytest

from conftest import PLUGIN_DIR
from fishtool_adapter.constants import DEFAULT_CHAT_CFG, DEFAULT_ENGINE_CFG

SCHEMA = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text("utf-8"))
I18N = {
    name: json.loads((PLUGIN_DIR / ".astrbot-plugin" / "i18n" / f"{name}.json").read_text("utf-8"))
    for name in ("zh-CN", "en-US")
}
README_ROWS = dict(re.findall(r"^\| `(\w+\.\w+)` \| `?([^`|]*?)`? \|", (PLUGIN_DIR / "README.md").read_text("utf-8"), re.M))
SECTIONS = {"engine": DEFAULT_ENGINE_CFG, "chat": DEFAULT_CHAT_CFG}


def _shown(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "空" if value == "" else str(value)


@pytest.mark.parametrize("section", sorted(SECTIONS))
def test_schema_matches_code_defaults(section):
    items = SCHEMA[section]["items"]
    defaults = SECTIONS[section]
    assert set(items) == set(defaults)
    for key, meta in items.items():
        assert meta["default"] == defaults[key], key


@pytest.mark.parametrize("lang", sorted(I18N))
@pytest.mark.parametrize("section", sorted(SECTIONS))
def test_i18n_covers_schema(lang, section):
    config = I18N[lang]["config"][section]
    for key, meta in SCHEMA[section]["items"].items():
        assert key in config, f"{lang}: {section}.{key}"
        if "options" in meta:
            assert len(config[key].get("labels", [])) == len(meta["options"]), f"{lang}: {section}.{key} labels"


@pytest.mark.parametrize("section", sorted(SECTIONS))
def test_readme_table_matches_schema(section):
    for key, meta in SCHEMA[section]["items"].items():
        name = f"{section}.{key}"
        assert name in README_ROWS, f"README 缺少 {name}"
        assert README_ROWS[name].strip() == _shown(meta["default"]), name


def test_no_stdlib_logging_in_plugin_process():
    """插件审核规则：插件进程内只能用 astrbot.api.logger（engine/、engine_launcher/ 是独立子进程，不受限）。"""
    offenders = []
    for path in PLUGIN_DIR.rglob("*.py"):
        rel = path.relative_to(PLUGIN_DIR).as_posix()
        if rel.startswith(("engine/", "engine_launcher/")):
            continue
        if re.search(r"^\s*(import logging|from logging)|getLogger\(", path.read_text("utf-8"), re.M):
            offenders.append(rel)
    assert offenders == []
