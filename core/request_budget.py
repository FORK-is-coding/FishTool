"""请求尝试预算的通用计数（规格 §8.1）。

本模块只做通用计数，不依赖任何业务模块（不 import client / service / DB / Web）。
设计要点：

- ``AttemptBudget`` 用「发送前钩子」而不是「发送后记账」来计数，
  因此每次真实 HTTP 尝试都会在真正发出去之前被计入；
- ``deadline_monotonic`` 使用 ``time.monotonic``，不受系统时间回拨影响；
- ``current_attempt_budget`` 是 ``ContextVar``，**默认值为 ``None``**，
  因此上下文之外的旧调用行为完全不变（这是硬约束）；
- 另有一层**进程级**滚动配额（规格 §2.2 / §2.6）：计数落 SQLite（``http_quota_buckets``），
  进程重启不清零；只有调用方显式传入 ``domain`` / ``category`` 时才参与判定，
  **不传时行为与改造前完全一致**；运行时判定**只按分类硬上限**，总闸不参与运行时拦截；
- 再有**风控兜底层**（规格 §2.4，落 SQLite ``domain_cooldown`` / ``http_risk_events`` /
  ``ip_circuit_state``）：``before_http_attempt`` 的顺序是「任务内预算 → IP 级熔断 →
  分域冷却 → 分类配额」；只传 ``domain`` 时只做熔断 / 冷却判定（免凭证通道的配额
  由调用方在 sources 层显式记账，避免同一次请求被计两次）；IP 级熔断**只能人工清除**，
  不做「到期自动恢复」；
- 总闸（``global_limit``）是**配置校验**：加载 ``config/budget.yaml`` 时断言
  ``sum(各分类上限) <= 总闸``，越界即抛 ``QuotaConfigError``；**它不是运行时限制器**
  （五类封顶后合计够不到总闸，运行时判总闸是死分支，已删除）。
- 同一事件循环内的 async child task 会继承同一 budget 对象，
  计数是同步的；不要把该对象跨线程共享。

典型用法::

    from core.request_budget import AttemptBudget, current_attempt_budget

    token = current_attempt_budget.set(AttemptBudget(max_attempts=3000, deadline_monotonic=...))
    try:
        ...
    finally:
        current_attempt_budget.reset(token)
"""
import logging
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from time import monotonic, time
from typing import Dict, Mapping, Optional, Tuple, Union

import yaml

logger = logging.getLogger(__name__)


class RequestBudgetExceeded(RuntimeError):
    """请求预算耗尽（超过 HTTP 尝试上限或已过 deadline）。

    该异常**不是**可重试网络异常：上层捕获后必须原样抛出，
    绝不能被转换成普通 APIError 后再次进入重试循环。
    """


@dataclass
class AttemptBudget:
    """一次排名任务允许的 HTTP 尝试预算。

    Attributes:
        max_attempts: 允许的真实 HTTP 尝试次数上限（含重试与签名密钥请求）。
        deadline_monotonic: 绝对截止时刻（``time.monotonic`` 口径）。
        attempts: 已消费的尝试次数（初始为 0）。
    """

    max_attempts: int
    deadline_monotonic: float
    attempts: int = 0

    def before_send(self):
        """在真正发送 HTTP 之前记账并检查预算。

        Returns:
            无。

        Raises:
            RequestBudgetExceeded: 已过 deadline（deadline_exceeded）或已达尝试上限
                （http_attempt_limit）时抛出。
        """
        # 先判 deadline：即使次数还没用满，超时也应立即停止。
        if monotonic() >= self.deadline_monotonic:
            raise RequestBudgetExceeded('deadline_exceeded')
        if self.attempts >= self.max_attempts:
            raise RequestBudgetExceeded('http_attempt_limit')
        self.attempts += 1


#: 当前协程任务生效的 HTTP 尝试预算；默认 None -> 旧调用不做任何计数。
current_attempt_budget: ContextVar[AttemptBudget | None] = ContextVar(
    'current_attempt_budget', default=None
)


class RequestQuotaExceeded(RequestBudgetExceeded):
    """进程级滚动配额拒绝（规格 §2.2 / §2.6）。

    用 ``reason`` 区分判定结果：

    - ``category:<类别>``：该类别已用 >= 类别上限；
    - ``store_write_failed``：配额计数写库失败，按保守方向拒发；
    - ``quota_store_unavailable``：配额读取不可用，同样保守拒发。

    继承 ``RequestBudgetExceeded``，因此上层「原样抛出、不进重试」的现有处理
    对配额拒绝同样生效（与改造前一致，不新增异常分支）。

    Attributes:
        reason: 拒绝原因（见上）。
    """

    def __init__(self, reason: str):
        """记录拒绝原因。

        Args:
            reason: 拒绝原因字符串。
        """
        super().__init__(reason)
        self.reason = reason


class QuotaConfigError(ValueError):
    """``config/budget.yaml`` 配额账本自相矛盾（**加载期配置校验**失败）。

    与运行期的 ``RequestQuotaExceeded`` 严格区分：

    - ``QuotaConfigError``：账本**配错了**（部署问题），加载即抛，必须让人立刻看到；
    - ``RequestQuotaExceeded``：配额**用满了**（运行状态），按保守方向拒发。

    典型触发：各分类上限之和 > 总闸。总闸在这里的角色是**配置校验**，
    不是运行时限制器（详见 :func:`_validate_global_gate`）。
    """


@dataclass(frozen=True)
class QuotaLimits:
    """``config/budget.yaml`` 配额账本的只读快照（配额数字的唯一来源）。

    Attributes:
        global_limit: 单账号总闸（两域合计，attempt / 滚动 24h）。
            **只用于配置校验与用量观测**（见 ``_validate_global_gate`` / ``log_quota_usage``），
            **不是运行时限制器**——运行时判定只看 ``category_limits``。
        category_limits: 类别 -> 上限（运行时唯一的挡板）。
        category_domains: 类别 -> 归属域；用量汇总按它对全量 (域, 类别) 求和。
    """

    global_limit: int
    category_limits: Mapping[str, int]
    category_domains: Mapping[str, str]

    @property
    def pairs(self) -> Tuple[Tuple[str, str], ...]:
        """返回全量 ``(域, 类别)`` 组合，用于配额用量汇总。

        Returns:
            Tuple[Tuple[str, str], ...]: 账本声明的全部记账单元。
        """
        return tuple((self.category_domains[name], name) for name in self.category_limits)

    def limit_of(self, category: str) -> Optional[int]:
        """返回某类别的上限。

        Args:
            category: 配额类别。

        Returns:
            int 或 None（未在账本声明的类别不做分类拦截，但仍受总闸约束）。
        """
        return self.category_limits.get(category)


def _budget_config_path() -> Path:
    """定位 ``config/budget.yaml``（frozen 环境取 exe 同级目录，与 ConfigManager 同口径）。

    Returns:
        Path: 配额账本文件路径（不保证存在）。
    """
    if getattr(sys, 'frozen', False):
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parent.parent
    return base_dir / 'config' / 'budget.yaml'


@lru_cache(maxsize=1)
def load_quota_limits() -> Optional[QuotaLimits]:
    """读取 ``config/budget.yaml`` 的配额账本（进程内缓存），并做总闸配置校验。

    总闸是**配置校验**（账本自洽性），不是运行时限制器：加载时断言
    ``sum(各分类上限) <= 总闸``（见 :func:`_validate_global_gate`）。

    设计取舍：账本**缺失 / 字段非法**时返回 ``None``，即「没有配额上下文」——
    此时 ``before_http_attempt`` 保持不拦截，这是任务硬约束
    （既有调用行为必须原样保留）。错误原因写日志，不在这里抛异常打断业务。

    唯一例外：**总闸 < 分类合计**属于账本自相矛盾（部署事故），必须立刻暴露，
    因此 ``QuotaConfigError`` 会原样抛出，不再降级为「无配额上下文」。

    Returns:
        QuotaLimits，或 None（账本不可用）。

    Raises:
        QuotaConfigError: 各分类上限之和超过总闸（账本配错）。
    """
    path = _budget_config_path()
    try:
        with path.open('r', encoding='utf-8') as handle:
            raw = yaml.safe_load(handle) or {}
        quota = raw.get('quota') or {}
        categories = quota.get('categories') or {}
        category_limits: Dict[str, int] = {}
        category_domains: Dict[str, str] = {}
        for name, entry in categories.items():
            entry = entry or {}
            category_limits[str(name)] = int(entry['limit'])
            category_domains[str(name)] = str(entry['domain'])
        global_limit = int(quota['global_limit'])
        if global_limit <= 0 or not category_limits:
            raise ValueError(
                f'budget.yaml 配额非法: global_limit={global_limit} categories={category_limits}'
            )
        # 配置校验（总闸的真实角色）：分类合计不得超过总闸，越界即抛。
        _validate_global_gate(global_limit, category_limits)
        return QuotaLimits(
            global_limit=global_limit,
            category_limits=category_limits,
            category_domains=category_domains,
        )
    except QuotaConfigError as exc:
        # 账本自相矛盾：记日志后原样抛出，让部署方在首次加载时就看到问题。
        logger.error('配额总闸配置校验失败（%s）: %s', path, exc)
        raise
    except Exception as exc:  # noqa: BLE001 - 配置问题不打断业务，转为「无配额上下文」
        logger.error('读取配额账本失败（%s），不做配额拦截: %r', path, exc)
        return None


def _validate_global_gate(global_limit: int, category_limits: Mapping[str, int]) -> None:
    """配置校验：各分类上限之和必须 <= 总闸（账本自洽性）。

    总闸的**真实角色是配置校验，不是运行时限制器**：五类配额全是硬上限，
    各类封顶后合计天然够不到总闸，因此「运行时按总闸判定」在当前设计下是
    永远执行不到的死分支（已从 ``_enforce_quota`` 删除）。把总闸前移成
    加载期断言后，三种口径：

    - 合计 == 总闸（真实账本 480+1000+150+72+98 = 1800）：合法，正常加载；
    - 合计 < 总闸：合法（允许留安全垫），正常加载；
    - 合计 > 总闸：账本自相矛盾，加载即抛 ``QuotaConfigError``。

    Args:
        global_limit: 单账号总闸（两域合计，attempt / 滚动 24h）。
        category_limits: 类别 -> 类别上限。

    Returns:
        无（校验通过）。

    Raises:
        QuotaConfigError: 合计 > 总闸时抛出；报错列出每个分类的值、合计、
            总闸值与超出量，便于一眼看出超了多少。
    """
    total = sum(category_limits.values())
    if total > global_limit:
        breakdown = ' + '.join(f'{name}={limit}' for name, limit in category_limits.items())
        raise QuotaConfigError(
            f'budget.yaml 配额账本不合法：各分类上限合计 {total} 超过总闸 {global_limit}'
            f'（超出 {total - global_limit}）。'
            f'各项：{breakdown}；合计={total}；总闸(global_limit)={global_limit}。'
        )


def _enforce_quota(domain: str, category: str) -> None:
    """进程级配额判定：只按**分类硬上限**判定，通过才记账放行（规格 §2.2 / §2.6）。

    这里**不再判总闸**：总闸是配置校验（见 :func:`_validate_global_gate`），
    **不是运行时限制器**——五类封顶后合计够不到总闸，运行时判总闸是死分支。
    分类到顶照旧拒发；无配额上下文（``load_quota_limits`` 返回 ``None``）时
    ``before_http_attempt`` 直接不拦截。

    Args:
        domain: 凭证域（cookie / no_cookie）。
        category: 配额类别（discovery / watch / ranking / maintenance / flex）。

    Returns:
        无（通过时本次尝试已计入配额桶）。

    Raises:
        RequestQuotaExceeded: 分类到顶 / 计数写库失败时抛出。
    """
    from core import quota_store

    limits = load_quota_limits()
    if limits is None:
        # 没有配额上下文（账本不可用）：不拦截，保持既有行为。
        return

    now = int(time())

    try:
        # 只判分类硬上限：账本声明的类别才有挡板，未声明的类别不做分类拦截。
        # 总闸已前移为加载期配置校验，这里不再有运行时总闸分支。
        category_limit = limits.limit_of(category)
        if category_limit is not None and quota_store.used(domain, category, now) >= category_limit:
            raise RequestQuotaExceeded(f'category:{category}')
    except RequestQuotaExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - 读不可用同样按保守方向拒发，绝不放行
        logger.error('配额读取不可用，按保守方向拒发 domain=%s category=%s: %r', domain, category, exc)
        raise RequestQuotaExceeded('quota_store_unavailable') from exc

    # 都通过才写入计数；写库失败直接拒发，不放行（宁严不松）。
    try:
        quota_store.bump(domain, category, now)
    except Exception as exc:  # noqa: BLE001
        logger.error('配额计数写入失败，按保守方向拒发 domain=%s category=%s: %r', domain, category, exc)
        raise RequestQuotaExceeded('store_write_failed') from exc


def log_quota_usage(now: Optional[int] = None) -> Optional[str]:
    """按「每轮一行」输出各类别已用 / 上限（规格 §2.6 可观测性）。

    Args:
        now: 当前 epoch 秒；缺省取当前时刻。

    Returns:
        str: 该行日志文本；账本不可用或统计失败时返回 None（不抛异常、不阻断调度）。
    """
    limits = load_quota_limits()
    if limits is None:
        return None

    from core import quota_store

    moment = int(now if now is not None else time())
    try:
        used_by_unit = {
            (domain, category): quota_store.used(domain, category, moment)
            for domain, category in limits.pairs
        }
    except Exception as exc:  # noqa: BLE001 - 观测失败不影响调度
        logger.error('配额用量统计失败: %r', exc)
        return None

    parts = []
    for category, limit in limits.category_limits.items():
        domain = limits.category_domains[category]
        parts.append(f'{category}({domain})={used_by_unit[(domain, category)]}/{limit}')
    total = sum(used_by_unit.values())
    line = f'[配额] 合计={total}/{limits.global_limit} ' + ' '.join(parts)
    logger.info(line)
    return line


def before_http_attempt(domain: Optional[str] = None, category: Optional[str] = None):
    """HTTP 发送前钩子：任务内预算 → IP 级熔断 → 分域冷却 → 进程级滚动配额。

    判定顺序（规格 §2.4）：**先查冷却、再查配额**；熔断/冷却命中即拒发。

    Args:
        domain: 凭证域（cookie / no_cookie）；缺省 ``None``。
        category: 配额类别（discovery / watch / ...）；缺省 ``None``。

    Returns:
        无。

    Raises:
        RequestBudgetExceeded: 任务内预算耗尽时由 ``AttemptBudget.before_send`` 抛出。
        RequestQuotaExceeded: 熔断（``ip:circuit_open``）/ 冷却（``domain:cooling``）/
            状态不可用 / 进程级配额到顶时抛出。

    硬约束：
        **不传 domain / category 时（既有调用方）行为与改造前完全一致**——
        只做任务内预算计数，不做熔断 / 冷却 / 配额拦截、不写任何表。
        只传 ``domain`` 不传 ``category`` 时：做熔断与分域冷却判定，**不记账配额**
        （免凭证通道由调用方在 sources 层显式记账，避免同一次请求被计两次）。
    """
    budget = current_attempt_budget.get()
    if budget is not None:
        budget.before_send()
    # 完全没有域上下文（既有调用方）：整条链路一根不动，行为与改造前一致。
    if domain is None:
        return
    _enforce_risk_gate(domain)
    if category is None:
        return
    _enforce_quota(domain, category)


# --------------------------------------------------------------------------
# 风控兜底：IP 级熔断 + 分域冷却（规格 §2.4）
# --------------------------------------------------------------------------

#: 两域标识（与 ``config/budget.yaml`` 的 ``domains`` 段一致）。
DOMAINS: Tuple[str, ...] = ('cookie', 'no_cookie')

#: 参与风控统计的码：HTTP 412 / 403 与业务码 -412（请求被拦截）/ -352（风控校验失败）。
RISK_CODES: Tuple[int, ...] = (412, 403, -412, -352)

#: IP 级熔断的滑动窗长度（秒）与「每域最少事件数」。
#: 这两个阈值规格 §2.4 有口径但账本未落字段，且本轮改动范围不含 ``config/budget.yaml``
#: （既有测试断言该文件 ``domains`` / ``ip_circuit_breaker`` 的键集合固定），
#: 因此以模块常量声明——它们不是「配额数字」，不参与总量统计。
IP_BREAKER_WINDOW_S = 600
IP_BREAKER_MIN_EVENTS_PER_DOMAIN = 3

#: 「连续 N 次 412 起冷却翻倍」的阈值（规格 §2.4：15→30→60→120 分钟）。
CONSECUTIVE_DOUBLE_THRESHOLD = 3

#: 风控事件流水的保留时长（秒）：滑窗 10 分钟，留 1 小时足够回看。
RISK_EVENT_RETENTION_S = 3600

#: 账本不可用时的兜底冷却参数（宁严不松：绝不放行不等于放弃冷却）。
FALLBACK_COOLDOWN_BASE_S = 900
FALLBACK_COOLDOWN_MAX_S = 7200
FALLBACK_IP_CIRCUIT_S = 3600


@dataclass(frozen=True)
class CooldownLimits:
    """``config/budget.yaml`` 的冷却参数只读快照（冷却数字的唯一来源）。

    Attributes:
        domains: 域 -> 该域冷却参数（``cooldown_base_s`` / ``cooldown_max_s`` /
            ``min_interval_s`` / ``max_concurrency``）。
        ip_circuit_s: IP 级熔断的基准冷却秒数（规格：1 小时起）。
    """

    domains: Mapping[str, Mapping[str, int]]
    ip_circuit_s: int

    def base_of(self, domain: str) -> int:
        """返回某域的单次 412 基准冷却秒数（缺字段时用兜底值）。

        Args:
            domain: 凭证域。

        Returns:
            int: 基准冷却秒数。
        """
        entry = self.domains.get(domain) or {}
        return int(entry.get('cooldown_base_s', FALLBACK_COOLDOWN_BASE_S))

    def max_of(self, domain: str) -> int:
        """返回某域的冷却封顶秒数（缺字段时用兜底值）。

        Args:
            domain: 凭证域。

        Returns:
            int: 冷却封顶秒数。
        """
        entry = self.domains.get(domain) or {}
        return int(entry.get('cooldown_max_s', FALLBACK_COOLDOWN_MAX_S))


@lru_cache(maxsize=1)
def load_cooldown_limits() -> Optional[CooldownLimits]:
    """读取 ``config/budget.yaml`` 的 ``domains`` / ``ip_circuit_breaker`` 段（进程内缓存）。

    与 :func:`load_quota_limits` 同一取舍：账本缺失 / 字段非法时返回 ``None``，
    调用方退到兜底常量（保守方向），不在这里抛异常打断业务。

    Returns:
        CooldownLimits，或 None（冷却账本不可用）。
    """
    path = _budget_config_path()
    try:
        with path.open('r', encoding='utf-8') as handle:
            raw = yaml.safe_load(handle) or {}
        domains = raw.get('domains') or {}
        ip_circuit = raw.get('ip_circuit_breaker') or {}
        return CooldownLimits(
            domains={str(name): dict(entry or {}) for name, entry in domains.items()},
            ip_circuit_s=int(ip_circuit.get('cooldown_s', FALLBACK_IP_CIRCUIT_S)),
        )
    except Exception as exc:  # noqa: BLE001 - 冷却账本问题不打断业务，退兜底常量
        logger.error('读取冷却账本失败（%s），使用兜底冷却参数: %r', path, exc)
        return None


def _now_epoch(now: Optional[Union[int, float]] = None) -> int:
    """把「可选 epoch 秒」归一成 int。

    Args:
        now: 传入的 epoch 秒；``None`` 表示取当前时刻。

    Returns:
        int: epoch 秒。
    """
    return int(now if now is not None else time())


def _cooldown_bounds(domain: str) -> Tuple[int, int]:
    """返回某域的 ``(基准冷却秒, 封顶冷却秒)``。

    Args:
        domain: 凭证域。

    Returns:
        Tuple[int, int]: 冷却界限；账本不可用时退兜底常量。
    """
    limits = load_cooldown_limits()
    if limits is None:
        return FALLBACK_COOLDOWN_BASE_S, FALLBACK_COOLDOWN_MAX_S
    return limits.base_of(domain), limits.max_of(domain)


def _enforce_risk_gate(domain: str) -> None:
    """判定 IP 级熔断与分域冷却；命中即拒发（规格 §2.4）。

    Args:
        domain: 凭证域。

    Returns:
        无（通过时本次请求可继续走配额判定）。

    Raises:
        RequestQuotaExceeded: 熔断开启（``ip:circuit_open``）、该域冷却中
            （``domain:cooling``），或状态读取不可用（保守拒发）。
    """
    from core import quota_store

    try:
        circuit = quota_store.get_ip_circuit()
    except Exception as exc:  # noqa: BLE001 - 读失败按保守方向拒发，绝不放行
        logger.error('IP 级熔断状态读取不可用，按保守方向拒发: %r', exc)
        raise RequestQuotaExceeded('ip_circuit_unavailable') from exc
    if circuit is not None:
        # 熔断只能人工清除（见 clear_ip_circuit），不做「到期自动恢复」。
        raise RequestQuotaExceeded('ip:circuit_open')

    try:
        cooldown = quota_store.get_domain_cooldown(domain)
    except Exception as exc:  # noqa: BLE001 - 读失败按保守方向拒发
        logger.error('分域冷却状态读取不可用，按保守方向拒发 domain=%s: %r', domain, exc)
        raise RequestQuotaExceeded('domain_cooldown_unavailable') from exc
    if cooldown is not None and int(cooldown[0]) > _now_epoch():
        raise RequestQuotaExceeded('domain:cooling')


def _maybe_open_ip_circuit(moment: int) -> bool:
    """按滑动窗判定是否置开 IP 级熔断（两域各自 >= 3 次风控）。

    Args:
        moment: 当前 epoch 秒。

    Returns:
        bool: 本次是否触发了熔断。
    """
    from core import quota_store

    limits = load_cooldown_limits()
    circuit_s = limits.ip_circuit_s if limits is not None else FALLBACK_IP_CIRCUIT_S
    since = moment - IP_BREAKER_WINDOW_S
    hits = {
        domain: quota_store.count_risk_events(domain, since_epoch=since, codes=RISK_CODES)
        for domain in DOMAINS
    }
    if all(count >= IP_BREAKER_MIN_EVENTS_PER_DOMAIN for count in hits.values()):
        quota_store.open_ip_circuit(
            cooldown_until_epoch=moment + circuit_s,
            reason=(
                f'两域在 {IP_BREAKER_WINDOW_S}s 窗口内各自 >= '
                f'{IP_BREAKER_MIN_EVENTS_PER_DOMAIN} 次风控: {hits}'
            ),
            now=moment,
        )
        logger.error('IP 级熔断触发，全局停采（需人工清除）: %s', hits)
        return True
    return False


def _report_risk(domain: str, code: int, *, now: Optional[Union[int, float]] = None) -> None:
    """记录一次风控并推进该域冷却（内部统一实现）。

    Args:
        domain: 凭证域。
        code: 风控码（正数 HTTP 状态码 / 负数业务码）。
        now: 当前 epoch 秒；缺省取当前时刻。

    Returns:
        无。**上报失败只记日志、不抛异常**——调用点正在抛原始风控异常，
        不能被上报异常覆盖；写失败会让随后的冷却读取同样失败，从而按保守方向拒发。
    """
    from core import quota_store

    moment = _now_epoch(now)
    try:
        base, cap = _cooldown_bounds(domain)
        current = quota_store.get_domain_cooldown(domain)
        consecutive = (int(current[1]) if current is not None else 0) + 1
        # 连续 3 次起翻倍：15 -> 30 -> 60 -> 120（封顶 2 小时）。
        if consecutive >= CONSECUTIVE_DOUBLE_THRESHOLD:
            duration = min(base * (2 ** (consecutive - CONSECUTIVE_DOUBLE_THRESHOLD + 1)), cap)
        else:
            duration = base
        until = moment + int(duration)
        if current is not None:
            # 不缩短既有的恢复时刻（429 可能已把该域推到更晚）。
            until = max(until, int(current[0]))
        quota_store.set_domain_cooldown(
            domain, cooldown_until_epoch=until, consecutive_412=consecutive, now=moment
        )
        quota_store.record_risk_event(domain, int(code), moment)
        quota_store.prune_risk_events(before_epoch=moment - RISK_EVENT_RETENTION_S)
        _maybe_open_ip_circuit(moment)
    except Exception as exc:  # noqa: BLE001 - 上报失败不得覆盖原始风控异常
        logger.error('风控上报失败（不影响原始异常）domain=%s code=%s: %r', domain, code, exc)


def report_http_risk(
    domain: str,
    status_code: int,
    *,
    category: Optional[str] = None,
    now: Optional[Union[int, float]] = None,
) -> None:
    """上报一次 HTTP 层风控状态码（412 反爬拦截 / 403 拒绝）。

    Args:
        domain: 凭证域（由发起请求的客户端携带）。
        status_code: HTTP 状态码（412 / 403）。
        category: 配额类别（仅作日志可观测，不参与判定）。
        now: 当前 epoch 秒；缺省取当前时刻。

    Returns:
        无。
    """
    logger.warning('上报 HTTP 风控 %s domain=%s category=%s', status_code, domain, category)
    _report_risk(domain, int(status_code), now=now)


def report_http_412(domain: str, *, category: Optional[str] = None, now: Optional[Union[int, float]] = None) -> None:
    """上报一次 HTTP 412 反爬拦截（client 的 HTTP 412 分支调用）。

    Args:
        domain: 凭证域（由发起请求的客户端携带）。
        category: 配额类别（仅作日志可观测，不参与判定）。
        now: 当前 epoch 秒；缺省取当前时刻。

    Returns:
        无。
    """
    report_http_risk(domain, 412, category=category, now=now)


def report_business_risk_code(
    domain: str,
    code: int,
    *,
    category: Optional[str] = None,
    now: Optional[Union[int, float]] = None,
) -> None:
    """上报一次业务风控码（-352 风控校验失败 / -412 请求被拦截）。

    Args:
        domain: 凭证域。
        code: 业务码（-352 / -412，也容忍其它负码）。
        category: 配额类别（仅作日志可观测）。
        now: 当前 epoch 秒；缺省取当前时刻。

    Returns:
        无。
    """
    logger.warning('上报业务风控码 %s domain=%s category=%s', code, domain, category)
    _report_risk(domain, int(code), now=now)


def report_status_429(domain: str, retry_after_s: Union[int, float], *, now: Optional[Union[int, float]] = None) -> None:
    """上报一次 429：按恢复时刻取该域共享的较晚者（规格 §2.4）。

    429 **不动** ``consecutive_412``（它不是 412，不参与翻倍）。

    Args:
        domain: 凭证域。
        retry_after_s: 本次建议的等待秒数（响应头 ``Retry-After`` 或限频器给出）。
        now: 当前 epoch 秒；缺省取当前时刻。

    Returns:
        无；写失败只记日志。
    """
    from core import quota_store

    moment = _now_epoch(now)
    try:
        quota_store.extend_domain_cooldown(
            domain,
            cooldown_until_epoch=moment + max(0, int(retry_after_s)),
            now=moment,
        )
    except Exception as exc:  # noqa: BLE001 - 上报失败不得覆盖原始限流异常
        logger.error('429 上报失败 domain=%s retry_after=%s: %r', domain, retry_after_s, exc)


def report_http_success(domain: str, *, now: Optional[Union[int, float]] = None) -> bool:
    """上报一次成功请求；**仅当冷却期满后的首次成功**才清零连续 412 计数。

    规格 §2.4 硬约束：不得冷却一结束就重置，否则「连续三次翻倍」永不触发。

    Args:
        domain: 凭证域。
        now: 当前 epoch 秒；缺省取当前时刻。

    Returns:
        bool: 本次是否发生了清零。
    """
    from core import quota_store

    moment = _now_epoch(now)
    try:
        current = quota_store.get_domain_cooldown(domain)
        if current is None:
            return False
        until, consecutive = int(current[0]), int(current[1])
        if consecutive > 0 and moment >= until:
            quota_store.reset_domain_consecutive(domain, now=moment)
            logger.info('冷却期满后首次成功，清零连续 412 计数 domain=%s', domain)
            return True
        return False
    except Exception as exc:  # noqa: BLE001 - 清零失败不影响本次请求
        logger.error('连续 412 计数清零失败 domain=%s: %r', domain, exc)
        return False


def ip_circuit_open() -> bool:
    """查询 IP 级熔断是否处于开启状态（可观测 / 运维用）。

    Returns:
        bool: 熔断行存在即视为开启；读取失败按保守方向当作「开启」。
    """
    from core import quota_store

    try:
        return quota_store.get_ip_circuit() is not None
    except Exception as exc:  # noqa: BLE001
        logger.error('IP 级熔断状态查询失败，按保守方向视为开启: %r', exc)
        return True


def clear_ip_circuit() -> bool:
    """**人工确认后**清除 IP 级熔断（唯一解除途径，不自动恢复）。

    Returns:
        bool: 是否真的清掉了一行熔断状态。

    Raises:
        Exception: 数据库不可用时原样抛出（清除失败必须让人看到）。
    """
    from core import quota_store

    cleared = quota_store.clear_ip_circuit()
    if cleared:
        logger.warning('人工清除 IP 级熔断，全局停采解除')
    return cleared
