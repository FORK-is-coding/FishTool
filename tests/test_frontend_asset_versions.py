"""前端脚本缓存版本号契约。

本轮缺陷修复（PR-04/07/08/09/13/14/24/25）改动了下列脚本，各 PR 按约定不改 ``?v=``，
统一由本 PR 追加 ``-fix20261027`` 后缀，确保打包版 / 已打开过页面的浏览器加载新脚本。
"""
from pathlib import Path
import re

INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "frontend" / "templates" / "index.html"
BUMPED_SCRIPTS = ("app.core.js", "app.hotspot.js", "app.events.js", "app.up.js", "app.auth.js", "app.lottery.js")


def _script_versions() -> dict:
    """返回 index.html 中 /static/js/*.js 的 ?v= 版本号映射。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    return dict(re.findall(r'/static/js/([\w.]+\.js)\?v=([^"]+)"', html))


def test_fixed_scripts_carry_bumped_version() -> None:
    """改过的脚本都带本轮版本后缀，避免浏览器沿用旧缓存。"""
    versions = _script_versions()
    for name in BUMPED_SCRIPTS:
        assert name in versions, f"index.html 未引用 {name}"
        assert versions[name].endswith("-fix20261027"), f"{name} 版本号未更新: {versions[name]}"


def test_each_script_is_referenced_once() -> None:
    """每个业务脚本只引用一次，避免同一脚本以新旧两个版本同时加载。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for name in BUMPED_SCRIPTS:
        assert len(re.findall(r'/static/js/' + re.escape(name) + r'\?', html)) == 1, name
