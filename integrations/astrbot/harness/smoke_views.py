"""Playwright 冒烟：在 AstrBot 面板里打开 FishTool 视图，逐页切换，检查无 JS 报错、主要请求成功。

前置：AstrBot 已运行且插件已安装（可先用 astrbot_harness.py --keep-running）。

用法::

    python integrations/astrbot/harness/smoke_views.py --base http://127.0.0.1:6195 \
        --password <面板密码> [--shots <截图目录>]

结果打印为 JSON；失败返回非零。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import httpx

logger = logging.getLogger("fishtool.smoke")
PLUGIN = "astrbot_plugin_fishtool"
PAGES = ["welcome", "hotspot", "comment", "up", "diagnosis", "lottery", "config", "logs", "usage"]
#: 已知无害的控制台输出（FishTool 自身或 AstrBot 面板，与适配无关）。
IGNORED_CONSOLE = ("favicon", "Download the Vue Devtools", "[HMR]")


def login(base: str, username: str, password: str) -> str:
    resp = httpx.post(f"{base}/api/v1/auth/login", json={"username": username, "password": password}, trust_env=False, timeout=30)
    token = (resp.json().get("data") or {}).get("token")
    if not token:
        raise RuntimeError(f"login failed: {resp.text[:300]}")
    return token


def discover_pages(frame) -> list[str]:
    return frame.evaluate("() => Array.from(document.querySelectorAll('.page[id$=\"-page\"]')).map(e => e.id.slice(0, -5))")


def run(base: str, username: str, password: str, shots: Path | None) -> dict:
    from playwright.sync_api import sync_playwright

    token = login(base, username, password)
    report: dict = {"pages": {}, "console_errors": [], "page_errors": [], "bridge_failures": []}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        # init script 也会在沙箱 iframe 里执行，那里访问 localStorage 会抛错，只在顶层窗口写 token。
        ctx.add_init_script(
            "if (window === window.top) { try { "
            f"localStorage.setItem('token', {json.dumps(token)}); localStorage.setItem('user', {json.dumps(username)});"
            " } catch (e) {} }"
        )
        page = ctx.new_page()

        def on_console(msg):
            if msg.type == "error" and not any(s in msg.text for s in IGNORED_CONSOLE):
                report["console_errors"].append(msg.text[:500])

        page.on("console", on_console)
        page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)[:500]))

        def on_response(resp):
            url = resp.url
            if f"/plugins/extensions/{PLUGIN}/" not in url:
                return
            try:
                body = resp.json()
            except Exception as exc:  # noqa: BLE001 - SSE / 下载不是 JSON
                logger.debug("non-json response %s: %r", url[:120], exc)
                return
            data = body.get("data") if isinstance(body, dict) else None
            status = data.get("http_status") if isinstance(data, dict) else None
            if resp.status != 200 or body.get("status") == "error" or (isinstance(status, int) and status >= 500):
                report["bridge_failures"].append({"url": url.split(PLUGIN, 1)[1][:200], "http": resp.status, "inner": status})

        page.on("response", on_response)
        page.goto(f"{base}/#/plugin-view/{PLUGIN}/console", wait_until="domcontentloaded")
        frame_el = page.wait_for_selector("iframe", timeout=60000)
        frame = frame_el.content_frame()
        frame.wait_for_function("() => window.__FISHTOOL_ASTRBOT__ === true && typeof navigateTo === 'function'", timeout=60000)
        time.sleep(3)
        pages = discover_pages(frame) or PAGES
        report["discovered_pages"] = pages
        for name in pages:
            before = len(report["page_errors"]) + len(report["console_errors"])
            frame.evaluate("(n) => navigateTo(n)", name)
            time.sleep(2.5)
            active = frame.evaluate("(n) => !!document.querySelector('#' + n + '-page.active')", name)
            errors = len(report["page_errors"]) + len(report["console_errors"]) - before
            report["pages"][name] = {"active": active, "new_errors": errors}
            if shots:
                shots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shots / f"{name}.png"))
        report["toast_socket"] = frame.evaluate("() => typeof appToastSocket !== 'undefined' && appToastSocket ? appToastSocket.readyState : null")
        report["resident_status"] = frame.evaluate(
            "async () => { try { const r = await apiRequest('/comment/resident/status', {}, false); return {ok: true, keys: Object.keys(r || {})}; } catch (e) { return {ok: false, status: e.status, msg: e.message}; } }"
        )
        report["events_connected"] = httpx.get(
            f"{base}/api/v1/plugins/extensions/{PLUGIN}/_status",
            headers={"Authorization": f"Bearer {token}"}, trust_env=False, timeout=30,
        ).json().get("events_connected")
        # 下面故意请求不存在的接口，apiRequest 会打一条 console.error，不计入报错。
        errors_before_probe = len(report["console_errors"])
        report["not_found"] = frame.evaluate(
            "async () => { try { await apiRequest('/definitely/not/here', {}, false); return {ok: true}; } catch (e) { return {ok: false, status: e.status}; } }"
        )
        time.sleep(0.5)
        del report["console_errors"][errors_before_probe:]
        browser.close()
    report["ok"] = (
        not report["page_errors"]
        and not report["console_errors"]
        and report["events_connected"] is True
        and all(v["active"] for v in report["pages"].values())
        and report["resident_status"].get("ok") is True
        and report["not_found"].get("status") == 404
        and report["toast_socket"] == 1
        and not report["bridge_failures"]
    )
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--username", default="astrbot")
    ap.add_argument("--password", required=True)
    ap.add_argument("--shots", default="")
    args = ap.parse_args(argv)
    report = run(args.base.rstrip("/"), args.username, args.password, Path(args.shots) if args.shots else None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
