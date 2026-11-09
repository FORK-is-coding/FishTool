"""前端静态资源缓存版本号契约。

修复 PR 按约定不改 ``?v=``，由版本号 PR 在最后统一追加后缀，
确保打包版 / 已打开过页面的浏览器加载新资源。

- ``-fix20261027``：PR-04/07/08/09/13/14/24/25 改动过的脚本；
- ``-fix20261030``：本轮改动过的脚本与样式表；
- ``-ui20261101``：前端视觉改版 PR（C+ 白色档案）改动过的脚本与全部样式表；
- ``-v029ui20261101``：桌面端 UI 对齐 + v0.2.9 PR 改动过的脚本与样式表；
- ``-v030``：v0.3.0 PR（版本号与 AGPL-3.0 声明）改动过的脚本；
- ``-v031``：v0.3.1 PR（开场 BUILD 号）改动过的脚本，必须是最后一段后缀；
- ``-budget20261104``：语义折叠预算模式 / 停止报告 / 预估时间一轮改动的资源；
- ``-flexgap20261105``：给语义预算模式补 flex gap 兜底，``style.flexgap.css`` 追加；
- ``-fix20261107``：文件最后一次修改晚于其 ``?v=`` 最后一次修改的脚本（含之前版本号 PR 漏追加的，#92）。
- ``-commentmode20261109``：抽奖视频评论采集方式（常规 / 楼中楼）改过的脚本与样式表，必须是这两个文件的最后一段后缀。
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
# v0.3.0 PR：开场 BUILD 号与启动公告里的开源协议名称，两个脚本追加 -v030。
BUMPED_V030 = {
    "js": ("app.intro.js", "app.notice.js"),
}
# v0.3.1 PR：开场 BUILD 号改为 0.3.1，app.intro.js 追加 -v031。
BUMPED_V031 = {
    "js": ("app.intro.js",),
}
# flex gap 兜底补漏 PR：style.flexgap.css 新增语义预算模式两条规则，追加 -flexgap20261105。
BUMPED_FLEXGAP_1105 = {
    "css": ("style.flexgap.css",),
}
# 语义折叠（可逆）PR：新增 app.comment.semantic.js / style.comment.semantic.css，并给
# app.comment.js 追加 -semantic20261102 后缀（挂入当前 BV 钩子，老缓存必须失效重新加载）。
BUMPED_SEMANTIC = {
    "js": ("app.comment.js",),
}
# 语义折叠预算模式 / 停止报告 / 预估时间 PR：给三个语义资源统一追加 -budget20261104，
# 否则打包版与已打开过页面的浏览器会继续使用旧缓存的面板脚本与样式。
BUMPED_BUDGET = {
    "js": ("app.comment.semantic.js", "app.comment.js"),
    "css": ("style.comment.semantic.css",),
}
# 20261107：文件最后一次修改晚于它的 ?v= 最后一次修改的脚本，统一追加 -fix20261107（#92）。
# 其中一部分是版本号 PR 之后才改的，另一部分在版本号 PR 之前就改了、但那次没追加：
# - app.config.js：单次输出上限输入框与 saveLLMMaxTokens（#76，晚于版本号 PR #71/#72），
#   以及每日上限保存（#61，版本号 PR 漏追加）；旧缓存下点“保存输出上限”会报 saveLLMMaxTokens is not defined。
# - app.hotspot.js / app.events.js / app.lottery.js：展示后端 warning 与每日上限提示（#62，
#   早于版本号 PR #71/#72，但那两次只覆盖了语义与 intro 资源，漏了它们）。
# - app.comment.semantic.js：预计剩余时间的毫秒换算与对时（#90，本分支）。
BUMPED_FIX_1107 = {
    "js": ("app.config.js", "app.hotspot.js", "app.events.js", "app.lottery.js",
           "app.comment.semantic.js"),
}
# 抽奖视频评论采集方式 PR：常规 / 楼中楼选择栏与模式相关文案，两个抽奖资源追加 -commentmode20261109。
BUMPED_COMMENTMODE = {
    "js": ("app.lottery.js",),
    "css": ("style.lottery.css",),
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
    later = (
        set(BUMPED_V029["js"]) | set(BUMPED_V029["css"]) | set(BUMPED_SEMANTIC["js"]) | set(BUMPED_V030["js"])
        | set(BUMPED_FLEXGAP_1105["css"]) | set(BUMPED_FIX_1107["js"])
        | set(BUMPED_COMMENTMODE["js"]) | set(BUMPED_COMMENTMODE["css"])
    )
    for kind, names in BUMPED_UI_1101.items():
        versions = _versions(kind)
        for name in names:
            assert name in versions, f"index.html 未引用 {name}"
            assert "-ui20261101" in versions[name], f"{name} 版本号未更新: {versions[name]}"
            if name not in later:
                assert versions[name].endswith("-ui20261101"), f"{name} 版本号未更新: {versions[name]}"


def test_assets_changed_in_v029_end_with_v029_suffix() -> None:
    """桌面端 UI 对齐 + v0.2.9 PR 改过的脚本与样式表都带 -v029ui20261101；没被 v0.3.0 改过的仍以它结尾。"""
    later = set(BUMPED_V030["js"]) | set(BUMPED_FLEXGAP_1105["css"]) | set(BUMPED_FIX_1107["js"])
    for kind, names in BUMPED_V029.items():
        versions = _versions(kind)
        for name in names:
            assert name in versions, f"index.html 未引用 {name}"
            assert "-v029ui20261101" in versions[name], f"{name} 版本号未更新: {versions[name]}"
            if name not in later:
                assert versions[name].endswith("-v029ui20261101"), f"{name} 版本号未更新: {versions[name]}"


def test_assets_changed_in_v030_end_with_v030_suffix() -> None:
    """v0.3.0 PR 改过的脚本都带 -v030；没被 v0.3.1 改过的仍以它结尾（只追加，不改历史后缀）。"""
    versions = _versions("js")
    later = set(BUMPED_V031["js"])
    for name in BUMPED_V030["js"]:
        assert name in versions, f"index.html 未引用 {name}"
        assert "-v030" in versions[name], f"{name} 版本号未更新: {versions[name]}"
        if name not in later:
            assert versions[name].endswith("-v030"), f"{name} 版本号未更新: {versions[name]}"


def test_assets_changed_in_v031_end_with_v031_suffix() -> None:
    """v0.3.1 PR 改过的脚本以 -v031 结尾。"""
    versions = _versions("js")
    for name in BUMPED_V031["js"]:
        assert name in versions, f"index.html 未引用 {name}"
        assert versions[name].endswith("-v031"), f"{name} 版本号未更新: {versions[name]}"



def test_flexgap_fallback_patch_ends_with_its_suffix() -> None:
    """flex gap 兜底补漏 PR 改过的样式表以 -flexgap20261105 结尾（只追加，不改历史后缀）。"""
    versions = _versions("css")
    for name in BUMPED_FLEXGAP_1105["css"]:
        assert name in versions, f"index.html 未引用 {name}"
        assert "-v029ui20261101" in versions[name], f"{name} 版本号未更新: {versions[name]}"
        assert versions[name].endswith("-flexgap20261105"), f"{name} 版本号未更新: {versions[name]}"


def test_semantic_fold_assets_carry_bumped_version() -> None:
    """语义折叠 PR：app.comment.js 追加 -semantic20261102；新增脚本/样式表各引用一次且带 ?v=。"""
    js_versions = _versions("js")
    for name in BUMPED_SEMANTIC["js"]:
        assert name in js_versions, f"index.html 未引用 {name}"
        assert js_versions[name].endswith("-semantic20261102-budget20261104"), \
            f"{name} 版本号未更新: {js_versions[name]}"
    # 新增的独立脚本 / 样式表：必须被引用且带独立 ?v=（老缓存失效）。
    assert js_versions.get("app.comment.semantic.js"), "index.html 未引用 app.comment.semantic.js"
    css_versions = _versions("css")
    assert css_versions.get("style.comment.semantic.css"), "index.html 未引用 style.comment.semantic.css"


def test_semantic_budget_assets_carry_bumped_version() -> None:
    """预算模式 / 停止报告 / 预估时间一轮：三个语义资源统一追加 -budget20261104；没被后续轮次改过的仍以它结尾。"""
    js_versions = _versions("js")
    css_versions = _versions("css")
    later = set(BUMPED_FIX_1107["js"])
    for name in BUMPED_BUDGET["js"]:
        assert name in js_versions, f"index.html 未引用 {name}"
        assert "-budget20261104" in js_versions[name], f"{name} 版本号未更新: {js_versions[name]}"
        if name not in later:
            assert js_versions[name].endswith("-budget20261104"), \
                f"{name} 版本号未更新: {js_versions[name]}"
    for name in BUMPED_BUDGET["css"]:
        assert name in css_versions, f"index.html 未引用 {name}"
        assert css_versions[name].endswith("-budget20261104"), \
            f"{name} 版本号未更新: {css_versions[name]}"


def test_assets_changed_after_last_bump_end_with_1107_suffix() -> None:
    """#92：文件改动晚于其 ?v= 的脚本追加 -fix20261107（只追加，不改历史后缀）。"""
    versions = _versions("js")
    later = set(BUMPED_COMMENTMODE["js"])
    for name in BUMPED_FIX_1107["js"]:
        assert name in versions, f"index.html 未引用 {name}"
        assert "-fix20261107" in versions[name], f"{name} 版本号未更新: {versions[name]}"
        if name not in later:
            assert versions[name].endswith("-fix20261107"), f"{name} 版本号未更新: {versions[name]}"
    # 历史后缀保留：不把旧后缀替换掉
    assert "-fix20261030" in versions["app.config.js"]
    assert "-budget20261104" in versions["app.comment.semantic.js"]


def test_lottery_comment_mode_assets_end_with_their_suffix() -> None:
    """抽奖评论采集方式 PR 改过的脚本与样式表以 -commentmode20261109 结尾（只追加，不改历史后缀）。"""
    for kind, names in BUMPED_COMMENTMODE.items():
        versions = _versions(kind)
        for name in names:
            assert name in versions, f"index.html 未引用 {name}"
            assert versions[name].endswith("-commentmode20261109"), f"{name} 版本号未更新: {versions[name]}"
    # 历史后缀保留
    assert "-fix20261107" in _versions("js")["app.lottery.js"]
    assert "-ui20261101" in _versions("css")["style.lottery.css"]


def test_each_asset_is_referenced_once() -> None:
    """每个业务脚本与样式表只引用一次，避免新旧两个版本同时加载。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    groups = (
        ("js", BUMPED_SCRIPTS + BUMPED_1030["js"] + BUMPED_UI_1101["js"] + BUMPED_V029["js"]
            + BUMPED_SEMANTIC["js"] + BUMPED_V030["js"] + BUMPED_V031["js"] + BUMPED_FIX_1107["js"]
            + ("app.comment.semantic.js",)),
        ("css", BUMPED_1030["css"] + BUMPED_UI_1101["css"] + BUMPED_V029["css"]
            + ("style.comment.semantic.css",)),
    )
    for kind, names in groups:
        for name in set(names):
            assert len(re.findall(rf"/static/{kind}/" + re.escape(name) + r"\?", html)) == 1, name
