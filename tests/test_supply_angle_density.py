"""FishTool 04 · 第四批 c 定向测试：供给角度密度与缺口候选（06 §7.1-§7.5）。

覆盖执行规格 §7 测试表：

- 分母：``angle_share`` 分母是 ``|C|`` 不是 ``|E|``；
- 覆盖率：``angle_coverage = |C|/|E|``、``unknown_angle_count = |E|-|C|``；
- 空集：``C`` 空 → ``angle_share`` 全 ``NULL``，不填 0；
- 低覆盖：coverage 低 → 不给拥挤 / 稀缺结论，返回原因码；
- 主副角度：副角度只展示，不进主分母；
- 包装 vs 正文：标题声称教程、无正文 → 不判 ``not_addressed``，为 ``unknown``；
- 反例四组：§5 四条逐条有专属用例；
- 无网络：断言本批不触发任何下载 / ASR / 网络路径；
- 措辞：输出标签只含「已见样本角度拥挤线索」，禁止市场级夸大字样；
- 门槛（默认阈值 / 最少作者 / 版本号）：``SupplyPolicy`` 默认值与版本输出。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from modules.hotspot.events.channel_b import (
    ANGLE_DENSITY_LABEL as CHANNEL_B_LABEL,
    DiscoveryRunView,
    discovery_signals,
)
from modules.hotspot.events.config import SupplyPolicy
from modules.hotspot.events.supply import (
    ANGLE_DENSITY_LABEL,
    GAP_CANDIDATE,
    GAP_COVERAGE_PROBE,
    GAP_DIFFERENTIATION,
    GAP_NOT_ENOUGH_EVIDENCE,
    GAP_PACKAGING,
    GAP_SUPPLY_PRESENT,
    SUPPLY_OUTPUT_LABELS,
    SupplyMember,
    assess_gap_candidate,
    build_need_supply_matrix,
    classify_member_need_state,
    compute_angle_density,
    content_evaluable_coverage,
    distinct_participant_count,
    interpret_supply_change,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPLY_SRC = PROJECT_ROOT / "modules" / "hotspot" / "events" / "supply.py"
CHANNEL_B_SRC = PROJECT_ROOT / "modules" / "hotspot" / "events" / "channel_b.py"

#: 被禁的市场级夸大措辞（只许出现在本测试的断言里，不许进生产代码 / 对外标签）。
FORBIDDEN_WORDING = ("饱和率", "蓝海", "空白", "市场未满足率")


def _member(bvid: str, **kwargs) -> SupplyMember:
    """构造测试用供给成员（默认仅包装层、未分类角度）。"""
    return SupplyMember(bvid=bvid, **kwargs)


# =========================================================================== §7.2 三件套


def test_angle_share_denominator_is_classified_not_members() -> None:
    """分母：``angle_share`` 分母是 |C| 不是 |E|；构造 |E| != |C| 样本分辨。"""
    members = [
        _member("BV1", angle="tutorial"),
        _member("BV2", angle="tutorial"),
        _member("BV3", angle="review"),
        _member("BV4", angle="unclassified"),  # 在 E 不在 C
    ]
    result = compute_angle_density(members)

    assert result.member_count == 4  # |E|
    assert result.classified_count == 3  # |C|
    # 2/|C|=2/3（不是 2/|E|=2/4=0.5）。
    assert result.angle_share["tutorial"] == pytest.approx(2 / 3)
    assert result.angle_share["tutorial"] != pytest.approx(0.5)
    assert result.angle_share["review"] == pytest.approx(1 / 3)
    total = sum(v for v in result.angle_share.values() if v is not None)
    assert total == pytest.approx(1.0)


def test_angle_coverage_and_unknown_angle_count() -> None:
    """覆盖率：``angle_coverage = |C|/|E|``、``unknown_angle_count = |E|-|C|``。"""
    members = [_member(f"BV{i}", angle="news" if i < 7 else "unclassified") for i in range(10)]
    result = compute_angle_density(members)

    assert result.member_count == 10
    assert result.classified_count == 7
    assert result.angle_coverage == pytest.approx(0.7)
    assert result.unknown_angle_count == 3
    assert result.unknown_angle_count == result.member_count - result.classified_count


def test_empty_classified_set_gives_null_shares_not_zero() -> None:
    """空集：C 空 → ``angle_share`` 全 NULL，不填 0。"""
    members = [_member("BV1"), _member("BV2"), _member("BV3")]
    result = compute_angle_density(members)

    assert result.classified_count == 0
    assert result.angle_share
    assert all(value is None for value in result.angle_share.values())
    assert all(value != 0 for value in result.angle_share.values())
    assert result.angle_coverage is None
    assert result.evaluable is False
    assert "no_classifiable_angle" in result.reason_codes


def test_empty_member_set_is_all_null() -> None:
    """|E| = 0：不编造任何比例。"""
    result = compute_angle_density([])

    assert result.member_count == 0
    assert result.angle_coverage is None
    assert result.unknown_angle_count == 0
    assert all(value is None for value in result.angle_share.values())


def test_low_coverage_withholds_crowding_conclusion() -> None:
    """低覆盖：coverage 低 → 不给拥挤 / 稀缺结论，返回原因码；只列已见内容。"""
    members = [_member(f"BV{i}", angle="tutorial" if i == 0 else "unclassified") for i in range(10)]
    result = compute_angle_density(members)

    assert result.angle_coverage == pytest.approx(0.1)
    assert result.evaluable is False
    assert "angle_low_coverage" in result.reason_codes
    assert "angle_small_sample" in result.reason_codes
    assert "crowding_not_evaluated" in result.reason_codes
    # 仍列出已见内容，但不作拥挤 / 稀缺判断。
    assert result.angle_share["tutorial"] == pytest.approx(1.0)


def test_secondary_angle_not_counted_in_main_denominator() -> None:
    """主副角度：副角度只展示，不进主分母。"""
    members = [
        _member("BV1", angle="tutorial", secondary_angles=("review",)),
        _member("BV2", angle="review"),
    ]
    result = compute_angle_density(members)

    # 主角度只有 tutorial / review 各 1，分母 |C|=2。
    assert result.angle_share["tutorial"] == pytest.approx(0.5)
    assert result.angle_share["review"] == pytest.approx(0.5)
    # 副角度单独展示，未把 review 加成 2。
    assert result.secondary_angle_index["review"] == ("BV1",)


def test_duplicate_bvid_counted_once() -> None:
    """唯一 BVID：同一 BVID 重复只算一次。"""
    members = [_member("BV1", angle="tutorial"), _member("BV1", angle="tutorial")]
    result = compute_angle_density(members)

    assert result.member_count == 1
    assert result.classified_count == 1
    assert "duplicate_bvid_deduplicated" in result.reason_codes


# =========================================================================== §7.1 包装 vs 正文


def test_title_claim_tutorial_without_body_is_unknown_not_not_addressed() -> None:
    """包装 vs 正文：标题声称教程、无正文 → 不判 not_addressed，为 unknown。"""
    # 标题写「新手教程」只证明包装声称教程，content_depth 仍是 title_only。
    claim = _member("BV1", content_depth="title_only", need_keys=("tutorial_steps",))
    assert claim.has_content_evidence is False
    # 提到 → mentions；没说「步骤完整」。
    assert classify_member_need_state(claim, "tutorial_steps") == "mentions"

    # 未提某需求 + 无正文 → unknown（不是 not_addressed、不是 0 供给）。
    silent = _member("BV2", content_depth="title_only", need_keys=())
    state = classify_member_need_state(silent, "tutorial_steps")
    assert state == "unknown"
    assert state != "not_addressed"


def test_body_evidence_can_address_and_can_negate_with_scope() -> None:
    """正文证据：有正文才可 addresses / 可有范围否定。"""
    addressing = _member(
        "BV1",
        content_depth="provided_transcript",
        need_keys=("steps",),
        positive_evidence_refs=("transcript#12",),
    )
    assert classify_member_need_state(addressing, "steps") == "addresses"

    negating = _member("BV2", content_depth="reviewed_content", need_keys=())
    assert classify_member_need_state(negating, "steps") == "not_addressed"

    # 只有包装层即使带 need_keys 也不能升级成 addresses。
    packaging = _member("BV3", content_depth="title_description", need_keys=("steps",))
    assert classify_member_need_state(packaging, "steps") == "mentions"


def test_supply_member_rejects_unknown_format_enum() -> None:
    """format 必须是白名单值（不能仅凭标题猜）。"""
    with pytest.raises(ValueError):
        _member("BV1", format="guess_from_title")


def test_content_evaluable_coverage_and_participants() -> None:
    """正文可评估覆盖 + 参与者去重计数。"""
    members = [
        _member("BV1", content_depth="provided_transcript", author_mid=1),
        _member("BV2", content_depth="reviewed_content", author_mid=1),
        _member("BV3", content_depth="title_only", author_mid=2),
        _member("BV4", content_depth="title_only", author_mid=2),
    ]
    assert content_evaluable_coverage(members) == pytest.approx(0.5)
    assert distinct_participant_count(members) == 2


# =========================================================================== §7.3 矩阵


def test_need_supply_matrix_states() -> None:
    """矩阵四状态：无正文未提 = unknown；有正文未提 = not_addressed。"""
    members = [
        _member("BV1", content_depth="title_only", need_keys=()),
        _member("BV2", content_depth="title_only", need_keys=("k",)),
        _member("BV3", content_depth="provided_transcript", need_keys=()),
        _member(
            "BV4",
            content_depth="provided_transcript",
            need_keys=("k",),
            positive_evidence_refs=("ref",),
        ),
    ]
    counts = build_need_supply_matrix(members, ["k"])["k"]["counts"]

    assert counts["unknown"] == 1  # BV1：无正文未提
    assert counts["mentions"] == 1  # BV2：包装层提到
    assert counts["not_addressed"] == 1  # BV3：有正文可评估否定
    assert counts["addresses"] == 1  # BV4：有正文 + 证据
    assert sum(counts.values()) == 4


# =========================================================================== §7.4 缺口候选


def test_candidate_gap_requires_body_coverage_threshold() -> None:
    """candidate_gap 需正文可评估覆盖 >= 0.7，否则只能 coverage_probe。"""
    body = [
        _member(f"B{i}", content_depth="provided_transcript", author_mid=i, need_keys=())
        for i in range(7)
    ]
    thin = [_member("T0", content_depth="title_only", author_mid=7)]
    met = assess_gap_candidate(
        body + thin, "x", query_count=2, retrieval_completed=True
    )
    assert content_evaluable_coverage(body + thin) == pytest.approx(7 / 8)
    assert met.result_type == GAP_CANDIDATE
    assert "body_evaluable_coverage_met" in met.reason_codes
    assert "sample_scope_only" in met.reason_codes

    half = [
        _member(f"H{i}", content_depth="provided_transcript" if i < 5 else "title_only",
                author_mid=i, need_keys=())
        for i in range(10)
    ]
    below = assess_gap_candidate(
        half, "x", query_count=2, retrieval_completed=True
    )
    assert content_evaluable_coverage(half) == pytest.approx(0.5)
    assert below.result_type == GAP_COVERAGE_PROBE
    assert below.result_type != GAP_CANDIDATE
    assert "coverage_below_threshold" in below.reason_codes


def test_supply_present_and_differentiation() -> None:
    """supply_present：多条可确认 addresses；differentiation：用户有明确不同素材。"""
    def _addr(i: int) -> SupplyMember:
        return _member(
            f"A{i}",
            content_depth="provided_transcript",
            author_mid=i,
            need_keys=("k",),
            positive_evidence_refs=(f"ref{i}",),
        )

    present = assess_gap_candidate(
        [_addr(1), _addr(2), _addr(3)], "k", query_count=2, retrieval_completed=True
    )
    assert present.result_type == GAP_SUPPLY_PRESENT

    diff = assess_gap_candidate(
        [_addr(1), _member("P2", content_depth="title_only", author_mid=2),
         _member("P3", content_depth="title_only", author_mid=3)],
        "k",
        query_count=2,
        retrieval_completed=True,
        user_has_distinct_asset=True,
    )
    assert diff.result_type == GAP_DIFFERENTIATION


def test_gap_requires_completed_retrieval() -> None:
    """检索未完成 → not_enough_evidence（不编造缺口）。"""
    members = [_member(f"B{i}", content_depth="provided_transcript", author_mid=i) for i in range(4)]
    out = assess_gap_candidate(members, "x", query_count=0, retrieval_completed=False)
    assert out.result_type == GAP_NOT_ENOUGH_EVIDENCE
    assert "retrieval_incomplete" in out.reason_codes


# =========================================================================== §7.5 反例四组


def test_anti_example_1_same_user_duplicated_comments_fails_three_participants() -> None:
    """反例①：同一用户复制 50 条评论 → 不满足 3 参与者。"""
    members = [_member(f"C{i}", content_depth="title_only", author_mid=1) for i in range(50)]
    assert distinct_participant_count(members) == 1

    out = assess_gap_candidate(members, "x", query_count=2, retrieval_completed=True)
    assert out.result_type == GAP_NOT_ENOUGH_EVIDENCE
    assert "insufficient_participants" in out.reason_codes
    assert out.distinct_participants == 1


def test_anti_example_2_titles_without_body_is_unknown_not_zero_supply() -> None:
    """反例②：20 标题无关键词但无正文 → coverage unknown，不是 0 供给。"""
    members = [
        _member(f"T{i}", content_depth="title_only", need_keys=(), author_mid=i)
        for i in range(20)
    ]
    matrix = build_need_supply_matrix(members, ["k"])["k"]
    assert matrix["counts"]["unknown"] == 20
    assert matrix["counts"]["addresses"] == 0
    assert matrix["counts"]["not_addressed"] == 0  # 无正文不得判否定

    out = assess_gap_candidate(members, "k", query_count=2, retrieval_completed=True)
    assert out.content_evaluable_coverage == 0.0
    assert out.result_type == GAP_PACKAGING
    assert out.result_type != GAP_CANDIDATE
    assert "packaging_layer_only" in out.reason_codes
    assert "not_for_high_investment" in out.reason_codes


def test_anti_example_3_old_videos_found_is_search_result_only() -> None:
    """反例③：新搜到 10 条旧片 → 只增加供给检索结果，不是新发布竞争激增。"""
    change = interpret_supply_change(newly_discovered=10, recently_published=0)
    assert change.interpretation == "supply_search_results_only"
    assert "supply_search_results_only" in change.reason_codes
    assert "recently_published_present" not in change.reason_codes


def test_anti_example_4_page_expansion_is_sampling_changed() -> None:
    """反例④：扩页后结果增加 → sampling_changed，不解读为供给增长。"""
    change = interpret_supply_change(
        newly_discovered=80, recently_published=30, sampling_changed=True
    )
    assert change.interpretation == "sampling_changed"
    assert change.reason_codes == ("sampling_changed",)


def test_recently_published_requires_window_confirmation() -> None:
    """只有确认窗口内新发布才给对应解读。"""
    change = interpret_supply_change(newly_discovered=5, recently_published=2)
    assert change.interpretation == "recently_published_present"


# =========================================================================== channel_b 接线


def test_channel_b_angle_density_uses_triplet_denominator() -> None:
    """通道 B：angle_density 换成 §7.2 三件套，分母是 |C|。"""
    run = DiscoveryRunView(
        "r1", 0, 1, "planH", 1, 1, ("kw",), 600, True,
        supply_members=(
            _member("BV1", angle="tutorial"),
            _member("BV2", angle="review"),
            _member("BV3", angle="unclassified"),
        ),
    )
    signals = discovery_signals(run)
    density = signals["angle_density"]

    assert density["classified_count"] == 2
    assert density["member_count"] == 3
    assert density["angle_share"]["tutorial"] == pytest.approx(0.5)  # 1/|C| 不是 1/|E|
    assert density["angle_coverage"] == pytest.approx(2 / 3)
    assert density["unknown_angle_count"] == 1
    assert signals["angle_density_label"] == ANGLE_DENSITY_LABEL
    assert CHANNEL_B_LABEL == "已见样本角度拥挤线索"


def test_channel_b_legacy_angle_tokens_no_longer_form_denominator() -> None:
    """旧 token 字段保留，但不再充当角度密度分母（C 空 → share 全 None）。"""
    run = DiscoveryRunView(
        "r1", 0, 1, "planH", 1, 1, ("kw",), 600, True,
        angle_tokens=("tutorial", "tutorial", "review"),
    )
    density = discovery_signals(run)["angle_density"]
    assert all(value is None for value in density["angle_share"].values())
    assert density["angle_coverage"] is None


# =========================================================================== 无网络 / 措辞 / 门槛


def test_no_network_or_download_dependencies() -> None:
    """无网络：断言本批不触发任何下载 / ASR / 网络依赖（纯标准库）。"""
    banned = {
        "requests",
        "aiohttp",
        "httpx",
        "urllib",
        "socket",
        "subprocess",
        "yt_dlp",
        "youtube_dl",
        "pytube",
        "whisper",
        "ffmpeg",
        "asyncio",
    }
    for src_path in (SUPPLY_SRC, CHANNEL_B_SRC):
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
        leaked = imported & banned
        assert not leaked, f"{src_path.name} 引入了禁用依赖：{leaked}"


def test_wording_only_allowed_label_and_no_market_hype() -> None:
    """措辞：输出标签只含「已见样本角度拥挤线索」，禁止市场级夸大字样。"""
    labels = [ANGLE_DENSITY_LABEL, *SUPPLY_OUTPUT_LABELS]

    notes: list[str] = []
    members = [
        _member(f"W{i}", content_depth="provided_transcript", author_mid=i) for i in range(4)
    ]
    for completed, queries, distinct in (
        (True, 2, False),
        (True, 2, True),
        (False, 0, False),
    ):
        assessment = assess_gap_candidate(
            members,
            "w",
            query_count=queries,
            retrieval_completed=completed,
            user_has_distinct_asset=distinct,
        )
        notes.extend(assessment.notes)
    for change in (
        interpret_supply_change(newly_discovered=1, recently_published=0),
        interpret_supply_change(newly_discovered=0, recently_published=1),
        interpret_supply_change(newly_discovered=0, recently_published=0, sampling_changed=True),
    ):
        labels.append(change.label)

    blob = "\n".join(labels + notes)
    assert ANGLE_DENSITY_LABEL == "已见样本角度拥挤线索"
    assert "已见样本角度拥挤线索" in blob
    for phrase in FORBIDDEN_WORDING:
        assert phrase not in blob, phrase


def test_supply_policy_defaults_and_version_output() -> None:
    """门槛：threshold=0.7 / 3 作者为可配置默认，并输出版本号。"""
    policy = SupplyPolicy()
    assert policy.candidate_gap_coverage_threshold == 0.7
    assert policy.candidate_gap_min_authors == 3
    assert policy.policy_version
    assert policy.as_dict()["policy_version"] == policy.policy_version

    density = compute_angle_density([])
    assert density.policy_version == policy.policy_version


def test_supply_policy_from_config_reads_isolated_section() -> None:
    """可配置：读独立 ``supply`` 段；未知字段报错，不静默降级。"""

    class _FakeConfig:
        """最小 ConfigManager 替身（只提供 ``get``）。"""

        def __init__(self, data: dict) -> None:
            self._data = data

        def get(self, key: str, default=None):
            return self._data.get(key, default)

    policy = SupplyPolicy.from_config(
        _FakeConfig({"supply": {"candidate_gap_min_authors": 5, "policy_version": "supply_c_v9"}})
    )
    assert policy.candidate_gap_min_authors == 5
    assert policy.policy_version == "supply_c_v9"

    with pytest.raises(ValueError):
        SupplyPolicy.from_config(_FakeConfig({"supply": {"unknown_key": 1}}))


def test_shipped_config_has_isolated_supply_section() -> None:
    """随包 config.yaml：supply 独立顶层段，未污染 hotspot.events 白名单段。"""
    cfg = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert cfg["supply"]["candidate_gap_coverage_threshold"] == 0.7
    assert cfg["supply"]["candidate_gap_min_authors"] == 3
    assert cfg["supply"]["policy_version"]
    assert "supply" not in cfg["hotspot"]["events"]
    assert "candidate_gap_coverage_threshold" not in cfg["hotspot"]["events"]


def test_frontend_wires_supply_angle_density_slot() -> None:
    """前端：供给角度密度展示位接线，coverage 不足标「不足」而不是显示 0。"""
    js = (PROJECT_ROOT / "web" / "frontend" / "static" / "js" / "app.events.js").read_text(
        encoding="utf-8"
    )
    assert "renderSupplyAngleDensity" in js
    assert "angle_density" in js
    assert "不足" in js
