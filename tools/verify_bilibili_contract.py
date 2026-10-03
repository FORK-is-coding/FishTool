"""FishTool 04 · 第三批 i：真·B 站低限额契约验证（手动跑，默认不联网）。

合规声明
--------
本脚本用于**接口契约验证**：字段形状、状态码、限流 / 风控表现、时间字段与分页语义。

硬约束（已获明确授权，必须低限额）
---------------------------------
- 总请求 **<= 12 次**，同一端点 **<= 3 次**（见 :data:`MAX_TOTAL_REQUESTS` /
  :data:`MAX_REQUESTS_PER_ENDPOINT`，运行时不允许放大）；
- **不做**批量抓取、不做遍历、不做压力测试，不写任何刷量用途代码；
- 出现风控（``-352`` / HTTP ``412`` / ``403`` / ``429``）或需要 Cookie / IP 被限 ->
  **立即停**并标未验证，**不绕**；
- 输出**只保留摘要**（请求数 / 端点 / 状态码 / 字段清单 / 观测时间），
  **不落 Cookie、不落完整响应、不落敏感请求头**。

用法
----
- 只打印计划、不发任何请求（安全，可自检）::

      python tools/verify_bilibili_contract.py --dry-run

- 显式确认后执行低限额真请求（须同时给 ``--live`` 与环境变量）::

      BILIBILI_LIVE_CONTRACT=1 python tools/verify_bilibili_contract.py --live --json

"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ===========================================================================
# 硬上限（钉死；任何路径都不得突破）
# ===========================================================================

#: 本次验证允许的总请求数上限。
MAX_TOTAL_REQUESTS: int = 12
#: 同一端点允许的请求数上限。
MAX_REQUESTS_PER_ENDPOINT: int = 3

#: 风控 / 限流信号（HTTP 状态码）。
RISK_HTTP_CODES = (403, 412, 429)
#: 风控 / 限流信号（B 站业务码）。
RISK_BIZ_CODES = (-352, -412, -403, -429, -509)

#: ``ranking/v2`` 需覆写 Referer 才不被判 -352（平台可观测行为，属易变项）。
RANKING_REFERER = "https://www.bilibili.com/v/popular/rank/all"

#: 出站请求头：只放浏览器 UA 与站点 Referer，**不含 Cookie / 不落敏感头**。
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}

#: 低限额契约验证计划（静态声明）。同一端点出现多步时，合计仍 <= 单端点上限。
PLAN: List[Dict[str, Any]] = [
    {"name": "nav", "endpoint": "/x/web-interface/nav", "params": {}},
    {"name": "popular_page1", "endpoint": "/x/web-interface/popular", "params": {"ps": 20, "pn": 1}},
    {"name": "popular_page2", "endpoint": "/x/web-interface/popular", "params": {"ps": 20, "pn": 2}},
    {
        "name": "ranking_all",
        "endpoint": "/x/web-interface/ranking/v2",
        "params": {"rid": 0, "type": "all"},
        "referer": RANKING_REFERER,
    },
    {"name": "search_square", "endpoint": "/x/web-interface/search/square", "params": {"limit": 10}},
    {"name": "view_detail", "endpoint": "/x/web-interface/view", "params": {}, "needs_bvid": True},
]


# ===========================================================================
# 计划（纯函数，不联网、无副作用）
# ===========================================================================

def build_plan() -> List[Dict[str, Any]]:
    """返回验证计划的深拷贝。

    Returns:
        list[dict]: 每个步骤含 ``name`` / ``endpoint`` / ``params`` 等字段。
    """
    return json.loads(json.dumps(PLAN))


def summarize_plan() -> Dict[str, Any]:
    """汇总计划规模（供自检 / 测试断言低限额；不联网）。

    Returns:
        dict: 步骤数、总请求数、每端点请求数、上限。
    """
    plan = build_plan()
    per_endpoint: Dict[str, int] = {}
    for step in plan:
        per_endpoint[step["endpoint"]] = per_endpoint.get(step["endpoint"], 0) + 1
    return {
        "steps": len(plan),
        "total_requests": len(plan),
        "per_endpoint": per_endpoint,
        "max_per_endpoint": max(per_endpoint.values()) if per_endpoint else 0,
        "max_total": MAX_TOTAL_REQUESTS,
        "max_per_endpoint_allowed": MAX_REQUESTS_PER_ENDPOINT,
    }


def live_enabled(environ: Optional[Dict[str, str]] = None) -> bool:
    """判断是否已显式开启真请求授权（默认关闭）。

    Args:
        environ: 环境变量映射；缺省读 ``os.environ``。

    Returns:
        bool: 仅当 ``BILIBILI_LIVE_CONTRACT=1`` 时为 True。
    """
    env = os.environ if environ is None else environ
    return str(env.get("BILIBILI_LIVE_CONTRACT", "")) == "1"


# ===========================================================================
# 摘要提取（只取字段名与状态码，绝不落值 / 完整响应）
# ===========================================================================

def shape_of(value: Any) -> List[str]:
    """只取字段名（不取任何值），用于契约字段形状摘要。"""
    if isinstance(value, dict):
        return sorted(str(key) for key in value.keys())
    return []


def _summarize_response(
    name: str, endpoint: str, http_status: int, text: str, observed_s: int
) -> Dict[str, Any]:
    """把一次响应压成契约摘要（字段形状 / 状态码 / 观测时间）。"""
    entry: Dict[str, Any] = {
        "name": name,
        "endpoint": endpoint,
        "http_status": int(http_status),
        "status": "unverified",
        "observed_s": int(observed_s),
    }
    try:
        payload = json.loads(text)
    except Exception:  # noqa: BLE001 - 非 JSON 体不算通过
        entry["reason_code"] = "non_json_body"
        return entry
    if not isinstance(payload, dict):
        entry["reason_code"] = "unexpected_body_shape"
        entry["body_keys"] = shape_of(payload)
        return entry

    code = payload.get("code")
    entry["business_code"] = code
    entry["envelope_keys"] = shape_of(payload)
    data = payload.get("data")
    entry["data_keys"] = shape_of(data)
    if isinstance(data, dict):
        items = data.get("list")
        if isinstance(items, list):
            entry["list_len"] = len(items)
            if items and isinstance(items[0], dict):
                entry["item_keys"] = shape_of(items[0])
                bvid = items[0].get("bvid")
                if bvid:
                    # 仅内部用于下一步 view 探测；带下划线，最终摘要会过滤掉。
                    entry["_first_bvid"] = str(bvid)

    if http_status == 200 and code == 0:
        entry["status"] = "verified"
    elif int(http_status) in RISK_HTTP_CODES or code in RISK_BIZ_CODES:
        entry["reason_code"] = f"risk_control:{http_status}:{code}"
    else:
        entry["reason_code"] = f"business_code:{code}"
    return entry


def _public_view(entry: Dict[str, Any]) -> Dict[str, Any]:
    """过滤掉内部字段（下划线前缀），确保不外泄非摘要数据。"""
    return {key: value for key, value in entry.items() if not str(key).startswith("_")}


# ===========================================================================
# 真请求（低限额；默认不执行）
# ===========================================================================

async def _fetch(session: Any, url: str, params: Dict[str, Any], headers: Dict[str, str], timeout: int):
    """发一次 GET（单次尝试，**不重试**，避免风控下被动放大请求数）。"""
    async with session.get(url, params=params, headers=headers, timeout=timeout) as resp:
        status = resp.status
        text = await resp.text()
    return status, text


async def run_live(*, timeout: int = 10) -> Dict[str, Any]:
    """执行低限额真请求契约验证（须由调用方先行确认授权）。

    Args:
        timeout: 单次请求超时（秒）。

    Returns:
        dict: 只含摘要（请求数 / 端点结果 / 停止原因 / 观测时间）。
    """
    import aiohttp

    plan = build_plan()
    results: List[Dict[str, Any]] = []
    request_count = 0
    stopped_reason: Optional[str] = None
    popular_bvid: Optional[str] = None

    async with aiohttp.ClientSession() as session:
        for step in plan:
            name = str(step["name"])
            endpoint = str(step["endpoint"])
            observed_s = int(time.time())

            if step.get("needs_bvid") and not popular_bvid:
                results.append(
                    {"name": name, "endpoint": endpoint, "status": "unverified",
                     "reason_code": "no_bvid_source", "observed_s": observed_s}
                )
                continue

            if request_count >= MAX_TOTAL_REQUESTS:
                results.append(
                    {"name": name, "endpoint": endpoint, "status": "unverified",
                     "reason_code": "max_total_requests_reached", "observed_s": observed_s}
                )
                continue

            params = {"bvid": popular_bvid} if step.get("needs_bvid") else dict(step.get("params") or {})
            headers = dict(DEFAULT_HEADERS)
            if step.get("referer"):
                headers["Referer"] = str(step["referer"])
            url = "https://api.bilibili.com" + endpoint

            request_count += 1
            try:
                status, text = await _fetch(session, url, params, headers, timeout)
            except Exception as exc:  # noqa: BLE001 - 网络异常如实标未验证后停止
                results.append(
                    {"name": name, "endpoint": endpoint, "status": "unverified",
                     "reason_code": "network_error", "error_type": type(exc).__name__,
                     "observed_s": observed_s}
                )
                stopped_reason = "network_error"
                break

            entry = _summarize_response(name, endpoint, status, text, observed_s)
            if entry.get("_first_bvid"):
                popular_bvid = entry["_first_bvid"]
            results.append(_public_view(entry))

            if int(status) in RISK_HTTP_CODES or entry.get("business_code") in RISK_BIZ_CODES:
                stopped_reason = f"risk_control:{status}:{entry.get('business_code')}"
                break

    return {
        "mode": "live",
        "request_count": request_count,
        "max_total": MAX_TOTAL_REQUESTS,
        "max_per_endpoint_allowed": MAX_REQUESTS_PER_ENDPOINT,
        "stopped_reason": stopped_reason,
        "observed_s": int(time.time()),
        "results": results,
    }


# ===========================================================================
# CLI
# ===========================================================================

def _format_plan(summary: Dict[str, Any]) -> str:
    """人类可读的计划摘要。"""
    lines = [
        "[契约验证计划]（未联网）",
        f"步骤数 / 总请求数：{summary['steps']} / {summary['total_requests']}",
        f"总请求上限：{summary['max_total']}；单端点上限：{summary['max_per_endpoint_allowed']}",
        "每端点请求数：",
    ]
    for endpoint, count in summary["per_endpoint"].items():
        lines.append(f"  - {endpoint}: {count}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数（缺省取 ``sys.argv``）。

    Returns:
        int: 0 成功；2 未授权执行真请求。
    """
    parser = argparse.ArgumentParser(description="B 站低限额契约验证（默认不联网）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不发任何请求")
    parser.add_argument("--live", action="store_true", help="显式执行低限额真请求（需授权）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出摘要")
    args = parser.parse_args(argv)

    if not args.live:
        summary = {"mode": "dry_run", **summarize_plan()}
        print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else _format_plan(summary))
        print("\n[提示] 未加 --live：仅打印计划，未发任何真实请求。")
        return 0

    if not live_enabled():
        print("[拒绝] 需显式设置 BILIBILI_LIVE_CONTRACT=1 才执行真请求（低限额授权）。")
        return 2

    summary = asyncio.run(run_live())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
