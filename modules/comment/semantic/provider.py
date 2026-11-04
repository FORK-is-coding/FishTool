"""评论语义折叠 · 模型调用适配层。

`ChatSemanticProvider` 把一次语义分组请求封装成**一次普通 chat completion**：
- 异步上下文管理器：进入时才构造 OpenAI 兼容客户端，退出时释放连接。
- `identity` / `configured`：无需建客户端即可读取（供上层做身份固定 / 一致性
  校验与配置探测），且**不把 API key 放进任何返回值**。
- `group(items, video_title, phase)`：发送脱敏载荷（只含 task-local id + 原文，
  绝不含 uid/uname/rpid），用固定系统提示词做注入防御、不开放任何工具，
  严格解析并校验响应结构后返回。

边界约定
--------
- 一次 group 只发一次请求；**不做任何隐式重试**，重试 / 预算 / 记账由上层负责。
- 失败一律抛语义异常（已脱敏，不含原始服务端报文）：响应内容非法抛
  `SemanticValidationError`；传输 / 参数层失败抛 `SemanticProviderError` 并带
  `category`，其中 `UNSUPPORTED_TEMPERATURE` 提示上层可做一次省略 temperature 的重试。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from core.config import config
from core.logger import get_logger
from core.exceptions import (
    LLMError,
    LLMNotConfiguredError,
    LLMAPIError,
    TokenLimitExceededError,
)
from llm.client import LLMClient

from .contracts import (
    PHASE_BATCH,
    VALID_PHASES,
    SYSTEM_PROMPT,
    OUTPUT_TOKENS,
    ProviderErrorCategory,
    SemanticProviderError,
    SemanticValidationError,
    build_user_payload,
    serialize_payload,
    parse_model_json,
    validate_grouping,
)

logger = get_logger(__name__)

def _normalize_base(api_base: str) -> str:
    """归一化 api_base：去掉末尾 '/'，保留 scheme/host/port/path。"""
    if not isinstance(api_base, str):
        return ""
    return api_base.rstrip("/")


def _read_api_key() -> Optional[str]:
    """读取 API key（加密 secrets 优先，兼容旧明文配置）。仅内部用，不外泄。"""
    return config.get_secret("llm_api_key") or config.get("llm.api_key")


#: 服务商余额 / 配额用尽的报文特征（小写匹配；不回显原文）。
#: 只收明确表示“余额 / 配额用尽”的说法。不收 "billing" 这类泛词：鉴权失败的报文里也常出现
#: billing 链接，会把 401/403 误判成余额不足，让用户去充值而不是检查 API Key。
#: 也不收裸 "quota exceeded"：Gemini 分钟级限频报文是 "Quota exceeded for metric:
#: generate_requests_per_minute..."，子串匹配会把 429 限频误判成余额不足。
_QUOTA_MARKERS = (
    "insufficient_quota", "insufficient quota", "insufficient balance", "insufficient_balance",
    "exceeded your current quota", "quota_exceeded",
    "余额不足", "账户余额不足", "欠费", "arrearage",
)


def _classify_api_error(err: LLMAPIError) -> str:
    """把 LLMAPIError 归类为脱敏的 ProviderErrorCategory。

    优先用异常上附带的 status_code；辅以消息里的无敏感关键词判别。
    绝不把原始服务端报文带出去。
    """
    status = getattr(err, "status_code", None)
    text = (getattr(err, "detail", "") or str(err) or "").lower()
    C = ProviderErrorCategory

    # 参数不被支持：部分兼容端点不接受 temperature，上层可省略后重试一次
    if "temperature" in text and (
            "unsupport" in text or "not support" in text
            or "does not support" in text or "unknown" in text
            or "invalid" in text):
        return C.UNSUPPORTED_TEMPERATURE
    if "model_not_found" in text or "模型不存在" in str(err) or status == 404:
        return C.MODEL_NOT_FOUND
    # 余额 / 配额用尽必须排在 429 之前：OpenAI 余额用尽返回的就是 429 + insufficient_quota，
    # 当成限频会让用户“稍后再试”，而等多久都不会恢复。401 不看报文特征，一律是鉴权失败。
    if status == 402 or (status != 401 and any(k in text for k in _QUOTA_MARKERS)):
        return C.QUOTA
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return C.RATE_LIMIT
    if status in (401, 403) or "unauthor" in text or "api key" in text \
            or "invalid key" in text or "forbidden" in text:
        return C.AUTH
    if status is not None and 500 <= status < 600:
        return C.SERVER
    return C.UNKNOWN


def _extract_content(response: Dict[str, Any]) -> str:
    """从 chat completion 响应提取助手文本，并严格校验可用性。

    拒绝：缺 choices / 空 choices / finish_reason 非 stop（截断）/ 显式 refusal /
    缺 message.content。全部抛 SemanticValidationError。
    """
    if not isinstance(response, dict):
        raise SemanticValidationError("模型响应结构非法", reason="bad_response")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise SemanticValidationError("模型响应缺少 choices", reason="no_choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise SemanticValidationError("choice 结构非法", reason="bad_choice")

    # 必须显式以 stop 结束：缺失（None）视为完整性不明，拒绝而非静默接受。
    # 不回显模型给的 finish_reason 原文（可能含不受控文本，防泄漏/注入 UI）。
    finish = choice.get("finish_reason")
    if finish != "stop":
        reason = "missing_finish_reason" if finish is None else "not_stop"
        raise SemanticValidationError("模型响应未正常结束", reason=reason)

    message = choice.get("message")
    if not isinstance(message, dict):
        raise SemanticValidationError("choice 缺少 message", reason="no_message")
    if message.get("refusal"):
        raise SemanticValidationError("模型拒绝作答", reason="refusal")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise SemanticValidationError("模型响应缺少文本内容", reason="empty_content")
    return content


class ChatSemanticProvider:
    """基于 OpenAI 兼容 LLMClient 的语义分组 provider（异步上下文管理器）。

    零参构造、import 期不触网：`provider_factory()` 即 `ChatSemanticProvider()`。
    真正的客户端只在 `async with` 进入时创建。
    """

    def __init__(self,
                 api_base: Optional[str] = None,
                 model: Optional[str] = None,
                 client_factory=None,
                 timeout: int = 60) -> None:
        """固定(可选)身份；不读 key、不建客户端、不触网。

        Args:
            api_base/model: 固定的模型身份；省略时进入上下文时按当前配置解析。
            client_factory: 依赖注入点，签名 (api_key, api_base, model, timeout)
                -> 支持 async with 的 LLMClient；默认用 llm.client.LLMClient。
            timeout: 底层 HTTP 超时秒数。
        """
        self._pinned_base = _normalize_base(api_base) if api_base else None
        self._pinned_model = model or None
        self._client_factory = client_factory or LLMClient
        self._timeout = timeout
        self._client = None
        # 由 service 在创建后按任务预算档位设置：默认档 True（全局日上限照查）；
        # 无限制档为 False（跳过全局日上限检查，用量照常记账）。属性而非 group() 参数：
        # 测试替身的 group 签名是写死的，加参数会破坏所有 FakeProvider。
        self.enforce_daily_limit = True

    # ---- 无需建客户端即可读取的身份 / 配置探测 ----

    @staticmethod
    def is_configured() -> bool:
        """是否已配置可用（有 key + api_base + model）。不建客户端、不触网。"""
        return bool(_read_api_key() and config.get("llm.api_base")
                    and config.get("llm.model"))

    @classmethod
    def current_identity(cls) -> Dict[str, str]:
        """按当前配置解析身份 {api_base, model}（不含 key）。供上层固定 / 校验。"""
        base = _normalize_base(config.get("llm.api_base", "") or "")
        model = config.get("llm.model", "") or ""
        return {"api_base": base, "model": model}

    @property
    def configured(self) -> bool:
        """同步属性：是否已配置可用（key + 解析出的 api_base + model）。

        固定身份优先，故带 pinned model 时即便全局配置缺 model 也算可用。
        不建客户端、不触网。
        """
        ident = self.identity
        return bool(_read_api_key() and ident["api_base"] and ident["model"])

    @property
    def identity(self) -> Dict[str, str]:
        """本 provider 固定的身份 {api_base, model}（不含 key）。

        固定值优先，缺省回落到当前配置；用于任务身份固定与 resume 一致性闸门。
        """
        base = self._pinned_base or _normalize_base(config.get("llm.api_base", "") or "")
        model = self._pinned_model or config.get("llm.model", "") or ""
        return {"api_base": base, "model": model}

    # ---- 异步上下文管理器 ----

    async def __aenter__(self) -> "ChatSemanticProvider":
        """进入时按身份构造底层客户端；未配置则抛脱敏的 provider 错误。"""
        if not self.configured:
            raise SemanticProviderError(
                "LLM 未配置", category=ProviderErrorCategory.NOT_CONFIGURED)
        ident = self.identity
        try:
            self._client = self._client_factory(
                api_key=_read_api_key(),
                api_base=ident["api_base"],
                model=ident["model"],
                timeout=self._timeout,
            )
        except LLMNotConfiguredError as exc:
            raise SemanticProviderError(
                "LLM 未配置", category=ProviderErrorCategory.NOT_CONFIGURED) from exc
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """退出时释放底层连接；关闭失败只记日志，不覆盖业务异常。"""
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不应升级
                logger.warning("关闭语义 provider 客户端失败: %s", exc)
        return False

    # ---- 核心：一次分组请求 ----

    async def group(self,
                    items: Sequence[Dict[str, Any]],
                    video_title: str = "",
                    phase: str = PHASE_BATCH,
                    omit_temperature: bool = False) -> Dict[str, Any]:
        """对一批评论做一次语义分组。

        Args:
            items: [{"id","content"}...]，跨批次可带 "members"；绝不含 uid/uname/rpid。
            video_title: 视频标题（同样当作不可信数据）。
            phase: "batch"（批内）或 "cross"（跨批次合并校验）。
            omit_temperature: True 时请求省略 temperature（供不支持该参数的端点）。

        Returns:
            {"data": 归一化分组, "usage_tokens": int|None, "usage_estimated": bool}

        Raises:
            SemanticValidationError: 响应内容 / 分组结构非法。
            SemanticProviderError: 传输 / 参数 / 配置层失败（带 category）。
        """
        if self._client is None:
            raise SemanticProviderError(
                "provider 未进入上下文", category=ProviderErrorCategory.UNKNOWN)
        if phase not in VALID_PHASES:
            raise SemanticValidationError(f"未知 phase: {phase!r}", reason="bad_phase")

        # 脱敏载荷（sanitize_items 在 build_user_payload 内部执行）
        payload = build_user_payload(items, video_title, phase)
        requested_ids = [it["id"] for it in payload["items"]]
        content_by_id = {it["id"]: it["content"] for it in payload["items"]}

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": serialize_payload(payload)},
        ]

        try:
            # 一次普通 chat completion；temperature=0 求稳定；不传任何 tools。
            response = await self._client.chat_completion(
                messages,
                temperature=0,
                max_tokens=OUTPUT_TOKENS,  # 与预算输出预留一致，不继承全局配置上限。
                stream=False,
                module="comment_semantic",
                omit_temperature=omit_temperature,
                enforce_daily_limit=self.enforce_daily_limit,
            )
        except TokenLimitExceededError as exc:
            raise SemanticProviderError(
                "模型调用触达 token 限额",
                category=ProviderErrorCategory.TOKEN_LIMIT) from exc
        except LLMAPIError as exc:
            raise SemanticProviderError(
                "模型调用失败", category=_classify_api_error(exc)) from exc
        except LLMError as exc:
            # 其余 LLM 错误（网络 / 包装异常）：脱敏为网络类，不带原文
            raise SemanticProviderError(
                "模型调用失败", category=ProviderErrorCategory.NETWORK) from exc

        content = _extract_content(response)
        raw = parse_model_json(content)
        data = validate_grouping(raw, requested_ids, content_by_id=content_by_id)

        usage = response.get("usage") if isinstance(response, dict) else None
        usage_tokens: Optional[int] = None
        usage_estimated = True
        if isinstance(usage, dict):
            total = usage.get("total_tokens")
            if isinstance(total, int) and total >= 0:
                usage_tokens = total
                usage_estimated = False

        return {
            "data": data,
            "usage_tokens": usage_tokens,
            "usage_estimated": usage_estimated,
        }


def provider_factory() -> ChatSemanticProvider:
    """默认工厂：零参、import 期不触网、不建客户端。"""
    return ChatSemanticProvider()
