"""抽奖工具前端运行态验收脚本。"""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE_URL = "http://127.0.0.1:8000/"
EVIDENCE_DIR = Path("evidence") / "lottery"


def verify_viewport(page, width: int, height: int, name: str) -> dict:
    """验证指定视口的导航、主题、布局与光晕。

    Args:
        page: Playwright 页面对象。
        width: 视口宽度。
        height: 视口高度。
        name: 证据文件名称前缀。

    Returns:
        结构化验证结果。
    """
    page.set_viewport_size({"width": width, "height": height})
    page.goto(BASE_URL, wait_until="networkidle")
    page.locator('[data-page="lottery"]').click()
    page.locator("#lottery-page.active").wait_for()
    card = page.locator("#lottery-page .section").first
    box = card.bounding_box()
    if box:
        page.mouse.move(box["x"] + box["width"] * 0.75, box["y"] + 80)
        page.wait_for_timeout(100)
    result = page.evaluate(
        """() => {
            const root = getComputedStyle(document.documentElement);
            const nav = [...document.querySelectorAll('.nav-item')].map(item => item.dataset.page);
            const card = document.querySelector('#lottery-page .section');
            return {
                navLotteryBeforeLogs: nav.indexOf('lottery') === nav.indexOf('logs') - 1,
                logsLast: nav.at(-1) === 'logs',
                pageVisible: document.querySelector('#lottery-page')?.classList.contains('active'),
                primaryColor: root.getPropertyValue('--primary-color').trim(),
                backgroundColor: root.getPropertyValue('--bg-primary').trim(),
                glowX: card?.style.getPropertyValue('--x') || '',
                horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
                resultContainers: Boolean(document.querySelector('#lottery-filter-result') && document.querySelector('#lottery-draw-result')),
            };
        }"""
    )
    page.screenshot(path=str(EVIDENCE_DIR / f"{name}.png"), full_page=True)
    return result


def main() -> None:
    """执行桌面与移动视口验收并保存 JSON 证据。"""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            desktop = verify_viewport(page, 1440, 1000, "lottery_desktop")
            mobile = verify_viewport(page, 390, 844, "lottery_mobile")
            openapi_response = page.request.get(f"{BASE_URL}openapi.json")
            openapi_paths = set(openapi_response.json().get("paths", {}))
            api_routes_present = {
                "/api/lottery/preview",
                "/api/lottery/quick-filter",
                "/api/lottery/verify-winners",
                "/api/lottery/filter/tasks",
                "/api/lottery/draw/tasks",
                "/api/lottery/tasks/{task_id}",
            }.issubset(openapi_paths)
            runtime_contract = page.evaluate(
                """async () => {
                    const triangleSource = renderDiagnosisCharts.toString();
                    const quickButton = document.querySelector('#lottery-quick-button');
                    const verifyButton = document.querySelector('#lottery-verify-winners-button');
                    await verifyLotteryWinners();
                    const emptyModalText = document.querySelector('.app-modal p')?.textContent || '';
                    document.querySelector('.app-modal-backdrop')?.remove();
                    const host = document.createElement('div');
                    host.innerHTML = renderLotteryMetadata({
                        level: 6,
                        is_vip: true,
                        vip_type: 2,
                        vip_label: '年度大会员',
                        ctime: '2026-08-10T12:00:00',
                    });
                    document.body.appendChild(host);
                    const level = host.querySelector('.level-6');
                    const annual = host.querySelector('.vip.annual');
                    const time = host.querySelector('time.time');
                    const result = {
                        triangleGraphic: triangleSource.includes("type: 'polygon'") && !triangleSource.includes("type: 'radar'"),
                        verifyButtonBesideCollector: Boolean(quickButton && verifyButton) && quickButton.nextElementSibling === verifyButton,
                        emptyWinnerPrompt: emptyModalText === '请先进行抽奖！',
                        level6Badge: Boolean(level) && getComputedStyle(level).backgroundColor === 'rgb(216, 91, 97)',
                        annualVipBadge: Boolean(annual) && annual.textContent.includes('年度大会员') && getComputedStyle(annual).backgroundColor === 'rgb(232, 117, 164)',
                        timestampInBadge: Boolean(time) && time.textContent.includes('2026'),
                    };
                    host.remove();
                    return result;
                }"""
            )
            browser.close()
        checks = {"desktop": desktop, "mobile": mobile, "apiRoutesPresent": api_routes_present, "runtimeContract": runtime_contract}
        required = [
            desktop["navLotteryBeforeLogs"], desktop["logsLast"], desktop["pageVisible"],
            desktop["primaryColor"] == "#d4a373", desktop["backgroundColor"] == "#faf7f2",
            bool(desktop["glowX"]), not desktop["horizontalOverflow"],
            mobile["pageVisible"], not mobile["horizontalOverflow"], mobile["resultContainers"],
            api_routes_present,
            runtime_contract["verifyButtonBesideCollector"], runtime_contract["emptyWinnerPrompt"],
            runtime_contract["level6Badge"],
            runtime_contract["annualVipBadge"], runtime_contract["timestampInBadge"],
        ]
        checks["passed"] = all(required)
        (EVIDENCE_DIR / "verification.json").write_text(
            json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not checks["passed"]:
            raise AssertionError(checks)
        print(json.dumps(checks, ensure_ascii=False))
    except Exception as exc:
        raise RuntimeError(f"抽奖工具前端验收失败: {exc}") from exc


if __name__ == "__main__":
    main()
