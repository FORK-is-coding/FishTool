"""评论语义折叠 provider + contracts 组件测试。

覆盖：
- contracts：sanitize_items 脱敏/校验、estimate_request_tokens 保守估算、
  parse_model_json 代码围栏/单对象、validate_grouping 严格校验与一致性守卫。
- provider：identity/configured 不建客户端、异步上下文、group 正常路径、
  响应非法抛 SemanticValidationError、传输失败脱敏为 SemanticProviderError
  且分类正确（含 UNSUPPORTED_TEMPERATURE / omit_temperature 透传）。

测试策略：用假 LLMClient（实现 chat_completion / close，不触网、无 API key），
断言真实调用路径与异常类型；不装新依赖、不扩张无关功能。
"""
from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from core.exceptions import LLMAPIError, LLMError, TokenLimitExceededError
from modules.comment.semantic import contracts as C
from modules.comment.semantic import provider as prov
from modules.comment.semantic.contracts import (
    ProviderErrorCategory,
    SemanticProviderError,
    SemanticValidationError,
)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _FakeClient:
    """假 LLMClient：记录调用、按预置返回/抛错，支持 async close。"""

    def __init__(self, *, response=None, error=None, **init_kwargs):
        self.response = response
        self.error = error
        self.init_kwargs = init_kwargs
        self.calls = []
        self.closed = 0

    async def chat_completion(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self):
        self.closed += 1


def _ok_response(data_json, usage=None):
    """构造一个 finish_reason=stop、content 为 data_json 的成功响应。"""
    choice = {"finish_reason": "stop", "message": {"content": data_json}}
    resp = {"choices": [choice]}
    if usage is not None:
        resp["usage"] = usage
    return resp


def _make_provider(client, **kw):
    """用注入的 client_factory 构造 provider，固定身份避免依赖真实配置。"""
    factory = lambda **_: client  # noqa: E731
    return prov.ChatSemanticProvider(
        api_base="http://test.local/v1/", model="test-model",
        client_factory=factory, **kw)


# ---------------------------------------------------------------------------
# contracts: sanitize_items
# ---------------------------------------------------------------------------


def test_sanitize_items_strips_private_fields():
    """脱敏只保留 id/content(+members)，剔除 uid/uname/rpid 等隐私键。"""
    out = C.sanitize_items([
        {"id": "c0", "content": "hi", "uid": 123, "uname": "x", "rpid": "9",
         "like": 5, "content_hash": "h"},
    ])
    assert out == [{"id": "c0", "content": "hi"}]


def test_sanitize_items_keeps_members_metadata():
    """跨批次 members 元数据保留且必须是非空字符串列表。"""
    out = C.sanitize_items([{"id": "c0", "content": "a", "members": ["c0", "c1"]}])
    assert out[0]["members"] == ["c0", "c1"]


def test_sanitize_items_rejects_bad_id_and_content():
    """缺 id / content 非字符串都抛校验错误。"""
    with pytest.raises(SemanticValidationError):
        C.sanitize_items([{"id": "", "content": "x"}])
    with pytest.raises(SemanticValidationError):
        C.sanitize_items([{"id": "c0", "content": 123}])


def test_sanitize_items_rejects_bad_members():
    """members 含非字符串时抛校验错误。"""
    with pytest.raises(SemanticValidationError):
        C.sanitize_items([{"id": "c0", "content": "a", "members": [1, 2]}])


# ---------------------------------------------------------------------------
# contracts: estimate_request_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_is_bytes_plus_output_and_estimated():
    """输入估算=UTF-8 字节数，叠加输出预留；显式标注 estimated。"""
    est = C.estimate_request_tokens([{"id": "c0", "content": "hello"}],
                                    "title", C.PHASE_BATCH, output_tokens=100)
    payload = C.build_user_payload([{"id": "c0", "content": "hello"}], "title",
                                   C.PHASE_BATCH)
    expected_in = len((C.SYSTEM_PROMPT + C.serialize_payload(payload)).encode("utf-8"))
    assert est.input_tokens == expected_in
    assert est.output_tokens == 100
    assert est.total_tokens == expected_in + 100
    assert est.estimated is True


def test_estimate_tokens_grows_with_content():
    """内容越多估算越大（单调、确定性）。"""
    small = C.estimate_request_tokens([{"id": "c0", "content": "a"}])
    big = C.estimate_request_tokens([{"id": "c0", "content": "a" * 1000}])
    assert big.input_tokens > small.input_tokens


# ---------------------------------------------------------------------------
# contracts: parse_model_json
# ---------------------------------------------------------------------------


def test_parse_model_json_plain_object():
    """纯 JSON 对象可解析。"""
    assert C.parse_model_json('{"a": 1}') == {"a": 1}


def test_parse_model_json_strips_code_fence():
    """去掉 ```json 代码围栏后解析。"""
    assert C.parse_model_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_model_json_rejects_non_object_and_garbage():
    """数组 / 非 JSON 文本都抛校验错误。"""
    with pytest.raises(SemanticValidationError):
        C.parse_model_json("[1, 2, 3]")
    with pytest.raises(SemanticValidationError):
        C.parse_model_json("not json at all")
    with pytest.raises(SemanticValidationError):
        C.parse_model_json("")


# ---------------------------------------------------------------------------
# contracts: validate_grouping
# ---------------------------------------------------------------------------


def test_validate_grouping_happy_path_normalizes():
    """合法分组归一化：补齐 reason/aspect/stance 空串，保留 related/uncertain。"""
    raw = {
        "equivalent_groups": [
            {"representative_id": "c0", "member_ids": ["c0", "c1"], "reason": "同义"}
        ],
        "related_groups": [{"member_ids": ["c2", "c3"], "label": "相关"}],
        "uncertain_ids": ["c4"],
    }
    out = C.validate_grouping(raw, ["c0", "c1", "c2", "c3", "c4"])
    g = out["equivalent_groups"][0]
    assert g["representative_id"] == "c0"
    assert g["member_ids"] == ["c0", "c1"]
    assert g["aspect"] == "" and g["stance"] == ""
    assert out["related_groups"][0]["label"] == "相关"
    assert out["uncertain_ids"] == ["c4"]


def test_validate_grouping_omitted_ids_are_independent():
    """未出现在任何组里的输入 id 合法（视为独立），不报错。"""
    raw = {"equivalent_groups": [{"representative_id": "c0",
                                  "member_ids": ["c0", "c1"], "reason": "x"}],
           "related_groups": [], "uncertain_ids": []}
    out = C.validate_grouping(raw, ["c0", "c1", "c2", "c3"])
    assert out["equivalent_groups"][0]["member_ids"] == ["c0", "c1"]


def test_validate_grouping_rejects_unknown_id():
    """引用 requested 之外的 id 非法。"""
    raw = {"equivalent_groups": [{"representative_id": "c0",
                                  "member_ids": ["c0", "cX"], "reason": "x"}],
           "related_groups": [], "uncertain_ids": []}
    with pytest.raises(SemanticValidationError):
        C.validate_grouping(raw, ["c0", "c1"])


def test_validate_grouping_rejects_intersecting_groups():
    """两个等价组成员相交非法。"""
    raw = {"equivalent_groups": [
        {"representative_id": "c0", "member_ids": ["c0", "c1"], "reason": "x"},
        {"representative_id": "c1", "member_ids": ["c1", "c2"], "reason": "y"},
    ], "related_groups": [], "uncertain_ids": []}
    with pytest.raises(SemanticValidationError):
        C.validate_grouping(raw, ["c0", "c1", "c2"])


def test_validate_grouping_rejects_representative_not_member():
    """representative 不在成员内非法。"""
    raw = {"equivalent_groups": [{"representative_id": "c9",
                                  "member_ids": ["c0", "c1"], "reason": "x"}],
           "related_groups": [], "uncertain_ids": []}
    with pytest.raises(SemanticValidationError):
        C.validate_grouping(raw, ["c0", "c1", "c9"])


def test_validate_grouping_rejects_group_smaller_than_two():
    """等价组成员 <2 非法。"""
    raw = {"equivalent_groups": [{"representative_id": "c0",
                                  "member_ids": ["c0"], "reason": "x"}],
           "related_groups": [], "uncertain_ids": []}
    with pytest.raises(SemanticValidationError):
        C.validate_grouping(raw, ["c0"])


def test_validate_grouping_rejects_oversize_group():
    """等价组成员超过 max_group_size 非法。"""
    members = [f"c{i}" for i in range(C.MAX_GROUP_SIZE + 1)]
    raw = {"equivalent_groups": [{"representative_id": "c0",
                                  "member_ids": members, "reason": "x"}],
           "related_groups": [], "uncertain_ids": []}
    with pytest.raises(SemanticValidationError):
        C.validate_grouping(raw, members)


def test_validate_grouping_rejects_duplicate_member():
    """等价组内成员重复非法。"""
    raw = {"equivalent_groups": [{"representative_id": "c0",
                                  "member_ids": ["c0", "c0"], "reason": "x"}],
           "related_groups": [], "uncertain_ids": []}
    with pytest.raises(SemanticValidationError):
        C.validate_grouping(raw, ["c0", "c1"])


def test_validate_grouping_conflict_guard_demotes_to_uncertain():
    """否定极性不一致的组被整体降级到 uncertain，不作为等价组发布。"""
    raw = {"equivalent_groups": [{"representative_id": "c0",
                                  "member_ids": ["c0", "c1"], "reason": "x"}],
           "related_groups": [], "uncertain_ids": []}
    content = {"c0": "这个结局很好", "c1": "这个结局不好"}
    out = C.validate_grouping(raw, ["c0", "c1"], content_by_id=content)
    assert out["equivalent_groups"] == []
    assert set(out["uncertain_ids"]) == {"c0", "c1"}


def test_validate_grouping_conflict_guard_number_mismatch():
    """关键数字集合不一致的组被降级到 uncertain。"""
    raw = {"equivalent_groups": [{"representative_id": "c0",
                                  "member_ids": ["c0", "c1"], "reason": "x"}],
           "related_groups": [], "uncertain_ids": []}
    content = {"c0": "充了100块", "c1": "充了500块"}
    out = C.validate_grouping(raw, ["c0", "c1"], content_by_id=content)
    assert out["equivalent_groups"] == []
    assert set(out["uncertain_ids"]) == {"c0", "c1"}


# ---------------------------------------------------------------------------
# provider: 身份 / 配置探测（不建客户端、不触网）
# ---------------------------------------------------------------------------


def test_identity_strips_trailing_slash_and_has_no_key():
    """identity 归一化 base（去尾斜杠），只含 api_base/model，无 key。"""
    p = prov.ChatSemanticProvider(api_base="http://h:8080/v1/", model="m")
    assert p.identity == {"api_base": "http://h:8080/v1", "model": "m"}
    assert "api_key" not in p.identity


def test_configured_reads_config_without_building_client(monkeypatch):
    """configured 同步读配置（key+base+model 齐全），不构造客户端。"""
    cfg = {"llm.api_base": "http://h/v1", "llm.model": "m"}
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    monkeypatch.setattr(prov.config, "get", lambda k, d=None: cfg.get(k, d))
    assert prov.ChatSemanticProvider.is_configured() is True
    p = prov.ChatSemanticProvider()
    assert p.configured is True


def test_configured_false_when_no_key(monkeypatch):
    """缺 key 时 configured 为 False。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: None)
    assert prov.ChatSemanticProvider.is_configured() is False


def test_configured_false_when_no_model(monkeypatch):
    """缺 model 时 configured 为 False（身份不完整不可用）。"""
    cfg = {"llm.api_base": "http://h/v1", "llm.model": ""}
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    monkeypatch.setattr(prov.config, "get", lambda k, d=None: cfg.get(k, d))
    assert prov.ChatSemanticProvider.is_configured() is False


def test_current_identity_from_config(monkeypatch):
    """current_identity 按配置解析 {api_base, model}，不含 key。"""
    cfg = {"llm.api_base": "http://h/v1/", "llm.model": "cfg-model"}
    monkeypatch.setattr(prov.config, "get", lambda k, d=None: cfg.get(k, d))
    assert prov.ChatSemanticProvider.current_identity() == {
        "api_base": "http://h/v1", "model": "cfg-model"}


def test_factory_builds_without_network():
    """provider_factory 零参、无网络副作用。"""
    p = prov.provider_factory()
    assert isinstance(p, prov.ChatSemanticProvider)
    assert p._client is None


# ---------------------------------------------------------------------------
# provider: 上下文 + group 正常路径
# ---------------------------------------------------------------------------


def test_aenter_unconfigured_raises_not_configured(monkeypatch):
    """未配置时进入上下文抛 SemanticProviderError(NOT_CONFIGURED)。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: None)
    p = prov.ChatSemanticProvider()

    async def _run():
        async with p:
            pass

    with pytest.raises(SemanticProviderError) as ei:
        asyncio.run(_run())
    assert ei.value.category == ProviderErrorCategory.NOT_CONFIGURED


def test_group_success_returns_data_and_actual_usage(monkeypatch):
    """成功路径：返回校验后的 data，usage 命中则 usage_estimated=False。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    data_json = ('{"equivalent_groups":[{"representative_id":"c0",'
                 '"member_ids":["c0","c1"],"reason":"同义"}],'
                 '"related_groups":[],"uncertain_ids":[]}')
    client = _FakeClient(response=_ok_response(data_json, usage={"total_tokens": 321}))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            return await prov_in.group(
                [{"id": "c0", "content": "a"}, {"id": "c1", "content": "a"}], "标题")

    out = asyncio.run(_run())
    assert out["usage_tokens"] == 321
    assert out["usage_estimated"] is False
    assert out["data"]["equivalent_groups"][0]["member_ids"] == ["c0", "c1"]
    assert client.closed == 1
    # temperature=0 实际传入；未开启 omit
    assert client.calls[0]["kwargs"]["temperature"] == 0
    assert client.calls[0]["kwargs"]["omit_temperature"] is False
    assert client.calls[0]["kwargs"]["module"] == "comment_semantic"


def test_group_missing_usage_marks_estimated(monkeypatch):
    """响应无有效 usage 时 usage_tokens=None、usage_estimated=True。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    data_json = ('{"equivalent_groups":[],"related_groups":[],'
                 '"uncertain_ids":["c0"]}')
    client = _FakeClient(response=_ok_response(data_json))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            return await prov_in.group([{"id": "c0", "content": "a"}])

    out = asyncio.run(_run())
    assert out["usage_tokens"] is None
    assert out["usage_estimated"] is True


def test_group_does_not_leak_private_fields_to_client(monkeypatch):
    """发给模型的载荷不含 uid/uname/rpid。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    data_json = '{"equivalent_groups":[],"related_groups":[],"uncertain_ids":[]}'
    client = _FakeClient(response=_ok_response(data_json))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group(
                [{"id": "c0", "content": "hi", "uid": 1, "uname": "u", "rpid": "9"}])

    asyncio.run(_run())
    sent = client.calls[0]["messages"][1]["content"]
    assert "uid" not in sent and "uname" not in sent and "rpid" not in sent


@pytest.mark.parametrize("phase", [C.PHASE_BATCH, C.PHASE_CROSS])
def test_real_client_caps_output_to_reservation_with_missing_usage(monkeypatch, phase):
    """全局上限 8000 不得覆盖语义模块预留；真实客户端走 MockTransport，无 usage 不虚报 0。"""
    llm = importlib.import_module("llm.client")
    values = {
        "llm.api_base": "http://127.0.0.1:9/v1",
        "llm.model": "offline-test",
        "llm.max_tokens": 8000,
        "llm.temperature": 0.8,
        "llm.daily_token_limit": 100000,
    }
    fake_config = SimpleNamespace(
        get=lambda key, default=None: values.get(key, default),
        get_secret=lambda key, default=None: "test-only-not-a-real-key",
    )
    monkeypatch.setattr(prov, "config", fake_config)
    monkeypatch.setattr(llm, "config", fake_config)
    monkeypatch.setattr(llm, "_db_tokens_used_on", lambda day: 0)
    captured = []
    http_clients = []
    original_http_client = httpx.AsyncClient

    def handle(request):
        captured.append(json.loads(request.content))
        data = {"equivalent_groups": [], "related_groups": [], "uncertain_ids": []}
        return httpx.Response(200, json=_ok_response(json.dumps(data)))

    def http_factory(**kwargs):
        client = original_http_client(transport=httpx.MockTransport(handle), **kwargs)
        http_clients.append(client)
        return client

    monkeypatch.setattr(llm.httpx, "AsyncClient", http_factory)
    items = [{"id": "c0", "content": "评论原文"}]
    title = "预算回归"
    reservation = C.estimate_request_tokens(items, title, phase)

    async def run():
        async with prov.ChatSemanticProvider() as provider:
            assert isinstance(provider._client, llm.LLMClient)
            return await provider.group(items, title, phase)

    result = asyncio.run(run())
    assert len(captured) == 1
    payload = captured[0]
    assert values["llm.max_tokens"] == 8000
    assert payload["max_tokens"] == reservation.output_tokens == C.OUTPUT_TOKENS == 2000
    sent_input_bytes = sum(len(message["content"].encode("utf-8"))
                           for message in payload["messages"])
    assert reservation.input_tokens == sent_input_bytes
    assert reservation.total_tokens == sent_input_bytes + payload["max_tokens"]
    assert reservation.estimated is True
    # None 要求调用方保留已预留的估算，而不是按零用量结算/退款。
    assert result["usage_tokens"] is None
    assert result["usage_estimated"] is True
    assert http_clients[0].is_closed
    assert "module" not in payload and "omit_temperature" not in payload


def test_group_requires_context():
    """未进入上下文直接 group 抛 provider 错误。"""
    p = prov.ChatSemanticProvider(api_base="http://h/v1", model="m")

    with pytest.raises(SemanticProviderError):
        asyncio.run(p.group([{"id": "c0", "content": "a"}]))


# ---------------------------------------------------------------------------
# provider: 响应非法 -> SemanticValidationError
# ---------------------------------------------------------------------------


def test_group_truncated_response_raises_validation(monkeypatch):
    """finish_reason!=stop（截断）抛 SemanticValidationError，且不回显模型文本。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    resp = {"choices": [{"finish_reason": "length",
                         "message": {"content": "{}"}}]}
    client = _FakeClient(response=resp)
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticValidationError) as ei:
        asyncio.run(_run())
    assert ei.value.reason == "not_stop"
    assert "length" not in str(ei.value)


def test_group_missing_finish_reason_rejected(monkeypatch):
    """缺 finish_reason 视为完整性不明，拒绝（不静默接受）。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    resp = {"choices": [{"message": {"content": "{}"}}]}
    client = _FakeClient(response=resp)
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticValidationError) as ei:
        asyncio.run(_run())
    assert ei.value.reason == "missing_finish_reason"


def test_group_refusal_raises_validation(monkeypatch):
    """refusal 字段非空抛 SemanticValidationError。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    resp = {"choices": [{"finish_reason": "stop",
                         "message": {"content": "", "refusal": "no"}}]}
    client = _FakeClient(response=resp)
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticValidationError):
        asyncio.run(_run())


def test_group_no_choices_raises_validation(monkeypatch):
    """缺 choices 抛 SemanticValidationError。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    client = _FakeClient(response={"choices": []})
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticValidationError):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# provider: 传输失败 -> SemanticProviderError（脱敏 + 分类）
# ---------------------------------------------------------------------------


def _api_error(message, status=None, detail=None):
    err = LLMAPIError(message)
    if status is not None:
        err.status_code = status
    if detail is not None:
        err.detail = detail
    return err


def test_group_unsupported_temperature_classified(monkeypatch):
    """temperature 不被支持 -> category=UNSUPPORTED_TEMPERATURE，且不含原始报文。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    err = _api_error("API请求失败: ...", status=400,
                     detail="temperature is not supported by this model")
    client = _FakeClient(error=err)
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticProviderError) as ei:
        asyncio.run(_run())
    assert ei.value.category == ProviderErrorCategory.UNSUPPORTED_TEMPERATURE
    assert "temperature is not supported" not in str(ei.value)


def test_group_omit_temperature_passthrough(monkeypatch):
    """omit_temperature=True 透传到底层 client（且不发 temperature）。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    data_json = '{"equivalent_groups":[],"related_groups":[],"uncertain_ids":[]}'
    client = _FakeClient(response=_ok_response(data_json))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}],
                                omit_temperature=True)

    asyncio.run(_run())
    assert client.calls[0]["kwargs"]["omit_temperature"] is True


def test_group_rate_limit_classified(monkeypatch):
    """429 -> category=RATE_LIMIT。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    client = _FakeClient(error=_api_error("API请求失败", status=429, detail="rate limit"))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticProviderError) as ei:
        asyncio.run(_run())
    assert ei.value.category == ProviderErrorCategory.RATE_LIMIT


@pytest.mark.parametrize("status, detail", [
    (402, "payment required"),
    # OpenAI 余额用尽：429 + insufficient_quota，必须判为余额不足而不是限频
    (429, '{"error":{"code":"insufficient_quota","message":"You exceeded your current quota"}}'),
    (400, "账户余额不足，请充值"),
    (403, "Arrearage: account is in arrears"),
])
def test_group_quota_classified_before_rate_limit(monkeypatch, status, detail):
    """余额 / 配额用尽 -> category=QUOTA（等待不会恢复），且不回显原始报文。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    client = _FakeClient(error=_api_error("API请求失败", status=status, detail=detail))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticProviderError) as ei:
        asyncio.run(_run())
    assert ei.value.category == ProviderErrorCategory.QUOTA
    assert detail not in str(ei.value)


def test_group_plain_429_still_rate_limit(monkeypatch):
    """普通 429（没有余额特征）仍是限频。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    client = _FakeClient(error=_api_error("API请求失败", status=429,
                                          detail="Too Many Requests, please slow down"))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticProviderError) as ei:
        asyncio.run(_run())
    assert ei.value.category == ProviderErrorCategory.RATE_LIMIT


def test_group_gemini_minute_quota_is_rate_limit_not_quota(monkeypatch):
    """Gemini 分钟级限频报文含 "Quota exceeded for metric: ... per_minute"，
    裸 "quota exceeded" 子串不能把它误判成余额不足（等一分钟就好，不是要去充值）。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    client = _FakeClient(error=_api_error(
        "API请求失败", status=429,
        detail='Quota exceeded for metric: generate_requests_per_minute, limit: 60'))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticProviderError) as ei:
        asyncio.run(_run())
    assert ei.value.category == ProviderErrorCategory.RATE_LIMIT


def test_group_auth_classified(monkeypatch):
    """401 -> category=AUTH。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    client = _FakeClient(error=_api_error("API请求失败", status=401, detail="unauthorized"))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticProviderError) as ei:
        asyncio.run(_run())
    assert ei.value.category == ProviderErrorCategory.AUTH


def test_group_passes_enforce_daily_limit_flag(monkeypatch):
    """provider 的 enforce_daily_limit 属性原样传给 chat_completion：
    默认 True（默认档照查全局日上限）；service 按任务档位改成 False。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")

    async def _run(p):
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    ok = _ok_response('{"equivalent_groups": [], "related_groups": [], "uncertain_ids": []}')
    client = _FakeClient(response=ok)
    asyncio.run(_run(_make_provider(client)))
    assert client.calls[0]["kwargs"]["enforce_daily_limit"] is True

    client2 = _FakeClient(response=ok)
    p2 = _make_provider(client2)
    p2.enforce_daily_limit = False
    asyncio.run(_run(p2))
    assert client2.calls[0]["kwargs"]["enforce_daily_limit"] is False


def test_group_token_limit_classified(monkeypatch):
    """底层 TokenLimitExceededError -> category=TOKEN_LIMIT。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    client = _FakeClient(error=TokenLimitExceededError(10, 5))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticProviderError) as ei:
        asyncio.run(_run())
    assert ei.value.category == ProviderErrorCategory.TOKEN_LIMIT


def test_group_generic_llm_error_classified_network(monkeypatch):
    """其余 LLMError -> category=NETWORK，脱敏。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    client = _FakeClient(error=LLMError("请求失败: secret-detail"))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticProviderError) as ei:
        asyncio.run(_run())
    assert ei.value.category == ProviderErrorCategory.NETWORK
    assert "secret-detail" not in str(ei.value)


def test_group_provider_does_not_retry_internally(monkeypatch):
    """provider 不做隐式重试：一次 group 只调用底层一次。"""
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    client = _FakeClient(error=_api_error("API请求失败", status=500, detail="boom"))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticProviderError):
        asyncio.run(_run())
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# PR 1 审查：billing 不是余额不足；401 一律是鉴权失败
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status, detail", [
    (401, "Incorrect API key provided. See https://platform.openai.com/account/billing"),
    (403, "You do not have access. Manage billing at https://example.com/billing"),
    (401, "insufficient_quota"),  # 401 不看报文特征
])
def test_group_auth_with_billing_text_is_not_quota(monkeypatch, status, detail):
    monkeypatch.setattr(prov, "_read_api_key", lambda: "sk-x")
    client = _FakeClient(error=_api_error("API请求失败", status=status, detail=detail))
    p = _make_provider(client)

    async def _run():
        async with p as prov_in:
            await prov_in.group([{"id": "c0", "content": "a"}])

    with pytest.raises(SemanticProviderError) as ei:
        asyncio.run(_run())
    assert ei.value.category == ProviderErrorCategory.AUTH
