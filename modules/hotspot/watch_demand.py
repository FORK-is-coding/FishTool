"""需求 → 采样节奏 / 准入派生 reason（FishTool 04 · 第二批 B/E）。

本模块是「需求命名空间 → 采样节奏」与「派生准入 reason」的**唯一实现**，02 编排层
（:mod:`modules.hotspot.watch_service`）只经它，不许在别处再抄一份。

依据（逐字对齐，不自创）：
- ``FishTool_04_..._02补充执行案`` §6.4 L635「多事件需求取最高采样频率但不能重复调用」；
  同一处「04申请快速追踪→更新需求，02调度使用需求interval」；
- §6.3 L623「普通watch：02默认1小时」；
- §6.3 L681 / §8.3 L682「关闭fast但仍有events普通需求时降为1h；关闭04撤全部events需求，
  manual/ranking原需求继续。仅events的目标停止，保留历史」；
- §6.5 L648「读取已有 ``HotspotWatch.active=False`` 和 ``stop_reason='manual_stop'``，
  返回派生 reason_code=blocked_by_user；这不是另一个必须新增的数据库列」。

设计口径：
- 每个命名空间有一个**默认间隔**（本批三者都 3600s，即 §6.3「普通watch 02默认1小时」）；
- 需求条目可自带 ``interval_s``（正整数秒）覆盖默认值，供调用方/测试表达「快采需求」；
- 目标行的**生效节奏 = 当前所有需求命名空间的间隔取最小值**（最高频），无任何需求时返回
  ``None``（表示「无需求 → 停采」）；
- 撤销某个命名空间后，剩余命名空间里最激进的那个自然成为新节奏 —— 这就是「退回其原节奏」；
- 派生 reason 一律**现算**，绝不落成新列（manual_stop 本身就是持久标记）。
"""
from __future__ import annotations

__all__ = [
    "NAMESPACE_DEFAULT_INTERVAL_S",
    "REASON_BLOCKED_BY_USER",
    "REASON_NO_DEMAND",
    "REASON_RELEASED",
    "REASON_TRACKING",
    "demand_reason",
    "descriptor_interval_s",
    "has_demands",
    "namespace_intervals",
    "resolve_interval_s",
]

#: 命名空间默认采样间隔（秒）。本批三者都按 §6.3「普通watch 02默认1小时」= 3600s。
NAMESPACE_DEFAULT_INTERVAL_S: dict[str, int] = {
    "manual": 3600,
    "ranking": 3600,
    "events": 3600,
}

#: 派生准入 reason：用户在 02 手动停追，04 不得自动重开（§6.5 L648）。
REASON_BLOCKED_BY_USER: str = "blocked_by_user"
#: 派生准入 reason：仍在池且有点需求，可正常参与调度。
REASON_TRACKING: str = "tracking"
#: 派生准入 reason：已释放且不是 manual_stop（如 expired / events_revoked）。
REASON_RELEASED: str = "released"
#: 派生准入 reason：在池但当前没有任何需求（不应长期存在，属异常可观测态）。
REASON_NO_DEMAND: str = "no_demand"


def descriptor_interval_s(namespace: str, descriptor: object) -> int:
    """读某条需求描述声明的采样间隔；缺失 / 非法时回退该命名空间默认间隔。

    Args:
        namespace: 需求命名空间。
        descriptor: 该条需求描述（``events`` 里是 ``{"bvid": ...}`` 形态；``manual`` /
            ``ranking`` 里是 ``{...}`` 形态）。可为 None。

    Returns:
        int: 正整数秒的采样间隔。
    """
    raw = descriptor.get("interval_s") if isinstance(descriptor, dict) else None
    # bool 是 int 子类，必须显式排除，避免 True/False 被当成 1/0。
    if type(raw) is int and raw > 0:
        return raw
    return NAMESPACE_DEFAULT_INTERVAL_S.get(namespace, 3600)


def has_demands(source_demands: object) -> bool:
    """判断 ``source_demands`` 里是否还有至少一个**非空**命名空间子快照。

    Args:
        source_demands: ``hotspot_watch.source_demands`` 解析后的对象。

    Returns:
        bool: 任一名命空间有非空字典即为 True；非法 / 空结构为 False。
    """
    if not isinstance(source_demands, dict):
        return False
    for payload in source_demands.values():
        if isinstance(payload, dict) and payload:
            return True
    return False


def namespace_intervals(source_demands: object) -> dict[str, int]:
    """算出每个「有需求」命名空间当前的生效间隔（同命名空间内取最小）。

    Args:
        source_demands: ``hotspot_watch.source_demands`` 解析后的对象。

    Returns:
        dict[str, int]: ``namespace -> 生效间隔秒``；无需求的命名空间不出现。
    """
    out: dict[str, int] = {}
    if not isinstance(source_demands, dict):
        return out
    for namespace, payload in source_demands.items():
        if not isinstance(namespace, str) or not isinstance(payload, dict) or not payload:
            continue
        best: int | None = None
        for descriptor in payload.values():
            interval = descriptor_interval_s(namespace, descriptor)
            best = interval if best is None else min(best, interval)
        if best is not None:
            out[namespace] = best
    return out


def resolve_interval_s(source_demands: object) -> int | None:
    """解析目标行的生效采样间隔：所有需求命名空间取最小值（最高频）。

    Args:
        source_demands: ``hotspot_watch.source_demands`` 解析后的对象。

    Returns:
        int | None: 生效间隔秒；**没有任何需求时返回 None**（语义为「无需求 → 停采」）。
    """
    intervals = namespace_intervals(source_demands)
    if not intervals:
        return None
    return min(intervals.values())


def demand_reason(*, active: bool, stop_reason: object, source_demands: object) -> str:
    """现算某行的准入派生 reason（不落列，照 §6.5 L648 口径）。

    Args:
        active: 该行 ``active`` 列。
        stop_reason: 该行 ``stop_reason`` 列。
        source_demands: 该行 ``source_demands`` 解析后的对象。

    Returns:
        str: :data:`REASON_BLOCKED_BY_USER` / :data:`REASON_RELEASED` /
        :data:`REASON_TRACKING` / :data:`REASON_NO_DEMAND` 之一。
    """
    if not active:
        # manual_stop 是持久阻止重开的标记，优先级高于一切（不得自动重开）。
        if isinstance(stop_reason, str) and stop_reason == "manual_stop":
            return REASON_BLOCKED_BY_USER
        return REASON_RELEASED
    return REASON_TRACKING if has_demands(source_demands) else REASON_NO_DEMAND
