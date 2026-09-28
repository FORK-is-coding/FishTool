"""抽奖候选字段标准化工具。"""

from datetime import datetime
from typing import Any, Dict, Optional

from core.database import Comment


def _vip_present(vip: Any) -> bool:
    """判断会员对象是否携带可解释字段（空 dict 视为未知，不是非会员）。"""
    return isinstance(vip, dict) and any(
        key in vip for key in ("vipType", "type", "vipStatus", "status")
    )


def _optional_int(value: Any) -> Optional[int]:
    """把明确存在的数值转为整数，缺失/布尔/非法值保持未知。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def comment_row_to_dict(row: Comment) -> Dict[str, Any]:
    """将评论 ORM 记录转换为抽奖候选字段。

    Args:
        row: 评论数据库记录。

    Returns:
        包含等级、会员状态和评论时间的候选字典。
    """
    level_info = row.level_info if isinstance(row.level_info, dict) and row.level_info else None
    vip = row.vip if _vip_present(row.vip) else None
    if vip is not None:
        vip_type = int(vip.get("vipType") or vip.get("type") or 0)
        vip_status = int(vip.get("vipStatus") or vip.get("status") or 0)
        is_vip = bool(vip_status or vip_type)
        vip_label = _vip_label(vip_status, vip_type)
    else:
        vip_type = None
        is_vip = None
        vip_label = None
    return {
        "rpid": row.rpid,
        "uid": row.uid,
        "uname": row.uname,
        "level_info": level_info,
        "vip": vip,
        "level": level_info.get("current_level") if level_info else None,
        "is_vip": is_vip,
        "vip_type": vip_type,
        "vip_label": vip_label,
        "content": row.content,
        "ctime": row.ctime.isoformat() if row.ctime else None,
        "like": row.like or 0,
        "reply_count": row.reply_count or 0,
    }


def parse_reply(reply: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """清洗 B 站评论对象，只保留抽奖所需字段。

    Args:
        reply: B 站评论接口返回的单条评论。

    Returns:
        标准候选字典；缺少 UID 时返回空。
    """
    member = reply.get("member") or {}
    uid = member.get("mid")
    if not uid:
        return None

    content = reply.get("content") or {}
    raw_level_info = member.get("level_info")
    level_info = raw_level_info if isinstance(raw_level_info, dict) and raw_level_info else None
    raw_vip = member.get("vip")
    vip = raw_vip if _vip_present(raw_vip) else None
    if vip is not None:
        vip_type = int(vip.get("vipType") or vip.get("type") or 0)
        vip_status = int(vip.get("vipStatus") or vip.get("status") or 0)
        is_vip = bool(vip_status or vip_type)
        vip_label = _vip_label(vip_status, vip_type)
    else:
        vip_type = None
        is_vip = None
        vip_label = None
    # 缺 ctime / 非法 ctime 保持 None，不由 0 伪造 Unix 纪元时间。
    ctime = None
    raw_ctime = reply.get("ctime")
    if isinstance(raw_ctime, (int, float)) and not isinstance(raw_ctime, bool) and raw_ctime:
        try:
            ctime = datetime.fromtimestamp(int(raw_ctime)).isoformat()
        except (OSError, OverflowError, ValueError):
            ctime = None
    return {
        "rpid": str(reply.get("rpid_str") or reply.get("rpid") or ""),
        "uid": int(uid),
        "uname": str(member.get("uname") or f"UID {uid}"),
        "level_info": level_info,
        "vip": vip,
        "level": level_info.get("current_level") if level_info else None,
        "is_vip": is_vip,
        "vip_type": vip_type,
        "vip_label": vip_label,
        "content": str(content.get("message") or ""),
        "ctime": ctime,
        "like": int(reply.get("like") or 0),
        "reply_count": int(reply.get("rcount") or 0),
    }


def _vip_label(vip_status: int, vip_type: int) -> str:
    """根据会员状态生成稳定展示标签。

    Args:
        vip_status: 会员启用状态。
        vip_type: 会员类型，2 表示年度大会员。

    Returns:
        中文会员标签。
    """
    if vip_type == 2:
        return "年度大会员"
    return "大会员" if vip_status or vip_type else "非会员"


def profile_from_user_info(
    info: Dict[str, Any], meta: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """从用户资料响应构造候选补全所需的最小画像。

    Args:
        info: B 站用户资料 data 字段。
        meta: 可选上游 wrapper ``_meta``；存在时按其 ``field_status`` 判定字段
            是否真实可用，避免兼容用的 level=0 覆盖 missing（规格 §3.2/§6.6）。

    Returns:
        等级、会员状态和更新时间组成的画像；无法确认的字段为 None。
    """
    field_status = (
        meta.get("field_status")
        if isinstance(meta, dict) and isinstance(meta.get("field_status"), dict)
        else {}
    )

    def _field_ok(key: str, present: bool) -> bool:
        """字段是否真实可用：有 meta 时以 meta 为准，否则回退到“是否存在”。"""
        state = field_status.get(key)
        if state is not None:
            return state == "ok"
        return present

    raw_vip = info.get("vip")
    vip = raw_vip if _vip_present(raw_vip) else None
    if vip is not None and _field_ok("is_vip", True):
        vip_type = int(vip.get("type") or vip.get("vipType") or 0)
        vip_status = int(vip.get("status") or vip.get("vipStatus") or 0)
        is_vip = bool(vip_status or vip_type)
        vip_label = _vip_label(vip_status, vip_type)
    else:
        vip = None
        vip_type = None
        is_vip = None
        vip_label = None
    return {
        "level": _optional_int(info.get("level")) if _field_ok("level", "level" in info) else None,
        "vip": vip,
        "is_vip": is_vip,
        "vip_type": vip_type,
        "vip_label": vip_label,
        "updated_at": datetime.now().isoformat(),
    }


def missing_metadata_uids(comments: list[Dict[str, Any]]) -> set[int]:
    """找出缺少抽奖筛选元数据的候选 UID。

    Args:
        comments: 标准候选评论列表。

    Returns:
        需要补全画像的 UID 集合。
    """
    return {
        int(item.get("uid") or 0)
        for item in comments
        if item.get("uid") and (
            item.get("level") is None
            or item.get("is_vip") is None
            or item.get("vip_type") is None
            or not item.get("vip_label")
        )
    }


def merge_profile_metadata(
    comments: list[Dict[str, Any]],
    profiles: Dict[int, Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """将画像字段填入评论副本，不覆盖已有真实值。

    Args:
        comments: 原始候选评论列表。
        profiles: UID 到补全画像的映射。

    Returns:
        合并后的新候选列表。
    """
    normalized = [dict(item) for item in comments]
    for item in normalized:
        profile = profiles.get(int(item.get("uid") or 0), {})
        for field in ("level", "is_vip", "vip_type"):
            if item.get(field) is None:
                item[field] = profile.get(field)
        # 会员标签优先用画像显式标签；否则仅在 is_vip 明确已知时派生，
        # 未知（None）不得由 False 回退伪造“非会员”。
        if not item.get("vip_label"):
            label = profile.get("vip_label")
            if not label:
                if item.get("is_vip") is True:
                    label = "大会员"
                elif item.get("is_vip") is False:
                    label = "非会员"
            item["vip_label"] = label
    return normalized


def unresolved_metadata_uids(comments: list[Dict[str, Any]]) -> list[int]:
    """列出仍缺少等级、会员或评论时间的候选 UID。

    Args:
        comments: 已执行画像合并的候选列表。

    Returns:
        未能满足抽奖筛选条件的 UID 列表。
    """
    return [
        int(item.get("uid") or 0)
        for item in comments
        if item.get("level") is None
        or item.get("is_vip") is None
        or not item.get("ctime")
    ]


def parse_comment_time(value: Any) -> Optional[datetime]:
    """把候选中的时间戳或 ISO 字符串转换为 datetime。

    Args:
        value: datetime、时间戳、ISO 字符串或空值。

    Returns:
        无时区 datetime；无法解析时返回空。
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def build_candidate_pool(
    comments: list[Dict[str, Any]],
    unique_users: bool,
    vip_only: bool,
    min_level: Optional[int],
    date_start: Optional[datetime],
    date_end: Optional[datetime],
) -> tuple[list[Dict[str, Any]], Dict[str, int]]:
    """应用日期、等级、会员和 UID 去重规则生成候选池。

    Args:
        comments: 元数据完整的候选评论。
        unique_users: 是否按 UID 去重。
        vip_only: 是否仅保留大会员。
        min_level: 最低账号等级。
        date_start: 评论时间下界。
        date_end: 评论时间上界。

    Returns:
        候选池与各规则排除数量。
    """
    pool: list[Dict[str, Any]] = []
    seen_users: set[int] = set()
    excluded = {"vip": 0, "level": 0, "date": 0, "duplicate": 0}
    for comment in comments:
        uid = int(comment.get("uid") or 0)
        if not uid:
            continue
        comment_time = parse_comment_time(comment.get("ctime"))
        if (date_start and (not comment_time or comment_time < date_start)) or (
            date_end and (not comment_time or comment_time > date_end)
        ):
            excluded["date"] += 1
            continue
        level = comment.get("level")
        if min_level is not None and (level is None or int(level) < min_level):
            excluded["level"] += 1
            continue
        if vip_only and comment.get("is_vip") is not True:
            excluded["vip"] += 1
            continue
        if unique_users and uid in seen_users:
            excluded["duplicate"] += 1
            continue
        seen_users.add(uid)
        pool.append(comment)
    return pool, excluded
