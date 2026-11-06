"""SentimentAnalyzer 的 LLM 分批总结（modules/comment/sentiment.py）。

覆盖：
- 请求不写死 max_tokens，交给配置 llm.max_tokens；
- 回复被截断（finish_reason=length）时该批记为 error、合并时跳过，不把半截 JSON 当原文保留；
- 正常回复照常解析。

LLM 使用契约级假客户端（只实现 chat_completion），不访问网络。仓库未装 pytest-asyncio，用 asyncio.run 驱动。
"""
from __future__ import annotations

import asyncio
import json

from modules.comment.sentiment import SentimentAnalyzer

SUMMARY = {
    "key_points": ["观点1"],
    "positive_highlights": ["亮点"],
    "negative_concerns": ["问题"],
    "unusual_signals": [],
}
BATCH = [{"content": "很好看"}, {"content": "有点无聊"}]
BASE = {"positive_ratio": 0.5, "negative_ratio": 0.5, "neutral_ratio": 0.0}


class _FakeLLM:
    def __init__(self, response: dict) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _reply(content: str, finish_reason: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def test_summary_request_does_not_hard_code_max_tokens() -> None:
    llm = _FakeLLM(_reply(json.dumps(SUMMARY, ensure_ascii=False)))
    analyzer = SentimentAnalyzer(llm_client=llm, use_llm_summary=True)

    summary = asyncio.run(analyzer._llm_summarize_batch(BATCH, BASE))

    assert "max_tokens" not in llm.calls[0]
    assert summary["key_points"] == ["观点1"]


def test_truncated_summary_is_an_error_not_raw_text() -> None:
    """截断的半截 JSON 不再以 {'raw': ...} 保留，而是记为该批 error 并提示调大输出上限。"""
    llm = _FakeLLM(_reply('{"key_points": ["写到一半', "length"))
    analyzer = SentimentAnalyzer(llm_client=llm, use_llm_summary=True)

    summary = asyncio.run(analyzer._llm_summarize_batch(BATCH, BASE))

    assert "raw" not in summary
    assert "单次输出上限" in summary["error"]


def test_truncated_batches_are_skipped_when_merging() -> None:
    llm = _FakeLLM(_reply('{"key_points": ["写到一半', "length"))
    analyzer = SentimentAnalyzer(llm_client=llm, use_llm_summary=True)

    result = asyncio.run(analyzer.analyze_with_llm_summary(BATCH, batch_size=1))

    assert result["used_llm"] is True
    assert result["llm_summary"]["key_points"] == []
