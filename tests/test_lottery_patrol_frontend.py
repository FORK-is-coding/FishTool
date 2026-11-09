"""AI 伪人巡检前端契约：DOM、加载顺序、前后端路径一致、风格与兼容约束。"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "web" / "frontend"
HTML = (FRONTEND / "templates" / "index.html").read_text(encoding="utf-8")
JS = (FRONTEND / "static" / "js" / "app.lottery.patrol.js").read_text(encoding="utf-8")
CSS = (FRONTEND / "static" / "css" / "style.lottery.patrol.css").read_text(encoding="utf-8")
BASE_CSS = (FRONTEND / "static" / "css" / "style.base.css").read_text(encoding="utf-8")


def test_required_dom_elements_exist():
    for element_id in (
        "lottery-view-draw", "lottery-view-patrol", "lottery-draw-view", "lottery-patrol-panel",
        "patrol-target", "patrol-scan-button", "patrol-judge-button", "patrol-report-button",
        "patrol-include-replies", "patrol-summary", "patrol-table-host", "patrol-ledger",
        "patrol-settings-button", "patrol-export-button", "patrol-clear-button",
    ):
        assert f'id="{element_id}"' in HTML, element_id


def test_three_main_buttons_have_expected_labels():
    for label in ("开始扫描", "AI 甄别", "一键举报"):
        assert label in HTML, label


def test_patrol_panel_hidden_by_default():
    assert re.search(r'<div id="lottery-patrol-panel"[^>]*\shidden', HTML)


def test_patrol_js_loaded_after_ui():
    assert HTML.index("app.lottery.patrol.js") > HTML.index("app.ui.js")


def test_patrol_css_loaded_before_flexgap():
    assert HTML.index("style.lottery.patrol.css") < HTML.index("style.flexgap.css")


def test_api_paths_exist_in_router():
    from web.routers import lottery_patrol

    # route.path 本身已包含 router.prefix，直接取完整路径，不要再拼接前缀。
    routes = {route.path for route in lottery_patrol.router.routes}

    def known(path):
        if not path.startswith("/"):
            return True
        # 前端拼的是 API_PREFIX + path，与 route.path 同样是完整路径。
        normalized = "/lottery/patrol" + re.sub(r"/\$\{[^}]+\}", "/{x}", path)
        if normalized.endswith("/"):
            # 形如 `/tasks/` + `${id}` + `/cancel` 的前缀拼接，统一成带参数的路径再比对。
            normalized = normalized.rstrip("/") + "/{x}"
        return normalized in routes or any(
            re.fullmatch(re.sub(r"\{[^}]+\}", r"[^/]+", route_path), normalized)
            or re.fullmatch(re.sub(r"\{[^}]+\}", r"[^/]+", route_path) + r"(?:/.*)?", normalized)
            for route_path in routes)

    paths = set(re.findall(r"""api(?:Read|Write)\(\s*['"`]([^'"`?]+)""", JS))
    paths |= set(re.findall(r"""(?:path|url)\s*[:=]\s*['"`]([^'"`?]+)""", JS))
    missing = {p for p in paths if not known(p)}
    assert not missing, f"前端调用了后端不存在的路径: {missing}"
    assert "/lottery/patrol/scans" in routes and "/lottery/patrol/tasks/{task_id}" in routes


def test_no_polltask_or_replaceall():
    assert "pollTask(" not in JS
    assert ".replaceAll(" not in JS
    # inset 简写不允许，但 box-shadow 的 inset 关键字合法（与现有兼容测试同口径）。
    assert not re.search(r"(?<![-\w])inset\s*:", CSS)
    # :is() / :where() 只检查选择器位置，先去掉注释，@media 查询里的 ": is" 不算。
    css_no_comments = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert not re.search(r"^\s*[^{}@]*:(?:is|where)\s*\(", css_no_comments, re.M)


def test_write_requests_carry_token_and_content_type():
    assert "/comment/semantic/local-token" in JS
    assert "'Content-Type': 'application/json'" in JS
    assert "X-Local-Token" in JS


def test_random_uuid_has_fallback():
    for line in JS.splitlines():
        if "crypto.randomUUID" in line:
            assert "window.crypto" in line or "?" in line, line.strip()


def test_css_only_uses_known_variables():
    defined = set(re.findall(r"^\s*(--[\w-]+):", BASE_CSS, re.M))
    used = set(re.findall(r"var\((--[\w-]+)", CSS))
    unknown = used - defined
    assert not unknown, f"使用了主样式表未定义的变量: {unknown}"


def test_css_no_new_hex_colors():
    matches = re.findall(r"#[0-9a-fA-F]{3,8}\b", CSS)
    # 白色背景与现有 .ranking-table 一致，其余一律走 :root 变量。
    assert set(matches) <= {"#fff"}, f"新增 CSS 引入了新的颜色值: {set(matches)}"


def test_flex_containers_do_not_use_gap():
    for block in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS):
        selector, body = block
        if re.search(r"display\s*:\s*(?:inline-)?flex", body):
            assert not re.search(r"(?:row-gap|column-gap|gap)\s*:", body), selector


def test_new_resources_versioned():
    assert re.search(r"app\.lottery\.patrol\.js\?v=patrol", HTML)
    assert re.search(r"style\.lottery\.patrol\.css\?v=patrol", HTML)
