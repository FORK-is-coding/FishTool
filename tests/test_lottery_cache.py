"""抽奖本地 JSON 缓存的契约级测试。

覆盖 modules/lottery/cache.py 的全部公开方法、容错分支与原子写入语义。
所有落盘一律使用 pytest 的 tmp_path，绝不读写项目 data/ 目录。
"""

import json
from pathlib import Path

import pytest

from modules.lottery.cache import LotteryCache


# ------------------------------------------------------------------ 夹具

@pytest.fixture()
def cache(tmp_path: Path) -> LotteryCache:
    """构造指向临时目录的真实缓存仓储对象。"""
    return LotteryCache(tmp_path / "lottery_cache")


# ------------------------------------------------- 路径推导

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
