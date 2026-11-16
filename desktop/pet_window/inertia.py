"""桌宠投掷惯性：释放测速与飞行推进（纯函数，不 import Qt）。

分层与 geometry.py 一致：本模块只做数值判定，屏幕尺寸、时钟、计时器都由
inertia_mixin.py 采集后作为参数传进来，因此可以脱离事件循环逐帧断言。

坐标系全部是 Qt 逻辑像素。一次投掷固定在「松手时所在屏幕」的可用区内运动、
不跨屏，所以逻辑坐标与设备像素比无关，不需要跨屏折算（跨屏折算只属于拖动
阶段，见 :func:`geometry.drag_target_across_screens`）。

物理模型只有速度、没有重力：

- 阻尼：``v ← v · e^(−λ·dt)``，λ 越大停得越快；
- 积分：``p ← p + v·dt``；
- 反弹：撞到边界时反转该轴速度并乘弹性系数，另一轴不受影响。

结束条件：速度低于 ``min_speed``、飞行时间超过 ``max_flight_s``，或单帧反射
次数异常（极小边界叠加极快速度）时贴边停住。所有路径都保证速度归零、位置留在
边界内，不会出现「无限飞」或「飞出屏幕」。
"""
from __future__ import annotations

from dataclasses import dataclass
from math import exp, hypot
from typing import Sequence

#: 一次位置采样：(毫秒时间戳, 窗口左上角逻辑 x, 窗口左上角逻辑 y)。
Sample = tuple[float, int, int]

#: 窗口左上角允许活动的矩形：(left, top, right, bottom)，单位逻辑 px。
Bounds = tuple[float, float, float, float]

#: 单帧最多反射几次。超出说明边界极小或速度极快，直接贴边停住而不是继续弹。
MAX_REFLECTIONS = 3

#: 单帧时间步的下限（秒）。避免 dt≈0 时「时间不前进、计时器却一直跑」。
MIN_STEP_S = 0.0001


@dataclass(frozen=True)
class InertiaConfig:
    """投掷手感的全部可调参数。

    改手感优先改这里的默认值，不要散落到 mixin 或事件回调里；:data:`DEFAULT_CONFIG`
    是唯一被生产代码引用的实例。

    阈值类参数（``min_throw_speed`` 起）同时决定了「慢拖不投掷」的三道闸，改动前
    请一并看 :func:`sample_velocity` 的说明。
    """

    #: 低于该速度视为慢拖，不投掷。单位逻辑 px/s。
    min_throw_speed: float = 250.0
    #: 甩速上限，超过按该值等比压缩，避免一甩就瞬移。
    #: 3800 ≈ 两屏宽/秒，覆盖快速甩动的手速；再高在 λ=2.0 下也滑不过一屏。
    max_throw_speed: float = 3800.0
    #: 测速只用最近这段时间内的采样。单位毫秒。
    sample_window_ms: float = 120.0
    #: 采样首末时间跨度下限；不足（如按下与释放落在同一毫秒）时无法判速。
    #: 12ms 约为 60Hz 事件间隔的 2/3：极快甩动往往只有两三个采样点、跨度很小，
    #: 门槛过高会把「甩得越快越不触发」变成常态；再短则速度会被像素量化放大。
    min_sample_span_ms: float = 12.0
    #: 最后一次采样距释放超过该值即视为「已停住」，不投掷。单位毫秒。
    release_stale_ms: float = 80.0
    #: 指数阻尼系数 λ。标准甩动约滑 0.5 屏宽、约 2s 停稳。
    damping_per_s: float = 2.0
    #: 撞边速度保留比例。0 为不反弹，1 为理想弹性。
    restitution: float = 0.65
    #: 低于该速度停稳。单位逻辑 px/s。
    min_speed: float = 40.0
    #: 单次飞行时长上限（秒），兜底防止参数异常导致永久飞行。
    max_flight_s: float = 6.0
    #: 单帧时间步上限（秒）。卡顿后不让一帧跨过整块屏幕。
    max_step_s: float = 0.05
    #: 单帧时间步下限（秒）。
    min_step_s: float = MIN_STEP_S


#: 生产代码使用的唯一参数实例。
DEFAULT_CONFIG = InertiaConfig()


@dataclass(frozen=True)
class FlightState:
    """飞行中的一帧状态。

    Attributes:
        x: 窗口左上角逻辑 x。
        y: 窗口左上角逻辑 y。
        vx: 水平速度，逻辑 px/s。
        vy: 垂直速度，逻辑 px/s。
        elapsed: 本次飞行已累计时长（秒）。
    """

    x: float
    y: float
    vx: float
    vy: float
    elapsed: float = 0.0

    @property
    def speed(self) -> float:
        """返回合速度大小（逻辑 px/s）。

        Returns:
            float: ``hypot(vx, vy)``。
        """
        return hypot(self.vx, self.vy)


def sample_velocity(
    samples: Sequence[Sample],
    release_ms: float,
    config: InertiaConfig = DEFAULT_CONFIG,
) -> tuple[float, float] | None:
    """用最近的采样估算释放瞬间的速度。

    「慢拖不触发投掷」靠三道闸，缺一都会误触发：

    1. 采样点少于 2 个、或首末时间跨度小于 ``min_sample_span_ms`` —— 一次事件里
       连续派发的按下/移动/释放跨度≈0，测不出速度，返回 ``None``（验收脚本正是
       这种派发方式）；
    2. 最后一次采样距释放超过 ``release_stale_ms`` —— 用户先停住再松手，返回
       ``None``，否则会把停住前的高速当成释放速度；
    3. 早于 ``sample_window_ms`` 的采样一律丢弃 —— 只跟最近几帧，抬手前的犹豫
       不会算进速度。

    Args:
        samples: 采样序列，按时间升序，每项 ``(毫秒, x, y)``。
        release_ms: 释放时刻，与采样使用同一时钟。
        config: 惯性参数。

    Returns:
        tuple[float, float] | None: ``(vx, vy)``，单位逻辑 px/s；无法可靠判断时
        返回 ``None``，调用方据此放弃投掷。
    """
    if len(samples) < 2:
        return None
    latest = samples[-1][0]
    if release_ms - latest > config.release_stale_ms:
        return None
    window_start = latest - config.sample_window_ms
    usable = [item for item in samples if item[0] >= window_start]
    if len(usable) < 2:
        return None
    first, last = usable[0], usable[-1]
    span = last[0] - first[0]
    if span < config.min_sample_span_ms:
        return None
    return (
        (last[1] - first[1]) * 1000.0 / span,
        (last[2] - first[2]) * 1000.0 / span,
    )


def should_throw(vx: float, vy: float, config: InertiaConfig = DEFAULT_CONFIG) -> bool:
    """判断速度是否达到投掷阈值。

    Args:
        vx: 水平速度，逻辑 px/s。
        vy: 垂直速度，逻辑 px/s。
        config: 惯性参数。

    Returns:
        bool: 合速度不小于 ``min_throw_speed`` 时为 True。
    """
    return hypot(vx, vy) >= config.min_throw_speed


def clamp_speed(vx: float, vy: float, limit: float) -> tuple[float, float]:
    """把速度等比压缩到上限以内，方向保持不变。

    Args:
        vx: 水平速度，逻辑 px/s。
        vy: 垂直速度，逻辑 px/s。
        limit: 合速度上限。

    Returns:
        tuple[float, float]: 压缩后的 ``(vx, vy)``；速度为零或未超限时原样返回。
    """
    speed = hypot(vx, vy)
    if speed <= 0 or speed <= limit:
        return vx, vy
    scale = max(limit, 0.0) / speed
    return vx * scale, vy * scale


def normalize_bounds(bounds: Bounds) -> Bounds:
    """规整边界，保证 ``right >= left``、``bottom >= top``。

    窗口比屏幕还大时可用区会算出反向矩形（``right < left``），这里收敛成零宽度，
    由 :func:`_advance_axis` 当作「贴边停住」处理，不会出现负方向弹跳。

    Args:
        bounds: ``(left, top, right, bottom)``。

    Returns:
        Bounds: 规整后的边界。
    """
    left, top, right, bottom = (float(value) for value in bounds)
    if right < left:
        right = left
    if bottom < top:
        bottom = top
    return left, top, right, bottom


def settle_into_bounds(x: float, y: float, bounds: Bounds) -> tuple[float, float]:
    """把坐标夹进边界内，用于飞行起点。

    Args:
        x: 期望的逻辑 x。
        y: 期望的逻辑 y。
        bounds: 边界矩形。

    Returns:
        tuple[float, float]: 夹紧后的 ``(x, y)``。
    """
    left, top, right, bottom = normalize_bounds(bounds)
    return min(max(float(x), left), right), min(max(float(y), top), bottom)


def _sanitize_step(dt: float, config: InertiaConfig) -> float:
    """把帧间隔收敛到可用区间。

    卡顿、系统休眠、时钟抖动都可能给出异常 dt；太小会让时间几乎不前进，太大
    会让一帧跨过整块屏幕直接穿墙。这里统一夹进 ``[min_step_s, max_step_s]``，
    NaN 与负值按 ``min_step_s`` 处理。

    Args:
        dt: 原始帧间隔（秒）。
        config: 惯性参数。

    Returns:
        float: 可用的帧间隔（秒），恒大于 0。
    """
    step = float(dt)
    if not step > 0:  # 同时拦住 NaN、0 与负数
        step = config.min_step_s
    step = min(step, config.max_step_s)
    return max(step, MIN_STEP_S)


def _advance_axis(
    position: float,
    velocity: float,
    low: float,
    high: float,
    restitution: float,
) -> tuple[float, float]:
    """推进单轴并处理撞边反射。

    用循环而不是一次判断，是为了覆盖「一帧内越过对面墙」的情况（极快速度或
    极小边界）；反射次数超过 :data:`MAX_REFLECTIONS` 说明参数异常，直接贴边
    并把该轴速度归零，保证一定会停。

    Args:
        position: 已积分的坐标。
        velocity: 该轴速度。
        low: 下界。
        high: 上界。
        restitution: 反弹速度保留比例。

    Returns:
        tuple[float, float]: ``(坐标, 速度)``，坐标一定落在 ``[low, high]``。
    """
    if high <= low:
        # 窗口比可用区还大：贴左上角且该轴不再产生速度。
        return low, 0.0
    for _ in range(MAX_REFLECTIONS + 1):
        if position < low:
            position = low + (low - position)
            velocity = -velocity * restitution
        elif position > high:
            position = high - (position - high)
            velocity = -velocity * restitution
        elif (position == low and velocity < 0) or (position == high and velocity > 0):
            # 刚好停在墙上、速度仍朝外：下一帧会再次越界，这里先反转。
            velocity = -velocity * restitution
        else:
            return position, velocity
    return min(max(position, low), high), 0.0


def step_flight(
    state: FlightState,
    dt: float,
    bounds: Bounds,
    config: InertiaConfig = DEFAULT_CONFIG,
) -> tuple[FlightState, bool]:
    """推进一帧飞行。

    阻尼先作用于速度、再用新速度积分位置，使「先减速再判断撞墙」与真实阻力
    一致；撞墙只反转该轴分量，另一轴继续滑行。

    Args:
        state: 当前帧状态。
        dt: 帧间隔（秒）；异常值会被 :func:`_sanitize_step` 收敛。
        bounds: 允许窗口左上角活动的矩形。
        config: 惯性参数。

    Returns:
        tuple[FlightState, bool]: 下一帧状态，以及本次飞行是否结束。结束帧的
        速度一定为 0、位置一定在边界内。
    """
    left, top, right, bottom = normalize_bounds(bounds)
    step = _sanitize_step(dt, config)
    decay = exp(-config.damping_per_s * step)
    vx = state.vx * decay
    vy = state.vy * decay
    x, vx = _advance_axis(state.x + vx * step, vx, left, right, config.restitution)
    y, vy = _advance_axis(state.y + vy * step, vy, top, bottom, config.restitution)
    elapsed = state.elapsed + step
    next_state = FlightState(x, y, vx, vy, elapsed)
    if next_state.speed < config.min_speed or elapsed >= config.max_flight_s:
        return FlightState(x, y, 0.0, 0.0, elapsed), True
    return next_state, False
