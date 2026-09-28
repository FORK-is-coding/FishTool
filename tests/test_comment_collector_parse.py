"""评论采集器评论解析 Mixin 的契约级测试。

覆盖 modules/comment/collector/parse_mixin.py：
- _parse_comment_replies：批量解析与空项过滤
- _parse_comment_reply：单条标准化（rpid/oid/uid/uname/avatar/level/vip/content/ctime/like/reply_count）

纯字典转换，无网络、无落库。
"""

from __future__ import annotations

from datetime import datetime

from modules.comment.collector.parse_mixin import CommentParseMixin


def _mix() -> CommentParseMixin:
    """构造可直接调用的解析 Mixin 实例。"""
    return CommentParseMixin()


def _full_reply() -> dict:
    """构造一条字段完整的 B 站原始评论。"""
    return {
        "rpid": 1001,
        "oid": 555,
        "member": {
            "mid": 42,
            "uname": "用户甲",
            "avatar": "https://avatar",
            "level_info": {"current_level": 5},
            "vip": {"vipStatus": 1, "vipType": 2},
        },
        "content": {"message": "评论内容"},
        "ctime": 1700000000,
        "like": 7,
        "rcount": 3,
    }


# --------------------------------------------------------------------- 单条解析

def test_parse_comment_reply_maps_all_fields() -> None:
    """完整原始评论应逐字段标准化。"""
    comment = _mix()._parse_comment_reply(_full_reply(), is_hot=True)

    assert comment["rpid"] == 1001
    assert comment["oid"] == 555
    assert comment["uid"] == 42
    assert comment["uname"] == "用户甲"
    assert comment["avatar"] == "https://avatar"
    assert comment["level_info"] == {"current_level": 5}
    assert comment["vip"] == {"vipStatus": 1, "vipType": 2}
    assert comment["level"] == 5
    assert comment["is_vip"] is True
    assert comment["vip_type"] == 2
    assert comment["content"] == "评论内容"
    assert comment["ctime"] == datetime.fromtimestamp(1700000000)
    assert comment["like"] == 7
    assert comment["reply_count"] == 3
    assert comment["is_hot"] is True
    assert isinstance(comment["fetched_at"], datetime)


def test_parse_comment_reply_defaults_for_missing_member() -> None:
    """反例修复（规格 §6.6）：缺 member 时会员/等级保持未知，不伪造非会员。"""
    comment = _mix()._parse_comment_reply({"rpid": 1, "content": {"message": "x"}})

    assert comment["uid"] is None
    assert comment["uname"] == ""
    assert comment["avatar"] == ""
    assert comment["level_info"] is None
    assert comment["vip"] is None
    assert comment["level"] is None
    assert comment["is_vip"] is None
    assert comment["vip_type"] is None


def test_parse_comment_reply_defaults_for_missing_content_and_stats() -> None:
    """反例修复（规格 §6.6）：缺 ctime 保持 None，不落到 Unix 纪元。"""
    comment = _mix()._parse_comment_reply({"rpid": 2, "member": {}})

    assert comment["content"] == ""
    assert comment["like"] == 0
    assert comment["reply_count"] == 0
    assert comment["ctime"] is None
    assert comment["is_hot"] is False


def test_parse_comment_reply_handles_null_member_subfields() -> None:
    """反例修复（规格 §6.6）：level_info/vip 为 None 时保持未知，不降级为 {}。"""
    reply = {"rpid": 3, "member": {"mid": 9, "level_info": None, "vip": None}}

    comment = _mix()._parse_comment_reply(reply)

    assert comment["uid"] == 9
    assert comment["level_info"] is None
    assert comment["vip"] is None
    assert comment["level"] is None
    assert comment["vip_type"] is None


def test_parse_comment_reply_vip_type_via_type_alias() -> None:
    """vip 只给 type 字段时应能读出会员类型；is_vip 只看 vipStatus/vipType。"""
    reply = {"rpid": 4, "member": {"vip": {"type": 1}}}

    comment = _mix()._parse_comment_reply(reply)

    assert comment["vip_type"] == 1
    assert comment["is_vip"] is False


def test_parse_comment_reply_vip_type_string_is_coerced() -> None:
    """vipType 为字符串时应强制转成整数。"""
    reply = {"rpid": 5, "member": {"vip": {"vipType": "2"}}}

    comment = _mix()._parse_comment_reply(reply)

    assert comment["vip_type"] == 2


def test_parse_comment_reply_non_vip_member() -> None:
    """普通用户应标记为非大会员且类型为 0。"""
    reply = {"rpid": 6, "member": {"vip": {"vipStatus": 0, "vipType": 0}}}

    comment = _mix()._parse_comment_reply(reply)

    assert comment["is_vip"] is False
    assert comment["vip_type"] == 0


# --------------------------------------------------------------------- 批量解析

def test_parse_comment_replies_filters_falsy_items() -> None:
    """批量解析应过滤 None/空字典等假值。"""
    replies = [_full_reply(), None, {}, 0]

    comments = _mix()._parse_comment_replies(replies, is_hot=False)

    assert len(comments) == 1
    assert comments[0]["rpid"] == 1001
    assert comments[0]["is_hot"] is False


def test_parse_comment_replies_empty_list() -> None:
    """空列表返回空列表。"""
    assert _mix()._parse_comment_replies([]) == []


def test_parse_comment_replies_marks_hot_flag() -> None:
    """is_hot 标记应透传到每条解析结果。"""
    replies = [{"rpid": 1}, {"rpid": 2}]

    comments = _mix()._parse_comment_replies(replies, is_hot=True)

    assert [item["is_hot"] for item in comments] == [True, True]
