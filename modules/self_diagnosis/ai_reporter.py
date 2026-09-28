"""基于账号真实公开数据生成 AI 调研报告与运营点评。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from core.logger import get_logger
from core.exceptions import LLMError
from llm.client import LLMClient

logger = get_logger(__name__)


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
            # 低温度保证结论稳定，max_tokens 控制响应规模。
            client = LLMClient()
            content = await client.simple_chat(
                prompt,
                system=self.SYSTEM_PROMPT,
                temperature=0.35,
                max_tokens=1200,
            )
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
            logger.warning("[AI自诊] 调研报告生成失败: %s", exc)
            return {
                "success": False,
                # 失败时返回可展示文案而非抛错，保证自诊大屏其余部分仍可渲染。
                "message": "AI调研暂不可用，请先检查大模型配置后重新自诊。",
                "error": str(exc),
                "generated_at": datetime.now().isoformat(),
            }
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception as exc:
                    logger.warning("[AI自诊] 关闭LLM客户端失败: %s", exc)

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
        # 宽容匹配第一对花括号，兼容模型偶尔输出的前后缀文本。
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise LLMError("LLM未返回JSON对象")
        # 保存 B 站接口响应，后续从中提取标题、作者和评论区标识。
        data = json.loads(match.group(0))
        report = str(data.get("report") or "").strip()
        commentary = str(data.get("commentary") or "").strip()
        highlights = [str(item).strip() for item in data.get("highlights", []) if str(item).strip()][:3]
        # report 与 commentary 为空视为无效响应，触发上层降级。
        if not report or not commentary:
            raise LLMError("LLM报告字段不完整")
        return {"report": report, "commentary": commentary, "highlights": highlights}