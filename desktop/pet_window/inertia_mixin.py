"""桌宠投掷惯性：采样、计时器与单屏边界采集（Qt 胶水）。

物理推进全在 :mod:`desktop.pet_window.inertia`（纯函数），这里只做四件事：

1. 从鼠标事件与窗口位置里采速度样本 —— 采「窗口自身的位移」而不是鼠标位移，
   因为那正是用户眼睛看到的速度，跨不同 DPI 的屏幕也不用额外折算；
2. 松手时用**松手所在屏幕**的可用区圈定飞行范围（扣掉任务栏），一次投掷
   只在这块屏内反射，绝不跨屏；
3. 用重复 ``QTimer`` 逐帧推进并 ``move()``，每帧走与拖拽完全相同的
   「移动三件套」（move → 尺寸纠正 → 菜单同步），避免跨屏尺寸漂移与菜单跟丢；
4. 按下 / 关窗 / 不可见 / 落定时把计时器停干净。

时间基准用 ``QElapsedTimer``（单调，不受系统时间调整影响），帧间隔取真实经过
时间而不是固定 16ms，卡顿时物理仍然正确（步长上限在纯函数内另有兜底）。
"""
from __future__ import annotations

from math import hypot

from PySide6.QtCore import QElapsedTimer, QPoint, QTimer
from PySide6.QtGui import QGuiApplication

from .assets import logger
from .inertia import (
    DEFAULT_CONFIG,
    FlightState,
    InertiaConfig,
    clamp_speed,
    sample_velocity,
    settle_into_bounds,
    should_throw,
    step_flight,
)


class InertiaMixin:
    """投掷惯性混入：采样、启动飞行、逐帧推进与「接住」。"""

    #: 飞行帧间隔（毫秒）。60fps 足够，再密只会增加 move() 次数。
    INERTIA_TICK_MS = 16

    #: 采样缓冲上限（个）。120ms 窗口按 16ms 一帧约 8 个，这里留三倍余量。
    INERTIA_SAMPLE_LIMIT = 24

    #: 手感参数。改默认值去 inertia.InertiaConfig，不要改这里。
    inertia_config: InertiaConfig = DEFAULT_CONFIG

    # ------------------------------------------------------------------
    # 探针：测试用替身覆盖这几个方法即可脱离真实时钟与屏幕
    # ------------------------------------------------------------------
    def _inertia_clock(self) -> QElapsedTimer:
        """懒创建单调时钟。

        Returns:
            QElapsedTimer: 已在运行的单调时钟。
        """
        clock = getattr(self, "_inertia_elapsed", None)
        if clock is None:
            clock = QElapsedTimer()
            clock.start()
            self._inertia_elapsed = clock
        return clock

    def _inertia_now_ms(self) -> float:
        """返回单调时钟当前时间。

        Returns:
            float: 毫秒；保留亚毫秒精度，避免 120ms 窗口内速度被取整放大。
        """
        return self._inertia_clock().nsecsElapsed() / 1_000_000.0

    def _inertia_bounds(self) -> tuple[float, float, float, float] | None:
        """返回松手所在屏幕里窗口左上角的允许范围（已扣掉任务栏）。

        Returns:
            tuple[float, float, float, float] | None: ``(left, top, right, bottom)``；
            取不到屏幕时返回 ``None``（例如无头环境），调用方据此放弃投掷。
        """
        window = self.frameGeometry()
        screen = (
            QGuiApplication.screenAt(window.center())
            or QGuiApplication.screenAt(window.topLeft())
            or QGuiApplication.primaryScreen()
        )
        if screen is None:
            return None
        area = screen.availableGeometry()
        width, height = self.width(), self.height()
        return (
            float(area.x()),
            float(area.y()),
            float(area.x() + area.width() - width),
            float(area.y() + area.height() - height),
        )

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def flight_active(self) -> bool:
        """当前是否有正在推进的投掷飞行。

        Returns:
            bool: 计时器在跑且持有飞行状态时为 True。
        """
        return getattr(self, "_inertia_state", None) is not None

    def _ensure_inertia_timer(self) -> QTimer:
        """懒创建飞行计时器。

        与项目里其它计时器不同，这里必须是**重复**计时器：飞行需要连续帧，而
        单次计时器每帧重启会多一次事件派发且更容易丢帧。父对象是本窗口，窗口
        销毁时随之回收；停止时机见 :meth:`_inertia_stop`。

        Returns:
            QTimer: 重复触发的飞行计时器。
        """
        timer = getattr(self, "_inertia_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(False)
            timer.timeout.connect(self._inertia_tick)
            self._inertia_timer = timer
        return timer

    def _inertia_stop(self, *, save_position: bool = False, restore_state: bool = True) -> None:
        """停止飞行并清空采样。

        Args:
            save_position: 是否把当前（落定）位置写回 QSettings。
            restore_state: 当前仍是「飞行中」时是否切回待机。通知等其它状态
                不该被投掷收尾覆盖，所以按当前状态判断。
        """
        timer = getattr(self, "_inertia_timer", None)
        if timer is not None:
            timer.stop()
        self._inertia_state = None
        self._inertia_bounds_snapshot = None
        self._inertia_samples = []
        if restore_state and getattr(self, "current_state", None) == getattr(
            self, "STATE_FLYING", object()
        ):
            self.set_state(getattr(self, "STATE_IDLE", 0))
        if save_position:
            saver = getattr(self, "_save_position_within_desktop", None)
            if saver is not None:
                saver()

    # ------------------------------------------------------------------
    # 钩子：由 DragMixin 的鼠标事件调用
    # ------------------------------------------------------------------
    def _inertia_catch(self) -> bool:
        """接住正在飞行的桌宠：停飞、位置不动。

        右键等非左键按下也走这里：只停飞，不碰「本次松手不弹菜单」标记与采样，
        否则会把随后一次正常的左键单击菜单一起吞掉。

        Returns:
            bool: 本次调用之前是否确实有飞行在推进。
        """
        if not self.flight_active:
            return False
        self._inertia_stop(save_position=True)
        logger.info("[桌宠] 飞行中按下，已接住")
        return True

    def _inertia_on_press(self, cursor=None) -> None:
        """左键按下：接住正在飞行的桌宠，并重新开始采样。

        接住不改位置：随后的 ``drag_position`` 由 DragMixin 按当时的窗口位置
        重算，所以从按下那一刻继续拖动不会瞬移。

        Args:
            cursor: 按下点全局坐标；仅用于调试，可为 ``None``。
        """
        caught = self._inertia_catch()
        # 接住那一下的松手只停飞、不弹菜单；DragMixin 读一次就清标记。
        self._suppress_click_menu = caught
        self._inertia_samples = [
            (self._inertia_now_ms(), self.pos().x(), self.pos().y())
        ]

    def _inertia_track(self) -> None:
        """拖拽过程中记录一帧位置样本。

        采窗口自身的 ``pos()``：它就是用户看到的速度，跨不同 DPI 的屏幕后也
        无需再按 DPR 折算（偏移换算只属于拖动阶段）。
        """
        if not getattr(self, "_left_button_pressed", False):
            return
        samples = list(getattr(self, "_inertia_samples", ()))
        samples.append((self._inertia_now_ms(), self.pos().x(), self.pos().y()))
        self._inertia_samples = samples[-self.INERTIA_SAMPLE_LIMIT :]

    def _inertia_on_release(self, was_dragging: bool) -> None:
        """鼠标释放：判定释放速度，达到阈值就启动飞行。

        Args:
            was_dragging: 本次按下到释放是否构成拖拽。单击不投掷，判定由
                DragMixin 负责，这里不重复计算。
        """
        samples = getattr(self, "_inertia_samples", [])
        self._inertia_samples = []
        if not was_dragging:
            return
        config = self.inertia_config
        velocity = sample_velocity(samples, self._inertia_now_ms(), config)
        if velocity is None:
            logger.debug("[桌宠] 释放时采样不足或已停住，不投掷")
            return
        vx, vy = clamp_speed(velocity[0], velocity[1], config.max_throw_speed)
        if not should_throw(vx, vy, config):
            logger.debug(f"[桌宠] 释放速度 {hypot(vx, vy):.0f}px/s 低于投掷阈值，不投掷")
            return
        bounds = self._inertia_bounds()
        if bounds is None:
            logger.warning("[桌宠] 未取到可用屏幕，放弃投掷")
            return
        x, y = settle_into_bounds(self.pos().x(), self.pos().y(), bounds)
        if (x, y) != (self.pos().x(), self.pos().y()):
            # 松手时半挂在屏幕边缘（或跨屏）：先夹进可用区，避免第一帧被边界
            # 强行弹回。代价是至多一次贴边移动，位置随后完全可预测。
            self.move(QPoint(int(x), int(y)))
            self._keep_pet_size_after_move()
            self._sync_menu_position()
        self._inertia_bounds_snapshot = bounds
        self._inertia_state = FlightState(float(x), float(y), vx, vy, 0.0)
        self._inertia_last_tick_ms = self._inertia_now_ms()
        flying = getattr(self, "STATE_FLYING", None)
        if flying is not None:
            self.set_state(flying)
        self._ensure_inertia_timer().start(self.INERTIA_TICK_MS)
        logger.info(
            f"[桌宠] 投掷: v=({vx:.0f}, {vy:.0f})px/s, 范围=({bounds[0]:.0f}, "
            f"{bounds[1]:.0f})-({bounds[2]:.0f}, {bounds[3]:.0f})"
        )

    # ------------------------------------------------------------------
    # 逐帧推进
    # ------------------------------------------------------------------
    def _inertia_tick(self, dt: float | None = None) -> None:
        """推进一帧飞行。

        Args:
            dt: 帧间隔（秒）。``None`` 时按真实经过时间计算（计时器调用路径）；
                测试直接传入固定值可得到完全确定的结果。
        """
        state = getattr(self, "_inertia_state", None)
        if state is None:
            self._inertia_stop(restore_state=False)
            return
        try:
            if getattr(self, "_left_button_pressed", False):
                # 拖拽中不该有飞行帧：宁可停飞，也不跟鼠标抢位置。
                self._inertia_stop(save_position=True)
                return
            visible = getattr(self, "isVisible", None)
            if visible is not None and not visible():
                self._inertia_stop(save_position=False)
                return
            bounds = getattr(self, "_inertia_bounds_snapshot", None)
            if bounds is None:
                self._inertia_stop(save_position=True)
                return
            if dt is None:
                now = self._inertia_now_ms()
                dt = (now - getattr(self, "_inertia_last_tick_ms", now)) / 1000.0
                self._inertia_last_tick_ms = now
            next_state, finished = step_flight(
                state, dt, bounds, self.inertia_config
            )
            self._inertia_state = next_state
            self.move(QPoint(int(round(next_state.x)), int(round(next_state.y))))
            self._keep_pet_size_after_move()
            syncer = getattr(self, "_sync_menu_position", None)
            if syncer is not None:
                syncer()
            if finished:
                self._inertia_stop(save_position=True)
        except Exception as exc:  # noqa: BLE001 - 计时器回调异常会每 16ms 刷一次
            logger.error(f"[桌宠] 飞行推进失败，已停止投掷: {exc}", exc_info=True)
            self._inertia_stop(save_position=True)
