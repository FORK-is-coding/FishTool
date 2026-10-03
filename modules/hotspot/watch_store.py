"""热点单视频跟踪（``hotspot_watch``）的持久化服务（FishTool 02 · 批 2）。

职责（对齐 02 方案 §8 / §0 裁定一·二·三 与前置方案 §3.2）：
- **幂等入库**：同一 bvid 重复入库只留一行（``bvid`` UNIQUE + SQLite UPSERT）；
- **三态判定**：跟踪中 / 已到期 / 已释放（态名照方案，不自创）；
- **调度查询**：捞出「该评估的 watch」；
- **清理查询**：捞出「该清理的过期行」，并提供原子释放；
- **fencing 写回**：``commit_state`` 带 ``state_revision`` 校验，旧代际写入丢弃。

事务口径：本模块只 ``flush``，**不** commit / rollback / close，事务由调用方持有
（与同层 ``modules/hotspot/snapshot_store.py`` 的 flush-only 约定一致）。所有公开函数
都接收调用方传入的 session，不在内部隐式 ``get_session()``，便于单测注入临时库。
"""
from __future__ import annotations

from enum import Enum

from sqlalchemy import bindparam, func, inspect as sa_inspect, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from core.database import HotspotWatch

from .algorithm.lifecycle_v2 import CoverageState

#: 默认采样间隔（秒）：02 §8.3「sample_interval_s 默认 3600」。
DEFAULT_SAMPLE_INTERVAL_S: int = 3600

#: 默认入池 TTL（秒）：02 §8「加入时给 14 天追踪 deadline」。
DEFAULT_TTL_S: int = 14 * 86400

#: coverage_state 合法取值；唯一来源是算法层 ``CoverageState``，不在此另造态名。
_COVERAGE_VALUES: frozenset = frozenset(state.value for state in CoverageState)


class WatchState(str, Enum):
    """跟踪三态（前置方案 §3.2「跟踪三态」，条件逐字实现，不自创态名）。

    - ``TRACKING`` 跟踪中：``active=1 AND ttl_end_epoch_s > now`` —— 参与调度；
    - ``EXPIRED``  已到期：``active=1 AND ttl_end_epoch_s <= now`` —— 等清理查询处理，不再发请求；
    - ``RELEASED`` 已释放：``active=0`` —— 归档，让出池子名额。
    """

    TRACKING = "tracking"
    EXPIRED = "expired"
    RELEASED = "released"


def _require_epoch(value, code: str) -> int:
    """校验秒级 UTC epoch：必须是 int（显式排除 bool）且非负。

    Args:
        value: 待校验值。
        code: 失败时抛出的稳定错误码。

    Returns:
        校验通过的 int 时间戳。

    Raises:
        ValueError: 非 int、bool 或为负时抛出 ``code``。
    """
    if type(value) is not int or value < 0:
        raise ValueError(code)
    return value


def classify_state(row: HotspotWatch, now_epoch_s: int) -> WatchState:
    """判定某行的跟踪三态（前置方案 §3.2 的条件，逐字实现）。

    Args:
        row: ``hotspot_watch`` 行。
        now_epoch_s: 判定时刻（UTC 秒）。

    Returns:
        ``WatchState`` 三态之一。
    """
    if not bool(row.active):
        return WatchState.RELEASED
    if int(row.ttl_end_epoch_s) > int(now_epoch_s):
        return WatchState.TRACKING
    return WatchState.EXPIRED


def validate_coverage(coverage_ratio, coverage_state) -> None:
    """校验 coverage 两级：要存就两个都在，且取值合法（02 §0 裁定三）。

    Args:
        coverage_ratio: 覆盖比例数值，或 None。
        coverage_state: 覆盖级别枚举值，或 None。

    Returns:
        无。

    Raises:
        ValueError: 只给其一，或 ratio 越界（0~1）/ state 非 ``CoverageState`` 取值。
    """
    has_ratio = coverage_ratio is not None
    has_state = coverage_state is not None
    if has_ratio != has_state:
        raise ValueError("coverage_pair_incomplete")
    if has_ratio:
        if isinstance(coverage_ratio, bool) or not 0.0 <= float(coverage_ratio) <= 1.0:
            raise ValueError("invalid_coverage_ratio")
        if coverage_state not in _COVERAGE_VALUES:
            raise ValueError("invalid_coverage_state")


def upsert_watch(
    session: Session,
    *,
    bvid: str,
    now_epoch_s: int,
    ttl_end_epoch_s: int | None = None,
    category_key: str | None = None,
    collection_tid: int | None = None,
    discovery_source: str | None = None,
    sample_interval_s: int = DEFAULT_SAMPLE_INTERVAL_S,
    next_due_epoch_s: int | None = None,
) -> HotspotWatch:
    """幂等入库：同一 bvid 只留一行，重复调用结果一致。

    行为对齐 02 §7.3 第 3 条：重复发现只更新 ``last_seen_epoch_s`` 与元信息，
    **不**重置 ``next_due_epoch_s``、不清 ``state_json`` / ``state_revision``、
    不挪 ``ttl_end_epoch_s``、不覆盖 ``first_seen_epoch_s``；元信息用
    ``coalesce(excluded, 旧值)`` 保证「传 None 不抹掉已有值」。

    Args:
        session: 调用方持有的 SQLAlchemy 会话；本函数只 flush。
        bvid: 视频 BV 号（唯一目标）。
        now_epoch_s: 本次发现时刻（UTC 秒）。
        ttl_end_epoch_s: 本档到期时刻；None 时按 ``now + DEFAULT_TTL_S``。
        category_key / collection_tid / discovery_source: 元信息，可为 None。
        sample_interval_s: 采样间隔（秒），默认 3600。
        next_due_epoch_s: 首采的下次应采样时刻；None 时按 ``now + sample_interval_s``。

    Returns:
        入库后的 ``HotspotWatch`` 行（已 flush，可由调用方读取）。

    Raises:
        ValueError: bvid 空 / 超长，或时间戳非法。
    """
    clean_bvid = str(bvid or "").strip()
    if not clean_bvid or len(clean_bvid) > 20:
        raise ValueError("invalid_bvid")
    now_epoch_s = _require_epoch(now_epoch_s, "invalid_now_epoch_s")
    sample_interval_s = _require_epoch(sample_interval_s, "invalid_sample_interval_s")
    ttl_end_epoch_s = (
        now_epoch_s + DEFAULT_TTL_S
        if ttl_end_epoch_s is None
        else _require_epoch(ttl_end_epoch_s, "invalid_ttl_end_epoch_s")
    )
    next_due_epoch_s = (
        now_epoch_s + sample_interval_s
        if next_due_epoch_s is None
        else _require_epoch(next_due_epoch_s, "invalid_next_due_epoch_s")
    )

    stmt = sqlite_insert(HotspotWatch).values(
        bvid=clean_bvid,
        category_key=category_key,
        collection_tid=collection_tid,
        discovery_source=discovery_source,
        active=True,
        first_seen_epoch_s=now_epoch_s,
        last_seen_epoch_s=now_epoch_s,
        ttl_end_epoch_s=ttl_end_epoch_s,
        next_due_epoch_s=next_due_epoch_s,
        failure_count=0,
        sample_interval_s=sample_interval_s,
        state_revision=0,
    )
    # 唯一约束冲突时改为「只更新 last_seen + 元信息」，调度 / 评估列一律不动。
    stmt = stmt.on_conflict_do_update(
        index_elements=[HotspotWatch.bvid],
        set_={
            "last_seen_epoch_s": stmt.excluded.last_seen_epoch_s,
            "category_key": func.coalesce(stmt.excluded.category_key, HotspotWatch.category_key),
            "collection_tid": func.coalesce(stmt.excluded.collection_tid, HotspotWatch.collection_tid),
            "discovery_source": func.coalesce(stmt.excluded.discovery_source, HotspotWatch.discovery_source),
        },
    )
    session.execute(stmt)
    session.flush()
    return session.query(HotspotWatch).filter(HotspotWatch.bvid == clean_bvid).one()


def count_active_watch(session: Session) -> int:
    """统计当前在池（``active=1``）的行数（准入容量闸门用）。

    Args:
        session: 调用方会话。

    Returns:
        int: ``active=1`` 的行数。
    """
    return int(
        session.query(func.count())
        .select_from(HotspotWatch)
        .filter(HotspotWatch.active.is_(True))
        .scalar()
        or 0
    )


def reactivate_watch(
    session: Session,
    bvid: str,
    *,
    now_epoch_s: int,
    next_due_epoch_s: int | None = None,
    sample_interval_s: int | None = None,
) -> bool:
    """把已释放（非 manual_stop）的行重新放回池并排程（准入放行用）。

    只写调度 / 生命周期列，**不碰** ``state_json`` / ``last_confirmed_stage`` /
    ``coverage_*`` / ``last_evaluation_epoch_s`` 等评估历史（连续窗状态必须保留）；
    按 02 §0 裁定一，改变调度状态即同事务 ``state_revision + 1``。

    Args:
        session: 调用方会话；只 flush。
        bvid: 目标 BV 号。
        now_epoch_s: 本次放行时刻（UTC 秒）。
        next_due_epoch_s: 下次应采样时刻；None 时按 ``now + interval``。
        sample_interval_s: 采样间隔；None 时保持原值（若非法则回退默认）。

    Returns:
        bool: 命中该 bvid 返回 True（rowcount==1），否则 False。

    Raises:
        ValueError: bvid 为空或时间戳非法。
    """
    clean_bvid = str(bvid or "").strip()
    if not clean_bvid:
        raise ValueError("invalid_bvid")
    now_epoch_s = _require_epoch(now_epoch_s, "invalid_now_epoch_s")
    values: dict = {
        "active": True,
        "stop_reason": None,
        "released_epoch_s": None,
        "last_seen_epoch_s": now_epoch_s,
        "state_revision": HotspotWatch.state_revision + 1,
    }
    if sample_interval_s is not None:
        values["sample_interval_s"] = _require_epoch(sample_interval_s, "invalid_sample_interval_s")
    if next_due_epoch_s is not None:
        values["next_due_epoch_s"] = _require_epoch(next_due_epoch_s, "invalid_next_due_epoch_s")
    result = session.execute(
        update(HotspotWatch).where(HotspotWatch.bvid == clean_bvid).values(**values)
    )
    session.flush()
    return int(result.rowcount or 0) == 1


def release_watch(
    session: Session,
    bvid: str,
    *,
    now_epoch_s: int,
    stop_reason: str,
) -> bool:
    """把在池行置为「已释放」（``active=0``），保留全部历史。

    用于「需求全撤 / 启动清理」场景：停采但**不删**任何评估历史、快照或其它列。
    同时把 ``state_revision + 1``：释放改变调度状态，借此 fence 掉释放前领取、释放后
    才回来的迟到写入（第二批 C）。

    Args:
        session: 调用方会话；只 flush。
        bvid: 目标 BV 号。
        now_epoch_s: 释放时刻（UTC 秒）。
        stop_reason: 释放原因码（如 ``events_revoked``）。

    Returns:
        bool: 命中该 bvid 返回 True。

    Raises:
        ValueError: bvid 为空或时间戳非法。
    """
    clean_bvid = str(bvid or "").strip()
    if not clean_bvid:
        raise ValueError("invalid_bvid")
    now_epoch_s = _require_epoch(now_epoch_s, "invalid_now_epoch_s")
    result = session.execute(
        update(HotspotWatch)
        .where(HotspotWatch.bvid == clean_bvid, HotspotWatch.active.is_(True))
        .values(
            active=False,
            stop_reason=str(stop_reason or "released")[:32],
            released_epoch_s=now_epoch_s,
            state_revision=HotspotWatch.state_revision + 1,
        )
    )
    session.flush()
    return int(result.rowcount or 0) == 1


def reschedule_watch(
    session: Session,
    bvid: str,
    *,
    now_epoch_s: int,
    sample_interval_s: int,
    next_due_epoch_s: int | None = None,
) -> bool:
    """按新的需求节奏重排某行（只改调度列 + 代际，不动评估历史）。

    用于「撤销某命名空间后重算 ``next_due_epoch_s``」（第二批 B）：清除 events 需求但仍有
    manual / ranking 需求时，退回其原节奏。**不碰** ``state_json`` / 快照 / 其它命名空间需求。

    Args:
        session: 调用方会话；只 flush。
        bvid: 目标 BV 号。
        now_epoch_s: 重算时刻（UTC 秒）。
        sample_interval_s: 新的采样间隔（正整数秒）。
        next_due_epoch_s: 新的下次应采样时刻；None 时按 ``now + interval``。

    Returns:
        bool: 命中该 bvid 返回 True。

    Raises:
        ValueError: bvid 为空或时间戳非法。
    """
    clean_bvid = str(bvid or "").strip()
    if not clean_bvid:
        raise ValueError("invalid_bvid")
    now_epoch_s = _require_epoch(now_epoch_s, "invalid_now_epoch_s")
    interval = _require_epoch(sample_interval_s, "invalid_sample_interval_s")
    if interval <= 0:
        raise ValueError("invalid_sample_interval_s")
    due = (
        now_epoch_s + interval
        if next_due_epoch_s is None
        else _require_epoch(next_due_epoch_s, "invalid_next_due_epoch_s")
    )
    result = session.execute(
        update(HotspotWatch)
        .where(HotspotWatch.bvid == clean_bvid)
        .values(
            sample_interval_s=interval,
            next_due_epoch_s=due,
            state_revision=HotspotWatch.state_revision + 1,
        )
    )
    session.flush()
    return int(result.rowcount or 0) == 1


def load_fast_until_map(session: Session, bvids: list) -> dict:
    """读出给定 bvid 的 ``fast_until_s``（04 迁移补的列，ORM 模型未声明）。

    列由 ``_migrate_hotspot_watch_event_columns`` 幂等补齐；在未跑迁移的库（如仅用
    ``Base.metadata.create_all`` 的测试库）上该列不存在，此时**优雅降级**为
    ``{}``（一律按普通节奏处理），绝不因缺列炸整轮。

    Args:
        session: 调用方会话。
        bvids: 待查询的 bvid 列表。

    Returns:
        dict: ``bvid -> fast_until_s (int 或 None)``；缺列时为空 dict。
    """
    wanted = [str(b).strip() for b in bvids if str(b).strip()]
    if not wanted:
        return {}
    try:
        if not sa_inspect(session.get_bind()).has_table("hotspot_watch"):
            return {}
        columns = {col["name"] for col in sa_inspect(session.get_bind()).get_columns("hotspot_watch")}
        if "fast_until_s" not in columns:
            return {}
        stmt = text(
            "SELECT bvid, fast_until_s FROM hotspot_watch WHERE bvid IN :bvids"
        ).bindparams(bindparam("bvids", expanding=True))
        rows = session.execute(stmt, {"bvids": wanted}).all()
    except Exception:  # noqa: BLE001 - 缺列 / 旧库一律降级为「无快采」，不炸整轮
        return {}
    return {str(row[0]): (int(row[1]) if row[1] is not None else None) for row in rows}


def find_due_for_eval(session: Session, now_epoch_s: int, *, limit: int = 1) -> list:
    """调度查询：捞出「该评估的 watch」（先清后调中的「调」侧）。

    谓词照前置方案 §3.2 第 1 条：
    ``WHERE active=1 AND ttl_end_epoch_s > :now AND next_due_epoch_s <= :now
      ORDER BY next_due_epoch_s LIMIT :n``。

    Args:
        session: 调用方会话。
        now_epoch_s: 当前时刻（UTC 秒）。
        limit: 单轮最多取多少条（>=1）。

    Returns:
        ``HotspotWatch`` 行列表，按 ``next_due_epoch_s`` 升序。
    """
    now_epoch_s = _require_epoch(now_epoch_s, "invalid_now_epoch_s")
    limit = max(1, int(limit))
    return (
        session.query(HotspotWatch)
        .filter(
            HotspotWatch.active.is_(True),
            HotspotWatch.ttl_end_epoch_s > now_epoch_s,
            HotspotWatch.next_due_epoch_s <= now_epoch_s,
        )
        .order_by(HotspotWatch.next_due_epoch_s.asc())
        .limit(limit)
        .all()
    )


def find_expired_for_cleanup(session: Session, now_epoch_s: int) -> list:
    """清理查询：捞出「该清理的历史 / 过期记录」（先清后调中的「清」侧）。

    谓词照前置方案 §3.2 第 2 条：``WHERE active=1 AND ttl_end_epoch_s <= :now``。
    与调度查询职责分开——过期行不满足调度谓词，只能被这条捞到；不单独清理就会
    永久卡 ``active=1``，池子只进不出。

    Args:
        session: 调用方会话。
        now_epoch_s: 当前时刻（UTC 秒）。

    Returns:
        ``HotspotWatch`` 行列表，按 ``ttl_end_epoch_s`` 升序。
    """
    now_epoch_s = _require_epoch(now_epoch_s, "invalid_now_epoch_s")
    return (
        session.query(HotspotWatch)
        .filter(
            HotspotWatch.active.is_(True),
            HotspotWatch.ttl_end_epoch_s <= now_epoch_s,
        )
        .order_by(HotspotWatch.ttl_end_epoch_s.asc())
        .all()
    )


def release_expired(session: Session, now_epoch_s: int) -> int:
    """把过期行原子置为「已释放」：``active=0``、``released_epoch_s=now``、``stop_reason='expired'``。

    同时把 ``state_revision`` +1：释放改变了调度状态，按 02 §0 裁定一「任何改变调度或
    业务状态的写入，同事务内 state_revision = state_revision + 1」，借此 fence 掉
    释放前领取、释放后才回来的迟到写入。

    Args:
        session: 调用方会话；本函数只 flush，提交由调用方完成。
        now_epoch_s: 释放时刻（UTC 秒）。

    Returns:
        被释放的行数。
    """
    now_epoch_s = _require_epoch(now_epoch_s, "invalid_now_epoch_s")
    result = session.execute(
        update(HotspotWatch)
        .where(
            HotspotWatch.active.is_(True),
            HotspotWatch.ttl_end_epoch_s <= now_epoch_s,
        )
        .values(
            active=False,
            released_epoch_s=now_epoch_s,
            stop_reason="expired",
            state_revision=HotspotWatch.state_revision + 1,
        )
    )
    session.flush()
    return int(result.rowcount or 0)


def claim_revision(session: Session, bvid: str) -> int:
    """读取某 bvid 当前的写回代际（不写回）；不存在返回 0。

    对应 02 §0 裁定一第 1 步「领取时读取行上既有 state_revision，记为 claim_revision」。

    Args:
        session: 调用方会话。
        bvid: 视频 BV 号。

    Returns:
        当前 ``state_revision``（int）。
    """
    row = (
        session.query(HotspotWatch.state_revision)
        .filter(HotspotWatch.bvid == str(bvid or "").strip())
        .first()
    )
    return 0 if row is None else int(row[0])


def commit_state(
    session: Session,
    bvid: str,
    *,
    claim_revision: int,
    last_evaluation_epoch_s: int | None = None,
    last_confirmed_stage: str | None = None,
    state_json: dict | None = None,
    coverage_ratio: float | None = None,
    coverage_state: str | None = None,
    require_active: bool = False,
) -> bool:
    """带 revision 校验的写回：旧代际写入一律丢弃（02 §0 裁定一）。

    实现语义等价于条件 UPDATE ``WHERE bvid=:bvid AND state_revision=:claim_revision``：
    只有领取时的代际与当前一致才落盘，并在同一列表达式里把代际 +1；否则返回 ``False``
    且不改变任何列（不写快照、不动调度、不覆盖新 owner）。

    Args:
        session: 调用方会话；本函数只 flush。
        bvid: 视频 BV 号。
        claim_revision: 领取时读到的代际号。
        last_evaluation_epoch_s: 已提交评估的固定网格右边界（UTC 秒），可为 None。
        last_confirmed_stage: 已确认阶段，可为 None。
        state_json: 状态 JSON（候选基线 / 计数 / segment 标识），可为 None。
        coverage_ratio: coverage 数值，可为 None。
        coverage_state: coverage 枚举值，可为 None；与 ratio 必须同给同缺。
        require_active: 为 True 时把 ``active=1`` 一并放进 WHERE 谓词，作为**原子围栏** ——
            需求被撤销 / 手动停追（``release_watch`` / ``manual_stop``）后，迟到 worker 的写回
            在同一条 UPDATE 内被判无效并丢弃（第二批 C）。默认 False 保持既有调用点不变。

    Returns:
        提交成功 True；因代际过期 / 已释放被丢弃 False。

    Raises:
        ValueError: bvid 空、时间戳非法，或 coverage 两级不成对 / 取值非法。
    """
    clean_bvid = str(bvid or "").strip()
    if not clean_bvid:
        raise ValueError("invalid_bvid")
    validate_coverage(coverage_ratio, coverage_state)

    # state_revision 用列表达式自增，保证「提交 + 代际前进」在一条 UPDATE 内原子完成。
    values: dict = {"state_revision": HotspotWatch.state_revision + 1}
    if last_evaluation_epoch_s is not None:
        values["last_evaluation_epoch_s"] = _require_epoch(
            last_evaluation_epoch_s, "invalid_last_evaluation_epoch_s"
        )
    if last_confirmed_stage is not None:
        values["last_confirmed_stage"] = last_confirmed_stage
    if state_json is not None:
        values["state_json"] = state_json
    if coverage_ratio is not None:
        values["coverage_ratio"] = float(coverage_ratio)
        values["coverage_state"] = coverage_state

    predicates = [
        HotspotWatch.bvid == clean_bvid,
        HotspotWatch.state_revision == _require_epoch(claim_revision, "invalid_claim_revision"),
    ]
    if require_active:
        # 迟到围栏：需求撤销 / 停追把 active 置 0，这条 UPDATE 便匹配不到行 -> 丢弃。
        predicates.append(HotspotWatch.active.is_(True))
    result = session.execute(update(HotspotWatch).where(*predicates).values(**values))
    session.flush()
    return int(result.rowcount or 0) == 1
