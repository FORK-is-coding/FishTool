"""三个用户报告场景的真实接口验收脚本。

仅访问 B 站公开只读接口，串行执行且复用项目限频器；不读取、输出或保存 Cookie。
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # 直接运行 tools 下脚本时显式加入项目根目录，保证业务包可导入。
    sys.path.insert(0, str(PROJECT_ROOT))

from bilibili.api import BilibiliAPI
from bilibili.auth import QRCodeLogin
from bilibili.rate_limiter import RateLimiter
from modules.lottery.service import LotteryService
from web.routers.logs import get_logs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = PROJECT_ROOT / "evidence" / "reported_fixes_verification.json"


async def verify_qrcode() -> dict[str, Any]:
    """调用官方接口生成二维码，返回不含二维码密钥和内容的证据。"""
    qr = QRCodeLogin()
    url, image = await qr.generate_qrcode()
    return {
        "passed": bool(url and qr.qrcode_key and image.startswith(b"\x89PNG")),
        "login_url_present": bool(url),
        "png_bytes": len(image),
        "qrcode_key_present": bool(qr.qrcode_key),
    }


async def verify_lottery() -> dict[str, Any]:
    """使用公开 BV 和 UID 验证目标预览与 UID 分析完整链路。"""
    api = BilibiliAPI(rate_limiter=RateLimiter())
    service = LotteryService(api)
    try:
        preview = await service.preview("BV1xx411c7mD")
        analysis = await service.quick_filter(2, None)
        profile = analysis.get("profile") or {}
        assessment = analysis.get("assessment") or {}
        return {
            "passed": bool(
                preview.get("title")
                and preview.get("author")
                and profile.get("name")
                and assessment.get("classification")
            ),
            "preview": {
                "target_id": preview.get("target_id"),
                "title": preview.get("title"),
                "author": preview.get("author"),
                "oid": preview.get("oid"),
            },
            "uid_analysis": {
                "uid": profile.get("uid"),
                "name": profile.get("name"),
                "level": profile.get("level"),
                "classification": assessment.get("classification"),
                "data_errors": profile.get("data_errors"),
            },
        }
    finally:
        await api.close()


async def verify_logs() -> dict[str, Any]:
    """调用日志查询函数并确认页面数据源返回实际记录。"""
    response = await get_logs(log_type="all", levels="DEBUG,INFO,WARNING,ERROR,CRITICAL", limit=20)
    records = response.get("records") or []
    sample = records[-1] if records else {}
    return {
        "passed": bool(records),
        "returned": response.get("returned"),
        "sample": {
            "timestamp": sample.get("timestamp"),
            "level": sample.get("level"),
            "source": sample.get("source"),
            "message": str(sample.get("message") or "")[:160],
        },
    }


async def main() -> int:
    """串行执行三个验收场景并把清洗后的证据写入 JSON。"""
    result: dict[str, Any] = {"verified_at": datetime.now().isoformat(timespec="seconds")}
    checks = (("qrcode", verify_qrcode), ("lottery", verify_lottery), ("logs", verify_logs))
    for name, check in checks:
        try:
            result[name] = await check()
        except Exception as exc:
            result[name] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
    result["all_passed"] = all(bool(result[name].get("passed")) for name, _ in checks)
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
