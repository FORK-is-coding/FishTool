"""engine 段配置收敛：手改配置文件留下的错误类型不能拖垮启动（#122）。"""
from __future__ import annotations

import pytest

from fishtool_adapter.constants import DEFAULT_ENGINE_CFG, engine_cfg


def test_empty_uses_defaults():
    assert engine_cfg(None) == DEFAULT_ENGINE_CFG
    assert engine_cfg({}) == DEFAULT_ENGINE_CFG
    assert engine_cfg("oops") == DEFAULT_ENGINE_CFG
    assert engine_cfg(None) is not DEFAULT_ENGINE_CFG


@pytest.mark.parametrize("raw,expected", [
    ({"port": "abc"}, 0),        # 旧代码在这里 ValueError，插件启动即失败
    ({"port": ""}, 0),
    ({"port": -1}, 0),
    ({"port": 8765}, 8765),
    ({"port": "8765"}, 8765),
    ({"port": 8765.0}, 8765),
    ({"port": True}, 0),         # bool 不是端口
    ({"port": 70000}, 65535),    # 越上限截断
])
def test_port(raw, expected):
    assert engine_cfg(raw)["port"] == expected


@pytest.mark.parametrize("raw,expected", [
    ({"request_timeout_s": "abc"}, 120),
    ({"request_timeout_s": 0}, 120),
    ({"request_timeout_s": 30}, 30),
    ({"long_request_timeout_s": "10.5"}, 1800),
    ({"long_request_timeout_s": 600.0}, 600),
])
def test_timeouts(raw, expected):
    key = next(iter(raw))
    assert engine_cfg(raw)[key] == expected


@pytest.mark.parametrize("raw,expected", [
    ({"autostart": "false"}, True),        # 字符串不是 bool，回落默认（默认是开）
    ({"autostart": False}, False),
    ({"autostart": 0}, True),
    ({"block_api_key_write": "true"}, False),   # 默认关；字符串不再被当真值
    ({"block_api_key_write": True}, True),
])
def test_booleans(raw, expected):
    key = next(iter(raw))
    assert engine_cfg(raw)[key] is expected


def test_strings_stripped():
    cfg = engine_cfg({"python": "  C:/py/python.exe  ", "pip_index_url": None})
    assert cfg["python"] == "C:/py/python.exe"
    assert cfg["pip_index_url"] == ""


def test_unknown_keys_dropped():
    """只有 schema 声明的键会进结果，避免拼错的键悄悄流到下游。"""
    assert set(engine_cfg({"port": 1, "typo": 2})) == set(DEFAULT_ENGINE_CFG)


def test_does_not_mutate_defaults():
    cfg = engine_cfg({"port": 9000})
    assert cfg["port"] == 9000 and DEFAULT_ENGINE_CFG["port"] == 0
