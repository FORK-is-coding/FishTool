"""热点生命周期算法 v2：固定 UTC 日窗新增强度 + 连续证据状态机。

设计依据：``FishTool_02_热点生命周期_专业方案与Agent执行``（R3.3 口径裁定 + §5/§6）。
本模块只实现**算法层**（纯函数 + ``LifecycleDetector`` 契约），不落库、不触网；
``state_revision`` 只在内存对象上做「提交前代际校验、旧代际丢弃」，不读写数据库。

三条口径在本模块的落点：
1. ``state_revision`` 作为写回代际 —— 见 :meth:`LifecycleV2.commit_state`；
2. 时间字段统一 ``*_epoch_s`` 后缀、秒级 int —— ``Point.epoch_s`` /
   ``as_of_epoch_s`` / ``TrendState.last_evaluation_epoch_s``，不使用 ``_ts`` / ``_at`` / 毫秒；
3. coverage 两级输出 —— :func:`classify_coverage` 产出 ``CoverageState`` 枚举，
   ``Detection.metrics`` 只给 ``coverage_ratio``（数值），``Detection.metadata`` 给
   ``coverage_state``（枚举值）与 ``confidence_kind``（非数值项通道）；
4. confidence 归位 —— 第一版 ``confidence`` 固定 ``0.0``、``confidence_kind`` 固定
   ``'not_estimated'``（方案 §10.1），不得临时把覆盖度拼成一个分数称概率；证据充分程度
   一律由 ``coverage_ratio`` / ``coverage_state`` / ``observed_windows`` 表达。

与 v1 的关系：v1 用「相邻累计相对变化 + 7 日等权平滑」；v2 改用**固定 24 小时窗的绝对
新增播放强度（播放/天）**，累计回撤只切段、绝不直接触发衰退。两者并存，v1 保留作对照回放。
"""
from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from .base import ConfidenceKind, Detection, LifecycleDetector, Snapshot
from .config import get_lifecycle_v2_config
from .publication_age import resolve_publication_age

# --------------------------------------------------------------------- 常量

DAY_S: int = 86400
"""一个自然日的秒数；UTC 00:00 为固定网格边界。"""

DEFAULT_GAP_S: int = 36 * 3600
"""相邻有效观测点允许的最大间隔；超过即断段，禁止跨空洞插值。"""

DEFAULT_MAX_STALENESS_S: int = 36 * 3600
"""采样新鲜度上限；超过视为陈旧（stale），不用过期数据推进候选计数。"""


class Stage:
    """生命周期阶段与展示兜底标签（定义取 02 方案 §5.4，不得自创）。"""

    OBSERVING = "观察期"
    EMERGING = "出现期"
    RISING = "上升期"
    MATURE = "成熟期"
    DECLINING = "衰退期"
    INSUFFICIENT = "数据不足"


class CoverageState(str, Enum):
    """coverage 两级输出的枚举侧（02 方案 R3.3 裁定三）。"""

    FULL_SUPPORT = "full_support"
    """完整支撑模式：整个窗口都被有效观测区间支撑。"""

    PROVISIONAL = "provisional"
    """暂定观察：部分窗口支撑，只能出「已观察部分平均强度」。"""

    INSUFFICIENT = "insufficient"
    """覆盖不足：证据比例过低，需要降置信度并提示还缺哪个点。"""


_COVERAGE_LABELS: dict[CoverageState, str] = {
    CoverageState.FULL_SUPPORT: "完整支撑",
    CoverageState.PROVISIONAL: "暂定观察",
    CoverageState.INSUFFICIENT: "覆盖不足",
}

_DIRECTION_STABLE = "stable"
_DIRECTION_UP = "up"
_DIRECTION_DOWN = "down"


# --------------------------------------------------------------------- 配置


@dataclass(frozen=True)
class LifecycleV2Config:
    """v2 实验默认阈值（02 方案 §5.1）。

    说明：本文件为生命周期算法层唯一新增文件，配置暂与算法同文件；后续批次
    （方案 §7.1 第 3 条）应迁移到 ``algorithm/config.py``，本批不越界改动该文件。
    """

    window_seconds: int = DAY_S
    gap_max_seconds: int = DEFAULT_GAP_S
    max_staleness_seconds: int = DEFAULT_MAX_STALENESS_S
    min_window_coverage: float = 0.85
    strict_full_support: bool = False
    absolute_delta: float = 20.0
    relative_delta: float = 0.20
    low_base: float = 20.0
    confirmation_windows: int = 2
    emerge_rate: float = 20.0
    emerge_age_days: float = 7.0
    # ---- 08 案 §J3（B6b）：第二条「新发现老视频」通道的门槛 ----
    # 工具首次发现（HotspotWatch.first_seen_epoch_s）距评估窗口端点 <= 该天数时，
    # 老视频也可判为出现期（依据 discovery）。与 emerge_age_days 各自独立。
    emerge_discovery_days: float = 2.0
    history_days: int = 30
    percentile_min_n: int = 20
    # ---- 08 案 §J5：B6b 独立升级门槛版本（不与 B6a 的 age_gate_1 合并）----
    threshold_version: str = "lifecycle_v2_age_gate_2"

    def as_dict(self) -> dict[str, Any]:
        """返回可序列化配置字典，供 ``config_schema``、日志与回放消费。"""
        return {
            "window_seconds": self.window_seconds,
            "gap_max_seconds": self.gap_max_seconds,
            "max_staleness_seconds": self.max_staleness_seconds,
            "min_window_coverage": self.min_window_coverage,
            "strict_full_support": self.strict_full_support,
            "absolute_delta": self.absolute_delta,
            "relative_delta": self.relative_delta,
            "low_base": self.low_base,
            "confirmation_windows": self.confirmation_windows,
            "emerge_rate": self.emerge_rate,
            "emerge_age_days": self.emerge_age_days,
            "emerge_discovery_days": self.emerge_discovery_days,
            "history_days": self.history_days,
            "percentile_min_n": self.percentile_min_n,
            "threshold_version": self.threshold_version,
        }


def _validate_config(config: LifecycleV2Config) -> None:
    """校验阈值配置，非法值直接抛 ``ValueError``，避免静默产出错误阶段。

    Args:
        config: 待校验的 v2 配置。

    Returns:
        无。

    Raises:
        ValueError: 任一整数/浮点阈值越界或类型不合法。
    """
    if type(config.window_seconds) is not int or config.window_seconds <= 0:
        raise ValueError("invalid_window_seconds")
    if type(config.gap_max_seconds) is not int or config.gap_max_seconds <= 0:
        raise ValueError("invalid_gap_max_seconds")
    if type(config.max_staleness_seconds) is not int or config.max_staleness_seconds <= 0:
        raise ValueError("invalid_max_staleness_seconds")
    if not 0.0 < config.min_window_coverage <= 1.0:
        raise ValueError("invalid_min_window_coverage")
    if type(config.confirmation_windows) is not int or config.confirmation_windows < 2:
        raise ValueError("invalid_confirmation_windows")
    if type(config.history_days) is not int or config.history_days < 0:
        raise ValueError("invalid_history_days")
    if type(config.percentile_min_n) is not int or config.percentile_min_n < 1:
        raise ValueError("invalid_percentile_min_n")
    for name in ("absolute_delta", "relative_delta", "low_base", "emerge_rate"):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"invalid_{name}")
    if config.absolute_delta <= 0 or config.low_base <= 0:
        raise ValueError("invalid_positive_threshold")
    if config.relative_delta < 0:
        raise ValueError("invalid_relative_delta")
    # ---- 08 案 §J3（B6a）：作品年龄门槛补验证（有限非负、排除 bool）----
    if (
        isinstance(config.emerge_age_days, bool)
        or not isinstance(config.emerge_age_days, (int, float))
        or not math.isfinite(config.emerge_age_days)
        or config.emerge_age_days < 0
    ):
        raise ValueError("invalid_emerge_age_days")
    # ---- 08 案 §J3（B6b）：发现时间门槛同样校验（有限非负、排除 bool）----
    if (
        isinstance(config.emerge_discovery_days, bool)
        or not isinstance(config.emerge_discovery_days, (int, float))
        or not math.isfinite(config.emerge_discovery_days)
        or config.emerge_discovery_days < 0
    ):
        raise ValueError("invalid_emerge_discovery_days")


def within_age_limit(reference_s: int, origin_s: int | None, days: float) -> bool:
    """判断「作品在 ``reference_s`` 时点的年龄是否不超过 ``days`` 天」。

    08 案 §J4：边界用**精确秒**比较，不截整天；``origin_s`` 未知（``None``）或非整数
    一律返回 ``False`` —— 门槛拿不到证据时**不**放行出现期（未知不开口子）。

    Args:
        reference_s: 评估窗口端点（UTC 秒级 int）。
        origin_s: 作品发布时间（UTC 秒级 int）；``None`` 表示无有效证据。
        days: 允许的最大稿龄（天）。

    Returns:
        年龄落在 ``[0, days]`` 闭区间返回 ``True``，否则 ``False``。
    """
    if type(reference_s) is not int or type(origin_s) is not int:
        return False
    elapsed = reference_s - origin_s
    return elapsed >= 0 and elapsed <= days * DAY_S


# --------------------------------------------------------------------- 纯数学内核


@dataclass(frozen=True)
class Point:
    """一个已定位到 UTC 秒的观测点。

    Args:
        epoch_s: 观测时刻（UTC 秒级 int），字段名遵守 ``*_epoch_s`` 口径。
        view: 累计播放量；``None`` 表示质量非 ok 的 marker。
        ok: 该点是否可作为有效端点参与插值与强度计算。
    """

    epoch_s: int
    view: int | None
    ok: bool


def valid_segments(points: list[Point], *, max_gap_s: int = DEFAULT_GAP_S) -> list[list[Point]]:
    """把观测点切成「有效单调段」。

    规则：同 epoch 冲突值、质量非 ok、播放回撤、相邻间隔超过 ``max_gap_s`` 都会断段；
    段内累计播放单调不减，保证不会出现负速度。

    Args:
        points: 观测点列表（可乱序，内部按 epoch 升序聚合）。
        max_gap_s: 相邻有效点允许的最大间隔（秒）。

    Returns:
        按时间升序的段列表，每个段是一串单调不减的有效点。

    Raises:
        ValueError: ``max_gap_s`` 非法，或存在无法定位时间的 marker。
    """
    if type(max_gap_s) is not int or max_gap_s <= 0:
        raise ValueError("invalid_gap_max_seconds")

    # 先按 epoch 聚合，保证乱序输入也得到同一结果。
    groups: dict[int, list[Point]] = {}
    for point in points:
        if type(point.epoch_s) is not int:
            raise ValueError("unlocated_time_marker")
        groups.setdefault(point.epoch_s, []).append(point)

    segments: list[list[Point]] = []
    current: list[Point] = []
    for epoch_s in sorted(groups):
        rows = groups[epoch_s]
        # 先做类型判定，避免把 dict/list 之类非法 view 送进 set 推导而抛 TypeError。
        good = all(row.ok is True and type(row.view) is int and row.view >= 0 for row in rows)
        conflict = good and len({row.view for row in rows}) != 1
        if not good or conflict:
            if current:
                segments.append(current)
            current = []
            continue
        point = rows[-1]
        if current and (point.view < current[-1].view or point.epoch_s - current[-1].epoch_s > max_gap_s):
            segments.append(current)
            current = []
        current.append(point)
    if current:
        segments.append(current)
    return segments


def boundary(seg: list[Point], epoch_s: int, *, max_gap_s: int = DEFAULT_GAP_S) -> float | None:
    """在单个有效段内取某个时刻的插值播放量。

    只允许段内双边线性插值；超出段范围或跨超长 gap 时返回 ``None``，绝不外推。

    Args:
        seg: 一个有效单调段。
        epoch_s: 目标时刻（UTC 秒）。
        max_gap_s: 插值相邻点允许的最大间隔。

    Returns:
        插值得到的播放量；不可插值时返回 ``None``。
    """
    if not seg:
        return None
    times = [point.epoch_s for point in seg]
    index = bisect_left(times, epoch_s)
    if index < len(seg) and seg[index].epoch_s == epoch_s:
        return float(seg[index].view)
    if index == 0 or index == len(seg):
        return None
    left, right = seg[index - 1], seg[index]
    if right.epoch_s - left.epoch_s > max_gap_s:
        return None
    ratio = (epoch_s - left.epoch_s) / (right.epoch_s - left.epoch_s)
    return left.view + (right.view - left.view) * ratio


def window_measure(
    seg: list[Point],
    end_epoch_s: int,
    *,
    window_s: int = DAY_S,
    gap_max_s: int = DEFAULT_GAP_S,
) -> tuple[float | None, float, int]:
    """测量以 ``end_epoch_s`` 右闭的窗口内的播放增量与支撑覆盖率。

    与 02 方案 §6.1 的差异（R3.3 裁定三的要求）：当窗口左端只有部分支撑、但右端有
    可用括点时，不再直接判「窗不可用」，而是返回**已观察区间**的增量与其占比；右端缺
    括点时仍返回 ``None``（禁止向前外推，保留方案 §4.1 语义）。

    Args:
        seg: 支撑该窗口的有效单调段（应取「支撑截至 T 的最新段」）。
        end_epoch_s: 窗口右边界（UTC 秒）。
        window_s: 窗口长度（秒）。
        gap_max_s: 相邻点允许的最大间隔。

    Returns:
        ``(delta, coverage_ratio, observed_seconds)``：
        ``delta`` 为已观察区间播放增量（不可用为 ``None``），
        ``coverage_ratio`` 为已观察区间占整窗比例（0~1），
        ``observed_seconds`` 为已观察区间时长（秒，用于把增量归一成日强度）。
    """
    if type(window_s) is not int or window_s <= 0:
        raise ValueError("invalid_window_seconds")
    left = end_epoch_s - window_s
    if len(seg) < 2:
        return None, 0.0, 0

    first, last = seg[0], seg[-1]
    span_start = left if first.epoch_s <= left else first.epoch_s
    span_end = end_epoch_s if last.epoch_s >= end_epoch_s else last.epoch_s
    if span_end <= span_start:
        return None, 0.0, 0

    observed = span_end - span_start
    coverage = min(1.0, observed / window_s)
    # 右端缺括点：不知道该时刻的播放量，返回空增量而非外推。
    if last.epoch_s < end_epoch_s:
        return None, coverage, observed

    start_view = boundary(seg, span_start, max_gap_s=gap_max_s)
    end_view = boundary(seg, span_end, max_gap_s=gap_max_s)
    if start_view is None or end_view is None:
        return None, coverage, observed
    delta = end_view - start_view
    if delta < 0:  # 有效单调段理论上不会出现；保守判为不可用。
        return None, coverage, observed
    return delta, coverage, observed


def classify_coverage(
    coverage: float,
    *,
    min_window_coverage: float = 0.85,
    strict_full_support: bool = False,
) -> CoverageState:
    """把数值覆盖率映射成两级输出里的枚举状态。

    Args:
        coverage: 窗口支撑占比（0~1）。
        min_window_coverage: 暂定观察门槛；``strict_full_support`` 打开时该门槛失效。
        strict_full_support: 精确模式开关，打开后只有 1.0 才算可用（默认关）。

    Returns:
        对应的 :class:`CoverageState`。
    """
    if coverage >= 1.0:
        return CoverageState.FULL_SUPPORT
    if strict_full_support:
        return CoverageState.INSUFFICIENT
    if coverage >= min_window_coverage:
        return CoverageState.PROVISIONAL
    return CoverageState.INSUFFICIENT


def direction(
    base: float,
    rate: float,
    *,
    absolute_delta: float = 20.0,
    relative_delta: float = 0.20,
    low_base: float = 20.0,
) -> str:
    """判断当前强度相对参考基线是 up / down / stable（02 方案 §5.2）。

    规则：``base >= low_base`` 时必须同时满足绝对与相对阈值（AND，非 OR）；
    ``base < low_base`` 时只用绝对阈值，避免低基数下的百分比爆炸。

    Args:
        base: 参考基线强度（播放/天）。
        rate: 当前强度（播放/天）。
        absolute_delta: 绝对变化阈值。
        relative_delta: 相对变化阈值。
        low_base: 低基数分界。

    Returns:
        ``"up"`` / ``"down"`` / ``"stable"``。
    """
    delta = rate - base
    if base < low_base:
        significant = abs(delta) >= absolute_delta
    else:
        significant = abs(delta) >= absolute_delta and abs(delta) / base >= relative_delta
    if not significant:
        return _DIRECTION_STABLE
    return _DIRECTION_UP if delta > 0 else _DIRECTION_DOWN


@dataclass
class TrendState:
    """逐 bvid 的连续证据状态机快照（对应 02 方案 §6.2 的 ``TrendState``）。

    ``state_revision`` 是该状态的写回代际：任何落盘都要走「带 revision 校验」的提交。
    """

    last_evaluation_epoch_s: int | None = None
    prev_rate: float | None = None
    candidate: str | None = None
    baseline: float | None = None
    count: int = 0
    stable_count: int = 0
    stage: str = Stage.OBSERVING
    state_revision: int = 0


def advance(
    state: TrendState,
    epoch_s: int,
    rate: float | None,
    *,
    config: LifecycleV2Config,
    segment_changed: bool = False,
    emerging: bool = False,
) -> TrendState:
    """把单日窗口强度推进状态机一步（原地更新并返回 ``state``）。

    无效日、非相邻日、段切换都会清空候选与 stable 计数（方案 §5.3 第 7 条）。
    ``rate is None`` 表示缺数据，不是衰退。

    Args:
        state: 当前状态（调用方应先复制）。
        epoch_s: 本窗口右边界（UTC 秒）。
        rate: 本窗口新增强度（播放/天）；``None`` 表示不可用。
        config: v2 阈值配置。
        segment_changed: 是否发生了有效段切换。
        emerging: 本窗是否满足「首次出现」条件。

    Returns:
        更新后的同一 ``state`` 对象。

    Raises:
        ValueError: 时间/窗口非法，或 rate 为负/非有限值。
    """
    if type(epoch_s) is not int or type(config.window_seconds) is not int or config.window_seconds <= 0:
        raise ValueError("invalid_window_time")
    if rate is not None and (isinstance(rate, bool) or not math.isfinite(rate) or rate < 0):
        raise ValueError("invalid_rate")

    contiguous = (
        state.last_evaluation_epoch_s is not None
        and epoch_s - state.last_evaluation_epoch_s == config.window_seconds
    )
    if segment_changed or not contiguous or rate is None:
        # 无效/非相邻/段切换：清空候选与稳定计数，当前段回到观察期。
        state.prev_rate = None
        state.candidate = None
        state.baseline = None
        state.count = 0
        state.stable_count = 0
        state.stage = Stage.OBSERVING
    state.last_evaluation_epoch_s = epoch_s
    if rate is None:
        return state

    if state.prev_rate is None:
        state.prev_rate = rate
        if emerging and state.stage == Stage.OBSERVING:
            state.stage = Stage.EMERGING
        return state

    reference = state.baseline if state.candidate else state.prev_rate
    current = direction(
        reference,
        rate,
        absolute_delta=config.absolute_delta,
        relative_delta=config.relative_delta,
        low_base=config.low_base,
    )
    if state.candidate and current != state.candidate:
        # 方向反转：撤销旧候选，改与上一窗比较重新起算。
        state.candidate = None
        state.baseline = None
        state.count = 0
        current = direction(
            state.prev_rate,
            rate,
            absolute_delta=config.absolute_delta,
            relative_delta=config.relative_delta,
            low_base=config.low_base,
        )
    if current == _DIRECTION_STABLE:
        state.stable_count += 1
        if state.stable_count >= config.confirmation_windows:
            state.stage = Stage.MATURE
    else:
        state.stable_count = 0
        if state.candidate == current:
            state.count += 1
        else:
            state.candidate = current
            state.baseline = state.prev_rate
            state.count = 1
        if state.count >= config.confirmation_windows:
            state.stage = Stage.RISING if current == _DIRECTION_UP else Stage.DECLINING
            state.candidate = None
            state.baseline = None
            state.count = 0
    state.prev_rate = rate
    return state


def _snapshot_epoch_s(snapshot: Snapshot) -> int | None:
    """读取快照的 UTC 秒级时间；缺失或类型非法返回 ``None``（不猜时区、不补 now）。"""
    epoch_s = getattr(snapshot, "captured_epoch_s", None)
    if type(epoch_s) is int and epoch_s >= 0:
        return epoch_s
    return None


def _first_seen_epoch_s(rows: list[Snapshot]) -> int | None:
    """从快照序列里取该 bvid 的「工具首次发现」时刻（08 案 §J3 第 5 条 / B6b）。

    ``first_seen_epoch_s`` 是行级携带、同一 bvid 一致的 watch 发现时间；任一有效整数
    即可采信，非整数 / 缺失一律 ``None``（无 watch 即无发现时间）。**绝不**回退到
    ``Video.created_at`` 或 ``captured`` 时间——那是另一回事，混用会让旧视频伪装成
    「刚发现 / 刚发布」。该值晚于评估窗口时由 :func:`within_age_limit` 判不可用。

    Args:
        rows: 同一 bvid 的 Snapshot 列表。

    Returns:
        有效的首次发现 UTC 秒级 int；无证据返回 ``None``。
    """
    for row in rows:
        value = getattr(row, "first_seen_epoch_s", None)
        if type(value) is int and value >= 0:
            return value
    return None


# --------------------------------------------------------------------- 分析结果


def _confidence_kind_value(kind: ConfidenceKind | str) -> str:
    """把 ``confidence_kind`` 归一为 wire 契约里的**小写字符串**。

    枚举成员取 ``.value``；已是字符串或未知实现原样返回，兼容旧数据与外部注入值。
    """
    if isinstance(kind, ConfidenceKind):
        return kind.value
    return str(kind)


#: ``Analysis.emergence_basis`` 取值（08 案 §J3：B6b 起 publication / discovery / none）。
#: - publication：作品在该窗口端点的年龄 <= ``emerge_age_days``（B6a 通道）。
#: - discovery：工具首次发现距该窗口端点 <= ``emerge_discovery_days``（B6b 新增通道）。
#: - none：两项证据均不成立（未知 / 冲突 / 未来 / 超龄）。
EMERGENCE_BASIS_PUBLICATION = "publication"
EMERGENCE_BASIS_DISCOVERY = "discovery"
EMERGENCE_BASIS_NONE = "none"


@dataclass
class Analysis:
    """单个 bvid 的分析中间结果（供检测输出与用例断言消费）。"""

    bvid: str
    tid: int
    title: str
    owner_mid: int
    owner_name: str
    stage: str
    state: TrendState
    rates: list[float]
    coverage_ratio: float
    coverage_state: CoverageState
    observed_windows: int
    sample_count: int
    staleness_hours: float
    data_status: str
    unlocated_count: int
    threshold_version: str
    # ---- 02 方案 §10.1：confidence 第一版归位 ----
    # 固定 0.0 + 'not_estimated'，绝不从覆盖度折算；证据充分程度看 coverage/observed_windows。
    # 说明：metadata 通道已落地 —— detect() 经 _metadata() 把 confidence_kind 与
    # coverage_state 一并写进 Detection.metadata，不塞进约定「仅数值」的 Detection.metrics。
    confidence: float = 0.0
    confidence_kind: str = ConfidenceKind.NOT_ESTIMATED.value
    # ---- M17：窗内长 gap 造成的部分覆盖窗只出 coverage，不推进阶段 ----
    gap_blocked_windows: int = 0
    # ---- 08 案 §H2：评估截止与作品稿龄（B4）----
    # as_of 是「本次评估截止」：实时路径显式传入；离线回放缺省回退最大观测点，
    # 此时 as_of_source 必须标 max_observed_fallback，不得当墙钟时间用。
    # age_days 仅在 age_status == 'ok' 时有值；未知一律 None，绝不填 0 或假值。
    as_of_epoch_s: int | None = None
    as_of_source: str = "unknown"
    age_days: float | None = None
    age_status: str = "unknown"
    age_source: str = "unknown"
    # ---- 08 案 §J3（B6b）：出现期依据（publication / discovery / none）----
    # publication：该窗口端点作品年龄 <= emerge_age_days 的发布时间证据成立；
    # discovery：工具首次发现距该窗口端点 <= emerge_discovery_days（新发现老视频通道）；
    # 二者都只约束 emerging；证据缺失 / 冲突 / 未来一律 none，绝不硬写 pubdate/first_seen 过门。
    emergence_basis: str = EMERGENCE_BASIS_NONE

    @property
    def rate_current(self) -> float | None:
        """最近一个可用窗的强度。"""
        return self.rates[-1] if self.rates else None

    @property
    def rate_previous(self) -> float | None:
        """倒数第二个可用窗的强度。"""
        return self.rates[-2] if len(self.rates) >= 2 else None

    @property
    def rate_delta(self) -> float | None:
        """相邻可用窗强度差。"""
        if self.rate_current is None or self.rate_previous is None:
            return None
        return self.rate_current - self.rate_previous

    @property
    def relative_change(self) -> float | None:
        """相对强度变化；上一窗为 0 或缺失时返回 ``None``，不伪造百分比。"""
        if self.rate_delta is None or not self.rate_previous:
            return None
        return self.rate_delta / self.rate_previous


# --------------------------------------------------------------------- 算法主体


class LifecycleV2(LifecycleDetector):
    """固定日窗 + 连续证据状态机的单视频生命周期算法。"""

    def __init__(
        self,
        domain: str = "default",
        config: LifecycleV2Config | None = None,
        as_of_epoch_s: int | None = None,
        initial_states: dict[str, TrendState] | None = None,
    ) -> None:
        """初始化算法。

        Args:
            domain: 领域名（当前仅透传，便于后续分桶阈值）。
            config: 显式配置；缺省按 ``domain`` 经 :func:`config.get_lifecycle_v2_config`
                取域配置，桶内无覆盖时即 :class:`LifecycleV2Config` 默认值（与改前一致）。
            as_of_epoch_s: 计算截止时刻（UTC 秒）；``None`` 时回退到最大观测点。
            initial_states: 先前状态（bvid -> TrendState），用于续算而非每次冷启动。

        Raises:
            ValueError: 配置非法或 ``as_of_epoch_s`` 非法。
        """
        self.domain = domain
        self.config = config or get_lifecycle_v2_config(domain)
        _validate_config(self.config)
        if as_of_epoch_s is not None and (type(as_of_epoch_s) is not int or as_of_epoch_s < 0):
            raise ValueError("invalid_as_of_epoch_s")
        self.as_of_epoch_s = as_of_epoch_s
        self._states: dict[str, TrendState] = {}
        for bvid, state in (initial_states or {}).items():
            self._states[str(bvid)] = replace(state)

    # ---- LifecycleDetector 契约 ----

    @property
    def version(self) -> str:
        """返回当前算法版本。"""
        return "lifecycle_v2"

    @property
    def config_schema(self) -> dict[str, Any]:
        """返回前端可展示的算法配置。"""
        return {
            "version": self.version,
            "domain": self.domain,
            "as_of_epoch_s": self.as_of_epoch_s,
            **self.config.as_dict(),
        }

    # ---- state_revision 代际（fencing）----

    @property
    def states(self) -> dict[str, TrendState]:
        """返回当前所有状态的副本，外部无法就地篡改内部代际。"""
        return {bvid: replace(state) for bvid, state in self._states.items()}

    def revision_of(self, bvid: str) -> int:
        """返回某 bvid 当前状态的代际号；无状态时为 0。"""
        state = self._states.get(bvid)
        return 0 if state is None else state.state_revision

    def claim_state(self, bvid: str) -> tuple[int, TrendState]:
        """领取状态：返回 ``(claim_revision, 状态副本)``，**不写回**。

        Args:
            bvid: 视频 BV 号。

        Returns:
            当前代际号与该代际下的状态副本。
        """
        state = self._states.get(bvid)
        if state is None:
            return 0, TrendState()
        return state.state_revision, replace(state)

    def commit_state(self, bvid: str, state: TrendState, *, claim_revision: int) -> bool:
        """带 revision 校验的写回：旧代际写入一律丢弃（02 方案 R3.3 裁定一）。

        实现语义等价于条件 UPDATE ``WHERE state_revision = :claim_revision``：
        只有领取时的代际与当前一致才落盘，并在同一次提交里把代际 +1；否则返回
        ``False`` 且不改变任何内部状态（不写快照、不动调度）。

        Args:
            bvid: 视频 BV 号。
            state: 待提交的新状态。
            claim_revision: 领取时读到的代际号。

        Returns:
            提交成功返回 ``True``；因代际过期被丢弃返回 ``False``。
        """
        key = str(bvid)
        current = self._states.get(key)
        current_revision = 0 if current is None else current.state_revision
        if claim_revision != current_revision:
            return False
        committed = replace(state)
        committed.state_revision = current_revision + 1
        self._states[key] = committed
        return True

    # ---- 分析 ----

    def _initial_state(self, bvid: str) -> TrendState:
        """取该 bvid 的续算起点（副本），无历史则用全新状态。"""
        current = self._states.get(bvid)
        return replace(current) if current is not None else TrendState()

    def analyze_one(self, bvid: str, rows: list[Snapshot]) -> Analysis:
        """分析单个 bvid 的快照序列，输出阶段与证据度量。

        H2 修复（08 案 A2/E1）：**先确定本次评估截止 ``as_of``，再据此筛选可见行**。
        旧实现先遍历全部行选 ``latest_row``、之后才按 ``as_of`` 过滤，导致显式 ``as_of``
        早于最新快照时，``tid`` / ``title`` / ``owner_mid`` / ``owner_name`` 取自评估时
        尚不存在的未来快照；``tid`` 还会经 ``detect`` 的 ``rates_by_tid`` 污染同源百分位
        分组。现在未来快照既不进 ``located``，也不参与 ``latest_row`` 竞选。
        """
        config = self.config
        # ---- 1. 先定 as_of：显式传入优先；缺省回退「最大观测点」（离线回放友好）----
        as_of = self.as_of_epoch_s
        if as_of is None:
            as_of = max(
                (epoch for epoch in (_snapshot_epoch_s(row) for row in rows) if epoch is not None),
                default=None,
            )

        # ---- 2. 主循环：epoch 缺失计 unlocated；epoch > as_of 的未来快照直接跳过 ----
        located: list[Point] = []
        unlocated = 0
        latest_row: Snapshot | None = None
        latest_epoch_s: int | None = None
        for row in rows:
            epoch_s = _snapshot_epoch_s(row)
            if epoch_s is None:
                unlocated += 1
                continue
            if as_of is not None and epoch_s > as_of:
                # H2：未来快照既不进 located，也不参与 latest_row 竞选；不计入 unlocated。
                continue
            view = getattr(row, "view", None)
            view_ok = getattr(row, "view_quality", "unknown") == "ok"
            ok = view_ok and type(view) is int and view >= 0
            located.append(Point(epoch_s=epoch_s, view=view if type(view) is int else None, ok=ok))
            if latest_epoch_s is None or epoch_s >= latest_epoch_s:
                latest_epoch_s = epoch_s
                latest_row = row

        tid = int(getattr(latest_row, "tid", 0) or 0) if latest_row is not None else 0
        title = str(getattr(latest_row, "title", "") or "") if latest_row is not None else ""
        owner_mid = int(getattr(latest_row, "owner_mid", 0) or 0) if latest_row is not None else 0
        owner_name = str(getattr(latest_row, "owner_name", "") or "") if latest_row is not None else ""
        sample_count = len(located)

        # ---- 3. 稿龄证据：只采信「可见且 status=ok」的发布时间（08 案 §H1）----
        # 未来快照已被上面的主循环挡在 located 之外，但 resolve_publication_age 内部仍
        # 按 capture <= as_of 再挡一道，保证它被单独调用时也守住同一条红线。
        age = resolve_publication_age(rows, as_of_epoch_s=as_of)
        if self.as_of_epoch_s is not None:
            as_of_source = "explicit"
        elif as_of is not None:
            as_of_source = "max_observed_fallback"
        else:
            as_of_source = "unknown"

        if as_of is None or not located:
            return Analysis(
                bvid=bvid, tid=tid, title=title, owner_mid=owner_mid, owner_name=owner_name,
                stage=Stage.INSUFFICIENT, state=self._initial_state(bvid), rates=[],
                coverage_ratio=0.0, coverage_state=CoverageState.INSUFFICIENT,
                observed_windows=0, sample_count=sample_count, staleness_hours=0.0,
                data_status="insufficient", unlocated_count=unlocated,
                threshold_version=config.threshold_version,
                as_of_epoch_s=as_of, as_of_source=as_of_source,
                age_days=age.days, age_status=age.status, age_source=age.source,
                emergence_basis=EMERGENCE_BASIS_NONE,
            )

        # 只用「支撑截至 T 的最新段」承载窗口，不从更老段挑好窗冒充当前。
        segments = valid_segments(located, max_gap_s=config.gap_max_seconds)
        segment = segments[-1] if segments else []
        window_s = config.window_seconds
        grid_end_s = (as_of // window_s) * window_s
        last_point_s = max(point.epoch_s for point in located)
        staleness_hours = round((as_of - last_point_s) / 3600.0, 3)
        stale = (as_of - last_point_s) > config.max_staleness_seconds
        data_status = "stale" if stale else "ok"

        state = self._initial_state(bvid)
        rates: list[float] = []
        observed_windows = 0
        gap_blocked_windows = 0
        last_coverage = 0.0
        emergence_basis = EMERGENCE_BASIS_NONE
        # B6b：该 bvid 的工具首次发现时刻（无 watch / 无发现时间即 None）；逐窗用 end_s 比较。
        first_seen_epoch_s = _first_seen_epoch_s(rows)
        # 观测历史起点：窗左端落在这条线右侧却没被支撑时，缺口属于「观测空洞/断档」；
        # 落在左侧只是「历史尚未开始」，两种口径不能混（R3.3 裁定三 × M17）。
        history_start_epoch_s = min(point.epoch_s for point in located)
        windows = [grid_end_s - index * window_s for index in range(config.history_days, -1, -1)]
        for end_s in windows:
            delta, coverage, observed_seconds = window_measure(
                segment, end_s, window_s=window_s, gap_max_s=config.gap_max_seconds
            )
            if stale and end_s == grid_end_s:
                # 陈旧：保留历史阶段，不用过期数据推进候选/阶段。
                continue
            coverage_state = classify_coverage(
                coverage,
                min_window_coverage=config.min_window_coverage,
                strict_full_support=config.strict_full_support,
            )
            rate: float | None = None
            if delta is not None and observed_seconds > 0:
                rate = delta / (observed_seconds / float(DAY_S))
            if config.strict_full_support and coverage_state is not CoverageState.FULL_SUPPORT:
                rate = None
            # M17：窗内长 gap（或回撤/坏点断档）造成覆盖不完整。delta 非 None 只说明两个边界
            # 都插到了值（「边界都有值」），增量仍只覆盖「已观察部分」，不得用来推进
            # candidate / count / stable_count —— 该窗只出 coverage，不推进阶段。
            # 与裁定三区分：缺口落在「历史尚未开始」一侧时仍可出阶段（provisional）。
            if delta is not None and coverage < 1.0 and (end_s - window_s) >= history_start_epoch_s:
                gap_blocked_windows += 1
                last_coverage = coverage
                continue
            # ---- 08 案 §J4（B6a）：作品年龄门槛进门 ----
            # 窗口级发布时间证据：只采信「该端点当时可见（capture <= end_s）且无冲突」的
            # 事实，不用今天才采到 / 后来纠正的元信息改写历史窗口的新旧判断。
            publication_for_window = resolve_publication_age(
                rows, as_of_epoch_s=end_s
            ).published_epoch_s
            by_publication = within_age_limit(
                end_s, publication_for_window, config.emerge_age_days
            )
            # B6b：第二条「新发现老视频」通道。first_seen 与发布时间证据各自独立；
            # 该值晚于 end_s（未来）由 within_age_limit 判不可用，不截整天、不 clamp。
            by_discovery = within_age_limit(
                end_s, first_seen_epoch_s, config.emerge_discovery_days
            )
            emerging = (
                state.prev_rate is None
                and rate is not None
                and rate >= config.emerge_rate
                and (by_publication or by_discovery)
            )
            if emerging:
                # 依据优先展示 publication（作品确实新）；只有老视频靠发现时间过门时才
                # 显式标 discovery —— 此时**不得**表述成「视频刚发布 / 事件刚发生」。
                if by_publication:
                    emergence_basis = EMERGENCE_BASIS_PUBLICATION
                else:
                    emergence_basis = EMERGENCE_BASIS_DISCOVERY
            advance(state, end_s, rate, config=config, emerging=emerging)
            if rate is not None:
                rates.append(rate)
                observed_windows += 1
            last_coverage = coverage

        coverage_state = classify_coverage(
            last_coverage,
            min_window_coverage=config.min_window_coverage,
            strict_full_support=config.strict_full_support,
        )
        stage = state.stage if observed_windows else Stage.INSUFFICIENT
        return Analysis(
            bvid=bvid, tid=tid, title=title, owner_mid=owner_mid, owner_name=owner_name,
            stage=stage, state=state, rates=rates, coverage_ratio=last_coverage,
            coverage_state=coverage_state, observed_windows=observed_windows,
            gap_blocked_windows=gap_blocked_windows,
            sample_count=sample_count, staleness_hours=staleness_hours,
            data_status=data_status, unlocated_count=unlocated,
            threshold_version=config.threshold_version,
            as_of_epoch_s=as_of, as_of_source=as_of_source,
            age_days=age.days, age_status=age.status, age_source=age.source,
            emergence_basis=emergence_basis,
        )

    def detect(self, snapshots: list[Snapshot]) -> list[Detection]:
        """按 BV 号分析快照并返回统一 ``Detection`` 列表（纯计算，不改内部代际）。"""
        grouped: dict[str, list[Snapshot]] = {}
        for snapshot in snapshots:
            bvid = getattr(snapshot, "bvid", "")
            if bvid:
                grouped.setdefault(bvid, []).append(snapshot)

        analyses = {bvid: self.analyze_one(bvid, rows) for bvid, rows in grouped.items()}

        # 同源辅助百分位：只在样本足够时才计算，样本不足则给 None 而不阻断绝对趋势。
        rates_by_tid: dict[int, list[float]] = {}
        for info in analyses.values():
            if info.tid and info.rate_current is not None:
                rates_by_tid.setdefault(info.tid, []).append(info.rate_current)

        detections: list[Detection] = []
        for bvid, info in analyses.items():
            percentile = self._percentile(info, rates_by_tid.get(info.tid, []))
            detections.append(
                Detection(
                    bvid=bvid,
                    stage=info.stage,
                    confidence=info.confidence,  # §10.1：固定 0.0，不从覆盖度折算
                    metrics=self._metrics(info, percentile),
                    metadata=self._metadata(info),
                    explain=self._explain(info, percentile),
                    algorithm_version=self.version,
                    title=info.title,
                    tid=info.tid,
                    owner_mid=info.owner_mid,
                    owner_name=info.owner_name,
                )
            )
        return sorted(detections, key=lambda item: item.bvid)

    # ---- 证据度量与文案 ----

    def _percentile(self, info: Analysis, peers: list[float]) -> float | None:
        """计算同源百分位；样本不足 ``percentile_min_n`` 时返回 ``None``。"""
        if info.rate_current is None or len(peers) < self.config.percentile_min_n:
            return None
        hits = sum(1 for rate in peers if rate <= info.rate_current)
        return 100.0 * hits / len(peers)

    def _metrics(self, info: Analysis, percentile: float | None) -> dict[str, float | int | None]:
        """组装纯数值指标（全部为数值或 None；非数值项 coverage_state 见 :meth:`_metadata`）。"""
        return {
            "rate_current": None if info.rate_current is None else round(info.rate_current, 4),
            "rate_previous": None if info.rate_previous is None else round(info.rate_previous, 4),
            "rate_delta": None if info.rate_delta is None else round(info.rate_delta, 4),
            "relative_change": None if info.relative_change is None else round(info.relative_change, 6),
            "coverage_ratio": round(info.coverage_ratio, 4),
            "staleness_hours": info.staleness_hours,
            "age_days": None if info.age_days is None else round(info.age_days, 4),
            "observed_windows": info.observed_windows,
            "sample_count": info.sample_count,
            "percentile": None if percentile is None else round(percentile, 4),
        }

    def _metadata(self, info: Analysis) -> dict[str, Any]:
        """组装非数值元数据通道；键集与仅数值的 ``metrics`` 互斥，供展示层并列消费。"""
        return {
            "confidence_kind": _confidence_kind_value(info.confidence_kind),
            "coverage_state": info.coverage_state.value,
            # ---- 08 案 §H2：稿龄证据走 metadata 通道（非数值项不进 metrics）----
            "as_of_epoch_s": info.as_of_epoch_s,
            "as_of_source": info.as_of_source,
            "age_status": info.age_status,
            "age_source": info.age_source,
            "age_reference": "evaluation_as_of",
            # ---- 08 案 §J3（B6a）：出现期依据（非数值项，不进 metrics）----
            "emergence_basis": info.emergence_basis,
        }

    def _explain(self, info: Analysis, percentile: float | None) -> str:
        """生成面向运营的可解释文案。"""
        label = _COVERAGE_LABELS[info.coverage_state]
        if info.rate_current is None:
            rate_text = "暂无可用的观察窗"
        else:
            rate_text = f"已观察区间新增强度 {info.rate_current:.1f} 播放/天"
        percentile_text = "同源样本不足，未计算百分位" if percentile is None else f"同分区第{percentile:.0f}百分位"
        return (
            f"固定{self.config.window_seconds // 3600}小时窗：覆盖{info.coverage_ratio:.0%}（{label}），"
            f"{rate_text}，当前为{info.stage}；有效窗{info.observed_windows}个、"
            f"陈旧度{info.staleness_hours:.1f}小时，{percentile_text}。"
        )
