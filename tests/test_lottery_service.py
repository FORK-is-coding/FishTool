"""抽奖核心服务编排的契约级测试。

覆盖 modules/lottery/service.py 的公开方法与内部编排分支：
目标预览、本地复用、动态分页采集、画像补全、AI 判定、随机抽取。
SQLite 一律使用 tmp_path 下的隔离库（monkeypatch 模块级 get_session），绝不触碰 data/*.db。
"""

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from bilibili.api import BilibiliAPI
from core.database import Comment, DatabaseManager, Video

from modules.lottery.service import LotteryService
from modules.lottery.target import LotteryTarget


# ------------------------------------------------------------------ 隔离夹具

@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch):
    """把 service 模块的 get_session 指向 tmp_path 下的隔离 SQLite 库。"""
    manager = DatabaseManager(str(tmp_path / "lottery_iso.db"))
    monkeypatch.setattr("modules.lottery.service.get_session", manager.get_session)
    yield manager
    manager.engine.dispose()


@pytest.fixture()
def service(tmp_path: Path, isolated_db) -> LotteryService:
    """构造缓存目录与数据库均隔离的真实 LotteryService。"""
    return LotteryService(BilibiliAPI(), cache_dir=tmp_path / "lottery_cache")


# ------------------------------------------------------------------ 契约级假对象

class _ProgressRecorder:
    """记录进度回调三元组的契约级收集器。"""

    def __init__(self) -> None:
        """初始化空记录。"""
        self.calls = []

    def __call__(self, stage: str, percent: int, message: str) -> None:
        """记录一次进度推送。"""
        self.calls.append((stage, percent, message))

    @property
    def stages(self) -> list:
        """返回按顺序记录的阶段名列表。"""
        return [call[0] for call in self.calls]


class _ContractClassify:
    """契约级 classify_profiles：记录批量组成并按 UID 给出判定。"""

    def __init__(self, suspicious_uids=()) -> None:
        """记录应判为可疑的 UID 集合。"""
        self.suspicious_uids = set(suspicious_uids)
        self.batches = []
        self.focus_templates = []

    async def __call__(self, profiles, focus_template=None):
        """返回与输入严格一一对应的判定，绝不访问 LLM。"""
        uids = [int(item["uid"]) for item in profiles]
        self.batches.append(uids)
        self.focus_templates.append(focus_template)
        return [
            {
                "uid": uid,
                "classification": "suspicious" if uid in self.suspicious_uids else "real",
                "confidence": 0.9,
                "reasons": ["契约判定"],
                "analysis_text": "契约判定",
                "source": "llm",
            }
            for uid in uids
        ]


class _ContractCollector:
    """契约级评论采集器：实现 collect_video_comments 契约。"""

    def __init__(self, comments=None, error=None) -> None:
        """配置返回的评论与可选异常。"""
        self.comments = comments or []
        self.error = error
        self.calls = []

    async def collect_video_comments(self, bvid, strategy=None, max_count=None):
        """返回预置评论或抛出预置异常。"""
        self.calls.append({"bvid": bvid, "strategy": strategy})
        if self.error is not None:
            raise self.error
        return [dict(item) for item in self.comments]


class _ExplodingSession:
    """契约级会话：任何查询都抛错，用于验证回滚与降级释放。"""

    def __init__(self) -> None:
        """初始化状态标记。"""
        self.rolled_back = False
        self.closed = False

    def query(self, *args, **kwargs):
        """模拟数据库不可用。"""
        raise RuntimeError("数据库不可用")

    def rollback(self) -> None:
        """记录回滚调用。"""
        self.rolled_back = True

    def close(self) -> None:
        """记录关闭调用。"""
        self.closed = True


# ------------------------------------------------------------------ 通用辅助

def _video_target(target_id: str = "BV1xx411c7mD") -> LotteryTarget:
    """构造一个视频抽奖目标。"""
    return LotteryTarget("video", target_id, 170001, 1, "标题", "作者")


def _dynamic_target(target_id: str = "123") -> LotteryTarget:
    """构造一个动态抽奖目标。"""
    return LotteryTarget("dynamic", target_id, 555, 17, "动态标题", "作者")


def _install_api_get(api: BilibiliAPI, responder) -> list:
    """把 api.get 换成契约级响应函数，返回调用记录列表。"""
    calls = []

    async def contract_get(url, params=None, **kwargs):
        """记录调用并返回预置响应。"""
        calls.append({"url": url, "params": params})
        return responder(url, params)

    api.get = contract_get
    return calls


def _install_profile_fetch(service: LotteryService, profiles_by_uid: dict) -> list:
    """把画像采集器换成契约级 fetch，返回被请求的 UID 列表。"""
    seen = []

    async def contract_fetch(uid):
        """返回预置画像。"""
        seen.append(uid)
        return dict(profiles_by_uid[uid])

    service._profile_collector.fetch = contract_fetch
    return seen


def _seed_video(manager: DatabaseManager, bvid: str, comments: list) -> int:
    """向隔离库写入一个视频及其评论，返回视频主键。"""
    session = manager.get_session()
    try:
        video = Video(bvid=bvid, aid=1, title="标题")
        session.add(video)
        session.flush()
        for item in comments:
            session.add(Comment(video_id=video.id, **item))
        session.commit()
        return video.id
    finally:
        session.close()


def _full_comment_row(uid: int, rpid: str, ctime: datetime, *, level: int = 6, vip_type: int = 0) -> dict:
    """构造字段完整的评论行数据，供抽奖过滤直接使用。"""
    return {
        "rpid": rpid,
        "uid": uid,
        "uname": f"用户{uid}",
        "content": "抽奖",
        "ctime": ctime,
        "level_info": {"current_level": level},
        "vip": {"vipType": vip_type, "vipStatus": 1 if vip_type else 0},
    }


def _raw_reply(rpid: str, uid: int, ctime: int = 1_700_000_000) -> dict:
    """构造一条 B 站评论接口原始回复。"""
    return {
        "rpid_str": rpid,
        "ctime": ctime,
        "member": {"mid": str(uid), "uname": f"用户{uid}", "level_info": {"current_level": 6}},
        "content": {"message": "参与"},
    }


# ================================================================ 初始化与委托

def test_init_defaults_cache_dir_under_data() -> None:
    """不传 cache_dir 时应落在 data/lottery_cache（仅计算路径，不落盘）。"""
    svc = LotteryService(BilibiliAPI())

    assert svc.cache_dir == Path("data") / "lottery_cache"
    assert svc._cache.cache_dir == svc.cache_dir


def test_init_respects_custom_cache_dir(service: LotteryService, tmp_path: Path) -> None:
    """显式传入的缓存目录应被原样采用。"""
    assert service.cache_dir == tmp_path / "lottery_cache"


def test_comment_row_to_dict_delegates_to_candidate() -> None:
    """静态兼容入口应与 candidate 模块行为一致。"""
    row = Comment()
    row.rpid = "1"
    row.uid = 9
    row.uname = "u"
    row.content = "c"
    row.vip = {}

    assert LotteryService._comment_row_to_dict(row)["vip_label"] == "非会员"


def test_parse_reply_delegates_to_candidate() -> None:
    """评论清洗兼容入口应返回标准候选字段。"""
    parsed = LotteryService._parse_reply(_raw_reply("1", 42))

    assert parsed["uid"] == 42
    assert parsed["rpid"] == "1"


def test_parse_comment_time_delegates_to_candidate() -> None:
    """时间解析兼容入口应接受多种输入。"""
    assert LotteryService._parse_comment_time(0) == datetime.fromtimestamp(0)
    assert LotteryService._parse_comment_time(None) is None


def test_dynamic_comment_cache_round_trip(service: LotteryService) -> None:
    """动态评论缓存读写委托应可往返。"""
    comments = [{"rpid": "1", "uid": 42}]

    service._save_dynamic_comments("D1", comments)

    assert service._load_dynamic_comments("D1") == comments
    assert service._load_dynamic_comments("D2") == []


def test_profile_cache_round_trip(service: LotteryService) -> None:
    """画像缓存读写委托应可往返。"""
    service._save_profile_cache({"42": {"level": 6}})

    assert service._load_profile_cache() == {"42": {"level": 6}}


# ================================================================ preview

def test_preview_returns_serializable_target(service: LotteryService, monkeypatch) -> None:
    """预览应把目标数据类转成前端可直接消费的字典。"""

    async def fake_fetch(api, raw_target):
        """返回固定目标，避免真实网络请求。"""
        return _video_target()

    monkeypatch.setattr("modules.lottery.service.fetch_target_metadata", fake_fetch)

    preview = asyncio.run(asyncio.wait_for(service.preview("BV1xx411c7mD"), timeout=5))

    assert preview["target_type"] == "video"
    assert preview["oid"] == 170001
    assert preview["title"] == "标题"


def test_preview_propagates_failure_to_caller(service: LotteryService, monkeypatch) -> None:
    """预览失败必须原样上抛，交由路由层转 HTTP 错误（从外部打异常）。"""

    async def failing_fetch(api, raw_target):
        """模拟目标解析失败。"""
        raise ValueError("未识别到有效 BV 号或动态完整链接")

    monkeypatch.setattr("modules.lottery.service.fetch_target_metadata", failing_fetch)

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(service.preview("坏输入"))

    assert "未识别到有效" in str(excinfo.value)


# ================================================================ 本地评论读取

def test_load_video_comments_returns_rows_ordered_by_time(
    service: LotteryService, isolated_db: DatabaseManager
) -> None:
    """本地复用应按评论时间升序返回。"""
    _seed_video(
        isolated_db,
        "BV100",
        [
            _full_comment_row(1, "c2", datetime(2024, 5, 2)),
            _full_comment_row(2, "c1", datetime(2024, 5, 1)),
        ],
    )

    rows = service._load_video_comments("BV100")

    assert [row["rpid"] for row in rows] == ["c1", "c2"]
    assert rows[0]["level"] == 6


def test_load_video_comments_returns_empty_for_unknown_video(service: LotteryService) -> None:
    """未采集过的视频返回空列表，触发在线采集。"""
    assert service._load_video_comments("BV_不存在") == []


def test_load_video_comments_degrades_and_closes_session(
    service: LotteryService, monkeypatch
) -> None:
    """数据库异常时降级为空列表，且会话必须被关闭。"""
    session = _ExplodingSession()
    monkeypatch.setattr("modules.lottery.service.get_session", lambda: session)

    assert service._load_video_comments("BV100") == []
    assert session.closed is True


# ================================================================ 动态评论采集

def test_collect_dynamic_comments_single_page_persists_cache(service: LotteryService) -> None:
    """一轮即到末页时应返回去重评论并落盘缓存。"""
    calls = _install_api_get(
        service.api,
        lambda url, params: {
            "cursor": {"is_end": True},
            "replies": [_raw_reply("1", 42), _raw_reply("1", 42), _raw_reply("2", 43)],
        },
    )
    progress = _ProgressRecorder()

    comments = asyncio.run(
        asyncio.wait_for(
            service._collect_dynamic_comments(_dynamic_target(), progress), timeout=5
        )
    )

    # 同一 rpid 只保留一次。
    assert [item["rpid"] for item in comments] == ["1", "2"]
    assert service._load_dynamic_comments("123") == comments
    assert calls[0]["params"]["oid"] == 555
    assert calls[0]["params"]["type"] == 17
    assert progress.stages == ["collecting_comments"]


def test_collect_dynamic_comments_follows_pagination_cursor(service: LotteryService) -> None:
    """首页未结束时应带 pagination_str 继续翻页，直到接口声明结束。"""
    pages = [
        {
            "cursor": {"is_end": False, "pagination_reply": {"next_offset": "OFF1"}, "next": 1},
            "replies": [_raw_reply("p1", 1)],
        },
        {"cursor": {"is_end": True}, "replies": [_raw_reply("p2", 2)]},
    ]
    seen_params = []

    def responder(url, params):
        """按顺序返回两页数据。"""
        seen_params.append(dict(params))
        return pages[len(seen_params) - 1]

    _install_api_get(service.api, responder)

    comments = asyncio.run(
        asyncio.wait_for(service._collect_dynamic_comments(_dynamic_target()), timeout=5)
    )

    assert [item["rpid"] for item in comments] == ["p1", "p2"]
    # 首页不带游标，第二页必须携带第一页给出的 offset。
    assert "pagination_str" not in seen_params[0]
    assert seen_params[1]["pagination_str"] == '{"offset":"OFF1"}'


def test_collect_dynamic_comments_stops_on_repeated_cursor(service: LotteryService) -> None:
    """接口重复返回同一游标时必须主动停止，避免死循环。"""
    same_cursor = {
        "cursor": {"is_end": False, "pagination_reply": {"next_offset": "SAME"}},
        "replies": [_raw_reply("r1", 1)],
    }
    calls = _install_api_get(service.api, lambda url, params: same_cursor)

    comments = asyncio.run(
        asyncio.wait_for(service._collect_dynamic_comments(_dynamic_target()), timeout=5)
    )

    # 只采集第一页，第二轮因重复游标提前退出。
    assert len(calls) == 2
    assert [item["rpid"] for item in comments] == ["r1"]


def test_collect_dynamic_comments_stops_when_replies_empty(service: LotteryService) -> None:
    """接口不再返回 replies 时结束翻页。"""
    pages = [
        {"cursor": {"is_end": False}, "replies": [_raw_reply("only", 1)]},
        {"cursor": {"is_end": False}, "replies": []},
    ]
    index = {"value": 0}

    def responder(url, params):
        """按顺序返回两页数据，第二页为空。"""
        page = pages[index["value"]]
        index["value"] += 1
        return page

    calls = _install_api_get(service.api, responder)

    comments = asyncio.run(
        asyncio.wait_for(service._collect_dynamic_comments(_dynamic_target()), timeout=5)
    )

    assert [item["rpid"] for item in comments] == ["only"]
    assert len(calls) == 2


def test_collect_dynamic_comments_filters_replies_without_uid(service: LotteryService) -> None:
    """缺少 mid 的评论被丢弃，不影响其它评论。"""
    _install_api_get(
        service.api,
        lambda url, params: {
            "cursor": {"is_end": True},
            "replies": [{"member": {}}, _raw_reply("ok", 7)],
        },
    )

    comments = asyncio.run(
        asyncio.wait_for(service._collect_dynamic_comments(_dynamic_target()), timeout=5)
    )

    assert [item["rpid"] for item in comments] == ["ok"]


def test_collect_dynamic_comments_respects_max_count_as_soft_cap(
    service: LotteryService
) -> None:
    """max_count 为软上限：达到后停止翻页，保证不会无休止请求。"""
    index = {"value": 0}

    def responder(url, params):
        """每次返回一页未结束的数据，用于验证软上限能终止循环。"""
        page = {
            "cursor": {
                "is_end": False,
                "pagination_reply": {"next_offset": f"O{index['value']}"},
            },
            "replies": [_raw_reply(f"r{index['value']}", index["value"] + 1)],
        }
        index["value"] += 1
        return page

    calls = _install_api_get(service.api, responder)

    comments = asyncio.run(
        asyncio.wait_for(
            service._collect_dynamic_comments(_dynamic_target(), None, max_count=1), timeout=5
        )
    )

    assert len(calls) == 1
    assert len(comments) == 1


# ================================================================ get_comments

def test_get_comments_prefers_local_video_rows(
    service: LotteryService, isolated_db: DatabaseManager
) -> None:
    """视频命中本地库时应跳过在线采集并推送复用进度。"""
    _seed_video(isolated_db, "BV200", [_full_comment_row(1, "c1", datetime(2024, 5, 1))])
    collector = _ContractCollector(comments=[{"rpid": "crawler"}])
    service.collector = collector
    progress = _ProgressRecorder()

    comments, source = asyncio.run(
        asyncio.wait_for(service.get_comments(_video_target("BV200"), progress), timeout=5)
    )

    assert source == "local"
    assert [row["rpid"] for row in comments] == ["c1"]
    assert collector.calls == []
    assert progress.stages == ["local_lookup", "local_reuse"]


def test_get_comments_prefers_local_dynamic_cache(
    service: LotteryService, monkeypatch
) -> None:
    """动态命中 JSON 缓存时同样跳过在线采集。"""
    service._save_dynamic_comments("123", [{"rpid": "cached", "uid": 1}])
    progress = _ProgressRecorder()

    comments, source = asyncio.run(
        asyncio.wait_for(service.get_comments(_dynamic_target(), progress), timeout=5)
    )

    assert source == "local"
    assert [item["rpid"] for item in comments] == ["cached"]


def test_get_comments_falls_back_to_full_video_collection(service: LotteryService) -> None:
    """视频本地未命中时应以全量策略调用统一采集器。"""
    from modules.comment.collector import CommentCollector

    collector = _ContractCollector(comments=[{"rpid": "online", "uid": 5}])
    service.collector = collector
    progress = _ProgressRecorder()

    comments, source = asyncio.run(
        asyncio.wait_for(service.get_comments(_video_target("BV404"), progress), timeout=5)
    )

    assert source == "crawler"
    assert [row["rpid"] for row in comments] == ["online"]
    assert collector.calls == [{"bvid": "BV404", "strategy": CommentCollector.STRATEGY_FULL}]
    assert progress.stages == ["local_lookup", "collecting_comments"]


def test_get_comments_falls_back_to_dynamic_collection(service: LotteryService) -> None:
    """动态本地未命中时走动态分页采集链路。"""
    _install_api_get(
        service.api,
        lambda url, params: {"cursor": {"is_end": True}, "replies": [_raw_reply("dyn", 8)]},
    )

    comments, source = asyncio.run(
        asyncio.wait_for(service.get_comments(_dynamic_target("999"), None), timeout=5)
    )

    assert source == "crawler"
    assert [item["rpid"] for item in comments] == ["dyn"]


# ================================================================ 单用户流程

def test_fetch_user_profile_delegates_to_collector(service: LotteryService) -> None:
    """单用户画像应直接委托给画像采集器。"""
    _install_profile_fetch(service, {42: {"uid": 42, "level": 6}})

    assert asyncio.run(service.fetch_user_profile(42)) == {"uid": 42, "level": 6}


def test_quick_filter_pairs_profile_with_assessment(
    service: LotteryService, monkeypatch
) -> None:
    """快速筛选应返回画像与判定结果两部分。"""
    _install_profile_fetch(service, {42: {"uid": 42, "level": 6}})
    fake = _ContractClassify()
    monkeypatch.setattr("modules.lottery.service.classify_profiles", fake)

    result = asyncio.run(service.quick_filter(42, "重点看活跃度"))

    assert result["profile"]["level"] == 6
    assert result["assessment"]["classification"] == "real"
    assert fake.focus_templates == ["重点看活跃度"]


# ================================================================ verify_winners

def test_verify_winners_rejects_empty_list(service: LotteryService) -> None:
    """空中奖名单必须显式报错（从外部打异常）。"""
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(service.verify_winners([]))

    assert "请先进行抽奖" in str(excinfo.value)


def test_verify_winners_rejects_invalid_uid(
    service: LotteryService, monkeypatch
) -> None:
    """中奖名单含无效 UID 时拒绝继续（从外部打异常）。"""
    monkeypatch.setattr("modules.lottery.service.classify_profiles", _ContractClassify())

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(service.verify_winners([{"uid": 0, "uname": "坏数据"}]))

    assert "无效 UID" in str(excinfo.value)


def test_verify_winners_reuses_cache_and_counts_sources(
    service: LotteryService, monkeypatch
) -> None:
    """命中缓存的画像不重复采集，并区分本地命中与在线补取数量。"""
    service._save_profile_cache(
        {
            "1": {
                "uid": 1,
                "level": 6,
                "recent_activity_count": 5,
                "lottery_repost_ratio": 0.1,
                "observable_account_days": 300,
            }
        }
    )
    seen = _install_profile_fetch(
        service,
        {
            2: {
                "uid": 2,
                "level": 5,
                "recent_activity_count": 4,
                "lottery_repost_ratio": 0.2,
                "observable_account_days": 200,
            }
        },
    )
    monkeypatch.setattr(
        "modules.lottery.service.classify_profiles", _ContractClassify(suspicious_uids={2})
    )

    # 外层 wait_for 作为长循环的双保险。
    result = asyncio.run(
        asyncio.wait_for(
            service.verify_winners(
                [{"uid": 1, "uname": "缓存用户"}, {"uid": 2, "uname": "新用户"}], "模板"
            ),
            timeout=5,
        )
    )

    assert result["winner_count"] == 2
    assert result["local_count"] == 1
    assert result["fetched_count"] == 1
    assert result["real_count"] == 1
    assert result["suspicious_count"] == 1
    assert seen == [2]
    # 在线补取的画像被回写缓存，便于下次复用。
    assert "2" in service._load_profile_cache()


def test_verify_winners_backfills_name_and_winner_fields(
    service: LotteryService, monkeypatch
) -> None:
    """画像缺失的昵称/等级应从缓存或中奖名单补齐，且不覆盖真实值。"""
    service._save_profile_cache(
        {
            "1": {
                "uid": 1,
                "level": 6,
                "recent_activity_count": 1,
                "lottery_repost_ratio": 0,
                "observable_account_days": 100,
            }
        }
    )
    monkeypatch.setattr("modules.lottery.service.classify_profiles", _ContractClassify())

    result = asyncio.run(
        asyncio.wait_for(
            service.verify_winners(
                [{"uid": 1, "uname": "中奖昵称", "level": 3, "ctime": "2024-05-01"}]
            ),
            timeout=5,
        )
    )

    profile = result["results"][0]["profile"]
    assert profile["name"] == "中奖昵称"
    # profile 已有 level=6，不得被中奖名单里的 level=3 覆盖。
    assert profile["level"] == 6
    assert profile["ctime"] == "2024-05-01"


# ================================================================ filter_real_users

def test_filter_real_users_dedupes_and_batches_profiles(
    service: LotteryService, monkeypatch
) -> None:
    """评论区去重后按 12 一批调用 AI 判定，并区分真人与可疑。"""
    comments = [
        {"uid": index, "uname": f"用户{index}", "rpid": f"r{index}"} for index in range(1, 14)
    ] + [{"uid": 1, "uname": "重复用户", "rpid": "dup"}]

    async def fake_get_comments(target, progress=None):
        """返回预置评论，避免真实采集。"""
        return comments, "local"

    service.get_comments = fake_get_comments
    _install_profile_fetch(service, {index: {"uid": index, "level": 6} for index in range(1, 14)})
    fake = _ContractClassify(suspicious_uids={13})
    monkeypatch.setattr("modules.lottery.service.classify_profiles", fake)
    progress = _ProgressRecorder()

    result = asyncio.run(
        asyncio.wait_for(
            service.filter_real_users(_video_target(), "模板", progress), timeout=5
        )
    )

    assert result["user_count"] == 13
    assert result["comment_count"] == 14
    assert result["data_source"] == "local"
    assert len(result["real_users"]) == 12
    assert len(result["suspicious_users"]) == 1
    # 13 位用户被切成 12 + 1 两批。
    assert [len(batch) for batch in fake.batches] == [12, 1]
    assert "profiling_users" in progress.stages
    assert "ai_classifying" in progress.stages


def test_filter_real_users_handles_empty_comment_pool(
    service: LotteryService, monkeypatch
) -> None:
    """无评论时不得除零崩溃，返回空结构。"""

    async def fake_get_comments(target, progress=None):
        """返回空评论列表。"""
        return [], "crawler"

    service.get_comments = fake_get_comments
    monkeypatch.setattr("modules.lottery.service.classify_profiles", _ContractClassify())

    result = asyncio.run(
        asyncio.wait_for(service.filter_real_users(_video_target(), None, None), timeout=5)
    )

    assert result["comment_count"] == 0
    assert result["user_count"] == 0
    assert result["real_users"] == []
    assert result["suspicious_users"] == []
    assert result["target"]["target_type"] == "video"


# ================================================================ 画像回写

def test_persist_comment_profiles_fills_missing_columns(
    service: LotteryService, isolated_db: DatabaseManager
) -> None:
    """应为同 UID 的缺字段评论原位补齐等级与会员信息。"""
    video_id = _seed_video(
        isolated_db,
        "BV300",
        [{"rpid": "p1", "uid": 42, "uname": "u", "content": "c", "ctime": datetime(2024, 5, 1)}],
    )

    service._persist_comment_profiles({42: {"level": 6, "vip": {"vipType": 1}}})

    session = isolated_db.get_session()
    try:
        row = session.query(Comment).filter(Comment.uid == 42).one()
        assert row.level_info == {"current_level": 6}
        assert row.vip == {"vipType": 1}
        assert row.video_id == video_id
    finally:
        session.close()


def test_persist_comment_profiles_keeps_existing_values(
    service: LotteryService, isolated_db: DatabaseManager
) -> None:
    """已有等级与会员的评论不得被覆盖。"""
    _seed_video(
        isolated_db,
        "BV301",
        [
            {
                "rpid": "p2",
                "uid": 43,
                "uname": "u",
                "content": "c",
                "ctime": datetime(2024, 5, 1),
                "level_info": {"current_level": 3},
                "vip": {"vipType": 0},
            }
        ],
    )

    service._persist_comment_profiles({43: {"level": 6, "vip": {"vipType": 2}}})

    session = isolated_db.get_session()
    try:
        row = session.query(Comment).filter(Comment.uid == 43).one()
        assert row.level_info == {"current_level": 3}
        assert row.vip == {"vipType": 0}
    finally:
        session.close()


def test_persist_comment_profiles_rolls_back_on_failure(
    service: LotteryService, monkeypatch
) -> None:
    """回写失败时必须回滚并释放会话，不向上抛异常。"""
    session = _ExplodingSession()
    monkeypatch.setattr("modules.lottery.service.get_session", lambda: session)

    service._persist_comment_profiles({42: {"level": 6, "vip": {"vipType": 1}}})

    assert session.rolled_back is True
    assert session.closed is True


# ================================================================ 元数据补全

def test_complete_draw_metadata_skips_when_already_complete(
    service: LotteryService
) -> None:
    """元数据齐备时直接返回副本，不触发任何补全请求。"""
    comments = [
        {
            "uid": 42,
            "level": 6,
            "is_vip": True,
            "vip_type": 1,
            "vip_label": "大会员",
            "ctime": "2024-05-01T00:00:00",
        }
    ]

    result = asyncio.run(asyncio.wait_for(service._complete_draw_metadata(comments), timeout=5))

    assert result == comments
    assert result is not comments


def test_complete_draw_metadata_merges_profile_cache(
    service: LotteryService, isolated_db: DatabaseManager
) -> None:
    """缺失字段应优先由画像缓存补全。"""
    service._save_profile_cache(
        {"42": {"level": 6, "is_vip": True, "vip_type": 1, "vip_label": "大会员"}}
    )
    comments = [
        {
            "uid": 42,
            "level": None,
            "is_vip": None,
            "vip_type": None,
            "vip_label": None,
            "ctime": "2024-05-01T00:00:00",
        }
    ]

    result = asyncio.run(asyncio.wait_for(service._complete_draw_metadata(comments), timeout=5))

    assert result[0]["level"] == 6
    assert result[0]["vip_label"] == "大会员"


def test_complete_draw_metadata_fetches_uncached_profiles(
    service: LotteryService, isolated_db: DatabaseManager
) -> None:
    """缓存未命中时应调用用户资料接口补全并回写缓存。"""

    async def fake_user_info(uid):
        """返回带等级与会员的用户资料。"""
        return {"data": {"level": 5, "vip": {"type": 1, "status": 1}}}

    service.api.get_user_info = fake_user_info
    comments = [
        {
            "uid": 77,
            "level": None,
            "is_vip": None,
            "vip_type": None,
            "vip_label": None,
            "ctime": "2024-05-01T00:00:00",
        }
    ]
    progress = _ProgressRecorder()

    result = asyncio.run(service._complete_draw_metadata(comments, progress))

    assert result[0]["level"] == 5
    assert result[0]["is_vip"] is True
    assert "77" in service._load_profile_cache()
    assert "completing_profiles" in progress.stages


def test_complete_draw_metadata_raises_when_still_unresolved(
    service: LotteryService, isolated_db: DatabaseManager
) -> None:
    """补全后仍缺关键字段时必须明确报错（从外部打异常）。"""

    async def failing_user_info(uid):
        """模拟资料接口失败。"""
        raise RuntimeError("风控")

    service.api.get_user_info = failing_user_info
    comments = [
        {
            "uid": 88,
            "level": None,
            "is_vip": None,
            "vip_type": None,
            "vip_label": None,
            "ctime": "2024-05-01T00:00:00",
        }
    ]

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(asyncio.wait_for(service._complete_draw_metadata(comments), timeout=5))

    assert "无法补齐 1 位候选用户" in str(excinfo.value)
    assert "88" in str(excinfo.value)


def test_resolve_missing_profiles_splits_cached_and_uncached(
    service: LotteryService
) -> None:
    """已缓存且字段完整的 UID 不重复请求接口。"""
    service._save_profile_cache({"1": {"level": 6, "is_vip": True}})
    seen = _install_profile_fetch(service, {})

    async def fake_user_info(uid):
        """返回资料，供未缓存 UID 使用。"""
        seen.append(uid)
        return {"data": {"level": 4, "vip": {"type": 0, "status": 0}}}

    service.api.get_user_info = fake_user_info

    profiles, cache, fetched = asyncio.run(
        asyncio.wait_for(service._resolve_missing_profiles({1, 2}, None), timeout=5)
    )

    assert fetched is True
    assert profiles[1] == {"level": 6, "is_vip": True}
    assert profiles[2]["level"] == 4
    assert seen == [2]


def test_resolve_missing_profiles_records_none_on_failure(
    service: LotteryService
) -> None:
    """单个 UID 资料失败时写入空占位，不阻断其它 UID。"""

    async def fake_user_info(uid):
        """对指定 UID 抛错。"""
        if uid == 5:
            raise RuntimeError("接口失败")
        return {"data": {"level": 6, "vip": {"type": 1, "status": 1}}}

    service.api.get_user_info = fake_user_info

    profiles, _, fetched = asyncio.run(
        asyncio.wait_for(service._resolve_missing_profiles({5, 6}, None), timeout=5)
    )

    assert profiles[5]["level"] is None
    assert profiles[5]["vip_label"] == "会员未知"
    assert profiles[6]["level"] == 6
    assert fetched is True


def test_resolve_missing_profiles_returns_false_without_uncached(
    service: LotteryService
) -> None:
    """全部命中缓存时不应标记执行过在线补取。"""
    service._save_profile_cache({"9": {"level": 6, "is_vip": False}})

    _, _, fetched = asyncio.run(
        asyncio.wait_for(service._resolve_missing_profiles({9}, None), timeout=5)
    )

    assert fetched is False


# ================================================================ draw

def _install_complete_local_comments(service: LotteryService, comments: list) -> None:
    """把 get_comments 替换为返回元数据完整评论的契约实现。"""

    async def fake_get_comments(target, progress=None):
        """返回预置评论。"""
        return [dict(item) for item in comments], "local"

    service.get_comments = fake_get_comments


def test_draw_samples_winners_from_filtered_pool(service: LotteryService) -> None:
    """抽奖应只从筛选后的候选池中随机抽取。"""
    _install_complete_local_comments(
        service,
        [
            _candidate(1, "c1", level=6, ctime="2024-05-01T00:00:00"),
            _candidate(2, "c2", level=1, ctime="2024-05-02T00:00:00"),
        ],
    )

    result = asyncio.run(
        asyncio.wait_for(
            service.draw(_video_target(), winner_count=1, unique_users=True, min_level=5),
            timeout=5,
        )
    )

    assert result["candidate_count"] == 1
    assert result["excluded"]["level"] == 1
    assert [item["rpid"] for item in result["winners"]] == ["c1"]
    assert result["data_source"] == "local"
    assert result["target"]["target_type"] == "video"


def test_draw_rejects_winner_count_over_pool(service: LotteryService) -> None:
    """抽奖人数超过候选池时必须报错（从外部打异常）。"""
    _install_complete_local_comments(
        service, [_candidate(1, "c1", level=6, ctime="2024-05-01T00:00:00")]
    )

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(service.draw(_video_target(), winner_count=2, unique_users=False))

    assert "筛选后有效候选仅 1 人" in str(excinfo.value)


def test_draw_serializes_filters_and_dates(service: LotteryService) -> None:
    """筛选条件回显应把 datetime 序列化为日期字符串。"""
    _install_complete_local_comments(
        service,
        [
            _candidate(1, "c1", level=6, ctime="2024-05-05T00:00:00"),
            _candidate(2, "c2", level=6, ctime="2024-05-06T00:00:00"),
        ],
    )

    result = asyncio.run(
        service.draw(
            _video_target(),
            winner_count=2,
            unique_users=True,
            vip_only=False,
            min_level=5,
            date_start=datetime(2024, 5, 1, 8, 30),
            date_end=datetime(2024, 5, 9, 23, 0),
        )
    )

    assert result["filters"] == {
        "unique_users": True,
        "vip_only": False,
        "min_level": 5,
        "date_start": "2024-05-01",
        "date_end": "2024-05-09",
        "real_only": False,
        "include_indeterminate": False,
    }
    assert len(result["winners"]) == 2


def test_draw_applies_vip_and_date_exclusions(service: LotteryService) -> None:
    """会员与时间窗条件应分别计入排除统计。"""
    _install_complete_local_comments(
        service,
        [
            _candidate(1, "c1", level=6, ctime="2024-05-05T00:00:00"),
            _candidate(2, "c2", level=6, ctime="2024-05-05T00:00:00", is_vip=False),
            _candidate(3, "c3", level=6, ctime="2024-01-01T00:00:00", is_vip=True),
        ],
    )

    result = asyncio.run(
        service.draw(
            _video_target(),
            winner_count=1,
            unique_users=True,
            vip_only=True,
            date_start=datetime(2024, 5, 1),
        )
    )

    assert result["candidate_count"] == 1
    assert result["excluded"]["vip"] == 1
    assert result["excluded"]["date"] == 1


def test_draw_with_zero_winners_returns_empty_list(service: LotteryService) -> None:
    """抽 0 人应正常返回空中奖名单，不抛异常。"""
    _install_complete_local_comments(
        service, [_candidate(1, "c1", level=6, ctime="2024-05-01T00:00:00")]
    )

    result = asyncio.run(service.draw(_video_target(), winner_count=0, unique_users=True))

    assert result["winners"] == []
    assert result["candidate_count"] == 1


def test_draw_rejects_empty_candidate_pool(service: LotteryService) -> None:
    """候选池为空时抽 1 人必须报错。"""
    _install_complete_local_comments(service, [])

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(service.draw(_video_target(), winner_count=1, unique_users=True))

    assert "有效候选仅 0 人" in str(excinfo.value)


def _candidate(uid: int, rpid: str, *, level: int, ctime: str, is_vip: bool = True) -> dict:
    """构造一条元数据完整的抽奖候选评论。"""
    return {
        "uid": uid,
        "rpid": rpid,
        "uname": f"用户{uid}",
        "level": level,
        "is_vip": is_vip,
        "vip_type": 1 if is_vip else 0,
        "vip_label": "大会员" if is_vip else "非会员",
        "ctime": ctime,
    }
