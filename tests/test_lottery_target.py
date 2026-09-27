"""抽奖目标解析与元数据获取的契约级测试。

覆盖 modules/lottery/target.py 的输入解析、链接识别与元数据映射分支。
B 站客户端使用真实 ``BilibiliAPI`` 实例，仅把网络方法替换为契约级响应函数。
"""

import asyncio

import pytest

from bilibili.api import BilibiliAPI

from modules.lottery.target import LotteryTarget, fetch_target_metadata, parse_target_input


# ------------------------------------------------------------------ 构造辅助

def _build_api(responder):
    """构造真实 BilibiliAPI 并注入契约级 get，返回 (api, 调用记录)。

    Args:
        responder: ``(url, params) -> dict`` 的同步响应函数。

    Returns:
        ``(api, calls)``；calls 按调用顺序记录 url 与 params。
    """
    api = BilibiliAPI()
    calls = []

    async def contract_get(url, params=None, **kwargs):
        """契约级 get：记录调用并返回预置响应，绝不发起真实请求。"""
        calls.append({"url": url, "params": params})
        return responder(url, params)

    # 实例级注入，避免改动类属性影响其它测试。
    api.get = contract_get
    return api, calls


def _video_responder(payload):
    """返回固定视频元数据响应的 responder。"""

    def responder(url, params):
        return payload

    return responder


# ------------------------------------------------- parse_target_input

def test_parse_target_input_accepts_plain_bvid() -> None:
    """纯 BV 号直接识别为视频目标。"""
    assert parse_target_input("BV1xx411c7mD") == ("video", "BV1xx411c7mD")


def test_parse_target_input_extracts_bvid_from_shared_text() -> None:
    """分享文案里夹带 BV 号时仍应提取为视频目标。"""
    raw = "来看看这个 https://www.bilibili.com/video/BV1xx411c7mD?spm=333 抽奖"

    assert parse_target_input(raw) == ("video", "BV1xx411c7mD")


def test_parse_target_input_strips_surrounding_whitespace() -> None:
    """首尾空白不影响 BV 号识别。"""
    assert parse_target_input("   BV1xx411c7mD\n") == ("video", "BV1xx411c7mD")


def test_parse_target_input_lowercase_bvid_keeps_original_case() -> None:
    """小写 BV 号可被大小写不敏感的正则命中，并原样返回。"""
    assert parse_target_input("bv1xx411c7md") == ("video", "bv1xx411c7md")


def test_parse_target_input_rejects_pure_digits() -> None:
    """纯数字可能是 UID/AV 号，必须显式拒绝并给出可操作提示（从外部打）。"""
    with pytest.raises(ValueError) as excinfo:
        parse_target_input("123456789")

    assert "完整链接" in str(excinfo.value)


def test_parse_target_input_accepts_opus_link() -> None:
    """新版 opus 动态链接可提取数字动态 ID。"""
    url = "https://www.bilibili.com/opus/123456789"

    assert parse_target_input(url) == ("dynamic", "123456789")


def test_parse_target_input_accepts_legacy_dynamic_link() -> None:
    """旧版 dynamic 链接同样被支持。"""
    url = "https://t.bilibili.com/dynamic/987654321"

    assert parse_target_input(url) == ("dynamic", "987654321")


def test_parse_target_input_prefers_path_id_over_query_id() -> None:
    """路径中的动态 ID 优先级高于查询参数。"""
    url = "https://www.bilibili.com/opus/111?dynamic_id=222"

    assert parse_target_input(url) == ("dynamic", "111")


def test_parse_target_input_falls_back_to_query_id() -> None:
    """路径无可识别 ID 时回退到 dynamic_id 查询参数。"""
    url = "https://www.bilibili.com/some/page?dynamic_id=777"

    assert parse_target_input(url) == ("dynamic", "777")


def test_parse_target_input_rejects_foreign_domain() -> None:
    """非 bilibili.com 域名必须拒绝（从外部打异常）。"""
    with pytest.raises(ValueError) as excinfo:
        parse_target_input("https://example.com/dynamic/123")

    assert "请输入" in str(excinfo.value)


def test_parse_target_input_rejects_non_http_scheme() -> None:
    """非 http/https 协议必须拒绝。"""
    with pytest.raises(ValueError) as excinfo:
        parse_target_input("ftp://www.bilibili.com/dynamic/123")

    assert "请输入" in str(excinfo.value)


def test_parse_target_input_rejects_bilibili_link_without_id() -> None:
    """域名合法但缺少动态 ID 时给出统一的未识别错误。"""
    with pytest.raises(ValueError) as excinfo:
        parse_target_input("https://www.bilibili.com/")

    assert "未识别到有效" in str(excinfo.value)


def test_parse_target_input_rejects_query_id_that_is_not_numeric() -> None:
    """查询参数里的非数字 dynamic_id 视为无效。"""
    with pytest.raises(ValueError) as excinfo:
        parse_target_input("https://www.bilibili.com/some/page?dynamic_id=abc")

    assert "未识别到有效" in str(excinfo.value)


def test_parse_target_input_rejects_empty_input() -> None:
    """空输入或纯空白走域名校验失败分支。"""
    with pytest.raises(ValueError):
        parse_target_input("")

    with pytest.raises(ValueError):
        parse_target_input("   ")


def test_parse_target_input_documents_suffix_host_matching() -> None:
    """记录当前契约：主机名按 endswith('bilibili.com') 判定，前缀相似域名亦会通过。"""
    # 该断言固化现状，便于未来收紧域名白名单时同步更新测试。
    assert parse_target_input("https://evilbilibili.com/dynamic/5") == ("dynamic", "5")


# ------------------------------------------------- LotteryTarget

def test_lottery_target_to_dict_matches_dataclass_fields() -> None:
    """to_dict 输出字段与构造函数一一对应，便于前端直接消费。"""
    target = LotteryTarget(
        target_type="video",
        target_id="BV1xx411c7mD",
        oid=170001,
        comment_type=1,
        title="标题",
        author="作者",
    )

    assert target.to_dict() == {
        "target_type": "video",
        "target_id": "BV1xx411c7mD",
        "oid": 170001,
        "comment_type": 1,
        "title": "标题",
        "author": "作者",
    }


def test_lottery_target_is_frozen() -> None:
    """冻结数据类不允许运行期篡改字段（从外部打异常）。"""
    target = LotteryTarget("video", "BV1", 1, 1, "t", "a")

    with pytest.raises(Exception):
        target.title = "被篡改"


# ------------------------------------------------- fetch_target_metadata（视频）

def test_fetch_target_metadata_maps_video_fields() -> None:
    """视频目标应映射出 aid、标题与发布者。"""
    api, calls = _build_api(
        _video_responder(
            {
                "bvid": "BV1xx411c7mD",
                "aid": 170001,
                "title": "回归测试视频",
                "owner": {"name": "测试UP"},
            }
        )
    )

    target = asyncio.run(
        asyncio.wait_for(fetch_target_metadata(api, "BV1xx411c7mD"), timeout=5)
    )

    assert target.target_type == "video"
    assert target.target_id == "BV1xx411c7mD"
    assert target.oid == 170001
    assert target.comment_type == 1
    assert target.title == "回归测试视频"
    assert target.author == "测试UP"
    assert calls[0]["url"] == f"{BilibiliAPI.BASE_URL}/x/web-interface/view"
    assert calls[0]["params"] == {"bvid": "BV1xx411c7mD"}


def test_fetch_target_metadata_uses_video_defaults_when_fields_missing() -> None:
    """视频接口缺字段时应补齐默认文案与 0 号评论对象。"""
    api, _ = _build_api(_video_responder({"owner": {}}))

    target = asyncio.run(fetch_target_metadata(api, "BV1xx411c7mD"))

    assert target.oid == 0
    assert target.title == "未获取到视频标题"
    assert target.author == "未获取到发布者"
    # 响应缺少 bvid 时回退到解析出的目标 ID。
    assert target.target_id == "BV1xx411c7mD"


def test_fetch_target_metadata_tolerates_non_dict_owner() -> None:
    """owner 为 None 时不得抛出 AttributeError。"""
    api, _ = _build_api(_video_responder({"aid": 5, "owner": None, "title": None}))

    target = asyncio.run(fetch_target_metadata(api, "BV1xx411c7mD"))

    assert target.author == "未获取到发布者"
    assert target.title == "未获取到视频标题"


# ------------------------------------------------- fetch_target_metadata（动态）

def _dynamic_detail(*, title=None, text=None, basic=None, author="动态作者") -> dict:
    """构造动态详情接口的嵌套响应结构；author 传 None 表示作者节点缺失。"""
    major = {"archive": {"title": title}} if title is not None else {}
    desc = {"text": text} if text is not None else {}
    modules = {"module_dynamic": {"major": major, "desc": desc}}
    if author is not None:
        modules["module_author"] = {"name": author}
    return {
        "item": {
            "basic": basic or {},
            "modules": modules,
        }
    }


def test_fetch_target_metadata_prefers_forwarded_archive_title() -> None:
    """转发动态标题优先取被转发视频标题。"""
    api, calls = _build_api(
        lambda url, params: _dynamic_detail(title="被转发的视频", text="动态正文")
    )

    target = asyncio.run(
        asyncio.wait_for(
            fetch_target_metadata(api, "https://www.bilibili.com/opus/123"), timeout=5
        )
    )

    assert target.target_type == "dynamic"
    assert target.target_id == "123"
    assert target.title == "被转发的视频"
    assert target.author == "动态作者"
    assert calls[0]["url"] == f"{BilibiliAPI.BASE_URL}/x/polymer/web-dynamic/v1/detail"
    assert calls[0]["params"] == {"id": "123"}


def test_fetch_target_metadata_falls_back_to_dynamic_text() -> None:
    """无转发视频标题时退回动态正文。"""
    api, _ = _build_api(lambda url, params: _dynamic_detail(text="纯文字动态"))

    target = asyncio.run(fetch_target_metadata(api, "https://www.bilibili.com/opus/123"))

    assert target.title == "纯文字动态"


def test_fetch_target_metadata_uses_placeholder_for_empty_dynamic() -> None:
    """既无转发标题、无正文、也无作者时给出固定占位文案。"""
    api, _ = _build_api(lambda url, params: _dynamic_detail(author=None))

    target = asyncio.run(fetch_target_metadata(api, "https://www.bilibili.com/opus/123"))

    assert target.title == "无文字动态"
    assert target.author == "未获取到发布者"


def test_fetch_target_metadata_reads_comment_identity_from_basic() -> None:
    """评论区标识与评论类型取自 basic 节点。"""
    api, _ = _build_api(
        lambda url, params: _dynamic_detail(
            text="正文", basic={"comment_id_str": "555", "comment_type": 17}
        )
    )

    target = asyncio.run(fetch_target_metadata(api, "https://www.bilibili.com/opus/123"))

    assert target.oid == 555
    assert target.comment_type == 17


def test_fetch_target_metadata_accepts_int_comment_id_and_default_type() -> None:
    """comment_id 为整数时可用，comment_type 缺失时默认为 17。"""
    api, _ = _build_api(
        lambda url, params: _dynamic_detail(text="正文", basic={"comment_id": 556})
    )

    target = asyncio.run(fetch_target_metadata(api, "https://www.bilibili.com/opus/123"))

    assert target.oid == 556
    assert target.comment_type == 17


def test_fetch_target_metadata_falls_back_to_target_id_for_oid() -> None:
    """basic 完全缺失时评论区对象回退为动态 ID 本身。"""
    api, _ = _build_api(lambda url, params: _dynamic_detail(text="正文"))

    target = asyncio.run(fetch_target_metadata(api, "https://www.bilibili.com/opus/999888"))

    assert target.oid == 999888
    assert target.comment_type == 17


def test_fetch_target_metadata_truncates_long_dynamic_title() -> None:
    """超长动态标题截断到 120 字，避免污染前端展示。"""
    long_title = "标" * 200
    api, _ = _build_api(lambda url, params: _dynamic_detail(text=long_title))

    target = asyncio.run(fetch_target_metadata(api, "https://www.bilibili.com/opus/123"))

    assert len(target.title) == 120
    assert target.title == "标" * 120


def test_fetch_target_metadata_strips_dynamic_title() -> None:
    """动态标题首尾空白应被去除。"""
    api, _ = _build_api(lambda url, params: _dynamic_detail(text="  前后有空格  "))

    target = asyncio.run(fetch_target_metadata(api, "https://www.bilibili.com/opus/123"))

    assert target.title == "前后有空格"


def test_fetch_target_metadata_tolerates_empty_detail_payload() -> None:
    """接口返回空字典时不抛异常，走全部默认分支。"""
    api, _ = _build_api(lambda url, params: {})

    target = asyncio.run(fetch_target_metadata(api, "https://www.bilibili.com/opus/321"))

    assert target.oid == 321
    assert target.comment_type == 17
    assert target.title == "无文字动态"
    assert target.author == "未获取到发布者"


def test_fetch_target_metadata_rejects_invalid_input_before_request() -> None:
    """非法输入应在发起请求前就被拒绝（从外部打异常，且不产生调用）。"""
    api, calls = _build_api(lambda url, params: {})

    with pytest.raises(ValueError):
        asyncio.run(fetch_target_metadata(api, "https://example.com/dynamic/1"))

    assert calls == []
