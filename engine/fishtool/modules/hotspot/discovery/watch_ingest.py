"""06 采集广度 · 桥接层：discovery 视频候选 → 02 watch 池（本批只做入池）。

职责单一
--------
只做一件事：把一轮 ``DiscoveryService.iter_video_candidates()`` 的**内存**候选按口径筛出来，
逐条调 ``watch_store.upsert_watch`` 入池，返回入池条数。

不发任何 HTTP（硬要求，06 §7 + 前置方案 §2.2）
---------------------------------------------
入池只吃上一轮 poll 的内存结果；「缓存 TTL = 该入口轮询间隔（10 / 30 分钟）」的陈旧度由
06 §7 明确接受。若某次确实需要更新的数，走 02 自身的详情采样，**不得**为了「更新鲜」绕开
共享缓存再打一次入口。本模块因此不 import 任何网络 / 客户端符号，也不调用任何 ``*_store``
之外的 I/O。

依赖方向（单向，防循环）
------------------------
    watch_ingest  →  discovery.contracts（来源常量）
    watch_ingest  →  modules.hotspot.watch_store（upsert_watch）

``discovery.service`` 与 ``watch_store`` **都不** import 本模块（见 06 批 1 红线）。

入池口径（06 §7「popular / ranking?rid=0 的条目作为 watch 候选」）
-----------------------------------------------------------------
- ``item["sources"]`` 含 ``popular`` 或 ``ranking_all``  → **入池**；
- ``ranking_all_others``（扩散线索，交 04）/ ``search_square``（热搜词）→ **不入池**。

调度口径
--------
``next_due_epoch_s`` / ``ttl_end_epoch_s`` / ``sample_interval_s`` 一律走 ``upsert_watch`` 的
缺省值（``now + sample_interval_s`` 等），本模块**不自己算偏移**；``discovery_source`` 填
候选自带的 ``display_source``（合并层已按固定优先级取好展示来源）。分类字段
（``legacy_tid`` / ``tidv2`` / ``category_key``）本批**不自行映射**：三套分类字段分属不同层级，
混映射会出错，一律留 None 由后续专门任务处理。

事务口径
--------
与 ``watch_store`` 保持一致：本模块只 ``flush``（经 ``upsert_watch``），
**不** commit / rollback / close；提交由调用方（``core.monitor_service`` 的装配段）负责。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from modules.hotspot.watch_store import upsert_watch

from .contracts import SOURCE_POPULAR, SOURCE_RANKING_ALL

logger = logging.getLogger(__name__)

#: 允许入 watch 池的来源集合（口径见 06 §7）。
#: ``ranking_all_others`` 是「同 UP 其他上榜作品」的扩散线索（给 04），不当 watch 候选；
#: ``search_square`` 是热搜词（不是视频候选），同样不入池。
WATCH_INGEST_SOURCES: frozenset = frozenset({SOURCE_POPULAR, SOURCE_RANKING_ALL})

__all__ = [
    'WATCH_INGEST_SOURCES',
    'is_watch_candidate',
    'ingest_video_candidates_to_watch',
]


def _as_candidate_list(candidates: Optional[Iterable[Dict[str, Any]]]) -> List[Any]:
    """把入参容错规整成列表（``None`` / 单条 / 任意可迭代都接得住）。

    Args:
        candidates: 候选集合；允许 None、单条 dict 或任意可迭代对象。

    Returns:
        List[Any]: 规整后的候选列表；不可迭代时返回空列表（不抛异常）。
    """
    if candidates is None:
        return []
    # 单条 dict 视为「一条候选」，避免被当成「key 可迭代」逐字符拆开。
    if isinstance(candidates, dict):
        return [candidates]
    try:
        return list(candidates)
    except TypeError:
        logger.warning("视频候选不是可迭代对象，按零候选处理: %r", type(candidates))
        return []


def is_watch_candidate(candidate: Any) -> bool:
    """判断一条合并候选是否命中 watch 入池口径。

    口径：``candidate["sources"]`` 里含 ``popular`` 或 ``ranking_all`` 即入池
    （多来源合并后 ``sources`` 全留，取并集判定）。

    Args:
        candidate: 一条 discovery 合并候选（``merge_video_candidates`` 产出的 dict）。

    Returns:
        bool: True 表示应入池。
    """
    if not isinstance(candidate, dict):
        return False
    sources = candidate.get("sources")
    if not isinstance(sources, (list, tuple, set, frozenset)):
        return False
    return any(source in WATCH_INGEST_SOURCES for source in sources)


def ingest_video_candidates_to_watch(
    session: Session,
    candidates: Optional[Iterable[Dict[str, Any]]],
    *,
    now_epoch_s: int,
) -> int:
    """把 discovery 视频候选按口径入 watch 池（**只吃内存结果，不发任何 HTTP**）。

    逐条判定来源 -> 调 ``upsert_watch``（幂等：同 bvid 只留一行，重复只更新 last_seen 与元信息，
    不重置 ``next_due_epoch_s`` / 不清 ``state_json`` / 不挪 ttl / 不覆盖 ``first_seen_epoch_s``）。

    Args:
        session: 调用方持有的 SQLAlchemy 会话；本函数只 flush，**不** commit / rollback / close。
        candidates: discovery 合并候选列表；``None`` / 空 / 全是不入池来源时返回 0，不抛异常。
        now_epoch_s: 本次入池时刻（UTC 秒）；非法时抛 ``ValueError``（不静默当 0 用）。

    Returns:
        int: 实际入池（成功 upsert）的条数。

    Raises:
        ValueError: ``now_epoch_s`` 不是合法秒级 UTC 时间戳。
    """
    # 时钟先行校验：非法时钟一律显式报错，避免被下面逐条的容错吞成「0 条入池」。
    if isinstance(now_epoch_s, bool) or not isinstance(now_epoch_s, int) or now_epoch_s < 0:
        raise ValueError("invalid_now_epoch_s")

    written = 0
    for candidate in _as_candidate_list(candidates):
        if not is_watch_candidate(candidate):
            # 含 ranking_all_others / search_square 的候选在此被拦下，不计入入池数。
            continue
        bvid = str(candidate.get("bvid") or "").strip()
        if not bvid:
            # 缺身份键的候选：记日志跳过，不让一条脏数据炸掉整批入池。
            logger.warning("候选缺少 bvid，跳过入池: %r", candidate)
            continue
        try:
            upsert_watch(
                session,
                bvid=bvid,
                now_epoch_s=now_epoch_s,
                # 展示来源由合并层按固定优先级定好；缺省 None 时 upsert 的 coalesce 保留旧值。
                discovery_source=candidate.get("display_source"),
            )
        except ValueError as exc:
            # upsert_watch 的校验失败（bvid / 时间戳非法）：单条跳过并留痕，其余继续。
            logger.warning("候选入池被拒（bvid=%s）: %s", bvid, exc)
            continue
        written += 1
    return written
