"""抽奖候选字段标准化工具的契约级测试。

覆盖 modules/lottery/candidate.py 的全部公开函数与边界分支。
评论 ORM 对象使用真实 ``core.database.Comment`` 瞬时实例（不入库、不落盘）。
"""

from datetime import datetime, timedelta

import pytest

from core.database import Comment

from modules.lottery.candidate import (
    _vip_label,
    build_candidate_pool,
    comment_row_to_dict,
    merge_profile_metadata,
    missing_metadata_uids,
    parse_comment_time,
    parse_reply,
    profile_from_user_info,
    unresolved_metadata_uids,
)


# ------------------------------------------------------------------ 构造辅助

def _make_comment(**overrides) -> Comment:
    """构造一个未持久化的真实 Comment ORM 实例，便于字段契约断言。"""
    row = Comment()
    row.rpid = overrides.pop("rpid", "1001")
    row.uid = overrides.pop("uid", 42)
    row.uname = overrides.pop("uname", "测试用户")
    row.content = overrides.pop("content", "抽奖留言")
    for field, value in overrides.items():
        setattr(row, field, value)
    return row


def _make_reply(**overrides) -> dict:
    """构造一条贴近 B 站评论接口返回结构的原始评论字典。"""
    reply = {
        "rpid": 999,
        "rpid_str": "999",
        "ctime": 1_700_000_000,
        "like": 3,
        "rcount": 2,
        "member": {
            "mid": "42",
            "uname": "评论者",
            "level_info": {"current_level": 6},
            "vip": {"vipType": 2, "vipStatus": 1},
        },
        "content": {"message": "我要参与"},
    }
    reply.update(overrides)
    return reply


# ------------------------------------------------- comment_row_to_dict

def test_comment_row_to_dict_keeps_unknowns_when_vip_and_level_missing() -> None:
    """vip 与 level_info 均缺失时应显式返回 None，供上层判断是否需要补全。"""
    row = _make_comment(ctime=datetime(2024, 5, 1, 12, 0, 0))

    payload = comment_row_to_dict(row)

    assert payload["rpid"] == "1001"
    assert payload["uid"] == 42
    assert payload["uname"] == "测试用户"
    assert payload["level_info"] is None
    assert payload["level"] is None
    assert payload["vip"] is None
    assert payload["is_vip"] is None
    assert payload["vip_type"] is None
    assert payload["vip_label"] is None
    assert payload["ctime"] == "2024-05-01T12:00:00"
    assert payload["like"] == 0
    assert payload["reply_count"] == 0


def test_comment_row_to_dict_resolves_vip_type_and_label() -> None:
    """有会员原始字段时应解析出年度大会员与等级。"""
    row = _make_comment(level_info={"current_level": 5}, vip={"vipType": 2, "vipStatus": 1})

    payload = comment_row_to_dict(row)

    assert payload["level"] == 5
    assert payload["is_vip"] is True
    assert payload["vip_type"] == 2
    assert payload["vip_label"] == "年度大会员"


def test_comment_row_to_dict_accepts_short_vip_keys() -> None:
    """兼容 type/status 简写键的旧数据。"""
    row = _make_comment(vip={"type": 1, "status": 1})

    payload = comment_row_to_dict(row)

    assert payload["vip_type"] == 1
    assert payload["is_vip"] is True
    assert payload["vip_label"] == "大会员"


def test_comment_row_to_dict_marks_empty_vip_as_non_member() -> None:
    """空会员对象属于"已知无会员"，应判为非会员而非未知。"""
    row = _make_comment(vip={})

    payload = comment_row_to_dict(row)

    assert payload["is_vip"] is False
    assert payload["vip_type"] == 0
    assert payload["vip_label"] == "非会员"
    assert payload["ctime"] is None


# ------------------------------------------------- parse_reply

def test_parse_reply_returns_none_when_mid_missing() -> None:
    """缺少 mid 的评论无法作为抽奖候选，应返回 None。"""
    assert parse_reply({"member": {}, "content": {"message": "x"}}) is None
    assert parse_reply({}) is None


def test_parse_reply_maps_public_fields() -> None:
    """完整评论应被清洗为抽奖所需的最小字段集。"""
    payload = parse_reply(_make_reply())

    assert payload["rpid"] == "999"
    assert payload["uid"] == 42
    assert payload["uname"] == "评论者"
    assert payload["level"] == 6
    assert payload["is_vip"] is True
    assert payload["vip_type"] == 2
    assert payload["vip_label"] == "年度大会员"
    assert payload["content"] == "我要参与"
    assert payload["like"] == 3
    assert payload["reply_count"] == 2
    assert payload["ctime"] == datetime.fromtimestamp(1_700_000_000).isoformat()


def test_parse_reply_falls_back_to_numeric_rpid_and_default_uname() -> None:
    """缺少 rpid_str 与 uname 时应回退到数字 rpid 与占位昵称。"""
    reply = _make_reply()
    reply.pop("rpid_str")
    reply["member"].pop("uname")

    payload = parse_reply(reply)

    assert payload["rpid"] == "999"
    assert payload["uname"] == "UID 42"


def test_parse_reply_tolerates_missing_optional_blocks() -> None:
    """缺少 content/vip/level_info/ctime 时使用安全默认值。"""
    reply = {"member": {"mid": "7"}}

    payload = parse_reply(reply)

    assert payload["rpid"] == ""
    assert payload["uid"] == 7
    assert payload["level_info"] is None
    assert payload["level"] is None
    assert payload["is_vip"] is False
    assert payload["vip_label"] == "非会员"
    assert payload["content"] == ""
    assert payload["ctime"] is None
    assert payload["like"] == 0
    assert payload["reply_count"] == 0


def test_parse_reply_accepts_string_and_int_like_fields() -> None:
    """like/rcount 以字符串返回时也应转成整数。"""
    payload = parse_reply(_make_reply(like="8", rcount="1"))

    assert payload["like"] == 8
    assert payload["reply_count"] == 1


# ------------------------------------------------- _vip_label

def test_vip_label_prefers_annual_member_by_type() -> None:
    """vip_type=2 时即便状态为 0 也应判为年度大会员。"""
    assert _vip_label(0, 2) == "年度大会员"
    assert _vip_label(1, 2) == "年度大会员"


def test_vip_label_distinguishes_member_and_non_member() -> None:
    """状态或类型任一为非零即为大会员，否则为非会员。"""
    assert _vip_label(1, 0) == "大会员"
    assert _vip_label(0, 1) == "大会员"
    assert _vip_label(0, 0) == "非会员"


# ------------------------------------------------- profile_from_user_info

def test_profile_from_user_info_reads_long_vip_keys_first() -> None:
    """用户资料接口使用 type/status 键，应优先生效。"""
    profile = profile_from_user_info(
        {"level": 6, "vip": {"type": 2, "status": 1, "vipType": 0, "vipStatus": 0}}
    )

    assert profile["level"] == 6
    assert profile["vip_type"] == 2
    assert profile["is_vip"] is True
    assert profile["vip_label"] == "年度大会员"


def test_profile_from_user_info_accepts_legacy_vip_keys() -> None:
    """仅提供 vipType/vipStatus 时应作为回退键生效。"""
    profile = profile_from_user_info({"vip": {"vipType": 1, "vipStatus": 1}})

    assert profile["vip_type"] == 1
    assert profile["is_vip"] is True
    assert profile["vip_label"] == "大会员"


def test_profile_from_user_info_defaults_level_and_stamps_time() -> None:
    """等级缺失时按 0 处理，并写入可解析的更新时间。"""
    profile = profile_from_user_info({})

    assert profile["level"] == 0
    assert profile["is_vip"] is False
    assert profile["vip_label"] == "非会员"
    # updated_at 必须是可被 parse_comment_time 解析的 ISO 字符串。
    assert isinstance(parse_comment_time(profile["updated_at"]), datetime)


# ------------------------------------------------- 缺失/未解决 UID 判定

def test_missing_metadata_uids_selects_only_incomplete_candidates() -> None:
    """只挑出等级、会员或标签缺失且 UID 有效的候选。"""
    comments = [
        {"uid": 1, "level": 6, "is_vip": True, "vip_type": 2, "vip_label": "年度大会员"},
        {"uid": 2, "level": None, "is_vip": True, "vip_type": 2, "vip_label": "年度大会员"},
        {"uid": 3, "level": 3, "is_vip": None, "vip_type": 0, "vip_label": "非会员"},
        {"uid": 4, "level": 3, "is_vip": False, "vip_type": None, "vip_label": "非会员"},
        {"uid": 5, "level": 3, "is_vip": False, "vip_type": 0, "vip_label": ""},
        {"uid": 0, "level": None, "is_vip": None, "vip_type": None, "vip_label": None},
        {"uid": None, "level": None, "is_vip": None, "vip_type": None, "vip_label": None},
    ]

    assert missing_metadata_uids(comments) == {2, 3, 4, 5}


def test_missing_metadata_uids_returns_empty_for_complete_pool() -> None:
    """元数据齐备时无需补全。"""
    comments = [{"uid": 8, "level": 6, "is_vip": False, "vip_type": 0, "vip_label": "非会员"}]

    assert missing_metadata_uids(comments) == set()


def test_unresolved_metadata_uids_flags_missing_level_vip_or_ctime() -> None:
    """仍缺等级、会员状态或评论时间的候选应被判定为未解决。"""
    comments = [
        {"uid": 1, "level": 6, "is_vip": True, "ctime": "2024-01-01T00:00:00"},
        {"uid": 2, "level": None, "is_vip": True, "ctime": "2024-01-01T00:00:00"},
        {"uid": 3, "level": 6, "is_vip": None, "ctime": "2024-01-01T00:00:00"},
        {"uid": 4, "level": 6, "is_vip": True, "ctime": None},
        {"uid": 0, "level": None, "is_vip": None, "ctime": None},
    ]

    assert unresolved_metadata_uids(comments) == [2, 3, 4, 0]


# ------------------------------------------------- merge_profile_metadata

def test_merge_profile_metadata_fills_only_missing_fields() -> None:
    """补全不得覆盖评论自带真实值，且不得修改原列表。"""
    comments = [
        {"uid": 42, "level": None, "is_vip": None, "vip_type": None, "vip_label": None},
        {"uid": 43, "level": 6, "is_vip": False, "vip_type": 0, "vip_label": "非会员"},
    ]
    profiles = {
        42: {"level": 5, "is_vip": True, "vip_type": 1, "vip_label": "大会员"},
        43: {"level": 1, "is_vip": True, "vip_type": 2, "vip_label": "年度大会员"},
    }

    merged = merge_profile_metadata(comments, profiles)

    assert merged[0]["level"] == 5
    assert merged[0]["vip_label"] == "大会员"
    # 已有真实值保持不变，同时原列表未被就地修改。
    assert merged[1]["level"] == 6
    assert merged[1]["vip_label"] == "非会员"
    assert comments[0]["level"] is None
    assert merged is not comments


def test_merge_profile_metadata_derives_label_from_is_vip() -> None:
    """画像缺标签时按补全后的 is_vip 推导标签。"""
    merged = merge_profile_metadata(
        [{"uid": 9, "level": None, "is_vip": None, "vip_type": None, "vip_label": None}],
        {9: {"level": 4, "is_vip": True, "vip_type": 1}},
    )

    assert merged[0]["vip_label"] == "大会员"


def test_merge_profile_metadata_defaults_to_non_member_label() -> None:
    """画像完全缺失时标签回退为非会员。"""
    merged = merge_profile_metadata([{"uid": 77}], {})

    assert merged[0]["level"] is None
    assert merged[0]["vip_label"] == "非会员"


# ------------------------------------------------- parse_comment_time

def test_parse_comment_time_passes_datetime_through() -> None:
    """datetime 输入的返回自身。"""
    value = datetime(2024, 3, 1)

    assert parse_comment_time(value) is value


def test_parse_comment_time_accepts_numeric_values() -> None:
    """整数与浮点时间戳都能转换。"""
    assert parse_comment_time(0) == datetime.fromtimestamp(0)
    assert parse_comment_time(1_700_000_000.5) == datetime.fromtimestamp(1_700_000_000.5)


def test_parse_comment_time_accepts_iso_strings() -> None:
    """支持 ISO 字符串与带 Z 的 UTC 字符串，并去掉时区信息。"""
    assert parse_comment_time("2024-05-01T08:00:00") == datetime(2024, 5, 1, 8, 0, 0)
    parsed = parse_comment_time("2024-05-01T08:00:00Z")
    assert parsed == datetime(2024, 5, 1, 8, 0, 0)
    assert parsed.tzinfo is None


def test_parse_comment_time_returns_none_for_unusable_input() -> None:
    """空值、非法字符串与越界时间戳统一返回 None。"""
    assert parse_comment_time(None) is None
    assert parse_comment_time("") is None
    assert parse_comment_time("昨天") is None
    assert parse_comment_time(10 ** 18) is None


# ------------------------------------------------- build_candidate_pool

def _candidate(uid: int, level: int, is_vip: bool, ctime: str) -> dict:
    """构造一条元数据完整的候选评论。"""
    return {"uid": uid, "level": level, "is_vip": is_vip, "ctime": ctime}


def test_build_candidate_pool_keeps_all_when_no_filter() -> None:
    """无任何筛选条件时全部入选且排除计数为 0。"""
    comments = [
        _candidate(1, 6, True, "2024-05-01T00:00:00"),
        _candidate(2, 1, False, "2024-05-02T00:00:00"),
    ]

    pool, excluded = build_candidate_pool(comments, False, False, None, None, None)

    assert len(pool) == 2
    assert excluded == {"vip": 0, "level": 0, "date": 0, "duplicate": 0}


def test_build_candidate_pool_applies_date_window() -> None:
    """超出时间窗或时间缺失的候选记入 date 排除。"""
    start = datetime(2024, 5, 1)
    end = datetime(2024, 5, 10)
    comments = [
        _candidate(1, 6, True, "2024-04-30T23:59:59"),
        _candidate(2, 6, True, "2024-05-05T00:00:00"),
        _candidate(3, 6, True, "2024-05-11T00:00:00"),
        {"uid": 4, "level": 6, "is_vip": True, "ctime": None},
    ]

    pool, excluded = build_candidate_pool(comments, False, False, None, start, end)

    assert [item["uid"] for item in pool] == [2]
    assert excluded["date"] == 3


def test_build_candidate_pool_enforces_min_level() -> None:
    """低于最低等级的候选记入 level 排除，缺失等级同样被排除。"""
    comments = [
        _candidate(1, 2, True, "2024-05-01T00:00:00"),
        _candidate(2, 5, True, "2024-05-01T00:00:00"),
        {"uid": 3, "level": None, "is_vip": True, "ctime": "2024-05-01T00:00:00"},
    ]

    pool, excluded = build_candidate_pool(comments, False, False, 5, None, None)

    assert [item["uid"] for item in pool] == [2]
    assert excluded["level"] == 2


def test_build_candidate_pool_requires_vip_when_requested() -> None:
    """vip_only 时仅保留 is_vip is True 的候选。"""
    comments = [
        _candidate(1, 6, True, "2024-05-01T00:00:00"),
        _candidate(2, 6, False, "2024-05-01T00:00:00"),
        {"uid": 3, "level": 6, "is_vip": None, "ctime": "2024-05-01T00:00:00"},
    ]

    pool, excluded = build_candidate_pool(comments, False, True, None, None, None)

    assert [item["uid"] for item in pool] == [1]
    assert excluded["vip"] == 2


def test_build_candidate_pool_deduplicates_by_uid() -> None:
    """unique_users 时同一 UID 只保留首条，其余记入 duplicate。"""
    comments = [
        _candidate(1, 6, True, "2024-05-01T00:00:00"),
        _candidate(1, 6, True, "2024-05-02T00:00:00"),
        _candidate(2, 6, True, "2024-05-03T00:00:00"),
    ]

    pool, excluded = build_candidate_pool(comments, True, False, None, None, None)

    assert [item["uid"] for item in pool] == [1, 2]
    assert excluded["duplicate"] == 1


def test_build_candidate_pool_skips_invalid_uid_silently() -> None:
    """UID 为空或 0 的候选直接丢弃，不计入任何排除统计。"""
    comments = [
        {"uid": 0, "level": 6, "is_vip": True, "ctime": "2024-05-01T00:00:00"},
        {"uid": None, "level": 6, "is_vip": True, "ctime": "2024-05-01T00:00:00"},
        _candidate(5, 6, True, "2024-05-01T00:00:00"),
    ]

    pool, excluded = build_candidate_pool(comments, True, True, 3, None, None)

    assert [item["uid"] for item in pool] == [5]
    assert excluded == {"vip": 0, "level": 0, "date": 0, "duplicate": 0}


def test_build_candidate_pool_counts_each_candidate_once() -> None:
    """多条件同时命中时只按首个命中规则计数，保证统计不重复。"""
    start = datetime(2024, 5, 1)
    comments = [
        # 同时违反日期与等级，应只记入 date。
        {"uid": 1, "level": 1, "is_vip": False, "ctime": "2024-01-01T00:00:00"},
    ]

    pool, excluded = build_candidate_pool(comments, False, True, 6, start, None)

    assert pool == []
    assert excluded == {"vip": 0, "level": 0, "date": 1, "duplicate": 0}


def test_build_candidate_pool_handles_datetime_objects_for_window() -> None:
    """ctime 已是 datetime 时同样参与时间窗过滤。"""
    base = datetime(2024, 5, 5, 12, 0, 0)
    comments = [{"uid": 1, "level": 6, "is_vip": True, "ctime": base}]

    pool, excluded = build_candidate_pool(
        comments, False, False, None, base - timedelta(days=1), base + timedelta(days=1)
    )

    assert [item["uid"] for item in pool] == [1]
    assert excluded["date"] == 0
