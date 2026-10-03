"""FishTool 04 · 第四批 c：供给角度密度与缺口候选（§7.1 / §7.2 / §7.3 / §7.4 / §7.5）。

上游依据：``FishTool_06_六层深度研判_差距评估与04补充执行案.md``

- **§7.1** L377-394 分开「作品可见包装」与「正文覆盖」
- **§7.2** L396-406 角度密度的唯一公式（三件套）
- **§7.3** L408-419 需求×供给矩阵
- **§7.4** L421-432 缺口候选政策
- **§7.5** L434-439 防误推荐反例

三件套公式（照抄 §7.2，**口径不许自己改**）：

.. code-block:: text

    angle_share[k]      = 属于角度 k 的唯一 BVID 数量 / |C|
    angle_coverage      = |C| / |E|
    unknown_angle_count = |E| - |C|

其中 ``E`` = 有效匹配成员集合、``C`` = 能分类角度的集合。**分母是 |C| 不是 |E|**。

红线（本批）：

- **不引入任何视频下载 / ASR / 转写依赖**（永久禁）；本模块纯标准库、无任何网络路径。
- ``C`` 为空 → ``angle_share`` 全部 ``None``（**不填 0**）。
- 小样本 / 低 coverage → **不评价拥挤 / 稀缺**，只列已见内容。
- 无正文时未提某需求 = ``unknown``，**不是 ``not_addressed``**（不是 0 供给）。
- 标题声称「教程」只证明**包装声称**，不证明步骤完整；用户提供的转写可作证据，
  **不许伪称已自动观看全片 / 已跑 ASR**。
- 只称「已见样本角度拥挤线索」；**不得**用 ``1 - 标题命中率`` 折算任何「未满足率」，
  也不得使用任何市场级夸大措辞（措辞自检见 ``SUPPLY_OUTPUT_LABELS`` 与测试）。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .config import DEFAULT_SUPPLY_POLICY_VERSION, SupplyPolicy

# --------------------------------------------------------------------------- 常量

#: 「角度拥挤」只称已见样本线索，不升格为任何市场级结论。
ANGLE_DENSITY_LABEL: str = "已见样本角度拥挤线索"

#: 记录字段 ``format`` 允许取值（06 §7.1，**不能仅凭标题猜**）。
FORMATS: tuple[str, ...] = ("long_video", "short_video", "live", "article", "unknown")
#: 记录字段 ``angle`` 允许取值。
ANGLES: tuple[str, ...] = (
    "news",
    "explanation",
    "tutorial",
    "review",
    "comparison",
    "summary",
    "reaction",
    "other",
    "unclassified",
)
#: 能进入 ``C``（可分类角度）的角度；``unclassified`` 只说明「无法分类」，不进分母。
CLASSIFIABLE_ANGLES: tuple[str, ...] = (
    "news",
    "explanation",
    "tutorial",
    "review",
    "comparison",
    "summary",
    "reaction",
    "other",
)
#: 无法分类角度的取值。
UNCLASSIFIED_ANGLE: str = "unclassified"

#: 记录字段 ``content_depth`` 允许取值（06 §7.1）。
CONTENT_DEPTHS: tuple[str, ...] = (
    "title_only",
    "title_description",
    "provided_transcript",
    "reviewed_content",
)
#: **正文可评估**深度：只有这两种才算拿到正文证据（其余只是包装层）。
CONTENT_EVIDENCE_DEPTHS: tuple[str, ...] = ("provided_transcript", "reviewed_content")
#: 仅包装层（标题 / 简介）深度：可运行，但不驱动高投入。
PACKAGING_DEPTHS: tuple[str, ...] = ("title_only", "title_description")

#: 记录字段 ``classification_source`` 允许取值。
CLASSIFICATION_SOURCES: tuple[str, ...] = ("strict_rule", "manual", "llm_proposed")

#: 需求×供给矩阵状态（06 §7.3）。
MATRIX_STATES: tuple[str, ...] = ("addresses", "mentions", "not_addressed", "unknown")

#: 缺口候选结果类型（06 §7.4）。
GAP_CANDIDATE: str = "candidate_gap"
GAP_DIFFERENTIATION: str = "differentiation_candidate"
GAP_SUPPLY_PRESENT: str = "supply_present"
GAP_NOT_ENOUGH_EVIDENCE: str = "not_enough_evidence"
#: 覆盖不足时的降级标签：只能放研究草案，**不许升格为市场级缺口结论**。
GAP_COVERAGE_PROBE: str = "coverage_probe"
#: 仅 title/description 模式下的检索包装层线索，**不驱动高投入**。
GAP_PACKAGING: str = "packaging_gap_candidate"

# 策略默认值集中定义在 ``events/config.py`` 的 ``SupplyPolicy``（与既有 DailyPolicy /
# EarlyPolicy / DiscoveryPolicy 同处），本模块只消费，不重复定义口径默认值。

# --------------------------------------------------------------------------- 记录


@dataclass(frozen=True)
class SupplyMember:
    """一个已接受成员的供给记录（06 §7.1）。

    Attributes:
        bvid: 唯一作品标识（**去重分母用唯一 BVID**）。
        format: ``long_video`` / ``short_video`` / ``live`` / ``article`` / ``unknown``；
            **不能仅凭标题猜**，无法确认就填 ``unknown``。
        angle: 主角度；无法分类填 ``unclassified``（只说明无法分类，不进 ``|C|``）。
        need_keys: 包装层（标题 / 简介）命中的需求键。
        content_depth: ``title_only`` / ``title_description`` / ``provided_transcript`` /
            ``reviewed_content``；**只有后两者算正文证据**。
        classification_source: ``strict_rule`` / ``manual`` / ``llm_proposed``。
        positive_evidence_refs: 明确回应某需求的正文证据引用（可为空）。
        secondary_angles: 副角度；**只展示、不进主分母**。
        need_states: 需求键 -> 矩阵状态（显式判定，优先于派生规则）。
        author_mid: 参与者（作者 / 用户）标识；用于「最少 3 参与者」门与去重。
    """

    bvid: str
    format: str = "unknown"
    angle: str = UNCLASSIFIED_ANGLE
    need_keys: tuple[str, ...] = ()
    content_depth: str = "title_only"
    classification_source: str = "strict_rule"
    positive_evidence_refs: tuple[str, ...] = ()
    secondary_angles: tuple[str, ...] = ()
    need_states: Mapping[str, str] = field(default_factory=dict)
    author_mid: int | None = None

    def __post_init__(self) -> None:
        """构造时严格校验枚举字段（非法即报错，不静默降级）。"""
        if self.format not in FORMATS:
            raise ValueError(f"invalid_supply_member_format:{self.format}")
        if self.angle not in ANGLES:
            raise ValueError(f"invalid_supply_member_angle:{self.angle}")
        if self.content_depth not in CONTENT_DEPTHS:
            raise ValueError(f"invalid_supply_member_content_depth:{self.content_depth}")
        if self.classification_source not in CLASSIFICATION_SOURCES:
            raise ValueError(
                f"invalid_supply_member_classification_source:{self.classification_source}"
            )
        for angle in self.secondary_angles:
            if angle not in ANGLES:
                raise ValueError(f"invalid_supply_member_secondary_angle:{angle}")
        for need_key, state in self.need_states.items():
            if state not in MATRIX_STATES:
                raise ValueError(f"invalid_supply_member_need_state:{need_key}:{state}")

    @property
    def has_content_evidence(self) -> bool:
        """是否拿到**正文证据**（``provided_transcript`` / ``reviewed_content``）。

        Returns:
            bool: 仅包装层（title_only / title_description）返回 ``False``。
        """
        return self.content_depth in CONTENT_EVIDENCE_DEPTHS

    @property
    def is_packaging_only(self) -> bool:
        """是否只有包装层可见（标题 / 简介）。

        Returns:
            bool: 未拿到正文时为 ``True``。
        """
        return self.content_depth in PACKAGING_DEPTHS


# --------------------------------------------------------------------------- 去重


def _unique_members_by_bvid(members: Iterable[SupplyMember]) -> tuple[list[SupplyMember], bool]:
    """按唯一 BVID 去重（保留首次出现），用于 |E| / |C| 分母。

    Args:
        members: 供给成员序列。

    Returns:
        ``(unique_members, had_duplicate)``：去重后的成员列表与是否出现过重复 BVID。
    """
    seen: dict[str, SupplyMember] = {}
    had_duplicate = False
    for member in members:
        if member.bvid in seen:
            had_duplicate = True
            continue
        seen[member.bvid] = member
    return list(seen.values()), had_duplicate


# --------------------------------------------------------------------------- 三件套


@dataclass(frozen=True)
class AngleDensity:
    """§7.2 角度密度三件套结果。

    Attributes:
        member_count: |E|，有效匹配成员（唯一 BVID）数量。
        classified_count: |C|，能分类角度的成员数量。
        angle_share: 角度 -> 唯一 BVID 数 / |C|；``C`` 空时全 ``None``。
        angle_coverage: |C| / |E|；``|E| == 0`` 时为 ``None``。
        unknown_angle_count: |E| - |C|。
        secondary_angle_index: 副角度 -> 展示用 BVID 列表（**不进分母**）。
        evaluable: 是否可评价「拥挤 / 稀缺」。
        reason_codes: 稳定原因码。
        label: 固定对外标签「已见样本角度拥挤线索」。
        policy_version: 策略版本。
    """

    member_count: int
    classified_count: int
    angle_share: Mapping[str, float | None]
    angle_coverage: float | None
    unknown_angle_count: int
    secondary_angle_index: Mapping[str, tuple[str, ...]]
    evaluable: bool
    reason_codes: tuple[str, ...]
    label: str = ANGLE_DENSITY_LABEL
    policy_version: str = DEFAULT_SUPPLY_POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        """导出为可序列化字典（供信号输出 / 日志 / 前端消费）。"""
        return {
            "member_count": self.member_count,
            "classified_count": self.classified_count,
            "angle_share": dict(self.angle_share),
            "angle_coverage": self.angle_coverage,
            "unknown_angle_count": self.unknown_angle_count,
            "secondary_angles": {k: list(v) for k, v in self.secondary_angle_index.items()},
            "evaluable": self.evaluable,
            "reason_codes": list(self.reason_codes),
            "label": self.label,
            "policy_version": self.policy_version,
        }


def compute_angle_density(
    members: Iterable[SupplyMember], *, policy: SupplyPolicy | None = None
) -> AngleDensity:
    """计算 §7.2 角度密度三件套。

    **分母是 |C| 不是 |E|**；每视频一个主角度，副角度只展示不重复计主分母；
    ``C`` 为空 → ``angle_share`` 全部 ``None``（不填 0）；小样本 / 低 coverage 不评价拥挤。

    Args:
        members: 供给成员序列（按唯一 BVID 去重）。
        policy: 供给策略；``None`` 用默认。

    Returns:
        AngleDensity: 三件套结果 + 可评价标志 + 原因码。
    """
    policy = policy or SupplyPolicy()
    unique, had_duplicate = _unique_members_by_bvid(members)

    member_count = len(unique)
    classified = [m for m in unique if m.angle != UNCLASSIFIED_ANGLE]
    classified_count = len(classified)

    reason_codes: list[str] = []
    if had_duplicate:
        reason_codes.append("duplicate_bvid_deduplicated")

    # 主角度唯一计数（每视频一个主角度）。
    counts: dict[str, int] = {angle: 0 for angle in CLASSIFIABLE_ANGLES}
    for member in classified:
        counts[member.angle] += 1

    if classified_count == 0:
        # C 为空 → share 全 NULL（不填 0）。
        angle_share: dict[str, float | None] = {angle: None for angle in CLASSIFIABLE_ANGLES}
        angle_coverage: float | None = None
        evaluable = False
        reason_codes.append("no_classifiable_angle")
        reason_codes.append("crowding_not_evaluated")
    else:
        angle_share = {angle: counts[angle] / classified_count for angle in CLASSIFIABLE_ANGLES}
        angle_coverage = classified_count / member_count if member_count > 0 else None
        evaluable = (
            classified_count >= policy.angle_min_classified
            and angle_coverage is not None
            and angle_coverage >= policy.angle_min_coverage
        )
        if classified_count < policy.angle_min_classified:
            reason_codes.append("angle_small_sample")
        if angle_coverage is not None and angle_coverage < policy.angle_min_coverage:
            reason_codes.append("angle_low_coverage")
        if not evaluable:
            reason_codes.append("crowding_not_evaluated")

    # 副角度只展示、不进主分母。
    secondary_index: dict[str, list[str]] = {}
    for member in unique:
        for angle in member.secondary_angles:
            if angle not in CLASSIFIABLE_ANGLES:
                continue
            bucket = secondary_index.setdefault(angle, [])
            if member.bvid not in bucket:
                bucket.append(member.bvid)

    return AngleDensity(
        member_count=member_count,
        classified_count=classified_count,
        angle_share=angle_share,
        angle_coverage=angle_coverage,
        unknown_angle_count=member_count - classified_count,
        secondary_angle_index={angle: tuple(bvids) for angle, bvids in secondary_index.items()},
        evaluable=evaluable,
        reason_codes=tuple(reason_codes),
        policy_version=policy.policy_version,
    )


# --------------------------------------------------------------------------- 需求×供给矩阵


def classify_member_need_state(member: SupplyMember, need_key: str) -> str:
    """判定单个成员对单个需求键的矩阵状态（06 §7.3）。

    铁律：没有正文时，「未提某需求」是 ``unknown``，**不是 ``not_addressed``**。

    Args:
        member: 供给成员记录。
        need_key: 需求键。

    Returns:
        str: ``addresses`` / ``mentions`` / ``not_addressed`` / ``unknown``。
    """
    explicit = member.need_states.get(need_key)
    if explicit is not None:
        return explicit

    mentioned = need_key in member.need_keys
    if member.has_content_evidence:
        # 有正文可评估：有明确证据引用才算 addresses；否则可有范围地否定。
        if mentioned and member.positive_evidence_refs:
            return "addresses"
        if mentioned:
            return "mentions"
        return "not_addressed"
    # 仅包装层：提到=mentions；未提=unknown（不是 0 供给 / 不是 not_addressed）。
    return "mentions" if mentioned else "unknown"


def build_need_supply_matrix(
    members: Iterable[SupplyMember], need_keys: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """构建需求×供给矩阵（06 §7.3）。

    Args:
        members: 供给成员序列（按唯一 BVID 去重）。
        need_keys: 需求键序列。

    Returns:
        dict: 每个 need_key -> ``{"states": {bvid: state}, "counts": {...}}``。
    """
    unique, _ = _unique_members_by_bvid(members)
    matrix: dict[str, dict[str, Any]] = {}
    for need_key in need_keys:
        states: dict[str, str] = {}
        counts: dict[str, int] = {state: 0 for state in MATRIX_STATES}
        for member in unique:
            state = classify_member_need_state(member, need_key)
            states[member.bvid] = state
            counts[state] += 1
        matrix[need_key] = {"states": states, "counts": counts}
    return matrix


def content_evaluable_coverage(members: Iterable[SupplyMember]) -> float | None:
    """正文可评估覆盖 = 有正文证据的唯一 BVID 数 / |E|。

    Args:
        members: 供给成员序列。

    Returns:
        float | None: 覆盖比例；无成员时为 ``None``。
    """
    unique, _ = _unique_members_by_bvid(members)
    if not unique:
        return None
    evaluable = sum(1 for m in unique if m.has_content_evidence)
    return evaluable / len(unique)


def distinct_participant_count(members: Iterable[SupplyMember]) -> int:
    """参与者（作者 / 用户）去重计数——同一人复制多条只算 1。

    Args:
        members: 供给成员序列。

    Returns:
        int: 唯一 ``author_mid`` 数量（未提供 author_mid 的成员不计入）。
    """
    unique, _ = _unique_members_by_bvid(members)
    author_ids = {m.author_mid for m in unique if m.author_mid is not None}
    return len(author_ids)


# --------------------------------------------------------------------------- 缺口候选


@dataclass(frozen=True)
class GapAssessment:
    """单个需求键的缺口候选评估（06 §7.4）。

    Attributes:
        need_key: 需求键。
        result_type: 结果类型（见模块常量 ``GAP_*``）。
        content_evaluable_coverage: 正文可评估覆盖；``None`` 表示无成员。
        counts: 四个矩阵状态的计数。
        distinct_participants: 去重后的参与人数。
        query_count: 本次定向检索查询次数。
        retrieval_completed: 定向检索是否按计划完成。
        reason_codes: 稳定原因码。
        notes: 面向用户的边界说明（**不含被禁措辞**）。
        policy_version: 策略版本。
    """

    need_key: str
    result_type: str
    content_evaluable_coverage: float | None
    counts: Mapping[str, int]
    distinct_participants: int
    query_count: int
    retrieval_completed: bool
    reason_codes: tuple[str, ...]
    notes: tuple[str, ...]
    policy_version: str = DEFAULT_SUPPLY_POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        """导出为可序列化字典。"""
        return {
            "need_key": self.need_key,
            "result_type": self.result_type,
            "content_evaluable_coverage": self.content_evaluable_coverage,
            "counts": dict(self.counts),
            "distinct_participants": self.distinct_participants,
            "query_count": self.query_count,
            "retrieval_completed": self.retrieval_completed,
            "reason_codes": list(self.reason_codes),
            "notes": list(self.notes),
            "policy_version": self.policy_version,
        }


def assess_gap_candidate(
    members: Iterable[SupplyMember],
    need_key: str,
    *,
    policy: SupplyPolicy | None = None,
    query_count: int = 0,
    retrieval_completed: bool = False,
    user_has_distinct_asset: bool = False,
) -> GapAssessment:
    """按 06 §7.4 判定单个需求键的缺口候选类型。

    硬约束：``candidate_gap`` 需**正文可评估覆盖 >= 0.7**，否则只能 ``coverage_probe``
    （放研究草案，**不许升格为市场级缺口结论**）；仅 title/description 模式用
    ``packaging_gap_candidate``（**不驱动高投入**）；达到门 **≠ 全网没有竞争**。

    Args:
        members: 供给成员序列。
        need_key: 需求键。
        policy: 供给策略；``None`` 用默认。
        query_count: 定向检索查询次数。
        retrieval_completed: 定向检索是否按计划完成。
        user_has_distinct_asset: 用户是否有可验证的不同素材 / 视角 / 形式。

    Returns:
        GapAssessment: 结果类型 + 原因码 + 边界说明。
    """
    policy = policy or SupplyPolicy()
    unique, _ = _unique_members_by_bvid(members)

    coverage = content_evaluable_coverage(unique)
    matrix = build_need_supply_matrix(unique, [need_key])[need_key]
    counts: dict[str, int] = dict(matrix["counts"])
    participants = distinct_participant_count(unique)

    def _result(result_type: str, reason_codes: list[str], notes: list[str]) -> GapAssessment:
        """构造结果，统一带上上下文与策略版本。"""
        return GapAssessment(
            need_key=need_key,
            result_type=result_type,
            content_evaluable_coverage=coverage,
            counts=counts,
            distinct_participants=participants,
            query_count=int(query_count),
            retrieval_completed=bool(retrieval_completed),
            reason_codes=tuple(reason_codes),
            notes=tuple(notes),
            policy_version=policy.policy_version,
        )

    # 反例①：同一参与者复制多条 → 不满足 3 参与者。
    if not retrieval_completed:
        return _result(
            GAP_NOT_ENOUGH_EVIDENCE,
            ["retrieval_incomplete"],
            ["定向检索未按计划完成，本次样本不足以判定供给缺口。"],
        )
    if query_count < policy.candidate_gap_min_queries:
        return _result(
            GAP_NOT_ENOUGH_EVIDENCE,
            ["insufficient_queries"],
            [f"定向检索查询数 {query_count} 少于门槛 {policy.candidate_gap_min_queries}。"],
        )
    if participants < policy.candidate_gap_min_authors:
        return _result(
            GAP_NOT_ENOUGH_EVIDENCE,
            ["insufficient_participants"],
            [
                f"去重后参与者 {participants} 少于门槛 {policy.candidate_gap_min_authors}；"
                "同一参与者复制多条只算 1，不足 3 参与者不构成供给样本。"
            ],
        )

    if counts["addresses"] >= policy.supply_present_min_addresses:
        return _result(
            GAP_SUPPLY_PRESENT,
            ["multiple_addresses_present"],
            ["已见样本内有多条可确认 addresses 作品，建议先列出链接避免重复制作。"],
        )
    if user_has_distinct_asset and counts["addresses"] >= 1:
        return _result(
            GAP_DIFFERENTIATION,
            ["distinct_asset_available"],
            ["已见相似内容，但用户有明确不同素材 / 视角 / 形式可验证。"],
        )
    if coverage is not None and coverage >= policy.candidate_gap_coverage_threshold:
        return _result(
            GAP_CANDIDATE,
            ["body_evaluable_coverage_met", "sample_scope_only"],
            [
                "本次样本内可确认回应较少；仅证明样本内结论，"
                "达到门槛 ≠ 全网没有竞争 / 不代表全网缺少此类作品。",
            ],
        )
    if coverage is not None and coverage == 0.0:
        return _result(
            GAP_PACKAGING,
            ["packaging_layer_only", "not_for_high_investment"],
            [
                "仅有标题 / 简介（包装层）线索，未看到正文；"
                "属检索包装层线索，不驱动高投入，也不等于 0 供给。",
            ],
        )
    return _result(
        GAP_COVERAGE_PROBE,
        ["coverage_below_threshold", "coverage_probe_only"],
        [
            f"正文可评估覆盖 {coverage} 低于门槛 {policy.candidate_gap_coverage_threshold}；"
            "只能作为研究草案的 coverage_probe。",
        ],
    )


# --------------------------------------------------------------------------- 供给变化解读（反例③④）


@dataclass(frozen=True)
class SupplyChange:
    """供给变化的合规解读（06 §7.5 反例③④）。

    Attributes:
        newly_discovered: 本次新发现的供给结果数。
        recently_published: 其中 pubdate 确在窗口内的数量。
        sampling_changed: 采样口径是否变化（扩页 / 换词等）。
        interpretation: 稳定解读码。
        reason_codes: 稳定原因码。
        label: 面向用户的说明（**不含被禁措辞**）。
    """

    newly_discovered: int
    recently_published: int
    sampling_changed: bool
    interpretation: str
    reason_codes: tuple[str, ...]
    label: str

    def as_dict(self) -> dict[str, Any]:
        """导出为可序列化字典。"""
        return {
            "newly_discovered": self.newly_discovered,
            "recently_published": self.recently_published,
            "sampling_changed": self.sampling_changed,
            "interpretation": self.interpretation,
            "reason_codes": list(self.reason_codes),
            "label": self.label,
        }


def interpret_supply_change(
    *,
    newly_discovered: int,
    recently_published: int,
    sampling_changed: bool = False,
) -> SupplyChange:
    """把「供给结果数变化」解读为合规结论（06 §7.5 反例③④）。

    - 扩页 / 换词引起的变化 → ``sampling_changed``，**不解读为供给增长**；
    - 新搜到旧片 → ``supply_search_results_only``，**不是新发布竞争增加**；
    - 只有确认窗口内新发布才 ``recently_published_present``。

    Args:
        newly_discovered: 本次新发现的供给结果数。
        recently_published: 其中窗口内新发布数量。
        sampling_changed: 采样口径是否变化。

    Returns:
        SupplyChange: 合规解读。
    """
    if sampling_changed:
        return SupplyChange(
            newly_discovered=int(newly_discovered),
            recently_published=int(recently_published),
            sampling_changed=True,
            interpretation="sampling_changed",
            reason_codes=("sampling_changed",),
            label="采样口径变化，本批不解读为供给变化。",
        )
    if newly_discovered > 0 and recently_published <= 0:
        return SupplyChange(
            newly_discovered=int(newly_discovered),
            recently_published=int(recently_published),
            sampling_changed=False,
            interpretation="supply_search_results_only",
            reason_codes=("supply_search_results_only",),
            label="已见样本供给检索结果增加（含旧片，不代表新发布竞争增加）。",
        )
    if recently_published > 0:
        return SupplyChange(
            newly_discovered=int(newly_discovered),
            recently_published=int(recently_published),
            sampling_changed=False,
            interpretation="recently_published_present",
            reason_codes=("recently_published_present",),
            label="本次已见样本内确认有窗口内新发布。",
        )
    return SupplyChange(
        newly_discovered=int(newly_discovered),
        recently_published=int(recently_published),
        sampling_changed=False,
        interpretation="no_change",
        reason_codes=("no_change",),
        label="本次样本内未见供给变化。",
    )


#: 全部对外标签（供措辞自检：禁止任何市场级夸大措辞，见测试）。
SUPPLY_OUTPUT_LABELS: tuple[str, ...] = (
    ANGLE_DENSITY_LABEL,
    "采样口径变化，本批不解读为供给变化。",
    "已见样本供给检索结果增加（含旧片，不代表新发布竞争增加）。",
    "本次已见样本内确认有窗口内新发布。",
    "本次样本内未见供给变化。",
)


__all__ = [
    # 常量
    "ANGLE_DENSITY_LABEL",
    "FORMATS",
    "ANGLES",
    "CLASSIFIABLE_ANGLES",
    "UNCLASSIFIED_ANGLE",
    "CONTENT_DEPTHS",
    "CONTENT_EVIDENCE_DEPTHS",
    "PACKAGING_DEPTHS",
    "CLASSIFICATION_SOURCES",
    "MATRIX_STATES",
    "GAP_CANDIDATE",
    "GAP_DIFFERENTIATION",
    "GAP_SUPPLY_PRESENT",
    "GAP_NOT_ENOUGH_EVIDENCE",
    "GAP_COVERAGE_PROBE",
    "GAP_PACKAGING",
    "DEFAULT_SUPPLY_POLICY_VERSION",
    "SUPPLY_OUTPUT_LABELS",
    # 策略
    "SupplyPolicy",
    # 记录
    "SupplyMember",
    # 三件套
    "AngleDensity",
    "compute_angle_density",
    # 矩阵
    "classify_member_need_state",
    "build_need_supply_matrix",
    "content_evaluable_coverage",
    "distinct_participant_count",
    # 缺口候选
    "GapAssessment",
    "assess_gap_candidate",
    # 供给变化
    "SupplyChange",
    "interpret_supply_change",
]
