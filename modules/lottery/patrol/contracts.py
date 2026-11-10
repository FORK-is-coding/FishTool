"""AI 伪人巡检的常量、默认配置、配置加载与领域异常。

所有 ``patrol.*`` 配置在这里都有默认值：打包版老用户的 ``config.yaml`` 不会自动获得新段，
缺项也必须能运行。安全相关上限（``HARD_*``）在代码里再次限制，配置改大无效。
"""
import os
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from core.exceptions import BiliOpsException
from core.logger import get_logger

logger = get_logger(__name__)

# ============ 版本 ============

RULES_VERSION = "2026.11.10-c"
PROMPT_VERSION = "patrol-judge-v3"
AI_CONSENT_VERSION = "patrol-v1"
RISK_NOTICE_VERSION = "patrol-risk-v1"
PROXY_CONSENT_VERSION = "patrol-report-v1"

# ============ 档位 / 来源 / 状态 ============

TIER_BOT = "bot"
TIER_SUSPECT = "suspect"
TIER_HUMAN = "human"
TIER_UNKNOWN = "unknown"
TIER_WHITELIST = "whitelist"
TIERS = (TIER_BOT, TIER_SUSPECT, TIER_HUMAN, TIER_UNKNOWN, TIER_WHITELIST)

SOURCE_RULE = "rule"
SOURCE_HARD_RULE = "hard_rule"
SOURCE_RULE_LLM = "rule+llm"
SOURCE_WHITELIST = "whitelist"

REPORT_STATUSES = frozenset({
    "none", "queued", "sending", "reported", "duplicate", "deleted", "failed", "unknown",
    "skipped", "skipped_rate_limited", "cancelled", "dry_run", "native_opened", "native_done",
})
#: 与 patrol_reports 部分唯一索引一致：这些状态视为“已占用”，不可再次提交。
REPORT_ACTIVE_STATUSES = frozenset({"sending", "reported", "duplicate"})
#: 不计入代发日限的状态（未真正发出请求）。
REPORT_NOT_ATTEMPTED = frozenset({"skipped", "skipped_rate_limited", "cancelled", "dry_run",
                                  "native_opened", "native_done"})
REPORT_MODES = frozenset({"native", "proxy", "dry_run"})

# ============ 硬上限（不可被配置放大） ============

HARD_MAX_COMMENTS = 2000
HARD_REPLY_REQUESTS = 60
HARD_JUDGE_ITEMS = 500
HARD_REPORT_DAILY = 30
HARD_REPORT_PER_TASK = 10
HARD_PER_ACCOUNT = 2

#: AI 外发字段白名单：judge.build_items 构造完会断言不含其他键。
LLM_ALLOWED_KEYS = (
    "id", "name", "sign", "level", "vip", "official", "text", "is_reply", "parent_text",
    "at_recommend", "same_account_comments", "rule_signals",
)

# ============ 默认配置（与设计稿 §5.8 对应） ============

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "comment_endpoint": "main",
    "max_comments_default": 500,
    "max_comments_hard": 2000,
    "reply_floors": 20,
    "reply_pages_per_floor": 3,
    "reply_requests_hard": 60,
    "judge": {
        "batch_size": 8,
        "max_items": 300,
        "max_items_hard": 500,
        "temperature": 0.2,
        "cache_days": 7,
    },
    "thresholds": {"bot": 75, "suspect": 45, "gray_low": 20, "gray_high": 85},
    "soft_cap": 70,
    "hard_hit_floor": 60,
    "disagreement_gap": 50,
    "fusion_weight_llm": 0.55,
    "report": {
        "default_mode": "native",
        "proxy_enabled": True,
        "dry_run": False,
        "require_ai": True,
        "per_account_limit": 1,
        "max_per_task": 5,
        "daily_limit": 10,
        "interval_seconds": [3, 6],
        "cooldown_minutes_after_limit": 30,
        "stale_warn_hours": 24,
        "stale_block_hours": 72,
        "preselect": False,
    },
    "retention_days": {"scans": 7, "reports": 180, "llm_cache": 7},
}


@dataclass(frozen=True)
class PatrolSettings:
    """已合并、已校验的巡检配置（只读）。"""

    enabled: bool
    comment_endpoint: str
    max_comments_default: int
    max_comments_hard: int
    reply_floors: int
    reply_pages_per_floor: int
    reply_requests_hard: int
    judge_batch_size: int
    judge_max_items: int
    judge_max_items_hard: int
    judge_temperature: float
    judge_cache_days: int
    bot_threshold: int
    suspect_threshold: int
    gray_low: int
    gray_high: int
    soft_cap: int
    hard_hit_floor: int
    disagreement_gap: int
    fusion_weight_llm: float
    report_default_mode: str
    report_proxy_allowed: bool
    report_dry_run: bool
    report_require_ai: bool
    report_per_account_limit: int
    report_max_per_task: int
    report_daily_limit: int
    report_interval_seconds: Tuple[float, float]
    report_cooldown_minutes: int
    report_stale_warn_hours: int
    report_stale_block_hours: int
    report_preselect: bool
    retention_scans_days: int
    retention_reports_days: int
    retention_llm_cache_days: int


def _deep_merge(base: Dict[str, Any], override: Any, path: str = "patrol") -> Dict[str, Any]:
    """只合并 base 中已知的键；类型不符的值丢弃并告警。"""
    result: Dict[str, Any] = {}
    override = override if isinstance(override, dict) else {}
    for key, default in base.items():
        if key not in override:
            result[key] = _copy(default)
            continue
        value = override[key]
        if isinstance(default, dict):
            result[key] = _deep_merge(default, value, f"{path}.{key}")
        elif _type_ok(default, value):
            result[key] = list(value) if isinstance(default, list) else value
        else:
            logger.warning("[巡检] 配置项 %s.%s 类型不符（%r），已使用默认值", path, key, value)
            result[key] = _copy(default)
    return result


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return list(value)
    return value


def _type_ok(default: Any, value: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, (list, tuple)) and len(value) == len(default) and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    return False


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _thresholds_consistent(m: Dict[str, Any]) -> bool:
    t = m["thresholds"]
    return (
        0 < t["suspect"] < t["bot"] <= 100
        and 0 <= t["gray_low"] < t["gray_high"] <= 99
        and 0 < m["soft_cap"] < t["bot"]
        and 0 < m["hard_hit_floor"] < t["bot"]
        and 0 < m["disagreement_gap"] <= 100
        and 0 < float(m["fusion_weight_llm"]) < 1
    )


def build_settings(raw: Any) -> PatrolSettings:
    """把原始 ``patrol`` 配置段合并默认值、clamp 硬上限、校验阈值一致性。"""
    m = _deep_merge(DEFAULTS, raw)
    if not _thresholds_consistent(m):
        logger.warning("[巡检] 阈值配置不一致，已回退默认值: thresholds=%s soft_cap=%s hard_hit_floor=%s",
                       m["thresholds"], m["soft_cap"], m["hard_hit_floor"])
        for key in ("thresholds", "soft_cap", "hard_hit_floor", "disagreement_gap", "fusion_weight_llm"):
            m[key] = _copy(DEFAULTS[key])
    j, t, r, keep = m["judge"], m["thresholds"], m["report"], m["retention_days"]
    max_hard = _clamp(m["max_comments_hard"], 1, HARD_MAX_COMMENTS)
    judge_hard = _clamp(j["max_items_hard"], 1, HARD_JUDGE_ITEMS)
    lo, hi = sorted(float(v) for v in r["interval_seconds"])
    lo, hi = max(2.0, lo), max(2.0, hi)
    endpoint = m["comment_endpoint"] if m["comment_endpoint"] in ("main", "wbi_main") else "main"
    mode = r["default_mode"] if r["default_mode"] in ("native", "proxy") else "native"
    return PatrolSettings(
        enabled=bool(m["enabled"]),
        comment_endpoint=endpoint,
        max_comments_default=_clamp(m["max_comments_default"], 1, max_hard),
        max_comments_hard=max_hard,
        reply_floors=_clamp(m["reply_floors"], 0, 200),
        reply_pages_per_floor=_clamp(m["reply_pages_per_floor"], 1, 10),
        reply_requests_hard=_clamp(m["reply_requests_hard"], 0, HARD_REPLY_REQUESTS),
        judge_batch_size=_clamp(j["batch_size"], 1, 20),
        judge_max_items=_clamp(j["max_items"], 1, judge_hard),
        judge_max_items_hard=judge_hard,
        judge_temperature=min(1.0, max(0.0, float(j["temperature"]))),
        judge_cache_days=_clamp(j["cache_days"], 0, 30),
        bot_threshold=int(t["bot"]),
        suspect_threshold=int(t["suspect"]),
        gray_low=int(t["gray_low"]),
        gray_high=int(t["gray_high"]),
        soft_cap=int(m["soft_cap"]),
        hard_hit_floor=int(m["hard_hit_floor"]),
        disagreement_gap=int(m["disagreement_gap"]),
        fusion_weight_llm=float(m["fusion_weight_llm"]),
        report_default_mode=mode,
        report_proxy_allowed=bool(r["proxy_enabled"]),
        report_dry_run=bool(r["dry_run"]),
        report_require_ai=bool(r["require_ai"]),
        report_per_account_limit=_clamp(r["per_account_limit"], 1, HARD_PER_ACCOUNT),
        report_max_per_task=_clamp(r["max_per_task"], 1, HARD_REPORT_PER_TASK),
        report_daily_limit=_clamp(r["daily_limit"], 0, HARD_REPORT_DAILY),
        report_interval_seconds=(lo, hi),
        report_cooldown_minutes=_clamp(r["cooldown_minutes_after_limit"], 1, 24 * 60),
        report_stale_warn_hours=_clamp(r["stale_warn_hours"], 1, 24 * 30),
        report_stale_block_hours=_clamp(r["stale_block_hours"], 1, 24 * 30),
        report_preselect=bool(r["preselect"]),
        retention_scans_days=_clamp(keep["scans"], 1, 365),
        retention_reports_days=_clamp(keep["reports"], 1, 3650),
        retention_llm_cache_days=_clamp(keep["llm_cache"], 1, 365),
    )


def load_settings() -> PatrolSettings:
    """读取当前配置。每次新建 ConfigManager，避免模块级单例不刷新。"""
    from core.config import ConfigManager
    try:
        raw = ConfigManager().get("patrol", {})
    except Exception as exc:  # 配置损坏时回退默认值，不阻断功能
        logger.warning("[巡检] 读取 patrol 配置失败，使用默认值: %s", exc)
        raw = {}
    return build_settings(raw)


def is_embedded_by_astrbot() -> bool:
    """是否由 AstrBot 插件启动（插件启动引擎时注入该环境变量）。"""
    return os.environ.get("FISHTOOL_EMBEDDED_BY", "").strip() == "astrbot"


def astrbot_allows_proxy_report() -> bool:
    """AstrBot 后台「允许 AI 伪人巡检代发举报」开关；只认 "1"。"""
    return os.environ.get("FISHTOOL_ASTRBOT_ALLOW_PROXY_REPORT", "0").strip() == "1"


# ============ 领域异常（路由层映射为 HTTP 状态码） ============

class PatrolError(BiliOpsException):
    """巡检领域异常基类。"""

    def __init__(self, message: str, code: str = "PATROL_ERROR"):
        super().__init__(message, code)


class PatrolLoginRequired(PatrolError):
    def __init__(self, message: str = "请先在欢迎页扫码登录 B站"):
        super().__init__(message, "PATROL_LOGIN_REQUIRED")


class PatrolConsentRequired(PatrolError):
    def __init__(self, message: str = "请先阅读并同意 AI 甄别的外发说明"):
        super().__init__(message, "PATROL_CONSENT_REQUIRED")


class PatrolReportUnavailable(PatrolError):
    def __init__(self, message: str):
        super().__init__(message, "PATROL_PROXY_UNAVAILABLE")


class PatrolAccountChanged(PatrolError):
    def __init__(self, message: str, *, scanner_mid: int = 0, current_uname: str = ""):
        super().__init__(message, "PATROL_ACCOUNT_CHANGED")
        self.scanner_mid = scanner_mid
        self.current_uname = current_uname


class PreferenceConflict(PatrolError):
    def __init__(self, message: str = "设置已在别处修改，请刷新后重试"):
        super().__init__(message, "PATROL_REVISION_CONFLICT")


class PatrolBusy(PatrolError):
    def __init__(self, message: str, code: str = "PATROL_BUSY"):
        super().__init__(message, code)


class StopBatch(PatrolError):
    """代发举报中途必须整批停止。"""

    def __init__(self, message: str, *, cooldown: bool = False, high: bool = False, bili_code: "int | None" = None):
        super().__init__(message, "PATROL_STOP_BATCH")
        self.cooldown = cooldown
        self.high = high
        self.bili_code = bili_code
