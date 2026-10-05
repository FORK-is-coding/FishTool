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
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any, Callable, Protocol

from sqlalchemy import bindparam, inspect as sa_inspect, text, update
from sqlalchemy.orm import Session

from core.data_quality import parse_count, read_stored_pubdate, utc_now_epoch_s
from core.database import HotspotWatch, Video, VideoStats, get_session
from core.logger import get_logger

from .algorithm import Detection, LifecycleV2, Snapshot, Stage, TrendState
from .risk_control import BudgetWiringError
from .watch_demand import (
    REASON_BLOCKED_BY_USER,
    REASON_NO_DEMAND,
    REASON_RELEASED,
    REASON_TRACKING,
    demand_reason,
    has_demands,
    resolve_interval_s,
)
from .watch_queue import AdmissionResult, WatchPool
from .watch_store import (
    DEFAULT_SAMPLE_INTERVAL_S,
    claim_revision,
    commit_state,
    find_due_for_budget_category,
    find_due_for_eval,
    load_fast_until_map,
    reactivate_watch,
    release_expired,
    release_watch,
    reschedule_watch,
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

#: 需求命名空间白名单（04 联合前置 P3）：只允许这三类，别处不许新造。
DEMAND_NAMESPACES: frozenset = frozenset({"manual", "ranking", "events"})

#: 释放原因码：需求（events 命名空间）全撤后停止采样（第二批 B / D）。**不是新列**，
#: 复用既有的 ``stop_reason`` 列（其列注释允许 ``expired`` / ``manual_stop``，本值同族）。
STOP_REASON_EVENTS_REVOKED: str = "events_revoked"

#: 预算类别（04 §6.3 L626 / §8.1 L656）：普通 watch 与快信号 watch 各自独立硬上限，
#: 目的就是「不让 fast 通道饿死 normal 基础采样」。
BUDGET_CATEGORY_NORMAL: str = "normal_watch"
BUDGET_CATEGORY_FAST: str = "fast_watch"

#: 预算类别白名单（顺序即选取优先级：先保底 normal，再 fast）。
BUDGET_CATEGORIES: tuple = (BUDGET_CATEGORY_NORMAL, BUDGET_CATEGORY_FAST)

#: 类间轮转的处理顺序（07 执行案 §9.2）：normal/fast 按类 FIFO 取候选、类间轮转。
#: 单 watch 实例只保存「下次起始类别」``_select_rotation`` 即可，不要求持久化。
BUDGET_ROTATION_ORDER: tuple = (BUDGET_CATEGORY_FAST, BUDGET_CATEGORY_NORMAL)

#: 准入派生 reason 的稳定取值集合（供调度侧 / 测试复用，不在本层另造态名）。
DEMAND_REASONS: frozenset = frozenset(
    {REASON_BLOCKED_BY_USER, REASON_TRACKING, REASON_RELEASED, REASON_NO_DEMAND}
)

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
        category: 本目标的预算类别（``normal_watch`` / ``fast_watch``），由
            ``fast_until_s`` 是否仍生效决定；第二批 F 的「不互相饿死」按它分桶。
    """

    bvid: str
    collection_tid: int | None
    sample_interval_s: int
    initial_state: TrendState
    category: str = BUDGET_CATEGORY_NORMAL


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
        budget_skipped: 因所属类别预算暂不可授予而被本轮跳过的到点目标数（第二批 F）。
        retry_delay_s: 本轮若有类别因预算被跳过，最早可重试的等待秒数；否则 None。
        budget_exhausted: 是否出现「所有到点项都被预算挡住、无一项可运行」。
        budget_deferred: 逐目标 reserve 时额度暂不可授予而被延期的目标数（07 案 W1）。
            **不是**平台失败，不计入 ``failed``、不增 ``failure_count``。
        candidates_considered: 选择器读取的候选目标数（07 案 §9.2；两类各按类 LIMIT 读取，
            **不等于**本轮允许处理数）。
        admitted: 本轮真正获准（``reserve`` 授予）并进入执行的目标数（07 案 §9.2），恒 ``<= limit``。
        collected: 本轮完成「采集 → 评估 → 写回」而未抛异常的目标数（= ``committed + dropped``）。
    """

    now_epoch_s: int = 0
    released: int = 0
    due_count: int = 0
    committed: int = 0
    dropped: int = 0
    failed: int = 0
    detections: list = field(default_factory=list)
    budget_skipped: int = 0
    retry_delay_s: float | None = None
    budget_exhausted: bool = False
    budget_deferred: int = 0
    candidates_considered: int = 0
    admitted: int = 0
    collected: int = 0
    #: 逐目标 reserve 被延期时的**按类别**明细（07 案 §10.3 budget_deferred_by_category）：
    #: 便于把「某类额度不足」（per-category 硬限）与「整轮 no-eligible」分开观测。
    #: 只记容量延期，**不是**平台失败，不影响 ``failure_count``。
    budget_deferred_by_category: dict = field(default_factory=dict)


@dataclass
class _Selection:
    """一轮「捞」的结果（含预算分桶信息，第二批 F / 07 案 W2）。

    Attributes:
        targets: 本轮锁定处理的候选（``<= limit``）。
        overflow: 已 peek 门控、但超出 ``limit`` 的有界补选池（07 案 §9.2）；仅用于
            ``reserve`` 因状态变化被拒时补选，**不**改变「本轮 admitted 总数 <= limit」。
        budget_skipped: 因所属类别本类额度不可授予而被跳过的候选数。
        retry_delay_s: 最早可重试等待秒数；无则 None。
        budget_exhausted: 是否「读到的候选全被预算挡住、无一项可运行」。
        candidates_considered: 两类读取的候选总数（两类各按类 LIMIT，不等于处理数）。
    """

    targets: list = field(default_factory=list)
    overflow: list = field(default_factory=list)
    budget_skipped: int = 0
    retry_delay_s: float | None = None
    budget_exhausted: bool = False
    candidates_considered: int = 0


@dataclass(frozen=True)
class _TargetClaim:
    """单目标「执行前重检」的短 session 快照（07 案 §9.3）。

    由 :meth:`WatchService._claim_short` 在**独立短 session** 内读出，session 随读随关；
    ``run_tick`` 随后以 :attr:`category` 做 ``reserve``、以 :attr:`claim_revision` 做 fenced 写回。

    Attributes:
        claim_revision: 读取时该行的 ``state_revision``（fencing 用）。
        category: 重检后的**真实当前类别**（``normal_watch`` / ``fast_watch``）——不拿旧分类扣错窗。
    """

    claim_revision: int
    category: str


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

    async def collect_admitted(
        self,
        bvid: str,
        *,
        admission: Any,
        collection_tid: int | None = None,
        source: str = WATCH_SOURCE,
    ) -> Any:
        """可选能力：带单请求准入凭证采一条快照（07 执行案 §8.2）。

        实现方若要在**有预算**的 watch 路径使用，必须实现本方法（或经适配器显式声明
        自身不另扣 L 预算）；否则编排层启动时报 ``BudgetWiringError``，不静默绕过。
        """
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

    async def collect_admitted(
        self,
        bvid: str,
        *,
        admission: Any,
        collection_tid: int | None = None,
        source: str = WATCH_SOURCE,
    ) -> Any:
        """带单请求准入凭证采一条快照（07 执行案 §8.2）。

        与 :meth:`collect` 的唯一差别是把 ``admission`` 透传给底层 ``collect_one``，由
        ``_fetch_view`` 以 ``issuer.redeem(...)`` 兑换这一份 L 层票据，**不再**二次扣费。

        Args:
            bvid: 视频 BV 号。
            admission: 由 ``RequestBudget.reserve`` 签发的逻辑准入凭证。
            collection_tid: 该目标的采集分区 ID。
            source: 快照来源标签。

        Returns:
            ``_save_snapshot`` 返回的信号条数（int）。

        Raises:
            Exception: 采集 / 落库 / 凭证兑换失败时原样上抛，由编排层计失败隔离。
        """
        return await self._collector.collect_one(
            bvid,
            collection_tid=(int(collection_tid) if collection_tid is not None else None),
            source=source,
            logical_admission=admission,
        )


def default_collector_port(budget: Any | None = None) -> HotspotCollectorPort:
    """惰性构造缺省采集端口：真实 ``BilibiliAPI`` + 既有 ``HotspotCollector``。

    只在调用方未显式注入端口时惰性触发；**模块导入期不构造客户端、不发任何网络请求**。
    采集类别走 ``watch`` 域（与 ``BilibiliAPICore`` 的 ``quota_category`` 口径一致）。

    Args:
        budget: 可选的 ``RequestBudget``（或同契约替身）；透传给 ``HotspotCollector``。
            缺省 ``None`` = 采集器自建默认预算（保持既有行为）。传入编排层的同一实例即可
            让缺省采集端口与 tick 预算门共用同一预算。

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
    return HotspotCollectorPort(HotspotCollector(BilibiliAPI(quota_category="watch"), budget=budget))


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


def _default_gate_version() -> str:
    """当前 v2 出现期门的默认 ``threshold_version``（08 案 §J5）。

    延迟导入避免 ``watch_service`` 与算法层在模块加载期相互牵扯；watch 链路恒走 v2、
    不经算法注册表，故取 dataclass 默认值。
    """
    from .algorithm.lifecycle_v2 import LifecycleV2Config

    return LifecycleV2Config().threshold_version


def state_to_json(state: TrendState, *, threshold_version: str | None = None) -> dict:
    """把状态机快照序列化为可落 ``state_json`` 的 JSON。

    刻意**不写** ``TrendState.state_revision``：那是算法在内存对象上的代际计数器，
    与 ``hotspot_watch.state_revision``（调度写回代际）不是同一个东西，混存会误导读端。

    08 案 §J5（B6a）：**必须**带上产出该状态的 ``threshold_version`` —— 门槛口径变过
    之后，旧版本状态里的 prev / candidate / count 不能再当同一套判据的续算起点。

    Args:
        state: 算法层 ``TrendState``。
        threshold_version: 产出该状态的算法版本；缺省取当前默认门版本。

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
        "threshold_version": threshold_version or _default_gate_version(),
    }


def state_from_json(payload: Any, *, gate_version: str | None = None) -> TrendState:
    """从 ``state_json`` 还原状态机；缺失 / 非法 / **版本不符**时返回全新状态。

    08 案 §J5（B6a）：读到的 ``threshold_version`` 与当前门版本不一致（含旧记录压根没有
    该字段）时，**不**沿用 prev / candidate / baseline / count / stable_count / stage ——
    那些计数是按旧门槛口径攒出来的，混用等于伪称新版本已确认。此时返回空状态，交给
    ``analyze_one`` 用现有快照做显式重算；raw 快照不受影响，历史 ``last_confirmed_stage``
    仍留在表列里作为 legacy 记录，本函数不删不写。

    Args:
        payload: ``hotspot_watch.state_json`` 读出的对象。
        gate_version: 期望的当前门版本；缺省取 :func:`_default_gate_version`。

    Returns:
        TrendState: 可直接作为 ``LifecycleV2(initial_states=...)`` 的续算起点。
    """
    if not isinstance(payload, dict):
        return TrendState()
    expected = gate_version or _default_gate_version()
    if payload.get("threshold_version") != expected:
        # 旧版本 / 无版本标记：不混用计数，从空状态显式重算。
        return TrendState()

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


def _require_epoch_s(value: int) -> int:
    """校验秒级 UTC epoch：必须是非负 int（显式排除 bool）。

    Args:
        value: 待校验值。

    Returns:
        int: 校验通过的秒级 epoch。

    Raises:
        ValueError: 非 int / 为 bool / 为负时抛出 ``invalid_now_s``。
    """
    if type(value) is not int or value < 0:
        raise ValueError("invalid_now_s")
    return value


def _json_to_obj(value: Any) -> Any:
    """把 ``source_demands`` 列原值还原成 Python 对象（缺失 / 非法一律当空）。

    Args:
        value: 从列里读出的原值（可能是 None / str / bytes / 已解析对象）。

    Returns:
        Any: 解析后的对象；无法解析时返回 None。
    """
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="ignore")
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


def _demand_bvids(namespace: str, demand_key: str, descriptor: dict) -> list:
    """从一个需求条目里解析出涉及的 bvid 列表。

    Args:
        namespace: 命名空间（``events`` 必须显式给 bvid，不拿 event_id 兜底当视频号）。
        demand_key: 该条目在 ``desired`` 里的键。
        descriptor: 需求描述。

    Returns:
        list[str]: 涉及的 bvid 列表（可能多个）。

    Raises:
        ValueError: 解析不出任何合法 bvid。
    """
    raw = descriptor.get("bvids")
    if isinstance(raw, (list, tuple)) and raw:
        return [b for b in (str(item).strip() for item in raw) if b]
    single = descriptor.get("bvid")
    if single is not None and str(single).strip():
        return [str(single).strip()]
    if namespace == "events":
        # events 的键是 event_id，不是视频号，缺 bvid/bvids 一律判非法而不是静默兜底。
        raise ValueError("invalid_desired_entry")
    # manual / ranking：键本身就是 bvid。
    bvid = str(demand_key).strip()
    if not bvid:
        raise ValueError("invalid_desired_entry")
    return [bvid]


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


def _resolve_stored_pubdate(stats: Any) -> tuple[int | None, str]:
    """从 ``video_stats`` 行读发布时间，把「旧行未写」与「接口缺失」区分开。

    口径唯一来源是 :func:`core.data_quality.read_stored_pubdate`（03 共享质量 helper）：
    行内三态 ok/missing/invalid 原样沿用；旧行 NULL / status 声称 ok 但 epoch 不可用，
    一律 ``unknown``，宁可保守也不编时间。
    """
    return read_stored_pubdate(
        getattr(stats, "pubdate_epoch_s", None), getattr(stats, "pubdate_status", None)
    )


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
    # 08 案 §J3 第 6 条（B6b）：同一 bvid 读同一 watch 发现时间，作为 Snapshot 的
    # first_seen_epoch_s（**不是** Video.created_at）。无 watch 即 None，不因此创建 watch。
    watch = session.query(HotspotWatch).filter(HotspotWatch.bvid == clean_bvid).first()
    first_seen_epoch_s = (
        watch.first_seen_epoch_s
        if watch is not None and type(watch.first_seen_epoch_s) is int and watch.first_seen_epoch_s >= 0
        else None
    )
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
        pubdate_epoch_s, pubdate_status = _resolve_stored_pubdate(stats)
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
                pubdate_epoch_s=pubdate_epoch_s,
                pubdate_status=pubdate_status,
                first_seen_epoch_s=first_seen_epoch_s,
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

    代际口径（批 3.5 · 改动一）：在 ``_collect_evaluate_commit`` 里本函数与 ``commit_state`` 处于
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


def _build_detector(
    initial_states: dict[str, TrendState],
    as_of_epoch_s: int,
    *,
    domain: str = "default",
) -> LifecycleV2:
    """缺省算法构造器：用批 1 的 ``LifecycleV2`` 并以历史状态续算（不冷启动）。

    Args:
        initial_states: ``bvid -> TrendState`` 的续算起点。
        as_of_epoch_s: 计算截止时刻（UTC 秒）。
        domain: 领域名，透传给 v2 供后续分桶阈值；缺省 ``"default"``（与改前一致）。

    Returns:
        LifecycleV2: 已注入历史状态的算法实例。
    """
    return LifecycleV2(
        domain=domain, as_of_epoch_s=as_of_epoch_s, initial_states=initial_states
    )


# --------------------------------------------------------------------- 编排层


class WatchService:
    """单视频跟踪编排层：一轮 tick = 先清 → 捞 → （领 → 采 → 评 → 写 → 排）× N。"""

    def __init__(
        self,
        *,
        collector_port: Any | None = None,
        session_factory: Callable[[], Session] | None = None,
        detector_factory: Callable[[dict[str, TrendState], int], LifecycleV2] | None = None,
        domain: str = "default",
        snapshot_loader: Callable[[Session, str], list[Snapshot]] | None = None,
        now_fn: Callable[[], int] = utc_now_epoch_s,
        now_mono_fn: Callable[[], float] = time.monotonic,
        pool: WatchPool | None = None,
        budget: Any | None = None,
        demand_reconcile_hook: Callable[..., Any] | None = None,
    ) -> None:
        """构造编排层（全部依赖可注入，便于单测与离线回放）。

        Args:
            collector_port: 采集端口（提供 ``async collect(bvid, *, collection_tid, source)``）；
                缺省时**惰性**构造真实 ``BilibiliAPI`` + ``HotspotCollector`` 端口。
            session_factory: 会话工厂；缺省用 ``core.database.get_session``。
            detector_factory: 算法构造器 ``(initial_states, as_of_epoch_s) -> LifecycleV2``；
                缺省用批 1 的 ``LifecycleV2``。**保持两参契约**，本层不向其追加必填参数。
            domain: 领域名（Step 2 domain 管道）；缺省 ``"default"``。仅缺省构造器据此把
                domain 透传给 ``LifecycleV2``；注入 ``detector_factory`` 时由注入方自理。
            snapshot_loader: 历史快照读取器 ``(session, bvid) -> list[Snapshot]``；
                缺省用本模块 :func:`load_bvid_snapshots`。
            now_fn: 时钟，返回 UTC 秒级 int；缺省 ``utc_now_epoch_s``。
            now_mono_fn: 单调时钟（``time.monotonic`` 口径），只喂 :class:`RequestBudget`；
                缺省 ``time.monotonic``。**绝不**把它当 epoch 落库。
            pool: watch 池有界准入控制器（第二批 A）；缺省惰性构造默认实例。
            budget: ``RequestBudget``（或同契约替身），第二批 F 的类别预算闸门；
                缺省 None = 不做预算门（保持既有 tick 行为）。
            demand_reconcile_hook: 每轮 tick 开头调一次的需求整编 hook，签名与 04 侧
                ``EventWatchDemandReconciler.reconcile`` 一致（``(session, *, now_s)``）；
                由组装处注入；**缺省 None = 不调用，行为与现状逐字节一致**。flush-only，
                与本轮调度同处一个短事务，随末尾 commit 一并落库。
        """
        self._collector_port = collector_port
        self._session_factory = session_factory or get_session
        self._domain = domain
        # 保持 ``detector_factory`` 的两参契约 ``(initial_states, as_of_epoch_s)``：注入方
        # 无需感知 domain；仅缺省构造器把 domain 透传给 ``LifecycleV2``（缺省值仍是
        # "default"，故缺省构造与改前逐字节一致）。
        self._detector_factory = detector_factory or partial(_build_detector, domain=self._domain)
        self._snapshot_loader = snapshot_loader or load_bvid_snapshots
        self._now_fn = now_fn
        self._now_mono_fn = now_mono_fn
        self._pool = pool
        self._budget = budget
        self._demand_reconcile_hook = demand_reconcile_hook
        # 07 案 §9.2：单实例保存「下次起始类别」即可（持久化不是必需）；每次真正授予后推进游标，
        # 被拒绝不永久霸占优先权。缺省起始类别取轮转环首位（fast_watch）。
        self._select_rotation: str = BUDGET_ROTATION_ORDER[0]
        # 07 案 §6：实例级 asyncio.Lock 串行**同实例**的 run_tick，避免两个 tick 并发消费同一批
        # 逐目标 Admission（reserve/redeem 是同步非阻塞临界区，但两个 tick 交替执行仍会交错）。
        # **边界**：跨进程 / 跨实例的严格 exactly-once 不在本批次承诺内——claim_revision 只是读取代际，
        # 不是数据库排他领取；单 web worker 部署下本锁已覆盖生产监控层的唯一实例。
        self._tick_lock: asyncio.Lock = asyncio.Lock()

    # ---- 依赖 ----

    @property
    def collector_port(self) -> Any:
        """采集端口；未显式注入时惰性构造（导入期不建客户端、不发请求）。

        缺省构造复用编排层自身的 ``budget``，使采集端口与 tick 预算门共用同一预算实例。
        """
        if self._collector_port is None:
            self._collector_port = default_collector_port(self._budget)
        return self._collector_port

    def _require_admission_capable_port(self) -> None:
        """有预算的 watch 路径要求端口实现单请求准入协议，否则显式报错。

        Raises:
            BudgetWiringError: 端口未提供 ``collect_admitted`` 时抛出（不静默绕过预算）。
        """
        port = self.collector_port
        if getattr(port, "collect_admitted", None) is None:
            raise BudgetWiringError(
                "有预算的 watch 路径要求端口实现 collect_admitted（单请求准入协议）；"
                f"当前端口 {type(port).__name__} 不支持，拒绝静默绕过预算。"
            )

    def _admission_now_mono(self, budget: Any | None) -> float:
        """取本轮准入用的统一单调时刻：优先用预算自身的 clock，保持 peek / reserve /
        redeem(issuer.clock()) 三者同一时钟（07 案 §7.5）。

        Args:
            budget: 本轮活动预算（可为 None）。

        Returns:
            float: 单调时钟读数。
        """
        clock = getattr(budget, "clock", None)
        if callable(clock):
            try:
                return float(clock())
            except Exception:  # noqa: BLE001 - 替身 clock 异常时回退到编排层时钟
                pass
        return float(self._now_mono_fn())

    @property
    def pool(self) -> WatchPool:
        """watch 池有界准入控制器；未显式注入时惰性构造默认实例（第二批 A）。"""
        if self._pool is None:
            self._pool = WatchPool()
        return self._pool

    @property
    def budget(self) -> Any | None:
        """预算对象（可为 None；None 表示不做类别预算门）。"""
        return self._budget

    @property
    def demand_reconcile_hook(self) -> Callable[..., Any] | None:
        """每轮需求整编 hook；None 表示不接线（行为与现状一致，4e）。"""
        return self._demand_reconcile_hook

    def logical_policy_snapshot(self) -> dict:
        """返回 watch **L 层**逻辑调度策略与预算的只读观测快照（07 案 §10.3）。

        口径说明：
        - 逻辑窗口用 ``monotonic``、HTTP 窗口用 ``epoch``，**不混时间基准**；这里只报告逻辑口径；
        - ``category_consumption``（累计）**不是** 24h 当前可用量，故只报 ``category_used``（窗口内占用）；
        - 预算为 None（legacy / 未接线）时如实标 ``logical_mode='unconfigured'``、``category_isolation=False``；
        - 这是**内存**逻辑账本，不冒充跨进程持久限额，也不代表 H 层 HTTP 父额度。

        Returns:
            dict: 含 ``logical_policy_version`` / ``logical_mode`` / ``category_isolation`` /
            ``logical_global_used`` / ``logical_category_used`` / ``reserved_admissions`` 等字段。
        """
        budget = self._budget
        if budget is None:
            return {
                "logical_policy_version": None,
                "logical_mode": "unconfigured",
                "category_isolation": False,
                "logical_global_used": None,
                "logical_category_used": None,
                "reserved_admissions": None,
                "http_quota_scope": "domain/category/hour_bucket",
                "http_parent_category": "watch",
            }
        now_mono = self._admission_now_mono(budget)
        try:
            snapshot = budget.snapshot(now_mono) if hasattr(budget, "snapshot") else {}
        except Exception:  # noqa: BLE001 - 观测快照失败绝不上抛，退化为粗粒度字段
            snapshot = {}
        category_limits = getattr(budget, "category_limits", None)
        return {
            "logical_policy_version": snapshot.get(
                "policy_version", getattr(budget, "policy_version", None)
            ),
            "logical_mode": snapshot.get(
                "mode", "partitioned" if category_limits is not None else "shared"
            ),
            "category_isolation": bool(
                snapshot.get("category_isolation", category_limits is not None)
            ),
            "logical_global_used": snapshot.get("global_used_60s"),
            "logical_category_used": snapshot.get("category_used"),
            "reserved_admissions": snapshot.get("reserved"),
            "http_quota_scope": "domain/category/hour_bucket",
            "http_parent_category": "watch",
        }

    # ---- 需求整编（04 联合前置 P3 · flush-only）----

    def admit(
        self,
        session: Session,
        *,
        bvid: str,
        now_s: int,
        ttl_end_epoch_s: int | None = None,
        category_key: str | None = None,
        collection_tid: int | None = None,
        discovery_source: str | None = None,
        sample_interval_s: int = DEFAULT_SAMPLE_INTERVAL_S,
        next_due_epoch_s: int | None = None,
    ) -> AdmissionResult:
        """容量检查后准入一个 bvid（第二批 A）：超上限返回 ``queued_capacity``。

        Args:
            session: 调用方会话；只 flush，提交由调用方完成。
            bvid: 目标 BV 号。
            now_s: 本次准入时刻（UTC 秒）。
            ttl_end_epoch_s / category_key / collection_tid / discovery_source: 元信息。
            sample_interval_s: 采样间隔（秒）。
            next_due_epoch_s: 首采下次应采样时刻。

        Returns:
            AdmissionResult: 见 :class:`modules.hotspot.watch_queue.AdmissionResult`。
        """
        return self.pool.try_admit(
            session,
            bvid=bvid,
            now_epoch_s=now_s,
            ttl_end_epoch_s=ttl_end_epoch_s,
            category_key=category_key,
            collection_tid=collection_tid,
            discovery_source=discovery_source,
            sample_interval_s=sample_interval_s,
            next_due_epoch_s=next_due_epoch_s,
        )

    def drain_admissions(self, session: Session, *, now_s: int) -> dict:
        """把待入队队列里仍有效的项在有空位时放进池，并淘汰超 deadline 的项（第二批 A）。

        Args:
            session: 调用方会话；只 flush。
            now_s: 本次放行时刻（UTC 秒）。

        Returns:
            dict: 见 :meth:`modules.hotspot.watch_queue.WatchPool.drain`。
        """
        return self.pool.drain(session, now_epoch_s=now_s)

    def demand_eligibility(self, session: Session, bvid: str) -> str:
        """现算某 bvid 的准入派生 reason（第二批 E；不落任何新列）。

        Args:
            session: 调用方会话。
            bvid: 目标 BV 号。

        Returns:
            str: ``manual_stop`` 的行返回 ``blocked_by_user``；否则见
            :func:`modules.hotspot.watch_demand.demand_reason`；行不存在返回 ``released``。
        """
        clean_bvid = str(bvid or "").strip()
        if not clean_bvid:
            return "released"
        stmt = text(
            "SELECT active, stop_reason, source_demands FROM hotspot_watch WHERE bvid = :bvid"
        )
        row = session.execute(stmt, {"bvid": clean_bvid}).first()
        if row is None:
            return "released"
        return demand_reason(
            active=bool(row[0]), stop_reason=row[1], source_demands=_json_to_obj(row[2])
        )

    def reconcile_demands(
        self, session: Session, *, namespace: str, desired: dict, now_s: int
    ) -> None:
        """按命名空间整编 ``source_demands``：只替换该命名空间，保留其他命名空间。

        语义（照 04 §3.4 原文）：

        - ``namespace`` 限 ``manual`` / ``ranking`` / ``events``，其他值直接 ``ValueError``；
        - ``desired`` 是**该命名空间的完整当前快照**：本轮没传进来的旧需求即「已撤销」，
          必须从 ``source_demands`` 里移除，**不许永久残留**；``events`` 按 ``event_id`` 分组；
        - 只替换该命名空间：``manual`` / ``ranking`` 等其它命名空间的子快照一字不动；
        - ``session`` 由调用方传入并持有短事务：本方法**只 flush**，
          **不** commit / rollback / close，也**不发任何网络**；
        - 需求落 ``hotspot_watch.source_demands`` JSON 列（该列由 04 幂等迁移补齐）。

        需求驱动的节奏重算（第二批 B，照 §6.3 L681 / §8.3 L682）：

        - 该命名空间快照**发生变化**的行，重算 ``sample_interval_s`` 与 ``next_due_epoch_s``：
          仍有需求 -> 取当前所有命名空间的最小间隔；无任何需求 -> 停采（``active=0``，
          ``stop_reason='events_revoked'``），但**保留全部历史**（快照 / 阶段 / coverage）；
        - ``active=0 AND stop_reason='manual_stop'`` 的行优先级最高：**不重开、不重排**（第二批 E）；
        - 重算只动调度 / 生命周期列，**不清** 02 阶段历史、原始快照或其它命名空间需求；
        - 任何需求变化都推进 ``state_revision``，作为迟到写入的围栏（第二批 C）。

        边界：``desired`` 里没有对应 watch 行的 bvid 本轮不落库（建行仍属
        ``upsert_watch`` / 发现入库的职责）。

        Args:
            session: 调用方持有的 SQLAlchemy 会话（短事务，本方法只 flush）。
            namespace: 需求命名空间，限 ``manual`` / ``ranking`` / ``events``。
            desired: 该命名空间的完整需求快照。``events`` 形如
                ``{"<event_id>": {"bvid": "BV..."}}`` 或 ``{"<event_id>": {"bvids": [...]}}``；
                ``manual`` / ``ranking`` 形如 ``{"<bvid>": {...}}``（键即 bvid）。
            now_s: 本次整编时刻（UTC 秒级整数 epoch）。

        Returns:
            无。

        Raises:
            ValueError: ``namespace`` 非法、``desired`` 非 dict / 条目非法、``now_s`` 非法，
                或 ``events`` 条目既无 ``bvid`` 也无 ``bvids``。
        """
        if namespace not in DEMAND_NAMESPACES:
            raise ValueError(f"invalid_namespace:{namespace}")
        if not isinstance(desired, dict):
            raise ValueError("invalid_desired")
        now_epoch_s = _require_epoch_s(now_s)

        # 归一化：bvid -> {本次该命名空间下的需求键 -> 需求描述}
        by_bvid = self._normalize_desired(namespace, desired)

        rows = self._load_demand_rows(session, list(by_bvid.keys()))
        for row_id, bvid, payload, active, stop_reason in rows:
            current = payload if isinstance(payload, dict) else {}
            incoming = by_bvid.get(bvid)
            merged = dict(current)
            if incoming:
                merged[namespace] = incoming
            else:
                merged.pop(namespace, None)  # 撤销：本轮快照里已消失的需求
            if merged == current:
                continue  # 无变化不写，避免无谓 UPDATE
            self._write_source_demands(session, row_id, merged or None)
            # 需求变了 -> 立刻重算该行节奏（第二批 B）
            self._apply_rhythm(
                session,
                bvid=bvid,
                merged=merged,
                active=active,
                stop_reason=stop_reason,
                now_s=now_epoch_s,
            )
        session.flush()
        logger.debug(
            "reconcile_demands namespace=%s now_s=%s 目标bvid数=%s 命中行数=%s",
            namespace,
            now_epoch_s,
            len(by_bvid),
            len(rows),
        )

    def _apply_rhythm(
        self,
        session: Session,
        *,
        bvid: str,
        merged: dict,
        active: bool,
        stop_reason: Any,
        now_s: int,
    ) -> None:
        """按 ``merged`` 重算某行的采样节奏（第二批 B/E）；只动调度 / 生命周期列。

        Args:
            session: 调用方会话；只 flush。
            bvid: 目标 BV 号。
            merged: 该行重算后的完整需求快照。
            active: 该行当前 ``active``。
            stop_reason: 该行当前 ``stop_reason``。
            now_s: 重算时刻（UTC 秒）。

        Returns:
            无。
        """
        # manual_stop 优先级最高：不改 active、不重排、不自动重开（第二批 E）。
        if (not active) and isinstance(stop_reason, str) and stop_reason == "manual_stop":
            logger.info("reconcile 跳过 manual_stop 行（blocked_by_user） bvid=%s", bvid)
            return
        interval = resolve_interval_s(merged)
        if interval is None:
            # 无任何需求 -> 停采；行保留、历史全留（第二批 B/D）。
            if active:
                release_watch(
                    session, bvid, now_epoch_s=now_s, stop_reason=STOP_REASON_EVENTS_REVOKED
                )
            return
        if not active:
            # 已释放（非 manual_stop）但当前有需求：按需求重新入池并排程。
            reactivate_watch(
                session,
                bvid,
                now_epoch_s=now_s,
                next_due_epoch_s=now_s + interval,
                sample_interval_s=interval,
            )
            return
        reschedule_watch(session, bvid, now_epoch_s=now_s, sample_interval_s=interval)

    # ---- 启动清理（第二批 D）----

    def recover_on_startup(self, session: Session, *, now_s: int) -> dict:
        """应用重启清理：撤遗留 events 需求、重算节奏、清孤儿快采（第二批 D / E49）。

        照 E49「04关闭后事件服务已停止再重启应用 | 启动清理遗留 events 需求并重算节奏；
        manual/ranking 继续，无孤儿快采」：

        - 逐行移除 ``source_demands`` 里的 ``events`` 命名空间，并按剩余需求重算节奏：
          仍有 manual / ranking -> 退回其原节奏（``active=1`` 保留）；无任何需求 -> 停采
          （``active=0``，``stop_reason='events_revoked'``），**历史全留**；
        - ``manual_stop`` 行不重开、不重排（派生 reason=``blocked_by_user``）；
        - 把主表所有 ``fast_until_s`` 置空：重启后不得留下不属于任何有效 panel 的孤儿快采。

        本方法只 flush，提交由调用方短事务完成；不改任何评估历史 / 原始快照。

        Args:
            session: 调用方会话。
            now_s: 本次恢复时刻（UTC 秒）。

        Returns:
            dict: ``events_cleared`` / ``released`` / ``rescheduled`` / ``blocked`` / ``fast_cleared``。
        """
        now_epoch_s = _require_epoch_s(now_s)
        summary = {
            "events_cleared": 0,
            "released": 0,
            "rescheduled": 0,
            "blocked": 0,
            "fast_cleared": 0,
        }
        for row_id, bvid, payload, active, stop_reason in self._load_demand_rows(session, []):
            current = payload if isinstance(payload, dict) else {}
            if "events" not in current:
                continue
            # manual_stop 优先：撤掉遗留的 events 需求，但绝不重开 / 重排（`_apply_rhythm` 会跳）。
            blocked = (not active) and isinstance(stop_reason, str) and stop_reason == "manual_stop"
            merged = dict(current)
            merged.pop("events", None)
            self._write_source_demands(session, row_id, merged or None)
            summary["events_cleared"] += 1
            self._apply_rhythm(
                session,
                bvid=bvid,
                merged=merged,
                active=active,
                stop_reason=stop_reason,
                now_s=now_epoch_s,
            )
            if blocked:
                summary["blocked"] += 1
            elif not has_demands(merged) and active:
                summary["released"] += 1
            else:
                summary["rescheduled"] += 1
        summary["fast_cleared"] = self._clear_orphan_fast(session)
        session.flush()
        logger.info(
            "watch 启动清理：events撤=%s 停采=%s 重排=%s manual_stop拦截=%s 清快采=%s",
            summary["events_cleared"],
            summary["released"],
            summary["rescheduled"],
            summary["blocked"],
            summary["fast_cleared"],
        )
        return summary

    @staticmethod
    def _clear_orphan_fast(session: Session) -> int:
        """把 ``hotspot_watch.fast_until_s`` 全部置空（清孤儿快采，第二批 D）。

        ``fast_until_s`` 由 04 迁移补齐，ORM 模型未声明；缺列（未跑迁移的库）时静默跳过。

        Args:
            session: 调用方会话；只 flush。

        Returns:
            int: 被置空的行数；缺列 / 异常时为 0。
        """
        try:
            inspector = sa_inspect(session.get_bind())
            if not inspector.has_table("hotspot_watch"):
                return 0
            columns = {col["name"] for col in inspector.get_columns("hotspot_watch")}
            if "fast_until_s" not in columns:
                return 0
            result = session.execute(
                text("UPDATE hotspot_watch SET fast_until_s = NULL WHERE fast_until_s IS NOT NULL")
            )
            session.flush()
            return int(result.rowcount or 0)
        except Exception:  # noqa: BLE001 - 清理快采失败不许炸启动
            logger.exception("清孤儿快采失败")
            return 0

    @staticmethod
    def _normalize_desired(namespace: str, desired: dict) -> dict:
        """把 ``desired`` 归一化成 ``bvid -> {需求键: 需求描述}``。

        Args:
            namespace: 已校验的命名空间。
            desired: 该命名空间的完整快照。

        Returns:
            dict: ``bvid -> {需求键: 需求描述}``。

        Raises:
            ValueError: 条目不是 dict，或 ``events`` 条目缺 bvid / bvids。
        """
        by_bvid: dict = {}
        for key, descriptor in desired.items():
            if descriptor is None:
                descriptor = {}
            if not isinstance(descriptor, dict):
                raise ValueError("invalid_desired_entry")
            demand_key = str(key)
            for bvid in _demand_bvids(namespace, demand_key, descriptor):
                by_bvid.setdefault(bvid, {})[demand_key] = descriptor
        return by_bvid

    @staticmethod
    def _load_demand_rows(session: Session, bvids: list) -> list:
        """读出「可能受影响」的行：``source_demands`` 非空的行 + 本次 desired 覆盖的 bvid 行。

        需求列是 JSON 文本，无法直接 SQL 过滤命名空间，故先把候选行读出来在 Python 侧整编；
        候选集由「已有需求的行」与「本轮 desired 涉及的行」两部分并集界定，足以覆盖「新增 /
        更新 / 撤销」三种情形。

        Args:
            session: 调用方会话。
            bvids: 本轮 desired 覆盖的 bvid 列表。

        Returns:
            list[tuple[int, str, Any, bool, Any]]:
            ``(id, bvid, source_demands 原值, active, stop_reason)``。
        """
        out: dict = {}
        base = text(
            "SELECT id, bvid, source_demands, active, stop_reason FROM hotspot_watch "
            "WHERE source_demands IS NOT NULL"
        )
        for row in session.execute(base).all():
            out[int(row[0])] = (
                int(row[0]),
                str(row[1]),
                _json_to_obj(row[2]),
                bool(row[3]),
                row[4],
            )
        wanted = [str(b).strip() for b in bvids if str(b).strip()]
        if wanted:
            stmt = text(
                "SELECT id, bvid, source_demands, active, stop_reason FROM hotspot_watch "
                "WHERE bvid IN :bvids"
            ).bindparams(bindparam("bvids", expanding=True))
            for row in session.execute(stmt, {"bvids": wanted}).all():
                out[int(row[0])] = (
                    int(row[0]),
                    str(row[1]),
                    _json_to_obj(row[2]),
                    bool(row[3]),
                    row[4],
                )
        return list(out.values())

    @staticmethod
    def _write_source_demands(session: Session, row_id: int, payload: Any) -> None:
        """把整编后的需求写回 ``source_demands``（``payload`` 为 None 表示清空）。

        Args:
            session: 调用方会话。
            row_id: ``hotspot_watch.id``。
            payload: 待写入的 Python 对象；None 表示置空。

        Returns:
            无。
        """
        session.execute(
            text("UPDATE hotspot_watch SET source_demands = :payload WHERE id = :row_id"),
            {
                "payload": None
                if payload is None
                else json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "row_id": int(row_id),
            },
        )

    # ---- 对外入口 ----

    async def run_tick(
        self, *, limit: int = DEFAULT_TICK_LIMIT, budget: Any | None = None
    ) -> TickResult:
        """跑一轮完整 tick（顺序：先清 → 捞 → 领 → 采 → 评 → 写 → 排）。

        串行（07 执行案 §6）：本方法先取**实例级** ``_tick_lock``，再执行一轮 tick。同一
        ``WatchService`` 实例上的并发调用会被串行化，避免两个 tick 并发消费同一批逐目标
        Admission。**边界**：跨进程 / 跨实例的严格 exactly-once **不在本批次承诺内** ——
        ``watch_store.claim_revision`` 只是读取代际，不是数据库排他领取；本锁只在单实例内生效。

        Args:
            limit: 本轮最多处理多少个目标（>=1）。
            budget: 预算对象（第二批 F）；None 时回退构造注入的 ``budget``；两者都 None =
                不做类别预算门（保持既有 tick 行为）。

        Returns:
            TickResult: 本轮结构化结果。
        """
        async with self._tick_lock:
            return await self._run_tick_locked(limit=limit, budget=budget)

    async def _run_tick_locked(
        self, *, limit: int, budget: Any | None = None
    ) -> TickResult:
        """:meth:`run_tick` 的实际执行体（调用时已持有实例级 ``_tick_lock``）。

        目标级异常在此被隔离并计入 ``TickResult.failed``；调度阶段（先清 / 捞）的
        基础设施异常会原样上抛，由 :meth:`_loop` 兜住后继续下一轮。

        Args:
            limit: 本轮最多处理多少个目标（>=1）。
            budget: 本轮活动预算对象；None 时回退构造注入的 ``budget``。

        Returns:
            TickResult: 本轮结构化结果。
        """
        now_epoch_s = int(self._now_fn())
        result = TickResult(now_epoch_s=now_epoch_s)
        active_budget = budget if budget is not None else self._budget
        # 统一准用单调时刻：selector 的 peek 与逐目标 reserve 用同一个 now，collector
        # 端 redeem 用 issuer.clock()，三者同一时钟（07 案 §7.5）。
        now_mono = self._admission_now_mono(active_budget)

        # ---- 先清 + 1. 捞：同一事务内「到期先归档、再捞该评估的」，
        #      保证本轮被释放的行不会出现在本轮调度结果里。
        session = self._session_factory()
        try:
            # ---- 可选：每轮开头做一次需求整编（4e 快采节奏接线）----
            # 组装处注入 04 侧 EventWatchDemandReconciler.reconcile（flush-only）；默认 None 时
            # 整段跳过 → 与既有 tick 行为逐字节一致。与本轮调度同处一个短事务，随末尾 commit 落库。
            if self._demand_reconcile_hook is not None:
                self._demand_reconcile_hook(session, now_s=now_epoch_s)
            result.released = release_expired(session, now_epoch_s)
            selection = self._select_targets(
                session, now_epoch_s, limit, budget=active_budget, now_mono=now_mono
            )
            targets = selection.targets
            result.budget_skipped = selection.budget_skipped
            result.retry_delay_s = selection.retry_delay_s
            result.budget_exhausted = selection.budget_exhausted
            result.candidates_considered = selection.candidates_considered
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("watch tick 调度阶段失败（先清 / 捞）")
            raise
        finally:
            session.close()

        result.due_count = len(targets)
        if not targets:
            logger.info(
                "watch tick 空转：now_epoch_s=%s 无该评估的目标（预算跳过=%s 预算耗尽=%s）",
                now_epoch_s,
                result.budget_skipped,
                result.budget_exhausted,
            )
            return result

        # 有预算的 watch 路径要求端口实现单请求准用协议；缺能力显式报错，不静默绕过。
        if active_budget is not None:
            self._require_admission_capable_port()

        # 08 案 §H4（R2）：不再整轮构造一个 detector。detector 改在
        # ``_collect_evaluate_commit`` 内、该目标采集完成且加载快照之后**逐目标**构造，
        # 评估截止取那一刻的墙钟 —— 否则本轮刚采到的点 captured_epoch_s 晚于轮开始时刻
        # 会被当未来点排掉，最新采样永远晚一轮才生效。
        # 07 案预算 / 凭证协议（reserve/redeem、admitted <= limit、补选池）一律不动。
        queue = list(targets)
        reserve_pool = list(selection.overflow)

        # 逐目标 reserve，紧邻单目标执行：**绝不**预先 reserve 整批；admitted 恒 <= limit。
        while queue and result.admitted < limit:
            target = queue.pop(0)
            # §9.3 第 2 步：先用**独立短 session**重检 active/ttl/next_due/fast_until 并读 claim
            # revision，随即将 session 关闭；随后 reserve 与 redeem 之间**不夹任何** DB session / 事务。
            claim = self._claim_short(target.bvid, now_epoch_s)
            if claim is None:
                # 目标本轮执行前已失效（被释放 / TTL 到期 / 未到点）：不 reserve、不发 HTTP、
                # 不计失败也不计延期；有界补选一个候选（仍受 admitted <= limit 约束）。
                if reserve_pool:
                    queue.append(reserve_pool.pop(0))
                continue
            admission = None
            if active_budget is not None:
                # §9.3 第 3 步：按**真实当前类别** reserve（fast_until 期间变化时不拿旧分类扣错窗），
                # 且不在目标内部等待（reserve 同步非阻塞；额度等待一律在事务外完成）。
                admission_result = active_budget.reserve(
                    claim.category, now_mono, operation_key=target.bvid
                )
                if not admission_result.decision.granted:
                    # 容量延期（global / category 不足）：记 budget_deferred，**不是**平台失败
                    # ——不增 failure_count、不触发退避、不计平台风控（07 案 §9.3 / §7.5）。
                    result.budget_deferred += 1
                    result.budget_deferred_by_category[claim.category] = (
                        result.budget_deferred_by_category.get(claim.category, 0) + 1
                    )
                    # 有界补选（07 案 §9.2）：状态变化导致 reserve 被拒时，从补选池补一个候选；
                    # 仍受 ``admitted <= limit`` 约束，绝不放大单轮处理量。
                    if reserve_pool:
                        queue.append(reserve_pool.pop(0))
                    continue
                admission = admission_result.admission
            result.admitted += 1
            try:
                outcome = await self._collect_evaluate_commit(
                    target,
                    claim=claim,
                    now_epoch_s=now_epoch_s,
                    admission=admission,
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
            finally:
                # 未兑换的 reservation 在此释放；已兑换时 release 是 no-op（不退款）。
                if admission is not None:
                    active_budget.release_unused(admission)
            result.collected += 1
            result.detections.extend(outcome.detections)
            if outcome.status == "committed":
                result.committed += 1
            else:
                result.dropped += 1

        logger.info(
            "watch tick: now_epoch_s=%s 释放=%s 到点=%s 候选=%s 准入=%s 写回=%s 丢弃=%s 失败=%s "
            "预算跳过=%s 预算延期=%s 预算延期类别=%s",
            result.now_epoch_s,
            result.released,
            result.due_count,
            result.candidates_considered,
            result.admitted,
            result.committed,
            result.dropped,
            result.failed,
            result.budget_skipped,
            result.budget_deferred,
            result.budget_deferred_by_category,
        )
        return result

    async def _loop(
        self,
        *,
        interval_s: int = DEFAULT_LOOP_INTERVAL_S,
        stop_event: asyncio.Event | None = None,
        limit: int = DEFAULT_TICK_LIMIT,
        budget: Any | None = None,
    ) -> None:
        """常驻调度循环：每 ``interval_s`` 秒跑一轮 tick，单轮异常只记日志、不中断循环。

        ``core/monitor_service._quota_housekeeping`` 的注释已约定：评论常驻服务保持评论
        职责不放 watch 采样循环，「先清后调」由本方法承担。本批只提供循环入口，**不改**
        ``core/`` 任何存量文件；是否挂到全局调度由后续整合批次决定。

        预算公平（第二批 F，照 §6.3 L630）：每轮只从**当前可授予预算**的类别里选 due 项，
        某类别配额用尽立即跳过，**绝不**在唯一 watch 循环里死等 fast 额度而挡住仍可运行的
        normal；只有「无类别可运行」时才等最早 ``retry_at`` 或 ``stop_event``。

        Args:
            interval_s: 轮询间隔（秒，>=1）。
            stop_event: 外部停止信号；缺省新建一个永不触发的 Event。
            limit: 每轮最多处理的目标数。
            budget: 预算对象；None 时回退构造注入的 ``budget``。

        Returns:
            无。
        """
        stop_event = stop_event or asyncio.Event()
        interval_s = max(1, int(interval_s))
        active_budget = budget if budget is not None else self._budget
        while not stop_event.is_set():
            wait_s = float(interval_s)
            try:
                result = await self.run_tick(limit=limit, budget=active_budget)
                wait_s = self._compute_wait_s(interval_s=interval_s, result=result)
            except Exception as exc:  # noqa: BLE001 - 单轮失败不允许中断常驻循环
                logger.exception("watch tick 异常，继续下一轮: %s", exc)
            if wait_s <= 0:
                # 兜底：绝不让唯一循环忙转（理论不可达，被拒时 retry_at 必在未来）。
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                continue

    @staticmethod
    def _compute_wait_s(*, interval_s: float, result: TickResult) -> float:
        """算下一轮等待秒数（纯函数，便于用假时钟直接断言，第二批 F）。

        规则（照 §6.3 L630）：无类别可运行（所有到点项都被预算挡住）-> 等最早 ``retry_at``；
        否则 -> 等正常轮询间隔。

        Args:
            interval_s: 正常轮询间隔（秒）。
            result: 本轮 tick 结果。

        Returns:
            float: 下一轮等待秒数（>=0）。
        """
        if result.budget_exhausted and result.retry_delay_s is not None:
            return max(0.0, float(result.retry_delay_s))
        return max(0.0, float(interval_s))

    # ---- 步骤 1：捞 ----

    def _load_targets(
        self, session: Session, now_epoch_s: int, limit: int
    ) -> list[WatchTarget]:
        """1. 捞：读该评估的 watch 行，抽出脱离 session 仍可用的目标（不做预算门）。

        Args:
            session: 调度会话。
            now_epoch_s: 本轮时刻（UTC 秒）。
            limit: 单轮上限（>=1）。

        Returns:
            list[WatchTarget]: 按 ``next_due_epoch_s`` 升序的目标列表。
        """
        rows = find_due_for_eval(session, now_epoch_s, limit=max(1, int(limit)))
        return [self._build_target(row) for row in rows]

    def _select_targets(
        self,
        session: Session,
        now_epoch_s: int,
        limit: int,
        *,
        budget: Any | None = None,
        now_mono: float | None = None,
    ) -> _Selection:
        """1. 捞（预算感知版，07 案 §9.2）：**只做无消费的候选排序**，类间轮转。

        选取规则：

        - 先**按类分别取候选**：``find_due_for_budget_category`` 两类各自最多读 ``limit`` 条
          （``fast_until_s`` 生效期归 ``fast_watch``、否则归 ``normal_watch``），稳定排序
          ``(next_due_epoch_s, bvid)``；大池不无界 ``all()``，也不再靠 ``limit * 8`` 假装看见全部类。
        - 调 ``RequestBudget.peek(kind, now_mono)``：**只读不记账**，只看该类别此刻是否仍可放行；
          某类本类额度不可授予时**跳过该类**（不再探它后续候选），继续看另一类（不互相饿死）。
        - 按类 FIFO + 类间轮转把候选排成有序表：起始类别取实例游标 ``_select_rotation``，
          **每次真正授予后**把游标推进到另一类（被拒绝不永久霸占优先权）。
        - ``targets`` 只取前 ``limit`` 条；其余门控后的候选进 ``overflow`` 作有界补选池。
          **本轮真正处理 / admitted 的目标数恒 <= limit**，绝不把两类 ``2 * limit`` 整表塞进
          ``run_tick``；两类分别取 ``limit`` 只是「候选读取上限」，不是各自允许处理 ``limit``。
        - **本函数不再扣费**：真正占用由 ``run_tick`` 逐目标 ``reserve`` 完成。
        - ``budget`` 为 None 时退化为不做预算门（保持既有 tick 行为）。

        Args:
            session: 调度会话。
            now_epoch_s: 本轮时刻（UTC 秒）。
            limit: 单轮上限（>=1）。
            budget: 预算对象或 None。
            now_mono: 单调时钟读数；None 时用 ``now_mono_fn``。

        Returns:
            _Selection: 入选目标 + 有界补选池 + 候选计数 + 预算跳过计数 + 最早可重试等待 + 是否耗尽。
        """
        limit = max(1, int(limit))
        if budget is None:
            targets = self._load_targets(session, now_epoch_s, limit)
            return _Selection(targets=targets, candidates_considered=len(targets))

        now_mono = float(self._now_mono_fn() if now_mono is None else now_mono)
        # 按类分别取候选（无消费；每类读取上限 = limit，不靠放大扫描倍数「碰巧通过」）。
        queues: dict[str, list] = {
            category: self._collect_category_candidates(
                session, now_epoch_s, category=category, limit=limit
            )
            for category in BUDGET_CATEGORIES
        }
        candidates_considered = sum(len(queue) for queue in queues.values())
        ordered, skipped, earliest_retry = self._interleave_by_rotation(queues, budget, now_mono)
        targets = ordered[:limit]
        # 已 peek 门控、但超出 limit 的候选：只作 reserve 被拒时的有界补选来源。
        overflow = ordered[limit : limit * 2]
        # 07 案 §9.2：每次真正「授予并锁定处理」后推进游标；被拒绝（未入选）不改游标、
        # 不永久霸占优先权。游标按**本轮最后一个入选目标**的类别取另一类。
        if targets:
            self._select_rotation = self._other_category(targets[-1].category)
        retry_delay_s = None
        if earliest_retry is not None:
            retry_delay_s = max(0.0, float(earliest_retry) - now_mono)
        return _Selection(
            targets=targets,
            overflow=overflow,
            budget_skipped=skipped,
            retry_delay_s=retry_delay_s,
            budget_exhausted=(not targets) and skipped > 0,
            candidates_considered=candidates_considered,
        )

    def _collect_category_candidates(
        self, session: Session, now_epoch_s: int, *, category: str, limit: int
    ) -> list:
        """按类取 due 候选（07 案 §9.1；走 ``watch_store`` 的按类查询，可被单测打桩）。

        Args:
            session: 调度会话。
            now_epoch_s: 本轮时刻（UTC 秒）。
            category: 预算类别（``normal_watch`` / ``fast_watch``）。
            limit: 本类读取上限（>=1）。

        Returns:
            list: ``hotspot_watch`` 行（按 ``(next_due_epoch_s, bvid)`` 升序）。
        """
        return list(
            find_due_for_budget_category(
                session, now_epoch_s, category=category, limit=max(1, int(limit))
            )
        )

    @staticmethod
    def _other_category(category: str) -> str:
        """返回轮转环里的另一类（本层只有 ``normal_watch`` / ``fast_watch`` 两类）。

        Args:
            category: 当前类别。

        Returns:
            str: 另一类别。
        """
        return (
            BUDGET_CATEGORY_NORMAL
            if category == BUDGET_CATEGORY_FAST
            else BUDGET_CATEGORY_FAST
        )

    def _interleave_by_rotation(
        self, queues: dict, budget: Any, now_mono: float
    ) -> tuple[list, int, float | None]:
        """按类 FIFO + 类间轮转把两类候选交错成有序表（``peek`` 只读门控，不占容量）。

        - 起始类别取实例游标 ``_select_rotation``；**每次真正授予**后把游标推进到另一类；
        - 某类本类额度不可授予时**跳过该类**（不再探它后续候选），继续另一类；
        - 两类候选都取尽 / 都跳过才停。

        Args:
            queues: ``{category: [rows...]}``（各类按 ``(next_due_epoch_s, bvid)`` 升序）。
            budget: 预算对象（提供 ``peek``）。
            now_mono: 单调时钟读数。

        Returns:
            ``(ordered_targets, skipped, earliest_retry_mono)``。
        """
        pos = {category: 0 for category in BUDGET_CATEGORIES}
        denied: set[str] = set()
        current = (
            self._select_rotation
            if self._select_rotation in BUDGET_CATEGORIES
            else BUDGET_ROTATION_ORDER[0]
        )
        ordered: list[WatchTarget] = []
        skipped = 0
        earliest_retry: float | None = None
        # 每处理一个候选，最多再花一次「切到另一类（该类已取尽 / 已跳过）再切回」的迭代，
        # 故上界取候选总数的 2 倍再留裕量；只用于兜底，不会真跑到这么多步。
        guard = 2 * sum(len(queue) for queue in queues.values()) + 2
        steps = 0
        while steps < guard:
            steps += 1
            if current in denied or pos[current] >= len(queues[current]):
                other = self._other_category(current)
                if other in denied or pos[other] >= len(queues[other]):
                    break
                current = other
                continue
            row = queues[current][pos[current]]
            pos[current] += 1
            # 只做非消费视图：peek 不占容量，也不授权发请求；真正占用在 run_tick reserve。
            decision = budget.peek(current, now_mono)
            if not getattr(decision, "granted", False):
                skipped += 1
                retry = getattr(decision, "retry_at_mono", None)
                if retry is not None:
                    earliest_retry = retry if earliest_retry is None else min(earliest_retry, retry)
                # 本类额度不可授予：跳过该类的其余候选，直接看另一类。
                denied.add(current)
                current = self._other_category(current)
                continue
            ordered.append(self._build_target(row, current))
            current = self._other_category(current)
        return ordered, skipped, earliest_retry

    @staticmethod
    def _build_target(row: Any, category: str = BUDGET_CATEGORY_NORMAL) -> WatchTarget:
        """把一行 ``hotspot_watch`` 抽成脱离 session 仍可用的目标。

        Args:
            row: ``hotspot_watch`` 行。
            category: 预算类别。

        Returns:
            WatchTarget。
        """
        sample_interval_s = getattr(row, "sample_interval_s", None)
        return WatchTarget(
            bvid=str(row.bvid),
            collection_tid=(int(row.collection_tid) if row.collection_tid is not None else None),
            sample_interval_s=(
                int(sample_interval_s)
                if type(sample_interval_s) is int and sample_interval_s > 0
                else DEFAULT_SAMPLE_INTERVAL_S
            ),
            initial_state=state_from_json(getattr(row, "state_json", None)),
            category=category,
        )

    # ---- 步骤 2..6：单目标处理 ----

    def _claim_short(self, bvid: str, now_epoch_s: int) -> "_TargetClaim | None":
        """§9.3 第 2 步：用**独立短 session**重检并读 claim，返回后立即关闭。

        重检内容：``active`` / ``ttl_end_epoch_s`` / ``next_due_epoch_s`` / ``fast_until_s``，
        并读取当前 ``state_revision`` 作为 claim。返回的 ``category`` 是**真实当前类别**
        （``fast_until_s`` 生效期为 ``fast_watch``，否则 ``normal_watch``）——``run_tick`` 用它
        reserve，避免拿旧分类扣错窗。行已失效（不存在 / 非 active / TTL 到期 / 未到点）返回 None：
        **不 reserve、不发 HTTP、不计失败**（07 案 §9.3 / §9.4）。

        关键不变量：本方法在 ``reserve`` **之前**完成，且 session 在此关闭；因此 ``reserve`` 与
        ``redeem`` 之间不夹任何 DB session / 事务（预算等待一律在事务外）。

        Args:
            bvid: 目标 BV 号。
            now_epoch_s: 本轮时刻（UTC 秒）。

        Returns:
            _TargetClaim | None: 通过重检时返回其代际与真实类别；否则 None。
        """
        clean_bvid = str(bvid or "").strip()
        session = self._session_factory()
        try:
            row = (
                session.query(
                    HotspotWatch.active,
                    HotspotWatch.ttl_end_epoch_s,
                    HotspotWatch.next_due_epoch_s,
                    HotspotWatch.state_revision,
                )
                .filter(HotspotWatch.bvid == clean_bvid)
                .first()
            )
            if row is None:
                return None
            active, ttl_end_epoch_s, next_due_epoch_s, revision = row
            # 需求已撤销 / 手动停追：active=0，执行前拦下（不拿到 lease 也不发 HTTP）。
            if not bool(active):
                return None
            # TTL 到期：先清已在调度阶段释放，这里兜住「调度选后、执行前」的空窗。
            if int(ttl_end_epoch_s) <= int(now_epoch_s):
                return None
            # 未到点：需求整编等可能把 next_due 推后，执行前再判一次。
            if int(next_due_epoch_s) > int(now_epoch_s):
                return None
            # 真实当前类别：用 04 迁移补的 fast_until_s（缺列时优雅降级为 None -> normal）。
            fast_until = load_fast_until_map(session, [clean_bvid]).get(clean_bvid)
            category = (
                BUDGET_CATEGORY_FAST
                if (type(fast_until) is int and fast_until > int(now_epoch_s))
                else BUDGET_CATEGORY_NORMAL
            )
            return _TargetClaim(claim_revision=int(revision), category=category)
        finally:
            session.close()

    # ---- 步骤 3..6：采 → 评 → fenced 写回 → 推进调度 ----

    async def _collect_evaluate_commit(
        self,
        target: WatchTarget,
        *,
        claim: _TargetClaim,
        now_epoch_s: int,
        admission: Any | None = None,
    ) -> TargetOutcome:
        """处理单个目标：采 → 评 → fenced 写回 → 推进调度（07 案 §9.3）。

        **无 session 跨采**：采集（``admission`` 非 None 时走 ``collect_admitted`` 兑换同一份
        L 票据）在**未打开任何 DB session** 时发生；采集完成后才新开一条短 session 读历史、
        评估、按 ``claim.claim_revision`` 做 fenced 写回并推进 ``next_due``。代际被抢先 / 需求
        已撤时丢弃本次结果（不写任何列），返回 ``dropped``。

        边界（07 案 §3.4 / §9.3）：本次只把新增的 L 预算等待移出 DB 事务；collector 自提交
        快照的既有行为原样保留，**不**扩大为「全采集写入都原子回滚」的未实测承诺。

        Args:
            target: 本轮目标。
            claim: :meth:`_claim_short` 读到的代际与真实类别。
            now_epoch_s: 本轮时刻（UTC 秒），用于 next_due 调度与日志。
            admission: 可选单请求准用凭证；非 None 时走 ``collect_admitted`` 兑换同一份
                L 票据，None 时保留原 ``collect`` 行为（无预算 / legacy 路径）。

        Returns:
            TargetOutcome: ``committed`` 或 ``dropped``（附带该目标的契约输出）。

        Raises:
            Exception: 采集 / 评估 / 写回失败时原样上抛（由 ``run_tick`` 计入失败隔离）。
        """
        # ---- 3. 采：**未打开任何 DB session** 时持准用票据走 collect_admitted；无票据走 collect。
        #      测试在此打桩，绝不真发请求。reserve→redeem 之间不夹 session / 事务。
        if admission is not None:
            await self.collector_port.collect_admitted(
                target.bvid,
                admission=admission,
                collection_tid=target.collection_tid,
                source=WATCH_SOURCE,
            )
        else:
            await self.collector_port.collect(
                target.bvid, collection_tid=target.collection_tid, source=WATCH_SOURCE
            )

        # ---- 采完才开新短 session：读历史 → 评 → fenced 写回 → 推进调度 ----
        session = self._session_factory()
        try:
            # ---- 4. 评：读该 bvid 的历史快照 -> 算法层 ----
            rows = self._snapshot_loader(session, target.bvid)
            # ---- 4.1 08 案 §H4（R2）：逐目标构造 detector，评估截止取「加载快照之后」的当前时刻 ----
            # 两参契约不变：``(initial_states, as_of_epoch_s)``；只是 initial_states 从
            # 「本轮全部目标」收窄为「仅当前目标」。续算起点依旧来自 ``target.initial_state``
            # （watch 表 ``state_json`` 恢复），不依赖整轮 detector 内存，所以逐目标构造不丢状态；
            # 写回仍走 ``commit_state(claim_revision=...)`` 的 fence。
            # ``evaluation_as_of`` 只用于评估，**不得**拿去驱动 next_due / 预算的单调时钟。
            evaluation_as_of = int(self._now_fn())
            detector = self._detector_factory(
                {target.bvid: target.initial_state}, evaluation_as_of
            )
            detections, analysis = self._evaluate(detector, target.bvid, rows)

            # ---- 4.5 需求围栏（第二批 C）：写入前再检查 active / 代际 ----
            # 需求撤销 / 手动停追会在采集期间把 active 置 0 并推进代际；此时**迟到结果必须丢弃**，
            # 绝不能「先写进去再判」。这里显式再查一次，步骤 5 的 UPDATE 还额外带 active 原子谓词。
            if not self._write_fence_ok(session, target.bvid, claim.claim_revision):
                session.rollback()
                logger.info(
                    "watch 迟到写入被需求围栏拦下（需求已撤销 / 已停追） bvid=%s claim_revision=%s",
                    target.bvid,
                    claim.claim_revision,
                )
                return TargetOutcome(status="dropped")

            # ---- 5. 写：带「领取时」代际的 fenced 提交（+ active 原子围栏）----
            # False = 领取后被别的代际抢先 / 需求已撤销 -> 丢弃这次结果，不许硬写。
            committed = commit_state(
                session,
                target.bvid,
                claim_revision=claim.claim_revision,
                last_evaluation_epoch_s=analysis.state.last_evaluation_epoch_s,
                last_confirmed_stage=_confirmed_stage(analysis),
                state_json=state_to_json(analysis.state),
                coverage_ratio=float(analysis.coverage_ratio),
                coverage_state=analysis.coverage_state.value,
                require_active=True,
            )
            if not committed:
                # 不写快照、不动调度、不覆盖新 owner：整轮结果丢弃。
                session.rollback()
                logger.info(
                    "watch 写回被 fence 丢弃（代际已过期 / 已释放） bvid=%s claim_revision=%s",
                    target.bvid,
                    claim.claim_revision,
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

    @staticmethod
    def _write_fence_ok(session: Session, bvid: str, claim_revision: int) -> bool:
        """写入前的最终围栏检查（第二批 C）：``active`` 仍为真且代际未变。

        需求撤销 / 手动停追会 ``release_watch``（``active=0`` + ``state_revision + 1``），
        因此这条检查能拦住「采集期间需求被撤、随后才回来写」的迟到 worker。它只是显式防线，
        真正的原子性由 :func:`modules.hotspot.watch_store.commit_state` 的
        ``require_active=True`` 在同一条 UPDATE 里保证。

        Args:
            session: 调用方会话。
            bvid: 目标 BV 号。
            claim_revision: 领取时读到的代际号。

        Returns:
            bool: 可写回返回 True；行不存在 / 已释放 / 代际变了返回 False。
        """
        row = session.execute(
            text("SELECT active, state_revision FROM hotspot_watch WHERE bvid = :bvid"),
            {"bvid": str(bvid or "").strip()},
        ).first()
        if row is None:
            return False
        if not bool(row[0]):
            return False
        return int(row[1]) == int(claim_revision)

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

#: 04 文档把 02 的唯一采样服务称作 ``HotspotWatchService``；仓库实现名是 :class:`WatchService`。
#: 这里给同一个类建档一个别名，**不是** 新起第二个采样服务。
HotspotWatchService = WatchService

__all__ = [
    "BUDGET_CATEGORIES",
    "BUDGET_CATEGORY_FAST",
    "BUDGET_CATEGORY_NORMAL",
    "DEMAND_NAMESPACES",
    "DEMAND_REASONS",
    "DEFAULT_LOOP_INTERVAL_S",
    "DEFAULT_TICK_LIMIT",
    "MAX_BACKOFF_S",
    "MAX_FAILURE_COUNT",
    "STOP_REASON_EVENTS_REVOKED",
    "WATCH_SOURCE",
    "AdmissionResult",
    "Detection",
    "HotspotCollectorPort",
    "SnapshotCollectorPort",
    "TargetOutcome",
    "TickResult",
    "WatchPool",
    "HotspotWatchService",
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
