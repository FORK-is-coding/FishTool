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
from typing import Dict, Mapping, Optional, Tuple

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
    """HTTP 发送前钩子：先任务内预算，再进程级滚动配额。

    Args:
        domain: 凭证域（cookie / no_cookie）；缺省 ``None``。
        category: 配额类别（discovery / watch / ...）；缺省 ``None``。

    Returns:
        无。

    Raises:
        RequestBudgetExceeded: 任务内预算耗尽时由 ``AttemptBudget.before_send`` 抛出。
        RequestQuotaExceeded: 进程级配额到顶或配额计数不可用时抛出。

    硬约束：
        **不传 domain / category 时（既有调用方）行为与改造前完全一致**——
        只做任务内预算计数，不做配额拦截、不写配额表。
    """
    budget = current_attempt_budget.get()
    if budget is not None:
        budget.before_send()
    # 没有配额上下文（未声明域 / 类别）时不拦截：既有调用行为原样保留。
    if domain is None or category is None:
        return
    _enforce_quota(domain, category)
