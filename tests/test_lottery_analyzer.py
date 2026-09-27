"""真人判定与文本清洗的契约级测试。

覆盖 modules/lottery/analyzer.py 的公开函数、内部清洗/归一化助手与异常兜底分支。
LLM 依赖使用契约级假客户端（真实实现异步上下文协议），不使用 AsyncMock 顶替 async with。
"""

import asyncio
import json

import pytest

from core.exceptions import LLMNotConfiguredError

from modules.lottery.analyzer import (
    DEFAULT_FOCUS_TEMPLATE,
    STRICT_SYSTEM_TEMPLATE,
    _clean_analysis_text,
    _extract_json,
    _flatten_analysis_values,
    _normalize_results,
    classify_profiles,
    heuristic_classify,
)


# ------------------------------------------------------------------ 契约级假客户端

class _ContractLLMClient:
    """契约级 LLM 客户端：真实实现 ``async with`` 协议与 simple_chat。"""

    # 由测试用例注入的 simple_chat 返回内容（可为异常实例）。
    payload = None
    # 最近一次调用的参数快照，供契约断言。
    captured: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        """记录构造参数（含 timeout），不读取任何密钥。"""
        _ContractLLMClient.captured["init_kwargs"] = kwargs
        _ContractLLMClient.captured["entered"] = False
        _ContractLLMClient.captured["exited"] = False

    async def __aenter__(self):
        """进入上下文，模拟连接建立。"""
        _ContractLLMClient.captured["entered"] = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """退出上下文，模拟连接释放，不吞掉异常。"""
        _ContractLLMClient.captured["exited"] = True
        return False

    async def simple_chat(self, prompt, system=None, **kwargs):
        """按预置内容返回或抛出异常，并记录提示词契约。"""
        _ContractLLMClient.captured["prompt"] = prompt
        _ContractLLMClient.captured["system"] = system
        _ContractLLMClient.captured["chat_kwargs"] = kwargs
        payload = _ContractLLMClient.payload
        if isinstance(payload, BaseException):
            raise payload
        return payload


class _UnconfiguredLLMClient:
    """契约级未配置客户端：构造即抛出 LLMNotConfiguredError。"""

    def __init__(self, *args, **kwargs) -> None:
        """模拟缺少密钥的构造失败。"""
        raise LLMNotConfiguredError("LLM API密钥未配置")


@pytest.fixture()
def contract_llm(monkeypatch):
    """把模块级 LLMClient 替换为契约级假客户端，并重置记录状态。"""
    _ContractLLMClient.payload = None
    _ContractLLMClient.captured = {}
    monkeypatch.setattr("modules.lottery.analyzer.LLMClient", _ContractLLMClient)
    return _ContractLLMClient


def _profile(uid: int, **overrides) -> dict:
    """构造一份已采集事实的画像。"""
    profile = {"uid": uid, "level": 6, "recent_activity_count": 5, "lottery_repost_ratio": 0.1, "video_count": 0}
    profile.update(overrides)
    return profile


# ------------------------------------------------- _flatten_analysis_values

def test_flatten_analysis_values_expands_nested_lists() -> None:
    """嵌套列表应被完全展平为字符串片段。"""
    assert _flatten_analysis_values(["甲", ["乙", ["丙"]]]) == ["甲", "乙", "丙"]


def test_flatten_analysis_values_respects_dict_key_priority() -> None:
    """字典按键优先级 reasons > reason > analysis > text > summary 取值。"""
    assert _flatten_analysis_values({"reason": "理由", "summary": "摘要"}) == ["理由"]
    assert _flatten_analysis_values({"analysis": "分析", "text": "正文"}) == ["分析"]
    assert _flatten_analysis_values({"text": "正文", "summary": "摘要"}) == ["正文"]
    assert _flatten_analysis_values({"summary": "摘要"}) == ["摘要"]


def test_flatten_analysis_values_returns_empty_for_unknown_dict() -> None:
    """字典不含任何已知理由键时返回空列表。"""
    assert _flatten_analysis_values({"uid": 1, "level": 6}) == []


def test_flatten_analysis_values_handles_scalars_and_none() -> None:
    """标量转字符串，None 直接跳过。"""
    assert _flatten_analysis_values("文本") == ["文本"]
    assert _flatten_analysis_values(0) == ["0"]
    assert _flatten_analysis_values(None) == []


# ------------------------------------------------- _clean_analysis_text

def test_clean_analysis_text_returns_plain_string() -> None:
    """普通中文文本应原样保留（仅压缩空白）。"""
    assert _clean_analysis_text("该账号等级高，活跃正常", []) == "该账号等级高，活跃正常"


def test_clean_analysis_text_collapses_whitespace_and_punctuation() -> None:
    """多余空白被移除，连续标点收敛为句号。"""
    assert _clean_analysis_text("该账号等级高，   活跃正常；；", []) == "该账号等级高，活跃正常"


def test_clean_analysis_text_joins_list_items_with_semicolon() -> None:
    """多段理由用中文分号拼接为一段。"""
    assert _clean_analysis_text(["等级高", "活跃正常"], []) == "等级高；活跃正常"


def test_clean_analysis_text_expands_nested_dict_reasons() -> None:
    """字典结构递归提取 reasons 字段。"""
    assert _clean_analysis_text({"reasons": ["等级高", "无异常"]}, []) == "等级高；无异常"
    assert _clean_analysis_text({"reason": "单一理由"}, []) == "单一理由"
    assert _clean_analysis_text({"analysis": "分析文本"}, []) == "分析文本"


def test_clean_analysis_text_decodes_double_encoded_json() -> None:
    """模型把 reasons 再编码成 JSON 字符串时应先解码再取自然语言。"""
    doubled = json.dumps({"reasons": ["等级高", "活跃正常"]}, ensure_ascii=False)

    assert _clean_analysis_text([doubled], []) == "等级高；活跃正常"


def test_clean_analysis_text_rejects_bracket_residue() -> None:
    """残留方括号的文本被判定为结构化残留，整体作废并回退默认文案。"""
    assert _clean_analysis_text("[已核验] 账号正常", []) == "未发现明显异常特征"


def test_clean_analysis_text_strips_markdown_prefix() -> None:
    """去掉「分析/理由/结论：」这类前缀。"""
    assert _clean_analysis_text("分析：该账号正常", []) == "该账号正常"
    assert _clean_analysis_text("结论: 真人", []) == "真人"


def test_clean_analysis_text_translates_technical_field_names() -> None:
    """内部技术字段名应替换为运营可读中文，不能外泄字段名。"""
    text = _clean_analysis_text("data_errors 为空，observable_account_days 较短", [])

    assert "data_errors" not in text
    assert "observable_account_days" not in text
    assert "数据异常" in text
    assert "账号公开活跃时间" in text


def test_clean_analysis_text_rejects_residual_json_and_falls_back() -> None:
    """清洗后仍残留 JSON 结构时应整体作废并回退规则文本。"""
    assert _clean_analysis_text(['{"uid": 1}'], ["规则兜底"]) == "规则兜底"
    assert _clean_analysis_text(["```json\n{\"a\": 1}\n```"], ["规则兜底"]) == "规则兜底"


def test_clean_analysis_text_rejects_english_technical_terms() -> None:
    """出现 uid 等未映射的英文技术词时整体作废。"""
    assert _clean_analysis_text("uid 已核对", ["规则兜底"]) == "规则兜底"


def test_clean_analysis_text_truncates_to_hundred_chars() -> None:
    """超长文本截断到 100 字。"""
    text = _clean_analysis_text("测" * 150, [])

    assert len(text) == 100
    assert text == "测" * 100


def test_clean_analysis_text_uses_default_when_everything_empty() -> None:
    """模型与规则都无内容时给出统一默认文案。"""
    assert _clean_analysis_text([], []) == "未发现明显异常特征"
    assert _clean_analysis_text(None, []) == "未发现明显异常特征"


def test_clean_analysis_text_falls_back_when_conversion_raises() -> None:
    """理由项无法字符串化时整体走规则兜底，不向上抛异常。"""

    class _BoomStr:
        """__str__ 抛异常的对象，用于触发顶层 except 分支。"""

        def __str__(self) -> str:
            """故意抛错，验证兜底。"""
            raise RuntimeError("字符串化失败")

    assert _clean_analysis_text([_BoomStr()], ["规则理由"]) == "规则理由"


# ------------------------------------------------- heuristic_classify

def test_heuristic_classify_marks_new_low_level_empty_account_suspicious() -> None:
    """等级低 + 无近期活动 + 历史短：累计 3 分达阈值判可疑。"""
    result = heuristic_classify(
        {
            "uid": 7,
            "level": 1,
            "recent_activity_count": 0,
            "lottery_repost_ratio": 0,
            "video_count": 0,
            "observable_account_days": 10,
        }
    )

    assert result["uid"] == 7
    assert result["classification"] == "suspicious"
    assert result["source"] == "heuristic"
    assert result["confidence"] == 0.65
    assert "账号等级偏低" in result["reasons"]
    assert "未观察到近期公开活动" in result["reasons"]
    assert "可观察公开活动历史不足30天" in result["reasons"]


def test_heuristic_classify_flags_heavy_lottery_reposter() -> None:
    """抽奖转发占比超 8 成且样本足够时直接给 4 分，判可疑。"""
    result = heuristic_classify(
        {"uid": 8, "level": 5, "recent_activity_count": 3, "lottery_repost_ratio": 0.9, "video_count": 0}
    )

    assert result["classification"] == "suspicious"
    assert result["confidence"] == 0.75
    assert "近期公开动态几乎全部为抽奖转发" in result["reasons"]


def test_heuristic_classify_flags_medium_lottery_ratio() -> None:
    """占比在 5 成到 8 成之间且样本 >= 4 时给 2 分，未达可疑阈值。"""
    result = heuristic_classify(
        {"uid": 9, "level": 6, "recent_activity_count": 4, "lottery_repost_ratio": 0.6, "video_count": 0}
    )

    assert result["classification"] == "real"
    assert result["confidence"] == 0.55
    assert "近期抽奖相关转发比例较高" in result["reasons"]


def test_heuristic_classify_ignores_lottery_ratio_without_enough_samples() -> None:
    """样本不足时不因高占比扣分。"""
    result = heuristic_classify(
        {"uid": 10, "level": 6, "recent_activity_count": 3, "lottery_repost_ratio": 0.6, "video_count": 0}
    )

    assert result["classification"] == "real"
    assert result["reasons"] == ["未发现明显抽奖号特征"]
    assert result["confidence"] == 0.75


def test_heuristic_classify_credits_public_uploads() -> None:
    """存在公开投稿时减 1 分。"""
    result = heuristic_classify(
        {"uid": 11, "level": 1, "recent_activity_count": 0, "lottery_repost_ratio": 0, "video_count": 5}
    )

    # 1（低等级）+ 1（无活动）- 1（有投稿）= 1 分，未达阈值。
    assert result["classification"] == "real"
    assert result["confidence"] == 0.65
    assert "存在公开投稿" in result["reasons"]


def test_heuristic_classify_scores_five_when_history_short_and_reposts_heavy() -> None:
    """历史不足 30 天 + 重度抽奖转发：累计 5 分，置信度 0.85。"""
    result = heuristic_classify(
        {
            "uid": 12,
            "level": 6,
            "recent_activity_count": 5,
            "lottery_repost_ratio": 0.85,
            "video_count": 0,
            "observable_account_days": 12,
        }
    )

    assert result["classification"] == "suspicious"
    # 浮点累加存在尾差，使用 approx 做数值断言。
    assert result["confidence"] == pytest.approx(0.85)


def test_heuristic_classify_caps_confidence_at_095() -> None:
    """满分画像的置信度封顶 0.95。"""
    result = heuristic_classify(
        {
            "uid": 13,
            "level": 1,
            "recent_activity_count": 4,
            "lottery_repost_ratio": 1.0,
            "video_count": 0,
            "observable_account_days": 5,
        }
    )

    assert result["classification"] == "suspicious"
    assert result["confidence"] == 0.95


def test_heuristic_classify_defaults_uid_and_missing_fields() -> None:
    """空画像不应崩溃；关键事实缺失时走证据门槛，判定为未知。"""
    result = heuristic_classify({})

    assert result["uid"] == 0
    assert result["classification"] == "indeterminate"
    assert result["confidence"] is None
    assert result["source"] == "evidence_gate"
    assert set(result["reason_codes"]) == {
        "missing_level",
        "missing_activity",
        "missing_lottery_ratio",
        "missing_video_count",
    }


def test_heuristic_classify_ignores_string_zero_values() -> None:
    """字符串 "0" 等假值按 0 处理；关键事实为 None 时走未知门槛。"""
    result = heuristic_classify(
        {
            "uid": 14,
            "level": "0",
            "recent_activity_count": "0",
            "lottery_repost_ratio": "0",
            "video_count": "0",
            "observable_account_days": 0,
        }
    )

    assert result["classification"] == "real"
    assert "账号等级偏低" in result["reasons"]

    # 关键事实缺失（None）不再按 0 处理，而是证据不足判定为未知。
    missing = heuristic_classify(
        {
            "uid": 14,
            "level": "0",
            "recent_activity_count": None,
            "lottery_repost_ratio": 0,
            "video_count": 0,
        }
    )
    assert missing["classification"] == "indeterminate"
    assert missing["reason_codes"] == ["missing_activity"]


# ------------------------------------------------- _extract_json

def test_extract_json_parses_plain_object() -> None:
    """纯净 JSON 对象直接解析。"""
    assert _extract_json('{"results": []}') == {"results": []}


def test_extract_json_parses_fenced_object() -> None:
    """带 markdown 围栏且前后有噪音文本时仍能提取花括号片段。"""
    text = '```json\n{"results": [{"uid": 1}]}\n```\n以上。'

    assert _extract_json(text) == {"results": [{"uid": 1}]}


def test_extract_json_raises_when_no_object_found() -> None:
    """没有任何花括号时抛出明确错误（从外部打异常）。"""
    with pytest.raises(ValueError) as excinfo:
        _extract_json("模型没有返回 JSON")

    assert "未返回 JSON 对象" in str(excinfo.value)


def test_extract_json_propagates_json_decode_error() -> None:
    """花括号内语法错误时由 json 层抛出，交由调用方兜底。"""
    with pytest.raises(json.JSONDecodeError):
        _extract_json("{这不是合法 json}")


# ------------------------------------------------- _normalize_results

def test_normalize_results_keeps_valid_llm_items() -> None:
    """合法的模型结果被采纳并标记来源为 llm。"""
    profiles = [_profile(1), _profile(2)]
    raw = [{"uid": 1, "classification": "suspicious", "confidence": 0.8, "reasons": ["等级低，活跃异常"]}]

    results = _normalize_results(raw, profiles)

    assert results[0]["uid"] == 1
    assert results[0]["classification"] == "suspicious"
    assert results[0]["confidence"] == 0.8
    assert results[0]["source"] == "llm"
    assert results[0]["reasons"] == ["等级低，活跃异常"]
    assert results[0]["analysis_text"] == "等级低，活跃异常"
    # 未覆盖的用户回退规则判定：real 在保守策略下降级为未知。
    assert results[1]["uid"] == 2
    assert results[1]["source"] == "evidence_gate"
    assert results[1]["classification"] == "indeterminate"
    assert results[1]["analysis_text"] == results[1]["reasons"][0]


def test_normalize_results_skips_unknown_uid_and_invalid_classification() -> None:
    """未知 UID、非法分类与非字典条目一律跳过并回退规则判定。"""
    profiles = [_profile(1)]
    raw = [
        {"uid": 999, "classification": "real", "confidence": 0.9, "reasons": ["x"]},
        {"uid": 1, "classification": "unknown", "confidence": 0.9, "reasons": ["x"]},
        "不是字典",
        {"classification": "real", "confidence": 0.9, "reasons": ["无 uid"]},
        {"uid": 1, "classification": "real"},
    ]

    results = _normalize_results(raw, profiles)

    assert len(results) == 1
    # 最后一条合法（uid=1 且分类为 real），因此来源是 llm。
    assert results[0]["source"] == "llm"
    assert results[0]["classification"] == "real"


def test_normalize_results_clamps_confidence_and_defaults_missing() -> None:
    """置信度越界被夹到 0-1，缺失时取 0.5。"""
    profiles = [_profile(1), _profile(2), _profile(3)]
    raw = [
        {"uid": 1, "classification": "real", "confidence": 5, "reasons": ["a"]},
        {"uid": 2, "classification": "real", "confidence": -3, "reasons": ["b"]},
        {"uid": 3, "classification": "real", "reasons": ["c"]},
    ]

    results = _normalize_results(raw, profiles)

    assert [item["confidence"] for item in results] == [1.0, 0.0, 0.5]


def test_normalize_results_treats_non_list_reasons_as_missing() -> None:
    """reasons 不是列表时视为缺失，回退到规则理由文本。"""
    profiles = [_profile(1, level=1, recent_activity_count=0, observable_account_days=10)]

    results = _normalize_results(
        [{"uid": 1, "classification": "real", "confidence": 0.7, "reasons": "等级偏低"}], profiles
    )

    assert results[0]["source"] == "llm"
    # 模型理由不可用，清洗后落回启发式理由。
    assert "账号等级偏低" in results[0]["analysis_text"]


def test_normalize_results_returns_fallback_for_empty_raw() -> None:
    """模型结果为空时全部用户走启发式判定。"""
    profiles = [_profile(1), _profile(2)]

    results = _normalize_results([], profiles)

    assert [item["source"] for item in results] == ["evidence_gate", "evidence_gate"]
    assert [item["classification"] for item in results] == ["indeterminate", "indeterminate"]
    assert all(item["analysis_text"] == item["reasons"][0] for item in results)


def test_normalize_results_preserves_input_order() -> None:
    """返回顺序必须与输入画像顺序一致。"""
    profiles = [_profile(20), _profile(10)]
    raw = [{"uid": 10, "classification": "real", "confidence": 0.6, "reasons": ["ok"]}]

    results = _normalize_results(raw, profiles)

    assert [item["uid"] for item in results] == [20, 10]


# ------------------------------------------------- classify_profiles

def test_classify_profiles_returns_empty_for_empty_input(contract_llm) -> None:
    """空输入直接返回空列表，不触发 LLM 调用。"""
    assert asyncio.run(classify_profiles([])) == []
    assert contract_llm.captured.get("entered") is None


def test_classify_profiles_uses_strict_prompt_contract(contract_llm) -> None:
    """提示词必须携带侧重点模板与紧凑 DATA_JSON，并传入严格系统提示词。"""
    contract_llm.payload = json.dumps(
        {"results": [{"uid": 1, "classification": "real", "confidence": 0.7, "reasons": ["正常"]}]},
        ensure_ascii=False,
    )

    results = asyncio.run(classify_profiles([_profile(1)], focus_template="重点看活跃度"))

    captured = contract_llm.captured
    assert captured["entered"] is True
    assert captured["exited"] is True
    assert captured["init_kwargs"] == {"timeout": 90}
    assert captured["system"] == STRICT_SYSTEM_TEMPLATE
    assert "FOCUS_TEMPLATE:" in captured["prompt"]
    assert "重点看活跃度" in captured["prompt"]
    assert '"uid":1' in captured["prompt"]
    # 批量 1 人时 max_tokens = min(3000, 350 + 1*120)。
    assert captured["chat_kwargs"]["max_tokens"] == 470
    assert captured["chat_kwargs"]["temperature"] == 0.1
    assert results[0]["source"] == "llm"


def test_classify_profiles_uses_default_focus_when_not_provided(contract_llm) -> None:
    """未传侧重点时使用默认模板。"""
    contract_llm.payload = '{"results": []}'

    asyncio.run(classify_profiles([_profile(1)]))

    assert DEFAULT_FOCUS_TEMPLATE in contract_llm.captured["prompt"]


def test_classify_profiles_blank_focus_is_stripped_not_defaulted(contract_llm) -> None:
    """记录当前契约：纯空白模板为真值，strip 后得到空侧重点而非回退默认。"""
    contract_llm.payload = '{"results": []}'

    asyncio.run(classify_profiles([_profile(1)], focus_template="   "))

    prompt = contract_llm.captured["prompt"]
    assert prompt.startswith("FOCUS_TEMPLATE:\n\nDATA_JSON:")
    assert DEFAULT_FOCUS_TEMPLATE not in prompt


def test_classify_profiles_truncates_overlong_focus_template(contract_llm) -> None:
    """超长侧重点截断到 1200 字，避免挤占模型上下文。"""
    contract_llm.payload = '{"results": []}'
    marker = "尾巴标记"
    focus = "前" * 1200 + marker

    asyncio.run(classify_profiles([_profile(1)], focus_template=focus))

    prompt = contract_llm.captured["prompt"]
    assert "前" * 1200 in prompt
    assert marker not in prompt


def test_classify_profiles_scales_max_tokens_with_batch_size(contract_llm) -> None:
    """max_tokens 随批量大小增长并封顶 3000。"""
    contract_llm.payload = '{"results": []}'

    asyncio.run(classify_profiles([_profile(index) for index in range(2)]))
    assert contract_llm.captured["chat_kwargs"]["max_tokens"] == 590

    asyncio.run(classify_profiles([_profile(index) for index in range(50)]))
    assert contract_llm.captured["chat_kwargs"]["max_tokens"] == 3000


def test_classify_profiles_falls_back_when_llm_raises(contract_llm) -> None:
    """LLM 调用抛异常时降级为可解释规则判定。"""
    contract_llm.payload = RuntimeError("连接失败")

    results = asyncio.run(
        classify_profiles(
            [_profile(1, level=1, recent_activity_count=0, observable_account_days=10)]
        )
    )

    assert results[0]["source"] == "heuristic"
    assert results[0]["classification"] == "suspicious"


def test_classify_profiles_falls_back_when_llm_returns_non_json(contract_llm) -> None:
    """模型返回非 JSON 文本时降级为规则判定。"""
    contract_llm.payload = "抱歉，我无法回答"

    results = asyncio.run(classify_profiles([_profile(1)]))

    # 普通账号在保守策略下回退为未知，而不是直接升级为真人。
    assert results[0]["source"] == "evidence_gate"
    assert results[0]["classification"] == "indeterminate"


def test_classify_profiles_falls_back_when_client_unconfigured(monkeypatch) -> None:
    """LLM 未配置（构造即失败）时同样降级，不影响业务返回。"""
    monkeypatch.setattr("modules.lottery.analyzer.LLMClient", _UnconfiguredLLMClient)

    results = asyncio.run(classify_profiles([_profile(1, level=1, recent_activity_count=0)]))

    assert results[0]["source"] == "evidence_gate"
    assert results[0]["classification"] == "indeterminate"
    assert results[0]["uid"] == 1


def test_classify_profiles_handles_null_results_field(contract_llm) -> None:
    """模型返回 results 为 null 时按空结果处理并回退规则。"""
    contract_llm.payload = '{"results": null}'

    results = asyncio.run(classify_profiles([_profile(3)]))

    assert results[0]["uid"] == 3
    assert results[0]["source"] == "evidence_gate"
    assert results[0]["classification"] == "indeterminate"
