"""02 / 04 共用的窗口网格内核（FishTool 04 · R5 前置 · P1）。

本模块是窗口右边界取整的**唯一实现**，02 与 04 都必须经它，不许各自再抄一份。

口径（照 ``FishTool_04_..._02补充执行案`` §3.4 原文，逐字实现）::

    T = window_end_s(as_of_s, window_s) = (as_of_s // window_s) * window_s

- 网格以 **UTC 零点** 为锚、**固定不滑动**：不按最近快照或客户端本地时间漂边界；
- ``window_s``（W）由**调用方**传：daily 用 ``86400``、early 用 ``7200``，
  本模块**不硬编码任何 W**；
- ``as_of_s`` 必须是合法整数 epoch（显式排除 ``bool``、负数、非 ``int``），否则抛
  ``ValueError``；
- ``window_s`` 必须是正整数，否则抛 ``ValueError``。

``compute_window_delta`` 属 02 后续，本批只留签名、不实现。
"""
from __future__ import annotations

__all__ = ["window_end_s", "compute_window_delta"]


def window_end_s(as_of_s: int, window_s: int) -> int:
    """把 ``as_of_s`` 向下取整到 ``window_s`` 网格的右边界（UTC 零点锚定）。

    Args:
        as_of_s: 本次实际执行的时钟上限，合法整数 sec 级 epoch（``>= 0``，显式排除 bool）。
        window_s: 窗口宽度（秒），必须为正整数。daily 传 86400 / early 传 7200。

    Returns:
        int: 固定网格右边界 ``T = (as_of_s // window_s) * window_s``。

    Raises:
        ValueError: ``as_of_s`` 非 int / 为 bool / 为负，或 ``window_s`` 非正整数。
    """
    # bool 是 int 的子类，必须显式排除，否则 True / False 会被当成 1 / 0 混进来。
    if type(as_of_s) is not int or as_of_s < 0:
        raise ValueError("invalid_as_of_s")
    if type(window_s) is not int or window_s <= 0:
        raise ValueError("invalid_window_s")
    # 唯一实现：向下取整乘法。epoch 0 即 1970-01-01T00:00:00Z，
    # 任何以整秒为宽度的窗口网格都天然锚定 UTC 零点，无需再做时区换算。
    return (as_of_s // window_s) * window_s


def compute_window_delta(
    snapshots,
    *,
    end_s: int,
    window_s: int,
    as_of_s: int,
    max_gap_s: int,
    require_coverage: float = 1.0,
):
    """占位签名：02 后续实现的「窗口内增量测量」，本批**不实现**。

    照 02 原文，其最终返回 ``WindowMeasure``（``delta / coverage / data_status /
    source_ids / counter_reset / time_basis / supporting_max_gap_s``）。本批只把签名
    固定下来，避免 02 / 04 各造一份漂移的副本。

    Args:
        snapshots: 该 bvid 的快照序列。
        end_s: 本窗口右边界（通常即 :func:`window_end_s` 的结果）。
        window_s: 窗口宽度（秒）。
        as_of_s: 本次实际执行的时钟上限。
        max_gap_s: 允许的最大采样间隔。
        require_coverage: 生效所需的最小覆盖比例，默认 1.0。

    Raises:
        NotImplementedError: 本批不实现，恒抛。
    """
    raise NotImplementedError("compute_window_delta 属 02 后续，本批仅留签名")
