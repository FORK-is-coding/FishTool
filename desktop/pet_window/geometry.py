"""桌宠窗口的屏幕可见性约束。

背景（多屏异构 DPI）：Windows 上把两块不同缩放的屏幕拼在一起（例如主屏
1920x1080 @100%、副屏 2560x1440 @150%），Qt 的逻辑坐标空间并不连续：屏幕原点
按主屏基准换算、屏幕尺寸按各自 DPR 换算，屏幕之间可能出现互不相邻的空隙。
窗口一旦被拖进空隙或任何屏幕之外，``QWidget.move()`` 的落点会变得不可预期
（实测请求逻辑坐标 -700 时窗口被送到主屏中间、请求 -3000 时被送到副屏之外），
用户表现为「桌宠消失」，而且坏坐标会被写进 QSettings，重启后依旧消失。

本模块负责两件事：把窗口拉回可见区，以及跨屏拖动时换算窗口目标坐标。

- :func:`clamp_rect` 是纯函数，只做几何运算、不依赖 Qt，可完整单测；
- :func:`clamp_to_desktop` 采集当前屏幕后调用纯函数，是可见性校正的生产入口；
- :func:`drag_target_across_screens` / :func:`drag_target` 是拖动换算的纯函数，
  由 ``DragMixin`` 每帧调用。

判定只看「窗口与屏幕的相交面积」：实测在异构 DPI 空隙里，窗口即使中心落在
屏幕外，物理上也常有一部分留在副屏右缘（可见、能用鼠标抓回），所以不做
「中心不在任何屏上就强制夹紧」这类附加判定，避免把正常的半挂拖动一并拽回。

约定：只调整位置，绝不改变尺寸；对已经足够可见的窗口原样返回（幂等），
因此在低频时机（松手、启动恢复）重复调用不会引入抖动。

已知限制（已知、暂不处理，见 PR 说明）：

- 窗口跨越不同 DPI 的屏幕边界的那一帧，Qt 会按新屏 DPR 重算窗口尺寸，桌宠可能
  出现一次极短的大小抽搐；``PetWindowBase._keep_pet_size`` 会在同一帧内纠正，
  但无法完全消除这一帧的视觉变化。
- 鼠标正好停在两块屏交界、且窗口目标位置需要跨越逻辑空隙时，个别鼠标事件会让
  位置微跳一次；继续移动即恢复与鼠标的绝对位置关系。
"""
from __future__ import annotations

from typing import Sequence

#: 单块屏幕快照：(几何 x, y, 宽, 高, 可用区 x, y, 宽, 高)，单位是 Qt 逻辑像素。
ScreenSnapshot = tuple[int, int, int, int, int, int, int, int]

#: 窗口矩形：(x, y, 宽, 高)，单位是 Qt 逻辑像素。
Rect = tuple[int, int, int, int]

#: 窗口至少要有这么多面积（逻辑 px²）落在某块屏幕上，才认为用户找得到它。
MIN_VISIBLE_PX = 4096

#: 窗口自身面积的最小可见比例，与 :data:`MIN_VISIBLE_PX` 取更容易满足的那个。
#: 取 15% 而不是更高：用户习惯把桌宠半挂在屏幕边缘，只要还露出够鼠标抓回来的一角
#: 就不该被强行拽回；真正判定「丢失」交给 :data:`MIN_VISIBLE_PX` 兜底。
MIN_VISIBLE_RATIO = 0.15


def _intersect_area(a: Rect, b: Rect) -> int:
    """返回两个矩形的相交面积（不相交时 0）。

    Args:
        a: 第一个矩形 ``(x, y, 宽, 高)``。
        b: 第二个矩形 ``(x, y, 宽, 高)``。

    Returns:
        int: 相交面积，单位逻辑 px²。
    """
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[0] + a[2], b[0] + b[2])
    bottom = min(a[1] + a[3], b[1] + b[3])
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


def _geometry(screen: ScreenSnapshot) -> Rect:
    """返回屏幕的完整几何（含任务栏区域）。

    Args:
        screen: 屏幕快照。

    Returns:
        Rect: ``(x, y, 宽, 高)``。
    """
    return screen[0], screen[1], screen[2], screen[3]


def _available(screen: ScreenSnapshot) -> Rect:
    """返回屏幕的可用区（已扣除任务栏 / Dock / 菜单栏）。

    Args:
        screen: 屏幕快照。

    Returns:
        Rect: ``(x, y, 宽, 高)``。
    """
    return screen[4], screen[5], screen[6], screen[7]


def _pick_target(window: Rect, screens: Sequence[ScreenSnapshot], visible: Sequence[int]) -> int:
    """选择夹紧要落到的目标屏幕。

    优先取与窗口相交面积最大的屏幕；全不相交时取中心点距离最近的屏幕。
    不假设主屏在左上角，也不假设任何具体布局。

    Args:
        window: 窗口矩形。
        screens: 屏幕快照列表。
        visible: 每块屏幕与窗口的相交面积，顺序与 ``screens`` 一致。

    Returns:
        int: 目标屏幕在列表中的下标。
    """
    best_area = max(visible)
    if best_area > 0:
        return list(visible).index(best_area)

    center_x = window[0] + window[2] / 2
    center_y = window[1] + window[3] / 2
    best_index = 0
    best_distance = None
    for index, screen in enumerate(screens):
        screen_x = screen[0] + screen[2] / 2
        screen_y = screen[1] + screen[3] / 2
        distance = (screen_x - center_x) ** 2 + (screen_y - center_y) ** 2
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


#: 带 DPR 的屏幕快照：(逻辑 x, y, 宽, 高, DPR)。
ScreenWithDpr = tuple[int, int, int, int, float]


def _find_logical_screen(
    x: float, y: float, screens: Sequence[ScreenWithDpr]
) -> ScreenWithDpr | None:
    """返回逻辑坐标所在的屏幕。

    Args:
        x: 逻辑 x。
        y: 逻辑 y。
        screens: 带 DPR 的屏幕快照列表。

    Returns:
        ScreenWithDpr | None: 命中的屏幕；没有命中时返回 None。
    """
    for screen in screens:
        if screen[0] <= x < screen[0] + screen[2] and screen[1] <= y < screen[1] + screen[3]:
            return screen
    return None


def drag_target_across_screens(
    cursor_x: float,
    cursor_y: float,
    offset_phys_x: float,
    offset_phys_y: float,
    screens: Sequence[ScreenWithDpr],
    primary_dpr: float,
) -> tuple[int, int] | None:
    """跨 DPI 拖动时，按「目标物理位置所在屏幕」折算窗口逻辑坐标。

    背景：Qt 的映射是 ``物理 = 屏逻辑原点 x 主屏DPR + (逻辑 - 屏逻辑原点) x 屏DPR``。
    在异构 DPI（主屏 100% + 副屏 150%）下，两块屏的逻辑矩形之间会出现物理上
    存在、逻辑上不存在的空隙；把「鼠标逻辑坐标 - 偏移」直接交给 ``move()``，
    目标一旦落进空隙，Qt 会把它按错误的屏幕解释（实测逻辑 -812 被送到物理 62），
    表现为桌宠跳到另一块屏、或者与鼠标脱节。

    这里改走物理坐标：先用鼠标所在屏把光标换算成物理，再减去物理偏移得到目标
    物理位置，最后用**目标所在屏**的逻辑空间反解出逻辑坐标。这样 ``move()``
    收到的坐标必然落在某块屏的逻辑矩形内，不会再经过空隙。

    Args:
        cursor_x: 鼠标逻辑 x。
        cursor_y: 鼠标逻辑 y。
        offset_phys_x: 按下时记录的物理偏移 x。
        offset_phys_y: 按下时记录的物理偏移 y。
        screens: 带 DPR 的屏幕快照。
        primary_dpr: 主屏设备像素比，用于换算屏幕的物理原点。

    Returns:
        tuple[int, int] | None: 窗口左上角逻辑坐标；屏幕信息不足时返回 None，
        调用方应回退到纯逻辑换算。
    """
    if not screens:
        return None
    primary = primary_dpr if primary_dpr > 0 else 1.0
    cursor_screen = _find_logical_screen(cursor_x, cursor_y, screens)
    if cursor_screen is None:
        return None

    origin_x, origin_y, _, _, cursor_dpr = cursor_screen
    dpr = cursor_dpr if cursor_dpr > 0 else 1.0
    phys_cursor_x = origin_x * primary + (cursor_x - origin_x) * dpr
    phys_cursor_y = origin_y * primary + (cursor_y - origin_y) * dpr
    target_phys_x = phys_cursor_x - offset_phys_x
    target_phys_y = phys_cursor_y - offset_phys_y

    for screen in screens:
        sx, sy, sw, sh, sdpr = screen
        scale = sdpr if sdpr > 0 else 1.0
        phys_left = sx * primary
        phys_top = sy * primary
        if not (phys_left <= target_phys_x < phys_left + sw * scale):
            continue
        if not (phys_top <= target_phys_y < phys_top + sh * scale):
            continue
        return (
            int(round(sx + (target_phys_x - phys_left) / scale)),
            int(round(sy + (target_phys_y - phys_top) / scale)),
        )
    return None


def drag_target(
    cursor_x: float,
    cursor_y: float,
    offset_phys_x: float,
    offset_phys_y: float,
    dpr: float,
) -> tuple[int, int]:
    """按物理偏移算出窗口左上角应处的逻辑坐标。

    抓手在桌宠上的位置是**物理**概念（手指按住的像素），它不随屏幕缩放变化；
    而 ``QWidget.move()`` 收的是逻辑坐标。跨到另一块 DPR 不同的屏幕后，若继续
    沿用按下时算出的逻辑偏移，抓取点会漂移（实测副屏 150% ↔ 主屏 100% 时偏移
    被放大/缩小 1.5 倍）。这里每帧按当前屏幕 DPR 把物理偏移折回逻辑坐标。

    Args:
        cursor_x: 鼠标当前逻辑 x（``QMouseEvent.globalPosition()``）。
        cursor_y: 鼠标当前逻辑 y。
        offset_phys_x: 按下时记录的物理偏移 x。
        offset_phys_y: 按下时记录的物理偏移 y。
        dpr: 鼠标当前所在屏幕的设备像素比；非正数按 1.0 处理。

    Returns:
        tuple[int, int]: 窗口左上角的逻辑坐标。
    """
    scale = dpr if dpr > 0 else 1.0
    return (
        int(round(cursor_x - offset_phys_x / scale)),
        int(round(cursor_y - offset_phys_y / scale)),
    )


def clamp_rect(
    window: Rect,
    screens: Sequence[ScreenSnapshot],
    *,
    min_visible_ratio: float = MIN_VISIBLE_RATIO,
    min_visible_px: int = MIN_VISIBLE_PX,
) -> Rect:
    """把窗口矩形夹紧到屏幕可见区内。

    判定只有一条：与所有屏幕的相交面积最大值低于阈值
    （``max(min_visible_px, 窗口面积 * min_visible_ratio)``）时才夹紧，否则
    原样返回，保证幂等：``clamp_rect(clamp_rect(r)) == clamp_rect(r)``。
    允许用户把窗口半挂在屏幕边缘，只要仍露出足够面积。

    为什么不用「窗口中心是否在某块屏上」作为附加信号：在异构 DPI 的空隙里
    窗口中心同样是屏外，但窗口本身往往仍部分可见（实测逻辑坐标 -900 的窗口
    物理落在副屏右缘），拿它做强制夹紧会把正常的半挂拖动一并拽回。

    Args:
        window: 窗口矩形 ``(x, y, 宽, 高)``。
        screens: 屏幕快照列表，每项见 :data:`ScreenSnapshot`；空列表时原样返回。
        min_visible_ratio: 最小可见比例。
        min_visible_px: 最小可见面积（逻辑 px²）。

    Returns:
        Rect: 夹紧后的窗口矩形；宽高始终与输入一致。
    """
    if not screens:
        return window

    x, y, width, height = window
    if width <= 0 or height <= 0:
        return window

    threshold = max(int(min_visible_px), int(width * height * min_visible_ratio))
    visible = [_intersect_area(window, _geometry(screen)) for screen in screens]
    if max(visible) >= threshold:
        return window

    target = _pick_target(window, screens, visible)
    available = _available(screens[target])
    if width > available[2]:
        # 窗口比目标屏还宽：贴左边，允许右侧溢出，不裁尺寸。
        next_x = available[0]
    else:
        next_x = min(max(x, available[0]), available[0] + available[2] - width)
    if height > available[3]:
        next_y = available[1]
    else:
        next_y = min(max(y, available[1]), available[1] + available[3] - height)
    return (next_x, next_y, width, height)


def screens_snapshot() -> list[ScreenSnapshot]:
    """采集当前所有屏幕的几何与可用区。

    Returns:
        list[ScreenSnapshot]: 屏幕快照；没有屏幕时为空列表。
    """
    from PySide6.QtGui import QGuiApplication

    snapshot: list[ScreenSnapshot] = []
    for screen in QGuiApplication.screens():
        geometry = screen.geometry()
        available = screen.availableGeometry()
        snapshot.append(
            (
                geometry.x(),
                geometry.y(),
                geometry.width(),
                geometry.height(),
                available.x(),
                available.y(),
                available.width(),
                available.height(),
            )
        )
    return snapshot


def clamp_to_desktop(window: Rect) -> Rect:
    """按当前真实桌面把窗口矩形夹紧到可见区。

    Args:
        window: 窗口矩形 ``(x, y, 宽, 高)``，Qt 逻辑像素。

    Returns:
        Rect: 夹紧后的窗口矩形；采集不到屏幕时原样返回。
    """
    screens = screens_snapshot()
    if not screens:
        return window
    return clamp_rect(window, screens)
