"""插件级常量：名称、超时、路由白名单。"""
from __future__ import annotations

#: 插件名，同时是 metadata.yaml 的 name、data/plugins 与 data/plugin_data 下的目录名。
PLUGIN_NAME = "astrbot_plugin_fishtool"

#: 引擎 venv 依赖清单文件名（位于插件根目录，构建时由 FishTool requirements.txt 生成）。
ENGINE_REQ_NAME = "engine_requirements.txt"

#: 引擎支持的 Python 版本。锁定的 pydantic 2.5.2 / aiohttp 3.9.1 没有 cp313 wheel。
SUPPORTED_ENGINE_PYTHON = {(3, 11), (3, 12)}

#: FishTool 写接口要求的本机 token 请求头（web/local_guard.py LOCAL_TOKEN_HEADER）。
LOCAL_TOKEN_HEADER = "X-Local-Token"

#: launcher 发现数据根已被另一个引擎占用时的退出码。
EXIT_LOCK_CONFLICT = 3

#: 默认配置（插件配置缺项时使用）。
DEFAULT_ENGINE_CFG = {
    "autostart": True,
    "python": "",
    "pip_index_url": "",
    "port": 0,
    "request_timeout_s": 120,
    "long_request_timeout_s": 1800,
    "block_api_key_write": False,
}


def engine_cfg(raw: dict | None) -> dict:
    """把插件配置里的 engine 段与默认值合并。

    Args:
        raw: 插件配置中的 ``engine`` 对象，可为空。

    Returns:
        dict: 补齐默认值后的新字典。
    """
    merged = dict(DEFAULT_ENGINE_CFG)
    for key, value in (raw or {}).items():
        if value is not None:
            merged[key] = value
    return merged
