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
                 close_error: Exception | None = None,
                 json_mode_error: Exception | None = None) -> None:
        self.model = "fake-model-v1"
        self.api_base = "https://fake.example/v1"
        self._content = content
        self._chat_error = chat_error
        self._close_error = close_error
        # 只在带 response_format 的请求上抛出，模拟端点不支持 JSON 模式。
        self._json_mode_error = json_mode_error
        self.chat_calls: list[dict] = []
        self.closed = 0

    async def simple_chat(self, prompt: str, system: str | None = None,
                          temperature: float | None = None, max_tokens: int | None = None,
                          response_format: dict | None = None) -> str:
        """记录参数后返回预置内容，或抛出注入异常。"""
        self.chat_calls.append(
            {"prompt": prompt, "system": system, "temperature": temperature,
             "max_tokens": max_tokens, "response_format": response_format}
        )
        if response_format is not None and self._json_mode_error is not None:
            raise self._json_mode_error
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
    """调用 LLM 时应带上系统提示与低温度；不写死 token 上限，交给配置 llm.max_tokens。"""
    fake = _FakeLLMClient()
    _patch_client(monkeypatch, fake)

    asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    call = fake.chat_calls[0]
    assert call["system"] == AIDiagnosisReporter.SYSTEM_PROMPT
    assert call["temperature"] == pytest.approx(0.35)
    assert call["max_tokens"] is None
    assert "严格 JSON" in call["prompt"]


def test_generate_request_uses_configured_max_tokens(monkeypatch):
    """经真实 LLMClient 发出的请求体里，max_tokens 应等于配置 llm.max_tokens。"""
    from llm import client as llm_module

    class _Config:
        values = {
            "llm.api_base": "http://test.local/v1",
            "llm.model": "test-model",
            "llm.max_tokens": 3456,
            "llm.daily_token_limit": 0,
        }

        def get(self, key, default=None):
            return self.values.get(key, default)

        def get_secret(self, key):
            return "sk-test" if key == "llm_api_key" else None

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": GOOD_JSON}}]}

    class _HTTP:
        def __init__(self, *args, **kwargs):
            self.payloads: list[dict] = []

        async def post(self, url, json=None):
            self.payloads.append(json)
            return _Response()

        async def aclose(self):
            return None

    http_clients: list[_HTTP] = []

    def http_factory(*args, **kwargs):
        http = _HTTP()
        http_clients.append(http)
        return http

    monkeypatch.setattr(llm_module, "config", _Config())
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", http_factory)
    monkeypatch.setattr(llm_module, "_db_tokens_used_on", lambda day: 0)
    monkeypatch.setattr(ai_reporter_module, "LLMClient", llm_module.LLMClient)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is True
    assert http_clients[0].payloads[0]["max_tokens"] == 3456


def _api_error(status_code: int, detail: str | None = None) -> Exception:
    """构造带状态码的 LLMAPIError，与 LLMClient.chat_completion 抛出的形状一致。"""
    from core.exceptions import LLMAPIError

    err = LLMAPIError(f"API请求失败: HTTP {status_code}")
    err.status_code = status_code
    if detail is not None:
        err.detail = detail
    return err


def test_generate_requests_json_object_mode(monkeypatch):
    """第一次请求带 response_format=json_object，让服务端强制只返回 JSON。"""
    fake = _FakeLLMClient()
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is True
    assert len(fake.chat_calls) == 1
    assert fake.chat_calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("status", [400, 422])
def test_generate_retries_without_json_mode_when_endpoint_rejects_it(monkeypatch, status):
    """端点明确抱怨 response_format（400/422）时，去掉参数再请求一次，结果照常可用。"""
    fake = _FakeLLMClient(json_mode_error=_api_error(
        status, detail="Unsupported parameter: 'response_format' is not supported with this model."
    ))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is True
    assert [c["response_format"] for c in fake.chat_calls] == [{"type": "json_object"}, None]
    assert fake.chat_calls[1]["system"] == AIDiagnosisReporter.SYSTEM_PROMPT
    assert fake.chat_calls[1]["temperature"] == pytest.approx(0.35)


@pytest.mark.parametrize("detail", [
    '{"error":{"code":"context_length_exceeded","message":"This model\'s maximum context length is 8192 tokens"}}',
    '{"error":{"code":"invalid_request_error","message":"messages is required"}}',
    "",
])
def test_generate_does_not_retry_400_without_json_mode_signal(monkeypatch, detail):
    """#105：400/422 只要没明确点名 JSON 模式，就不降级重试——重试必然同样失败。

    context_length_exceeded 这类错误重试一次会白烧一整份全量诊断提示词，
    还把真实原因伪装成“端点拒绝 JSON 模式”。
    """
    fake = _FakeLLMClient(json_mode_error=_api_error(400, detail=detail))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is False
    assert len(fake.chat_calls) == 1
    assert fake.chat_calls[0]["response_format"] == {"type": "json_object"}


def test_generate_keeps_upstream_error_code_on_context_length_exceeded(monkeypatch):
    """#105/#108：不重试，且 error 里保留可检索的错误码 context_length_exceeded。"""
    fake = _FakeLLMClient(json_mode_error=_api_error(
        400, detail='{"error":{"code":"context_length_exceeded","message":"too long"}}'
    ))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert len(fake.chat_calls) == 1
    assert "HTTP 400" in result["error"]
    assert "context_length_exceeded" in result["error"]
    assert "too long" not in result["error"]


@pytest.mark.parametrize("status", [401, 429, 500])
def test_generate_does_not_retry_other_api_errors(monkeypatch, status):
    """鉴权、限频、服务端错误与 JSON 模式无关，不去掉参数重试，直接降级。"""
    fake = _FakeLLMClient(json_mode_error=_api_error(status))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is False
    assert len(fake.chat_calls) == 1


def test_json_mode_reaches_request_body(monkeypatch):
    """经真实 LLMClient 发出的请求体里带 response_format，且没有混入其他内部参数。"""
    from llm import client as llm_module

    class _Config:
        values = {"llm.api_base": "http://test.local/v1", "llm.model": "test-model",
                  "llm.max_tokens": 2000, "llm.daily_token_limit": 0}

        def get(self, key, default=None):
            return self.values.get(key, default)

        def get_secret(self, key):
            return "sk-test" if key == "llm_api_key" else None

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": GOOD_JSON}, "finish_reason": "stop"}]}

    payloads: list[dict] = []

    class _HTTP:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, json=None):
            payloads.append(json)
            return _Response()

        async def aclose(self):
            return None

    monkeypatch.setattr(llm_module, "config", _Config())
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", _HTTP)
    monkeypatch.setattr(llm_module, "_db_tokens_used_on", lambda day: 0)
    monkeypatch.setattr(ai_reporter_module, "LLMClient", llm_module.LLMClient)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is True
    assert payloads[0]["response_format"] == {"type": "json_object"}


def test_generate_closes_client_after_success(monkeypatch):
    """成功路径也必须关闭 LLM 客户端。"""
    fake = _FakeLLMClient()
    _patch_client(monkeypatch, fake)

    asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))
    assert fake.closed == 1


# ---------------------------------------------------------------------------
# generate 降级路径
# ---------------------------------------------------------------------------


def test_token_limit_user_message_names_limit_and_settings_page():
    """TokenLimitExceededError.user_message：带用量/上限，指出配置页。"""
    from core.exceptions import TokenLimitExceededError

    msg = TokenLimitExceededError(2_000_001, 2_000_000).user_message
    assert "2000001/2000000" in msg
    assert "配置页" in msg and "每日 token 上限" in msg


def test_generate_returns_degraded_result_on_llm_error(monkeypatch):
    """LLM 调用失败时返回可展示的降级结构而非抛错；原始错误放在 error 字段。"""
    fake = _FakeLLMClient(chat_error=RuntimeError("boom"))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is False
    assert "内部错误" in result["message"]
    assert result["error"] == "boom"
    assert "generated_at" in result


def _not_configured():
    from core.exceptions import LLMNotConfiguredError
    return LLMNotConfiguredError("LLM API密钥未配置")


def _model_not_found():
    from core.exceptions import LLMAPIError
    err = LLMAPIError("LLM模型不存在：x。请在配置中改为 qwen-plus")
    err.status_code = 404
    return err


def _network_error():
    from core.exceptions import LLMError
    return LLMError("请求失败: ConnectTimeout")


@pytest.mark.parametrize("make_error, content, expected", [
    (_not_configured, None, "大模型未配置"),
    (lambda: _api_error(401), None, "检查 API Key"),
    (lambda: _api_error(403), None, "检查 API Key"),
    (_model_not_found, None, "更换模型"),
    (lambda: _api_error(429), None, "HTTP 429"),
    (lambda: _api_error(500), None, "HTTP 500"),
    (_network_error, None, "网络"),
    (None, "抱歉，我无法以 JSON 输出", "没有按要求返回 JSON"),
    (None, "{bad json}", "没有按要求返回 JSON"),
    (None, '{"report": "", "commentary": "c"}', "没有按要求返回 JSON"),
    (None, '["not", "an", "object"]', "没有按要求返回 JSON"),
])
def test_generate_failure_message_matches_cause(monkeypatch, make_error, content, expected):
    """每种失败给出对应提示，不再一律提示“检查大模型配置”。"""
    fake = _FakeLLMClient(
        chat_error=make_error() if make_error else None,
        content=content if content is not None else GOOD_JSON,
    )
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is False
    assert expected in result["message"]
    assert "检查大模型配置" not in result["message"]


@pytest.mark.parametrize("make_error", [
    lambda: _api_error(429), lambda: _api_error(500), _network_error,
])
def test_transient_failures_do_not_send_user_to_config(monkeypatch, make_error):
    """限频、服务端错误、网络问题与配置无关，提示里不让用户去改配置。"""
    fake = _FakeLLMClient(chat_error=make_error())
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert "配置页" not in result["message"]
    assert "API Key" not in result["message"]
    assert "稍后" in result["message"] or "网络" in result["message"]


def test_generate_daily_token_limit_does_not_blame_model_config(monkeypatch):
    """每日上限用完时提示调高上限，而不是让用户去“检查大模型配置”。"""
    from core.exceptions import TokenLimitExceededError

    fake = _FakeLLMClient(chat_error=TokenLimitExceededError(150, 100))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is False
    assert "每日上限" in result["message"]
    assert "配置页" in result["message"]
    assert "检查大模型配置" not in result["message"]
    assert fake.closed == 1  # 仍然关闭客户端


def test_generate_truncated_output_tells_user_to_raise_output_limit(monkeypatch):
    """输出被截断时提示调大单次输出上限，不提示去检查大模型配置。"""
    from core.exceptions import LLMOutputTruncatedError

    fake = _FakeLLMClient(chat_error=LLMOutputTruncatedError(2400, 0, 5000))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is False
    assert "单次输出上限" in result["message"]
    assert "思考模式" in result["message"]
    assert "检查大模型配置" not in result["message"]
    assert "finish_reason=length" in result["error"]
    assert fake.closed == 1


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


# ---------------------------------------------------------------------------
# #106 贪心正则与 highlights 类型守卫
# ---------------------------------------------------------------------------


def test_parse_response_ignores_trailing_text_with_braces():
    """JSON 之后追加含 "}" 的说明不得让整份报告判失败（旧贪婪正则会吞到最后一个 }）。"""
    content = (
        '{"report":"账号稳健","commentary":"提高更新频率","highlights":["粉丝增长"]}\n'
        "补充说明见 {附录}，其中 {a} 与 {b} 都是引用。"
    )
    parsed = AIDiagnosisReporter()._parse_response(content)

    assert parsed["report"] == "账号稳健"
    assert parsed["commentary"] == "提高更新频率"
    assert parsed["highlights"] == ["粉丝增长"]


def test_parse_response_accepts_wrapped_json_with_trailing_brace():
    """前后带自然语言、且尾部含花括号时仍能取出唯一的 JSON 对象。"""
    content = f"以下是结果：\n{GOOD_JSON}\n（详见 {{{{附录}}}}）"
    parsed = AIDiagnosisReporter()._parse_response(content)

    assert parsed["report"] == "账号整体表现稳健，近期投稿节奏稳定。"
    assert parsed["highlights"] == ["粉丝稳步增长", "互动率良好", "选题贴近热点"]


@pytest.mark.parametrize("highlights, expected", [
    ("优秀", ["优秀"]),          # 字符串整体当一条，不切成单字
    (3, []),                     # 数字不迭代、不抛 TypeError
    (None, []),                  # 缺省/None 安全降级
    ({"k1": "v1", "k2": "v2"}, []),  # 字典不把键名当亮点
    ([{"a": 1}, ["x"], 7, " 有效 ", ""], ["7", "有效"]),
    (["a" * 200], ["a" * 80]),   # 元素截断，避免撑坏前端
])
def test_parse_response_normalizes_highlights_types(highlights, expected):
    """highlights 非列表/元素类型异常时安全降级，且不切字、不抛异常。"""
    content = json.dumps({"report": "r", "commentary": "c", "highlights": highlights},
                         ensure_ascii=False)
    parsed = AIDiagnosisReporter()._parse_response(content)

    assert parsed["highlights"] == expected


# ---------------------------------------------------------------------------
# #108 error 字段不得回显上游响应体
# ---------------------------------------------------------------------------


def test_generate_error_field_excludes_upstream_body(monkeypatch):
    """error 只保留状态码与错误码：不出现响应体里的密钥、trace id，长度受控。"""
    body = (
        '{"error":{"code":"invalid_api_key","type":"invalid_request_error","message":"bad key",'
        '"echo":"sk-live-abcdef0123456789","trace_id":"trace-internal-42"},'
        '"request":"' + "x" * 2000 + '"}'
    )
    fake = _FakeLLMClient(chat_error=_api_error(400, detail=body))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["success"] is False
    assert "sk-live" not in result["error"]
    assert "trace-internal-42" not in result["error"]
    assert "x" * 50 not in result["error"]
    assert len(result["error"]) <= 120
    assert "HTTP 400" in result["error"]
    assert "invalid_api_key" in result["error"]


def test_generate_error_field_ignores_string_error_body(monkeypatch):
    """error 是字符串时不能把前 64 字当错误码，必须退回纯状态码。"""
    body = (
        '{"error":"sk-live-abcdef0123456789 trace-internal-42 '
        + "x" * 200
        + '"}'
    )
    fake = _FakeLLMClient(chat_error=_api_error(400, detail=body))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["error"] == "HTTP 400"
    assert "sk-live" not in result["error"]
    assert "trace-internal-42" not in result["error"]


def test_generate_error_field_falls_back_to_status_without_json_body(monkeypatch):
    """非 JSON 报文（HTML 网关页）不提炼错误码，也不把正文透进 error。"""
    fake = _FakeLLMClient(chat_error=_api_error(502, detail="<html>Bad Gateway sk-raw</html>"))
    _patch_client(monkeypatch, fake)

    result = asyncio.run(AIDiagnosisReporter().generate(SAMPLE_SELF_DATA))

    assert result["error"] == "HTTP 502"
    assert "sk-raw" not in result["error"]
    assert "Bad Gateway" not in result["error"]
