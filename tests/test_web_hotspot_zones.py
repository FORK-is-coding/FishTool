"""web.routers.hotspot.routes_zones 分区列表接口测试（第4批 · web 段）。

覆盖对象：
- GET /zones -> get_supported_zones

验证维度：
返回结构 zones/zone_options/count / 绘画二级分区追加契约 / 一级分区列表不受污染。

测试策略：
- 用 fastapi.testclient.TestClient 挂载真实 router，不启动真实服务。
- 通过 monkeypatch 固定 TagCloudGenerator 的分区选项，避免依赖真实分区表变动。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.hotspot.tag_cloud import TagCloudGenerator


@pytest.fixture()
def client():
    """挂载 hotspot router 的测试客户端。"""
    from web.routers import hotspot

    app = FastAPI()
    app.include_router(hotspot.router, prefix="/api/hotspot")
    return TestClient(app)


@pytest.fixture()
def fixed_zones(monkeypatch):
    """固定两个一级分区 + 绘画 TID。"""
    monkeypatch.setattr(
        TagCloudGenerator,
        "get_zone_options",
        staticmethod(lambda: [{"name": "游戏", "tid": 1008}, {"name": "科技", "tid": 1012}]),
    )
    monkeypatch.setattr(TagCloudGenerator, "PAINT_TID", 27)
    return {"游戏": 1008, "科技": 1012}


def test_zones_appends_paint_to_display_list(client, fixed_zones):
    """展示列表应包含一级分区并追加绘画。"""
    response = client.get("/api/hotspot/zones")
    assert response.status_code == 200
    body = response.json()
    assert body["zones"] == ["游戏", "科技", "绘画"]


def test_zones_count_covers_paint(client, fixed_zones):
    """count 应等于一级分区数 + 1（含绘画）。"""
    body = client.get("/api/hotspot/zones").json()
    assert body["count"] == 3


def test_zone_options_paint_entry_uses_scheme_c(client, fixed_zones):
    """zone_options 中绘画项应带 paint_c 方案与 PAINT_TID，且不污染一级分区契约。"""
    body = client.get("/api/hotspot/zones").json()
    options = body["zone_options"]
    names = [item["name"] for item in options]
    assert names == ["游戏", "科技", "绘画"]

    paint = options[-1]
    assert paint == {"name": "绘画", "tid": 27, "scheme": "paint_c"}

    # 一级分区项不应被附加 scheme 字段
    assert all("scheme" not in item for item in options[:-1])


def test_zones_does_not_mutate_zone_map(client, fixed_zones):
    """接口不应把绘画写回 ZONE_MAP，避免污染真实分区表。"""
    client.get("/api/hotspot/zones")
    assert "绘画" not in TagCloudGenerator.ZONE_MAP
