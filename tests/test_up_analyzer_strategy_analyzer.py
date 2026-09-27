"""运营策略分析器的契约级测试。

覆盖 modules/up_analyzer/strategy_analyzer.py 的提示词构造、LLM 调用降级、
身份回显校验与五维结构化解析。LLM 依赖使用契约级假客户端（真实实现 await 调用）。
"""

import asyncio
from datetime import datetime

import pytest

from core.exceptions import LLMError, LLMNotConfiguredError

from modules.up_analyzer.strategy_analyzer import StrategyAnalyzer


# ------------------------------------------------------------------ 契约级假客户端

class _ContractLLMClient:
    """契约级 LLM 客户端：实现 chat_completion 契约，不访问网络。"""

    def __init__(self, response=None, error=None) -> None:
        """配置返回值或待抛异常。"""
        self._response = response
        self._error = error
        self.calls = []

    async def chat_completion(self, messages=None, **kwargs):
        """记录调用参数并返回预置响应或抛出预置异常。"""
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self._error is not None:
            raise self._error
        return self._response


class _UnconfiguredClient:
    """契约级未配置客户端：构造即抛 LLMNotConfiguredError。"""

    def __init__(self, *args, **kwargs) -> None:
        """模拟缺少密钥时的构造失败。"""
        raise LLMNotConfiguredError("LLM API密钥未配置")


class _AutoConfiguredClient:
    """契约级可自动配置客户端：构造成功，用于验证 has_llm 分支。"""

    def __init__(self, *args, **kwargs) -> None:
        """记录构造参数。"""
        self.init_kwargs = kwargs


# ------------------------------------------------------------------ 数据与响应构造

def _up_data(uid=42, name="测试UP", **overrides) -> dict:
    """构造一份标准 UP 主数据，允许逐字段覆盖。"""
    payload = {
        "uid": uid,
        "data_source": "bilibili+local",
        "completeness": "partial",
        "data": {
            "name": name,
            "fans": 12345,
            "total_play": 678900,
            "metrics": {"post_frequency": 2.5, "engagement_rate": 3.1, "avg_play": 4567},
            "video_list": [
                {"title": "视频一", "play": 1000},
                {"title": "视频二", "play": 2000},
            ],
        },
    }
    payload.update(overrides)
    return payload


def _valid_content(uid=42, name="测试UP") -> str:
    """构造包含身份回显与五维结构的合法模型输出。"""
    lines = [f"身份核对: UID={uid} | 昵称={name}"]
    for dimension, marker in [
        ("选题方向", "A"),
        ("标题套路", "B"),
        ("封面风格", "C"),
        ("发布节奏", "D"),
        ("互动引导", "E"),
    ]:
        lines.append(f"===== {dimension} =====")
        lines.append(f"核心观察: 观察{marker}")
        lines.append(f"具体打法: 打法{marker}")
        lines.append(f"关键要点: 要点{marker}")
    return "\n".join(lines)


def _chat_response(content: str) -> dict:
    """把文本包装成 OpenAI 兼容返回结构。"""
    return {"choices": [{"message": {"content": content}}]}


# ================================================================ __init__

def test_init_uses_injected_client() -> None:
    """显式注入客户端时直接视为已配置。"""
    client = _ContractLLMClient()

    analyzer = StrategyAnalyzer(client)

    assert analyzer.has_llm is True
    assert analyzer.llm_client is client


def test_init_marks_unconfigured_when_client_creation_fails(monkeypatch) -> None:
    """自动创建客户端失败时应标记为未配置而非抛异常。"""
    monkeypatch.setattr("modules.up_analyzer.strategy_analyzer.LLMClient", _UnconfiguredClient)

    analyzer = StrategyAnalyzer()

    assert analyzer.has_llm is False


def test_init_succeeds_when_client_auto_configured(monkeypatch) -> None:
    """自动创建成功时应标记为已配置。"""
    monkeypatch.setattr("modules.up_analyzer.strategy_analyzer.LLMClient", _AutoConfiguredClient)

    analyzer = StrategyAnalyzer()

    assert analyzer.has_llm is True
    assert isinstance(analyzer.llm_client, _AutoConfiguredClient)


# ================================================================ format_up_data_for_prompt

def test_format_up_data_marks_zeroroku_full_source() -> None:
    """完整数据源应标注为 zeroroku 三方平台。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())

    text = analyzer.format_up_data_for_prompt(_up_data(completeness="full"))

    assert "目标UP主UID: 42" in text
    assert "目标UP主昵称（仅作身份核对）: 测试UP" in text
    assert "数据来源: zeroroku三方平台（完整数据）" in text


def test_format_up_data_marks_partial_source_for_unknown_completeness() -> None:
    """非 full 的完整度一律标注为部分数据。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())

    text = analyzer.format_up_data_for_prompt(_up_data(completeness="unknown"))

    assert "数据来源: B站公开页面+本地估算（部分数据）" in text


def test_format_up_data_formats_numbers_with_thousand_separators() -> None:
    """粉丝数、累计播放与平均播放应带千分位。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())

    text = analyzer.format_up_data_for_prompt(_up_data())

    assert "目标UP主累计播放量: 678,900" in text
    assert "粉丝数: 12,345" in text
    assert "平均播放: 4,567" in text
    assert "投稿频率: 2.5视频/周" in text
    assert "互动率: 3.1%" in text


def test_format_up_data_omits_zero_valued_metrics() -> None:
    """指标为 0 或缺失时不应输出空行。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())
    data = _up_data()
    data["data"]["fans"] = 0
    data["data"]["metrics"] = {"post_frequency": 0, "engagement_rate": 0, "avg_play": 0}

    text = analyzer.format_up_data_for_prompt(data)

    assert "粉丝数" not in text
    assert "投稿频率" not in text
    assert "互动率" not in text
    assert "平均播放" not in text


def test_format_up_data_uses_placeholder_when_name_missing() -> None:
    """昵称缺失时使用占位文案，避免模型推测身份。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())
    data = _up_data()
    data["data"]["name"] = ""

    text = analyzer.format_up_data_for_prompt(data)

    assert "目标UP主昵称（仅作身份核对）: 未获取到昵称" in text


def test_format_up_data_lists_videos_with_play_counts() -> None:
    """近期视频应带序号与播放量列出。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())

    text = analyzer.format_up_data_for_prompt(_up_data())

    assert "近期视频数据（共2个）:" in text
    assert "1. 视频一 (播放: 1,000)" in text
    assert "2. 视频二 (播放: 2,000)" in text


def test_format_up_data_tolerates_empty_payload() -> None:
    """空数据不得抛异常，UID 与昵称走占位输出。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())

    text = analyzer.format_up_data_for_prompt({})

    assert "目标UP主UID: None" in text
    assert "未获取到昵称" in text
    assert "数据来源: B站公开页面+本地估算（部分数据）" in text


# ================================================================ build_analysis_prompt

def test_build_analysis_prompt_contains_identity_and_dimensions() -> None:
    """提示词必须包含身份边界、目标 UID 昵称以及五个维度的输出模板。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())

    prompt = analyzer.build_analysis_prompt(_up_data())

    assert "UID=42" in prompt
    assert "昵称=测试UP" in prompt
    assert "不得使用训练记忆" in prompt
    for dimension in ["选题方向", "标题套路", "封面风格", "发布节奏", "互动引导"]:
        assert f"===== {dimension} =====" in prompt
    # 五维结构各带三层小节。
    assert prompt.count("核心观察:") == 5
    assert prompt.count("具体打法:") == 5
    assert prompt.count("关键要点:") == 5


def test_build_analysis_prompt_embeds_formatted_data() -> None:
    """提示词应内嵌格式化后的实时数据。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())

    prompt = analyzer.build_analysis_prompt(_up_data())

    assert "目标UP主累计播放量: 678,900" in prompt
    assert "近期视频数据（共2个）:" in prompt


# ================================================================ analyze_strategy

def test_analyze_strategy_returns_hint_when_llm_not_configured() -> None:
    """未配置 LLM 时返回引导信息，而不是报错。"""
    analyzer = StrategyAnalyzer.__new__(StrategyAnalyzer)
    analyzer.llm_client = None
    analyzer.has_llm = False

    result = asyncio.run(analyzer.analyze_strategy(_up_data()))

    assert result["success"] is False
    assert result["error"] == "llm_not_configured"
    assert "需要配置大模型API" in result["message"]
    assert "Base URL" in result["config_hint"]


def test_analyze_strategy_parses_valid_response() -> None:
    """合法响应应返回五维解析结果与原始文本。"""
    client = _ContractLLMClient(response=_chat_response(_valid_content()))
    analyzer = StrategyAnalyzer(client)

    result = asyncio.run(asyncio.wait_for(analyzer.analyze_strategy(_up_data()), timeout=5))

    assert result["success"] is True
    assert result["uid"] == 42
    assert result["data_completeness"] == "partial"
    assert result["analysis"]["选题方向"]["核心观察"] == "观察A"
    assert result["analysis"]["互动引导"]["关键要点"] == "要点E"
    assert result["raw_text"] == _valid_content()
    assert isinstance(datetime.fromisoformat(result["analyzed_at"]), datetime)


def test_analyze_strategy_sends_identity_constrained_messages() -> None:
    """调用参数必须携带身份约束系统提示词与固定采样参数。"""
    client = _ContractLLMClient(response=_chat_response(_valid_content()))
    analyzer = StrategyAnalyzer(client)

    asyncio.run(analyzer.analyze_strategy(_up_data()))

    call = client.calls[0]
    system_message = call["messages"][0]
    user_message = call["messages"][1]
    assert system_message["role"] == "system"
    assert "UID=42" in system_message["content"]
    assert "昵称=测试UP" in system_message["content"]
    assert "身份核对" in system_message["content"]
    assert user_message["role"] == "user"
    assert call["kwargs"] == {"temperature": 0.7, "max_tokens": 2000}


def test_analyze_strategy_rejects_missing_identity_echo() -> None:
    """模型未按要求回显身份时应拒绝整份分析（从外部打异常结果结构）。"""
    content = "===== 选题方向 =====\n核心观察: 无身份回显"
    analyzer = StrategyAnalyzer(_ContractLLMClient(response=_chat_response(content)))

    result = asyncio.run(analyzer.analyze_strategy(_up_data()))

    assert result["success"] is False
    assert result["error"] == "llm_error"
    assert "未按要求回显" in result["message"]


def test_analyze_strategy_rejects_uid_mismatch() -> None:
    """回显 UID 与目标不符时必须拒绝，避免串号分析。"""
    analyzer = StrategyAnalyzer(
        _ContractLLMClient(response=_chat_response(_valid_content(uid=999)))
    )

    result = asyncio.run(analyzer.analyze_strategy(_up_data()))

    assert result["success"] is False
    assert result["error"] == "llm_error"
    assert "其他UP主身份" in result["message"]


def test_analyze_strategy_rejects_name_mismatch() -> None:
    """回显昵称与目标不符时同样拒绝。"""
    analyzer = StrategyAnalyzer(
        _ContractLLMClient(response=_chat_response(_valid_content(name="别的UP主")))
    )

    result = asyncio.run(analyzer.analyze_strategy(_up_data()))

    assert result["success"] is False
    assert result["error"] == "llm_error"
    assert "其他UP主身份" in result["message"]


def test_analyze_strategy_rejects_empty_choices() -> None:
    """返回体没有可用内容时应报 llm_error。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient(response={"choices": []}))

    result = asyncio.run(analyzer.analyze_strategy(_up_data()))

    assert result["success"] is False
    assert result["error"] == "llm_error"
    assert "返回内容为空" in result["message"]


def test_analyze_strategy_rejects_response_without_choices_key() -> None:
    """返回体缺少 choices 字段时按空内容处理。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient(response={}))

    result = asyncio.run(analyzer.analyze_strategy(_up_data()))

    assert result["success"] is False
    assert result["error"] == "llm_error"


def test_analyze_strategy_reports_llm_error_from_client() -> None:
    """客户端抛出 LLMError 时应归类为 llm_error。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient(error=LLMError("上游限流")))

    result = asyncio.run(analyzer.analyze_strategy(_up_data()))

    assert result["success"] is False
    assert result["error"] == "llm_error"
    assert "上游限流" in result["message"]


def test_analyze_strategy_reports_unknown_error_from_client() -> None:
    """客户端抛出非 LLM 异常时应归类为 unknown_error。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient(error=ValueError("解析炸了")))

    result = asyncio.run(analyzer.analyze_strategy(_up_data()))

    assert result["success"] is False
    assert result["error"] == "unknown_error"
    assert "解析炸了" in result["message"]


def test_analyze_strategy_handles_missing_data_name() -> None:
    """目标昵称缺失时用占位文案参与身份校验。"""
    content = _valid_content(name="未获取到昵称")
    analyzer = StrategyAnalyzer(_ContractLLMClient(response=_chat_response(content)))
    payload = _up_data()
    payload["data"]["name"] = ""

    result = asyncio.run(analyzer.analyze_strategy(payload))

    assert result["success"] is True


# ================================================================ parse_analysis_result

def test_parse_analysis_result_reads_separator_format() -> None:
    """标准分隔符格式应被完整解析。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())

    parsed = analyzer.parse_analysis_result(_valid_content())

    assert parsed["选题方向"] == {"核心观察": "观察A", "具体打法": "打法A", "关键要点": "要点A"}
    assert parsed["互动引导"]["核心观察"] == "观察E"


def test_parse_analysis_result_normalizes_markdown_decorations() -> None:
    """加粗、标题与列表前缀都应被兼容。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())
    text = "\n".join(
        [
            "**选题方向**",
            "核心观察: 加粗标题",
            "## 标题套路",
            "核心观察：全角冒号",
            "- 封面风格",
            "具体打法: 列表前缀",
            "3. 发布节奏",
            "关键要点: 数字前缀",
        ]
    )

    parsed = analyzer.parse_analysis_result(text)

    assert parsed["选题方向"]["核心观察"] == "加粗标题"
    assert parsed["标题套路"]["核心观察"] == "全角冒号"
    assert parsed["封面风格"]["具体打法"] == "列表前缀"
    assert parsed["发布节奏"]["关键要点"] == "数字前缀"


def test_parse_analysis_result_accumulates_multiline_fields() -> None:
    """字段正文跨行时应按原顺序累积。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())
    text = "\n".join(
        [
            "===== 互动引导 =====",
            "核心观察: 第一行",
            "第二行",
            "具体打法:",
            "续行内容",
        ]
    )

    parsed = analyzer.parse_analysis_result(text)

    assert parsed["互动引导"]["核心观察"] == "第一行\n第二行"
    assert parsed["互动引导"]["具体打法"] == "续行内容"


def test_parse_analysis_result_returns_all_dimensions_for_empty_text() -> None:
    """空文本应返回结构完整但内容为空的五维结果。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())

    parsed = analyzer.parse_analysis_result("")

    assert set(parsed) == {"选题方向", "标题套路", "封面风格", "发布节奏", "互动引导"}
    for dimension in parsed.values():
        assert set(dimension) == {"核心观察", "具体打法", "关键要点"}
        assert all(value == "" for value in dimension.values())


def test_parse_analysis_result_ignores_content_before_dimension() -> None:
    """维度标题之前的散落文本不应被写入任何字段。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())
    text = "\n".join(
        [
            "身份核对: UID=42 | 昵称=测试UP",
            "一些前言",
            "核心观察: 孤立字段",
            "===== 封面风格 =====",
            "核心观察: 正确归属",
        ]
    )

    parsed = analyzer.parse_analysis_result(text)

    assert parsed["封面风格"]["核心观察"] == "正确归属"
    assert parsed["选题方向"]["核心观察"] == ""


def test_parse_analysis_result_keeps_partial_dimensions() -> None:
    """只解析到部分维度时，其余维度保留空结构而非丢失。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())

    parsed = analyzer.parse_analysis_result("===== 选题方向 =====\n核心观察: 仅一项")

    assert parsed["选题方向"]["核心观察"] == "仅一项"
    assert parsed["选题方向"]["具体打法"] == ""
    assert parsed["标题套路"] == {"核心观察": "", "具体打法": "", "关键要点": ""}


# ================================================================ generate_fallback_tips

def test_generate_fallback_tips_covers_all_dimensions() -> None:
    """降级建议应覆盖五个维度且内容非空。"""
    analyzer = StrategyAnalyzer(_ContractLLMClient())

    tips = analyzer.generate_fallback_tips()

    assert set(tips) == {"选题方向", "标题套路", "封面风格", "发布节奏", "互动引导"}
    assert all(isinstance(value, str) and value for value in tips.values())
