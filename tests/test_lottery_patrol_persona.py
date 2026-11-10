"""AI 惯用短句腔调语料：命中、累加封顶、口语降级、嘲讽排除、不越过“高度疑似”。"""
import pytest

from modules.lottery.patrol.contracts import build_settings
from modules.lottery.patrol.judge import FEW_SHOT_EXAMPLES, SYSTEM_PROMPT
from modules.lottery.patrol.rules import compile_rules, score_comment
from modules.lottery.patrol.rules_default import COMPUTED_WEIGHTS

S = build_settings({})
C = compile_rules()
STRONG = COMPUTED_WEIGHTS["text.persona_phrase.strong"]
WEAK = COMPUTED_WEIGHTS["text.persona_phrase.weak"]
CAP = COMPUTED_WEIGHTS["text.persona_phrase.cap"]


def _persona(text, name="普通用户", level=4):
    r = score_comment({"mid": 1, "uname": name, "content": text, "level": level, "is_reply": False,
                       "at_users": [], "is_vip": False, "official_type": -1}, C, S)
    hits = [h for h in r["hits"] if h["rule_id"] == "text.persona_phrase"]
    return r, (hits[0]["weight"] if hits else 0)


@pytest.mark.parametrize("text", [
    "这句话稳", "比之前的都重", "这个账我记住了", "睡吧，我守着", "你去，我跟", "问别人还是问自己",
    "什么心思都写在脸上", "这句话接的漂亮", "行，我认", "这句话我不接", "没接你那句话", "比之前的都强",
    "他记得这事", "这话我留着", "你这句话我记着", "这话是ta告诉你的，还是你自己想明白的",
    "声音没抖，但比刚才稳", "比刚才平", "这话我收下了", "说的比谁都响", "这次就是认了", "你自己掂量",
    "这个我认", "这个我认。", "我认", "我认。", "你说得对，我认", "这次失误，我认！",
])
def test_strong_phrases_hit(text):
    _r, w = _persona(text)
    assert w >= STRONG, text


@pytest.mark.parametrize("text", [
    "我认识他", "我认为这期不错", "我认真看完了", "我认错", "我认了吧哈哈", "我认不出来",
    "这个我认识", "我认得这个人", "这个我认为不行", "我认同", "我认输",
])
def test_wo_ren_everyday_words_not_counted(text):
    """「我认识 / 我认为 / 我认真」等日常说法不算「我认」腔调。"""
    _r, w = _persona(text)
    assert w == 0, text


def test_overlapping_phrases_counted_once():
    """「行，我认」同时含「我认」，同一段文字只计一次。"""
    _r, w = _persona("行，我认")
    assert w == STRONG


@pytest.mark.parametrize("text", [
    "我记下了", "我记住了", "我不拦", "我不催你", "认下来", "认这个", "收不回去", "认得实",
    "回去睡", "很平", "稳", "很直", "嘴比心沉", "话比人重",
])
def test_weak_phrases_hit_lightly(text):
    _r, w = _persona(text)
    assert w == WEAK, text


def test_multiple_phrases_add_up_but_capped():
    r, w = _persona("这句话稳。比之前的都重。这个账我记住了，你自己掂量。睡吧，我守着。")
    assert w == CAP
    assert r["rule_score"] < S.bot_threshold  # 只靠腔调软信号到不了“高度疑似”


def test_persona_plus_hard_hit_can_reach_bot():
    r, _w = _persona("本鲸记下了。这句话稳，比之前的都重，你自己掂量。")
    assert r["hard_groups"] and r["rule_score"] >= S.bot_threshold


@pytest.mark.parametrize("text", [
    "评论区一股AI味，张口就是我记下了这句话稳",
    "又是这套人机话术：这句话稳、我记住了",
    "这种话一看就是机器人写的：你自己掂量",
])
def test_mocking_ai_tone_not_counted(text):
    _r, w = _persona(text)
    assert w == 0, text


@pytest.mark.parametrize("text", [
    "回去睡觉了晚安", "这游戏真稳，一次没崩", "这个手机比上一代稳多了吧",
])
def test_everyday_sentences_not_counted(text):
    _r, w = _persona(text)
    assert w == 0, text


def test_colloquial_tone_downgrades_strong_phrase():
    _r, w = _persona("主播今天比之前的都强啊哈哈")
    assert w == WEAK


def test_persona_phrase_inside_prompt_and_examples():
    assert "这句话稳" in SYSTEM_PROMPT and "你自己掂量" in SYSTEM_PROMPT
    assert "单独出现不算证据" in SYSTEM_PROMPT
    texts = {e["text"]: e["expect_ai_score"] for e in FEW_SHOT_EXAMPLES}
    assert any(score >= 80 and "这句话稳" in t for t, score in texts.items())
    assert any(score <= 10 and "AI味" in t for t, score in texts.items())
