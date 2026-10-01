"""06 采集广度 · 聚合入口低频联网冒烟（规格 §9.2）。

合规声明
--------
- 仅调用 B 站**公开聚合入口**（search/square、popular、ranking/v2），**不发送 Cookie、
  不绕过登录或风控、不做高频采集**；
- 三入口各请求 **1 次**，请求之间 `time.sleep` 间隔，属于一次性低频验证，非轮询采集；
- 一律遵守 `config/budget.yaml` 的 discovery / ranking 配额与共享退避（本脚本把配额
  库指向临时文件，不污染真实 `data/`）。

用法::

    python tools/discovery_smoke.py

退出码 0 表示三入口 code==0、条数 > 0、必填字段非空；否则为 1。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# 允许脚本直接以 `python tools/discovery_smoke.py` 运行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bilibili.api import BilibiliAPI  # noqa: E402
from core.database import init_database  # noqa: E402
from modules.hotspot.discovery import sources  # noqa: E402

#: 入口之间的礼貌间隔（秒）。
_SLEEP_BETWEEN_S = 2.0


def _keyword_report(envelope: dict, captured: int) -> dict:
    """解析并汇总 search/square 结果。"""
    outcome = sources.parse_hot_keywords(envelope, captured_epoch_s=captured)
    return {
        "state": outcome.state,
        "code": outcome.error_code,
        "error_message": envelope.get("message"),
        "returned_count": outcome.returned_count,
        "item_count": len(outcome.items),
        "all_heat_positive_int": all(
            isinstance(item.heat_score, int) and item.heat_score > 0 for item in outcome.items
        ),
        "sample_keyword": outcome.items[0].keyword if outcome.items else None,
        "sample_heat": outcome.items[0].heat_score if outcome.items else None,
    }


def _video_report(envelope: dict, captured: int, source: str) -> dict:
    """解析并汇总 popular / ranking 结果。"""
    outcome = sources.parse_popular(envelope, captured_epoch_s=captured) if source == "popular" \
        else sources.parse_ranking(envelope, captured_epoch_s=captured)
    sample = outcome.items[0] if outcome.items else None
    return {
        "state": outcome.state,
        "code": outcome.error_code,
        "error_message": envelope.get("message"),
        "returned_count": outcome.returned_count,
        "item_count": len(outcome.items),
        "others_count": outcome.others_count,
        "sample_bvid": getattr(sample, "bvid", None),
        "sample_has_bvid": bool(sample and sample.bvid),
        "sample_owner_present": bool(sample and sample.owner_status == "ok"),
        "sample_view_present": bool(sample and sample.view_status == "ok"),
    }


async def _run() -> dict:
    """执行三入口一次性冒烟。"""
    captured = int(time.time())
    report: dict = {"captured_epoch_s": captured, "sources": {}}
    async with BilibiliAPI(cookie="") as api:
        report["sources"]["search_square"] = _keyword_report(
            await sources.fetch_hot_keywords(api), captured
        )
        await asyncio.sleep(_SLEEP_BETWEEN_S)

        report["sources"]["popular"] = _video_report(
            await sources.fetch_popular_page(api, page=1), captured, "popular"
        )
        await asyncio.sleep(_SLEEP_BETWEEN_S)

        report["sources"]["ranking_all"] = _video_report(
            await sources.fetch_ranking(api, rid=0), captured, "ranking_all"
        )
    return report


def main() -> int:
    """入口：跑冒烟、打印 JSON、返回退出码。"""
    # 配额库指向临时文件，避免污染真实 data/。
    init_database(str(Path(tempfile.mkdtemp(prefix="discovery_smoke_")) / "smoke.db"))
    report = asyncio.run(_run())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 便于测试/运维留档：默认写到 data/discovery_smoke_report.json。
    try:
        report_path = Path(os.environ.get("FISHTOOL_SMOKE_REPORT", "data/discovery_smoke_report.json"))
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    ok = True
    for name, item in report["sources"].items():
        if item["state"] != "ok" or item["returned_count"] <= 0:
            ok = False
        if name in ("popular", "ranking_all") and not (
            item["sample_has_bvid"] and item["sample_owner_present"] and item["sample_view_present"]
        ):
            ok = False
    if report["sources"]["search_square"].get("all_heat_positive_int") is not True:
        ok = False
    print(f"\n[smoke] 结论: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
