"""热点算法边界适配层的契约级测试。

覆盖 modules/hotspot/algorithm/adapter.py 的两个公开函数：
- snapshot_from_mapping：采集字典 -> Snapshot 的字段兜底与时间解析
- detection_to_dto：Detection -> 稳定 JSON DTO

重点验证：
- captured_at / snapshot_time 两种字段名与字符串、datetime、非法值的降级
- owner_mid / owner_name 的别名兼容（mid / author）
- 缺失或非法数值的兜底（None -> 0）
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from modules.hotspot.algorithm.adapter import detection_to_dto, snapshot_from_mapping
from modules.hotspot.algorithm.base import Detection


def test_snapshot_from_mapping_maps_all_fields() -> None:
    """完整字典应逐字段映射到 Snapshot。"""
    captured = datetime(2026, 5, 1, 8, 30)

    snapshot = snapshot_from_mapping(
        {
            "bvid": "BV1",
            "tid": 4,
            "captured_at": captured,
            "view": 1234,
            "title": "标题",
            "owner_mid": 777,
            "owner_name": "UP主",
            "source": "ranking",
        }
    )

    assert snapshot.bvid == "BV1"
    assert snapshot.tid == 4
    assert snapshot.captured_at == captured
    assert snapshot.view == 1234
    assert snapshot.title == "标题"
    assert snapshot.owner_mid == 777
    assert snapshot.owner_name == "UP主"
    assert snapshot.source == "ranking"


def test_snapshot_from_mapping_parses_iso_string_time() -> None:
    """captured_at 为 ISO 字符串时应解析为 datetime。"""
    snapshot = snapshot_from_mapping({"bvid": "BV2", "captured_at": "2026-01-02T03:04:05"})

    assert snapshot.captured_at == datetime(2026, 1, 2, 3, 4, 5)


def test_snapshot_from_mapping_falls_back_to_snapshot_time() -> None:
    """captured_at 缺失时应回退读取 snapshot_time。"""
    snapshot = snapshot_from_mapping({"bvid": "BV3", "snapshot_time": "2026-02-03T04:05:06"})

    assert snapshot.captured_at == datetime(2026, 2, 3, 4, 5, 6)


def test_snapshot_from_mapping_uses_now_when_time_missing() -> None:
    """两种时间字段都缺失时应兜底为当前时间。"""
    before = datetime.now()

    snapshot = snapshot_from_mapping({"bvid": "BV4"})

    assert isinstance(snapshot.captured_at, datetime)
    assert snapshot.captured_at >= before


def test_snapshot_from_mapping_uses_now_for_non_datetime_time() -> None:
    """时间字段为不可解析的数值时应兜底为当前时间。"""
    snapshot = snapshot_from_mapping({"bvid": "BV5", "captured_at": 1234567890})

    assert isinstance(snapshot.captured_at, datetime)


def test_snapshot_from_mapping_applies_defaults_for_missing_fields() -> None:
    """缺失字段应给出契约默认值：空串 / 0 / unknown。"""
    snapshot = snapshot_from_mapping({})

    assert snapshot.bvid == ""
    assert snapshot.tid == 0
    assert snapshot.view == 0
    assert snapshot.title == ""
    assert snapshot.owner_mid == 0
    assert snapshot.owner_name == ""
    assert snapshot.source == "unknown"


def test_snapshot_from_mapping_accepts_mid_and_author_aliases() -> None:
    """owner_mid / owner_name 应兼容采集层的 mid / author 别名。"""
    snapshot = snapshot_from_mapping({"bvid": "BV6", "mid": 42, "author": "别名UP"})

    assert snapshot.owner_mid == 42
    assert snapshot.owner_name == "别名UP"


def test_snapshot_from_mapping_coerces_bvid_to_str() -> None:
    """bvid 传入非字符串时应强制转成字符串。"""
    snapshot = snapshot_from_mapping({"bvid": 12345})

    assert snapshot.bvid == "12345"


def test_snapshot_from_mapping_raises_on_non_numeric_tid() -> None:
    """tid 为非数字字符串时当前实现直接抛 ValueError（固化现状）。"""
    with pytest.raises(ValueError):
        snapshot_from_mapping({"bvid": "BV7", "tid": "not-a-number"})


def test_snapshot_from_mapping_passes_explicit_first_seen() -> None:
    """B6b：显式 first_seen_epoch_s 原样透传为工具首次发现时刻。"""
    snapshot = snapshot_from_mapping({"bvid": "BV8", "first_seen_epoch_s": 1750000000})

    assert snapshot.first_seen_epoch_s == 1750000000


def test_snapshot_from_mapping_never_guesses_first_seen() -> None:
    """B6b：无 first_seen 时不拿 pubdate / captured_at / now 顶替，保持 None。"""
    snapshot = snapshot_from_mapping(
        {"bvid": "BV9", "pubdate_epoch_s": 1700000000, "captured_at": datetime(2026, 1, 1)}
    )

    assert snapshot.first_seen_epoch_s is None


@pytest.mark.parametrize("bad", [None, "1700000000", 1.5, True, -5])
def test_snapshot_from_mapping_rejects_non_int_first_seen(bad) -> None:
    """B6b：非「非负 int」的 first_seen 一律降为 None（不收字符串/浮点/bool/负数）。"""
    snapshot = snapshot_from_mapping({"bvid": "BV10", "first_seen_epoch_s": bad})

    assert snapshot.first_seen_epoch_s is None


def test_detection_to_dto_exports_all_keys() -> None:
    """DTO 应包含展示层依赖的全部字段。"""
    detection = Detection(
        bvid="BV1",
        stage="上升期",
        confidence=0.42,
        metrics={"percentile": 88.0},
        explain="解释文本",
        algorithm_version="heuristic_v1",
        title="标题",
        tid=4,
        owner_mid=100,
        owner_name="UP",
    )

    dto = detection_to_dto(detection)

    assert dto == {
        "bvid": "BV1",
        "title": "标题",
        "tid": 4,
        "owner_mid": 100,
        "owner_name": "UP",
        "stage": "上升期",
        "confidence": 0.42,
        "metrics": {"percentile": 88.0},
        "metadata": {},
        "explain": "解释文本",
        "algorithm_version": "heuristic_v1",
    }


def test_detection_to_dto_keeps_defaults() -> None:
    """最小 Detection 应导出默认值而非丢失字段。"""
    dto = detection_to_dto(Detection(bvid="BV2", stage="观察期", confidence=0.0))

    assert dto["title"] == ""
    assert dto["tid"] == 0
    assert dto["metrics"] == {}
    assert dto["metadata"] == {}
    assert dto["explain"] == ""
    assert dto["algorithm_version"] == ""


def test_detection_to_dto_passes_through_metadata() -> None:
    """metadata 通道应原样透传（confidence_kind / coverage_state），且与 metrics 键集互斥。"""
    detection = Detection(
        bvid="BV3",
        stage="上升期",
        confidence=0.0,
        metrics={"coverage_ratio": 0.92},
        metadata={"confidence_kind": "not_estimated", "coverage_state": "provisional"},
        algorithm_version="lifecycle_v2",
    )

    dto = detection_to_dto(detection)

    assert dto["metadata"] == {"confidence_kind": "not_estimated", "coverage_state": "provisional"}
    # 键集互斥：非数值项不得混入 metrics，数值项不得混入 metadata。
    assert "coverage_state" not in dto["metrics"]
    assert "coverage_ratio" not in dto["metadata"]
    # DTO 需经 HTTP 下发给展示层，必须可 JSON 序列化。
    json.dumps(dto)
