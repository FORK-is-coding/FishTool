"""视图转换：绝对 /static 全部改写、shim 注入顺序、文件名替换、禁用 query 参数守护。

主线前端改了 index.html / CSS 写法后，这里第一时间失败，提示更新转换规则。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import ASTRBOT_DIR, REPO_ROOT
from release_tools.transform_views import (
    BRIDGE_SDK_TAG,
    OVERRIDES_TAG,
    SHIM_TAG,
    check_no_absolute_static,
    transform_css,
    transform_views,
)

FRONTEND = REPO_ROOT / "web" / "frontend"


@pytest.fixture(scope="module")
def views(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("views") / "console"
    return transform_views(FRONTEND, ASTRBOT_DIR / "frontend_shim", out)


def test_no_absolute_static_left(views: Path):
    assert check_no_absolute_static(views) == []


def test_shim_injected_before_app_scripts(views: Path):
    html = (views / "index.html").read_text("utf-8")
    i_sdk = html.index(BRIDGE_SDK_TAG)
    i_shim = html.index(SHIM_TAG)
    i_first_css = html.index('href="./static/css/')
    i_core = html.index('src="./static/js/app.core.js')
    i_over = html.index(OVERRIDES_TAG)
    assert i_sdk < i_shim < i_first_css < i_core < i_over
    assert html.count(BRIDGE_SDK_TAG) == 1
    assert 'data-three="./static/vendor/three.min.js' in html
    assert 'data-logo="./static/intro/logo-mark.js' in html


def test_every_referenced_asset_exists(views: Path):
    html = (views / "index.html").read_text("utf-8")
    refs = re.findall(r'(?:href|src|data-three|data-logo)="\./([^"?#]+)', html)
    assert refs, "no relative asset refs found"
    for ref in refs:
        assert (views / ref).is_file(), ref
    for css in (views / "static" / "css").glob("*.css"):
        for ref in re.findall(r'url\(\s*["\']?(\.\./[^)"\']+)', css.read_text("utf-8")):
            assert (css.parent / ref).resolve().is_file(), f"{css.name}: {ref}"
    fonts = (views / "static" / "fonts" / "fonts.css").read_text("utf-8")
    for ref in re.findall(r'url\(\s*["\']?(\./[^)"\']+)', fonts):
        assert (views / "static" / "fonts" / ref).resolve().is_file(), ref


def test_chinese_filenames_replaced(views: Path):
    assert (views / "static" / "group-qr.jpg").is_file()
    assert not (views / "static" / "群聊.jpg").exists()
    assert not (views / "static" / "图标.png").exists()
    ui = (views / "static" / "js" / "app.ui.js").read_text("utf-8")
    assert "static/group-qr.jpg" in ui and "群聊.jpg" not in ui


def test_intro_fallback_paths_rewritten(views: Path):
    intro = (views / "static" / "js" / "app.intro.js").read_text("utf-8")
    assert "'/static/" not in intro and '"/static/' not in intro


def test_transform_css_rules():
    assert transform_css('@import url("/static/fonts/fonts.css");', in_fonts_dir=False) == '@import url("../fonts/fonts.css");'
    assert transform_css("url(/static/tex/a.svg)", in_fonts_dir=False) == "url(../tex/a.svg)"
    assert transform_css("url('/static/fonts/x.woff2')", in_fonts_dir=True) == "url('./x.woff2')"


def test_frontend_query_has_no_api_key_params():
    """query 里出现 key / api_key 会被 AstrBot 当成 API Key 认证并返回 401（执行案 7.1）。"""
    pattern = re.compile(r"[?&](?:key|api_key)=")
    for path in list((FRONTEND / "static" / "js").glob("*.js")) + [FRONTEND / "templates" / "index.html"]:
        text = path.read_text("utf-8")
        assert not pattern.search(text), path.name
        assert "searchParams.set('key'" not in text and 'searchParams.set("key"' not in text


def test_only_one_fetch_and_websocket_in_frontend():
    """shim 只替换全局 fetch / WebSocket；新增其它网络通道（EventSource、XMLHttpRequest）需要同步适配。"""
    js = "\n".join(p.read_text("utf-8") for p in (FRONTEND / "static" / "js").glob("*.js"))
    assert "EventSource(" not in js
    assert "XMLHttpRequest" not in js
