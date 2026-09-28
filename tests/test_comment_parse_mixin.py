"""评论解析 Mixin 契约测试（modules/comment/collector/parse_mixin.py）。

覆盖：
- _parse_comment_reply：单条评论标准化字段与缺省兜底
- _parse_comment_replies：批量解析与空项过滤

测试策略：
- 使用真实 CommentCollector（仅注入契约级假 API 以满足构造依赖），解析链路零网络；
- 非法时间戳等异常分支从外部用 pytest.raises 断言，固化当前契约。
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from modules.comment.collector import CommentCollector


def build_parser() -> CommentCollector:
    """构造只用于解析的真实采集器实例。"""
    return CommentCollector(api=SimpleNamespace())


def make_raw_reply(**overrides) -> dict:
    """构造一份完整的 B站评论接口原始响应。"""
    reply = {
        "rpid": 123456,
        "oid": 999,
        "member": {
            "mid": 777,
            "uname": "评论者",
            "avatar": "https://avatar.example/a.png",
            "level_info": {"current_level": 5},
            "vip": {"vipStatus": 1, "vipType": 2},
        },
        "content": {"message": "评论正文"},
        "ctime": 1766400000,
        "like": 12,
        "rcount": 3,
    }
    reply.update(overrides)
    return reply


# ---------------------------------------------------------------------------
# _parse_comment_reply
# ---------------------------------------------------------------------------


def test_parse_comment_reply_maps_all_contract_fields():
    """完整响应应映射出下游依赖的全部标准字段。"""
    before = datetime.now()

    parsed = build_parser()._parse_comment_reply(make_raw_reply(), is_hot=True)

    assert parsed["rpid"] == 123456
    assert parsed["oid"] == 999
    assert parsed["uid"] == 777
    assert parsed["uname"] == "评论者"
    assert parsed["avatar"] == "https://avatar.example/a.png"
    assert parsed["level_info"] == {"current_level": 5}
    assert parsed["level"] == 5
    assert parsed["is_vip"] is True
    assert parsed["vip_type"] == 2
    assert parsed["content"] == "评论正文"
    assert parsed["ctime"] == datetime.fromtimestamp(1766400000)
    assert parsed["like"] == 12
    assert parsed["reply_count"] == 3
    assert parsed["is_hot"] is True
    assert isinstance(parsed["fetched_at"], datetime)
    assert parsed["fetched_at"] >= before


def test_parse_comment_reply_marks_non_hot_by_default():
    """默认 is_hot 为 False，普通评论不能被误标成热门。"""
    parsed = build_parser()._parse_comment_reply(make_raw_reply())

    assert parsed["is_hot"] is False


def test_parse_comment_reply_keeps_missing_vip_and_ctime_unknown():
    """反例修复（规格 §6.6）：空响应体缺会员/时间时保持未知，不伪造值。

    旧预期把 level_info/vip 归零为 {}、is_vip=False、vip_type=0、ctime 落到
    Unix 纪元，等于在最早入口给“未采集”盖上“非会员 + 1970-01-01”的假事实，
    会让下游把未知用户当已知。新预期这些字段为 None，其余结构默认值不变。
    """
    parsed = build_parser()._parse_comment_reply({})

    assert parsed["rpid"] is None
    assert parsed["oid"] is None
    assert parsed["uid"] is None
    assert parsed["uname"] == ""
    assert parsed["avatar"] == ""
    assert parsed["level_info"] is None
    assert parsed["vip"] is None
    assert parsed["level"] is None
    assert parsed["is_vip"] is None
    assert parsed["vip_type"] is None
    assert parsed["content"] == ""
    assert parsed["ctime"] is None
    assert parsed["like"] == 0
    assert parsed["reply_count"] == 0


def test_parse_comment_reply_tolerates_null_nested_objects():
    """member/content 子对象为 None 时保持未知，不伪造非会员。"""
    parsed = build_parser()._parse_comment_reply(
        {"rpid": 1, "member": {"mid": 9, "level_info": None, "vip": None}, "content": {}, "ctime": 1}
    )

    assert parsed["uid"] == 9
    assert parsed["level"] is None
    # 反例（规格 §6.6）：vip 为 None 是未知，不是已知非会员。
    assert parsed["is_vip"] is None
    assert parsed["vip_type"] is None
    assert parsed["content"] == ""


def test_parse_comment_reply_detects_vip_via_vip_type_only():
    """只有 vipType 没有 vipStatus 时同样应识别为大会员。"""
    parsed = build_parser()._parse_comment_reply(
        make_raw_reply(member={"mid": 1, "vip": {"vipType": 2}})
    )

    assert parsed["is_vip"] is True
    assert parsed["vip_type"] == 2


def test_parse_comment_reply_reads_vip_type_from_legacy_field():
    """兼容旧字段 vip.type：只影响 vip_type，不参与 is_vip 判定（固化现状的不对称）。"""
    parsed = build_parser()._parse_comment_reply(
        make_raw_reply(member={"mid": 1, "vip": {"type": 1}})
    )

    assert parsed["vip_type"] == 1
    # is_vip 只看 vipStatus/vipType；仅有旧字段 type 时仍是 False。
    assert parsed["is_vip"] is False


def test_parse_comment_reply_keeps_partial_fields_when_missing():
    """仅提供部分字段时其余字段独立兜底。"""
    parsed = build_parser()._parse_comment_reply({"rpid": 5, "ctime": 100})

    assert parsed["rpid"] == 5
    assert parsed["like"] == 0
    assert parsed["reply_count"] == 0
    assert parsed["uname"] == ""


def test_parse_comment_reply_returns_none_for_non_numeric_ctime():
    """反例修复（规格 §6.6）：ctime 为 None/非法时返回 None，不再抛 TypeError。

    旧契约用 fromtimestamp(ctime) 直接抛错，一条坏时间戳会炸掉整批解析，
    且缺失时间被当成真实采集事实。新契约缺失/非法时间保持 None，交由下游按未知处理。
    """
    parser = build_parser()

    assert parser._parse_comment_reply({"rpid": 1, "ctime": None})["ctime"] is None
    assert parser._parse_comment_reply({"rpid": 1, "ctime": "not-a-timestamp"})["ctime"] is None


# ---------------------------------------------------------------------------
# _parse_comment_replies
# ---------------------------------------------------------------------------


def test_parse_comment_replies_filters_falsy_items_and_keeps_order():
    """批量解析应跳过空项并保持原顺序。"""
    replies = [make_raw_reply(rpid=1), {}, None, make_raw_reply(rpid=2)]

    parsed = build_parser()._parse_comment_replies(replies)

    assert [item["rpid"] for item in parsed] == [1, 2]


def test_parse_comment_replies_propagates_hot_flag():
    """批量解析把 is_hot 透传给每条评论。"""
    parsed = build_parser()._parse_comment_replies([make_raw_reply(rpid=1)], is_hot=True)

    assert parsed[0]["is_hot"] is True


def test_parse_comment_replies_on_empty_input_returns_empty_list():
    """空列表或全空项应返回空列表。"""
    parser = build_parser()

    assert parser._parse_comment_replies([]) == []
    assert parser._parse_comment_replies([None, {}, 0, ""]) == []


def test_parse_comment_replies_keeps_bad_ctime_item_as_unknown():
    """反例修复（规格 §6.6）：一条坏 ctime 不再炸掉整批，缺失时间保持 None。"""
    replies = [make_raw_reply(rpid=1), {"rpid": 2, "ctime": None}]

    parsed = build_parser()._parse_comment_replies(replies)

    assert [item["rpid"] for item in parsed] == [1, 2]
    assert parsed[1]["ctime"] is None
