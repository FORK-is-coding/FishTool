"""web.routers.hotspot.schemas 请求模型测试（第4批 · web 段）。

覆盖对象：
- TagCloudRequest
- ActivityRequest
- TopicGenerateRequest
- TopicUpdateRequest

验证维度：
必填字段校验 / 默认值 / 类型强制转换 / model_dump 输出契约。

测试策略：
- 直接实例化 Pydantic 模型，不使用 TestClient。
- 校验失败分支从外部以 ``pytest.raises(ValidationError)`` 断言。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from web.routers.hotspot.schemas import (
    ActivityRequest,
    TagCloudRequest,
    TopicGenerateRequest,
    TopicUpdateRequest,
)


# ---------------------------------------------------------------------------
# TagCloudRequest
# ---------------------------------------------------------------------------


def test_tag_cloud_request_defaults():
    """limit 与 top_n 应取默认 100 / 50，zone_name 必填。"""
    request = TagCloudRequest(zone_name="游戏")
    assert request.zone_name == "游戏"
    assert request.limit == 100
    assert request.top_n == 50


def test_tag_cloud_request_requires_zone_name():
    """缺少 zone_name 应触发校验错误。"""
    with pytest.raises(ValidationError):
        TagCloudRequest()


def test_tag_cloud_request_coerces_numeric_strings():
    """字符串数字应被强制转换为 int。"""
    request = TagCloudRequest(zone_name="科技", limit="200", top_n="30")
    assert request.limit == 200
    assert request.top_n == 30


# ---------------------------------------------------------------------------
# ActivityRequest
# ---------------------------------------------------------------------------


def test_activity_request_defaults():
    """include_ugc 默认 True，zone 默认 all。"""
    request = ActivityRequest()
    assert request.include_ugc is True
    assert request.zone == "all"


def test_activity_request_accepts_overrides():
    """显式传参应覆盖默认值。"""
    request = ActivityRequest(include_ugc=False, zone="game")
    assert request.include_ugc is False
    assert request.zone == "game"


# ---------------------------------------------------------------------------
# TopicGenerateRequest
# ---------------------------------------------------------------------------


def test_topic_generate_request_defaults():
    """count 默认 10，use_llm 默认 True，direction/zone_name 必填。"""
    request = TopicGenerateRequest(direction="游戏攻略", zone_name="游戏")
    assert request.count == 10
    assert request.use_llm is True


def test_topic_generate_request_requires_direction_and_zone():
    """缺少必填字段应触发校验错误。"""
    with pytest.raises(ValidationError):
        TopicGenerateRequest(direction="只有方向")
    with pytest.raises(ValidationError):
        TopicGenerateRequest(zone_name="只有分区")


def test_topic_generate_request_model_dump_contract():
    """model_dump 输出字段集合应与接口契约一致。"""
    request = TopicGenerateRequest(direction="d", zone_name="z", count=5, use_llm=False)
    assert request.model_dump() == {
        "direction": "d",
        "zone_name": "z",
        "count": 5,
        "use_llm": False,
    }


# ---------------------------------------------------------------------------
# TopicUpdateRequest
# ---------------------------------------------------------------------------


def test_topic_update_request_accepts_status():
    """status 为唯一必填字段。"""
    request = TopicUpdateRequest(status="adopted")
    assert request.status == "adopted"


def test_topic_update_request_requires_status():
    """缺少 status 应触发校验错误。"""
    with pytest.raises(ValidationError):
        TopicUpdateRequest()
