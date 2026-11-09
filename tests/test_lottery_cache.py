"""抽奖本地 JSON 缓存的契约级测试。

覆盖 modules/lottery/cache.py 的全部公开方法、容错分支与原子写入语义。
所有落盘一律使用 pytest 的 tmp_path，绝不读写项目 data/ 目录。
"""

import json
from pathlib import Path

import pytest

from modules.lottery.cache import LotteryCache, snapshot_is_reusable


# ------------------------------------------------------------------ 夹具

@pytest.fixture()
def cache(tmp_path: Path) -> LotteryCache:
    """构造指向临时目录的真实缓存仓储对象。"""
    return LotteryCache(tmp_path / "lottery_cache")


# ------------------------------------------------- 路径推导

def test_batch_merge_and_lookup_match_single_path(cache: LotteryCache) -> None:
    """批量写入的分组结果与单条 begin/merge 路径同一命中规则：成功即 hit，失败保留旧值但不命中。"""
    now = 1_700_000_000
    cache.merge_profiles_batch(
        {1: {"info": {"level": 6}, "videos": {"video_count": 3}}, 2: None},
        namespace="full", groups=["info", "videos"], now_s=now,
    )
    lookups = cache.get_profiles_batch([1, 2, 3], namespace="full", now_s=now + 10, required_groups=["info", "videos"])

    assert lookups[1].cache_state == "hit"
    assert lookups[1].profile == {"level": 6, "video_count": 3}
    assert lookups[2].cache_state == "stale"
    assert lookups[3].cache_state == "miss"
    # 与单条查询结果一致。
    single = cache.get_profile(1, namespace="full", now_s=now + 10, required_groups=["info", "videos"])
    assert single == lookups[1]


def test_batch_merge_failure_keeps_previous_success(cache: LotteryCache) -> None:
    """批量登记失败时保留历史成功值（不命中），与 merge_attempt 失败分支一致。"""
    now = 1_700_000_000
    cache.merge_profiles_batch({1: {"info": {"level": 5}}}, namespace="full", groups=["info"], now_s=now)
    cache.merge_profiles_batch({1: None}, namespace="full", groups=["info"], now_s=now + 5)
    lookup = cache.get_profiles_batch([1], namespace="full", now_s=now + 6, required_groups=["info"])[1]

    assert lookup.cache_state == "stale"
    assert lookup.profile == {"level": 5}


def test_batch_merge_writes_file_once_per_batch(cache: LotteryCache, monkeypatch) -> None:
    """一批多用户只写一次画像文件，避免真人筛选时整份文件被反复重写。"""
    writes = []
    original = cache._write_profile_doc

    def counting_write(doc):
        writes.append(1)
        original(doc)

    monkeypatch.setattr(cache, "_write_profile_doc", counting_write)
    cache.merge_profiles_batch(
        {uid: {"info": {"level": 6}} for uid in range(1, 21)}, namespace="full", groups=["info"], now_s=1_700_000_000
    )
    assert writes == [1]


def test_assessments_round_trip_and_expire(cache: LotteryCache, monkeypatch) -> None:
    """AI 判定按键保存与读取；过期判定读不到，并在下次写入时清理。"""
    import modules.lottery.cache as cache_module

    clock = {"now": 1_700_000_000}
    monkeypatch.setattr(cache_module.time, "time", lambda: clock["now"])
    cache.save_assessments({"k1": {"uid": 1, "classification": "real", "source": "llm"}})

    assert cache.load_assessments(["k1", "k2"]) == {"k1": {"uid": 1, "classification": "real", "source": "llm"}}
    clock["now"] += cache_module._ASSESSMENT_TTL_S + 1
    assert cache.load_assessments(["k1"]) == {}
    cache.save_assessments({"k2": {"uid": 2, "classification": "suspicious", "source": "llm"}})
    stored = json.loads(cache.profile_path().read_text(encoding="utf-8"))["assessments"]
    assert set(stored) == {"k2"}


def test_paths_are_derived_from_cache_dir(cache: LotteryCache, tmp_path: Path) -> None:
    """动态路径与画像路径应稳定挂载在注入的缓存目录下。"""
    root = tmp_path / "lottery_cache"

    assert cache.dynamic_path("D123") == root / "dynamic_D123.json"
    assert cache.profile_path() == root / "user_profiles.json"
    # 只做路径计算，不应产生任何目录副作用。
    assert not root.exists()


# ------------------------------------------------- 动态评论缓存

def test_load_dynamic_comments_returns_empty_when_file_missing(cache: LotteryCache) -> None:
    """缓存文件不存在时返回空列表，而不是抛异常。"""
    assert cache.load_dynamic_comments("D404") == []


def test_save_then_load_dynamic_comments_round_trip(cache: LotteryCache) -> None:
    """保存后应能原样读回评论列表，并落有保存时间戳。"""
    comments = [{"rpid": "1", "uid": 42, "content": "抽奖"}, {"rpid": "2", "uid": 43}]

    cache.save_dynamic_comments("D100", comments)

    assert cache.load_dynamic_comments("D100") == comments
    payload = json.loads(cache.dynamic_path("D100").read_text(encoding="utf-8"))
    assert payload["comments"] == comments
    assert isinstance(payload["saved_at"], str) and payload["saved_at"]


def test_save_is_atomic_and_leaves_no_tmp_file(cache: LotteryCache) -> None:
    """原子写入完成后不应残留同目录临时文件。"""
    cache.save_dynamic_comments("D101", [{"rpid": "9"}])

    leftovers = list(cache.dynamic_path("D101").parent.glob("*.tmp"))
    assert leftovers == []


def test_load_dynamic_comments_ignores_non_dict_payload(cache: LotteryCache) -> None:
    """顶层不是 JSON 对象时按损坏处理，返回空列表。"""
    path = cache.dynamic_path("D_list")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert cache.load_dynamic_comments("D_list") == []


def test_load_dynamic_comments_ignores_non_list_comments(cache: LotteryCache) -> None:
    """comments 字段不是列表时返回空列表。"""
    path = cache.dynamic_path("D_str")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"comments": "坏数据"}), encoding="utf-8")

    assert cache.load_dynamic_comments("D_str") == []


def test_load_dynamic_comments_survives_broken_json(cache: LotteryCache) -> None:
    """JSON 语法损坏时应降级为空列表并记录日志。"""
    path = cache.dynamic_path("D_broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{不是合法 JSON", encoding="utf-8")

    assert cache.load_dynamic_comments("D_broken") == []


def test_load_dynamic_comments_survives_oserror(cache: LotteryCache) -> None:
    """目标是目录导致 OSError 时应降级为空列表。"""
    # 把缓存目标建成目录，read_text 会抛 IsADirectoryError（OSError 子类）。
    cache.dynamic_path("D_dir").mkdir(parents=True, exist_ok=True)

    assert cache.load_dynamic_comments("D_dir") == []


# ------------------------------------------------- 评论快照完整性（issue #5-6）

_NOW = 1_800_000_000


@pytest.mark.parametrize(
    "snapshot, ttl, expected",
    [
        ({"complete": True, "saved_epoch_s": _NOW}, 600, True),
        ({"complete": True, "saved_epoch_s": _NOW - 600}, 600, True),
        # 过期。
        ({"complete": True, "saved_epoch_s": _NOW - 601}, 600, False),
        # 部分采集。
        ({"complete": False, "saved_epoch_s": _NOW}, 600, False),
        # 旧格式：没有完整性标记与秒级时间戳。
        ({"saved_at": "2026-01-01T00:00:00", "comments": []}, 600, False),
        ({"complete": True}, 600, False),
        # 标记必须是真布尔，不接受字符串/整数等“看起来为真”的值。
        ({"complete": "true", "saved_epoch_s": _NOW}, 600, False),
        ({"complete": 1, "saved_epoch_s": _NOW}, 600, False),
        # 时间戳类型异常或来自未来。
        ({"complete": True, "saved_epoch_s": str(_NOW)}, 600, False),
        ({"complete": True, "saved_epoch_s": float(_NOW)}, 600, False),
        ({"complete": True, "saved_epoch_s": _NOW + 5}, 600, False),
        # TTL 非法。
        ({"complete": True, "saved_epoch_s": _NOW}, 0, False),
        # 非字典。
        (None, 600, False),
        ([], 600, False),
    ],
)
def test_snapshot_is_reusable_table(snapshot, ttl, expected) -> None:
    """只有近期且显式完整的快照可复用。"""
    assert snapshot_is_reusable(snapshot, _NOW, ttl) is expected


def test_save_dynamic_comments_records_completeness(cache: LotteryCache) -> None:
    """保存动态缓存时应记录完整性与秒级时间戳，默认按不完整保存。"""
    cache.save_dynamic_comments("D200", [{"rpid": "1"}])
    cache.save_dynamic_comments("D201", [{"rpid": "2"}], complete=True)

    partial = cache.load_dynamic_snapshot("D200")
    full = cache.load_dynamic_snapshot("D201")
    assert partial["complete"] is False
    assert full["complete"] is True
    assert full["comments"] == [{"rpid": "2"}]
    assert type(full["saved_epoch_s"]) is int


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"saved_at": "2026-01-01T00:00:00", "comments": [{"rpid": "old"}]}),
        json.dumps({"complete": "yes", "saved_epoch_s": "坏值", "comments": "坏数据"}),
    ],
)
def test_load_dynamic_snapshot_tolerates_legacy_payload(cache: LotteryCache, raw: str) -> None:
    """旧格式或字段异常的历史文件不抛异常，且按不完整处理。"""
    path = cache.dynamic_path("D_old")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")

    snapshot = cache.load_dynamic_snapshot("D_old")

    assert snapshot["complete"] is False
    assert isinstance(snapshot["comments"], list)
    assert snapshot_is_reusable(snapshot, _NOW, 600) is False
    # 旧读取入口保持兼容。
    assert isinstance(cache.load_dynamic_comments("D_old"), list)


@pytest.mark.parametrize("raw", ["{坏 JSON", json.dumps([1, 2])])
def test_load_snapshots_return_none_when_corrupted(cache: LotteryCache, raw: str) -> None:
    """快照文件损坏或顶层不是对象时返回 None。"""
    for path in (cache.dynamic_path("D_bad"), cache.video_snapshot_path("BV_bad")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")

    assert cache.load_dynamic_snapshot("D_bad") is None
    assert cache.load_video_snapshot("BV_bad") is None


def test_video_snapshot_round_trip(cache: LotteryCache, tmp_path: Path) -> None:
    """视频快照标记可往返，缺失时返回 None。"""
    assert cache.video_snapshot_path("BV1") == tmp_path / "lottery_cache" / "video_BV1.json"
    assert cache.load_video_snapshot("BV1") is None

    cache.mark_video_snapshot("BV1", 3)

    snapshot = cache.load_video_snapshot("BV1")
    assert snapshot["complete"] is True
    assert snapshot["comment_count"] == 3
    assert type(snapshot["saved_epoch_s"]) is int


# ------------------------------------------------- 用户画像缓存

def test_load_profiles_returns_empty_when_file_missing(cache: LotteryCache) -> None:
    """画像文件缺失时返回空字典。"""
    assert cache.load_profiles() == {}


def test_save_then_load_profiles_round_trip(cache: LotteryCache) -> None:
    """画像保存后应能读回，且忽略 saved_at 之外的包装结构。"""
    profiles = {"42": {"level": 5, "is_vip": True, "vip_label": "大会员"}}

    cache.save_profiles(profiles)

    assert cache.load_profiles() == profiles


def test_load_profiles_ignores_non_dict_payload(cache: LotteryCache) -> None:
    """画像顶层不是对象时返回空字典。"""
    path = cache.profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(["a", "b"]), encoding="utf-8")

    assert cache.load_profiles() == {}


def test_load_profiles_survives_broken_json(cache: LotteryCache) -> None:
    """画像 JSON 损坏时返回空字典。"""
    path = cache.profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json", encoding="utf-8")

    assert cache.load_profiles() == {}


# ------------------------------------------------- 写入失败容错

def test_write_json_swallows_type_error(tmp_path: Path) -> None:
    """不可序列化数据只记录日志，不向上抛异常。"""
    cache = LotteryCache(tmp_path / "cache")
    # set 无法被 json.dumps 序列化，触发 TypeError 分支。
    cache.save_profiles({"42": {"tags": {"a", "b"}}})

    assert not cache.profile_path().exists()


def test_write_json_swallows_oserror_when_dir_is_file(tmp_path: Path) -> None:
    """缓存目录被同名文件占用时，mkdir 报错应被吞掉。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    cache = LotteryCache(blocker)

    # 不应抛异常，仅记录 warning。
    cache.save_profiles({"42": {"level": 6}})

    assert blocker.is_file()


def test_write_json_creates_missing_cache_dir(tmp_path: Path) -> None:
    """首次写入应自动创建多级缓存目录。"""
    target_dir = tmp_path / "a" / "b" / "c"
    cache = LotteryCache(target_dir)

    cache.save_profiles({"1": {"level": 1}})

    assert target_dir.is_dir()
    assert cache.profile_path().exists()
