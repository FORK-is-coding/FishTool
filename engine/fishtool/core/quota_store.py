"""HTTP 尝试配额的小时桶持久化存储（规格 §2.6）。

职责边界（与 ``core/request_budget.py`` 同等级解耦）：

- 只依赖数据库（``core.database``），**不 import 任何业务模块**；
- 只做「记账 / 读取 / 裁剪」，**不做任何配额判定**（判定在 ``request_budget.py``）；
- 不持有进程内状态：所有计数都在 SQLite 里，进程重启后仍能读回，
  这正是「总闸不能靠 ContextVar」的落点。

公开接口分成两组（其余都是私有实现细节）：

一、滚动配额桶（规格 §2.6）：

- :func:`bump`  —— UPSERT 当前小时桶 ``count + 1``；
- :func:`used`  —— SUM 滚动窗口（24h）内该 (域, 类别) 的已用次数；
- :func:`prune` —— 删除保留窗口（25h）之前的桶。

二、风控状态（规格 §2.4，2026-10-02 批次 B/C 新增）：

- 分域冷却：:func:`get_domain_cooldown` / :func:`set_domain_cooldown` /
  :func:`extend_domain_cooldown`（429 取较晚者）/ :func:`reset_domain_consecutive`
  （冷却期满后首次成功才清零）；
- 风控事件流水：:func:`record_risk_event` / :func:`count_risk_events` /
  :func:`prune_risk_events`（IP 级熔断 10 分钟滑窗的统计来源）；
- IP 级熔断：:func:`get_ip_circuit` / :func:`open_ip_circuit` / :func:`clear_ip_circuit`
  （只由人工显式清除，不自动恢复）。

与第一组同一口径：**全部落 SQLite，进程重启不清零；只做记账 / 读取 / 裁剪，
不做任何判定**（判定在 ``request_budget.py``）。

窗口小时数来自 ``config/budget.yaml``（唯一账本），代码里不写死配额数字；
冷却时长同样来自该账本的 ``domains`` / ``ip_circuit_breaker`` 段。
"""
from __future__ import annotations

import logging
import sys
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Union

import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.database import HttpQuotaBucket, get_session
from core.database.models_quota import DomainCooldown, HttpRiskEvent, IpCircuitState

logger = logging.getLogger(__name__)

#: 小时桶宽度（秒）。桶编号 = epoch 秒 // HOUR_SECONDS。
HOUR_SECONDS = 3600

#: 计数表本体（UPSERT / DELETE 都以它为唯一目标）。
_TABLE = HttpQuotaBucket.__table__


def _budget_config_path() -> Path:
    """定位 ``config/budget.yaml``（frozen 环境取 exe 同级目录，与 ConfigManager 同口径）。

    Returns:
        配额账本文件的绝对路径（不保证存在）。
    """
    if getattr(sys, 'frozen', False):
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parent.parent
    return base_dir / 'config' / 'budget.yaml'


@lru_cache(maxsize=1)
def _window_params() -> Mapping[str, int]:
    """读取配额账本里的窗口参数（进程内缓存，缺文件时抛错而不猜数）。

    Returns:
        ``{'window_hours': 24, 'retention_hours': 25}``。

    Raises:
        Exception: 文件缺失 / YAML 非法 / 窗口参数不合法时抛出，
            调用方按「配额不可用」保守处理（绝不静默放行）。
    """
    path = _budget_config_path()
    with path.open('r', encoding='utf-8') as handle:
        raw = yaml.safe_load(handle) or {}
    quota = raw.get('quota') or {}
    window_hours = int(quota['window_hours'])
    retention_hours = int(quota['retention_hours'])
    # 保留窗口必须不小于统计窗口，否则会把还在统计期内的桶提前裁掉。
    if window_hours < 1 or retention_hours < window_hours:
        raise ValueError(
            f'budget.yaml 窗口参数非法: window_hours={window_hours} retention_hours={retention_hours}'
        )
    return {'window_hours': window_hours, 'retention_hours': retention_hours}


def _bucket_of(now: Union[int, float]) -> int:
    """把 epoch 秒折算成小时桶编号。

    Args:
        now: epoch 秒（int / float）。

    Returns:
        int: 小时桶编号。
    """
    return int(now) // HOUR_SECONDS


def _window_start(now: Union[int, float], hours: int) -> int:
    """按小时桶对齐计算滚动窗口的起始桶编号（含）。

    Args:
        now: epoch 秒。
        hours: 窗口长度（小时）。

    Returns:
        int: 起始桶编号 = 当前桶 - (hours - 1)，正好覆盖 ``hours`` 个桶。
    """
    return _bucket_of(now) - (hours - 1)


def bump(domain: str, category: str, now: Union[int, float]) -> None:
    """把「(域, 类别) × 当前小时桶」的计数 +1（UPSERT，同桶累加）。

    Args:
        domain: 凭证域，如 ``cookie`` / ``no_cookie``。
        category: 配额类别，如 ``discovery`` / ``watch``。
        now: 当前 epoch 秒。

    Returns:
        无。

    Raises:
        Exception: 任何数据库异常都原样抛出；调用方必须按「拒发」处理（宁严不松）。
    """
    session = get_session()
    try:
        statement = sqlite_insert(_TABLE).values(
            domain=str(domain),
            category=str(category),
            hour_bucket=_bucket_of(now),
            count=1,
        )
        # 命中主键 (domain, category, hour_bucket) 时累加，而不是新增一行。
        statement = statement.on_conflict_do_update(
            index_elements=['domain', 'category', 'hour_bucket'],
            set_={'count': _TABLE.c.count + 1},
        )
        session.execute(statement)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception('配额计数写入失败 domain=%s category=%s', domain, category)
        raise
    finally:
        session.close()


def used(domain: str, category: str, now: Union[int, float]) -> int:
    """读取滚动窗口内该 (域, 类别) 的已用尝试次数。

    Args:
        domain: 凭证域。
        category: 配额类别。
        now: 当前 epoch 秒。

    Returns:
        int: 最近 ``window_hours`` 个小时桶的 count 之和。

    Raises:
        Exception: 数据库或账本不可用时原样抛出；调用方按「配额不可用」保守处理。
    """
    start_bucket = _window_start(now, int(_window_params()['window_hours']))
    session = get_session()
    try:
        statement = select(func.coalesce(func.sum(_TABLE.c.count), 0)).where(
            _TABLE.c.domain == str(domain),
            _TABLE.c.category == str(category),
            _TABLE.c.hour_bucket >= start_bucket,
        )
        return int(session.execute(statement).scalar_one() or 0)
    finally:
        session.close()


def prune(now: Union[int, float]) -> int:
    """删除保留窗口（25 小时）之前的配额桶。

    Args:
        now: 当前 epoch 秒。

    Returns:
        int: 被删除的行数。

    Raises:
        Exception: 数据库或账本不可用时原样抛出；调度侧捕获后跳过本轮。
    """
    cutoff_bucket = _window_start(now, int(_window_params()['retention_hours']))
    session = get_session()
    try:
        result = session.execute(delete(_TABLE).where(_TABLE.c.hour_bucket < cutoff_bucket))
        session.commit()
        return int(result.rowcount or 0)
    except Exception:
        session.rollback()
        logger.exception('配额桶裁剪失败 cutoff_bucket=%s', cutoff_bucket)
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------
# 分域冷却（规格 §2.4）
# --------------------------------------------------------------------------

#: IP 级熔断状态表的单行键（该表只存一行）。
IP_CIRCUIT_KEY = 'ip'


def get_domain_cooldown(domain: str) -> Union[tuple, None]:
    """读取某域的冷却状态。

    Args:
        domain: 凭证域（``cookie`` / ``no_cookie``）。

    Returns:
        ``(cooldown_until_epoch, consecutive_412)``；该域无记录时返回 ``None``
        （表示「从未冷却」，等价于 ``(0, 0)``）。

    Raises:
        Exception: 数据库不可用时原样抛出；调用方按「保守方向」处理（视为仍在冷却）。
    """
    session = get_session()
    try:
        row = session.get(DomainCooldown, str(domain))
        if row is None:
            return None
        return (int(row.cooldown_until_epoch or 0), int(row.consecutive_412 or 0))
    finally:
        session.close()


def set_domain_cooldown(
    domain: str,
    *,
    cooldown_until_epoch: Union[int, float],
    consecutive_412: int,
    now: Union[int, float],
) -> None:
    """整体写入某域的冷却状态（UPSERT，主键 ``domain``）。

    Args:
        domain: 凭证域。
        cooldown_until_epoch: 冷却截止时刻（epoch 秒）。
        consecutive_412: 连续 412 次数。
        now: 当前 epoch 秒（写 ``updated_epoch``）。

    Returns:
        无。

    Raises:
        Exception: 任何数据库异常都原样抛出（调用方需自行决定是否降级）。
    """
    session = get_session()
    try:
        values = {
            'domain': str(domain),
            'cooldown_until_epoch': int(cooldown_until_epoch),
            'consecutive_412': int(consecutive_412),
            'updated_epoch': int(now),
        }
        statement = sqlite_insert(DomainCooldown.__table__).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=['domain'],
            set_={
                'cooldown_until_epoch': values['cooldown_until_epoch'],
                'consecutive_412': values['consecutive_412'],
                'updated_epoch': values['updated_epoch'],
            },
        )
        session.execute(statement)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception('分域冷却写入失败 domain=%s', domain)
        raise
    finally:
        session.close()


def extend_domain_cooldown(domain: str, *, cooldown_until_epoch: Union[int, float], now: Union[int, float]) -> int:
    """把某域冷却截止时刻「取较晚者」（429 共享恢复时刻口径）。

    只动 ``cooldown_until_epoch``，**不动** ``consecutive_412``——429 不是 412，
    不参与「连续 3 次翻倍」的计数。

    Args:
        domain: 凭证域。
        cooldown_until_epoch: 本次建议的恢复时刻（epoch 秒）。
        now: 当前 epoch 秒。

    Returns:
        int: 取较晚者之后的最终截止时刻。

    Raises:
        Exception: 数据库不可用时原样抛出。
    """
    current = get_domain_cooldown(domain)
    until = int(cooldown_until_epoch)
    consecutive = 0
    if current is not None:
        until = max(until, int(current[0]))
        consecutive = int(current[1])
    set_domain_cooldown(domain, cooldown_until_epoch=until, consecutive_412=consecutive, now=now)
    return until


def reset_domain_consecutive(domain: str, *, now: Union[int, float]) -> bool:
    """把某域的「连续 412 次数」清零（只保留冷却截止时刻不变）。

    调用方**必须**先确认「冷却已期满且本次请求成功」，否则会出现
    「冷却一结束就清零」的漏洞（连续三次翻倍永远触发不到）。

    Args:
        domain: 凭证域。
        now: 当前 epoch 秒。

    Returns:
        bool: 本次是否真的发生了清零（无记录 / 已是 0 时返回 ``False``）。

    Raises:
        Exception: 数据库不可用时原样抛出。
    """
    current = get_domain_cooldown(domain)
    if current is None or int(current[1]) <= 0:
        return False
    set_domain_cooldown(domain, cooldown_until_epoch=int(current[0]), consecutive_412=0, now=now)
    return True


# --------------------------------------------------------------------------
# 风控事件流水（IP 级熔断滑窗依据，规格 §2.4）
# --------------------------------------------------------------------------

def record_risk_event(domain: str, status_code: int, now: Union[int, float]) -> None:
    """记录一次风控事件（HTTP 412/403 或业务码 -412/-352）。

    Args:
        domain: 凭证域。
        status_code: 风控码（正数为 HTTP 状态码，负数为 B 站业务码）。
        now: 发生时刻（epoch 秒）。

    Returns:
        无。

    Raises:
        Exception: 数据库不可用时原样抛出。
    """
    session = get_session()
    try:
        session.add(HttpRiskEvent(domain=str(domain), status_code=int(status_code), epoch=int(now)))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception('风控事件写入失败 domain=%s status_code=%s', domain, status_code)
        raise
    finally:
        session.close()


def count_risk_events(domain: str, *, since_epoch: Union[int, float], codes: tuple) -> int:
    """统计某域在滑窗内的风控事件次数（按码过滤）。

    Args:
        domain: 凭证域。
        since_epoch: 滑窗起点（含）。
        codes: 参与统计的风控码集合。

    Returns:
        int: 命中条数。

    Raises:
        Exception: 数据库不可用时原样抛出。
    """
    session = get_session()
    try:
        statement = select(func.count()).select_from(HttpRiskEvent.__table__).where(
            HttpRiskEvent.__table__.c.domain == str(domain),
            HttpRiskEvent.__table__.c.epoch >= int(since_epoch),
            HttpRiskEvent.__table__.c.status_code.in_(tuple(int(code) for code in codes)),
        )
        return int(session.execute(statement).scalar_one() or 0)
    finally:
        session.close()


def prune_risk_events(*, before_epoch: Union[int, float]) -> int:
    """删除滑窗之前的旧风控事件（避免流水无限增长）。

    Args:
        before_epoch: 早于此时刻（不含）的事件被删除。

    Returns:
        int: 被删除的行数。

    Raises:
        Exception: 数据库不可用时原样抛出。
    """
    session = get_session()
    try:
        result = session.execute(
            delete(HttpRiskEvent.__table__).where(HttpRiskEvent.__table__.c.epoch < int(before_epoch))
        )
        session.commit()
        return int(result.rowcount or 0)
    except Exception:
        session.rollback()
        logger.exception('风控事件裁剪失败 before_epoch=%s', before_epoch)
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------
# IP 级熔断（规格 §2.4）
# --------------------------------------------------------------------------

def get_ip_circuit() -> Union[dict, None]:
    """读取 IP 级熔断状态。

    Returns:
        ``{'opened_epoch', 'cooldown_until_epoch', 'reason', 'updated_epoch'}``；
        从未熔断过时返回 ``None``。

    Raises:
        Exception: 数据库不可用时原样抛出。
    """
    session = get_session()
    try:
        row = session.get(IpCircuitState, IP_CIRCUIT_KEY)
        if row is None:
            return None
        return {
            'opened_epoch': int(row.opened_epoch or 0),
            'cooldown_until_epoch': int(row.cooldown_until_epoch or 0),
            'reason': str(row.reason or ''),
            'updated_epoch': int(row.updated_epoch or 0),
        }
    finally:
        session.close()


def open_ip_circuit(*, cooldown_until_epoch: Union[int, float], reason: str, now: Union[int, float]) -> None:
    """置开 IP 级熔断（UPSERT 单行）。

    Args:
        cooldown_until_epoch: 最早可恢复时刻（仅供参考，实际须人工清除）。
        reason: 触发原因（可观测）。
        now: 触发时刻（epoch 秒）。

    Returns:
        无。

    Raises:
        Exception: 数据库不可用时原样抛出。
    """
    session = get_session()
    try:
        values = {
            'key': IP_CIRCUIT_KEY,
            'opened_epoch': int(now),
            'cooldown_until_epoch': int(cooldown_until_epoch),
            'reason': str(reason)[:200],
            'updated_epoch': int(now),
        }
        statement = sqlite_insert(IpCircuitState.__table__).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=['key'],
            set_={
                'opened_epoch': values['opened_epoch'],
                'cooldown_until_epoch': values['cooldown_until_epoch'],
                'reason': values['reason'],
                'updated_epoch': values['updated_epoch'],
            },
        )
        session.execute(statement)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception('IP 级熔断状态写入失败 reason=%s', reason)
        raise
    finally:
        session.close()


def clear_ip_circuit() -> bool:
    """人工清除 IP 级熔断（唯一解除途径）。

    Returns:
        bool: 是否存在并删除了熔断行。

    Raises:
        Exception: 数据库不可用时原样抛出。
    """
    session = get_session()
    try:
        result = session.execute(
            delete(IpCircuitState.__table__).where(IpCircuitState.__table__.c.key == IP_CIRCUIT_KEY)
        )
        session.commit()
        return bool(result.rowcount)
    except Exception:
        session.rollback()
        logger.exception('IP 级熔断状态清除失败')
        raise
    finally:
        session.close()
