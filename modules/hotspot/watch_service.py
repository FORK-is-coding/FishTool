"""热点单视频跟踪的编排层（FishTool 02 · 批 3）：把「捞 → 采 → 评 → 写」串成一轮 tick。

职责边界（严格对齐 ``FishTool_02_热点生命周期_专业方案与Agent执行`` §7 / §8 / §0 裁定一·二·三
与 ``FishTool_前置方案_预算重分与02租约规格`` §3.2）：

- **只管编排**：调度查询 → 领取代际 → 调采集端口 → 调算法 → fenced 写回 → 推进下次调度；
- **不写采集逻辑**：采集一律走可注入的端口；缺省端口复用既有
  ``modules/hotspot/collector.py`` 的**既有单视频采集入口**（该文件一行未改，也不往
  采集器里塞任何算法感知 —— 沿用 collector 开篇边界约定「采集器不感知生命周期算法」）；
- **不写算法**：阶段 / 状态机一律由批 1 的 ``algorithm/lifecycle_v2.LifecycleV2`` 产出，
  本层只负责搬运与落库（``algorithm/`` 两文件在本批红线冻结内，一行未改）；
- **时间字段**一律 ``*_epoch_s`` 秒级 int，不使用 ``_ts`` / ``_at`` / 毫秒。

一轮 tick 的顺序（**顺序不许换**）::

    先清：release_expired(now)          # 到期先归档，让出名额
    1. 捞：find_due_for_eval(session, now, limit)
    2. 领：claim_revision(session, bvid)                 # 记下代际
    3. 采：调既有采集端口拿一条新快照（不新写采集逻辑）
    4. 评：LifecycleV2.detect(...) + 单目标引擎取状态机   # 见 _evaluate 注释
    5. 写：commit_state(session, bvid, claim_revision=领取时的值, ...)
           → 返回 False = 代际已过期 → 丢弃这次结果，不写任何列
    6. 排：推进 next_due_epoch_s = now + 该行的 sample_interval_s
       # 事务口径（批 3.5 · 改动一）：步骤 5 与步骤 6 同处一个每目标事务，
       # 两次写入只让代际净 +1（由 commit_state 统一下发），不跳两格。

失败隔离：任一环节抛异常只记 ``failure_count + 1`` 与 ``last_error_code``（不含错误正文 /
Cookie），并按指数退避推进 ``next_due_epoch_s``（退避不小于正常采样间隔，上限 6 小时），
本轮继续处理下一个目标，绝不炸掉整轮。

合规与预算：本层不新增任何 HTTP 调用；网络与限频 / 预算全部沿用既有采集器与
``modules/hotspot/risk_control.RequestBudget``。测试一律对采集端口打桩，绝不真发请求。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol

from sqlalchemy import update
from sqlalchemy.orm import Session

from core.data_quality import parse_count, utc_now_epoch_s
from core.database import HotspotWatch, Video, VideoStats, get_session
from core.logger import get_logger

from .algorithm import Detection, LifecycleV2, Snapshot, Stage, TrendState
from .watch_store import (
    DEFAULT_SAMPLE_INTERVAL_S,
    claim_revision,
    commit_state,
    find_due_for_eval,
    release_expired,
)

logger = get_logger(__name__)

#: 单轮 tick 默认处理的目标数上限（``find_due_for_eval`` 的 limit）。
DEFAULT_TICK_LIMIT: int = 20

#: 常驻循环默认轮询间隔（秒）。
DEFAULT_LOOP_INTERVAL_S: int = 60

#: 失败退避上限（秒）：6 小时。指数退避超过该值即封顶。
MAX_BACKOFF_S: int = 6 * 3600

#: 连续失败计数封顶值：防止 ``2 ** failure_count`` 无上限上飘。
MAX_FAILURE_COUNT: int = 10

#: 采集来源标签：写入 ``videos`` / ``video_stats.source``，与 ranking / paint_c 并列。
WATCH_SOURCE: str = "watch"

#: 成功提交但阶段仍为「数据不足」时，不覆盖历史的 ``last_confirmed_stage``。
_UNCONFIRMED_STAGE: str = Stage.INSUFFICIENT


# --------------------------------------------------------------------- 数据载体


@dataclass(frozen=True)
class WatchTarget:
    """本轮待处理目标（脱离 session 后仍可用）。

    Attributes:
        bvid: 视频 BV 号。
        collection_tid: 该行归属的采集分区 ID，可为 None。
        sample_interval_s: 该行的采样间隔（秒），用于步骤 6 推进 ``next_due_epoch_s``。
        initial_state: 从 ``state_json`` 还原的状态机续算起点（无历史时为全新状态）。
    """

    bvid: str
    collection_tid: int | None
    sample_interval_s: int
    initial_state: TrendState


@dataclass(frozen=True)
class TargetOutcome:
    """单个目标的处理结果。

    Attributes:
        status: ``committed``（写回成功）或 ``dropped``（代际过期被 fence 丢弃）。
        detections: 该目标的算法契约输出（供后续 API / 展示批消费）。
    """

    status: str
    detections: list = field(default_factory=list)


@dataclass
class TickResult:
    """一轮 tick 的结构化结果，供调度侧日志与断言消费。

    Attributes:
        now_epoch_s: 本轮时刻（UTC 秒级 int）。
        released: 本轮「先清」归档的行数。
        due_count: 本轮「捞」到的目标数。
        committed: 写回成功的目标数。
        dropped: 因代际过期被丢弃的目标数（fencing 生效次数）。
        failed: 采集 / 评估 / 写回任一步抛异常的目标数。
        detections: 本轮全部算法契约输出。
    """

    now_epoch_s: int = 0
    released: int = 0
    due_count: int = 0
    committed: int = 0
    dropped: int = 0
    failed: int = 0
    detections: list = field(default_factory=list)


# --------------------------------------------------------------------- 采集端口


class SnapshotCollectorPort(Protocol):
    """采集端口契约：给一个 bvid，采一条新的 view 快照并落库。

    实现方负责网络、限频与持久化；失败直接抛异常，由编排层计入失败隔离。
    """

    async def collect(
        self, bvid: str, *, collection_tid: int | None = None, source: str = WATCH_SOURCE
    ) -> Any:
        """采集并落库一条快照，返回值由实现方定义（编排层不解释）。"""
        ...


class HotspotCollectorPort:
    """把既有 ``HotspotCollector`` 的单视频采集路径适配成 watch 采集端口。

    只复用 collector 的**公开单视频入口**，本层不新增任何采集逻辑、也不感知生命周期算法：

    - ``HotspotCollector.collect_one(bvid, *, collection_tid, source)``：采一条 view 快照并落
      ``videos`` / ``video_stats``（内部即 ``_fetch_view`` → ``_save_snapshot``，带预算 / 限频）。

    该方法正是 collector 自己 ``collect()`` 对**单个视频**所做的既有动作的等价抽取
    （批 3.5 起 ``collect()`` 反向复用 ``collect_one``，一份逻辑两个入口）；本类只是把
    「单视频采一条快照」暴露成可注入端口，便于编排层在测试中打桩，不再触碰任何私有方法。
    """

    def __init__(self, collector: Any) -> None:
        """绑定一个已初始化的既有采集器实例。

        Args:
            collector: ``modules.hotspot.collector.HotspotCollector`` 实例（或同契约替身）。
        """
        self._collector = collector

    async def collect(
        self, bvid: str, *, collection_tid: int | None = None, source: str = WATCH_SOURCE
    ) -> Any:
        """按 bvid 采一条 view 快照并落库。

        Args:
            bvid: 视频 BV 号。
            collection_tid: 该目标的采集分区 ID；详情接口的 tid 可能是二级分区，
                归属分区必须用这个值（与既有 ``_save_snapshot`` 口径一致）。
            source: 快照来源标签。

        Returns:
            ``_save_snapshot`` 返回的信号条数（int）。

        Raises:
            Exception: 采集 / 落库失败时原样上抛，由编排层计入 ``failure_count``。
        """
        return await self._collector.collect_one(
            bvid,
            collection_tid=(int(collection_tid) if collection_tid is not None else None),
            source=source,
        )


def default_collector_port() -> HotspotCollectorPort:
    """惰性构造缺省采集端口：真实 ``BilibiliAPI`` + 既有 ``HotspotCollector``。

    只在调用方未显式注入端口时惰性触发；**模块导入期不构造客户端、不发任何网络请求**。
    采集类别走 ``watch`` 域（与 ``BilibiliAPICore`` 的 ``quota_category`` 口径一致）。

    Returns:
        绑定既有采集器的 :class:`HotspotCollectorPort`。

    Raises:
        RuntimeError: 构造真实 API / 采集器失败时抛出中文错误。
    """
    try:
        from bilibili.api import BilibiliAPI

        from .collector import HotspotCollector
    except Exception as exc:  # noqa: BLE001 - 环境缺依赖时给中文错误而非裸 ImportError
        raise RuntimeError(f"构造 watch 缺省采集端口失败: {exc}") from exc
    return HotspotCollectorPort(HotspotCollector(BilibiliAPI(quota_category="watch")))


# --------------------------------------------------------------------- 纯函数工具


def error_code_of(exc: BaseException) -> str:
    """从异常提取稳定错误码：优先 ``exc.code``，否则异常类名。

    只取短码、截断 64 字符，**不写**错误正文 / Cookie（对齐 ``last_error_code`` 列注释）。

    Args:
        exc: 捕获到的异常。

    Returns:
        str: 稳定错误码。
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()[:64]
    return type(exc).__name__[:64]


def state_to_json(state: TrendState) -> dict:
    """把状态机快照序列化为可落 ``state_json`` 的 JSON。

    刻意**不写** ``TrendState.state_revision``：那是算法在内存对象上的代际计数器，
    与 ``hotspot_watch.state_revision``（调度写回代际）不是同一个东西，混存会误导读端。

    Args:
        state: 算法层 ``TrendState``。

    Returns:
        dict: 仅含续算所需字段的 JSON 字典。
    """
    return {
        "last_evaluation_epoch_s": state.last_evaluation_epoch_s,
        "prev_rate": state.prev_rate,
        "candidate": state.candidate,
        "baseline": state.baseline,
        "count": int(state.count),
        "stable_count": int(state.stable_count),
        "stage": state.stage,
    }


def state_from_json(payload: Any) -> TrendState:
    """从 ``state_json`` 还原状态机；缺失 / 非法时返回全新状态（不猜、不伪造）。

    Args:
        payload: ``hotspot_watch.state_json`` 读出的对象。

    Returns:
        TrendState: 可直接作为 ``LifecycleV2(initial_states=...)`` 的续算起点。
    """

    def _num(key: str) -> float | None:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def _non_negative_int(key: str) -> int:
        value = payload.get(key)
        if type(value) is not int or value < 0:
            return 0
        return value

    if not isinstance(payload, dict):
        return TrendState()

    last_eval = payload.get("last_evaluation_epoch_s")
    candidate = payload.get("candidate")
    stage = payload.get("stage")
    return TrendState(
        last_evaluation_epoch_s=(last_eval if type(last_eval) is int and last_eval >= 0 else None),
        prev_rate=_num("prev_rate"),
        candidate=(candidate if isinstance(candidate, str) and candidate else None),
        baseline=_num("baseline"),
        count=_non_negative_int("count"),
        stable_count=_non_negative_int("stable_count"),
        stage=(stage if isinstance(stage, str) and stage else Stage.OBSERVING),
    )


def _resolve_view(stats: VideoStats) -> tuple[int | None, str]:
    """按质量口径解析一条快照的 view：只有明确 ok 且可解析为整数才有效。

    口径与展示端 ``routes_lifecycle._load_snapshots`` 一致（03 规格 §4.4）：质量非 ok 的
    记录保留 ``view=None`` 的 marker，绝不兜底成 0。

    Args:
        stats: ``video_stats`` 行。

    Returns:
        ``(value, quality)``；quality 为 ``ok`` / ``missing`` / ``invalid`` / ``unknown``。
    """
    metric_status = getattr(stats, "metric_status", None)
    stated = None
    if isinstance(metric_status, dict):
        stated = metric_status.get("view")
    if not isinstance(stated, str) or not stated:
        fallback = getattr(stats, "view_status", None)
        stated = fallback if isinstance(fallback, str) and fallback else "unknown"
    if stated != "ok":
        return None, stated
    value, parsed = parse_count(getattr(stats, "view", None))
    if parsed != "ok":
        return None, parsed
    return value, "ok"


def load_bvid_snapshots(session: Session, bvid: str) -> list[Snapshot]:
    """读某个 bvid 的历史快照序列，供算法层评估（步骤 4 的取数）。

    Args:
        session: 调用方会话。
        bvid: 视频 BV 号。

    Returns:
        list[Snapshot]: 按 ``snapshot_time`` 升序；质量非 ok 的记录 ``view=None``，
        但保留已知 ``captured_epoch_s`` 供算法断段。
    """
    clean_bvid = str(bvid or "").strip()
    if not clean_bvid:
        return []
    query = (
        session.query(Video, VideoStats)
        .join(VideoStats, Video.id == VideoStats.video_id)
        .filter(Video.bvid == clean_bvid)
        .order_by(VideoStats.snapshot_time.asc())
    )
    rows: list[Snapshot] = []
    for video, stats in query.all():
        view_value, view_quality = _resolve_view(stats)
        raw_view, _ = parse_count(getattr(stats, "view", None))
        epoch = getattr(stats, "captured_epoch_s", None)
        epoch = epoch if type(epoch) is int else None
        captured_at = stats.snapshot_time
        if captured_at is None:
            # 仅在有明确 epoch 时做显示兜底；不凭机器时区解释旧 naive 时间。
            captured_at = datetime.fromtimestamp(epoch) if epoch is not None else datetime.now()
        rows.append(
            Snapshot(
                bvid=getattr(video, "bvid", clean_bvid),
                tid=int(getattr(video, "tid", 0) or 0),
                captured_at=captured_at,
                view=view_value,
                title=str(getattr(video, "title", "") or ""),
                owner_mid=int(getattr(video, "mid", 0) or 0),
                owner_name=str(getattr(video, "author", "") or ""),
                source="video_stats",
                captured_epoch_s=epoch,
                view_quality=view_quality,
                raw_view=raw_view,
                metric_status=(
                    stats.metric_status if isinstance(getattr(stats, "metric_status", None), dict) else None
                ),
                collection_tid=(
                    stats.collection_tid if type(getattr(stats, "collection_tid", None)) is int else None
                ),
                raw_tid=stats.raw_tid if type(getattr(stats, "raw_tid", None)) is int else None,
            )
        )
    return rows


def advance_next_due(
    session: Session,
    bvid: str,
    *,
    now_epoch_s: int,
    sample_interval_s: int,
    bump_revision: bool = True,
) -> None:
    """步骤 6 · 排：推进 ``next_due_epoch_s = now + sample_interval_s``，并记一次成功。

    改的是调度 / 业务列（``next_due_epoch_s`` / ``last_success_epoch_s`` / ``failure_count``），
    按 02 §0 裁定一「任何改变调度或业务状态的写入，同事务内 ``state_revision + 1``」，
    默认 ``bump_revision=True`` 时把代际 +1，fence 掉更早领取的迟到写入。

    代际口径（批 3.5 · 改动一）：在 ``_process_target`` 里本函数与 ``commit_state`` 处于
    **同一事务**、都是「改变状态」的写入；若两处各 +1，一轮 tick 会净 +2，跟轮次对不上账。
    故 caller 传 ``bump_revision=False``，把这一轮的 ``state_revision + 1`` 交给
    ``commit_state`` 统一下发：一轮 tick = 一个事务 = 代际净 +1（调度列与状态列同时落）。

    Args:
        session: 调用方会话；本函数只 flush，提交由调用方完成。
        bvid: 目标 BV 号。
        now_epoch_s: 本轮时刻（UTC 秒）。
        sample_interval_s: 该行的采样间隔；非法值回退 ``DEFAULT_SAMPLE_INTERVAL_S``。
        bump_revision: 是否在本函数内把 ``state_revision + 1``；缺省 True。
            同一事务内已有别的状态写入（如 ``commit_state``）时传 False，避免重复 +1。

    Returns:
        无。
    """
    interval = (
        sample_interval_s
        if type(sample_interval_s) is int and sample_interval_s > 0
        else DEFAULT_SAMPLE_INTERVAL_S
    )
    now_epoch_s = int(now_epoch_s)
    values: dict[str, Any] = {
        "next_due_epoch_s": now_epoch_s + interval,
        "last_success_epoch_s": now_epoch_s,
        "failure_count": 0,
        "last_error_code": None,
    }
    # 代际 +1 在同一 UPDATE 内用列表达式完成，保证「提交 + 代际前进」原子。
    if bump_revision:
        values["state_revision"] = HotspotWatch.state_revision + 1
    session.execute(
        update(HotspotWatch)
        .where(HotspotWatch.bvid == str(bvid or "").strip())
        .values(**values)
    )
    session.flush()


def record_failure(
    session: Session,
    bvid: str,
    *,
    error_code: str,
    now_epoch_s: int,
    sample_interval_s: int = DEFAULT_SAMPLE_INTERVAL_S,
) -> None:
    """失败隔离落点：``failure_count`` 累加、``last_error_code``、``last_attempt_epoch_s``，并按指数退避推进调度。

    只写稳定错误码，不写错误正文 / Cookie。**本轮推进** ``next_due_epoch_s``（批 3.5 · 改动二）::

        退避 = min(sample_interval_s * 2 ** failure_count, MAX_BACKOFF_S)   # 上限 6 小时
        next_due = now + max(sample_interval_s, 退避)                        # 退避永不小于正常间隔

    关键点：
    - ``failure_count`` 先按「本次失败后」的新值计算（封顶 ``MAX_FAILURE_COUNT``），再定退避；
    - ``max(sample_interval_s, 退避)`` 兜住「``sample_interval_s`` 本身 > 6h」的洞：
      否则 ``min(..., 6h)`` 会把退避压得比正常间隔还短 —— 失败反而加速，完全反了；
    - 成功一次由 :func:`advance_next_due` 把 ``failure_count`` 清零、``last_error_code`` 置空。

    代际 +1 理由同 :func:`advance_next_due`（改了业务列）；失败走独立事务，故此处仍需 +1。

    Args:
        session: 调用方会话；本函数只 flush，提交由调用方完成。
        bvid: 目标 BV 号。
        error_code: 稳定错误码（短码，无正文）。
        now_epoch_s: 本轮时刻（UTC 秒）。
        sample_interval_s: 该行的采样间隔；非法值回退 ``DEFAULT_SAMPLE_INTERVAL_S``。

    Returns:
        无。
    """
    clean_bvid = str(bvid or "").strip()
    interval = (
        sample_interval_s
        if type(sample_interval_s) is int and sample_interval_s > 0
        else DEFAULT_SAMPLE_INTERVAL_S
    )
    now_epoch_s = int(now_epoch_s)
    # 读当前失败计数（同事务内已 flush 的旧值，或已提交的历史值），据此算本次失败后的新计数。
    current = (
        session.query(HotspotWatch.failure_count)
        .filter(HotspotWatch.bvid == clean_bvid)
        .first()
    )
    base = int(current[0]) if current is not None and current[0] is not None else 0
    failure_count = min(base + 1, MAX_FAILURE_COUNT)
    backoff_s = min(interval * (2 ** failure_count), MAX_BACKOFF_S)
    delay_s = max(interval, backoff_s)
    session.execute(
        update(HotspotWatch)
        .where(HotspotWatch.bvid == clean_bvid)
        .values(
            failure_count=failure_count,
            last_error_code=str(error_code or "unknown_error")[:64],
            last_attempt_epoch_s=now_epoch_s,
            next_due_epoch_s=now_epoch_s + delay_s,
            state_revision=HotspotWatch.state_revision + 1,
        )
    )
    session.flush()


def _confirmed_stage(analysis: Any) -> str | None:
    """取「最近一次已确认阶段」；算法判为「数据不足」时不覆盖历史，返回 None。

    Args:
        analysis: 算法层 ``Analysis``。

    Returns:
        str | None: 已确认阶段名，或 None（表示不写这一列）。
    """
    stage = getattr(analysis, "stage", None)
    if not isinstance(stage, str) or stage == _UNCONFIRMED_STAGE:
        return None
    return stage


def _build_detector(initial_states: dict[str, TrendState], as_of_epoch_s: int) -> LifecycleV2:
    """缺省算法构造器：用批 1 的 ``LifecycleV2`` 并以历史状态续算（不冷启动）。

    Args:
        initial_states: ``bvid -> TrendState`` 的续算起点。
        as_of_epoch_s: 计算截止时刻（UTC 秒）。

    Returns:
        LifecycleV2: 已注入历史状态的算法实例。
    """
    return LifecycleV2(as_of_epoch_s=as_of_epoch_s, initial_states=initial_states)


# --------------------------------------------------------------------- 编排层


class WatchService:
    """单视频跟踪编排层：一轮 tick = 先清 → 捞 → （领 → 采 → 评 → 写 → 排）× N。"""

    def __init__(
        self,
        *,
        collector_port: Any | None = None,
        session_factory: Callable[[], Session] | None = None,
        detector_factory: Callable[[dict[str, TrendState], int], LifecycleV2] | None = None,
        snapshot_loader: Callable[[Session, str], list[Snapshot]] | None = None,
        now_fn: Callable[[], int] = utc_now_epoch_s,
    ) -> None:
        """构造编排层（全部依赖可注入，便于单测与离线回放）。

        Args:
            collector_port: 采集端口（提供 ``async collect(bvid, *, collection_tid, source)``）；
                缺省时**惰性**构造真实 ``BilibiliAPI`` + ``HotspotCollector`` 端口。
            session_factory: 会话工厂；缺省用 ``core.database.get_session``。
            detector_factory: 算法构造器 ``(initial_states, as_of_epoch_s) -> LifecycleV2``；
                缺省用批 1 的 ``LifecycleV2``。
            snapshot_loader: 历史快照读取器 ``(session, bvid) -> list[Snapshot]``；
                缺省用本模块 :func:`load_bvid_snapshots`。
            now_fn: 时钟，返回 UTC 秒级 int；缺省 ``utc_now_epoch_s``。
        """
        self._collector_port = collector_port
        self._session_factory = session_factory or get_session
        self._detector_factory = detector_factory or _build_detector
        self._snapshot_loader = snapshot_loader or load_bvid_snapshots
        self._now_fn = now_fn

    # ---- 依赖 ----

    @property
    def collector_port(self) -> Any:
        """采集端口；未显式注入时惰性构造（导入期不建客户端、不发请求）。"""
        if self._collector_port is None:
            self._collector_port = default_collector_port()
        return self._collector_port

    # ---- 对外入口 ----

    async def run_tick(self, *, limit: int = DEFAULT_TICK_LIMIT) -> TickResult:
        """跑一轮完整 tick（顺序：先清 → 捞 → 领 → 采 → 评 → 写 → 排）。

        目标级异常在此被隔离并计入 ``TickResult.failed``；调度阶段（先清 / 捞）的
        基础设施异常会原样上抛，由 :meth:`_loop` 兜住后继续下一轮。

        Args:
            limit: 本轮最多处理多少个目标（>=1）。

        Returns:
            TickResult: 本轮结构化结果。
        """
        now_epoch_s = int(self._now_fn())
        result = TickResult(now_epoch_s=now_epoch_s)

        # ---- 先清 + 1. 捞：同一事务内「到期先归档、再捞该评估的」，
        #      保证本轮被释放的行不会出现在本轮调度结果里。
        session = self._session_factory()
        try:
            result.released = release_expired(session, now_epoch_s)
            targets = self._load_targets(session, now_epoch_s, limit)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("watch tick 调度阶段失败（先清 / 捞）")
            raise
        finally:
            session.close()

        result.due_count = len(targets)
        if not targets:
            logger.info("watch tick 空转：now_epoch_s=%s 无该评估的目标", now_epoch_s)
            return result

        # 算法只构造一次：用本轮各目标的历史状态续算，避免每次冷启动。
        detector = self._detector_factory(
            {target.bvid: target.initial_state for target in targets}, now_epoch_s
        )

        for target in targets:
            try:
                outcome = await self._process_target(
                    target, now_epoch_s=now_epoch_s, detector=detector
                )
            except Exception as exc:  # noqa: BLE001 - 失败隔离：单个目标炸了不进整轮
                result.failed += 1
                logger.warning("watch 目标处理失败 bvid=%s: %s", target.bvid, exc)
                self._record_failure(
                    target.bvid,
                    exc,
                    now_epoch_s,
                    sample_interval_s=target.sample_interval_s,
                )
                continue
            result.detections.extend(outcome.detections)
            if outcome.status == "committed":
                result.committed += 1
            else:
                result.dropped += 1

        logger.info(
            "watch tick: now_epoch_s=%s 释放=%s 到点=%s 写回=%s 丢弃=%s 失败=%s",
            result.now_epoch_s,
            result.released,
            result.due_count,
            result.committed,
            result.dropped,
            result.failed,
        )
        return result

    async def _loop(
        self,
        *,
        interval_s: int = DEFAULT_LOOP_INTERVAL_S,
        stop_event: asyncio.Event | None = None,
        limit: int = DEFAULT_TICK_LIMIT,
    ) -> None:
        """常驻调度循环：每 ``interval_s`` 秒跑一轮 tick，单轮异常只记日志、不中断循环。

        ``core/monitor_service._quota_housekeeping`` 的注释已约定：评论常驻服务保持评论
        职责不放 watch 采样循环，「先清后调」由本方法承担。本批只提供循环入口，**不改**
        ``core/`` 任何存量文件；是否挂到全局调度由后续整合批次决定。

        Args:
            interval_s: 轮询间隔（秒，>=1）。
            stop_event: 外部停止信号；缺省新建一个永不触发的 Event。
            limit: 每轮最多处理的目标数。

        Returns:
            无。
        """
        stop_event = stop_event or asyncio.Event()
        interval_s = max(1, int(interval_s))
        while not stop_event.is_set():
            try:
                await self.run_tick(limit=limit)
            except Exception as exc:  # noqa: BLE001 - 单轮失败不允许中断常驻循环
                logger.exception("watch tick 异常，继续下一轮: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                continue

    # ---- 步骤 1：捞 ----

    def _load_targets(
        self, session: Session, now_epoch_s: int, limit: int
    ) -> list[WatchTarget]:
        """1. 捞：读该评估的 watch 行，抽出脱离 session 仍可用的目标。

        Args:
            session: 调度会话。
            now_epoch_s: 本轮时刻（UTC 秒）。
            limit: 单轮上限（>=1）。

        Returns:
            list[WatchTarget]: 按 ``next_due_epoch_s`` 升序的目标列表。
        """
        rows = find_due_for_eval(session, now_epoch_s, limit=max(1, int(limit)))
        targets: list[WatchTarget] = []
        for row in rows:
            sample_interval_s = getattr(row, "sample_interval_s", None)
            targets.append(
                WatchTarget(
                    bvid=str(row.bvid),
                    collection_tid=(
                        int(row.collection_tid) if row.collection_tid is not None else None
                    ),
                    sample_interval_s=(
                        int(sample_interval_s)
                        if type(sample_interval_s) is int and sample_interval_s > 0
                        else DEFAULT_SAMPLE_INTERVAL_S
                    ),
                    initial_state=state_from_json(getattr(row, "state_json", None)),
                )
            )
        return targets

    # ---- 步骤 2..6：单目标处理 ----

    async def _process_target(
        self, target: WatchTarget, *, now_epoch_s: int, detector: LifecycleV2
    ) -> TargetOutcome:
        """处理单个目标：领代际 → 采 → 评 → fenced 写回 → 推进调度。

        本方法内部**只有一条事务**：任一步抛异常即 rollback 并上抛（由 :meth:`run_tick`
        计入失败隔离）；代际过期时同样 rollback 并返回 ``dropped``，不写任何列。

        Args:
            target: 本轮目标。
            now_epoch_s: 本轮时刻（UTC 秒）。
            detector: 已注入历史状态的算法实例。

        Returns:
            TargetOutcome: ``committed`` 或 ``dropped``（附带该目标的契约输出）。

        Raises:
            Exception: 采集 / 评估 / 写回失败时原样上抛。
        """
        session = self._session_factory()
        try:
            # ---- 2. 领：记下代际，步骤 5 必须用它做 fencing ----
            claim = claim_revision(session, target.bvid)

            # ---- 3. 采：调既有采集端口的现有入口（测试在此打桩，绝不真发请求）----
            await self.collector_port.collect(
                target.bvid, collection_tid=target.collection_tid, source=WATCH_SOURCE
            )

            # ---- 4. 评：读该 bvid 的历史快照 -> 算法层 ----
            rows = self._snapshot_loader(session, target.bvid)
            detections, analysis = self._evaluate(detector, target.bvid, rows)

            # ---- 5. 写：带「领取时」代际的 fenced 提交 ----
            # False = 领取后被别的代际抢先 -> 丢弃这次结果，不许硬写。
            committed = commit_state(
                session,
                target.bvid,
                claim_revision=claim,
                last_evaluation_epoch_s=analysis.state.last_evaluation_epoch_s,
                last_confirmed_stage=_confirmed_stage(analysis),
                state_json=state_to_json(analysis.state),
                coverage_ratio=float(analysis.coverage_ratio),
                coverage_state=analysis.coverage_state.value,
            )
            if not committed:
                # 不写快照、不动调度、不覆盖新 owner：整轮结果丢弃。
                session.rollback()
                logger.info(
                    "watch 写回被 fence 丢弃（代际已过期） bvid=%s claim_revision=%s",
                    target.bvid,
                    claim,
                )
                return TargetOutcome(status="dropped")

            # ---- 6. 排：推进 next_due = now + sample_interval_s ----
            # 与步骤 5 的 commit_state 同处一个事务：代际 +1 由 commit_state 统一下发，
            # 这里传 bump_revision=False，保证「一轮 tick = 一个事务 = 代际净 +1」不跳两格。
            advance_next_due(
                session,
                target.bvid,
                now_epoch_s=now_epoch_s,
                sample_interval_s=target.sample_interval_s,
                bump_revision=False,
            )
            session.commit()
            return TargetOutcome(status="committed", detections=detections)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _evaluate(
        detector: LifecycleV2, bvid: str, rows: list[Snapshot]
    ) -> tuple[list, Any]:
        """评：调算法层 ``algorithm/lifecycle_v2``（纯计算，不落库、不触网）。

        - ``detect(rows)``：算法契约入口，产出统一 ``Detection``（进本轮结果，供后续
          API / 展示批消费）；
        - ``analyze_one(bvid, rows)``：同一引擎的单目标入口，额外给出 ``TrendState`` 与
          coverage 两级 —— 步骤 5 要落 ``state_json`` / ``last_evaluation_epoch_s`` /
          ``coverage_*``，而 ``detect`` 只返回 ``Detection``、不暴露状态机，且批 1 的
          ``algorithm/`` 两文件在本批红线冻结内（一行不许改），故状态机取单目标引擎的输出。

        两次调用共享同一次快照读取结果，都是纯函数计算。

        Args:
            detector: 算法实例。
            bvid: 目标 BV 号。
            rows: 该目标的快照序列。

        Returns:
            ``(detections, analysis)``。
        """
        snapshots = list(rows)
        detections = detector.detect(snapshots)
        analysis = detector.analyze_one(bvid, snapshots)
        return detections, analysis

    # ---- 失败隔离落点 ----

    def _record_failure(
        self,
        bvid: str,
        exc: BaseException,
        now_epoch_s: int,
        *,
        sample_interval_s: int = DEFAULT_SAMPLE_INTERVAL_S,
    ) -> None:
        """记录一次目标失败；本方法自身的新异常只记日志，绝不再上抛。

        Args:
            bvid: 目标 BV 号。
            exc: 触发失败的异常。
            now_epoch_s: 本轮时刻（UTC 秒）。
            sample_interval_s: 该目标行的采样间隔，用于按指数退避推进 ``next_due``。

        Returns:
            无。
        """
        session = self._session_factory()
        try:
            record_failure(
                session,
                bvid,
                error_code=error_code_of(exc),
                now_epoch_s=now_epoch_s,
                sample_interval_s=sample_interval_s,
            )
            session.commit()
        except Exception:  # noqa: BLE001 - 记失败再失败也不许影响整轮
            session.rollback()
            logger.exception("记录 watch 失败计数失败 bvid=%s", bvid)
        finally:
            session.close()


#: 便于外部按名字取缺省算法（与 ``TickResult.detections`` 的类型提示配合）。
DEFAULT_DETECTOR = LifecycleV2

__all__ = [
    "DEFAULT_LOOP_INTERVAL_S",
    "DEFAULT_TICK_LIMIT",
    "MAX_BACKOFF_S",
    "MAX_FAILURE_COUNT",
    "WATCH_SOURCE",
    "Detection",
    "HotspotCollectorPort",
    "SnapshotCollectorPort",
    "TargetOutcome",
    "TickResult",
    "WatchService",
    "WatchTarget",
    "advance_next_due",
    "default_collector_port",
    "error_code_of",
    "load_bvid_snapshots",
    "record_failure",
    "state_from_json",
    "state_to_json",
]
