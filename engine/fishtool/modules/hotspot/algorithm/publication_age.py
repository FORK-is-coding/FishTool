"""发布时间证据解析与小数稿龄计算（08 案 B4 · 纯函数）。

只依赖标准库与 ``core.data_quality``；**不**访问 DB / Web / API，**不**读系统时钟。
调用方必须显式传入 ``as_of_epoch_s``，年龄一律相对该评估截止时刻计算。

口径要点（对齐 08 案 §H1）：

- 只有 ``pubdate_status == 'ok'`` 且 epoch 为正整数的快照才是**有效发布时间证据**；
  ``view_quality`` 不是发布时间有效性的替代 —— 播放量缺失不废弃有效发布时间；
- ``captured_epoch_s`` 缺失或晚于 ``as_of`` 的快照是未来信息，**不**参与发布时间选择；
- 同一 bvid 出现多个**不同**的有效发布时间 -> ``conflicting_publication_time``，
  不选最早 / 最新 / 多数票，也不自动重写历史（需新的证据版本才能纠正）；
- 无有效证据时按 invalid / missing / unknown 的优先级如实报状态，**不**返回 0 天；
- 发布时间晚于 ``as_of`` -> ``future_publication``，**不** clamp 成 0；
- 不使用 ``timedelta.days``，不从首次被发现时间反推作品年龄。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: 一天秒数；年龄 = (as_of - published) / 86400，保留小数，绝不取整丢精度。
SECONDS_PER_DAY = 86400

#: ``PublicationAge.status`` 的全部取值（08 案 §H1 / §B4 验收枚举）。
AGE_STATUS_OK = "ok"
AGE_STATUS_MISSING = "missing"
AGE_STATUS_INVALID = "invalid"
AGE_STATUS_UNKNOWN = "unknown"
AGE_STATUS_FUTURE_PUBLICATION = "future_publication"
AGE_STATUS_CONFLICTING = "conflicting_publication_time"
AGE_STATUS_AS_OF_UNAVAILABLE = "as_of_unavailable"

#: ``age_source`` 取值：证据来自哪一列 / 为何不可用。
AGE_SOURCE_PUBDATE = "pubdate_epoch_s"
AGE_SOURCE_STATUS = "pubdate_status"
AGE_SOURCE_CONFLICT = "conflict"
AGE_SOURCE_NO_EVIDENCE = "no_evidence"
AGE_SOURCE_AS_OF_MISSING = "as_of_missing"


@dataclass(frozen=True)
class PublicationAge:
    """一次稿龄解析结果：``days`` 仅在 ``status == 'ok'`` 时非 None。"""

    days: float | None
    status: str
    source: str
    published_epoch_s: int | None
    as_of_epoch_s: int | None


def _capture_epoch(snapshot: Any) -> int | None:
    """取快照采集时刻（UTC 秒级 int）；缺失或非 int 一律视为不可比较。"""
    epoch = getattr(snapshot, "captured_epoch_s", None)
    return epoch if type(epoch) is int else None


def _pubdate_evidence(snapshot: Any) -> tuple[int | None, str]:
    """取单条快照的 ``(发布时间 epoch, 状态)``。

    只有 ``status == 'ok'`` 且 epoch 为正整数才回传 epoch；其余情况 epoch 恒为
    ``None`` —— 状态与数值必须自洽，读端不允许出现「status 非 ok 却带时间」。
    """
    epoch = getattr(snapshot, "pubdate_epoch_s", None)
    status = getattr(snapshot, "pubdate_status", None)
    if status != "ok" or type(epoch) is not int or epoch <= 0:
        return None, status if isinstance(status, str) else AGE_STATUS_UNKNOWN
    return epoch, AGE_STATUS_OK


def resolve_publication_age(
    snapshots: Iterable[Any], *, as_of_epoch_s: int | None
) -> PublicationAge:
    """解析稿龄：返回 ``(天数, 状态, 来源)`` 三元证据，绝不编造时间。

    Args:
        snapshots: 该 bvid 的快照序列（``Snapshot`` 或同构对象）。
        as_of_epoch_s: 本次评估截止时刻（UTC 秒级 int）。

    Returns:
        PublicationAge: ``days`` 仅在 ``status == 'ok'`` 时非 None。

    Raises:
        ValueError: ``as_of_epoch_s`` 非 None 但不是合法的非负整数。
            （``None`` 不是错误，按 ``as_of_unavailable`` 返回。）

    处理顺序（08 案 §H1）：

    1. as_of 为 None -> ``as_of_unavailable``；非法值直接报参数错误，不静默降级；
    2. 只保留「有明确 capture 且 capture <= as_of」的发布时间证据；
    3. ``status == 'ok'`` 且 epoch 有效才算有效证据；
    4. 多个有效且互不相同的 pubdate -> ``conflicting_publication_time``；
    5. 无有效证据 -> 按 invalid / missing / unknown 如实返回，不返回 0 天；
    6. 唯一有效 pubdate > as_of -> ``future_publication``；否则算小数天数。
    """
    # ---- 1. as_of 校验 ----
    if as_of_epoch_s is None:
        return PublicationAge(None, AGE_STATUS_AS_OF_UNAVAILABLE, AGE_SOURCE_AS_OF_MISSING, None, None)
    if type(as_of_epoch_s) is not int or as_of_epoch_s < 0:
        raise ValueError("invalid_as_of_epoch_s")

    # ---- 2/3. 只采信「可见且明确为 ok」的发布时间 ----
    valid: set[int] = set()
    seen_invalid = False
    seen_missing = False
    seen_unknown = False

    for snapshot in snapshots:
        capture = _capture_epoch(snapshot)
        if capture is None or capture > as_of_epoch_s:
            # 无明确采集时刻 / 未来快照：不参与发布时间选择。
            continue
        epoch, status = _pubdate_evidence(snapshot)
        if status == AGE_STATUS_OK and epoch is not None:
            valid.add(epoch)
        elif status == AGE_STATUS_INVALID:
            seen_invalid = True
        elif status == AGE_STATUS_MISSING:
            seen_missing = True
        else:
            seen_unknown = True

    # ---- 4. 冲突优先暴露，不挑对当前叙事有利的值 ----
    if len(valid) > 1:
        return PublicationAge(
            None, AGE_STATUS_CONFLICTING, AGE_SOURCE_CONFLICT, None, as_of_epoch_s
        )

    # ---- 6. 唯一有效证据 ----
    if len(valid) == 1:
        published = next(iter(valid))
        if published > as_of_epoch_s:
            return PublicationAge(
                None,
                AGE_STATUS_FUTURE_PUBLICATION,
                AGE_SOURCE_PUBDATE,
                published,
                as_of_epoch_s,
            )
        days = (as_of_epoch_s - published) / SECONDS_PER_DAY
        return PublicationAge(
            days, AGE_STATUS_OK, AGE_SOURCE_PUBDATE, published, as_of_epoch_s
        )

    # ---- 5. 无有效证据：如实报状态，绝不返回 0 天 ----
    if seen_invalid:
        return PublicationAge(None, AGE_STATUS_INVALID, AGE_SOURCE_STATUS, None, as_of_epoch_s)
    if seen_missing:
        return PublicationAge(None, AGE_STATUS_MISSING, AGE_SOURCE_STATUS, None, as_of_epoch_s)
    if seen_unknown:
        return PublicationAge(None, AGE_STATUS_UNKNOWN, AGE_SOURCE_STATUS, None, as_of_epoch_s)
    return PublicationAge(None, AGE_STATUS_UNKNOWN, AGE_SOURCE_NO_EVIDENCE, None, as_of_epoch_s)


__all__ = [
    "PublicationAge",
    "resolve_publication_age",
    "SECONDS_PER_DAY",
    "AGE_STATUS_OK",
    "AGE_STATUS_MISSING",
    "AGE_STATUS_INVALID",
    "AGE_STATUS_UNKNOWN",
    "AGE_STATUS_FUTURE_PUBLICATION",
    "AGE_STATUS_CONFLICTING",
    "AGE_STATUS_AS_OF_UNAVAILABLE",
    "AGE_SOURCE_PUBDATE",
    "AGE_SOURCE_STATUS",
    "AGE_SOURCE_CONFLICT",
    "AGE_SOURCE_NO_EVIDENCE",
    "AGE_SOURCE_AS_OF_MISSING",
]
