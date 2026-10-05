"""08 案 §J（B6a + B6b）：作品年龄 / 新发现进入「出现期」门槛 —— E 系列专项用例。

口径（08 案 R2 · J2-a / J2-b / J4 / J5）：

- 首窗出现期 = 首次形成有效强度 **且** 强度 >= ``emerge_rate`` **且** 满足下列任一依据：
  - publication：作品在该窗口端点的年龄 <= ``emerge_age_days``（默认 7 日，B6a）；
  - discovery：工具首次发现（``Snapshot.first_seen_epoch_s``）距该端点
    <= ``emerge_discovery_days``（默认 2 日，B6b「新发现老视频」通道）；
- 窗口级发布时间证据只采信「该端点当时可见（``captured_epoch_s <= end_s``）且无冲突」的
  事实，不用今天才采到 / 后来纠正的元信息改写历史窗口的新旧判断；
- 证据缺失 / 冲突 / 未来 / 晚于端点的快照一律 **不** 放行，未知不开口子；
- 命中 discovery 依据时必须显式标 ``discovery``，**不得**表述成「视频刚发布 / 事件刚发生」
  （由断言 ``emergence_basis == EMERGENCE_BASIS_DISCOVERY`` 固定）；
- 门槛未知只约束 emerging，不否定后续窗口的上升 / 成熟 / 衰退；
- threshold_version = ``lifecycle_v2_age_gate_2``（B6b 独立版本，不与 B6a 的 age_gate_1 合并）。

纯内存计算，固定时钟，不触网、不落库、不读密钥。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modules.hotspot.algorithm.base import Snapshot
from modules.hotspot.algorithm.lifecycle_v2 import (
    DAY_S,
    EMERGENCE_BASIS_DISCOVERY,
    EMERGENCE_BASIS_NONE,
    EMERGENCE_BASIS_PUBLICATION,
    LifecycleV2,
    LifecycleV2Config,
    Stage,
    TrendState,
    within_age_limit,
)
from modules.hotspot.watch_service import state_from_json, state_to_json

# 统一测试时钟：2026-09-01T00:00:00Z（UTC 午夜，秒级 int）。
E: int = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
DAY: int = DAY_S


def _snap(
    epoch_s: int,
    view,
    *,
    pubdate_epoch_s: int | None = None,
    pubdate_status: str = "ok",
    first_seen_epoch_s: int | None = None,
    quality: str = "ok",
    bvid: str = "BV1",
    tid: int = 4,
    mid: int = 1001,
) -> Snapshot:
    """构造带采集时刻 / 发布时间 / 首次发现证据的快照（默认都无，逼用例显式声明证据）。"""
    return Snapshot(
        bvid=bvid,
        tid=tid,
        captured_at=datetime.fromtimestamp(epoch_s, tz=timezone.utc),
        view=view,
        captured_epoch_s=epoch_s,
        view_quality=quality,
        title=f"标题-{bvid}",
        owner_mid=mid,
        owner_name=f"UP-{mid}",
        pubdate_epoch_s=pubdate_epoch_s,
        pubdate_status=pubdate_status,
        first_seen_epoch_s=first_seen_epoch_s,
    )


def _analysis(rows, *, as_of: int, config: LifecycleV2Config | None = None):
    """单目标分析入口（固定时钟，不触网）。"""
    return LifecycleV2(config=config, as_of_epoch_s=as_of).analyze_one("BV1", rows)


def _fresh_pair(
    pubdate_epoch_s: int | None,
    *,
    status: str = "ok",
    first_seen_epoch_s: int | None = None,
) -> list[Snapshot]:
    """两日观测、rate=50 的首窗样本；发布时间 / 首次发现证据由调用方给。"""
    return [
        _snap(E, 1000, pubdate_epoch_s=pubdate_epoch_s, pubdate_status=status,
              first_seen_epoch_s=first_seen_epoch_s),
        _snap(E + DAY, 1050, pubdate_epoch_s=pubdate_epoch_s, pubdate_status=status,
              first_seen_epoch_s=first_seen_epoch_s),
    ]


# --------------------------------------------------------------------- E01—E07


def test_e01_fresh_publication_enters_emerging() -> None:
    """E01：一个有效窗、年龄 1 日、rate 50 -> 出现期，依据 publication。"""
    analysis = _analysis(_fresh_pair(E), as_of=E + DAY)

    assert analysis.stage == Stage.EMERGING
    assert analysis.emergence_basis == EMERGENCE_BASIS_PUBLICATION
    assert analysis.rate_current == pytest.approx(50.0)


def test_e02_old_publication_stays_observing() -> None:
    """E02：年龄 30 日、first_seen 也 30 日前、首窗 rate 50 -> 只观察。

    两个通道都不成立：publication 超龄（30 > 7），discovery 的发现时间也在 30 天前
    （30 > 2）；有流量不等于新出现。
    """
    origin = E + DAY - 30 * DAY
    analysis = _analysis(_fresh_pair(origin, first_seen_epoch_s=origin), as_of=E + DAY)

    assert analysis.stage == Stage.OBSERVING
    assert analysis.emergence_basis == EMERGENCE_BASIS_NONE
    # 门槛只约束出现期，不吞掉强度证据（后续窗口仍能按方向推进）。
    assert analysis.rate_current == pytest.approx(50.0)


def test_e03_recently_discovered_old_video_emerges_by_discovery() -> None:
    """E03（B6b 口径）：年龄 30 日、first_seen 1 日前 -> 允许 discovery 依据。

    发布 30 天前（publication 超龄），但工具首次发现距窗口端点仅 1 天，落在
    ``emerge_discovery_days``（默认 2 日）内 -> 出现期，依据必须显式标 ``discovery``
    （「新发现老视频」），**不得**表述成视频刚发布；age_days 仍如实为 30 天。
    """
    pub = E + DAY - 30 * DAY
    first_seen = E  # 距 end_s = E + DAY 恰好 1 日
    analysis = _analysis(_fresh_pair(pub, first_seen_epoch_s=first_seen), as_of=E + DAY)

    assert analysis.stage == Stage.EMERGING
    assert analysis.emergence_basis == EMERGENCE_BASIS_DISCOVERY
    # 新发现的老视频：稿龄如实 30 天，不因过门而改写成「刚发布」。
    assert analysis.age_status == "ok"
    assert analysis.age_days == pytest.approx(30.0)


def test_e03_old_video_without_watch_stays_observing() -> None:
    """E03 反向：同一 30 日老视频，无 first_seen（无 watch）时 discovery 不可用，仍只观察。"""
    pub = E + DAY - 30 * DAY
    analysis = _analysis(_fresh_pair(pub), as_of=E + DAY)

    assert analysis.stage != Stage.EMERGING
    assert analysis.emergence_basis == EMERGENCE_BASIS_NONE


def test_e04_unknown_publication_blocks_emerging_but_not_trend() -> None:
    """E04：发布时间全未知 -> 不进出现期，但足够窗口后仍可上升 / 成熟。"""
    rows = [
        _snap(E + index * DAY, view, pubdate_epoch_s=None, pubdate_status="missing")
        for index, view in enumerate([1000, 1100, 1250, 1400, 1550, 1700])
    ]
    analysis = _analysis(rows, as_of=E + 5 * DAY)

    assert analysis.stage != Stage.EMERGING
    assert analysis.stage in {Stage.RISING, Stage.MATURE}
    assert analysis.emergence_basis == EMERGENCE_BASIS_NONE


@pytest.mark.parametrize(
    "age_s,expected",
    [
        (7 * DAY, True),  # 7 日整：通过
        (7 * DAY + 1, False),  # 7 日 + 1 秒：不通过（精确秒，不截整天）
    ],
)
def test_e05_boundary_uses_exact_seconds(age_s: int, expected: bool) -> None:
    """E05：publication 门在「7 日整 / 7 日 + 1 秒」两侧分别通过 / 不通过。"""
    end_s = E + DAY
    pub = end_s - age_s
    analysis = _analysis(_fresh_pair(pub), as_of=end_s)

    assert within_age_limit(end_s, pub, 7.0) is expected
    assert (analysis.stage == Stage.EMERGING) is expected


@pytest.mark.parametrize(
    "age_s,expected",
    [
        (2 * DAY, True),       # discovery 2 日整：通过
        (2 * DAY + 1, False),  # 2 日 + 1 秒：不通过（精确秒，不截整天）
    ],
)
def test_e06_discovery_boundary_uses_exact_seconds(age_s: int, expected: bool) -> None:
    """E06：discovery 门在「2 日整 / 2 日 + 1 秒」两侧分别通过 / 不通过。

    发布 30 天前（publication 超龄），是否出现期只由 first_seen 决定；命中时依据为 discovery。
    """
    end_s = E + DAY
    pub = end_s - 30 * DAY
    first_seen = end_s - age_s

    assert within_age_limit(end_s, first_seen, 2.0) is expected
    analysis = _analysis(_fresh_pair(pub, first_seen_epoch_s=first_seen), as_of=end_s)
    assert (analysis.stage == Stage.EMERGING) is expected
    if expected:
        assert analysis.emergence_basis == EMERGENCE_BASIS_DISCOVERY
    else:
        assert analysis.emergence_basis == EMERGENCE_BASIS_NONE


def test_e07_threshold_change_switches_classification() -> None:
    """E07：emerge_age_days 7 -> 3，5 日作品的首窗分类随配置改变（配置真的生效）。"""
    pub = E + DAY - 5 * DAY
    rows = _fresh_pair(pub)

    loose = _analysis(rows, as_of=E + DAY, config=LifecycleV2Config(emerge_age_days=7.0))
    tight = _analysis(rows, as_of=E + DAY, config=LifecycleV2Config(emerge_age_days=3.0))

    assert loose.stage == Stage.EMERGING
    assert tight.stage == Stage.OBSERVING


def test_e08_legacy_state_without_gate_version_is_not_reused() -> None:
    """E08：新门版本读旧 ``state_json`` -> 不沿用旧候选计数，也不伪称新版本已确认。"""
    legacy = {
        "last_evaluation_epoch_s": E,
        "prev_rate": 100.0,
        "candidate": "up",
        "baseline": 100.0,
        "count": 3,
        "stable_count": 2,
        "stage": Stage.EMERGING,
    }

    restored = state_from_json(legacy)

    assert restored.count == 0
    assert restored.stable_count == 0
    assert restored.candidate is None
    assert restored.prev_rate is None
    assert restored.stage == Stage.OBSERVING


def test_e08_same_version_state_still_resumes() -> None:
    """E08 对照：带当前门版本的状态照常续算（版本栅栏不能把正常续算也一起清掉）。"""
    state = TrendState()
    state.prev_rate = 100.0
    state.candidate = "up"
    state.count = 3

    round_trip = state_from_json(state_to_json(state))

    assert round_trip.prev_rate == pytest.approx(100.0)
    assert round_trip.candidate == "up"
    assert round_trip.count == 3


def test_e08_gate_1_state_not_reused_under_gate_2() -> None:
    """E08：新门版本 age_gate_2 读 age_gate_1 旧 state_json -> 不沿用旧候选计数。

    旧版本计数是按旧门槛口径攒的；跨版本续算等于伪称新版本已确认，故一律清空重算；
    同版本（当前 age_gate_2）的状态照常续算，版本栅栏只清跨版本。
    """
    gate_1_state = {
        "last_evaluation_epoch_s": E + DAY,
        "prev_rate": 100.0,
        "candidate": "up",
        "baseline": 100.0,
        "count": 3,
        "stable_count": 2,
        "stage": Stage.RISING,
        "threshold_version": "lifecycle_v2_age_gate_1",
    }

    restored = state_from_json(gate_1_state)

    assert restored.count == 0
    assert restored.stable_count == 0
    assert restored.candidate is None
    assert restored.prev_rate is None
    assert restored.baseline is None
    assert restored.stage == Stage.OBSERVING

    same_version = dict(gate_1_state, threshold_version="lifecycle_v2_age_gate_2")
    kept = state_from_json(same_version)
    assert kept.prev_rate == pytest.approx(100.0)
    assert kept.count == 3
    assert kept.stage == Stage.RISING


def test_publication_takes_precedence_over_discovery() -> None:
    """作品既新又刚被发现时依据为 publication（更具体），不误标 discovery。"""
    analysis = _analysis(_fresh_pair(E, first_seen_epoch_s=E), as_of=E + DAY)
    assert analysis.stage == Stage.EMERGING
    assert analysis.emergence_basis == EMERGENCE_BASIS_PUBLICATION


def test_metadata_carries_discovery_basis() -> None:
    """命中 discovery 依据时，metadata.emergence_basis 显式落 wire 值 discovery。"""
    pub = E + DAY - 30 * DAY
    detection = LifecycleV2(as_of_epoch_s=E + DAY).detect(
        _fresh_pair(pub, first_seen_epoch_s=E)
    )[0]
    assert detection.metadata["emergence_basis"] == EMERGENCE_BASIS_DISCOVERY
    assert all(isinstance(value, (int, float)) or value is None for value in detection.metrics.values())


# --------------------------------------------------------------------- 反向钉死


def test_conflicting_publication_blocks_emerging() -> None:
    """同一 bvid 两个不同有效发布时间 -> 不选有利值，也不放行出现期。"""
    rows = [
        _snap(E, 1000, pubdate_epoch_s=E),
        _snap(E + DAY, 1050, pubdate_epoch_s=E - 3600),
    ]
    analysis = _analysis(rows, as_of=E + DAY)

    assert analysis.stage == Stage.OBSERVING
    assert analysis.age_status == "conflicting_publication_time"
    assert analysis.emergence_basis == EMERGENCE_BASIS_NONE


def test_future_publication_blocks_emerging() -> None:
    """发布时间晚于窗口端点 -> 不放行，也不 clamp 成 0 天。"""
    future_pub = E + DAY + 10 * DAY
    analysis = _analysis(_fresh_pair(future_pub), as_of=E + DAY)

    assert analysis.stage == Stage.OBSERVING
    assert analysis.age_status == "future_publication"
    assert analysis.age_days is None


def test_future_snapshot_pubdate_does_not_rewrite_past_window() -> None:
    """晚于窗口端点的快照即使带「新鲜」发布时间，也不能让该窗口过门（knowledge cutoff）。"""
    rows = [
        _snap(E, 1000, pubdate_epoch_s=None, pubdate_status="missing"),
        _snap(E + DAY, 1050, pubdate_epoch_s=None, pubdate_status="missing"),
        _snap(E + 5 * DAY, 1600, pubdate_epoch_s=E + 5 * DAY),
    ]
    analysis = _analysis(rows, as_of=E + 5 * DAY)

    assert analysis.stage != Stage.EMERGING
    assert analysis.emergence_basis == EMERGENCE_BASIS_NONE


def test_invalid_publication_status_blocks_emerging() -> None:
    """status 非 ok（invalid）不能当作有效证据，哪怕 epoch 看着像正数。"""
    rows = [
        _snap(E, 1000, pubdate_epoch_s=E, pubdate_status="invalid"),
        _snap(E + DAY, 1050, pubdate_epoch_s=E, pubdate_status="invalid"),
    ]
    analysis = _analysis(rows, as_of=E + DAY)

    assert analysis.stage == Stage.OBSERVING
    assert analysis.age_status == "invalid"


# --------------------------------------------------------------------- 配置与通道


@pytest.mark.parametrize(
    "bad",
    [-1.0, -0.5, float("nan"), float("inf"), True, "7", None],
)
def test_config_rejects_invalid_emerge_age_days(bad) -> None:
    """emerge_age_days 必须是有限非负数值，bool / 字符串 / None 一律拒绝。"""
    with pytest.raises(ValueError, match="invalid_emerge_age_days"):
        LifecycleV2(config=LifecycleV2Config(emerge_age_days=bad))


def test_config_accepts_zero_and_integer_days() -> None:
    """0 天（只认同一秒发布）与整数天都合法。"""
    for value in (0, 0.0, 7, 7.5):
        assert LifecycleV2(config=LifecycleV2Config(emerge_age_days=value)).config.emerge_age_days == value


def test_default_threshold_version_is_age_gate_2() -> None:
    """B6b 在 B6a 之上再升一级；独立版本，不与 age_gate_1 合并。"""
    assert LifecycleV2Config().threshold_version == "lifecycle_v2_age_gate_2"


@pytest.mark.parametrize(
    "bad",
    [-1.0, -0.5, float("nan"), float("inf"), True, "2", None],
)
def test_config_rejects_invalid_emerge_discovery_days(bad) -> None:
    """emerge_discovery_days 必须是有限非负数值，bool / 字符串 / None 一律拒绝。"""
    with pytest.raises(ValueError, match="invalid_emerge_discovery_days"):
        LifecycleV2(config=LifecycleV2Config(emerge_discovery_days=bad))


def test_config_exposes_discovery_days() -> None:
    """emerge_discovery_days 必须随 as_dict / config_schema 暴露（默认 2 日）。"""
    dumped = LifecycleV2Config().as_dict()
    assert dumped["emerge_discovery_days"] == pytest.approx(2.0)
    assert LifecycleV2().config_schema["emerge_discovery_days"] == pytest.approx(2.0)


def test_snapshot_first_seen_defaults_to_none() -> None:
    """Snapshot 追加 first_seen_epoch_s 字段，默认 None（旧构造点不受影响）。"""
    snap = Snapshot(bvid="BV1", tid=4, captured_at=datetime(2026, 9, 1, tzinfo=timezone.utc), view=1)
    assert snap.first_seen_epoch_s is None


@pytest.mark.parametrize(
    "reference_s,origin_s,days,expected",
    [
        (1000, 1000, 7.0, True),
        (1000 + 7 * DAY, 1000, 7.0, True),
        (1000 + 7 * DAY + 1, 1000, 7.0, False),
        (999, 1000, 7.0, False),  # 负年龄不放行
        (1000, None, 7.0, False),  # 证据缺失不放行
        (1000, 1000.0, 7.0, False),  # 浮点 epoch 不是合法证据
        (True, 1000, 7.0, False),  # bool 不是 int
    ],
)
def test_within_age_limit_pure_boundaries(reference_s, origin_s, days, expected) -> None:
    """门是纯函数：边界精确到秒，非 int / 未知一律 False。"""
    assert within_age_limit(reference_s, origin_s, days) is expected


def test_metadata_carries_basis_and_metrics_stay_numeric() -> None:
    """emergence_basis 走 metadata 通道；metrics 仍是纯数值。"""
    detection = LifecycleV2(as_of_epoch_s=E + DAY).detect(_fresh_pair(E))[0]

    assert detection.metadata["emergence_basis"] == EMERGENCE_BASIS_PUBLICATION
    assert all(
        isinstance(value, (int, float)) or value is None for value in detection.metrics.values()
    )
