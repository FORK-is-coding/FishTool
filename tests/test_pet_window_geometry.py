"""桌宠窗口可见性约束（clamp）的纯函数与集成测试。

覆盖目标：
1. ``clamp_rect`` 在各种屏幕布局下的判定与夹紧结果（纯函数，不依赖 Qt 实例）；
2. 幂等性：``clamp_rect(clamp_rect(r)) == clamp_rect(r)``，这是「低频重复调用不抖动」的前提；
3. ``clamp_to_desktop`` 与真实屏幕的衔接（offscreen 单屏）；
4. 松手保存 / 启动恢复两条集成路径的越界修正与异常兜底。
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QRect, QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

import desktop.pet_window.base as pet_base
import desktop.pet_window.geometry as geometry
import desktop.pet_window.drag_mixin as pet_drag
from desktop.pet_window.geometry import clamp_rect, clamp_to_desktop, screens_snapshot


def screen(geometry, available=None):
    """构造一块屏幕快照。

    Args:
        geometry: 屏幕几何 ``(x, y, 宽, 高)``。
        available: 可用区；省略时与 ``geometry`` 相同。

    Returns:
        tuple: ``(gx, gy, gw, gh, ax, ay, aw, ah)``。
    """
    gx, gy, gw, gh = geometry
    ax, ay, aw, ah = available or geometry
    return (gx, gy, gw, gh, ax, ay, aw, ah)


#: 主屏 1920x1080 @100%。
PRIMARY = screen((0, 0, 1920, 1080), (0, 0, 1920, 1032))
#: 副屏在左侧、150% 缩放：逻辑 1707x960，原点仍是物理 -2560（异构 DPI 的典型形态）。
SECONDARY = screen((-2560, -357, 1707, 960), (-2560, -357, 1707, 912))


# ===========================================================================
# clamp_rect：判定
# ===========================================================================
def test_clamp_keeps_fully_visible_window():
    """完全在屏幕内的窗口原样返回。"""
    window = (900, 500, 150, 150)
    assert clamp_rect(window, [PRIMARY]) == window


def test_clamp_keeps_window_hanging_over_the_edge():
    """半挂在屏幕边缘、但仍露出足够面积时不做任何改动。"""
    window = (1800, 1000, 150, 150)  # 可见 120x80 = 9600 px²，约 42.7%
    assert clamp_rect(window, [PRIMARY]) == window


def test_clamp_pulls_back_fully_outside_window():
    """完全落在屏幕外时拉回最近的可用区。"""
    window = (3000, 2000, 150, 150)
    fitted = clamp_rect(window, [PRIMARY])
    assert fitted == (1920 - 150, 1032 - 150, 150, 150)


def test_clamp_pulls_back_window_below_visible_threshold():
    """可见面积低于阈值（这里只剩 20x20）时也要拉回。"""
    window = (1900, 1012, 150, 150)
    fitted = clamp_rect(window, [PRIMARY])
    assert fitted[0] <= PRIMARY[4] + PRIMARY[6] - 150
    assert fitted[1] <= PRIMARY[5] + PRIMARY[7] - 150


def test_clamp_keeps_rect_still_visible_in_dpi_gap():
    """异构 DPI 逻辑空隙里仍部分可见的窗口不强行拉回。

    实测该位置窗口物理落在副屏右缘（可见），只有真正落到屏幕外才需要救援；
    用「中心是否在某块屏上」做附加判定会把这类正常半挂一并拽回。
    """
    window = (-900, 200, 150, 150)  # 与副屏相交 47x150 = 7050 > 4096
    assert clamp_rect(window, [PRIMARY, SECONDARY]) == window


def test_clamp_pulls_back_when_only_a_sliver_is_visible():
    """只露出极窄一条（低于最小可见面积）时也要拉回。"""
    window = (-900, -500, 150, 150)  # 与副屏相交 47x7 = 329
    fitted = clamp_rect(window, [PRIMARY, SECONDARY])
    assert fitted == (-1003, -357, 150, 150), "应夹到副屏可用区右上角"


# ===========================================================================
# clamp_rect：目标屏选择
# ===========================================================================
def test_clamp_picks_nearest_screen_when_no_overlap():
    """与所有屏幕都不相交时，选中心点距离最近的屏幕。"""
    top_screen = screen((0, -1080, 1920, 1080))
    window = (2000, -400, 150, 150)
    fitted = clamp_rect(window, [PRIMARY, top_screen])
    assert fitted == (1770, -400, 150, 150), "应落到上方那块屏里"


def test_clamp_handles_negative_origin_screens():
    """副屏在主屏左上（负坐标）时按最近屏夹紧，不假设原点。"""
    left_screen = screen((-1920, -1080, 1920, 1080))
    window = (-300, -200, 150, 150)  # 完全落在两块屏之间
    fitted = clamp_rect(window, [PRIMARY, left_screen])
    assert fitted[0] >= left_screen[0] and fitted[1] >= left_screen[1]
    assert fitted[0] + 150 <= left_screen[0] + left_screen[2]
    assert fitted[1] + 150 <= left_screen[1] + left_screen[3]


def test_clamp_uses_available_area_not_full_geometry():
    """判定用完整几何，夹紧用可用区：落在任务栏区域的窗口被推到可用区内。"""
    with_taskbar = screen((0, 0, 1920, 1080), (0, 0, 1920, 1000))
    window = (100, 1060, 150, 150)  # 只剩 20px 高露出（150x20=3000 < 4096）
    fitted = clamp_rect(window, [with_taskbar])
    assert fitted == (100, 850, 150, 150)


def test_clamp_keeps_window_that_still_shows_a_corner():
    """窗口贴边后仍露出成块可见区域时不动，避免拖动到边缘时被强行拽回。"""
    window = (764, 639, 150, 150)  # 右侧只剩 36px：36x150=5400 > 4096
    assert clamp_rect(window, [PRIMARY]) == window


# ===========================================================================
# clamp_rect：边界与健壮性
# ===========================================================================
def test_clamp_returns_input_without_screens():
    """屏幕列表为空（热插拔瞬间）时不做任何改动，避免抖动。"""
    window = (700, 700, 150, 150)
    assert clamp_rect(window, []) == window


@pytest.mark.parametrize("size", [(0, 150), (150, 0), (-10, 150)])
def test_clamp_ignores_invalid_size(size):
    """宽高非正时不参与计算，原样返回。"""
    window = (100, 100, size[0], size[1])
    assert clamp_rect(window, [PRIMARY]) == window


def test_clamp_pins_top_left_when_window_larger_than_screen():
    """窗口比目标屏还大时贴左上，允许溢出但不裁尺寸。"""
    tiny = screen((0, 0, 400, 300))
    fitted = clamp_rect((380, 250, 500, 500), [tiny])
    assert fitted == (0, 0, 500, 500)


@pytest.mark.parametrize(
    "window,screens",
    [
        ((900, 500, 150, 150), [PRIMARY]),
        ((1800, 1000, 150, 150), [PRIMARY]),
        ((3000, 2000, 150, 150), [PRIMARY]),
        ((-900, 200, 150, 150), [PRIMARY, SECONDARY]),
        ((-3000, 200, 150, 150), [PRIMARY, SECONDARY]),
        ((2000, -400, 150, 150), [PRIMARY, screen((0, -1080, 1920, 1080))]),
        ((700, 700, 150, 150), []),
        ((380, 250, 500, 500), [screen((0, 0, 400, 300))]),
    ],
)
def test_clamp_is_idempotent(window, screens):
    """重复调用结果稳定：这是每帧/多次调用不产生累积漂移的前提。"""
    once = clamp_rect(window, screens)
    assert clamp_rect(once, screens) == once


# ===========================================================================
# clamp_to_desktop：真实屏幕衔接（offscreen 单屏）
# ===========================================================================
@pytest.fixture(scope="module")
def qt_app():
    """提供进程内唯一的 QApplication（offscreen）。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_screens_snapshot_shape(qt_app):
    """屏幕快照应包含至少一块屏，且每项是 8 元组。"""
    snapshot = screens_snapshot()
    assert snapshot, "测试环境至少应有一块屏幕"
    for item in snapshot:
        assert len(item) == 8
        assert item[2] > 0 and item[3] > 0


def test_clamp_to_desktop_keeps_visible_rect(qt_app):
    """屏内窗口经过真实屏幕采集后仍然不动。"""
    available = QGuiApplication.primaryScreen().availableGeometry()
    window = (available.x() + 10, available.y() + 10, 150, 150)
    assert clamp_to_desktop(window) == window


def test_clamp_to_desktop_pulls_back_offscreen_rect(qt_app):
    """完全在屏外的窗口被拉回可用区。"""
    available = QGuiApplication.primaryScreen().availableGeometry()
    window = (available.right() + 500, available.bottom() + 500, 150, 150)
    fitted = clamp_to_desktop(window)
    assert available.contains(QRect(fitted[0], fitted[1], fitted[2], fitted[3]))


# ===========================================================================
# 集成：松手保存与启动恢复
# ===========================================================================
class _SettingsStub:
    """QSettings 替身，避免测试写 Windows 注册表。"""

    def __init__(self):
        """初始化键值存储。"""
        self.values: dict = {}

    def setValue(self, key, value):  # noqa: N802 - 对齐 QSettings API
        """记录写入。"""
        self.values[key] = value

    def value(self, key, default=None):
        """读取写入值。"""
        return self.values.get(key, default)


class _WindowHost:
    """松手保存路径所需的最小宿主。"""

    def __init__(self):
        """准备位置、尺寸与调用记录。"""
        self.settings = _SettingsStub()
        self.moved: list = []

    def pos(self):
        """返回固定窗口坐标。"""
        return QPoint(10, 10)

    def frameGeometry(self):  # noqa: N802 - 对齐 Qt API
        """返回固定窗口外框（无边框窗口与 geometry 相同）。"""
        return QRect(10, 10, 150, 150)

    def width(self):
        """返回固定宽度。"""
        return 150

    def height(self):
        """返回固定高度。"""
        return 150

    def move(self, point):
        """记录移动目标。"""
        self.moved.append(QPoint(point))


def test_save_position_pulls_back_when_outside(monkeypatch):
    """松手时窗口若在可见区外，先移回再保存。"""
    monkeypatch.setattr(pet_drag, "clamp_to_desktop", lambda rect: (200, 300, rect[2], rect[3]))
    host = _WindowHost()

    pet_drag.DragMixin._save_position_within_desktop(host)

    assert host.moved == [QPoint(200, 300)]
    assert host.settings.values["position"] == QPoint(200, 300)


def test_save_position_keeps_original_when_inside(monkeypatch):
    """松手时窗口在可见区内，不产生额外移动。"""
    monkeypatch.setattr(pet_drag, "clamp_to_desktop", lambda rect: rect)
    host = _WindowHost()

    pet_drag.DragMixin._save_position_within_desktop(host)

    assert host.moved == []
    assert host.settings.values["position"] == QPoint(10, 10)


def test_save_position_survives_clamp_error(monkeypatch):
    """校正抛异常时仍要保存原坐标，不能打断松手流程。"""

    def _boom(_rect):
        raise RuntimeError("screen query failed")

    monkeypatch.setattr(pet_drag, "clamp_to_desktop", _boom)
    host = _WindowHost()

    pet_drag.DragMixin._save_position_within_desktop(host)

    assert host.moved == []
    assert host.settings.values["position"] == QPoint(10, 10)


class _BaseHost:
    """启动恢复路径所需的最小宿主。"""

    def width(self):
        """返回固定宽度。"""
        return 150

    def height(self):
        """返回固定高度。"""
        return 150


def test_clamp_saved_position_returns_original_when_visible(monkeypatch):
    """屏内保存位置原样返回。"""
    monkeypatch.setattr(pet_base, "clamp_to_desktop", lambda rect: rect)
    result = pet_base.PetWindowBase._clamp_saved_position(_BaseHost(), QPoint(100, 100))
    assert result == QPoint(100, 100)


def test_clamp_saved_position_fixes_outside_position(monkeypatch):
    """越界保存位置被修正到可见区。"""
    monkeypatch.setattr(pet_base, "clamp_to_desktop", lambda rect: (40, 50, rect[2], rect[3]))
    result = pet_base.PetWindowBase._clamp_saved_position(_BaseHost(), QPoint(-3000, 200))
    assert result == QPoint(40, 50)


def test_clamp_saved_position_falls_back_on_error(monkeypatch):
    """校正失败时沿用旧坐标，不影响启动。"""

    def _boom(_rect):
        raise RuntimeError("screen query failed")

    monkeypatch.setattr(pet_base, "clamp_to_desktop", _boom)
    result = pet_base.PetWindowBase._clamp_saved_position(_BaseHost(), QPoint(-3000, 200))
    assert result == QPoint(-3000, 200)


# ===========================================================================
# 跨 DPI 边界的尺寸漂移纠正
# ===========================================================================
class _SizeHost:
    """尺寸纠正所需的最小宿主。"""

    def __init__(self, size, expected=None):
        """记录当前尺寸与设计尺寸。

        Args:
            size: 当前 ``(宽, 高)``。
            expected: 设计尺寸；``None`` 表示尚未记录。
        """
        self._size = tuple(size)
        self._pet_size = QSize(*expected) if expected else None
        self.resized: list = []

    def size(self):
        """返回当前尺寸。"""
        return QSize(*self._size)

    def resize(self, size):
        """记录并应用尺寸。"""
        self.resized.append((size.width(), size.height()))
        self._size = (size.width(), size.height())


def test_keep_pet_size_restores_drifted_size():
    """跨屏被重算成 100x100 时恢复成设计尺寸。"""
    host = _SizeHost((100, 100), expected=(150, 150))
    pet_base.PetWindowBase._keep_pet_size(host)
    assert host.resized == [(150, 150)]


def test_keep_pet_size_is_noop_when_size_matches():
    """尺寸正常时不做任何改动（可高频调用）。"""
    host = _SizeHost((150, 150), expected=(150, 150))
    pet_base.PetWindowBase._keep_pet_size(host)
    assert host.resized == []


def test_keep_pet_size_without_baseline_does_nothing():
    """没有记录设计尺寸的宿主（测试替身）直接跳过。"""
    host = _SizeHost((150, 150), expected=None)
    pet_base.PetWindowBase._keep_pet_size(host)
    assert host.resized == []


def test_keep_pet_size_after_move_tolerates_host_without_keeper():
    """测试替身没有尺寸纠正能力时，移动后的纠正调用必须安全跳过。"""

    class _Bare:
        pass

    pet_drag.DragMixin._keep_pet_size_after_move(_Bare())  # 不应抛异常


def test_keep_pet_size_after_move_calls_host_keeper():
    """真实宿主存在纠正方法时会被调用一次。"""
    calls = []

    class _WithKeeper:
        def _keep_pet_size(self):
            calls.append(1)

    pet_drag.DragMixin._keep_pet_size_after_move(_WithKeeper())
    assert calls == [1]


# ===========================================================================
# 跨屏拖动：按目标物理位置所在屏幕折算逻辑坐标
# ===========================================================================
#: 主屏 1920x1080 @100%。
_S_MAIN = (0, 0, 1920, 1080, 1.0)
#: 副屏 1707x960 @150%，逻辑原点 -2560（异构 DPI 的典型形态）。
_S_SIDE = (-2560, -357, 1707, 960, 1.5)


def test_drag_target_keeps_cursor_anchor_on_same_screen():
    """同屏内：物理偏移 / DPR 后与鼠标保持固定距离。"""
    result = geometry.drag_target_across_screens(900, 500, 112.5, 112.5, [_S_MAIN], 1.0)
    assert result == (788, 388)


def test_drag_target_switches_to_target_screen_space():
    """目标物理位置落在副屏时，用副屏的逻辑空间给出坐标（避开逻辑空隙）。"""
    # 鼠标在主屏最左 (0, 275)，物理偏移 112.5 -> 目标物理在副屏右部 (-112.5, 162.5)
    result = geometry.drag_target_across_screens(0, 275, 112.5, 112.5, [_S_MAIN, _S_SIDE], 1.0)
    assert result is not None
    x, y = result
    # 反算：副屏逻辑 -> 物理，应回到目标物理位置
    phys_x = _S_SIDE[0] * 1.0 + (x - _S_SIDE[0]) * 1.5
    phys_y = _S_SIDE[1] * 1.0 + (y - _S_SIDE[1]) * 1.5
    assert abs(phys_x - (-112.5)) <= 1.5
    assert abs(phys_y - 162.5) <= 1.5


def test_drag_target_stays_in_side_screen_when_cursor_there():
    """鼠标在副屏内：结果必须落在副屏逻辑矩形里，而不是空隙。"""
    result = geometry.drag_target_across_screens(-1200, 275, 112.5, 112.5, [_S_MAIN, _S_SIDE], 1.0)
    assert result is not None
    x, y = result
    assert _S_SIDE[0] <= x < _S_SIDE[0] + _S_SIDE[2]
    assert _S_SIDE[1] <= y < _S_SIDE[1] + _S_SIDE[3]


def test_drag_target_returns_none_without_screens():
    """没有屏幕信息时返回 None，交由调用方回退。"""
    assert geometry.drag_target_across_screens(0, 0, 10, 10, [], 1.0) is None


def test_drag_target_returns_none_when_cursor_offscreen():
    """鼠标逻辑坐标不在任何屏（异常输入）时返回 None。"""
    assert geometry.drag_target_across_screens(-400, 0, 10, 10, [_S_MAIN, _S_SIDE], 1.0) is None


def test_drag_target_with_non_unit_primary_dpr():
    """主屏非 100% 时，屏幕物理原点按主屏 DPR 换算。"""
    # 主屏 1.5、副屏 1.0：副屏逻辑原点 = -2560/1.5 ≈ -1707，物理原点仍是 -2560
    main = (0, 0, 1280, 720, 1.5)
    side = (-1707, -238, 2560, 1440, 1.0)
    result = geometry.drag_target_across_screens(10, 200, 60, 60, [main, side], 1.5)
    assert result is not None
    x, _ = result
    phys_x = side[0] * 1.5 + (x - side[0]) * 1.0
    # 光标物理 = 10*1.5 = 15，减去 60 -> -45
    assert abs(phys_x - (-45)) <= 2
