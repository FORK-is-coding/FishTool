"""AI 伪人巡检评论采集器：分页、上限、楼中楼、字段解析、错误分流。"""
import asyncio
import json

import pytest

from core.exceptions import BilibiliAPIError, CookieExpiredError, NetworkError
from modules.lottery.patrol.collector import PatrolCollector, ScanCancelled, VideoMeta, parse_reply
from modules.lottery.patrol.contracts import build_settings

BASE = "https://api.bilibili.com"


def _reply(rpid, mid=None, *, rcount=0, sub=None, root=0, parent=0, text=None):
    return {
        "rpid": rpid, "root": root, "parent": parent, "ctime": 1762000000 + rpid, "like": 1, "rcount": rcount,
        "member": {"mid": mid or rpid + 1000, "uname": f"u{rpid}", "sign": "签名",
                   "level_info": {"current_level": 3}, "vip": {"vipStatus": 0},
                   "official_verify": {"type": -1}},
        "content": {"message": text or f"评论{rpid}", "members": []},
        "reply_control": {"location": "IP属地：上海"},
        "replies": sub or [],
    }


class _FakeApi:
    BASE_URL = BASE

    def __init__(self, pages=None, reply_pages=None, errors=None, meta=None):
        self.pages = list(pages or [])
        self.reply_pages = reply_pages or {}
        self.errors = list(errors or [])
        self.meta = meta
        self.calls = []

    async def get(self, url, params=None, need_sign=False, **kw):
        self.calls.append((url, dict(params or {}), need_sign))
        if url.endswith("/x/web-interface/view"):
            return self.meta
        if url.endswith("/x/v2/reply/reply"):
            return self.reply_pages.get((params["root"], params["pn"]), {"replies": [], "page": {"count": 0}})
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        return self.pages.pop(0) if self.pages else {"replies": [], "cursor": {"is_end": True}}


def _page(rpids, offset, *, is_end=False, rcount=0):
    return {"replies": [_reply(r, rcount=rcount) for r in rpids],
            "cursor": {"is_end": is_end, "pagination_reply": {"next_offset": offset}}}


META = VideoMeta("BV1xx411c7mD", 123, "标题", 9, "UP", 50)
S = build_settings({})


def _collect(api, **kw):
    kw.setdefault("max_comments", 500)
    kw.setdefault("include_replies", False)
    return asyncio.run(PatrolCollector(api, S).collect(META, **kw))


def test_pages_until_is_end():
    api = _FakeApi(pages=[_page([1, 2], "a"), _page([3, 4], "b"), _page([5], "c", is_end=True)])
    res = _collect(api)
    assert [c["rpid"] for c in res.comments] == [1, 2, 3, 4, 5]
    assert res.pages == 3 and res.partial is False
    second = api.calls[1][1]
    assert json.loads(second["pagination_str"]) == {"offset": "a"}
    assert second["mode"] == 2 and second["type"] == 1 and second["oid"] == 123


def test_hot_mode_uses_mode_3():
    api = _FakeApi(pages=[_page([1], "a", is_end=True)])
    _collect(api, mode="hot")
    assert api.calls[0][1]["mode"] == 3


def test_repeated_cursor_marks_partial():
    api = _FakeApi(pages=[_page([1], "a"), _page([2], "a"), _page([3], "z", is_end=True)])
    res = _collect(api)
    assert res.partial and "重复游标" in res.partial_reason
    assert [c["rpid"] for c in res.comments] == [1, 2]


def test_max_comments_stops_early():
    api = _FakeApi(pages=[_page(list(range(1, 21)), "a"), _page(list(range(21, 41)), "b"), _page([99], "c")])
    res = _collect(api, max_comments=30)
    assert res.pages == 2 and res.partial and res.partial_reason == "已达采集上限"


def test_preview_replies_collected_without_requests():
    page = {"replies": [_reply(1, sub=[_reply(11, root=1, parent=1)])], "cursor": {"is_end": True}}
    api = _FakeApi(pages=[page])
    res = _collect(api)
    sub = [c for c in res.comments if c["rpid"] == 11][0]
    assert sub["is_reply"] and sub["root_rpid"] == 1
    assert not [c for c in api.calls if c[0].endswith("/reply/reply")]


def test_include_replies_false_skips_reply_api():
    api = _FakeApi(pages=[_page([1], "a", is_end=True, rcount=50)])
    _collect(api, include_replies=False)
    assert not [c for c in api.calls if c[0].endswith("/reply/reply")]


def test_reply_request_budget_is_60():
    rpids = list(range(1, 101))
    pages = [_page(rpids[i:i + 20], f"o{i}", is_end=(i == 80), rcount=100) for i in range(0, 100, 20)]
    reply_pages = {(r, pn): {"replies": [_reply(10000 + r * 10 + pn, root=r, parent=r)], "page": {"count": 100}}
                   for r in rpids for pn in (1, 2, 3)}
    api = _FakeApi(pages=pages, reply_pages=reply_pages)
    res = _collect(api, include_replies=True)
    assert res.reply_requests == 60
    assert len([c for c in api.calls if c[0].endswith("/reply/reply")]) == 60
    # 默认 20 楼 × 3 页 = 60，刚好用完预算，没有遗漏楼层，不算不完整。
    assert "楼中楼已达请求上限" not in res.partial_reason


def test_reply_budget_exceeded_marks_partial():
    rpids = list(range(1, 101))
    pages = [_page(rpids[i:i + 20], f"o{i}", is_end=(i == 80), rcount=100) for i in range(0, 100, 20)]
    reply_pages = {(r, pn): {"replies": [_reply(10000 + r * 10 + pn, root=r, parent=r)], "page": {"count": 100}}
                   for r in rpids for pn in (1, 2, 3)}
    api = _FakeApi(pages=pages, reply_pages=reply_pages)
    settings = build_settings({"reply_floors": 40})
    res = asyncio.run(PatrolCollector(api, settings).collect(META, max_comments=500, include_replies=True))
    assert res.reply_requests == 60
    assert res.partial and "楼中楼已达请求上限" in res.partial_reason


def test_only_floors_with_rcount_over_3():
    api = _FakeApi(pages=[_page([1, 2], "a", is_end=True, rcount=2)])
    res = _collect(api, include_replies=True)
    assert res.reply_requests == 0


def test_parse_reply_fields_and_no_location():
    item = parse_reply(_reply(5, text="@阿杰 看这个"), is_reply=False)
    assert item["sign"] == "签名" and item["level"] == 3 and item["is_vip"] is False
    assert item["official_type"] == -1 and item["is_reply"] is False
    assert "location" not in item and "reply_control" not in item and "avatar" not in item


def test_parse_reply_missing_fields():
    raw = _reply(5)
    raw["member"].pop("sign")
    raw["member"].pop("official_verify")
    item = parse_reply(raw, is_reply=False)
    assert item["sign"] == "" and item["official_type"] == -1
    assert parse_reply({"rpid": 1, "member": {}}, is_reply=False) is None
    assert parse_reply("bad", is_reply=False) is None


def test_at_users_parsed():
    raw = _reply(5)
    raw["content"]["members"] = [{"mid": "77", "uname": "阿杰"}, {"mid": None}]
    assert parse_reply(raw, is_reply=False)["at_users"] == [{"mid": 77, "uname": "阿杰"}]


def test_risk_error_propagates():
    api = _FakeApi(errors=[BilibiliAPIError("请求被风控: x", bili_code=-352)])
    with pytest.raises(BilibiliAPIError):
        _collect(api)


def test_cookie_expired_propagates():
    api = _FakeApi(errors=[CookieExpiredError("x")])
    with pytest.raises(CookieExpiredError):
        _collect(api)


def test_two_consecutive_ordinary_failures_mark_partial():
    api = _FakeApi(pages=[_page([1], "a")], errors=[None, NetworkError("x"), BilibiliAPIError("API错误 [-400]: x", bili_code=-400)])
    res = _collect(api)
    assert res.partial and res.partial_reason == "评论分页连续失败" and res.failed_pages == 2
    assert [c["rpid"] for c in res.comments] == [1]


def test_cancel_between_pages():
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 1

    api = _FakeApi(pages=[_page([1], "a"), _page([2], "b")])
    with pytest.raises(ScanCancelled):
        _collect(api, is_cancelled=cancelled)


def test_checkpoint_callback_receives_cursor():
    seen = []
    api = _FakeApi(pages=[_page([1], "a"), _page([2], "b", is_end=True)])
    _collect(api, checkpoint=seen.append)
    assert json.loads(seen[0]["pagination_str"]) == {"offset": "a"}


def test_resume_with_invalid_cursor_raises():
    api = _FakeApi(pages=[{"replies": [], "cursor": {"is_end": True}}])
    with pytest.raises(ValueError, match="断点已失效"):
        _collect(api, resume_cursor='{"offset":"stale"}')


def test_wbi_endpoint_option():
    api = _FakeApi(pages=[_page([1], "a", is_end=True)])
    asyncio.run(PatrolCollector(api, build_settings({"comment_endpoint": "wbi_main"})).collect(
        META, max_comments=10, include_replies=False))
    url, params, signed = api.calls[0]
    assert url.endswith("/x/v2/reply/wbi/main") and signed is True and params["web_location"] == 1315875


def test_fetch_video_meta():
    api = _FakeApi(meta={"bvid": "BV1xx411c7mD", "aid": 123, "title": "T", "owner": {"mid": 9, "name": "UP"},
                         "stat": {"reply": 88}})
    meta = asyncio.run(PatrolCollector(api, S).fetch_video_meta("https://www.bilibili.com/video/BV1xx411c7mD"))
    assert (meta.aid, meta.owner_mid, meta.reply_count) == (123, 9, 88)


def test_dynamic_link_rejected():
    api = _FakeApi()
    with pytest.raises(ValueError, match="只支持视频"):
        asyncio.run(PatrolCollector(api, S).fetch_video_meta("https://www.bilibili.com/opus/123456789"))
