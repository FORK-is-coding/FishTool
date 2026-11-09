"""`_build_batch_similarity` 批次相似度工厂测试。

覆盖：默认实现与原始逐对调用等价、懒加载指纹缓存按文本计数、零/单条输入
不产生指纹工作、批次互不污染、原始文本直传、异常透传、实例/子类/类级
覆盖严格两参数透传（以模块级稳定常量判定替换）、替代工厂整体接管生产
管线、"真不错"/"真好"边界行为与补丁前一致（文档化局限非语义正确性）、
生产 `_deduplicate_by_fuzzy` 确实接入缓存并降低 SimHash 调用次数。
"""
from copy import deepcopy
from datetime import datetime
import random

import pytest

from modules.comment.deduplicator import CommentDeduplicator, _ORIGINAL_CALCULATE_SIMILARITY


def make_comment(rpid, uid, content, ctime=None, sentiment="neutral"):
    return {"rpid": rpid, "uid": uid, "uname": f"user-{uid}", "content": content,
            "ctime": ctime, "sentiment": sentiment}


def _distinct_text_dataset(n):
    """n 条字面互不相同的中文文本，用于验证缓存按文本数而非配对数计次。"""
    base_chars = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
    texts = []
    for i in range(n):
        chunk = base_chars[i % len(base_chars):] + base_chars[: i % len(base_chars) + 1]
        texts.append(f"评论第{i}条内容{chunk}结尾标记{i}")
    return texts


class _CountingDeduplicator(CommentDeduplicator):
    """统计 `_simhash` 实际调用次数，不改算法。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.simhash_calls = []

    def _simhash(self, text):
        self.simhash_calls.append(text)
        return super()._simhash(text)


FIXED_TEXT_PAIRS = [
    ("这个视频真不错看完很开心", "这个视频真好看完很开心"),
    ("今天天气晴朗适合出门散步", "今天天气晴朗适合出门散步"),
    ("什么时候更新下一集呢", "完全不相关的另外一段话内容"),
    ("", "非空文本内容测试用例"),
]


@pytest.mark.parametrize(("text1", "text2"), FIXED_TEXT_PAIRS)
def test_batch_similarity_matches_original_scorer_on_fixed_pairs(text1, text2):
    dedup = CommentDeduplicator()
    expected = dedup._calculate_similarity(text1, text2)
    batch_fn = dedup._build_batch_similarity([text1, text2])
    assert batch_fn(text1, text2) == expected


def test_batch_similarity_matches_original_scorer_on_distinct_dataset():
    dedup = CommentDeduplicator()
    texts = _distinct_text_dataset(12)
    batch_fn = dedup._build_batch_similarity(texts)
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            assert batch_fn(texts[i], texts[j]) == dedup._calculate_similarity(texts[i], texts[j])


def test_batch_similarity_caches_simhash_per_distinct_text():
    """100 条两两不同文本全量比较：_simhash 调用数应等于文本数（按文本缓存，不是按哈希值）。"""
    dedup = _CountingDeduplicator()
    texts = _distinct_text_dataset(100)
    batch_fn = dedup._build_batch_similarity(texts)
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            batch_fn(texts[i], texts[j])
    assert len(dedup.simhash_calls) == len(texts)
    assert set(dedup.simhash_calls) == set(texts)


def test_batch_similarity_cache_is_lazy_not_prepopulated():
    dedup = _CountingDeduplicator()
    dedup._build_batch_similarity(_distinct_text_dataset(20))
    assert dedup.simhash_calls == []


def test_batch_similarity_empty_input_does_no_fingerprint_work():
    dedup = _CountingDeduplicator()
    dedup._build_batch_similarity([])
    assert dedup.simhash_calls == []


def test_batch_similarity_single_short_input_does_no_fingerprint_work():
    dedup = _CountingDeduplicator()
    dedup._build_batch_similarity(["短"])
    assert dedup.simhash_calls == []


def test_repeated_batches_have_independent_caches():
    """批次局部缓存：两次独立构造各自懒加载，互不复用对方缓存。"""
    dedup = _CountingDeduplicator()
    texts = ["第一批次文本甲", "第一批次文本乙"]

    dedup._build_batch_similarity(texts)(texts[0], texts[1])
    assert set(dedup.simhash_calls) == set(texts)

    dedup.simhash_calls.clear()
    dedup._build_batch_similarity(texts)(texts[0], texts[1])
    assert set(dedup.simhash_calls) == set(texts)


def test_batch_similarity_receives_raw_unnormalized_text():
    dedup = CommentDeduplicator()
    raw1, raw2 = "Hello World", "hello world"
    expected = dedup._calculate_similarity(raw1, raw2)
    assert dedup._build_batch_similarity([raw1, raw2])(raw1, raw2) == expected


def test_batch_similarity_propagates_exceptions_from_simhash():
    class _BoomDeduplicator(CommentDeduplicator):
        def _simhash(self, text):
            if text == "boom":
                raise RuntimeError("simhash explode")
            return super()._simhash(text)

    dedup = _BoomDeduplicator()
    batch_fn = dedup._build_batch_similarity(["boom", "ok"])
    with pytest.raises(RuntimeError, match="simhash explode"):
        batch_fn("boom", "ok")


def test_batch_similarity_propagates_exceptions_from_custom_scorer():
    dedup = CommentDeduplicator()
    dedup._calculate_similarity = lambda t1, t2: (_ for _ in ()).throw(ValueError("custom scorer explode"))
    batch_fn = dedup._build_batch_similarity(["a", "b"])
    with pytest.raises(ValueError, match="custom scorer explode"):
        batch_fn("a", "b")


def test_instance_level_override_returned_as_is_strict_two_args():
    dedup = CommentDeduplicator()
    calls = []

    def _only_two_args(t1, t2):
        calls.append((t1, t2))
        return 0.42

    dedup._calculate_similarity = _only_two_args
    assert getattr(dedup._calculate_similarity, "__func__", None) is not _ORIGINAL_CALCULATE_SIMILARITY

    batch_fn = dedup._build_batch_similarity(["x", "y"])
    assert batch_fn("x", "y") == 0.42
    assert calls == [("x", "y")]
    assert batch_fn is dedup._calculate_similarity  # 实例属性本身是同一对象，可用 is


def test_subclass_override_returned_as_is_strict_two_args():
    class _FixedScoreDeduplicator(CommentDeduplicator):
        def _calculate_similarity(self, t1, t2):
            return 0.77

    dedup = _FixedScoreDeduplicator()
    assert getattr(dedup._calculate_similarity, "__func__", None) is not _ORIGINAL_CALCULATE_SIMILARITY

    batch_fn = dedup._build_batch_similarity(["foo", "bar"])
    # 绑定方法每次属性访问产生新对象，但 == 按 __self__/__func__ 比较，语义等价
    assert batch_fn == dedup._calculate_similarity
    assert batch_fn("foo", "bar") == 0.77


def test_class_level_monkeypatch_returned_as_is_strict_two_args(monkeypatch):
    """类级别整体打补丁：判定基准必须是模块常量，不能是当前（已被替换的）类属性。"""

    def _patched(self, t1, t2):
        return 0.55

    monkeypatch.setattr(CommentDeduplicator, "_calculate_similarity", _patched)
    dedup = CommentDeduplicator()
    assert getattr(dedup._calculate_similarity, "__func__", None) is not _ORIGINAL_CALCULATE_SIMILARITY

    batch_fn = dedup._build_batch_similarity(["m", "n"])
    assert batch_fn == dedup._calculate_similarity
    assert batch_fn("m", "n") == 0.55


def test_default_implementation_still_resolves_to_original_constant():
    dedup = CommentDeduplicator()
    assert getattr(dedup._calculate_similarity, "__func__", None) is _ORIGINAL_CALCULATE_SIMILARITY
    batch_fn = dedup._build_batch_similarity(["abc", "abd"])
    assert batch_fn != dedup._calculate_similarity  # 默认实现被包装，不是裸绑定方法


def test_alternative_factory_override_drives_real_pipeline():
    """调用方可整体覆盖 `_build_batch_similarity` 本身（未来向量化 hook 的真实落点），
    并通过真实 `_deduplicate_by_fuzzy` 管线验证接管生效，而非仅替换 `_calculate_similarity`。
    """

    class _FakeLocalScorerDeduplicator(CommentDeduplicator):
        def _build_batch_similarity(self, texts):
            def _fake_scorer(t1, t2):
                return 1.0 if t1 == t2 else 0.0
            return _fake_scorer

    dedup = _FakeLocalScorerDeduplicator(short_text_threshold=5, fuzzy_match_threshold=0.85)
    comments = [
        make_comment(1, 1, "完全相同的长评论内容"),
        make_comment(2, 2, "完全相同的长评论内容"),
        make_comment(3, 3, "另一条不同的长评论内容"),
    ]

    result, groups = dedup._deduplicate_by_fuzzy(comments)

    assert [c["rpid"] for c in result] == [1, 3]
    assert groups[0]["rpids"] == [1, 2]


def test_boundary_zhen_bucuo_vs_zhen_hao_behavior_documented_not_semantic():
    """等价优化不改变短评豁免；不把近义词识别当成当前算法能力。"""
    dedup = _CountingDeduplicator()
    comments = [make_comment(1, 1, "真不错"), make_comment(2, 2, "真好")]
    result = dedup.deduplicate(comments)
    assert result["deduplicated_count"] == 2
    assert result["fuzzy_groups"] == []
    assert dedup.simhash_calls == []


def test_fuzzy_dedup_pipeline_uses_batch_cache_and_cuts_simhash_calls():
    """生产管线必须真正接入批次工厂：100 条长评论下 `_simhash` 调用次数应
    等于参与比较的文本数而非配对数（4950），证明不是只加了个未使用的
    helper。数据集中个别文本本身字面高度相似会被正常折叠，这里不假设
    零折叠，只锁定"调用次数 == 文本数 << 配对数"这一缓存生效的核心信号。
    """
    dedup = _CountingDeduplicator(short_text_threshold=5, fuzzy_match_threshold=0.85)
    texts = _distinct_text_dataset(100)
    comments = [make_comment(i, i, t) for i, t in enumerate(texts)]

    result, groups = dedup._deduplicate_by_fuzzy(comments)

    assert len(result) + sum(g["count"] - 1 for g in groups) == 100
    pair_count = 100 * 99 // 2
    assert len(dedup.simhash_calls) == len(texts)
    assert len(dedup.simhash_calls) < pair_count


def test_fuzzy_dedup_pipeline_still_folds_similar_long_comments():
    dedup = CommentDeduplicator(short_text_threshold=5, fuzzy_match_threshold=0.85)
    comments = [
        make_comment(1, 1, "短评"),
        make_comment(2, 2, "这个视频内容非常清楚详细而且节奏舒服讲解认真让我看完收获很多"),
        make_comment(3, 3, "这个视频内容非常清楚详细而且节奏舒服讲解认真让我看完收获很多啊"),
    ]

    result, groups = dedup._deduplicate_by_fuzzy(comments)

    assert [item["rpid"] for item in result] == [1, 2]
    assert result[1].get("fuzzy_duplicate_count") == 2
    assert groups[0]["rpids"] == [2, 3]


def test_full_deduplicate_equivalent_with_subclass_factory_override():
    """子类覆盖 `_calculate_similarity`（旧式两参数 scorer）时，完整 `deduplicate()`
    结果须与该 scorer 预期一致，证明批次工厂在替换场景下严格透传、不破坏四层管线。
    """

    class _StartsWithScorer(CommentDeduplicator):
        def _calculate_similarity(self, t1, t2):
            return 0.9 if t1[:4] == t2[:4] else 0.0

    frozen_now = datetime(2026, 8, 22, 10, 0)
    dedup = _StartsWithScorer(short_text_threshold=5, fuzzy_match_threshold=0.8)
    comments = [
        make_comment(1, 10, "这个视频非常精彩", frozen_now, "positive"),
        make_comment(2, 11, "这个视频真的精彩", frozen_now, "positive"),
        make_comment(3, 12, "完全不一样的内容", frozen_now, "neutral"),
    ]

    result = dedup.deduplicate(comments)
    result.pop("processed_at", None)

    assert result["deduplicated_count"] == 2
    assert len(result["fuzzy_groups"]) == 1
    assert result["fuzzy_groups"][0]["rpids"] == [1, 2]


class _UncachedDeduplicator(CommentDeduplicator):
    """对照路径：保持旧两两循环每次现场计算的判定入口。"""

    def _build_batch_similarity(self, texts):
        return self._calculate_similarity


def _without_timestamp(result):
    return {key: value for key, value in result.items() if key != "processed_at"}


@pytest.mark.parametrize("seed", range(12))
def test_full_pipeline_matches_uncached_reference_on_seeded_batches(seed):
    """比较完整四层输出和原始输入，而不只比较相似度函数。"""
    rng = random.Random(seed)
    alphabet = "这段视频讲解内容非常清楚值得推荐学习不错不好继续更新啊！ ABCxyz123"
    long_text = "这个视频内容非常清楚详细而且节奏舒服讲解认真让我看完收获很多"
    negation = "我认为这个视频的讲解非常清楚而且内容详细节奏舒服值得大家认真看完并且推荐给身边的朋友"
    # 否定长句当前可能被折叠；这里仅验证前后相同，不把它当成语义正确。
    texts = ["真不错", "真好", " A B ", "ab", long_text, long_text + "啊",
             negation, negation.replace("值得", "不值得"), long_text, long_text]
    texts.extend("".join(rng.choices(alphabet, k=rng.randrange(0, 35))) for _ in range(10))
    comments = [make_comment(i, rng.randrange(4), text, datetime(2026, 11, 2, 10 + i % 3),
                             rng.choice(["positive", "negative", "neutral"]))
                for i, text in enumerate(texts)]
    original = deepcopy(comments)
    settings = {"short_text_threshold": rng.choice([0, 5, 8]),
                "fuzzy_match_threshold": rng.choice([0.7, 0.85, 0.95])}
    expected = _UncachedDeduplicator(**settings).deduplicate(deepcopy(comments))
    actual = CommentDeduplicator(**settings).deduplicate(comments)
    assert _without_timestamp(actual) == _without_timestamp(expected)
    assert comments == original


def test_zero_fingerprint_and_repeated_text_are_cached_by_key(monkeypatch):
    dedup = CommentDeduplicator()
    calls = []

    def zero_hash(text):
        calls.append(text)
        return 0

    monkeypatch.setattr(dedup, "_simhash", zero_hash)
    scorer = dedup._build_batch_similarity(["alpha", "alpha", "bravo"])
    assert scorer("alpha", "alpha") == 1.0
    scorer("alpha", "bravo")
    scorer("bravo", "alpha")
    assert calls == ["alpha", "bravo"]


@pytest.mark.parametrize("texts", [[], ["一条足够长的评论"], ["真好", "不错"]])
def test_pipeline_without_long_pairs_does_not_hash(texts):
    dedup = _CountingDeduplicator()
    comments = [make_comment(i, i, text) for i, text in enumerate(texts)]
    dedup.deduplicate(comments)
    assert dedup.simhash_calls == []


def test_pipeline_repeated_batches_and_failure_do_not_retain_cache(monkeypatch):
    dedup = _CountingDeduplicator()
    comments = [make_comment(1, 1, "这是一条完整的评论甲"),
                make_comment(2, 2, "这是一条完整的评论乙")]
    first = dedup.deduplicate(comments)
    calls = list(dedup.simhash_calls)
    dedup.simhash_calls.clear()
    assert _without_timestamp(dedup.deduplicate(comments)) == _without_timestamp(first)
    assert dedup.simhash_calls == calls

    original_hash = dedup._simhash

    def fail_second(text):
        if text == comments[1]["content"]:
            raise RuntimeError("temporary hash failure")
        return original_hash(text)

    monkeypatch.setattr(dedup, "_simhash", fail_second)
    with pytest.raises(RuntimeError, match="temporary hash failure"):
        dedup.deduplicate(comments)
    monkeypatch.setattr(dedup, "_simhash", original_hash)
    dedup.simhash_calls.clear()
    assert _without_timestamp(dedup.deduplicate(comments)) == _without_timestamp(first)
    assert dedup.simhash_calls == calls


def test_indexed_candidates_preserve_fuzzy_results_and_skip_distant_pairs(monkeypatch):
    from modules.comment import deduplicator as dedup_module

    rng = random.Random(41)
    # 覆盖跨不同分段的 0/1/2/3 位差异与 4 位粗筛边界。
    fingerprints = [0, 1, (1 << 16) | 1, (1 << 32) | (1 << 16) | 1,
                    (1 << 48) | (1 << 32) | (1 << 16) | 1]
    fingerprints.extend(rng.getrandbits(64) for _ in range(250))
    texts = [f"这是一条待比较的完整评论{i:04d}" for i in range(len(fingerprints))]
    by_text = dict(zip(texts, fingerprints))
    comments = [make_comment(i, i, text) for i, text in enumerate(texts)]
    optimized = CommentDeduplicator()
    reference = _UncachedDeduplicator()
    monkeypatch.setattr(optimized, "_simhash", by_text.__getitem__)
    monkeypatch.setattr(reference, "_simhash", by_text.__getitem__)

    batch = optimized._build_batch_similarity(texts)
    for i, fingerprint in enumerate(fingerprints):
        required = {j for j in range(i + 1, len(texts))
                    if (fingerprint ^ fingerprints[j]).bit_count() <= 3}
        assert required <= set(batch.candidates(texts, i))

    calls = []
    original_call = dedup_module._SimHashBatch.__call__

    def counted_call(self, a, b):
        calls.append((a, b))
        return original_call(self, a, b)

    monkeypatch.setattr(dedup_module._SimHashBatch, "__call__", counted_call)
    assert optimized._deduplicate_by_fuzzy(comments) == reference._deduplicate_by_fuzzy(comments)
    assert len(calls) < len(texts) * (len(texts) - 1) // 20

    # 0 分也能过门槛时，必须保留粗筛不通过的配对。
    optimized.fuzzy_match_threshold = reference.fuzzy_match_threshold = 0
    assert optimized._deduplicate_by_fuzzy(comments) == reference._deduplicate_by_fuzzy(comments)
