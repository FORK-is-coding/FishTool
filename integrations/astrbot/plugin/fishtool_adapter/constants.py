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
    "allow_proxy_report": False,
}


def engine_cfg(raw: dict | None) -> dict:
    """把插件配置里的 engine 段与默认值合并，并把非法值收敛为默认值。

    面板保存时 AstrBot 会做类型校验，但手改配置文件或旧版本残留的值不会经过校验：
    ``port: 'abc'`` 会让 ``int()`` 在启动时抛 ValueError，``'false'`` 之类字符串还是真值。
    这里与 :func:`chat_cfg` 同构地兜一层，保证下游可以直接按类型使用。

    Args:
        raw: 插件配置中的 ``engine`` 对象，可为空。

    Returns:
        dict: 补齐默认值、收敛非法值后的新字典。
    """
    raw = raw if isinstance(raw, dict) else {}
    d = DEFAULT_ENGINE_CFG
    return {
        "autostart": _as_bool_lenient(raw.get("autostart"), d["autostart"]),
        "python": _as_str(raw.get("python")),
        "pip_index_url": _as_str(raw.get("pip_index_url")),
        "port": _as_int(raw.get("port"), d["port"], minimum=0, maximum=65535),
        "request_timeout_s": _as_int(
            raw.get("request_timeout_s"), d["request_timeout_s"], minimum=1, maximum=3600),
        "long_request_timeout_s": _as_int(
            raw.get("long_request_timeout_s"), d["long_request_timeout_s"], minimum=1, maximum=86400),
        "block_api_key_write": _as_bool_lenient(raw.get("block_api_key_write"), d["block_api_key_write"]),
        "allow_proxy_report": _as_bool_lenient(raw.get("allow_proxy_report"), d["allow_proxy_report"]),
    }


#: 聊天指令与告警推送的默认配置（_conf_schema.json 的 chat 段）。
#: 推送订阅不在这里：存在 remote/subscriptions.json（见 subscriptions.py）。
DEFAULT_CHAT_CFG = {
    "enable": True,
    "restricted_scope": "admin",
    "cooldown_seconds": 10,
    "notify_min_level": "high",
    "notify_merge_seconds": 120,
    "report_font_cjk": "",
    "report_font_latin": "",
    "report_charts": True,
}

#: 受限级别指令（告警、监控状态）的可用范围。
RESTRICTED_SCOPES = ("admin", "subscribed", "everyone")

#: 告警级别，从高到低。
ALERT_LEVELS = ("high", "medium", "low")


def _as_bool(value, default: bool) -> bool:
    return value if isinstance(value, bool) else default


_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})


def _as_bool_lenient(value, default: bool) -> bool:
    """宽松布尔：额外接受 ``"true"``/``"false"`` 等字符串与 0/1，其他值回退默认值。

    只用于引擎段的开关。改造前 ``engine_cfg`` 原样透传，下游按真值判断，手改成
    ``block_api_key_write: "true"`` 时防护是开着的；严格 ``_as_bool`` 会把它回落成
    默认 False，悄悄关掉 API Key 写保护。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    return default


def _as_int(value, default: int, *, minimum: int, maximum: int) -> int:
    """整数配置：非法或越下限用默认值，越上限截到上限。接受 ``"10"``、``10.0``，不接受 ``"10.5"``。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < minimum:
        return default
    return min(number, maximum)


def _as_str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def chat_cfg(raw: dict | None) -> dict:
    """把插件配置里的 chat 段与默认值合并，并把非法值收敛为默认值。

    面板保存时 AstrBot 会做类型校验，但手改配置文件或旧版本残留的值不会经过校验，
    所以这里再兜一层，保证后续代码可以直接按类型使用。

    Args:
        raw: 插件配置中的 ``chat`` 对象，可为空。

    Returns:
        dict: 补齐默认值、收敛非法值后的新字典。
    """
    raw = raw if isinstance(raw, dict) else {}
    d = DEFAULT_CHAT_CFG
    scope = _as_str(raw.get("restricted_scope"))
    level = _as_str(raw.get("notify_min_level"))
    return {
        "enable": _as_bool(raw.get("enable"), d["enable"]),
        "restricted_scope": scope if scope in RESTRICTED_SCOPES else d["restricted_scope"],
        "cooldown_seconds": _as_int(raw.get("cooldown_seconds"), d["cooldown_seconds"], minimum=0, maximum=3600),
        "notify_min_level": level if level in ALERT_LEVELS else d["notify_min_level"],
        "notify_merge_seconds": _as_int(raw.get("notify_merge_seconds"), d["notify_merge_seconds"], minimum=1, maximum=3600),
        "report_font_cjk": _as_str(raw.get("report_font_cjk")),
        "report_font_latin": _as_str(raw.get("report_font_latin")),
        "report_charts": _as_bool(raw.get("report_charts"), d["report_charts"]),
    }
