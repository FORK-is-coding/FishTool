"""发布时间证据与稿龄（08 案 B4）的纯函数 + 算法接入契约测试。

覆盖点：

- ``resolve_publication_age`` 七个 ``age_status`` 取值全部可达：
  ``ok`` / ``missing`` / ``invalid`` / ``unknown`` /
  ``future_publication`` / ``conflicting_publication_time`` / ``as_of_unavailable``；
- 未来快照（``captured_epoch_s`` 缺失或晚于 ``as_of``）**不**参与发布时间选择；
- 同一 bvid 多个**不同**有效 pubdate -> 保守报冲突，不选最早 / 最新 / 多数票；
- 无有效证据时一律 ``days is None``，绝不返回 0 天、绝不 clamp；
- ``lifecycle_v2.analyze_one`` / ``detect`` 的稿龄字段与 metadata 通道贯通，
  且 metrics / metadata 键集互斥（数值项与非数值项分流）。

全部为纯内存计算，使用固定时钟；不触网、不落库、不读取任何密钥。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modules.hotspot.algorithm.base import Snapshot
from modules.hotspot.algorithm.lifecycle_v2 import DAY_S, LifecycleV2
from modules.hotspot.algorithm.publication_age import (
    AGE_SOURCE_AS_OF_MISSING,
    AGE_SOURCE_CONFLICT,
    AGE_SOURCE_PUBDATE,
    AGE_STATUS_AS_OF_UNAVAILABLE,
    AGE_STATUS_CONFLICTING,
    AGE_STATUS_FUTURE_PUBLICATION,
    AGE_STATUS_INVALID,
    AGE_STATUS_MISSING,
    AGE_STATUS_OK,
    AGE_STATUS_UNKNOWN,
    resolve_publication_age,
)

# 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int）。
E: int = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
DAY: int = DAY_S


# --------------------------------------------------------------------- 构造工具


def _pub(
    capture_epoch_s: int | None,
    *,
    pubdate: int | None = None,
    status: str = "ok",
    view_quality: str = "ok",
) -> Snapshot:
    """构造一条带发布时间证据的快照。``capture_epoch_s=None`` 表示无采集时刻。"""
    return Snapshot(
        bvid="BV1",
        tid=4,
        captured_at=datetime.fromtimestamp(capture_epoch_s or E, tz=timezone.utc),
        view=1000,
        captured_epoch_s=capture_epoch_s,
        view_quality=view_quality,
        pubdate_epoch_s=pubdate,
        pubdate_status=status,
    )


# ----------------------------------------------------------- 1. 七个状态全取值


def test_ok_single_evidence_returns_fractional_days() -> None:
    """唯一有效发布时间 -> 小数天数，不取整、不 clamp。"""
    age = resolve_publication_age([_pub(E, pubdate=E - 5 * DAY + 43200)], as_of_epoch_s=E)
    assert age.status == AGE_STATUS_OK
    assert age.days == pytest.approx(4.5)
    assert age.source == AGE_SOURCE_PUBDATE
    assert age.published_epoch_s == E - 5 * DAY + 43200


def test_missing_status_reports_missing() -> None:
    age = resolve_publication_age(
        [_pub(E, pubdate=None, status="missing")], as_of_epoch_s=E
    )
    assert age.status == AGE_STATUS_MISSING
    assert age.days is None


def test_invalid_status_reports_invalid() -> None:
    age = resolve_publication_age(
        [_pub(E, pubdate=None, status="invalid")], as_of_epoch_s=E
    )
    assert age.status == AGE_STATUS_INVALID
    assert age.days is None


def test_unknown_status_reports_unknown() -> None:
    """旧行 / 无据可查（status='unknown'）如实报 unknown，不猜。"""
    age = resolve_publication_age(
        [_pub(E, pubdate=None, status="unknown")], as_of_epoch_s=E
    )
    assert age.status == AGE_STATUS_UNKNOWN
    assert age.days is None


def test_future_publication_not_clamped_to_zero() -> None:
    """发布时间晚于 as_of -> future_publication，不 clamp 成 0 天。"""
    age = resolve_publication_age([_pub(E, pubdate=E + DAY)], as_of_epoch_s=E)
    assert age.status == AGE_STATUS_FUTURE_PUBLICATION
    assert age.days is None
    assert age.published_epoch_s == E + DAY


def test_conflicting_distinct_pubdates_reports_conflict() -> None:
    """两个不同有效 pubdate -> 冲突；不选最早 / 最新 / 多数票。"""
    rows = [_pub(E, pubdate=E - 2 * DAY), _pub(E - 3600, pubdate=E - 3 * DAY)]
    age = resolve_publication_age(rows, as_of_epoch_s=E)
    assert age.status == AGE_STATUS_CONFLICTING
    assert age.days is None
    assert age.published_epoch_s is None
    assert age.source == AGE_SOURCE_CONFLICT


def test_as_of_none_reports_unavailable() -> None:
    age = resolve_publication_age([_pub(E, pubdate=E - DAY)], as_of_epoch_s=None)
    assert age.status == AGE_STATUS_AS_OF_UNAVAILABLE
    assert age.days is None
    assert age.source == AGE_SOURCE_AS_OF_MISSING


def test_as_of_invalid_raises() -> None:
    """非法 as_of 直接报参数错误，不静默降级成某个状态。"""
    with pytest.raises(ValueError):
        resolve_publication_age([_pub(E, pubdate=E - DAY)], as_of_epoch_s=-1)


# ----------------------------------------------------------------- 2. 未来信息


def test_future_snapshot_excluded_from_evidence() -> None:
    """未来快照的 pubdate 不参与选择；否则会被误判成冲突。"""
    rows = [
        _pub(E, pubdate=E - 2 * DAY),
        _pub(E + DAY, pubdate=E - 3 * DAY),  # 采集在 as_of 之后 -> 未来信息
    ]
    age = resolve_publication_age(rows, as_of_epoch_s=E)
    assert age.status == AGE_STATUS_OK
    assert age.days == pytest.approx(2.0)


def test_only_future_evidence_yields_no_evidence() -> None:
    """唯一证据来自未来快照 -> 无有效证据，不返回天数。"""
    age = resolve_publication_age([_pub(E + DAY, pubdate=E - DAY)], as_of_epoch_s=E)
    assert age.status == AGE_STATUS_UNKNOWN
    assert age.days is None


def test_snapshot_without_capture_excluded() -> None:
    """无明确采集时刻的快照不可比较，不参与发布时间选择。"""
    age = resolve_publication_age(
        [_pub(None, pubdate=E - 3 * DAY)], as_of_epoch_s=E
    )
    assert age.status == AGE_STATUS_UNKNOWN
    assert age.days is None


# ------------------------------------------------------------- 3. 其它边界口径


def test_same_pubdate_repeated_is_not_conflict() -> None:
    """同一发布时间的多条快照只是重复观测，不构成冲突。"""
    rows = [_pub(E, pubdate=E - 2 * DAY), _pub(E - 3600, pubdate=E - 2 * DAY)]
    age = resolve_publication_age(rows, as_of_epoch_s=E)
    assert age.status == AGE_STATUS_OK
    assert age.days == pytest.approx(2.0)


def test_view_quality_does_not_invalidate_pubdate() -> None:
    """播放量缺失不废弃有效发布时间（view_quality 不是发布时间有效性的替代）。"""
    age = resolve_publication_age(
        [_pub(E, pubdate=E - DAY, view_quality="missing")], as_of_epoch_s=E
    )
    assert age.status == AGE_STATUS_OK
    assert age.days == pytest.approx(1.0)


def test_ok_with_non_positive_epoch_is_not_evidence() -> None:
    """status 标 ok 但 epoch 非正整数 -> 不算证据，不产生 0 天。"""
    age = resolve_publication_age([_pub(E, pubdate=0)], as_of_epoch_s=E)
    assert age.status == AGE_STATUS_UNKNOWN
    assert age.days is None


def test_invalid_takes_precedence_over_missing() -> None:
    """无有效证据时按 invalid / missing / unknown 的优先级如实报状态。"""
    rows = [
        _pub(E, pubdate=None, status="missing"),
        _pub(E - 3600, pubdate=None, status="invalid"),
    ]
    age = resolve_publication_age(rows, as_of_epoch_s=E)
    assert age.status == AGE_STATUS_INVALID


# ------------------------------------------------------- 4. lifecycle_v2 接入


def _rows_with_pubdate() -> list[Snapshot]:
    """给出一条够长、带有效发布时间的观测序列。"""
    published = E - 30 * DAY
    return [
        _pub(E - (10 - i) * 3600, pubdate=published) for i in range(11)
    ]


def test_analyze_one_exposes_age_fields() -> None:
    analysis = LifecycleV2(as_of_epoch_s=E).analyze_one("BV1", _rows_with_pubdate())
    assert analysis.age_status == AGE_STATUS_OK
    assert analysis.age_days == pytest.approx(30.0)
    assert analysis.age_source == AGE_SOURCE_PUBDATE
    assert analysis.as_of_epoch_s == E
    assert analysis.as_of_source == "explicit"


def test_analyze_one_default_as_of_marks_fallback_source() -> None:
    """离线未显式传 as_of 时回退最大观测点，来源必须如实标注。"""
    analysis = LifecycleV2().analyze_one("BV1", _rows_with_pubdate())
    assert analysis.as_of_source == "max_observed_fallback"
    assert analysis.as_of_epoch_s == E


def test_detect_metadata_carries_age_channel() -> None:
    rows = _rows_with_pubdate()
    detection = LifecycleV2(as_of_epoch_s=E).detect(rows)[0]
    assert detection.metadata["age_status"] == AGE_STATUS_OK
    assert detection.metadata["age_source"] == AGE_SOURCE_PUBDATE
    assert detection.metadata["as_of_epoch_s"] == E
    assert detection.metadata["as_of_source"] == "explicit"
    assert detection.metadata["age_reference"] == "evaluation_as_of"


def test_metrics_and_metadata_key_sets_are_disjoint() -> None:
    """稿龄数值进 metrics，状态走 metadata；两边键集互斥，不互相污染。"""
    rows = _rows_with_pubdate()
    detection = LifecycleV2(as_of_epoch_s=E).detect(rows)[0]
    assert detection.metrics["age_days"] == pytest.approx(30.0)
    assert "age_days" not in detection.metadata
    assert "age_status" not in detection.metrics
    assert "as_of_epoch_s" not in detection.metrics


def test_unknown_age_keeps_metrics_none() -> None:
    """无有效发布时间时 metrics.age_days 为 None，不写 0 冒充。"""
    rows = [_pub(E - (10 - i) * 3600, pubdate=None, status="missing") for i in range(11)]
    detection = LifecycleV2(as_of_epoch_s=E).detect(rows)[0]
    assert detection.metrics["age_days"] is None
    assert detection.metadata["age_status"] == AGE_STATUS_MISSING
