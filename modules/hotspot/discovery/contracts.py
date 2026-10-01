"""06 采集广度 · 发现通道契约层（规格 §4 + 顶部复核意见）。

本模块只放**纯数据契约与纯函数**：DTO、质量三态解析、来源优先级与合并规则。
不 import 网络 / DB / Web，不发起任何 I/O，供 ``sources.py``（I/O+解析）与
``service.py``（调度+落库）共同复用。

硬约束（顶部复核意见优先于 06 R1.0 正文，落地在本层）：

1. **去重口径**：候选身份按 ``bvid`` 去重，**发现来源全部保留**；原始统计保留
   来源 + 观测时间；展示值按**预先固定的来源优先级**取值，冲突打标记；
   未知 owner / tid / view **不得从无证据处补成已验证值**，一律记 ``missing``。
   —— 绝不按「播放量较高」挑边，避免向上选择偏差。
2. **heat_score 语义**：只叫「平台接口返回热搜分数」，**不得**命名为搜索人数 /
   播放量 / 独立用户数；**不进入播放增量计算**（与 01 口径一致）。
   ``keyword`` 有效但 ``heat_score`` 缺失 -> 保留候选、score 记 ``None`` / 状态
   ``missing``，**不整条丢**；非法值（bool / 负数 / 非数值）-> 该条跳过，
   **不得写 0**。
3. **三套分类字段并存**：``pid_v2`` / ``tidv2`` / legacy ``tid`` 分别存字段和
   状态，**不做「优先取一个当统一 tid」**（可能不是同一层级体系，数值混查会出错）。

状态三态沿用 ``core.data_quality`` 口径：``ok`` / ``missing`` / ``invalid``。
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.data_quality import parse_count

# --------------------------------------------------------------------------
# 来源标识与状态常量（避免魔法字符串散落）
# --------------------------------------------------------------------------

#: ``search/square`` 热搜关键词来源。
SOURCE_SEARCH_SQUARE = "search_square"
#: ``popular`` 综合热门来源。
SOURCE_POPULAR = "popular"
#: ``ranking/v2?rid=0`` 全站榜主条目来源。
SOURCE_RANKING_ALL = "ranking_all"
#: ``ranking/v2?rid=0`` 条目内 ``others`` 数组（同 UP 其他上榜作品），单独落库。
SOURCE_RANKING_ALL_OTHERS = "ranking_all_others"

#: 单源运行态（进快照）。
STATE_OK = "ok"
STATE_PARTIAL = "partial"
STATE_ERROR = "error"

#: 质量三态（与 core.data_quality 对齐）。
QUALITY_OK = "ok"
QUALITY_MISSING = "missing"
QUALITY_INVALID = "invalid"

#: ``heat_score`` 的语义标签：平台接口返回热搜分数，禁止改叫搜索人数/播放量/独立用户数。
HEAT_SCORE_LABEL = "platform_search_square_score"

#: **预先固定**的展示值来源优先级（高 -> 低）。
#: 同一 ``bvid`` 从多个入口被发现的，展示值取优先级最高的一条；绝不按播放量挑。
DISCOVERY_SOURCE_PRIORITY: Tuple[str, ...] = (
    SOURCE_RANKING_ALL,
    SOURCE_POPULAR,
    SOURCE_RANKING_ALL_OTHERS,
)

_SOURCE_RANK: Dict[str, int] = {name: index for index, name in enumerate(DISCOVERY_SOURCE_PRIORITY)}


def source_priority(source: str) -> int:
    """返回来源的固定优先级序号（越小越优先，未知来源排最后）。

    Args:
        source: 发现来源标识。

    Returns:
        int: 优先级序号；未登记来源返回 ``len(DISCOVERY_SOURCE_PRIORITY)``。
    """
    return _SOURCE_RANK.get(source, len(DISCOVERY_SOURCE_PRIORITY))


# --------------------------------------------------------------------------
# 质量解析（纯函数）
# --------------------------------------------------------------------------

def parse_heat_score(raw: Any) -> Tuple[Optional[int], str]:
    """解析 ``search/square`` 的 ``heat_score``。

    语义：**平台接口返回热搜分数**，不得当作搜索人数 / 播放量 / 独立用户数，
    也不得进入播放增量计算。

    Args:
        raw: 接口原值，可能为 int / str / None / bool / float。

    Returns:
        Tuple[Optional[int], str]: ``(value, status)``。
            - 非负整数（或纯数字字符串）-> ``(int, 'ok')``，真实 0 视为合法；
            - 缺失（None）-> ``(None, 'missing')``，调用方**保留候选**、score 记 NULL；
            - bool / 负数 / 非数值类型 -> ``(None, 'invalid')``，调用方**跳过该条**，
              绝不写 0。

    说明：直接复用 ``core.data_quality.parse_count``，保证与 03 的质量口径一致。
    """
    return parse_count(raw)


def parse_optional_int(raw: Any) -> Tuple[Optional[int], str]:
    """解析可选整数类字段（legacy tid / tidv2 / pid_v2 / owner.mid）。

    Args:
        raw: 接口原值。

    Returns:
        Tuple[Optional[int], str]: ``(value, status)``，语义同 :func:`parse_count`；
        缺失记 ``missing``、非法记 ``invalid``，绝不补 0。
    """
    return parse_count(raw)


def _clean_str(raw: Any) -> Optional[str]:
    """把可选字符串字段规整为去空白后的字符串；非字符串 / 空白返回 None。

    Args:
        raw: 接口原值。

    Returns:
        Optional[str]: 去空白后的字符串，或 None。
    """
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    return None


# --------------------------------------------------------------------------
# DTO
# --------------------------------------------------------------------------

@dataclass
class BroadKeyword:
    """一条热搜关键词观测（``search/square``）。

    Attributes:
        keyword: 关键词，已去首尾空白，非空。
        heat_score: 平台接口返回热搜分数；缺失时为 None（SQL 写 NULL）。
        heat_status: ``ok`` / ``missing``（非法值不会进入 DTO，已在解析层跳过）。
        rank: 列表内名次，从 1 开始。
        source: 固定 ``search_square``。
        captured_epoch_s: 该观测对应的 UTC 秒级时间戳。
        show_name: 可选展示名。
    """

    keyword: str
    heat_score: Optional[int]
    heat_status: str
    rank: Optional[int]
    source: str = SOURCE_SEARCH_SQUARE
    captured_epoch_s: int = 0
    show_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的纯字典。"""
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass
class BroadVideo:
    """一条视频发现候选（``popular`` / ``ranking_all`` / ``ranking_all_others``）。

    质量字段一律带 ``*_status``（``ok`` / ``missing`` / ``invalid``），未知值保持 None，
    **不从无证据处补成已验证值**。

    Attributes:
        bvid: 视频 BV 号，非空（身份键）。
        source: 本条的发现来源。
        captured_epoch_s: 观测时刻（UTC 秒）。
        position: 列表内名次，从 1 开始；``others`` 条目可为 None。
        legacy_tid: 兼容用旧分类 id（与 tidv2 / pid_v2 分属不同层级，不合并）。
        legacy_tid_status: legacy tid 的质量状态。
        tidv2: v2 分类 id。
        tidv2_status: tidv2 的质量状态。
        pid_v2: v2 父分类 id。
        pid_v2_status: pid_v2 的质量状态。
        tname: 旧分类名。
        tnamev2: v2 分类名。
        owner_mid: UP 主 mid。
        owner_status: owner_mid 的质量状态。
        view: 接口返回播放量（仅作发现，不得当账号成绩）。
        view_status: view 的质量状态。
        rcmd_reason: 推荐理由（popular 特有，可空）。
    """

    bvid: str
    source: str
    captured_epoch_s: int
    position: Optional[int] = None
    legacy_tid: Optional[int] = None
    legacy_tid_status: str = QUALITY_MISSING
    tidv2: Optional[int] = None
    tidv2_status: str = QUALITY_MISSING
    pid_v2: Optional[int] = None
    pid_v2_status: str = QUALITY_MISSING
    tname: Optional[str] = None
    tnamev2: Optional[str] = None
    owner_mid: Optional[int] = None
    owner_status: str = QUALITY_MISSING
    view: Optional[int] = None
    view_status: str = QUALITY_MISSING
    rcmd_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的纯字典。"""
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass
class ParseOutcome:
    """一个来源一轮解析的结果（供 service 组装快照）。

    Attributes:
        state: ``ok`` / ``partial`` / ``error``。
        items: 解析出的 DTO 列表（已过滤非法条目）。
        returned_count: 接口原始返回条数（未过滤，用于识别「真空榜」）。
        error_code: 非零业务码；无则 None。
        reason: 失败 / 部分失败原因短码（便于诊断）。
        others_count: ranking ``others`` 原始条数（其余来源为 0）。
    """

    state: str
    items: List[Any] = field(default_factory=list)
    returned_count: int = 0
    error_code: Optional[int] = None
    reason: Optional[str] = None
    others_count: int = 0

    def to_summary(self) -> Dict[str, Any]:
        """转成快照用的精简摘要（不含逐条明细）。"""
        return {
            "state": self.state,
            "item_count": len(self.items),
            "returned_count": self.returned_count,
            "error_code": self.error_code,
            "reason": self.reason,
            "others_count": self.others_count,
        }


# --------------------------------------------------------------------------
# 合并（按 bvid 保身份，来源全留，展示值按固定优先级）
# --------------------------------------------------------------------------

def merge_video_candidates(items: Iterable[BroadVideo]) -> List[Dict[str, Any]]:
    """按 ``bvid`` 合并视频候选，发现来源全部保留，展示值按固定优先级取值。

    规则（硬约束 1）：
        - 身份按 ``bvid`` 去重；同一 ``bvid`` 的每条来源全部记入 ``source_details``；
        - 原始统计保留「来源 + 观测时间」（``source_details`` 每项都带 captured_epoch_s）；
        - 展示值取来源优先级最高的一条；同优先级取观测时间最早的一条，保证确定性；
          **绝不按 max 播放量挑边**；
        - 多来源展示字段（view / legacy_tid / pid_v2 / tidv2）不一致 -> ``conflict=True``；
        - 未知字段保持 None（不从其他来源补齐成已验证值）。

    Args:
        items: BroadVideo 可迭代对象（可含跨来源重复 bvid）。

    Returns:
        List[Dict[str, Any]]: 按 bvid 升序稳定排序的合并结果（dict 列表）。
    """
    groups: Dict[str, List[BroadVideo]] = {}
    for item in items:
        if not isinstance(item, BroadVideo):
            continue
        groups.setdefault(item.bvid, []).append(item)

    merged: List[Dict[str, Any]] = []
    for bvid in sorted(groups):
        entries = sorted(
            groups[bvid],
            key=lambda row: (source_priority(row.source), row.captured_epoch_s, row.position or 0),
        )
        display = entries[0]
        signatures = {
            (row.view, row.legacy_tid, row.pid_v2, row.tidv2) for row in entries
        }
        merged.append(
            {
                "bvid": bvid,
                "display_source": display.source,
                "conflict": len(signatures) > 1,
                "sources": [row.source for row in entries],
                "source_details": [
                    {
                        "source": row.source,
                        "captured_epoch_s": row.captured_epoch_s,
                        "position": row.position,
                        "view": row.view,
                        "view_status": row.view_status,
                        "legacy_tid": row.legacy_tid,
                        "legacy_tid_status": row.legacy_tid_status,
                        "tidv2": row.tidv2,
                        "tidv2_status": row.tidv2_status,
                        "pid_v2": row.pid_v2,
                        "pid_v2_status": row.pid_v2_status,
                        "owner_mid": row.owner_mid,
                        "owner_status": row.owner_status,
                    }
                    for row in entries
                ],
                # 展示字段只取优先级最高的一条，缺失保持 None（不跨来源补值）。
                "legacy_tid": display.legacy_tid,
                "legacy_tid_status": display.legacy_tid_status,
                "tidv2": display.tidv2,
                "tidv2_status": display.tidv2_status,
                "pid_v2": display.pid_v2,
                "pid_v2_status": display.pid_v2_status,
                "tname": display.tname,
                "tnamev2": display.tnamev2,
                "owner_mid": display.owner_mid,
                "owner_status": display.owner_status,
                "view": display.view,
                "view_status": display.view_status,
                "rcmd_reason": display.rcmd_reason,
                "captured_epoch_s": display.captured_epoch_s,
            }
        )
    return merged
