"""热点请求预算和统一风控策略。

预算时钟一律用 ``time.monotonic()``（进程内单调时钟）；它**不是** UTC epoch，因此本模块
里的任何时刻都不得当 epoch 持久化，也不得把 ``retry_at_mono`` 当成 UTC 时间写盘。计数只
在内存里，**进程重启即清零**——不存在「跨重启累计硬上限」，要作那种承诺必须在外部另行
持久化计数之后才行。

## 单请求预算协议（07 执行案 §7）

在本模块原有的「立即提交一次逻辑消费」入口（:meth:`RequestBudget.try_acquire` /
:meth:`RequestBudget.acquire`）之外，新增**单请求准入凭证**协议：

``peek`` → ``reserve`` → ``redeem`` / ``release_unused``

- ``peek`` 纯只读，不记账，只用于挑候选；**不能**用来授权发起请求；
- ``reserve`` 原子检查全局窗与本类别窗，放行则占一份容量（写一条 ``LedgerEntry``）；
- ``redeem`` 只把**同一条** ``reserved`` 变为 ``committed``，**绝不再追加第二条**；
- ``release_unused`` 只撤销尚未 redeem 的 reservation，已 redeem 不退款。

凭证（:class:`LogicalAdmission`）由预算对象自身签发、只在进程内传递，**不是**认证 token；
防重放完全依赖三项校验：同一 issuer 签发、同一 ``operation_key``、状态仍为 ``reserved``
且只能兑换一次（R7.1 起不再校验 owner_task）。

## watch 逻辑调度策略（07 执行案 §10.1 / §10.2）

``load_watch_scheduler_policy(path)`` 从 ``config/budget.yaml`` 的 ``watch_scheduler`` 段读出
:class:`WatchSchedulerPolicy`：``mode`` 为 ``partitioned``（类别窗 + 总窗并行，任一不足即拒绝）或
``shared_only``（仅总窗，显式 ``category_isolation=False``）。该段只约束 **L 层逻辑账本**，
**不写 HTTP 账本、不冒充跨进程持久限额**，也不改动 H 层任何数字。
"""
from __future__ import annotations

import asyncio
import logging
import math
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

#: 预留凭证的硬超时默认值（秒）。
#:
#: 07 执行案 §7.3：``reserve`` 时写 ``reserve_deadline_mono = now + reserve_timeout_s``，
#: 该值取「单目标采集截止时间」，与本轮单目标 deadline 同源，不另设一个更长的值。
#: W1 先以可配置的固定默认值落地机制；部署侧可按单目标采集预算覆盖
#: （构造参数 ``reserve_timeout_s``）。超时后对该凭证的 ``redeem`` 一律拒绝
#: （``admission_expired``），只释放占用、不补记 committed。
DEFAULT_RESERVE_TIMEOUT_S: float = 60.0

#: 仅被未释放 reservation 阻塞时建议的短轮询间隔（秒）。
#: 不编造 reservation「将永久占用」，但也不谎报一个精确的释放时刻。
_RESERVE_POLL_S: float = 0.5


@dataclass(frozen=True)
class BudgetDecision:
    """一次「非阻塞预算尝试」的结果（不可变）。

    Attributes:
        granted: 是否获准。对 ``try_acquire`` 表示已原子记入总额与该类别消费；
            对 ``peek`` / ``reserve`` 表示当前窗口仍有容量。
        retry_at_mono: 被拒时的最早可重试时刻（``time.monotonic()`` 口径）；获准时为 None。
        reason_code: 被拒原因码（``backoff`` 表示 429 退避中，``rate_limited`` 表示撞上某条
            窗口限速，``unknown_category`` 表示分桶模式收到未登记类别）；获准时为 None。
    """

    granted: bool
    retry_at_mono: Optional[float] = None
    reason_code: Optional[str] = None


class InvalidLogicalAdmission(Exception):
    """非法 / 复用 / 过期的逻辑准入凭证。

    Attributes:
        reason_code: 稳定原因码，目前有 ``invalid_or_reused_admission`` 与
            ``admission_expired`` 两种，供调用方 / 测试断言。
    """

    def __init__(self, reason_code: str) -> None:
        """初始化异常。

        Args:
            reason_code: 稳定原因码。
        """
        self.reason_code = reason_code
        super().__init__(reason_code)


class BudgetWiringError(RuntimeError):
    """预算接线错误：例如有预算的 watch 路径注入了不支持准入协议的自定义端口。"""

    code: str = "budget_wiring_error"

    def __init__(self, message: str = "") -> None:
        """初始化异常。

        Args:
            message: 面向部署者的中文说明；缺省用类级 ``code``。
        """
        super().__init__(message or self.code)


@dataclass(frozen=True)
class LogicalAdmission:
    """一次逻辑准入凭证（**仅进程内传递，不进任何 DTO / HTTP 参数**）。

    Attributes:
        entry_id: 账本条目 ID。**只由** :meth:`RequestBudget.reserve` 创建。
        operation_key: 该凭证绑定的操作标识（watch 链路里是 bvid）。
        kind: 消费类别标签。
        issuer: 签发该凭证的 :class:`RequestBudget` 实例（身份校验用 ``is``）。
    """

    entry_id: str
    operation_key: str
    kind: str
    issuer: "RequestBudget"


@dataclass(frozen=True)
class AdmissionResult:
    """一次准入尝试的结构化结果。

    Attributes:
        decision: 准入裁决（``granted`` 为 True 时 ``admission`` 必非 None）。
        admission: 获准时签发的凭证；被拒时为 None。
    """

    decision: BudgetDecision
    admission: Optional[LogicalAdmission] = None


@dataclass
class LedgerEntry:
    """一条逻辑账本条目（内存态，进程重启即清空）。

    Attributes:
        entry_id: 唯一 ID。
        kind: 消费类别标签。
        operation_key: 该条目绑定的操作标识。
        reserved_at_mono: 预留时刻（单调时钟）。
        reserve_deadline_mono: 预留硬超时（超过即视为过期释放）。
        charged_at_mono: 真正 redeemed 时的单调时刻；未兑换为 None。
        state: ``reserved`` / ``committed`` / ``cancelled``。
    """

    entry_id: str
    kind: str
    operation_key: str
    reserved_at_mono: float
    reserve_deadline_mono: float
    charged_at_mono: Optional[float] = None
    state: str = "reserved"


def validated_issuer(admission: Any) -> "RequestBudget":
    """受控地取出凭证的发行方（拒绝任意「有 redeem 属性」的伪凭证）。

    Args:
        admission: 待校验对象。

    Returns:
        RequestBudget: 合法凭证的发行方实例。

    Raises:
        InvalidLogicalAdmission: 对象不是 :class:`LogicalAdmission`，或其 issuer
            不是 :class:`RequestBudget`。
    """
    if not isinstance(admission, LogicalAdmission):
        raise InvalidLogicalAdmission("invalid_or_reused_admission")
    issuer = admission.issuer
    if not isinstance(issuer, RequestBudget):
        raise InvalidLogicalAdmission("invalid_or_reused_admission")
    return issuer


def _coerce_positive_int(value: Any, field_name: str) -> int:
    """严格校验窗口上限：必须是正整数，拒绝 bool / 0 / 负数 / 非整数。

    Args:
        value: 待校验值。
        field_name: 报错时展示的字段名。

    Returns:
        int: 校验通过的正整数。

    Raises:
        ValueError: 非正整数（含 bool）时抛出。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"窗口上限必须是正整数: {field_name}={value!r}")
    return value


def _normalize_category_limits(
    raw: Any,
) -> Optional[dict[str, tuple[tuple[float, int], ...]]]:
    """把 ``category_limits`` 归一化成 ``{kind: ((width, limit), ...)}``。

    Args:
        raw: 原始配置。``None`` 表示 shared 兼容模式（不做类别分桶）。
            否则为 ``{kind: {"per_minute":..,"per_hour":..,"per_day":..}}``
            或 ``{kind: (per_minute, per_hour, per_day)}``。

    Returns:
        Optional[dict]: 归一化后的类别窗口表；``None`` 表示 shared 模式。

    Raises:
        ValueError: 结构非法或存在非正整数时抛出（不静默降级）。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise ValueError("category_limits 必须是非空映射")
    normalized: dict[str, tuple[tuple[float, int], ...]] = {}
    for kind, spec in raw.items():
        if not isinstance(kind, str) or not kind:
            raise ValueError("category_limits 的键必须是非空字符串")
        if isinstance(spec, dict):
            pm = _coerce_positive_int(spec.get("per_minute"), f"{kind}.per_minute")
            ph = _coerce_positive_int(spec.get("per_hour"), f"{kind}.per_hour")
            pd = _coerce_positive_int(spec.get("per_day"), f"{kind}.per_day")
        elif isinstance(spec, (tuple, list)) and len(spec) == 3:
            if all(isinstance(pair, (tuple, list)) and len(pair) == 2 for pair in spec):
                # 幂等：已是归一化形态 ``((width, limit), ...)``（例如来自
                # :class:`WatchSchedulerPolicy`），校验后原样透传。
                normalized[kind] = tuple(
                    (float(width), _coerce_positive_int(limit, f"{kind}.limit"))
                    for width, limit in spec
                )
                continue
            pm, ph, pd = (
                _coerce_positive_int(item, kind) for item in spec
            )
        else:
            raise ValueError(f"category_limits[{kind}] 格式非法: {spec!r}")
        normalized[kind] = ((60.0, pm), (3600.0, ph), (86400.0, pd))
    return normalized


# --------------------------------------------------------------------- watch 逻辑调度策略
#
# 07 执行案 §10.1 / §10.2：``config/budget.yaml`` 的 ``watch_scheduler`` 段只控制 **L 层逻辑预算**，
# 不改动 H 层任何数字（1800 及五类 HTTP 上限一律不碰）。该段是**新的业务分配决定**，不是仓库既有
# 常量、也不是实盘最优值；原状态「待维护者确认（pending_maintainer_approval）」已于 2026-10-05
# 获维护者批准照该组运行（见 :data:`WATCH_SCHEDULER_PENDING_NOTE`）。

#: 配置键名（``config/budget.yaml``）。
WATCH_SCHEDULER_KEY: str = "watch_scheduler"

#: 逻辑调度模式：``partitioned``（类别窗 + 总窗并行） / ``shared_only``（仅总窗）。
WATCH_SCHEDULER_MODE_PARTITIONED: str = "partitioned"
WATCH_SCHEDULER_MODE_SHARED_ONLY: str = "shared_only"
WATCH_SCHEDULER_MODES: tuple[str, ...] = (
    WATCH_SCHEDULER_MODE_PARTITIONED,
    WATCH_SCHEDULER_MODE_SHARED_ONLY,
)

#: 轮转 / 公平顺序缺省：先 normal 后 fast（仅用于策略文档，运行时轮转起始类别见 ``WatchService``）。
DEFAULT_WATCH_FAIR_ORDER: tuple[str, ...] = ("normal_watch", "fast_watch")

#: 策略状态标记（07 执行案 §10.1）。落配置 / docstring 时逐处引用，避免把提案当既有常量。
#: 名称保留（``PENDING_NOTE``）以兼容既有引用：常量同时记录初始状态
#: ``pending_maintainer_approval``（待维护者确认）与 2026-10-05 维护者已批准，
#: 不把提案数字伪装成既有常量或实盘最优值。
WATCH_SCHEDULER_PENDING_NOTE: str = (
    "07 执行案 §10.1 提出的初始 L 层策略：原状态 pending_maintainer_approval（待维护者确认），"
    "已于 2026-10-05 获维护者批准照该组运行；仍非仓库既有常量、非实盘最优值。"
)


class WatchSchedulerConfigError(ValueError):
    """watch 逻辑调度策略配置非法（加载期即抛，**绝不**静默降级回 shared）。"""

    code: str = "watch_scheduler_config_error"


@dataclass(frozen=True)
class WatchSchedulerPolicy:
    """``watch_scheduler`` 段的只读快照（L 层逻辑调度策略的唯一来源）。

    Attributes:
        policy_version: 策略版本标签（写进状态与文档）。
        mode: ``partitioned`` / ``shared_only``。
        per_minute / per_hour / per_day: 总窗（global）上限。
        category_limits: 归一化类别窗表 ``{kind: ((width, limit), ...)}``；``shared_only`` 时为
            ``None``（不做类别隔离）。
        fair_order: 类间轮转的类别顺序（仅含已登记类别）。
        category_isolation: 是否**真的**做了类别隔离。``shared_only`` 显式为 ``False``——
            不得默认静默降级，也不得拿它冒充「类别配额已修复」的验收结论。
        source: 配置来源（路径或说明），便于启动诊断。
    """

    policy_version: str
    mode: str
    per_minute: int
    per_hour: int
    per_day: int
    category_limits: Optional[dict[str, tuple[tuple[float, int], ...]]]
    fair_order: tuple[str, ...]
    category_isolation: bool
    source: str = ""

    def build_budget(self, **overrides: Any) -> "RequestBudget":
        """按本策略构造一个 :class:`RequestBudget`（W3 生产工厂用；测试可注入小数字覆盖）。

        Args:
            **overrides: 覆盖 ``per_minute`` / ``per_hour`` / ``per_day`` / ``category_limits``
                / ``clock`` / ``reserve_timeout_s`` / ``policy_version`` 等构造参数。

        Returns:
            RequestBudget: 与策略同源的真预算器（``shared_only`` 时 ``category_limits=None``）。
        """
        kwargs: dict[str, Any] = {
            "per_minute": self.per_minute,
            "per_hour": self.per_hour,
            "per_day": self.per_day,
            "category_limits": self.category_limits,
            "policy_version": self.policy_version,
        }
        kwargs.update(overrides)
        return RequestBudget(**kwargs)


def _watch_scheduler_config_path() -> Path:
    """定位 ``config/budget.yaml``（与 ``core.request_budget._budget_config_path`` **同口径**）。

    frozen 环境取 exe 同级目录；源码环境取本文件向上三级（``modules/hotspot`` -> 仓库根）。

    Returns:
        Path: 账本文件路径（不保证存在）。
    """
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parent.parent.parent
    return base_dir / "config" / "budget.yaml"


def _compat_shared_policy(*, source: str) -> WatchSchedulerPolicy:
    """旧配置 / 缺段时的**显式**兼容策略：shared_only + ``category_isolation=False``。

    这不是静默降级——调用方负责打印 warning，并在状态里如实显示 ``category_isolation=False``。
    ``20 / 300 / 3000`` 与 :class:`RequestBudget` 既有默认同值，**不是**新配额、也不是实盘最优。

    Args:
        source: 配置来源说明。

    Returns:
        WatchSchedulerPolicy: 兼容策略（不做类别隔离）。
    """
    return WatchSchedulerPolicy(
        policy_version="watch_logical_compat",
        mode=WATCH_SCHEDULER_MODE_SHARED_ONLY,
        per_minute=20,
        per_hour=300,
        per_day=3000,
        category_limits=None,
        fair_order=DEFAULT_WATCH_FAIR_ORDER,
        category_isolation=False,
        source=source,
    )


def _parse_watch_scheduler_policy(section: Any, *, source: str) -> WatchSchedulerPolicy:
    """把 ``watch_scheduler`` 段解析成 :class:`WatchSchedulerPolicy`（非法即抛，不降级）。

    Args:
        section: YAML 解析出的 ``watch_scheduler`` 映射。
        source: 配置来源说明（报错用）。

    Returns:
        WatchSchedulerPolicy: 校验通过的策略。

    Raises:
        WatchSchedulerConfigError: 结构 / 取值非法，或类别窗合计超过总窗。
    """
    if not isinstance(section, dict):
        raise WatchSchedulerConfigError(f"{source} 的 watch_scheduler 必须是映射")
    mode = section.get("mode")
    if mode not in WATCH_SCHEDULER_MODES:
        raise WatchSchedulerConfigError(
            f"{source} 的 watch_scheduler.mode 非法: {mode!r}（应为 {WATCH_SCHEDULER_MODES}）"
        )
    policy_version = str(section.get("policy_version") or "watch_logical_v1")
    total = section.get("total") or {}
    if not isinstance(total, dict):
        raise WatchSchedulerConfigError(f"{source} 的 watch_scheduler.total 必须是映射")

    if mode == WATCH_SCHEDULER_MODE_PARTITIONED:
        per_minute = _coerce_positive_int(total.get("per_minute"), "total.per_minute")
        per_hour = _coerce_positive_int(total.get("per_hour"), "total.per_hour")
        per_day = _coerce_positive_int(total.get("per_day"), "total.per_day")
        normalized = _normalize_category_limits(section.get("category_limits"))
        if normalized is None:
            raise WatchSchedulerConfigError(
                f"{source} 的 partitioned 模式必须提供 category_limits"
            )
        for required in DEFAULT_WATCH_FAIR_ORDER:
            if required not in normalized:
                raise WatchSchedulerConfigError(
                    f"{source} 的 partitioned 模式缺类别 {required!r}"
                )
        # 类别窗与总窗并行校验的前提：每个窗口上「各类上限之和 <= 总窗上限」，否则分桶自相矛盾。
        totals = (per_minute, per_hour, per_day)
        for index, field_name in enumerate(("per_minute", "per_hour", "per_day")):
            category_sum = sum(windows[index][1] for windows in normalized.values())
            if category_sum > totals[index]:
                raise WatchSchedulerConfigError(
                    f"{source} 的类别上限合计 {category_sum} 超过 total.{field_name}={totals[index]}"
                )
        fair_order = tuple(section.get("fair_order") or DEFAULT_WATCH_FAIR_ORDER)
        for kind in fair_order:
            if kind not in normalized:
                raise WatchSchedulerConfigError(
                    f"{source} 的 fair_order 含未登记类别 {kind!r}"
                )
        return WatchSchedulerPolicy(
            policy_version=policy_version,
            mode=mode,
            per_minute=per_minute,
            per_hour=per_hour,
            per_day=per_day,
            category_limits=normalized,
            fair_order=fair_order,
            category_isolation=True,
            source=source,
        )

    # shared_only：保留共享限额，显式声明 category_isolation=false。
    per_minute = _coerce_positive_int(total.get("per_minute", 20), "total.per_minute")
    per_hour = _coerce_positive_int(total.get("per_hour", 300), "total.per_hour")
    per_day = _coerce_positive_int(total.get("per_day", 3000), "total.per_day")
    return WatchSchedulerPolicy(
        policy_version=policy_version,
        mode=mode,
        per_minute=per_minute,
        per_hour=per_hour,
        per_day=per_day,
        category_limits=None,
        fair_order=DEFAULT_WATCH_FAIR_ORDER,
        category_isolation=False,
        source=source,
    )


def load_watch_scheduler_policy(path: Any = None) -> WatchSchedulerPolicy:
    """加载并校验 ``config/budget.yaml`` 的 ``watch_scheduler`` 段（返回纯配置对象）。

    缺文件 / 缺段：返回**显式** shared_only 兼容策略并打印 warning（``category_isolation=False``），
    绝不静默把它当「类别配额已启用」。有段但非法：抛 :class:`WatchSchedulerConfigError`，
    停 watch 采样并暴露配置错误，而不是悄悄回 shared（07 执行案 §10.2）。

    Args:
        path: 账本路径；``None`` 时按 :func:`_watch_scheduler_config_path` 定位（与
            ``core.request_budget`` 同口径）。

    Returns:
        WatchSchedulerPolicy: 只读策略对象。

    Raises:
        WatchSchedulerConfigError: 段结构 / 取值非法。
    """
    config_path = Path(path) if path is not None else _watch_scheduler_config_path()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        logger.warning(
            "budget.yaml 不存在（%s）：watch 逻辑策略按 shared_only 兼容运行，"
            "category_isolation=false（待维护者升级配置）",
            config_path,
        )
        return _compat_shared_policy(source=str(config_path))
    except Exception as exc:  # noqa: BLE001 - 账本损坏必须显式暴露，不猜数
        raise WatchSchedulerConfigError(f"读取 {config_path} 失败: {exc!r}") from exc
    if not isinstance(raw, dict):
        raise WatchSchedulerConfigError(f"{config_path} 顶层不是映射")

    section = raw.get(WATCH_SCHEDULER_KEY)
    if section is None:
        logger.warning(
            "%s 缺少 watch_scheduler 段：本次按 shared_only 兼容运行，category_isolation=false"
            "（待维护者升级配置，不得据此声称类别配额已修复）",
            config_path,
        )
        return _compat_shared_policy(source=str(config_path))
    return _parse_watch_scheduler_policy(section, source=str(config_path))


class RequestBudget:
    """按分钟、小时、天限制请求数量，超限排到下一可用窗口。

    - 同步入口 :meth:`try_acquire`：**不许** ``await`` / ``sleep``，也**不得**在持锁状态下
      等待预算——只做一次「立刻给结论」的检查。
    - 异步入口 :meth:`acquire`：旧调用点的兼容包装，内部循环调 :meth:`try_acquire`，拿到
      ``retry_at_mono`` 后**在锁外** ``await asyncio.sleep``。
    - 单请求准入协议 :meth:`peek` / :meth:`reserve` / :meth:`redeem` / :meth:`release_unused`
      / :meth:`snapshot`：详见模块文档。
    """

    def __init__(
        self,
        per_minute: int = 20,
        per_hour: int = 300,
        per_day: int = 3000,
        *,
        category_limits: Any = None,
        clock: Any = time.monotonic,
        reserve_timeout_s: float = DEFAULT_RESERVE_TIMEOUT_S,
        policy_version: Optional[str] = None,
    ) -> None:
        """初始化请求预算。

        Args:
            per_minute: 每分钟上限（全局窗）。
            per_hour: 每小时上限（全局窗）。
            per_day: 每天上限（全局窗）。
            category_limits: 类别分桶上限；``None`` 保持旧 shared 模式（不做类别隔离）。
            clock: 单调时钟取值函数，缺省 ``time.monotonic``。所有 reserve / redeem /
                peek 必须使用同一 clock，不能selector 用一个过期 now、collector 用真实新时间。
            reserve_timeout_s: 预留硬超时秒数，见 :data:`DEFAULT_RESERVE_TIMEOUT_S`。
            policy_version: 逻辑调度策略版本标签（可选，仅用于观测；``None`` 表示未声明）。
        """
        self.limits = ((60.0, per_minute), (3600.0, per_hour), (86400.0, per_day))
        self._requests: deque[float] = deque()
        # 临界区里没有任何 await，用同步短锁即可；asyncio.Lock 会逼出「持锁 sleep」的老问题。
        self._lock = threading.Lock()
        self.backoff_until = 0.0
        # 各类别消费计数（仅内存，进程重启清零）。准许才 +1，拒绝不扣。**仅作观测**。
        self.category_consumption: dict[str, int] = {}
        # 单请求准入协议的账本注册表：entry_id -> LedgerEntry。entry_id 只由 reserve 创建。
        self._entries: dict[str, LedgerEntry] = {}
        self.clock = clock
        self.reserve_timeout_s = float(reserve_timeout_s)
        self.category_limits = _normalize_category_limits(category_limits)
        self.policy_version = policy_version

    # ------------------------------------------------------------------ 同步非阻塞检查

    def try_acquire(self, kind: str, now_mono: float) -> BudgetDecision:
        """同步、非阻塞地尝试领取一次预算（**不得** await / sleep / 持锁等待）。

        这是旧的「立即提交一次逻辑消费」入口；它走全局 ``_requests`` 总量，**不**参与类别
        分桶（分桶由 :meth:`reserve` / :meth:`peek` 负责）。保留它以保证既有调用点与既有
        shared 测试逐字节兼容。

        Args:
            kind: 消费类别标签（如 ``general`` / ``normal_watch`` / ``fast``）。
            now_mono: 当前单调时钟读数（``time.monotonic()``）。

        Returns:
            BudgetDecision: 准许时已原子记入总额与 ``kind`` 消费；拒绝时两者都不扣，
            并给出最早可重试的单调时刻 ``retry_at_mono`` 与 ``reason_code``。
        """
        with self._lock:
            retry_at_mono, reason_code = self._plan_locked(now_mono)
            if retry_at_mono <= now_mono:
                # 准许：总额 + 该类别消费一起在锁内原子记入。
                self._requests.append(now_mono)
                self.category_consumption[kind] = self.category_consumption.get(kind, 0) + 1
                return BudgetDecision(granted=True)
            # 拒绝：不改 _requests、不改类别消费。
            return BudgetDecision(
                granted=False, retry_at_mono=retry_at_mono, reason_code=reason_code
            )

    def _plan_locked(self, now_mono: float) -> tuple[float, str]:
        """在持锁状态下算出「最早可放行时刻 + 原因码」（调用方必须已持锁）。

        Args:
            now_mono: 当前单调时钟读数。

        Returns:
            ``(retry_at_mono, reason_code)``；``retry_at_mono <= now_mono`` 表示可放行。
        """
        # 先过期清理：只保留最近 24h 的请求时间戳（天窗）。
        while self._requests and now_mono - self._requests[0] >= 86400:
            self._requests.popleft()
        earliest = self.backoff_until
        reason = "backoff"
        for window, limit in self.limits:
            if len(self._requests) >= limit:
                # 该窗口下，第 limit 个（从尾部数）请求的时间 + 窗口宽度即最早可再发时刻。
                candidate = self._requests[-limit] + window
                if candidate > earliest:
                    earliest = candidate
                    reason = "rate_limited"
        return earliest, reason

    # ------------------------------------------------------------------ 单请求准入协议

    def peek(self, kind: str, now_mono: float) -> BudgetDecision:
        """纯只读地看当前窗口是否仍可放行一次 ``kind``（**不记账、不授权发请求**）。

        Args:
            kind: 消费类别标签。
            now_mono: 当前单调时钟读数。

        Returns:
            BudgetDecision: ``granted`` 只表示「此刻 reserve 会放行」，不占用任何容量。
        """
        now_mono = float(now_mono)
        with self._lock:
            self._expire_reservations_locked(now_mono)
            retry_at_mono, reason_code = self._plan_ledger_locked(kind, now_mono)
            if retry_at_mono <= now_mono:
                return BudgetDecision(granted=True)
            return BudgetDecision(
                granted=False, retry_at_mono=retry_at_mono, reason_code=reason_code
            )

    def reserve(
        self, kind: str, now_mono: float, *, operation_key: str
    ) -> AdmissionResult:
        """原子检查全局窗与本类别窗，放行则占一份容量（写一条 ``reserved`` 条目）。

        **必须紧邻单目标执行**调用，绝不预先 reserve 整批目标。

        Args:
            kind: 消费类别标签。
            now_mono: 当前单调时钟读数。
            operation_key: 该凭证绑定的操作标识（watch 链路里是 bvid）。

        Returns:
            AdmissionResult: 获准时携带新签发的 :class:`LogicalAdmission`；被拒时
            ``admission`` 为 None，``decision`` 给出 ``retry_at_mono`` 与 ``reason_code``。
        """
        now_mono = float(now_mono)
        key = str(operation_key)
        with self._lock:
            self._expire_reservations_locked(now_mono)
            retry_at_mono, reason_code = self._plan_ledger_locked(kind, now_mono)
            if retry_at_mono > now_mono:
                return AdmissionResult(
                    decision=BudgetDecision(
                        granted=False, retry_at_mono=retry_at_mono, reason_code=reason_code
                    )
                )
            entry_id = uuid.uuid4().hex
            entry = LedgerEntry(
                entry_id=entry_id,
                kind=kind,
                operation_key=key,
                reserved_at_mono=now_mono,
                reserve_deadline_mono=now_mono + self.reserve_timeout_s,
            )
            self._entries[entry_id] = entry
            admission = LogicalAdmission(
                entry_id=entry_id, operation_key=key, kind=kind, issuer=self
            )
            return AdmissionResult(decision=BudgetDecision(granted=True), admission=admission)

    def redeem(self, admission: Any, *, operation_key: str, now_mono: float) -> None:
        """把**同一条** reservation 兑换成 committed（不追加第二条、不调用 acquire）。

        三项校验：同一 issuer（``admission.issuer is self``）、同一 ``operation_key``、
        状态仍为 ``reserved`` 且只能兑换一次。R7.1 起**不**校验 owner_task。

        Args:
            admission: :meth:`reserve` 签发的凭证。
            operation_key: 本次兑换绑定的操作标识（必须与签发时一致）。
            now_mono: 当前单调时钟读数。

        Raises:
            InvalidLogicalAdmission: 校验失败；或已超过 ``reserve_deadline_mono``
                （此时该条目标为 ``cancelled`` 且原因码为 ``admission_expired``）。
        """
        now_mono = float(now_mono)
        key = str(operation_key)
        with self._lock:
            if not isinstance(admission, LogicalAdmission) or admission.issuer is not self:
                raise InvalidLogicalAdmission("invalid_or_reused_admission")
            entry = self._entries.get(admission.entry_id)
            if entry is None or entry.state != "reserved" or entry.operation_key != key:
                raise InvalidLogicalAdmission("invalid_or_reused_admission")
            if now_mono > entry.reserve_deadline_mono:
                # 超时预留只释放占用，不补记 committed。
                entry.state = "cancelled"
                raise InvalidLogicalAdmission("admission_expired")
            entry.state = "committed"
            entry.charged_at_mono = now_mono
            # 只变更这一条 entry；绝不再次追加一个总量消费。

    def release_unused(self, admission: Any) -> bool:
        """撤销**尚未 redeem** 的 reservation。

        已 redeem（``committed``）不退款——即使外部请求失败 / 业务结果后来被丢弃；已
        ``cancelled`` 的条目也不再处理。

        Args:
            admission: 待撤销的凭证。

        Returns:
            bool: 本次确实撤销了一条 ``reserved`` 条目返回 True；否则返回 False。
        """
        with self._lock:
            if not isinstance(admission, LogicalAdmission) or admission.issuer is not self:
                return False
            entry = self._entries.get(admission.entry_id)
            if entry is None or entry.state != "reserved":
                return False
            entry.state = "cancelled"
            return True

    def snapshot(self, now_mono: float) -> dict:
        """返回当前账本占用快照（观测用；会先做一次预留超时清扫）。

        Args:
            now_mono: 当前单调时钟读数。

        Returns:
            dict: 含 reserved / committed / cancelled 条数、各窗口全局占用、类别占用、
            模式标识。**不要**把 ``category_consumption``（累计计数）当成 24h 当前可用量。
        """
        now_mono = float(now_mono)
        with self._lock:
            self._expire_reservations_locked(now_mono)
            reserved = sum(1 for e in self._entries.values() if e.state == "reserved")
            committed = sum(1 for e in self._entries.values() if e.state == "committed")
            cancelled = sum(1 for e in self._entries.values() if e.state == "cancelled")
            category_used: dict[str, int] = {}
            if self.category_limits is not None:
                for kind in self.category_limits:
                    category_used[kind] = self._category_used_locked(kind, 86400.0, now_mono)
            return {
                "mode": "partitioned" if self.category_limits is not None else "shared",
                # 07 案 §10.1：类别隔离是否真的启用，用状态如实展示，不得默认静默降级。
                "category_isolation": self.category_limits is not None,
                "policy_version": self.policy_version,
                "reserved": reserved,
                "committed": committed,
                "cancelled": cancelled,
                "entries": len(self._entries),
                "global_used_60s": self._window_used_locked(60.0, now_mono),
                "global_used_3600s": self._window_used_locked(3600.0, now_mono),
                "global_used_86400s": self._window_used_locked(86400.0, now_mono),
                "category_used": category_used,
                "category_consumption": dict(self.category_consumption),
            }

    # ------------------------------------------------------------------ 账本内部工具（持锁）

    def _expire_reservations_locked(self, now_mono: float) -> None:
        """把超过 ``reserve_deadline_mono`` 的 ``reserved`` 条目标为 ``cancelled``（持锁）。

        Args:
            now_mono: 当前单调时钟读数。

        Returns:
            无。
        """
        for entry in self._entries.values():
            if entry.state == "reserved" and now_mono > entry.reserve_deadline_mono:
                entry.state = "cancelled"

    def _window_used_locked(self, width: float, now_mono: float) -> int:
        """算全局窗占用：legacy ``_requests`` + 全部 reservation + 窗内 committed。

        Args:
            width: 窗口宽度（秒）。
            now_mono: 当前单调时钟读数。

        Returns:
            int: 该窗口当前占用的逻辑条目数。
        """
        used = 0
        for ts in self._requests:
            if now_mono - ts < width:
                used += 1
        for entry in self._entries.values():
            if entry.state == "reserved":
                # 未释放的 reservation 在任何窗口都占用（不因时间窗过期悄悄释放）。
                used += 1
            elif (
                entry.state == "committed"
                and entry.charged_at_mono is not None
                and now_mono - entry.charged_at_mono < width
            ):
                used += 1
        return used

    def _category_used_locked(self, kind: str, width: float, now_mono: float) -> int:
        """算某类别窗占用：该类全部 reservation + 窗内 committed。

        Args:
            kind: 消费类别标签。
            width: 窗口宽度（秒）。
            now_mono: 当前单调时钟读数。

        Returns:
            int: 该类别该窗口当前占用的逻辑条目数。
        """
        used = 0
        for entry in self._entries.values():
            if entry.kind != kind:
                continue
            if entry.state == "reserved":
                used += 1
            elif (
                entry.state == "committed"
                and entry.charged_at_mono is not None
                and now_mono - entry.charged_at_mono < width
            ):
                used += 1
        return used

    def _freed_at_locked(self, kind: Optional[str], width: float, now_mono: float) -> float:
        """估算某窗口「最早可释放一个名额」的单调时刻（持锁）。

        Args:
            kind: 类别标签；``None`` 表示全局窗。
            width: 窗口宽度（秒）。
            now_mono: 当前单调时钟读数。

        Returns:
            float: 估算的释放时刻。若窗口内只有未释放 reservation（不会随时间自然过期），
            返回一个短轮询时刻，不编造永久占用。
        """
        timestamps: list[float] = []
        if kind is None:
            for ts in self._requests:
                if now_mono - ts < width:
                    timestamps.append(ts)
        for entry in self._entries.values():
            if kind is not None and entry.kind != kind:
                continue
            if entry.state == "committed" and entry.charged_at_mono is not None:
                if now_mono - entry.charged_at_mono < width:
                    timestamps.append(entry.charged_at_mono)
        if not timestamps:
            return now_mono + _RESERVE_POLL_S
        return min(timestamps) + width

    def _plan_ledger_locked(self, kind: str, now_mono: float) -> tuple[float, Optional[str]]:
        """在持锁状态下算出账本「最早可放行时刻 + 原因码」（持锁）。

        Args:
            kind: 消费类别标签。
            now_mono: 当前单调时钟读数。

        Returns:
            ``(retry_at_mono, reason_code)``；``retry_at_mono <= now_mono`` 表示可放行。
        """
        blocked = False
        retry_at_mono = 0.0
        reason_code = "rate_limited"
        for width, limit in self.limits:
            if self._window_used_locked(width, now_mono) >= limit:
                blocked = True
                retry_at_mono = max(retry_at_mono, self._freed_at_locked(None, width, now_mono))
        if self.category_limits is not None:
            category_windows = self.category_limits.get(kind)
            if category_windows is None:
                # 分桶模式收到未登记类别：显式拒绝，不静默并入共享总量。
                return math.inf, "unknown_category"
            for width, limit in category_windows:
                if self._category_used_locked(kind, width, now_mono) >= limit:
                    blocked = True
                    retry_at_mono = max(
                        retry_at_mono, self._freed_at_locked(kind, width, now_mono)
                    )
        if blocked:
            return retry_at_mono, reason_code
        return now_mono, None

    # ------------------------------------------------------------------ 异步兼容入口

    async def acquire(self) -> float:
        """等待可用预算并返回实际等待秒数（保留无参调用兼容）。

        内部按 ``kind='general'`` 循环调 :meth:`try_acquire`；被拒时拿着 ``retry_at_mono``
        **在锁外** ``await asyncio.sleep``，因此一个调用者在等，其他调用者的
        :meth:`try_acquire` **不会**被堵在锁外。

        Returns:
            float: 本调用实际累计等待的秒数。
        """
        waited = 0.0
        while True:
            decision = self.try_acquire("general", self.clock())
            if decision.granted:
                return waited
            now = self.clock()
            retry_at = decision.retry_at_mono if decision.retry_at_mono is not None else now
            delay = retry_at - now
            if delay <= 0:
                # 理论不可达（retry_at 已过就会被准许）；兜底让出一次事件循环，避免忙等。
                await asyncio.sleep(0)
                continue
            # 锁此刻已经放开：在锁外睡，别的调用者可以照常 try_acquire。
            await asyncio.sleep(delay)
            waited += delay

    def report_429(self, attempt: int = 0) -> float:
        """登记 429 并返回 30 秒到 10 分钟封顶的指数退避秒数。

        Args:
            attempt: 第几次重试（0 起）；用于指数增长。

        Returns:
            float: 本次退避秒数（30s 起、600s 封顶）。
        """
        delay = min(600.0, 30.0 * (2 ** max(0, attempt)))
        # backoff_until 是单调时钟口径；只推进不后退，避免并发下退避被抹平。
        self.backoff_until = max(self.backoff_until, time.monotonic() + delay)
        return delay
