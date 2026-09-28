"""真人判定与强制提示词注入。

LLM 只能根据调用方传入的结构化画像判断，不允许补充外部事实或猜测身份。
"""

# JSON 解析用于处理模型返回与二次编码。
import json
# 数值校验：拒绝 NaN/inf 置信度。
import math
# 正则用于剥离 Markdown 围栏与残留 JSON。
import re
# 类型标注保证接口一致性与 IDE 提示。
from typing import Any, Dict, Iterable, List, Optional

# 统一日志记录器。
from core.logger import get_logger
# LLM 客户端，负责受约束判定请求。
from llm.client import LLMClient

# 模块级日志实例。
logger = get_logger(__name__)

# 默认侧重点模板：核验等级/活跃度/抽奖转发。
DEFAULT_FOCUS_TEMPLATE = """重点核验账号等级、可观察账号年龄、近期活跃度，以及近期动态是否几乎全部为转发抽奖。不要因低等级单一指标直接判定。"""

# 严格系统提示词：只允许依据 DATA_JSON 事实判断。
STRICT_SYSTEM_TEMPLATE = """你是B站抽奖运营审核助手。你只能依据用户消息中 DATA_JSON 的事实进行判断，不得联网、补全事实或猜测身份。请为每个用户生成一条面向运营人员的专业化分析，放在 reasons 数组中且只能有1条，内容不超过100个汉字，直接说明关键特征和判定结论，表达自然、完整、可读。classification 必须是 real、suspicious 或 indeterminate；资料不足时必须 indeterminate，不得猜测。real 仅表示当前公开证据未发现明显异常，不代表身份认证。输出必须是严格 JSON，不要 Markdown。批量输入输出 {"results":[{"uid":整数,"classification":"real"或"suspicious"或"indeterminate","confidence":0到1或null,"reasons":["一条分析文本"]}]}；单用户也使用相同 results 数组。严禁出现 data_errors、observable_account_days 等字段名，严禁输出 JSON 片段、英文字段名、英文技术术语、代码、Markdown 或解释性前后缀。"""


# 模型输出中的技术字段名映射为运营可读中文。
_ANALYSIS_FIELD_REPLACEMENTS = {
    "data_errors": "数据异常",
    "observable_account_days": "账号公开活跃时间",
    "recent_activity_count": "近期活动情况",
    "lottery_repost_ratio": "抽奖转发占比",
    "video_count": "投稿数量",
    "follower_count": "粉丝数量",
    "classification": "判定结果",
    "confidence": "可信度",
}


def _clean_analysis_text(reasons: Any, fallback: List[str]) -> str:
    """将模型理由归一化为运营人员可读的单段自然语言。

    Args:
        reasons: 模型返回的理由列表、字典、JSON 字符串或普通文本。
        fallback: 模型内容无法使用时的规则分析。

    Returns:
        不含 JSON、代码、字段拼接和 Markdown 的 100 字以内文本。
    """
    # 进入异常保护，任何解析失败都走规则兜底。
    try:
        # 统一成列表处理，兼容单条字符串或列表输入。
        values: List[Any] = reasons if isinstance(reasons, list) else [reasons]
        # 收集清洗后的自然语言片段。
        extracted: List[str] = []
        # 遍历模型返回的每个理由项。
        for value in values:
            # 字典结构递归提取理由字段。
            if isinstance(value, dict):
                # 字典可能嵌套 reasons 字段，取第一个可用键递归展开。
                nested = value.get("reasons") or value.get("reason") or value.get("analysis")
                if nested is not None:
                # 递归展开嵌套理由。
                    extracted.extend(_flatten_analysis_values(nested))
                continue
            # 字符串可能是纯文本或 JSON 字符串。
            if isinstance(value, str):
                # 去掉首尾空白再判断格式。
                candidate = value.strip()
                # LLM 偶尔把 reasons 再编码成 JSON，先解码再取自然语言字段。
                if candidate.startswith(("{", "[")):
                    try:
                    # 二次编码的 JSON 先解码再提取。
                        decoded = json.loads(candidate)
                    # 解码后递归展开字段。
                        extracted.extend(_flatten_analysis_values(decoded))
                        continue
                    except json.JSONDecodeError:
                        pass
            # 普通字符串直接作为理由片段。
                extracted.append(candidate)
            elif value is not None:
                # 其他类型统一转字符串。
                extracted.append(str(value).strip())

        # 用中文分号拼接全部片段，保证展示为一段完整文本。
        text = "；".join(item for item in extracted if item)
        # 技术字段名替换为运营可读中文，避免暴露内部字段。
        for technical_name, readable_name in _ANALYSIS_FIELD_REPLACEMENTS.items():
            # 替换技术字段名为运营可读中文。
            text = text.replace(technical_name, readable_name)
        # 去掉可能的 Markdown 代码块围栏。
        text = re.sub(r"```(?:json|text)?|```", "", text, flags=re.IGNORECASE)
        # 去掉常见的"分析/理由/结论"前缀。
        text = re.sub(r"^[\s]*(?:分析|理由|结论)[\s]*[:：]", "", text)
        # 拒绝残留 JSON/代码；英文技术字段不应进入运营展示。
        if re.search(r"[{}\[\]]|\"\s*:\s*", text) or re.search(r"\b(?:uid|json|classification|confidence|reasons)\b", text, re.IGNORECASE):
            # 残留 JSON 或英文字段时直接清空文本。
            text = ""
        # 压缩全部空白，避免运营展示出现多余空格。
        text = re.sub(r"\s+", "", text)
        # 收敛连续标点，统一结尾。
        text = re.sub(r"[；，。]{2,}", "。", text).strip("；，。:： \t\r\n")
        if text:
            # 最终文本截断到 100 字。
            return text[:100]
        # 模型内容不可用时回退到规则分析文本。
        fallback_text = "；".join(str(item).strip() for item in fallback if item).strip()
        # 规则兜底文本同样做空白压缩。
        fallback_text = re.sub(r"\s+", "", fallback_text).strip("；，。:： \t\r\n")
        # 无可用规则时使用默认文案。
        return (fallback_text or "未发现明显异常特征")[:100]
    except Exception as exc:
        # 清洗异常不阻断流程，直接使用规则结论。
        logger.warning("清洗运营分析文本失败，使用规则结论: %s", exc)
        # 最坏情况返回规则兜底文本。
        return ("；".join(str(item) for item in fallback if item) or "未发现明显异常特征")[:100]


def _flatten_analysis_values(value: Any) -> List[str]:
    """递归提取模型结构中的自然语言理由，避免把对象直接转字符串。

    Args:
        value: 待提取的模型字段。

    Returns:
        自然语言片段列表。
    """
    # 列表逐项递归展开。
    if isinstance(value, list):
        # 列表逐项递归展开。
        return [part for item in value for part in _flatten_analysis_values(item)]
    # 字典按优先级取理由字段。
    if isinstance(value, dict):
        # 按优先级取常见理由字段名。
        for key in ("reasons", "reason", "analysis", "text", "summary"):
            # 找到首个可用字段后立即递归。
            if key in value:
                return _flatten_analysis_values(value[key])
        return []
    # 标量值直接转字符串，None 跳过。
    return [str(value).strip()] if value is not None else []


def evaluate_evidence_quality(profile: Dict[str, Any]) -> tuple[bool, List[str]]:
    """检查真人判定所需关键画像是否完整。

    Args:
        profile: 已采集的结构化用户画像。

    Returns:
        证据是否足够，以及缺失字段对应的稳定原因列表。
    """
    required = {
        "level": "missing_level",
        "recent_activity_count": "missing_activity",
        "lottery_repost_ratio": "missing_lottery_ratio",
        "video_count": "missing_video_count",
    }
    field_status = profile.get("field_status") if isinstance(profile.get("field_status"), dict) else {}
    reasons: List[str] = []
    for field, reason in required.items():
        value = profile.get(field)
        state = field_status.get(field)
        # 值缺失，或来源字段状态非 ok（missing/invalid/soft legacy）都视为证据不足。
        if value is None or (state is not None and state != "ok"):
            reasons.append(reason)
    return not reasons, reasons


def make_indeterminate_result(uid: int, reason_codes: List[str]) -> Dict[str, Any]:
    """构造公开证据不足时的统一未知结果。"""
    result = {
        "uid": uid,
        "classification": "indeterminate",
        "confidence": None,
        "reasons": ["公开资料不足，无法完成可靠判断"],
        "reason_codes": reason_codes,
        "source": "evidence_gate",
    }
    result["analysis_text"] = result["reasons"][0]
    return result


def heuristic_classify(profile: Dict[str, Any]) -> Dict[str, Any]:
    """依据可解释规则生成无 LLM 时的保守判定。

    Args:
        profile: 已采集的结构化用户画像。

    Returns:
        包含分类、置信度和理由的判定结果。
    """
    evidence_ok, evidence_reasons = evaluate_evidence_quality(profile)
    if not evidence_ok:
        return make_indeterminate_result(int(profile.get("uid") or 0), evidence_reasons)

    # 分数越高越倾向判为可疑账号。
    score = 0
    # 存放规则命中原因，供运营人员查看。
    reasons: List[str] = []
    # 读取画像关键特征字段。
    level = int(profile.get("level") or 0)
    # 近期活动数：0 视为可疑。
    activity_count = int(profile.get("recent_activity_count") or 0)
    # 抽奖转发占比：识别抽奖号。
    lottery_ratio = float(profile.get("lottery_repost_ratio") or 0)
    # 可观察账号年龄：判断新号。
    account_days = int(profile.get("observable_account_days") or 0)

    # 每命中一个抽奖号特征就加分，最终分数决定判定倾向。
    if level <= 1:
        score += 1
        reasons.append("账号等级偏低")
    # 等级过低判定：等级<=1 视为可疑特征。
    if activity_count == 0:
        score += 1
        reasons.append("未观察到近期公开活动")
    # 新号判定：可观察历史不足 30 天。
    if 0 < account_days < 30:
        score += 1
        reasons.append("可观察公开活动历史不足30天")
    # 重度抽奖号判定：占比超 80% 且样本足够。
    if lottery_ratio >= 0.8 and activity_count >= 3:
        score += 4
        reasons.append("近期公开动态几乎全部为抽奖转发")
    # 中度抽奖号判定：占比超 50% 且样本足够。
    elif lottery_ratio >= 0.5 and activity_count >= 4:
        score += 2
        reasons.append("近期抽奖相关转发比例较高")
    # 有投稿内容可降低可疑分。
    if int(profile.get("video_count") or 0) > 0:
        score -= 1
        reasons.append("存在公开投稿")

    # 分数达到阈值判定为可疑账号。
    suspicious = score >= 3
    # 组装启发式判定结果。
    return {
        "uid": int(profile.get("uid") or 0),
        "classification": "suspicious" if suspicious else "real",
        # 置信度随分数远离阈值而提高，封顶 0.95。
        "confidence": min(0.95, 0.55 + abs(score - 2) * 0.1),
        "reasons": reasons or ["未发现明显抽奖号特征"],
        "source": "heuristic",
    }


def _extract_json(text: str) -> Dict[str, Any]:
    """从模型文本中提取 JSON 对象并拒绝非对象结果。"""
    # 去掉可能的 markdown 代码块围栏，只保留花括号片段。
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.IGNORECASE).strip()
    # 从清洗后的文本中提取花括号 JSON 片段。
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError("AI 未返回 JSON 对象")
    # 提取到内容后统一用 json.loads 解析，语法错误会由调用方兜底。
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("AI JSON 顶层必须为对象")
    return value


def _normalize_results(
    raw_results: Iterable[Any],
    profiles: List[Dict[str, Any]],
    allow_fallback_real: bool = False,
) -> List[Dict[str, Any]]:
    """校验模型结果，缺失结果执行不会升级为真人的保守回退。"""
    # 预生成每个用户的规则兜底判定。
    fallback = {int(item["uid"]): heuristic_classify(item) for item in profiles}
    profile_by_uid = {int(item["uid"]): item for item in profiles}
    if not allow_fallback_real:
        for uid, fallback_item in fallback.items():
            if fallback_item.get("classification") == "real":
                fallback[uid] = make_indeterminate_result(uid, ["llm_unavailable_no_real_upgrade"])
    # 把规则兜底的理由统一清洗成运营可读文本，供 LLM 结果缺失时复用。
    for fallback_item in fallback.values():
        # 兜底理由统一清洗为运营可读文本。
        fallback_item["reasons"] = [_clean_analysis_text(fallback_item.get("reasons"), [])]
        # 同时提供 analysis_text 供前端直接展示。
        fallback_item["analysis_text"] = fallback_item["reasons"][0]
    # 存放通过校验的模型结果，按 UID 索引。
    normalized: Dict[int, Dict[str, Any]] = {}
    # 只接受结构与分类值合法的模型结果，其余用户回退到启发式判定。
    for item in raw_results:
        # 非字典条目直接跳过。
        if not isinstance(item, dict):
            continue
        # 提取用户 UID。
        uid = int(item.get("uid") or 0)
        # 未知用户或分类值非法时回退规则。
        if uid not in fallback or item.get("classification") not in {"real", "suspicious", "indeterminate"}:
            continue
        evidence_ok, evidence_reasons = evaluate_evidence_quality(profile_by_uid[uid])
        if not evidence_ok:
            normalized[uid] = make_indeterminate_result(uid, evidence_reasons)
            continue
        # 理由必须是列表，否则视为缺失。
        reasons = item.get("reasons") if isinstance(item.get("reasons"), list) else []
        # 组装归一化结果，置信度限制在 0-1。
        # 置信度归一化：拒绝 NaN/inf/非数值，避免污染下游比较（规格 §7.3）。
        confidence: Optional[float] = None
        if item["classification"] != "indeterminate":
            raw_confidence = item.get("confidence")
            if raw_confidence is None:
                confidence = 0.5
            else:
                try:
                    parsed_confidence = float(raw_confidence)
                except (TypeError, ValueError):
                    parsed_confidence = math.nan
                confidence = (
                    max(0.0, min(1.0, parsed_confidence))
                    if math.isfinite(parsed_confidence)
                    else None
                )
        normalized[uid] = {
            "uid": uid,
            "classification": item["classification"],
            "confidence": confidence,
            "reasons": [_clean_analysis_text(reasons, fallback[uid]["reasons"])],
            "analysis_text": _clean_analysis_text(reasons, fallback[uid]["reasons"]),
            "source": "llm",
        }
    # 按输入画像顺序返回，缺失用户用规则兜底。
    return [normalized.get(int(profile["uid"]), fallback[int(profile["uid"])]) for profile in profiles]


async def classify_profiles(
    profiles: List[Dict[str, Any]],
    focus_template: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """使用受约束提示词批量判定用户真实性。

    Args:
        profiles: 仅包含已采集事实的用户画像列表。
        focus_template: 可配置侧重点；会注入严格数据边界之后。

    Returns:
        与输入 UID 一一对应的判定列表；LLM 不可用时返回规则结果。
    """
    if not profiles:
        return []
    # 侧重点模板截断到 1200 字，防止超长输入挤占模型上下文。
    focus = (focus_template or DEFAULT_FOCUS_TEMPLATE).strip()[:1200]
    # 只序列化已采集的事实字段，不传任何未经验证的补充信息。
    payload = json.dumps(profiles, ensure_ascii=False, separators=(",", ":"))
    # 拼接最终提示词，强制约束在事实范围内。
    prompt = f"FOCUS_TEMPLATE:\n{focus}\nDATA_JSON:\n{payload}"
    try:
        # 用上下文管理器包住 LLM 连接，确保成功或异常都能释放资源。
        async with LLMClient(timeout=90) as client:
            # 调用 LLM 获取结构化判定文本。
            content = await client.simple_chat(
                prompt,
                system=STRICT_SYSTEM_TEMPLATE,
                temperature=0.1,
                max_tokens=min(3000, 350 + len(profiles) * 120),
            )
        # 解析模型 JSON 结果，越界或缺失字段由规则结果兜底。
        parsed = _extract_json(content)
        # 解析模型结果，异常由兜底逻辑接管。
        return _normalize_results(parsed.get("results") or [], profiles)
    except Exception as exc:
        # LLM 不可用不阻断业务，降级为可解释规则判定。
        logger.warning("真人筛选 LLM 不可用，使用可解释规则兜底: %s", exc)
        # LLM 不可用不阻断业务，降级为规则判定。
        return _normalize_results([], profiles)