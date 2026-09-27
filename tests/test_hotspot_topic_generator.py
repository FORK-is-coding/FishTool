"""AI 选题助手的契约级测试。

覆盖 modules/hotspot/topic_generator.py 的 TopicGenerator：
- is_llm_available：None / 未配置 / 已配置
- _build_generation_prompt：分区/方向/数量/Top10 标签拼接
- _parse_llm_response：直出数组、包裹文本、非 JSON、损坏 JSON
- generate_topics_with_llm：未配置异常、正常解析、缺 choices、上游异常冒泡
- generate_topics_fallback：模板轮转、单标签/空标签边界
- generate_topics：无热词短路、LLM 优先、降级、LLMNotConfiguredError 兜底
- _save_to_topic_library / get_topic_library / update_topic_status 的持久化契约

LLM 与词云生成器均使用真实实现 __init__ 的契约级假对象（非 AsyncMock），
数据库使用 tmp_path 下的真实 SQLite。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from core.database import DatabaseManager, Topic
from core.exceptions import LLMNotConfiguredError
from modules.hotspot import topic_generator as topic_module
from modules.hotspot.topic_generator import TopicGenerator


# --------------------------------------------------------------------- 假对象

class FakeLLM:
    """契约级假 LLM：可注入响应、配置状态与异常。"""

    def __init__(self, *, response=None, configured: bool = True, error: Exception | None = None) -> None:
        self._response = response
        self._configured = configured
        self._error = error
        self.calls: list[dict] = []

    def is_configured(self) -> bool:
        """返回预置的配置状态。"""
        return self._configured

    async def chat_completion(self, **kwargs):
        """记录调用参数并返回预置响应或抛出预置异常。"""
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class FakeTagGenerator:
    """契约级假词云生成器，返回可控的词频结构。"""

    def __init__(self, word_frequency: dict | None = None) -> None:
        self._word_frequency = dict(word_frequency or {})
        self.calls: list[tuple] = []

    async def generate_cloud_data(self, zone_name, limit=50, top_n=20):
        """返回 {"word_frequency": {...}} 契约结构。"""
        self.calls.append((zone_name, limit, top_n))
        return {"word_frequency": dict(self._word_frequency)}


def _llm_response(topics: list[dict]) -> dict:
    """把选题数组包成 OpenAI 风格响应。"""
    import json

    return {"choices": [{"message": {"content": json.dumps(topics, ensure_ascii=False)}}]}


@pytest.fixture()
def db_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """构造真实临时库并接管模块级 get_session。"""
    manager = DatabaseManager(str(tmp_path / "topic.db"))
    monkeypatch.setattr(topic_module, "get_session", manager.get_session)
    return manager


def _build(generator_tags: dict | None = None, llm=None) -> TopicGenerator:
    """构造注入假依赖的选题助手。"""
    return TopicGenerator(api=object(), llm_client=llm, tag_generator=FakeTagGenerator(generator_tags))


# --------------------------------------------------------------------- is_llm_available

def test_is_llm_available_false_without_client() -> None:
    """未注入 LLM 时不可用。"""
    assert _build().is_llm_available() is False


def test_is_llm_available_false_when_not_configured() -> None:
    """LLM 存在但未配置时不可用。"""
    generator = _build(llm=FakeLLM(configured=False))

    assert generator.is_llm_available() is False


def test_is_llm_available_true_when_configured() -> None:
    """LLM 已配置时可用。"""
    generator = _build(llm=FakeLLM(configured=True))

    assert generator.is_llm_available() is True


# --------------------------------------------------------------------- prompt

def test_build_generation_prompt_contains_context() -> None:
    """提示词应包含分区、方向、数量与前 10 个热词。"""
    generator = _build()
    tags = [f"tag{index}" for index in range(12)]

    prompt = generator._build_generation_prompt("游戏解说", "游戏", tags, 5)

    assert "游戏" in prompt
    assert "游戏解说" in prompt
    assert "5" in prompt
    assert "tag9" in prompt
    assert "tag10" not in prompt


# --------------------------------------------------------------------- 解析

def test_parse_llm_response_direct_array() -> None:
    """以 [ 开头的纯数组应被直接解析。"""
    generator = _build()

    assert generator._parse_llm_response('[{"title": "t"}]') == [{"title": "t"}]


def test_parse_llm_response_extracts_wrapped_array() -> None:
    """说明文字包裹的数组应被正则提取。"""
    generator = _build()

    result = generator._parse_llm_response('好的，结果如下：[{"title": "t"}] 以上。')

    assert result == [{"title": "t"}]


def test_parse_llm_response_returns_empty_without_array() -> None:
    """不含数组时返回空列表。"""
    generator = _build()

    assert generator._parse_llm_response("没有任何 JSON") == []


def test_parse_llm_response_survives_broken_json() -> None:
    """损坏的 JSON 数组应降级为空列表而非抛异常。"""
    generator = _build()

    assert generator._parse_llm_response("[这不是合法 JSON") == []


# --------------------------------------------------------------------- LLM 生成

def test_generate_topics_with_llm_raises_when_not_configured() -> None:
    """LLM 未配置时应抛 LLMNotConfiguredError。"""
    generator = _build()

    with pytest.raises(LLMNotConfiguredError):
        __import__("asyncio").run(
            generator.generate_topics_with_llm("方向", "游戏", ["a", "b"], 3)
        )


def test_generate_topics_with_llm_formats_and_limits() -> None:
    """LLM 返回应被标准化并截断到 count。"""
    import asyncio

    llm = FakeLLM(response=_llm_response([
        {"title": "选题A", "description": "d", "keywords": ["k"], "reason": "r", "difficulty": "easy", "related_tags": ["a"]},
        {"title": "选题B"},
        {"title": "选题C"},
    ]))
    generator = _build(llm=llm)

    topics = asyncio.run(generator.generate_topics_with_llm("方向", "游戏", ["a", "b"], 2))

    assert [item["title"] for item in topics] == ["选题A", "选题B"]
    assert topics[0]["difficulty"] == "easy"
    assert topics[0]["zone_name"] == "游戏"
    assert topics[0]["direction"] == "方向"
    assert topics[0]["status"] == "pending"
    assert isinstance(topics[0]["generated_at"], datetime)
    # 未提供字段应给出默认值
    assert topics[1]["difficulty"] == "medium"
    assert topics[1]["keywords"] == []


def test_generate_topics_with_llm_handles_missing_choices() -> None:
    """响应缺少 choices 时按空内容处理，返回空选题。"""
    import asyncio

    generator = _build(llm=FakeLLM(response={"foo": "bar"}))

    assert asyncio.run(generator.generate_topics_with_llm("方向", "游戏", ["a"], 5)) == []


def test_generate_topics_with_llm_propagates_upstream_error() -> None:
    """LLM 调用异常应继续向上抛出，由调用方决定降级。"""
    import asyncio

    generator = _build(llm=FakeLLM(error=RuntimeError("llm down")))

    with pytest.raises(RuntimeError):
        asyncio.run(generator.generate_topics_with_llm("方向", "游戏", ["a"], 5))


# --------------------------------------------------------------------- 降级方案

def test_fallback_cycles_templates_and_fills_tags() -> None:
    """降级方案应轮转模板并用热词填充。"""
    import asyncio

    generator = _build()

    topics = asyncio.run(generator.generate_topics_fallback("游戏解说", "游戏", ["a", "b", "c"], 2))

    assert len(topics) == 2
    assert "a" in topics[0]["title"]
    assert topics[0]["keywords"] == ["a", "b", "游戏解说"]
    assert topics[0]["related_tags"] == ["a", "b"]
    assert topics[0]["difficulty"] == "medium"


def test_fallback_limited_by_available_tags() -> None:
    """生成数量不能超过热词数量。"""
    import asyncio

    generator = _build()

    topics = asyncio.run(generator.generate_topics_fallback("方向", "分区", ["only"], 10))

    assert len(topics) == 1
    assert topics[0]["related_tags"] == ["only", "创意内容"]


def test_fallback_empty_tags_produces_nothing() -> None:
    """无热词时不生成任何选题。"""
    import asyncio

    generator = _build()

    assert asyncio.run(generator.generate_topics_fallback("方向", "分区", [], 5)) == []


# --------------------------------------------------------------------- 统一入口

def test_generate_topics_short_circuits_without_hot_tags(db_env) -> None:
    """拿不到热词时应返回成功标记为 False 的稳定结构。"""
    import asyncio

    generator = _build(generator_tags={})

    result = asyncio.run(generator.generate_topics("方向", "游戏", count=3))

    assert result == {
        "success": False,
        "error": "未获取到热门tag数据",
        "hot_tags": [],
        "topics": [],
    }


def test_generate_topics_uses_fallback_when_llm_disabled(db_env) -> None:
    """use_llm=False 时应走降级方案并落库。"""
    import asyncio

    generator = _build(generator_tags={"a": 3, "b": 2})

    result = asyncio.run(generator.generate_topics("方向", "游戏", count=2, use_llm=False))

    assert result["success"] is True
    assert result["used_llm"] is False
    assert result["hot_tags"] == ["a", "b"]
    assert len(result["topics"]) == 2
    assert all(isinstance(item_id, int) for item_id in result["saved_ids"])


def test_generate_topics_uses_llm_when_available(db_env) -> None:
    """LLM 可用且 use_llm=True 时应使用 LLM 结果。"""
    import asyncio

    llm = FakeLLM(response=_llm_response([{"title": "AI选题", "difficulty": "hard"}]))
    generator = _build(generator_tags={"a": 3, "b": 2}, llm=llm)

    result = asyncio.run(generator.generate_topics("方向", "游戏", count=1, use_llm=True))

    assert result["used_llm"] is True
    assert result["topics"][0]["title"] == "AI选题"
    assert len(llm.calls) == 1
    assert llm.calls[0]["temperature"] == 0.8


def test_generate_topics_falls_back_when_llm_unavailable(db_env) -> None:
    """use_llm=True 但 LLM 未配置时应静默降级，used_llm 为 False。"""
    import asyncio

    generator = _build(generator_tags={"a": 3, "b": 2}, llm=FakeLLM(configured=False))

    result = asyncio.run(generator.generate_topics("方向", "游戏", count=2, use_llm=True))

    assert result["success"] is True
    assert result["used_llm"] is False
    assert len(result["topics"]) == 2


def test_generate_topics_recovers_from_llm_not_configured_error(db_env) -> None:
    """LLM 中途抛 LLMNotConfiguredError 时应切降级并返回警示。"""
    import asyncio

    llm = FakeLLM(configured=True, error=LLMNotConfiguredError("boom"))
    generator = _build(generator_tags={"a": 3, "b": 2}, llm=llm)

    result = asyncio.run(generator.generate_topics("方向", "游戏", count=2, use_llm=True))

    assert result["used_llm"] is False
    assert "降级方案" in result["warning"]
    assert len(result["topics"]) == 2


# --------------------------------------------------------------------- 选题库持久化

def test_save_to_topic_library_persists_keywords_and_context(db_env) -> None:
    """保存后 keywords 应同时进 tags 与 ai_suggestions。"""
    import asyncio

    generator = _build()
    topics = [{
        "title": "标题",
        "description": "描述",
        "keywords": ["k1", "k2"],
        "reason": "理由",
        "difficulty": "hard",
        "zone_name": "游戏",
        "direction": "解说",
        "related_tags": ["tag"],
        "generated_at": datetime(2026, 1, 1),
        "status": "pending",
    }]

    saved_ids = asyncio.run(generator._save_to_topic_library(topics))

    assert len(saved_ids) == 1
    session = db_env.get_session()
    try:
        row = session.query(Topic).filter_by(id=saved_ids[0]).one()
        assert row.title == "标题"
        assert row.tags == ["k1", "k2"]
        assert row.source == "llm_generated"
        assert row.ai_suggestions["keywords"] == ["k1", "k2"]
        assert row.ai_suggestions["generated_at"] == "2026-01-01T00:00:00"
    finally:
        session.close()


def test_save_to_topic_library_defaults_generated_at_when_missing(db_env) -> None:
    """缺少 generated_at 时用当前时间补全可序列化字符串。"""
    import asyncio

    generator = _build()

    saved_ids = asyncio.run(generator._save_to_topic_library([{"title": "T", "keywords": []}]))

    session = db_env.get_session()
    try:
        row = session.query(Topic).filter_by(id=saved_ids[0]).one()
        assert isinstance(row.ai_suggestions["generated_at"], str)
    finally:
        session.close()


def test_save_to_topic_library_returns_empty_on_error(db_env) -> None:
    """缺少必填 title 时保存失败应回滚并返回空列表。"""
    import asyncio

    generator = _build()

    assert asyncio.run(generator._save_to_topic_library([{"keywords": ["k"]}])) == []
    # 失败不应留下任何行
    session = db_env.get_session()
    try:
        assert session.query(Topic).count() == 0
    finally:
        session.close()


def test_get_topic_library_serializes_context(db_env) -> None:
    """查询结果应回填 ai_suggestions 中的方向/难度/关键词。"""
    import asyncio

    generator = _build()
    asyncio.run(generator._save_to_topic_library([{
        "title": "标题",
        "description": "描述",
        "keywords": ["k"],
        "reason": "理由",
        "difficulty": "easy",
        "zone_name": "游戏",
        "direction": "解说",
        "related_tags": ["tag"],
        "generated_at": datetime(2026, 1, 1),
    }]))

    rows = asyncio.run(generator.get_topic_library())

    assert len(rows) == 1
    assert rows[0]["title"] == "标题"
    assert rows[0]["category"] == "游戏"
    assert rows[0]["direction"] == "解说"
    assert rows[0]["difficulty"] == "easy"
    assert rows[0]["keywords"] == ["k"]
    assert rows[0]["status"] == "pending"
    assert rows[0]["created_at"]


def test_get_topic_library_filters_and_limits(db_env) -> None:
    """分区与状态过滤应生效，limit 应限制条数。"""
    import asyncio

    generator = _build()
    asyncio.run(generator._save_to_topic_library([
        {"title": "A", "keywords": [], "zone_name": "游戏", "generated_at": datetime(2026, 1, 1)},
        {"title": "B", "keywords": [], "zone_name": "动画", "generated_at": datetime(2026, 1, 1)},
        {"title": "C", "keywords": [], "zone_name": "游戏", "generated_at": datetime(2026, 1, 1)},
    ]))

    game_rows = asyncio.run(generator.get_topic_library(zone_name="游戏"))
    limited = asyncio.run(generator.get_topic_library(limit=1))
    published = asyncio.run(generator.get_topic_library(status="published"))

    assert {row["title"] for row in game_rows} == {"A", "C"}
    assert len(limited) == 1
    assert published == []


def test_update_topic_status_success_and_missing(db_env) -> None:
    """存在的选题改状态返回 True，不存在返回 False。"""
    import asyncio

    generator = _build()
    saved_ids = asyncio.run(generator._save_to_topic_library([{"title": "T", "keywords": []}]))

    assert asyncio.run(generator.update_topic_status(saved_ids[0], "adopted")) is True
    assert asyncio.run(generator.update_topic_status(99999, "adopted")) is False

    rows = asyncio.run(generator.get_topic_library())
    assert rows[0]["status"] == "adopted"
