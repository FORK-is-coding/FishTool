"""AI 伪人巡检规则引擎：文本归一化、单条打分、跨评论特征。

全部是纯函数（除跨评论阶段复用 CommentDeduplicator 的相似度算法），无网络 / 数据库访问。
打分口径见设计稿 §5.3：

    hard  = Σ 硬命中（同组取一次）
    soft  = min(soft_cap, Σ 正向软信号（同组取最高）)
    minus = Σ 负向信号
    rule_score = clamp(hard + soft + minus, 0, 99)

只靠软信号永远到不了 bot 档（soft_cap < bot 阈值），必须有硬命中或经过 AI 甄别。
"""
import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .contracts import PatrolSettings
from .rules_default import (
    COLLOQUIAL_PATTERN,
    COMPUTED_LABELS,
    COMPUTED_WEIGHTS,
    DEFAULT_RULES,
    DRIVE_AWAY_PATTERN,
    FOCUS_TEXT_PATTERN,
    PERSONA_MOCK_PATTERN,
    PERSONA_PHRASES,
    RECOMMEND_PATTERN,
    SIGN_FACTOR,
    Rule,
)

#: 繁体 → 简体（只覆盖规则相关字）。
_T2S = str.maketrans({"魚": "鱼", "鯨": "鲸", "飯": "饭", "養": "养", "鯤": "鲲", "藍": "蓝", "機": "机", "滾": "滚"})
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_NAME_SEPARATORS = re.compile(r"[\s.\-_·~★☆•|/\\]+")
_MAX_TEXT = 500

_DRIVE_RE = re.compile(DRIVE_AWAY_PATTERN)
_FOCUS_RE = re.compile(FOCUS_TEXT_PATTERN)
_RECOMMEND_RE = re.compile(RECOMMEND_PATTERN)
_COLLOQUIAL_RE = re.compile(COLLOQUIAL_PATTERN)
_TIMESTAMP_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")
_LIST_RE = re.compile(r"(?:^|[\s\n，。;；])(?:[1-9][.、)）]|[①②③④⑤])")
_SEQUENCE_RE = re.compile(r"首先.*其次.*(?:最后|总之)", re.S)
#: “6”只在单独成句（可带标点 / 空白）时算口语，避免“6分钟”“01:46”误判。
_SLANG_RE = re.compile(r"哈哈|绷不住|草|笑死|^[\s!！~～.。]*6{1,3}[\s!！~～.。]*$")
_FORMAL_RE = re.compile(r"——|「|」")
_PERSONA_RES = tuple((label, re.compile(pattern), tier) for label, pattern, tier in PERSONA_PHRASES)
_PERSONA_MOCK_RE = re.compile(PERSONA_MOCK_PATTERN)


def persona_phrase_hit(text_norm: str, raw_text: str) -> Optional["Hit"]:
    """AI 惯用短句腔调：同一条评论命中的不同短语累加，合计封顶。

    评论在嘲讽 / 引用 AI 腔（含「AI 味」「人机」「又是这套」等）时不计分，避免误伤吐槽者。
    """
    if not text_norm or _PERSONA_MOCK_RE.search(text_norm):
        return None
    found = [(label, regex.search(text_norm), tier) for label, regex, tier in _PERSONA_RES]
    found = [(label, m, tier) for label, m, tier in found if m]
    if not found:
        return None
    # 同一段文字只计一次：重叠的命中保留更长的那条（如「行，我认」与「我认」只计前者）。
    found.sort(key=lambda item: -(item[1].end() - item[1].start()))
    kept: List[Tuple[str, Any, str]] = []
    for label, m, tier in found:
        if all(m.end() <= k.start() or m.start() >= k.end() for _l, k, _t in kept):
            kept.append((label, m, tier))
    found = sorted(kept, key=lambda item: item[1].start())
    # AI 腔调短促、不带语气词。去掉命中的腔调片段后，剩余文字仍带「哈 / 啊 / 吧 / 呢 / 嘛」等口语词时，
    # 整句腔调只按弱信号计（“睡吧，我守着”本身含「吧」，不能因此降级）。
    rest = text_norm
    for _label, m, _tier in found:
        rest = rest.replace(m.group(0), " ")
    colloquial = bool(_COLLOQUIAL_RE.search(rest))
    total = 0
    matched: List[str] = []
    first_snippet = ""
    for label, m, tier in found:
        total += COMPUTED_WEIGHTS["text.persona_phrase.weak" if colloquial else f"text.persona_phrase.{tier}"]
        matched.append(label)
        if not first_snippet:
            first_snippet = _snippet(raw_text, m.group(0))
    if not matched:
        return None
    weight = min(COMPUTED_WEIGHTS["text.persona_phrase.cap"], total)
    group, kind, base_label = COMPUTED_LABELS["text.persona_phrase"]
    shown = "、".join(matched[:3]) + ("等" if len(matched) > 3 else "")
    return Hit("text.persona_phrase", group, "text", weight, kind, f"AI 惯用短句腔调：{shown}", first_snippet)


@dataclass
class Hit:
    """一条规则命中。"""

    rule_id: str
    group: str
    field: str
    weight: int
    kind: str
    label: str
    snippet: str = ""


@dataclass(frozen=True)
class CompiledRule:
    rule: Rule
    regex: "re.Pattern[str]"


def normalize_text(value: Optional[str], *, is_name: bool = False) -> str:
    """规则匹配前的统一归一化。"""
    text = unicodedata.normalize("NFKC", value or "")
    text = text.translate(_T2S)
    text = _ZERO_WIDTH.sub("", text)
    text = text.lower()
    if is_name:
        text = _NAME_SEPARATORS.sub("", text)
    return text[:_MAX_TEXT]


def _user_keyword_rules(user_keywords: Optional[Sequence[Dict[str, Any]]]) -> List[Rule]:
    """用户自定义关键词 → 规则；只按字面匹配，权重 clamp 到 [-30, 30]，不允许硬命中。"""
    rules: List[Rule] = []
    for idx, item in enumerate(user_keywords or []):
        if not isinstance(item, dict):
            continue
        word = normalize_text(str(item.get("word") or ""), is_name=item.get("field") == "name").strip()
        field = item.get("field")
        try:
            weight = int(item.get("weight", 0))
        except (TypeError, ValueError):
            continue
        if not word or len(word) > 20 or field not in ("name", "sign", "text") or weight == 0:
            continue
        weight = max(-30, min(30, weight))
        rules.append(Rule(
            rule_id=f"user.{idx}", group=f"U_{idx}", field=field, pattern=re.escape(word),
            weight=weight, kind="soft" if weight > 0 else "minus",
            label=f"自定义关键词「{item.get('word')}」",
        ))
    return rules


_COMPILE_CACHE: Dict[str, List[CompiledRule]] = {}


def compile_rules(user_keywords: Optional[Sequence[Dict[str, Any]]] = None,
                  rules: Iterable[Rule] = DEFAULT_RULES) -> List[CompiledRule]:
    """编译默认规则 + 用户关键词；按内容哈希缓存。"""
    rules = list(rules)
    key = hashlib.sha256(json.dumps([user_keywords or [], [r.rule_id for r in rules]],
                                    ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    cached = _COMPILE_CACHE.get(key)
    if cached is not None:
        return cached
    compiled = [CompiledRule(rule, re.compile(rule.pattern)) for rule in rules + _user_keyword_rules(user_keywords)]
    if len(_COMPILE_CACHE) > 32:
        _COMPILE_CACHE.clear()
    _COMPILE_CACHE[key] = compiled
    return compiled


def _snippet(original: str, matched: str) -> str:
    """返回原文中确实存在、可用于前端高亮的片段。

    匹配在归一化文本上进行，归一化可能改变大小写 / 繁简 / 零宽字符。
    只有在原文（或其小写形式）中能定位到子串时才返回该子串；定位不到返回空串，
    前端对应地不高亮，避免把高亮打到错误位置。
    """
    if not matched or not original:
        return ""
    if matched in original:
        return matched
    lowered = original.lower()
    pos = lowered.find(matched)
    if pos >= 0:
        return original[pos:pos + len(matched)]
    return ""


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（两侧都截到 20 字）。"""
    a, b = a[:20], b[:20]
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _computed(rule_id: str, field: str, weight: Optional[int] = None, snippet: str = "") -> Hit:
    group, kind, label = COMPUTED_LABELS[rule_id]
    return Hit(rule_id, group, field, COMPUTED_WEIGHTS[rule_id] if weight is None else weight, kind, label, snippet)


def _dedupe_groups(hits: List[Hit]) -> List[Hit]:
    """同组只保留权重绝对值最大的一条。"""
    best: Dict[str, Hit] = {}
    for hit in hits:
        cur = best.get(hit.group)
        if cur is None or abs(hit.weight) > abs(cur.weight):
            best[hit.group] = hit
    return list(best.values())


def summarize(hits: List[Hit], soft_cap: int) -> Tuple[int, List[str]]:
    """按口径汇总分数；返回 (rule_score, 硬命中组列表)。"""
    hard = sum(h.weight for h in hits if h.kind == "hard")
    soft = min(soft_cap, sum(h.weight for h in hits if h.kind == "soft" and h.weight > 0))
    minus = sum(h.weight for h in hits if h.kind == "minus" or (h.kind == "soft" and h.weight < 0))
    score = max(0, min(99, hard + soft + minus))
    hard_groups = sorted({h.group for h in hits if h.kind == "hard"})
    return score, hard_groups


def has_focus(hits: Iterable[Hit]) -> bool:
    """是否命中重点词组（G_*）或硬命中（H_*）。"""
    return any(h.group.startswith(("G_", "H_")) and h.weight > 0 for h in hits)


def score_comment(comment: Dict[str, Any], compiled: Sequence[CompiledRule], settings: PatrolSettings, *,
                  whitelist: Optional[Set[int]] = None, owner_mid: Optional[int] = None,
                  self_mid: Optional[int] = None) -> Dict[str, Any]:
    """对一条评论做第一轮（单条）打分。

    Args:
        comment: 需含 mid, uname, sign, level, is_vip, official_type, content, is_reply, at_users。
        compiled: compile_rules() 的结果。
        settings: 巡检配置。
        whitelist: 用户白名单 mid。
        owner_mid: UP 主 mid（视为白名单）。
        self_mid: 当前登录账号 mid（视为白名单）。

    Returns:
        ``{rule_score, hits, hard_groups, has_focus_hit, source}``，hits 为 dict 列表。
    """
    mid = comment.get("mid")
    white = set(whitelist or ())
    if mid is not None and (mid in white or (owner_mid and mid == owner_mid) or (self_mid and mid == self_mid)):
        return {"rule_score": 0, "hits": [], "hard_groups": [], "has_focus_hit": False, "source": "whitelist"}

    raw_name = comment.get("uname") or ""
    raw_sign = comment.get("sign") or ""
    raw_text = comment.get("content") or ""
    fields = {
        "name": (raw_name, normalize_text(raw_name, is_name=True)),
        "sign": (raw_sign, normalize_text(raw_sign, is_name=True)),
        "text": (raw_text, normalize_text(raw_text)),
    }
    hits: List[Hit] = []
    for item in compiled:
        rule = item.rule
        targets = [rule.field]
        if rule.field == "name" and rule.apply_sign:
            targets.append("sign")
        for field in targets:
            original, norm = fields[field]
            if not norm:
                continue
            match = item.regex.search(norm)
            if not match:
                continue
            weight = rule.weight if field == rule.field else int(round(rule.weight * SIGN_FACTOR))
            hits.append(Hit(rule.rule_id, rule.group, field, weight, rule.kind, rule.label,
                            _snippet(original, match.group(0))))

    text_norm = fields["text"][1]
    text_len = len(raw_text.strip())

    # 驱赶语境：正文同时出现重点词与驱赶词 → 视为真人在骂机器人。
    if text_norm and _FOCUS_RE.search(text_norm) and _DRIVE_RE.search(text_norm):
        hits.append(_computed("human.drive_away", "text"))

    # @他人后推荐视频（只看一级评论）。
    at_users = comment.get("at_users") or []
    if not comment.get("is_reply") and at_users and text_norm and _RECOMMEND_RE.search(text_norm):
        name_norm = fields["name"][1]
        bonus = any(_lcs_len(name_norm, normalize_text(str(u.get("uname") or ""), is_name=True)) >= 2
                    for u in at_users if isinstance(u, dict))
        weight = COMPUTED_WEIGHTS["text.at_recommend"] + (COMPUTED_WEIGHTS["text.at_recommend.bonus"] if bonus else 0)
        hits.append(_computed("text.at_recommend", "text", weight))

    # 复述字幕式总结。
    if text_norm and (len(_TIMESTAMP_RE.findall(text_norm)) >= 2 or len(_LIST_RE.findall(raw_text)) >= 3
                      or _SEQUENCE_RE.search(text_norm)):
        hits.append(_computed("text.subtitle_summary", "text"))

    # 短评论书面化标点且无口语词。
    if 0 < text_len <= 60 and _FORMAL_RE.search(raw_text) and not _COLLOQUIAL_RE.search(text_norm):
        hits.append(_computed("text.formal_punct", "text"))

    # AI 惯用短句腔调（用户提供的语料合集）。
    persona = persona_phrase_hit(text_norm, raw_text)
    if persona is not None:
        hits.append(persona)

    # 人类信号。
    if 0 < text_len <= 20 and raw_text.strip().endswith(("?", "？")):
        hits.append(_computed("human.question", "text"))
    if 0 < text_len <= 12 and _SLANG_RE.search(text_norm):
        hits.append(_computed("human.short_slang", "text"))

    # 账号信号。
    level = comment.get("level")
    if isinstance(level, int):
        if level <= 2:
            hits.append(_computed("acct.low_level", "account"))
        elif level >= 6:
            hits.append(_computed("acct.lv6", "account"))
    official = comment.get("official_type")
    if isinstance(official, int) and official >= 0:
        hits.append(_computed("acct.official", "account"))
    if comment.get("is_vip"):
        hits.append(_computed("acct.vip", "account"))

    hits = _dedupe_groups(hits)
    score, hard_groups = summarize(hits, settings.soft_cap)
    return {
        "rule_score": score,
        "hits": [asdict(h) for h in hits],
        "hard_groups": hard_groups,
        "has_focus_hit": has_focus(hits),
        "source": "hard_rule" if hard_groups else "rule",
    }


def _hits_from_dicts(items: List[Dict[str, Any]]) -> List[Hit]:
    return [Hit(**{k: d.get(k, "") for k in ("rule_id", "group", "field", "weight", "kind", "label", "snippet")})
            for d in items]


def _as_ts(value: Any) -> Optional[float]:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return None


def apply_cross_features(items: List[Dict[str, Any]], settings: PatrolSettings, dedup: Any = None) -> None:
    """第二轮：跨评论特征（原地修改 items 的 rule_score / hits / hard_groups / source）。

    判定条件只读第一轮分数，不迭代，避免循环依赖。items 每条需含 rpid, mid, content, ctime,
    is_reply, parent_rpid, rule_score, hits, has_focus_hit, source。
    """
    if not items:
        return
    if dedup is None:
        from modules.comment.deduplicator import CommentDeduplicator
        dedup = CommentDeduplicator()
    first_round = {it["rpid"]: it.get("rule_score", 0) for it in items}
    by_rpid = {it["rpid"]: it for it in items}
    extra: Dict[int, List[Hit]] = {}

    def add(rpid: int, hit: Hit) -> None:
        bucket = extra.setdefault(rpid, [])
        if all(h.rule_id != hit.rule_id for h in bucket):
            bucket.append(hit)

    # cross.copy：≥15 字、不同 mid 之间高度相似；按 SimHash 高 16 位分桶。
    buckets: Dict[int, List[Tuple[Dict[str, Any], str, int]]] = {}
    for it in items:
        if it.get("source") == "whitelist":
            continue
        text = dedup._normalize_content(it.get("content") or "")
        if len(text) < 15:
            continue
        fp = dedup._simhash(text)
        buckets.setdefault(fp >> 48, []).append((it, text, fp))
    for group in buckets.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            a, ta, fa = group[i]
            for j in range(i + 1, len(group)):
                b, tb, fb = group[j]
                if a.get("mid") == b.get("mid"):
                    continue
                if dedup._calculate_similarity(ta, tb, fa, fb) >= dedup.fuzzy_match_threshold:
                    add(a["rpid"], _computed("cross.copy", "cross"))
                    add(b["rpid"], _computed("cross.copy", "cross"))

    # cross.same_account_burst：同一 mid ≥3 条、相邻间隔都 <120 秒。
    by_mid: Dict[Any, List[Dict[str, Any]]] = {}
    for it in items:
        by_mid.setdefault(it.get("mid"), []).append(it)
    for mid, group in by_mid.items():
        stamps = sorted((t, it["rpid"]) for it in group if (t := _as_ts(it.get("ctime"))) is not None)
        if len(stamps) >= 3 and all(stamps[k + 1][0] - stamps[k][0] < 120 for k in range(len(stamps) - 1)):
            for _, rpid in stamps:
                if by_rpid[rpid].get("source") != "whitelist":
                    add(rpid, _computed("cross.same_account_burst", "cross"))

    # 回复关系（楼中楼）。
    replies_to: Dict[Tuple[Any, Any], int] = {}
    for it in items:
        parent = by_rpid.get(it.get("parent_rpid")) if it.get("is_reply") else None
        if parent is None:
            continue
        key = (it.get("mid"), parent.get("mid"))
        replies_to[key] = replies_to.get(key, 0) + 1

    # cross.bot_chat：A↔B 互相回复各 ≥1 次，且对方第一轮 ≥ suspect 阈值。
    mid_first_max: Dict[Any, int] = {}
    for it in items:
        mid_first_max[it.get("mid")] = max(mid_first_max.get(it.get("mid"), 0), first_round[it["rpid"]])
    chat_mids: Set[Any] = set()
    for (a, b), _count in replies_to.items():
        if a != b and replies_to.get((b, a)) and mid_first_max.get(b, 0) >= settings.suspect_threshold:
            chat_mids.add(a)
    for it in items:
        if it.get("mid") in chat_mids and it.get("is_reply") and it.get("source") != "whitelist":
            add(it["rpid"], _computed("cross.bot_chat", "cross"))

    # cross.called_out：楼中楼回复含驱赶词 + 重点词，且被回复者本身有重点词 / 硬命中。
    called_by: Dict[Any, Set[Any]] = {}
    for it in items:
        if not it.get("is_reply"):
            continue
        parent = by_rpid.get(it.get("parent_rpid")) or by_rpid.get(it.get("root_rpid"))
        if parent is None or parent.get("mid") == it.get("mid") or not parent.get("has_focus_hit"):
            continue
        norm = normalize_text(it.get("content") or "")
        if _DRIVE_RE.search(norm) and _FOCUS_RE.search(norm):
            called_by.setdefault(parent.get("mid"), set()).add(it.get("mid"))
    for target_mid, callers in called_by.items():
        weight = min(COMPUTED_WEIGHTS["cross.called_out.cap"], COMPUTED_WEIGHTS["cross.called_out"] * len(callers))
        for it in by_mid.get(target_mid, []):
            if it.get("source") != "whitelist":
                add(it["rpid"], _computed("cross.called_out", "cross", weight))

    for rpid, new_hits in extra.items():
        it = by_rpid[rpid]
        hits = _dedupe_groups(_hits_from_dicts(it.get("hits") or []) + new_hits)
        score, hard_groups = summarize(hits, settings.soft_cap)
        it["hits"] = [asdict(h) for h in hits]
        it["rule_score"] = score
        it["hard_groups"] = hard_groups
        it["has_focus_hit"] = has_focus(hits)


def rule_signal_labels(hits: Iterable[Dict[str, Any]], limit: int = 6) -> List[str]:
    """给 AI 外发的中文命中类别（去重、只取正向、最多 limit 条）。"""
    labels: List[str] = []
    for h in sorted(hits or [], key=lambda x: -int(x.get("weight") or 0)):
        if int(h.get("weight") or 0) <= 0:
            continue
        label = str(h.get("label") or "")
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels
