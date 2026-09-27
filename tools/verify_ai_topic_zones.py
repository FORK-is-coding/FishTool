"""合规声明：仅低频访问 B 站公开分区接口，用于功能验收，不绕过登录或风控。"""
import asyncio
import json
from typing import Any, Dict, List

from bilibili.api import BilibiliAPI
from modules.hotspot.tag_cloud import TagCloudGenerator


class VerificationLimiter:
    """在线验收用固定延时限频器。"""

    async def acquire(self, endpoint: str = "unknown") -> None:
        """每次请求前等待，避免高频访问公开接口。"""
        await asyncio.sleep(0.8)


async def verify_zone(
    generator: TagCloudGenerator,
    zone_name: str,
    zone_id: int,
) -> Dict[str, Any]:
    """验证单个分区能否匿名获取视频。

    Args:
        generator: 分区采集器。
        zone_name: 中文分区名。
        zone_id: B站分区 ID。

    Returns:
        结构化验收结果。
    """
    try:
        videos = await generator.get_zone_ranking(zone_id, limit=3)
        return {
            "zone": zone_name,
            "rid": zone_id,
            "ok": bool(videos),
            "video_count": len(videos),
        }
    except Exception as exc:
        return {
            "zone": zone_name,
            "rid": zone_id,
            "ok": False,
            "video_count": 0,
            "error": str(exc),
        }


async def main() -> None:
    """串行验证全部支持分区并输出 JSON 摘要。"""
    results: List[Dict[str, Any]] = []
    async with BilibiliAPI() as api:
        generator = TagCloudGenerator(api, VerificationLimiter())
        for zone_name, zone_id in TagCloudGenerator.ZONE_MAP.items():
            results.append(await verify_zone(generator, zone_name, zone_id))

    summary = {
        "total": len(results),
        "passed": sum(1 for item in results if item["ok"]),
        "failed": sum(1 for item in results if not item["ok"]),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
