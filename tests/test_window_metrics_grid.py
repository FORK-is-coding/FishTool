"""P1 · 窗口网格 ``window_end_s`` 契约测试（FishTool 04 · R5 前置）。

被测：``modules/hotspot/algorithm/window_metrics.py::window_end_s``。

覆盖点（逐条对齐派单 P1）：
- daily(86400) / early(7200) 两组固定向量对拍 ``(as_of_s // W) * W``；
- 边界：``as_of_s=0``、刚好落在网格线上（不动点）、``as_of_s < W``；
- 非法输入一律 ``ValueError``：``as_of_s`` 为 bool / 负数 / float / 字符串 / None，
  ``window_s`` 为 0 / 负数 / 非 int；
- W 由调用方传入（不是模块里写死的常量）：同一 as_of 用 86400 与 7200 得不同结果。

纯函数断言，不触网、不落盘、不依赖时钟。
"""
from __future__ import annotations

import pytest

from modules.hotspot.algorithm.window_metrics import compute_window_delta, window_end_s

#: daily 窗口宽度（秒）。
DAY_W = 86400
#: early 窗口宽度（秒）。
EARLY_W = 7200


# --------------------------------------------------------------------------- 固定向量对拍


@pytest.mark.parametrize(
    "as_of_s,want",
    [
        (0, 0),
        (1, 0),
        (DAY_W - 1, 0),
        (DAY_W, DAY_W),
        (DAY_W + 1, DAY_W),
        (DAY_W * 3 + 5, DAY_W * 3),
        (DAY_W * 100, DAY_W * 100),
        (DAY_W * 100 + DAY_W - 1, DAY_W * 100),
        (1_800_000_000, (1_800_000_000 // DAY_W) * DAY_W),
    ],
)
def test_daily_grid_fixed_vectors(as_of_s: int, want: int) -> None:
    """daily 网格固定向量：``window_end_s(as_of, 86400) == (as_of // 86400) * 86400``。"""
    assert window_end_s(as_of_s, DAY_W) == want


@pytest.mark.parametrize(
    "as_of_s,want",
    [
        (0, 0),
        (1, 0),
        (EARLY_W - 1, 0),
        (EARLY_W, EARLY_W),
        (EARLY_W + 1, EARLY_W),
        (EARLY_W * 4 + 3, EARLY_W * 4),
        (1_800_000_000, (1_800_000_000 // EARLY_W) * EARLY_W),
    ],
)
def test_early_grid_fixed_vectors(as_of_s: int, want: int) -> None:
    """early 网格固定向量：``window_end_s(as_of, 7200) == (as_of // 7200) * 7200``。"""
    assert window_end_s(as_of_s, EARLY_W) == want


@pytest.mark.parametrize("as_of_s", [0, 1, 7199, 7200, 7201, 86399, 86400, 123456789])
def test_matches_floor_multiplication(as_of_s: int) -> None:
    """任意 as_of 都必须严格等于向下取整乘法（两组 W 都验）。"""
    assert window_end_s(as_of_s, DAY_W) == (as_of_s // DAY_W) * DAY_W
    assert window_end_s(as_of_s, EARLY_W) == (as_of_s // EARLY_W) * EARLY_W


# --------------------------------------------------------------------------- 边界


def test_as_of_zero_is_anchored_at_utc_epoch() -> None:
    """as_of_s=0（UTC 零点）本身就是网格原点。"""
    assert window_end_s(0, DAY_W) == 0
    assert window_end_s(0, EARLY_W) == 0


@pytest.mark.parametrize("k", [0, 1, 2, 7, 365])
def test_as_of_on_grid_line_is_fixed_point(k: int) -> None:
    """刚好落在网格线上的 as_of 必须原地不动（固定网格，不滑动）。"""
    assert window_end_s(DAY_W * k, DAY_W) == DAY_W * k
    assert window_end_s(EARLY_W * k, EARLY_W) == EARLY_W * k


def test_as_of_less_than_window_returns_zero() -> None:
    """``as_of_s < W`` 时右边界回落 0。"""
    assert window_end_s(DAY_W - 1, DAY_W) == 0
    assert window_end_s(EARLY_W - 1, EARLY_W) == 0
    assert window_end_s(1, DAY_W) == 0


# --------------------------------------------------------------------------- W 来自调用方


def test_window_width_is_caller_supplied_not_hardcoded() -> None:
    """W 必须是调用方传的：同一 as_of 用 86400 与 7200 得不同结果。"""
    as_of = 100_000  # 100000 // 86400 == 1 -> 86400；100000 // 7200 == 13 -> 93600
    daily = window_end_s(as_of, DAY_W)
    early = window_end_s(as_of, EARLY_W)
    assert daily == DAY_W
    assert early == EARLY_W * 13
    assert daily != early


@pytest.mark.parametrize("w", [1, 2, 60, 3600, 7200, 86400, 100000])
def test_w_is_honored_for_arbitrary_positive_int(w: int) -> None:
    """任意正整数 W 都被当作网格宽度使用（模块内无固定常量兜底）。"""
    as_of = 1_800_000_000
    assert window_end_s(as_of, w) == (as_of // w) * w


# --------------------------------------------------------------------------- 非法输入


@pytest.mark.parametrize(
    "bad_as_of",
    [
        True,          # bool 混入（int 子类，必须显式排除）
        False,
        -1,
        -86400,
        1.0,           # float
        0.0,
        "0",           # 字符串
        None,
        [],            # 非标量
        {},
    ],
)
def test_invalid_as_of_raises_value_error(bad_as_of) -> None:
    """as_of_s 非法（bool/负数/float/字符串/None 等）必须抛 ValueError。"""
    with pytest.raises(ValueError):
        window_end_s(bad_as_of, DAY_W)


@pytest.mark.parametrize(
    "bad_window",
    [
        0,
        -1,
        -7200,
        1.0,           # float
        "86400",       # 字符串
        None,
        True,          # bool 混入（W 必须是真正整数 int）
        False,
    ],
)
def test_invalid_window_raises_value_error(bad_window) -> None:
    """window_s 非正整数 int 必须抛 ValueError。"""
    with pytest.raises(ValueError):
        window_end_s(1_800_000_000, bad_window)


def test_returns_plain_int_type() -> None:
    """返回值必须是真 int（不是 bool / float / numpy 之类的伪装）。"""
    result = window_end_s(1_800_000_000, DAY_W)
    assert type(result) is int


# --------------------------------------------------------------------------- 占位签名


def test_compute_window_delta_is_placeholder() -> None:
    """``compute_window_delta`` 本批只留签名，恒抛 NotImplementedError（02 后续实现）。"""
    with pytest.raises(NotImplementedError):
        compute_window_delta(
            [],
            end_s=DAY_W,
            window_s=DAY_W,
            as_of_s=DAY_W,
            max_gap_s=DAY_W,
        )
