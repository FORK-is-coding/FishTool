"""互动效率三角维度图浏览器回归验证。"""
from __future__ import annotations

import json
from typing import Any

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8001"


def build_response() -> dict[str, Any]:
    """构造含超上限比例的自诊接口响应，用于验证前端百分比封顶规则。

    Returns:
        可直接由 Playwright 路由拦截返回的接口响应。
    """
    return {
        "success": True,
        "data": {
            "self_data": {
                "uid": 10001,
                "basic_info": {"name": "互动图验证账号", "level": 6},
                "fan_stats": {"follower": 100, "following": 10},
                "video_stats": {
                    "total_count": 3,
                    "total_play": 1000,
                    "total_comment": 20,
                    "total_favorite": 30,
                    "avg_play": 333,
                    "avg_favorite": 10,
                    "max_play_video": {"play": 500},
                },
                "engagement_metrics": {
                    "play_to_fans_ratio": 200,
                    "comment_to_play_ratio": 500,
                    "favorite_to_play_ratio": 35,
                },
                "post_rhythm": {},
                "tag_cloud": {"word_frequency": {}, "video_count": 0, "tagged_video_count": 0, "tag_count": 0},
                "data_availability": {"engagement_metrics": True},
                "fetched_at": "2026-08-21T00:00:00",
            },
            "benchmark": None,
            "ai_report": {"success": False, "message": "测试环境不请求 AI"},
        },
    }


def verify_chart() -> dict[str, Any]:
    """在真实浏览器中检查雷达图配置、绘制像素与控制台错误。

    Returns:
        含图表数据、绘制结果和错误列表的验证摘要。
    """
    console_errors: list[str] = []
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.route(
            "**/api/analysis/self-diagnosis",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(build_response(), ensure_ascii=False),
            ),
        )
        page.route(
            "**/api/comment/resident/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": {}}),
            ),
        )
        page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        page.click('[data-page="self-diagnosis"]')
        page.fill("#diagnosis-uid", "10001")
        page.click('button:has-text("开始自诊")')
        page.wait_for_selector("#diagnosis-engagement-chart canvas", timeout=30000)
        page.wait_for_timeout(300)
        chart = page.evaluate(
            """() => {
                const element = document.getElementById('diagnosis-engagement-chart');
                const instance = echarts.getInstanceByDom(element);
                const option = instance.getOption();
                const canvas = element.querySelector('canvas');
                const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
                let opaquePixels = 0;
                for (let index = 3; index < pixels.length; index += 64) {
                    if (pixels[index] > 0) opaquePixels += 1;
                }
                return {
                    width: element.clientWidth,
                    height: element.clientHeight,
                    values: option.series[0].data[0].value,
                    radarShape: option.radar[0].shape,
                    hasLine: (option.series[0].lineStyle[0] || option.series[0].lineStyle).width > 0,
                    hasFill: (option.series[0].areaStyle[0] || option.series[0].areaStyle).opacity > 0,
                    hasPoints: (Array.isArray(option.series[0].symbol) ? option.series[0].symbol[0] : option.series[0].symbol) === 'circle',
                    labels: option.radar[0].indicator.map(item => item.name),
                    opaquePixels,
                };
            }"""
        )
        browser.close()

    chart["console_errors"] = console_errors
    chart["page_errors"] = page_errors
    chart["passed"] = (
        chart["width"] > 0
        and chart["height"] >= 320
        and chart["values"] == [100, 100, 35]
        and chart["radarShape"] == "polygon"
        and chart["hasLine"]
        and chart["hasFill"]
        and chart["hasPoints"]
        and chart["opaquePixels"] > 100
        and not console_errors
        and not page_errors
    )
    return chart


def main() -> None:
    """执行验证并在失败时以非零状态退出。"""
    result = verify_chart()
    print(json.dumps(result, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
