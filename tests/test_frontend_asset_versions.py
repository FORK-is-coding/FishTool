"""前端静态资源缓存版本号契约。

修复 PR 按约定不改 ``?v=``，由版本号 PR 在最后统一追加后缀，
确保打包版 / 已打开过页面的浏览器加载新资源。

- ``-fix20261027``：PR-04/07/08/09/13/14/24/25 改动过的脚本；
- ``-fix20261030``：本轮改动过的脚本与样式表；
- ``-ui20261101``：前端视觉改版 PR（C+ 白色档案）改动过的脚本与全部样式表；
- ``-v029ui20261101``：桌面端 UI 对齐 + v0.2.9 PR 改动过的脚本与样式表，必须是最后一段后缀。
"""
from pathlib import Path
import re

INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "frontend" / "templates" / "index.html"
BUMPED_SCRIPTS = ("app.core.js", "app.hotspot.js", "app.events.js", "app.up.js", "app.auth.js", "app.lottery.js")
BUMPED_1030 = {
    "js": ("app.core.js", "app.lottery.js", "app.auth.js", "app.config.js", "app.ui.js", "app.hotspot.js"),
    "css": ("style.hotspot.css", "style.modal.css"),
}
BUMPED_UI_1101 = {
    "js": ("app.core.js", "app.auth.js", "app.hotspot.js", "app.up.js", "app.comment.js"),
    "css": (
        "style.lottery.css", "style.resident.css", "style.base.css", "style.pages.css", "style.log.css",
        "style.config.css", "style.comment.css", "style.up.css", "style.modal.css", "style.responsive.css",
        "style.logviewer.css", "style.help.css", "style.hotspot.css", "style.flexgap.css",
    ),
}
BUMPED_V029 = {
    "js": ("app.ui.js", "app.auth.js", "app.lottery.js", "app.intro.js"),
    "css": ("style.base.css", "style.modal.css", "style.flexgap.css"),
}
# 语义折叠（可逆）PR：新增 app.comment.semantic.js / style.comment.semantic.css，并给
# app.comment.js 追加 -semantic20261102 后缀（挂入当前 BV 钩子，老缓存必须失效重新加载）。
BUMPED_SEMANTIC = {
    "js": ("app.comment.js",),
}


def _versions(kind: str) -> dict:
    """返回 index.html 中 /static/<kind>/* 的 ?v= 版本号映射。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    return dict(re.findall(rf'/static/{kind}/([\w.]+\.{kind})\?v=([^"]+)"', html))


def test_fixed_scripts_carry_bumped_version() -> None:
    """20261027 一轮改过的脚本仍保留该轮后缀（后续轮次只在其后追加）。"""
    versions = _versions("js")
    for name in BUMPED_SCRIPTS:
        assert name in versions, f"index.html 未引用 {name}"
        assert "-fix20261027" in versions[name], f"{name} 版本号未更新: {versions[name]}"


def test_assets_changed_in_1030_carry_bumped_version() -> None:
    """20261030 一轮改过的脚本与样式表仍保留 -fix20261030 后缀（后续轮次只在其后追加）。"""
    for kind, names in BUMPED_1030.items():
        versions = _versions(kind)
        for name in names:
            assert name in versions, f"index.html 未引用 {name}"
            assert "-fix20261030" in versions[name], f"{name} 版本号未更新: {versions[name]}"


def test_assets_changed_in_ui_redesign_end_with_ui_suffix() -> None:
    """视觉改版 PR 改过的脚本与样式表都带 -ui20261101；没被后续轮次改过的仍以它结尾。"""
    later = set(BUMPED_V029["js"]) | set(BUMPED_V029["css"]) | set(BUMPED_SEMANTIC["js"])
    for kind, names in BUMPED_UI_1101.items():
        versions = _versions(kind)
        for name in names:
            assert name in versions, f"index.html 未引用 {name}"
            assert "-ui20261101" in versions[name], f"{name} 版本号未更新: {versions[name]}"
            if name not in later:
                assert versions[name].endswith("-ui20261101"), f"{name} 版本号未更新: {versions[name]}"


def test_assets_changed_in_v029_end_with_v029_suffix() -> None:
    """桌面端 UI 对齐 + v0.2.9 PR 改过的脚本与样式表都以 -v029ui20261101 结尾。"""
    for kind, names in BUMPED_V029.items():
        versions = _versions(kind)
        for name in names:
            assert name in versions, f"index.html 未引用 {name}"
            assert versions[name].endswith("-v029ui20261101"), f"{name} 版本号未更新: {versions[name]}"


def test_semantic_fold_assets_carry_bumped_version() -> None:
    """语义折叠 PR：app.comment.js 追加 -semantic20261102；新增脚本/样式表各引用一次且带 ?v=。"""
    js_versions = _versions("js")
    for name in BUMPED_SEMANTIC["js"]:
        assert name in js_versions, f"index.html 未引用 {name}"
        assert js_versions[name].endswith("-semantic20261102"), f"{name} 版本号未更新: {js_versions[name]}"
    # 新增的独立脚本 / 样式表：必须被引用且带独立 ?v=（老缓存失效）。
    assert js_versions.get("app.comment.semantic.js"), "index.html 未引用 app.comment.semantic.js"
    css_versions = _versions("css")
    assert css_versions.get("style.comment.semantic.css"), "index.html 未引用 style.comment.semantic.css"


def test_each_asset_is_referenced_once() -> None:
    """每个业务脚本与样式表只引用一次，避免新旧两个版本同时加载。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    groups = (
        ("js", BUMPED_SCRIPTS + BUMPED_1030["js"] + BUMPED_UI_1101["js"] + BUMPED_V029["js"]
            + BUMPED_SEMANTIC["js"] + ("app.comment.semantic.js",)),
        ("css", BUMPED_1030["css"] + BUMPED_UI_1101["css"] + BUMPED_V029["css"]
            + ("style.comment.semantic.css",)),
    )
    for kind, names in groups:
        for name in set(names):
            assert len(re.findall(rf"/static/{kind}/" + re.escape(name) + r"\?", html)) == 1, name
