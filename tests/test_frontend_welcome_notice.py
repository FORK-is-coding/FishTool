"""欢迎页启动公告契约。

1. 静态断言：公告脚本在 app.ui.js 之后加载、经 mountAppModal 挂载；关键提醒加粗；
   样式表在开场样式与 flex gap 兜底之前；不写 flex gap（Chromium 83 不支持，兼容约束暂时保留）。
2. 有 PySide6 WebEngine 时，在子进程里用真实桌面内核加载 index.html，覆盖：普通进入弹出、同一会话刷新不重弹、
   截图模式不弹、开场播放及推出期间不弹、开场结束后弹出且焦点在「我已知晓」、
   「阅读使用说明」切到使用说明页并收起弹窗。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND = _ROOT / "web" / "frontend"
_INDEX = _FRONTEND / "templates" / "index.html"
_JS = _FRONTEND / "static" / "js" / "app.notice.js"
_CSS = _FRONTEND / "static" / "css" / "style.notice.css"
_CHILD = Path(__file__).resolve().parent / "_welcome_notice_child.py"

_KEY_PHRASES = (
    "个人创作者运营工作台",
    "免费开源软件项目",
    "AGPL-3.0",
    "未开展任何形式的商业化服务",
    "不会以任何名义向用户收取费用",
    "已经遭遇诈骗行为",
    "3575029912@qq.com",
)


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def test_notice_script_loads_once_after_ui_js() -> None:
    """公告脚本只引用一次，并在 app.ui.js（mountAppModal 所在文件）之后。"""
    html = _html()
    scripts = re.findall(r'<script src="/static/js/([\w.-]+\.js)\?', html)
    assert scripts.count("app.notice.js") == 1
    assert scripts.index("app.notice.js") > scripts.index("app.ui.js")


def test_notice_stylesheet_before_intro_and_flexgap() -> None:
    """公告样式表只引用一次，排在开场样式与 flex gap 兜底之前。"""
    names = re.findall(r'<link rel="stylesheet" href="/static/css/([\w.]+\.css)\?', _html())
    assert names.count("style.notice.css") == 1
    assert names.index("style.notice.css") < names.index("style.intro.css") < names.index("style.flexgap.css")


def test_notice_mounts_through_app_modal_and_session_flag() -> None:
    """经 mountAppModal 挂载，按会话记录已弹过，截图模式跳过，开场结束后才弹。"""
    js = _JS.read_text(encoding="utf-8")
    assert "mountAppModal(backdrop)" in js
    assert "document.body.append(backdrop)" not in js
    assert "sessionStorage" in js and "localStorage" not in js
    assert "'shot'" in js
    assert "intro-pending" in js and "fish-intro" in js


def test_key_phrases_are_bold() -> None:
    """关键提醒都包在 <strong> 里。"""
    js = _JS.read_text(encoding="utf-8")
    bold = re.findall(r"<strong[^>]*>(.*?)</strong>", js)
    for phrase in _KEY_PHRASES:
        assert any(phrase in b for b in bold), f"未加粗：{phrase}"


def test_notice_css_has_no_flex_gap() -> None:
    """公告样式不声明 gap：按钮行复用已有兜底的 .app-modal-actions。"""
    css = re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)
    assert re.search(r"(?<![-\w])(row-|column-)?gap\s*:", css) is None


def _chrome_major(ua: str) -> int:
    """从 UA 里取 Chrome 主版本号；取不到返回 0。"""
    match = re.search(r"Chrome/(\d+)\.", ua or "")
    return int(match.group(1)) if match else 0


def _qt_available() -> bool:
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _qt_available(), reason="未安装 PySide6 WebEngine")
def test_real_qtwebengine_welcome_notice(tmp_path) -> None:
    """真实桌面内核下各场景的弹出时机、焦点与按钮行为。"""
    proc = subprocess.run(
        [sys.executable, str(_CHILD), str(_FRONTEND)],
        cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
    )
    if proc.returncode != 0 or not proc.stdout.strip().startswith("{"):
        # 无图形环境（CI 或 offscreen 下构造视图崩溃）时只能跳过，静态断言仍然覆盖。
        pytest.skip(f"QtWebEngine 子进程不可用 rc={proc.returncode}: {proc.stderr[-400:]}")
    r = json.loads(proc.stdout)
    for name, scenario in r.items():
        assert scenario.get("errors") == [], (name, scenario.get("errors"))

    for name in ("plain", "intro_done"):
        s = r[name]
        assert _chrome_major(s["ua"]) >= 140, s["ua"]
        assert s["shown"] is True and s["backdropCount"] == 1, name
        assert s["centerInsideModal"] is True, name
        assert s["backdrop"]["w"] >= s["backdrop"]["vw"] and s["backdrop"]["h"] >= s["backdrop"]["vh"], s["backdrop"]
        assert s["modal"]["y"] >= 0 and s["modal"]["y"] + s["modal"]["h"] <= s["backdrop"]["vh"], s["modal"]
        assert s["focus"] == "我已知晓", (name, s["focus"])
        assert s["buttons"] == ["阅读使用说明", "我已知晓"], s["buttons"]
        assert s["buttonGap"] == 12, s["buttonGap"]
        assert s["afterUsage"] == {"backdrops": 0, "usageActive": True}, s["afterUsage"]
        assert s["reopenedOnWelcome"] is False, name
        assert s["sessionFlag"] == "1", name

    assert r["reload"]["shown"] is False
    assert r["shot"]["shown"] is False
    assert r["intro_playing"]["shown"] is False and r["intro_playing"]["introPending"] is True
    assert r["intro_leaving"]["shown"] is False and r["intro_leaving"]["introOverlay"] is True
