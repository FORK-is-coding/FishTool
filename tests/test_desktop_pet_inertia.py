"""桌宠投掷惯性（纯物理）用例。

覆盖目标：
1. ``InertiaConfig`` 的默认值就是产品拍板的手感参数（防手滑改动）；
2. 释放测速的三道闸 —— 采样不足/跨度过小/已停住都不会误触发投掷；
3. 单帧推进：阻尼衰减、撞边反射、弹性系数、贴边停止、异常 dt 兜底；
4. 「不跨屏、不无限飞」——任意输入下坐标始终留在边界内、速度终会归零；
5. 纯函数约束：``inertia.py`` 不 import Qt，可以脱离事件循环逐帧断言。
"""
from __future__ import annotations

import inspect
import math
from pathlib import Path

import pytest

import desktop.pet_window.inertia as inertia
from desktop.pet_window.inertia import (
    DEFAULT_CONFIG,
    FlightState,
    InertiaConfig,
    clamp_speed,
    normalize_bounds,
    sample_velocity,
    settle_into_bounds,
    should_throw,
    step_flight,
)

#: 1536x864 的可用区里，150x150 的桌宠左上角允许范围。
BOUNDS = (0.0, 0.0, 1386.0, 714.0)


# ===========================================================================
# 1. 参数契约
# ===========================================================================
def test_default_config_matches_agreed_handles():
    """默认参数必须与已确认的手感一致，改动需同步本用例与文档。"""
    config = DEFAULT_CONFIG

    assert config.min_throw_speed == 250.0
    assert config.max_throw_speed == 3800.0
    assert config.sample_window_ms == 120.0
    assert config.min_sample_span_ms == 12.0
    assert config.release_stale_ms == 80.0
    assert config.damping_per_s == 2.0
    assert config.restitution == 0.65
    assert config.min_speed == 40.0
    assert config.max_flight_s == 6.0
    assert config.max_step_s == 0.05
    assert InertiaConfig() == DEFAULT_CONFIG, "无参构造应等于生产实例"


def test_inertia_module_does_not_import_qt():
    """纯函数模块不允许 import Qt：否则无法脱离事件循环单测。"""
    source = Path(inertia.__file__).read_text(encoding="utf-8")

    assert "from PySide6" not in source
    assert "import PySide6" not in source


# ===========================================================================
# 2. 释放测速：三道闸
# ===========================================================================
def test_sample_velocity_needs_two_samples():
    """只有一个采样点时测不出速度。"""
    assert sample_velocity([(0, 100, 100)], 10) is None


def test_sample_velocity_rejects_zero_span():
    """按下与释放落在同一毫秒（验收脚本的派发方式）不能判速。"""
    samples = [(0, 100, 100), (0, 200, 100)]

    assert sample_velocity(samples, 0) is None


def test_sample_velocity_rejects_short_span():
    """跨度小于 min_sample_span_ms 时无法可靠判速。"""
    samples = [(0, 0, 0), (10, 500, 0)]  # 10ms < 12ms

    assert sample_velocity(samples, 10) is None


def test_sample_velocity_rejects_stale_release():
    """先停住再松手：末次采样距今超过 release_stale_ms 时按静止处理。"""
    samples = [(0, 0, 0), (100, 120, 0)]

    assert sample_velocity(samples, 100 + 81) is None
    assert sample_velocity(samples, 100 + 80) is not None, "恰好等于阈值仍可判速"


def test_sample_velocity_only_uses_recent_window():
    """窗口外的旧采样被丢弃，只按最近几帧算速度。"""
    # 0~300ms 极慢（100px/300ms），300~400ms 极快（300px/100ms）
    samples = [(0, 0, 0), (300, 100, 0), (400, 400, 0)]
    vx, vy = sample_velocity(samples, 400)

    assert vx == pytest.approx(3000.0, rel=1e-6)  # (400-100)px / 100ms
    assert vy == 0.0


def test_sample_velocity_returns_speed_and_direction():
    """正常同向快拖：速度为正，单位 px/s。"""
    samples = [(0, 100, 100), (40, 200, 100), (80, 320, 120)]
    vx, vy = sample_velocity(samples, 90)

    assert vx == pytest.approx(2750.0, rel=1e-6)  # 220px / 80ms
    assert vy == pytest.approx(250.0, rel=1e-6)  # 20px / 80ms


def test_sample_velocity_handles_reverse_direction():
    """反向拖动时速度为负，用于决定飞行方向。"""
    samples = [(0, 500, 300), (100, 400, 200)]
    vx, vy = sample_velocity(samples, 100)

    assert vx == pytest.approx(-1000.0, rel=1e-6)
    assert vy == pytest.approx(-1000.0, rel=1e-6)


# ===========================================================================
# 3. 阈值与速度压缩
# ===========================================================================
@pytest.mark.parametrize(
    ("vx", "vy", "expected"),
    [
        (0.0, 0.0, False),
        (249.9, 0.0, False),
        (250.0, 0.0, True),
        (150.0, 200.0, True),  # 合速度 250
        (-250.0, 0.0, True),
    ],
)
def test_should_throw_threshold(vx, vy, expected):
    """投掷阈值按合速度判定，方向不影响触发。"""
    assert should_throw(vx, vy) is expected


def test_clamp_speed_limits_magnitude_but_keeps_direction():
    """超过上限时等比压缩，方向角不变。"""
    vx, vy = clamp_speed(3000.0, 4000.0, 2200.0)

    assert math.hypot(vx, vy) == pytest.approx(2200.0, rel=1e-9)
    assert vx / vy == pytest.approx(3000.0 / 4000.0, rel=1e-9)


def test_clamp_speed_is_noop_below_limit_and_for_zero():
    """未超限与零速度向量原样返回。"""
    assert clamp_speed(100.0, 50.0, 2200.0) == (100.0, 50.0)
    assert clamp_speed(0.0, 0.0, 2200.0) == (0.0, 0.0)


def test_clamp_speed_with_zero_limit_zeroes_velocity():
    """上限为 0（异常配置）时速度归零，而不是除零。"""
    assert clamp_speed(100.0, 100.0, 0.0) == (0.0, 0.0)


# ===========================================================================
# 4. 边界与起点
# ===========================================================================
def test_normalize_bounds_fixes_inverted_rectangle():
    """窗口比屏幕还大时可用区会反向，规整成零宽度而不是负方向。"""
    assert normalize_bounds((100.0, 50.0, 40.0, 10.0)) == (100.0, 50.0, 100.0, 50.0)


def test_normalize_bounds_keeps_valid_rectangle():
    """正常矩形不变。"""
    assert normalize_bounds(BOUNDS) == BOUNDS


@pytest.mark.parametrize(
    ("x", "y", "expected"),
    [
        (200.0, 300.0, (200.0, 300.0)),
        (-50.0, -80.0, (0.0, 0.0)),
        (9999.0, 9999.0, (1386.0, 714.0)),
    ],
)
def test_settle_into_bounds_clamps_start(x, y, expected):
    """飞行起点必须夹进松手所在屏幕的可用区。"""
    assert settle_into_bounds(x, y, BOUNDS) == expected


# ===========================================================================
# 5. 单帧推进
# ===========================================================================
def test_step_advances_and_decays():
    """无碰撞的一帧：位置前进、速度按指数衰减。"""
    state = FlightState(500.0, 300.0, 1000.0, 0.0)
    next_state, finished = step_flight(state, 0.05, BOUNDS)

    assert finished is False
    assert next_state.x > state.x
    assert next_state.speed < state.speed
    assert next_state.elapsed == pytest.approx(0.05)


def test_step_reflects_on_right_wall():
    """撞右墙：x 夹回边界内、水平速度反向并乘弹性系数。"""
    state = FlightState(1380.0, 300.0, 1000.0, 0.0)
    next_state, finished = step_flight(state, 0.05, BOUNDS)

    assert finished is False
    assert next_state.x <= BOUNDS[2]
    assert next_state.vx < 0
    assert abs(next_state.vx) == pytest.approx(1000.0 * math.exp(-2.0 * 0.05) * 0.65)


def test_step_reflects_on_top_wall():
    """撞上墙：只有垂直分量反向。"""
    state = FlightState(500.0, 1.0, 500.0, -800.0)
    next_state, _ = step_flight(state, 0.05, BOUNDS)

    assert next_state.y >= BOUNDS[1]
    assert next_state.vy > 0
    assert next_state.vx > 0


def test_step_with_zero_restitution_stops_normal_component():
    """弹性为 0 时贴边停下该轴，不反弹。"""
    config = InertiaConfig(restitution=0.0)
    state = FlightState(1380.0, 300.0, 2000.0, 0.0)
    next_state, _ = step_flight(state, 0.05, BOUNDS, config)

    assert next_state.vx == 0.0
    assert next_state.x <= BOUNDS[2]


def test_step_survives_multiple_reflections_in_one_frame():
    """极小边界叠加极快速度：一帧内多次反射后仍留在边界内。"""
    tiny = (0.0, 0.0, 10.0, 10.0)
    state = FlightState(5.0, 5.0, 60000.0, 60000.0)
    next_state, _ = step_flight(state, 0.05, tiny)

    assert tiny[0] <= next_state.x <= tiny[2]
    assert tiny[1] <= next_state.y <= tiny[3]


def test_step_pins_when_window_larger_than_screen():
    """窗口比可用区还大：贴左上角且该轴速度归零，不会负方向乱弹。"""
    degenerate = (50.0, 60.0, 50.0, 60.0)
    state = FlightState(0.0, 0.0, 1000.0, -1000.0)
    next_state, _ = step_flight(state, 0.05, degenerate)

    assert (next_state.x, next_state.y) == (50.0, 60.0)
    assert (next_state.vx, next_state.vy) == (0.0, 0.0)


@pytest.mark.parametrize("dt", [0.0, -1.0, float("nan")])
def test_step_sanitizes_bad_dt(dt):
    """异常 dt 按最小步长处理：时间仍然前进，不会死循环。"""
    state = FlightState(500.0, 300.0, 0.0, 0.0)
    next_state, _ = step_flight(state, dt, BOUNDS)

    assert next_state.elapsed > 0.0


def test_step_clamps_huge_dt_to_max_step():
    """卡顿给出 10s 的 dt 时，按 max_step_s 推进，不会一帧穿墙。"""
    state = FlightState(0.0, 0.0, 2200.0, 0.0)
    huge, _ = step_flight(state, 10.0, BOUNDS)
    capped, _ = step_flight(state, DEFAULT_CONFIG.max_step_s, BOUNDS)

    assert huge.elapsed == pytest.approx(DEFAULT_CONFIG.max_step_s)
    assert huge.x == pytest.approx(capped.x)
    assert BOUNDS[0] <= huge.x <= BOUNDS[2]


# ===========================================================================
# 6. 结束条件
# ===========================================================================
def test_step_finishes_when_speed_below_minimum():
    """速度低于 min_speed：本次飞行结束，速度归零。"""
    state = FlightState(500.0, 300.0, DEFAULT_CONFIG.min_speed - 1.0, 0.0)
    next_state, finished = step_flight(state, 0.016, BOUNDS)

    assert finished is True
    assert (next_state.vx, next_state.vy) == (0.0, 0.0)


def test_step_finishes_on_max_flight_time():
    """飞行超过 max_flight_s 时强制结束（即使速度还很大）。"""
    config = InertiaConfig(max_flight_s=0.03)
    state = FlightState(500.0, 300.0, 2200.0, 0.0)
    next_state, finished = step_flight(state, 0.05, BOUNDS, config)

    assert finished is True
    assert (next_state.vx, next_state.vy) == (0.0, 0.0)
    assert BOUNDS[0] <= next_state.x <= BOUNDS[2]


def test_full_flight_terminates_inside_bounds():
    """标准甩动的完整飞行：有限帧内结束，且每一帧都在边界内。"""
    state = FlightState(300.0, 200.0, 2000.0, 900.0)
    frames = 0
    while True:
        state, finished = step_flight(state, 0.016, BOUNDS)
        frames += 1
        assert BOUNDS[0] <= state.x <= BOUNDS[2]
        assert BOUNDS[1] <= state.y <= BOUNDS[3]
        if finished:
            break
        assert frames < 1000, "飞行必须在 max_flight_s 内结束"

    assert state.elapsed <= DEFAULT_CONFIG.max_flight_s + 1e-9
    assert state.speed == 0.0


def test_full_flight_stays_on_single_screen_bounds():
    """投掷限制在松手所在屏的可用区内：坐标永不越界（不跨屏）。"""
    state = FlightState(10.0, 10.0, -2200.0, 2200.0)
    for _ in range(400):
        state, finished = step_flight(state, 0.016, BOUNDS)
        assert BOUNDS[0] <= state.x <= BOUNDS[2]
        assert BOUNDS[1] <= state.y <= BOUNDS[3]
        if finished:
            break


def test_flight_bounces_visible_number_of_times():
    """贴着墙甩出去应发生方向变化（而不是贴边不弹）。

    注意：指数阻尼下速度随**距离**线性衰减（``v(x) = v0 - λ·Δx``），从远处甩
    出去可能在到墙前就已低于 ``min_speed`` 而停住，所以这里从靠近右墙处起步。
    """
    state = FlightState(1300.0, 200.0, 2200.0, 0.0)
    flipped = 0
    previous_vx = state.vx
    for _ in range(400):
        state, finished = step_flight(state, 0.016, BOUNDS)
        if state.vx * previous_vx < 0:
            flipped += 1
        previous_vx = state.vx
        if finished:
            break

    assert flipped >= 1, "撞到右墙后至少反向一次"


# ===========================================================================
# 6. 极快甩动：触发面与速度上限
# ===========================================================================
def test_very_fast_flick_is_still_throwable():
    """极快甩动的采样跨度可能只有十几毫秒，也必须能判出速度并触发投掷。

    回归背景：``min_sample_span_ms`` 早先取 25ms，快速甩动常只有两三个采样点、
    跨度 15~24ms，被误判为「无法判速」而完全不触发；表现为「甩得越快越不飞」。
    """
    samples = [(0.0, 0, 0), (15.0, 300, 0)]  # 15ms 内 300px，约 20000px/s

    velocity = sample_velocity(samples, 15.0)
    assert velocity is not None, "15ms 跨度必须仍可判速"
    vx, vy = clamp_speed(velocity[0], velocity[1], DEFAULT_CONFIG.max_throw_speed)
    assert math.hypot(vx, vy) == pytest.approx(DEFAULT_CONFIG.max_throw_speed, rel=1e-9)
    assert should_throw(vx, vy, DEFAULT_CONFIG) is True


def test_throw_ceiling_covers_hand_speed():
    """上限要能容纳常见甩动手速，而不是把快速甩动一律压到「慢动作」。"""
    assert DEFAULT_CONFIG.max_throw_speed >= 3600.0

    vx, vy = clamp_speed(6000.0, 0.0, DEFAULT_CONFIG.max_throw_speed)
    assert math.hypot(vx, vy) == pytest.approx(DEFAULT_CONFIG.max_throw_speed, rel=1e-9)


def test_sample_span_gate_still_blocks_noise():
    """放宽到 12ms 之后，真正的噪声级跨度（几毫秒）仍然被挡住。"""
    assert sample_velocity([(0.0, 0, 0), (5.0, 4, 0)], 5.0) is None
