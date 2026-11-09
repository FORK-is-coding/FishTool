"""基于账号真实公开数据生成 AI 调研报告与运营点评。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from core.logger import get_logger
from core.exceptions import (
    LLMAPIError,
    LLMError,
    LLMNotConfiguredError,
    LLMOutputTruncatedError,
    TokenLimitExceededError,
)
from llm.client import LLMClient

logger = get_logger(__name__)

#: OpenAI 兼容的 JSON 模式：要求服务端只返回一个 JSON 对象（DeepSeek、通义、OpenAI 等支持）。
JSON_RESPONSE_FORMAT = {"type": "json_object"}
#: 端点不认识 response_format 时常见的返回码；只有这两种情况才去掉参数重试。
_UNSUPPORTED_PARAM_STATUS = (400, 422)
#: 400/422 里去掉 response_format 重试也必然失败的错误码（#106）：
#: 输入超长与 JSON 模式无关，重试只会再烧一次全量诊断提示词。
#: 刻意只列已知错误码、不反向要求报文点名 JSON 模式：不少兼容端点（含中文报文、
#: 空报文、``{"error":"Invalid argument"}``）拒绝该参数时根本不说原因。
_NON_RETRYABLE_ERROR_CODES = frozenset({
    "context_length_exceeded",
    "string_above_max_length",
})
#: 单条亮点最大长度：模型偶尔把整段说明塞进 highlights，过长会撑坏前端卡片。
_MAX_HIGHLIGHT_CHARS = 80
#: 对外 error 字段（状态码 + 错误码）长度上限，确保任何拼装都不会膨胀。
_MAX_ERROR_CHARS = 120
#: 错误码只允许这些字符；上游把正文塞进 error/code 时不会通过。
_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class _ResponseFormatError(LLMError):
    """模型回复不是可用的报告 JSON（没有 JSON 对象，或缺 report/commentary）。

    单独成类，是为了和网络错误、超时等同样属于 LLMError 的失败区分开，给出不同的提示。
    """


def _extract_error_code(detail: Any) -> str:
    """从上游错误体里只提炼错误码，绝不把正文透出去。

    上游错误体（``LLMAPIError.detail``）常见形态是
    ``{"error": {"code": "context_length_exceeded", "message": "...", ...}}``，
    里面可能回显 API Key 片段、内部 trace id 或请求片段。这里按 JSON 解析后只取
    ``error.code`` / ``error.type`` / 顶层 ``code``，取不到就返回空串，调用方退化成只报状态码。
    """
    text = str(detail or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        # 非 JSON 报文（HTML 网关页、纯文本）一律不提炼，避免把正文当错误码。
        return ""
    candidates: list[Any] = []
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            candidates = [error.get("code"), error.get("type")]
        # 顶层 code 是常见的结构化错误码；``error`` 为字符串时直接丢弃，
        # 否则整段上游正文会被当作错误码透给前端。
        candidates.append(data.get("code"))
    for item in candidates:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            continue
        code = str(item).strip()
        if _ERROR_CODE_RE.fullmatch(code):
            return code
    return ""


class AIDiagnosisReporter:
    """将自诊结构化数据转换为可展示的 AI 运营结论。"""

    SYSTEM_PROMPT = (
        # 约束模型：只依据真实数据判断、建议可执行可量化，避免幻觉污染结论。
        "你是资深B站内容运营分析师。只依据提供的真实公开数据判断，"
        "不得虚构完播率、观众画像或流量来源。建议必须具体、可执行、可量化。"
        "禁止把未知当 0、把部分采集当全部、把均播/粉丝比当触达率或流量来源；"
        "遇到不可用/部分采集字段时必须明确说明数据不足。"
    )

    async def generate(self, self_data: dict[str, Any]) -> dict[str, Any]:
        """调用已配置 LLM 生成调研报告与点评。

        Args:
            self_data: SelfAnalyzer 产出的账号真实数据。

        Returns:
            包含报告、点评、亮点和生成状态的字典；调用失败时返回可展示的降级信息。
        """
        payload = self._build_payload(self_data)
        # 要求严格 JSON 输出且不用 Markdown 代码块，便于后续直接解析。
        prompt = (
            "请基于以下账号数据输出严格 JSON，不要使用 Markdown 代码块。结构必须为："
            '{"report":"180-260字调研报告","commentary":"100-180字运营点评",'
            '"highlights":["结论1","结论2","结论3"]}。\n'
            f"账号数据：{json.dumps(payload, ensure_ascii=False)}"
        )
        client = None
        try:
            # 低温度保证结论稳定。不传 max_tokens：由 LLMClient 读取配置 llm.max_tokens，
            # 写死的上限会让配置失效，输出被截断后 JSON 不完整、解析失败。
            client = LLMClient()
            content = await self._chat_json(client, prompt)
            parsed = self._parse_response(content)
            # 解析成功后带上模型名与生成时间，供前端展示溯源信息。
            return {
                "success": True,
                "report": parsed["report"],
                "commentary": parsed["commentary"],
                "highlights": parsed["highlights"],
                "model": client.model,
                "generated_at": datetime.now().isoformat(),
            }
        except Exception as exc:
            # 失败时返回可展示文案而非抛错，保证自诊大屏其余部分仍可渲染。
            # 文案按原因区分：以前一律提示“检查大模型配置”，配置没问题的用户会白白去改配置。
            kind, message = self._describe_failure(exc)
            logger.warning("[AI自诊] 调研报告生成失败（%s）: %s", kind, exc)
            return {
                "success": False,
                "message": message,
                # 只写状态码/错误码：error 会随 /api/analysis/self-diagnosis 响应原样交给调用方，
                # 而 LLMAPIError.detail 里可能有上游响应体（回显 key、trace id），不往这里带。
                "error": self._safe_error(exc),
                "generated_at": datetime.now().isoformat(),
            }
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception as exc:
                    logger.warning("[AI自诊] 关闭LLM客户端失败: %s", exc)

    @staticmethod
    def _describe_failure(exc: Exception) -> tuple[str, str]:
        """把失败原因归类，返回 (日志里的类别, 给用户看的提示)。

        只有“未配置”和“鉴权失败/模型不存在”才需要用户去改大模型配置；其余情况要么调上限，
        要么稍后重试，要么换模型，提示里写清楚该做什么。
        """
        if isinstance(exc, LLMOutputTruncatedError):
            # 内容不完整，不能拿半截 JSON 补齐后当结论展示。
            return "输出截断", exc.user_message
        if isinstance(exc, TokenLimitExceededError):
            # 每日上限用完不是配置问题。
            return "每日上限", exc.user_message
        if isinstance(exc, LLMNotConfiguredError):
            return "未配置", "大模型未配置，请在配置页填写 API Key、Base URL 和模型后重新自诊。"
        if isinstance(exc, LLMAPIError):
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                return "鉴权失败", f"模型接口拒绝访问（HTTP {status}），请检查 API Key 是否有效后重新自诊。"
            if status == 429:
                return "限频", "模型接口请求过于频繁或额度不足（HTTP 429），请稍后重新自诊。"
            if str(exc).startswith("LLM模型不存在"):
                return "模型不存在", "配置的模型在服务商处不存在，请在配置页更换模型后重新自诊。"
            label = f"HTTP {status}" if status else "无状态码"
            return "接口错误", f"模型接口返回错误（{label}），请稍后重新自诊；持续出现请查看日志。"
        if isinstance(exc, (_ResponseFormatError, json.JSONDecodeError)):
            return "未按JSON返回", "模型没有按要求返回 JSON，报告无法解析。请重新自诊；多次出现可在配置页换一个模型。"
        if isinstance(exc, LLMError):
            # LLMClient 把网络错误、超时等包装成 LLMError("请求失败: ...")。
            return "请求失败", "请求模型时网络异常或超时，请检查网络后重新自诊。"
        return "内部错误", "AI调研生成时出现内部错误，请重新自诊；持续出现请查看日志。"

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """构造可对外的 error 字段，绝不复述上游响应体。

        ``LLMAPIError`` 的 message 已由 ``LLMClient`` 收敛成“HTTP 状态码”，detail 里才可能有
        上游正文；这里只用 JSON 解析提炼错误码，拼成「HTTP 400 context_length_exceeded」。
        其他异常（截断、每日上限、网络、内部错误）本身不含上游报文，保持原有 ``str(exc)``。
        """
        if not isinstance(exc, LLMAPIError):
            return str(exc)
        status = getattr(exc, "status_code", None)
        code = _extract_error_code(getattr(exc, "detail", ""))
        if not code and str(exc).startswith("LLM模型不存在"):
            # model_not_found 是 LLMClient 自己判定的归类，不含上游正文，保留可检索的错误码。
            code = "model_not_found"
        label = f"HTTP {status}" if status else "无状态码"
        text = f"{label} {code}".strip() if code else label
        return text[:_MAX_ERROR_CHARS]

    @staticmethod
    def _should_retry_without_json_mode(exc: LLMAPIError) -> bool:
        """判断这次失败是否值得去掉 ``response_format`` 重试一次。

        400/422 默认重试（与引入 JSON 模式时的约定一致：兼容端点拒绝该参数时
        常常不说原因）；只有能从 detail 提炼出 ``_NON_RETRYABLE_ERROR_CODES``
        里的错误码时才不重试。其他状态码一律不重试。
        """
        if getattr(exc, "status_code", None) not in _UNSUPPORTED_PARAM_STATUS:
            return False
        code = _extract_error_code(getattr(exc, "detail", "")).lower()
        return code not in _NON_RETRYABLE_ERROR_CODES

    async def _chat_json(self, client: LLMClient, prompt: str) -> str:
        """请求模型并要求服务端返回 JSON 对象；端点不支持 JSON 模式时去掉参数重试一次。

        提示词里已经要求输出 JSON，这里再用 ``response_format`` 让服务端强制约束。
        部分 OpenAI 兼容端点不认识这个参数，会返回 400/422；这时退回只靠提示词约束。
        400/422 里已知与 JSON 模式无关的错误码（如 context_length_exceeded）不重试；
        其他错误（401、429、5xx、网络）原样抛出，不重试。
        """
        kwargs = {"system": self.SYSTEM_PROMPT, "temperature": 0.35}
        try:
            return await client.simple_chat(prompt, response_format=JSON_RESPONSE_FORMAT, **kwargs)
        except LLMAPIError as exc:
            if not self._should_retry_without_json_mode(exc):
                raise
            logger.warning(
                "[AI自诊] 端点拒绝 JSON 模式（HTTP %s），改为只用提示词约束重试",
                exc.status_code,
            )
            return await client.simple_chat(prompt, **kwargs)

    def _build_payload(self, self_data: dict[str, Any]) -> dict[str, Any]:
        """筛选 LLM 所需字段，避免发送冗余接口内容。

        Args:
            self_data: 原始自诊数据。

        Returns:
            仅包含运营分析必要指标的精简字典。
        """
        return {
            # 只挑运营分析必要字段，标签截取前 20 个高频词。
            "uid": self_data.get("uid"),
            "账号": self_data.get("basic_info", {}).get("name"),
            "粉丝": self_data.get("fan_stats", {}).get("follower"),
            "投稿统计": self_data.get("video_stats", {}),
            # 采集覆盖：让模型知道部分/失败状态，禁止把 partial 当全部（§5.5）。
            "视频采集覆盖": self_data.get("video_collection", {}),
            "指标覆盖度": self_data.get("video_stats", {}).get("coverage", {}),
            "互动指标": self_data.get("engagement_metrics", {}),
            "投稿节奏": self_data.get("post_rhythm", {}),
            "高频标签": list(self_data.get("tag_cloud", {}).get("word_frequency", {}).items())[:20],
            "不可用数据": [
                key for key, available in self_data.get("data_availability", {}).items() if not available
            ],
        }

    @staticmethod
    def _normalize_highlights(raw: Any) -> list[str]:
        """把 highlights 收敛成最多 3 条非空字符串。

        模型偶尔不按结构返回：字符串如果被直接迭代会切成单字、字典被迭代成键名、
        数字直接抛 TypeError。这里对非列表值安全降级——字符串整体当一条亮点（不切字），
        其他类型当没有亮点，元素统一转字符串并截断。
        """
        if isinstance(raw, str):
            items: list[Any] = [raw]
        elif isinstance(raw, (list, tuple)):
            items = list(raw)
        else:
            items = []
        highlights: list[str] = []
        for item in items:
            if isinstance(item, (dict, list, tuple, set)) or item is None:
                # 嵌套结构没有可展示的语义，跳过而不是把键名/字符串化结果当亮点。
                continue
            text = str(item).strip()
            if text:
                highlights.append(text[:_MAX_HIGHLIGHT_CHARS])
            if len(highlights) >= 3:
                break
        return highlights

    def _parse_response(self, content: str) -> dict[str, Any]:
        """解析并校验 LLM JSON 响应。

        Args:
            content: LLM 返回文本。

        Returns:
            规范化后的报告结构。

        Raises:
            ValueError: 响应不含有效 JSON 或必要字段为空。
        """
        text = str(content or "").strip()
        # 从第一个 "{" 起用 raw_decode 增量解析：它只吃一个完整 JSON 值，
        # 模型在 JSON 后面追加的自然语言（哪怕带 "}"，如“补充说明见 {附录}”）不会被吞进来。
        # 旧的贪婪正则 r"\{[\s\S]*\}" 会匹配到最后一个 "}"，导致 JSONDecodeError。
        start = text.find("{")
        if start < 0:
            raise _ResponseFormatError("LLM未返回JSON对象")
        data, _end = json.JSONDecoder().raw_decode(text[start:])
        if not isinstance(data, dict):
            raise _ResponseFormatError("LLM返回的JSON不是对象")
        report = str(data.get("report") or "").strip()
        commentary = str(data.get("commentary") or "").strip()
        highlights = self._normalize_highlights(data.get("highlights"))
        # report 与 commentary 为空视为无效响应，触发上层降级。
        if not report or not commentary:
            raise _ResponseFormatError("LLM报告字段不完整")
        return {"report": report, "commentary": commentary, "highlights": highlights}