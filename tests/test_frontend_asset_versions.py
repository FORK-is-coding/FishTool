"""前端静态资源缓存版本号契约。

修复 PR 按约定不改 ``?v=``，由版本号 PR 在最后统一追加后缀，
确保打包版 / 已打开过页面的浏览器加载新资源。

- ``-fix20261027``：PR-04/07/08/09/13/14/24/25 改动过的脚本；
- ``-fix20261030``：本轮改动过的脚本与样式表。
"""
from pathlib import Path
import re

INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "frontend" / "templates" / "index.html"
BUMPED_SCRIPTS = ("app.core.js", "app.hotspot.js", "app.events.js", "app.up.js", "app.auth.js", "app.lottery.js")
BUMPED_1030 = {
    "js": ("app.core.js", "app.lottery.js", "app.auth.js", "app.config.js", "app.ui.js", "app.hotspot.js"),
    "css": ("style.hotspot.css", "style.modal.css"),
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
    """本轮改过的脚本与样式表都带 -fix20261030 后缀。"""
    for kind, names in BUMPED_1030.items():
        versions = _versions(kind)
        for name in names:
            assert name in versions, f"index.html 未引用 {name}"
            assert versions[name].endswith("-fix20261030"), f"{name} 版本号未更新: {versions[name]}"


def test_each_asset_is_referenced_once() -> None:
    """每个业务脚本与样式表只引用一次，避免新旧两个版本同时加载。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for kind, names in (("js", BUMPED_SCRIPTS + BUMPED_1030["js"]), ("css", BUMPED_1030["css"])):
        for name in set(names):
            assert len(re.findall(rf"/static/{kind}/" + re.escape(name) + r"\?", html)) == 1, name
