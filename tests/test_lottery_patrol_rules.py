"""AI 伪人巡检规则引擎：五个重点词的字段权重、误报样例、封顶、跨评论特征。"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from modules.lottery.patrol.contracts import build_settings
from modules.lottery.patrol.rules import (
    apply_cross_features,
    compile_rules,
    normalize_text,
    rule_signal_labels,
    score_comment,
)

S = build_settings({})
COMPILED = compile_rules()
NEGATIVE_FIXTURE = Path(__file__).parent / "fixtures" / "patrol_negative_samples.json"


def _c(name="普通用户", sign="", text="正常评论内容", level=4, **kw):
    base = {"mid": kw.pop("mid", 1), "uname": name, "sign": sign, "level": level, "is_vip": False,
            "official_type": -1, "content": text, "is_reply": False, "at_users": [], "rpid": kw.pop("rpid", 1)}
    base.update(kw)
    return base


def _score(**kw):
    return score_comment(_c(**kw), COMPILED, S)


def _ids(result):
    return {h["rule_id"] for h in result["hits"]}


def _hard(result):
    return bool(result["hard_groups"])


# ---------- 归一化 ----------

@pytest.mark.parametrize("raw", ["大.肥.鱼", "大_肥_魚", "大 肥 鱼", "大​肥‍鱼"])
def test_name_normalization_variants_hit(raw):
    assert "name.dafeiyu" in _ids(_score(name=raw))


def test_fullwidth_deepseek_hits():
    assert "name.deepseek" in _ids(_score(name="ＤｅｅｐＳｅｅｋ小助手"))
    assert normalize_text("ＡＢＣ") == "abc"


# ---------- 五个重点词：昵称强、正文弱 ----------

def test_dafeiyu_counts_once_in_group():
    r = _score(name="大肥鱼")
    weights = [h["weight"] for h in r["hits"] if h["group"] == "G_feiyu"]
    assert weights == [45]


def test_name_and_sign_same_group_not_stacked():
    r = _score(name="大肥鱼", sign="我是大肥鱼")
    assert [h["weight"] for h in r["hits"] if h["group"] == "G_feiyu"] == [45]


def test_sign_only_uses_factor():
    r = _score(sign="小明家的大肥鱼")
    g = [h for h in r["hits"] if h["group"] == "G_feiyu"]
    assert g and g[0]["field"] == "sign" and g[0]["weight"] == 36


@pytest.mark.parametrize("name,rule_id", [
    ("阿杰的大肥鱼", "name.dafeiyu"), ("鲸鱼娘小蓝", "name.feiyu"), ("deepseek粉丝", "name.deepseek"),
    ("小明家的DSH", "name.dsh"), ("吃白饭的鲸", "name.baifan"), ("养鱼达人", "name.yangyu"),
])
def test_focus_words_in_name(name, rule_id):
    assert rule_id in _ids(_score(name=name))


@pytest.mark.parametrize("text", ["deepseek 新版本比上一版好用吗？", "用 DeepSeek 写代码挺方便的"])
def test_deepseek_in_text_scores_zero(text):
    r = _score(text=text)
    assert sum(h["weight"] for h in r["hits"] if h["group"] == "S_ds") == 0
    assert r["rule_score"] < S.suspect_threshold


def test_drive_away_context_is_human():
    r = _score(text="评论区大肥鱼太多了，回家吃白饭吧")
    assert "human.drive_away" in _ids(r)
    assert r["rule_score"] == 0


# ---------- 边界匹配 ----------

@pytest.mark.parametrize("name,hit", [("Kdsh_2001", False), ("小明dsh", True), ("dsh", True), ("ds h", True)])
def test_dsh_boundary(name, hit):
    assert ("name.dsh" in _ids(_score(name=name))) is hit


@pytest.mark.parametrize("name,hit", [("阿杰的大肥鱼", True), ("会飞的鱼", False), ("快乐的小鲸鱼呀", False)])
def test_owner_of_only_at_end(name, hit):
    assert ("name.owner_of" in _ids(_score(name=name))) is hit


@pytest.mark.parametrize("text", [
    "作为AI从业者说一句，这期讲得很准", "我是AI工程师，这个思路不对", "今天去日本鱼生店吃了刺身",
    "日本鲸类保护很严格", "DeepSeek Harness 好用吗", "肚子咕噜咕噜叫", "作为班主任助手我觉得",
])
def test_known_false_positives_not_hard(text):
    assert not _hard(_score(text=text))


@pytest.mark.parametrize("text", [
    "本鲸记下了，下次还来", "我是一只AI，但我也爱看这个", "我住在DSH里每天看视频",
    "要是想语气再欠一点，可以用这句：……", "好的，以下是给你的评论：这期真不错",
])
def test_hard_hits(text):
    assert _hard(_score(text=text))


@pytest.mark.parametrize("text,hit", [("666", True), ("01:46", False), ("哈哈哈", True), ("这里6分钟的地方讲得好细", False)])
def test_short_slang(text, hit):
    assert ("human.short_slang" in _ids(_score(text=text))) is hit


# ---------- 汇总口径 ----------

def test_soft_only_never_reaches_bot():
    r = _score(name="阿杰的大肥鱼🐳", sign="deepseek 吃白饭 养鱼", level=1,
               text="@阿杰 这条值得看，01:40 和 03:20 两段讲得很清楚 ×19 **重点**（尾巴摇了摇）",
               at_users=[{"mid": 2, "uname": "阿杰"}])
    assert not _hard(r)
    assert r["rule_score"] <= S.soft_cap < S.bot_threshold


def test_hard_plus_soft_can_reach_bot():
    r = _score(name="阿杰的大肥鱼", level=2, text="本鲸今天也在认真看视频哦，主人记得给我充token")
    assert _hard(r) and r["rule_score"] >= S.bot_threshold


def test_design_doc_examples():
    assert _score(name="阿杰的大肥鱼", level=2)["rule_score"] == 69
    r = _score(name="阿杰的大肥鱼", level=2, text="@阿杰 这条值得看", at_users=[{"mid": 2, "uname": "阿杰"}])
    assert r["rule_score"] == 70


def test_whitelist_owner_and_self():
    assert score_comment(_c(name="大肥鱼", mid=7), COMPILED, S, whitelist={7})["source"] == "whitelist"
    assert score_comment(_c(name="大肥鱼", mid=8), COMPILED, S, owner_mid=8)["rule_score"] == 0
    assert score_comment(_c(name="大肥鱼", mid=9), COMPILED, S, self_mid=9)["rule_score"] == 0


def test_account_signals():
    assert "acct.official" in _ids(score_comment({**_c(), "official_type": 0}, COMPILED, S))
    assert "acct.lv6" in _ids(_score(level=6))
    assert "acct.low_level" in _ids(_score(level=1))
    assert not ({"acct.low_level", "acct.lv6"} & _ids(_score(level=None)))


# ---------- 用户关键词 ----------

def test_user_keywords_literal_and_clamped():
    compiled = compile_rules([{"word": "a.b", "field": "text", "weight": 50}])
    hit = score_comment(_c(text="xx a.b yy"), compiled, S)
    miss = score_comment(_c(text="xx axb yy"), compiled, S)
    user_hits = [h for h in hit["hits"] if h["rule_id"].startswith("user.")]
    assert user_hits and user_hits[0]["weight"] == 30 and user_hits[0]["kind"] == "soft"
    assert not [h for h in miss["hits"] if h["rule_id"].startswith("user.")]


def test_user_keywords_invalid_ignored():
    compiled = compile_rules([{"word": "", "field": "text", "weight": 5}, {"word": "x", "field": "bad", "weight": 5},
                              {"word": "y" * 21, "field": "text", "weight": 5}, "junk"])
    assert not [c for c in compiled if c.rule.rule_id.startswith("user.")]


# ---------- 跨评论 ----------

def _scored(**kw):
    c = _c(**kw)
    c.update(score_comment(c, COMPILED, S))
    return c


def test_cross_copy_marks_both_different_mids():
    text = "这期视频讲得非常清楚，散热测试和噪音对比都很有参考价值"
    items = [_scored(mid=i, rpid=i, text=text) for i in (1, 2, 3)]
    apply_cross_features(items, S)
    assert all("cross.copy" in _ids(it) for it in items)


def test_cross_copy_ignores_same_mid_and_short():
    text = "这期视频讲得非常清楚，散热测试和噪音对比都很有参考价值"
    items = [_scored(mid=1, rpid=1, text=text), _scored(mid=1, rpid=2, text=text),
             _scored(mid=2, rpid=3, text="短评"), _scored(mid=3, rpid=4, text="短评")]
    apply_cross_features(items, S)
    assert not any("cross.copy" in _ids(it) for it in items)


def test_same_account_burst():
    t0 = datetime(2026, 11, 1, 12, 0, 0)
    items = [_scored(mid=5, rpid=i, text=f"第{i}条", ctime=t0 + timedelta(seconds=30 * i)) for i in range(3)]
    apply_cross_features(items, S)
    assert all("cross.same_account_burst" in _ids(it) for it in items)


def test_called_out_capped_at_15():
    bot = _scored(mid=100, rpid=1, name="阿杰的大肥鱼", text="这期值得看")
    callers = [_scored(mid=200 + i, rpid=10 + i, text="大肥鱼滚啊，别刷了", is_reply=True,
                       parent_rpid=1, root_rpid=1) for i in range(3)]
    items = [bot] + callers
    apply_cross_features(items, S)
    called = [h for h in bot["hits"] if h["rule_id"] == "cross.called_out"]
    assert called and called[0]["weight"] == 15


def test_called_out_requires_target_focus_hit():
    human = _scored(mid=100, rpid=1, name="普通人", text="这期值得看")
    items = [human, _scored(mid=201, rpid=11, text="大肥鱼滚", is_reply=True, parent_rpid=1, root_rpid=1)]
    apply_cross_features(items, S)
    assert "cross.called_out" not in _ids(human)


def test_rule_signal_labels_positive_only():
    r = _score(name="阿杰的大肥鱼", level=6)
    labels = rule_signal_labels(r["hits"])
    assert "昵称含重点词「大肥鱼」" in labels and "账号 Lv6" not in labels


# ---------- 负样本回归集 ----------

def _negative_samples():
    if not NEGATIVE_FIXTURE.exists():
        pytest.skip("负样本集尚未生成")
    return json.loads(NEGATIVE_FIXTURE.read_text("utf-8"))


def test_negative_samples_never_bot():
    samples = _negative_samples()
    assert len(samples) >= 200
    offenders = []
    for i, s in enumerate(samples):
        r = score_comment(_c(name=s.get("name", ""), sign=s.get("sign", ""), text=s.get("text", ""),
                             level=s.get("level", 4), mid=1000 + i, rpid=1000 + i), COMPILED, S)
        if r["rule_score"] >= S.bot_threshold or r["hard_groups"]:
            offenders.append((s, r["rule_score"], sorted(_ids(r))))
        if s.get("note") == "驱赶类" and r["rule_score"] >= S.suspect_threshold:
            offenders.append((s, r["rule_score"], "驱赶类进入疑似档"))
    assert offenders == []
