"""modules.self_diagnosis.ai_reporter 测试（第4批 · 其他段）。

覆盖对象：
- AIDiagnosisReporter：SYSTEM_PROMPT、generate（成功/降级/关闭容错）、
  _build_payload（字段筛选与截断）、_parse_response（宽容解析与校验失败）。

测试策略：
- LLM 层使用契约级假客户端（暴露 model / simple_chat / close 协议），不使用 AsyncMock，
  也不发起真实网络请求。
- 仓库未安装 pytest-asyncio，async 用例统一用 ``asyncio.run(...)`` 驱动。
- 解析失败分支一律从外部以 pytest.raises 断言。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.exceptions import LLMError
from modules.self_diagnosis import ai_reporter as ai_reporter_module
from modules.self_diagnosis.ai_reporter import AIDiagnosisReporter


GOOD_JSON = json.dumps(
    {
        "report": "账号整体表现稳健，近期投稿节奏稳定。",
        "commentary": "建议提高更新频率并强化封面标题。",
        "highlights": ["粉丝稳步增长", "互动率良好", "选题贴近热点", "第四条应被截断"],
    },
    ensure_ascii=False,
)


# ---------------------------------------------------------------------------
# 契约级假 LLM 客户端
# ---------------------------------------------------------------------------


class _FakeLLMClient:
    """记录调用参数的假 LLM 客户端，可注入聊天/关闭异常。"""

    def __init__(self, content: str = GOOD_JSON, chat_error: Exception | None = None,
                 close_error: Exception | None = None) -> None:
        self.model = "fake-model-v1"
        self.api_base = "https://fake.example/v1"
        self._content = content
        self._chat_error = chat_error
        self._close_error = close_error
        self.chat_calls: list[dict] = []
        self.closed = 0

    async def simple_chat(self, prompt: str, system: str | None = None,
                          temperature: float | None = None, max_tokens: int | None = None) -> str:
        """记录参数后返回预置内容，或抛出注入异常。"""
        self.chat_calls.append(
            {"prompt": prompt, "system": system, "temperature": temperature, "max_tokens": max_tokens}
        )
        if self._chat_error is not None:
            raise self._chat_error
        return self._content

    async def close(self) -> None:
        """累加关闭次数，或抛出注入异常。"""
        self.closed += 1
        if self._close_error is not None:
            raise self._close_error


class _FakeFactory:
    """统计实例化次数的假构造器。"""

    def __init__(self, client: object | None) -> None:
        self._client = client
        self.calls = 0

    def __call__(self):
        """返回注入的客户端实例。"""
        self.calls += 1
        if isinstance(self._client, Exception):
            raise self._client
        return self._client


def _patch_client(monkeypatch, client) -> _FakeFactory:
    """把 ai_reporter 模块内的 LLMClient 替换为假工厂。"""
    factory = _FakeFactory(client)
    monkeypatch.setattr(ai_reporter_module, "LLMClient", factory)
    return factory


SAMPLE_SELF_DATA = {
    "uid": 42,
    "basic_info": {"name": "测试UP主"},
    "fan_stats": {"follower": 1234},
    "video_stats": {"total": 20},
    "engagement_metrics": {"like_rate": 0.05},
    "post_rhythm": {"per_week": 2},
    "tag_cloud": {"word_frequency": {f"tag{i}": 100 - i for i in range(25)}},
    "data_availability": {"fan_stats": True, "video_stats": False},
}


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT 与 generate 成功路径
# ---------------------------------------------------------------------------


def test_system_prompt_is_non_empty_constraint_text():
    """系统提示词应约束模型只依据真实数据、不得虚构。"""
    prompt = AIDiagnosisReporter.SYSTEM_PROMPT
    assert isinstance(prompt, str) and prompt
    assert "不得虚构" in prompt


def test_generate_success_returns_parsed_report(monkeypatch):
    """generate 成功时应返回报告/点评/亮点与模型溯源信息。"""
    fake = _FakeLLMClient()
    factory = _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert factory.calls == 1
    assert result["success"] is True
    assert result["report"] == "账号整体表现稳健，近期投稿节奏稳定。"
    assert result["commentary"] == "建议提高更新频率并强化封面标题。"
    assert result["highlights"] == ["粉丝稳步增长", "互动率良好", "选题贴近热点"]
    assert result["model"] == "fake-model-v1"
    assert "T" in result["generated_at"]  # isoformat 溯源时间


def test_generate_passes_expected_llm_parameters(monkeypatch):
    """调用 LLM 时应带上系统提示、低温度与 token 上限。"""
    fake = _FakeLLMClient()
    _patch_client(monkeypatch, fake)

    asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    call = fake.chat_calls[0]
    assert call["system"] == AIDiagnosisReporter.SYSTEM_PROMPT
    assert call["temperature"] == pytest.approx(0.35)
    assert call["max_tokens"] == 1200
    assert "严格 JSON" in call["prompt"]


def test_generate_closes_client_after_success(monkeypatch):
    """成功路径也必须关闭 LLM 客户端。"""
    fake = _FakeLLMClient()
    _patch_client(monkeypatch, fake)

    asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))
    assert fake.closed == 1


# ---------------------------------------------------------------------------
# generate 降级路径
# ---------------------------------------------------------------------------


def test_generate_returns_degraded_result_on_llm_error(monkeypatch):
    """LLM 调用失败时返回可展示的降级结构而非抛错。"""
    fake = _FakeLLMClient(chat_error=RuntimeError("no api key"))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is False
    assert "AI调研暂不可用" in result["message"]
    assert result["error"] == "no api key"
    assert "generated_at" in result


def test_generate_returns_degraded_result_on_parse_failure(monkeypatch):
    """LLM 返回非 JSON 文本时同样降级。"""
    fake = _FakeLLMClient(content="抱歉，我无法以 JSON 输出")
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))
    assert result["success"] is False


def test_generate_returns_degraded_when_client_construction_fails(monkeypatch):
    """LLMClient 构造失败时也应降级（finally 中无 client 可关）。"""
    _patch_client(monkeypatch, RuntimeError("constructor boom"))

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))
    assert result["success"] is False
    assert result["error"] == "constructor boom"


def test_generate_swallows_close_failure(monkeypatch):
    """close 抛错不得影响已生成的返回结果。"""
    fake = _FakeLLMClient(close_error=RuntimeError("close boom"))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is True
    assert fake.closed == 1


# ---------------------------------------------------------------------------
# _build_payload 字段筛选
# ---------------------------------------------------------------------------


def test_build_payload_selects_and_truncates_fields():
    """payload 只保留运营必要字段，高频标签截取前 20 个。"""
    payload = AIDiagnosisReporter()._build_payload(SAMPLE_SELF_DATA)

    assert payload["uid"] == 42
    assert payload["账号"] == "测试UP主"
    assert payload["粉丝"] == 1234
    assert payload["投稿统计"] == {"total": 20}
    assert len(payload["高频标签"]) == 20
    assert payload["高频标签"][0] == ("tag0", 100)
    # data_availability 中为 False 的项进入不可用清单
    assert payload["不可用数据"] == ["video_stats"]


def test_build_payload_tolerates_missing_keys():
    """空数据不应抛错，缺失字段以 None/空集合兜底。"""
    payload = AIDiagnosisReporter()._build_payload({})

    assert payload["uid"] is None
    assert payload["账号"] is None
    assert payload["粉丝"] is None
    assert payload["高频标签"] == []
    assert payload["不可用数据"] == []


# ---------------------------------------------------------------------------
# _parse_response 解析与校验
# ---------------------------------------------------------------------------


def test_parse_response_extracts_json_with_surrounding_text():
    """前后带自然语言时应能提取中间 JSON 对象。"""
    text = f"好的，以下是结果：\n{GOOD_JSON}\n希望有帮助。"
    parsed = AIDiagnosisReporter()._parse_response(text)

    assert parsed["report"]
    assert parsed["commentary"]
    assert parsed["highlights"] == ["粉丝稳步增长", "互动率良好", "选题贴近热点"]


def test_parse_response_strips_and_drops_empty_highlights():
    """亮点应去除空白项并只保留前 3 条。"""
    content = json.dumps(
        {"report": "r", "commentary": "c", "highlights": [" a ", "", "  ", "b", "c", "d"]},
        ensure_ascii=False,
    )
    parsed = AIDiagnosisReporter()._parse_response(content)
    assert parsed["highlights"] == ["a", "b", "c"]


def test_parse_response_raises_when_no_json_object():
    """不含花括号对象时应抛 LLMError。"""
    with pytest.raises(LLMError):
        AIDiagnosisReporter()._parse_response("没有任何 JSON")


def test_parse_response_raises_when_report_missing():
    """report/commentary 缺失或为空视为无效响应。"""
    content = json.dumps({"report": "", "commentary": "有内容"}, ensure_ascii=False)
    with pytest.raises(LLMError):
        AIDiagnosisReporter()._parse_response(content)


def test_parse_response_raises_on_malformed_json():
    """花括号存在但 JSON 非法时抛 ValueError（JSONDecodeError 子类）。"""
    with pytest.raises(ValueError):
        AIDiagnosisReporter()._parse_response("{bad json}")


def test_parse_response_handles_none_content():
    """None 输入被字符串化后视为无 JSON，抛 LLMError。"""
    with pytest.raises(LLMError):
        AIDiagnosisReporter()._parse_response(None)
