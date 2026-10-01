"""HTTP 尝试配额的小时桶持久化存储（规格 §2.6）。

职责边界（与 ``core/request_budget.py`` 同等级解耦）：

- 只依赖数据库（``core.database``），**不 import 任何业务模块**；
- 只做「记账 / 读取 / 裁剪」，**不做任何配额判定**（判定在 ``request_budget.py``）；
- 不持有进程内状态：所有计数都在 SQLite 里，进程重启后仍能读回，
  这正是「总闸不能靠 ContextVar」的落点。

公开接口只有三个（其余都是私有实现细节）：

- :func:`bump`  —— UPSERT 当前小时桶 ``count + 1``；
- :func:`used`  —— SUM 滚动窗口（24h）内该 (域, 类别) 的已用次数；
- :func:`prune` —— 删除保留窗口（25h）之前的桶。

窗口小时数来自 ``config/budget.yaml``（唯一账本），代码里不写死配额数字。
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
