"""首次启动开场动画契约（INTRO_PLAN 5.2）。

1. 静态断言：新增资源齐全（three.js r160 + LICENSE、LOGO 数据、控制器、首屏判断、样式）；
   index.html 的四处接入位置正确；intro-gate.js 不依赖 app.core.js；
   去掉 // 与 /* */ 注释后，开场脚本与样式不含 Chromium 83 不支持的写法。
2. 有 node 时，用 vm 加最小的伪 window/document 运行 intro-gate.js，逐项验证播放条件与 10s 兜底。
3. 有 Qt 时，在子进程里用真实 QtWebEngine 加载 index.html（无痕 profile）：
   ?intro=full 能播完、Escape 跳过后很快结束、普通浏览器默认不播。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND = _ROOT / "web" / "frontend"
_STATIC = _FRONTEND / "static"
_INDEX = _FRONTEND / "templates" / "index.html"
_GATE = _STATIC / "js" / "intro-gate.js"
_INTRO = _STATIC / "js" / "app.intro.js"
_CSS = _STATIC / "css" / "style.intro.css"
_CORE = _STATIC / "js" / "app.core.js"
_THREE = _STATIC / "vendor" / "three.min.js"
_CHILD = Path(__file__).resolve().parent / "_intro_child.py"

_NEW_FILES = (
    _GATE, _INTRO, _CSS, _THREE,
    _STATIC / "vendor" / "three.LICENSE.txt",
    _STATIC / "intro" / "logo-mark.js",
    _ROOT / "tools" / "intro" / "make_logo_mark.py",
)

# Chromium 83 不支持的 JS 写法（ES2020 起）；?. 排除 a ?.5 : b 这类三元加小数
_JS_RULES = {
    "可选链 ?.": re.compile(r"\?\.(?!\d)"),
    "空值合并 ??": re.compile(r"\?\?"),
    "逻辑赋值 ||=": re.compile(r"\|\|="),
    "逻辑赋值 &&=": re.compile(r"&&="),
    "Array.at": re.compile(r"\.at\("),
    "String.replaceAll": re.compile(r"\.replaceAll\("),
    "structuredClone": re.compile(r"\bstructuredClone\b"),
    "Promise.any": re.compile(r"\bPromise\.any\b"),
}
# Chromium 83 不支持的 CSS 写法；flex 容器 gap 由 style.flexgap.css 兜底，开场样式直接不写 gap
_CSS_RULES = {
    "inset 简写": re.compile(r"(?<![-\w])inset\s*:"),
    ":is()": re.compile(r":is\("),
    ":where()": re.compile(r":where\("),
    ":has()": re.compile(r":has\("),
    ":focus-visible": re.compile(r":focus-visible"),
    "aspect-ratio": re.compile(r"aspect-ratio"),
    "color-mix()": re.compile(r"color-mix\("),
    "无前缀 mask-image": re.compile(r"(?<!-webkit-)mask-image"),
    "gap": re.compile(r"(?<![-\w])(?:row-|column-)?gap\s*:"),
}


def _strip_js_comments(text: str) -> str:
    """去掉 // 与 /* */ 注释，保留换行（行号不变）；跳过字符串内的 // 与 /*。

    开场脚本的字符串里有 'FISHTOOL // CONSOLE' 这类文案，按行正则会误删整行，所以逐字符扫描。
    """
    out, i, n, quote = [], 0, len(text), ""
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
        elif ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append(re.sub(r"[^\n]", " ", text[i:end]))
            i = end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _strip_css_comments(text: str) -> str:
    """去掉 CSS 注释但保留换行。"""
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def _hits(path: Path, rules: dict, strip) -> list[str]:
    text = strip(path.read_text(encoding="utf-8"))
    return [
        f"{path.name}:{text.count(chr(10), 0, m.start()) + 1} {label}"
        for label, pattern in rules.items() for m in pattern.finditer(text)
    ]


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


# ---------------- 资源 ----------------

def test_intro_assets_exist() -> None:
    """开场动画新增的脚本、样式、LOGO 数据、three.js 与许可证、生成工具都在仓库里。"""
    missing = [p.relative_to(_ROOT).as_posix() for p in _NEW_FILES if not p.is_file()]
    assert missing == [], missing


def test_three_revision_is_r160() -> None:
    """three.min.js 是 r160（Chromium 83 实测可用的版本），许可证随附。"""
    text = _THREE.read_text(encoding="utf-8")
    # 压缩版写成 const e="160" … REVISION=e，未压缩版写成 REVISION = '160'
    direct = re.search(r"REVISION\s*=\s*['\"](\d+)['\"]", text)
    if direct:
        revision = direct.group(1)
    else:
        alias = re.search(r"\bREVISION\s*=\s*([A-Za-z_$][\w$]*)", text)
        assert alias, "three.min.js 中找不到 REVISION"
        value = re.search(r"[\s,;{(]" + re.escape(alias.group(1)) + r"\s*=\s*['\"](\d+)['\"]", text)
        assert value, f"找不到 {alias.group(1)} 的取值"
        revision = value.group(1)
    assert revision == "160"
    assert "MIT License" in (_STATIC / "vendor" / "three.LICENSE.txt").read_text(encoding="utf-8")


def test_controller_defaults_point_to_static_paths() -> None:
    """app.intro.js 的按需加载默认路径是 /static 下的真实位置（data-* 缺失时也能找到资源）。"""
    text = _INTRO.read_text(encoding="utf-8")
    assert "'/static/vendor/three.min.js'" in text
    assert "'/static/intro/logo-mark.js'" in text
    assert "window.FISH_LOGO = 'data:image/png;base64," in (_STATIC / "intro" / "logo-mark.js").read_text(encoding="utf-8")


def test_intro_css_textures_use_static_paths() -> None:
    """style.intro.css 的纹理都用 /static/tex/ 绝对路径，且文件存在。"""
    css = _strip_css_comments(_CSS.read_text(encoding="utf-8"))
    urls = re.findall(r'url\("([^"]+)"\)', css)
    assert urls, "style.intro.css 应引用 C+ 纹理"
    assert all(u.startswith("/static/tex/") for u in urls), urls
    missing = [u for u in urls if not (_STATIC / u[len("/static/"):]).is_file()]
    assert missing == [], missing


# ---------------- index.html 接入 ----------------

def test_intro_stylesheet_before_flexgap_fallback() -> None:
    """style.intro.css 紧挨在 style.flexgap.css 之前，flex gap 兜底仍是最后一个样式表。"""
    links = re.findall(r'<link rel="stylesheet" href="/static/css/([\w.]+\.css)\?v=([^"]+)"', _html())
    names = [name for name, _ in links]
    assert names[-2:] == ["style.intro.css", "style.flexgap.css"], names
    assert dict(links)["style.intro.css"] == "ui20261101"


def test_intro_gate_is_last_in_head() -> None:
    """intro-gate.js 在 <head> 末尾同步加载（不带 async/defer），在主界面渲染前决定是否遮住。"""
    html = _html()
    head = html[:html.index("</head>")]
    tag = '<script src="/static/js/intro-gate.js?v=ui20261101"></script>'
    assert head.count(tag) == 1
    assert head.rstrip().endswith(tag)
    assert html.count("intro-gate.js") == 1


def test_intro_controller_follows_core() -> None:
    """app.intro.js 紧跟 app.core.js，并用 data-three / data-logo 指明按需加载的资源。"""
    scripts = re.findall(r'<script src="/static/js/(app\.[a-z]+\.js)', _html())
    assert scripts[:2] == ["app.core.js", "app.intro.js"], scripts
    tag = re.search(r'<script src="/static/js/app\.intro\.js\?v=([^"]+)"([^>]*)>', _html())
    # 开场 PR 引入时是 ui20261101；之后的 PR 只在其后追加后缀（v0.2.9 改了开场里的 BUILD 号）。
    assert tag and tag.group(1).startswith("ui20261101"), tag and tag.group(1)
    assert 'data-three="/static/vendor/three.min.js?v=r160"' in tag.group(2)
    assert 'data-logo="/static/intro/logo-mark.js?v=ui20261101"' in tag.group(2)


def test_replay_button_inside_hero_meta() -> None:
    """重播按钮在欢迎页 .hero-meta 内、.meta-r 之后，内联事件只传固定的 event。"""
    html = _html()
    meta = re.search(r'<div class="hero-meta">(.*?)\n\s*</div>', html, re.S)
    assert meta, "欢迎页缺少 .hero-meta"
    body = meta.group(1)
    assert html.count('id="intro-replay-btn"') == 1
    assert 'id="intro-replay-btn"' in body
    assert body.index('class="meta-r"') < body.index('id="intro-replay-btn"')
    button = re.search(r'<button[^>]*id="intro-replay-btn"[^>]*>(.*?)</button>', body, re.S)
    assert button
    assert 'onclick="FishIntro.replay(event)"' in button.group(0)
    assert 'type="button"' in button.group(0)
    assert "<svg" in button.group(1) and "REPLAY INTRO" in button.group(1)


# ---------------- 依赖与语法 ----------------

def test_intro_gate_does_not_use_core_helpers() -> None:
    """intro-gate.js 先于 app.core.js 加载，不能引用其中的任何函数。"""
    core = _CORE.read_text(encoding="utf-8")
    helpers = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", core, re.M))
    assert {"escapeHtml", "jsArg", "apiRequest"} <= helpers
    gate = _strip_js_comments(_GATE.read_text(encoding="utf-8"))
    used = sorted(h for h in helpers if re.search(rf"(?<![\w$.]){re.escape(h)}\s*\(", gate))
    assert used == [], f"intro-gate.js 引用了 app.core.js 的函数：{used}"
    assert "API_BASE" not in gate


def test_intro_scripts_have_no_es2020_syntax() -> None:
    """去掉全部注释后，开场脚本不使用 ES2020 起的语法与 API。"""
    hits = []
    for path in (_GATE, _INTRO, _STATIC / "intro" / "logo-mark.js"):
        hits += _hits(path, _JS_RULES, _strip_js_comments)
    assert hits == []


def test_intro_css_has_no_chromium83_unsupported_syntax() -> None:
    """去掉注释后，style.intro.css 不使用 Chromium 83 不支持的写法，也不写 gap。"""
    assert _hits(_CSS, _CSS_RULES, _strip_css_comments) == []

# ---------------- 首屏判断行为（node） ----------------

_GATE_NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');
// node -e 下 argv 没有脚本路径位，取末尾两个参数，与调用方式无关。
const [gatePath, casesJson] = process.argv.slice(-2);
const source = fs.readFileSync(gatePath, 'utf8');
const cases = JSON.parse(casesJson);

function run(c) {
  const classes = new Set();
  const timers = [];
  const removed = [];
  // 兜底计时器到点时要移除的开场层：只有 parentNode.removeChild 被用到
  const overlay = { parentNode: { removeChild(el) { removed.push(el); } } };
  const storage = {
    getItem(key) {
      if (c.storageThrows) throw new Error('SecurityError');
      return c.stored && key === 'fishtool.intro.v1' ? '1' : null;
    },
  };
  const win = {
    location: { search: c.search },
    URLSearchParams,
    setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
    document: {
      documentElement: {
        classList: {
          add(n) { classes.add(n); },
          remove(n) { classes.delete(n); },
          contains(n) { return classes.has(n); },
        },
      },
      getElementById(id) { return id === 'fish-intro' && c.overlay ? overlay : null; },
    },
  };
  if (c.storageThrows) {
    Object.defineProperty(win, 'localStorage', { get() { throw new Error('SecurityError'); } });
  } else {
    win.localStorage = storage;
  }
  if (c.desktop) win.__FISHTOOL_DESKTOP__ = true;
  win.window = win;
  vm.createContext(win);
  vm.runInContext(source, win);
  const out = {
    name: c.name,
    pending: classes.has('intro-pending'),
    timers: timers.map(t => t.ms),
    gateTimer: win.__fishIntroGate === undefined ? null : win.__fishIntroGate,
  };
  if (c.fireTimers) {
    timers.forEach(t => t.fn());
    out.pendingAfterTimer = classes.has('intro-pending');
    out.overlayRemoved = removed.length === 1 && removed[0] === overlay;
  }
  return out;
}

process.stdout.write(JSON.stringify(cases.map(run)));
"""

_GATE_CASES = [
    {"name": "desktop_first", "search": "", "desktop": True},
    {"name": "desktop_played", "search": "", "desktop": True, "stored": True},
    {"name": "browser", "search": ""},
    {"name": "browser_force_full", "search": "?intro=full"},
    {"name": "browser_force_lite", "search": "?intro=lite"},
    {"name": "desktop_off", "search": "?intro=off", "desktop": True},
    {"name": "desktop_shot", "search": "?shot=1", "desktop": True},
    {"name": "desktop_storage_throws", "search": "", "desktop": True, "storageThrows": True},
    {"name": "fallback_timer", "search": "", "desktop": True, "overlay": True, "fireTimers": True},
]


@pytest.fixture(scope="module")
def gate_results() -> dict:
    if shutil.which("node") is None:
        pytest.skip("本机无 node")
    proc = subprocess.run(
        ["node", "-e", _GATE_NODE_SCRIPT, str(_GATE), json.dumps(_GATE_CASES)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    )
    return {item["name"]: item for item in json.loads(proc.stdout)}


@pytest.mark.parametrize(
    "name, expected",
    [
        ("desktop_first", True),
        ("desktop_played", False),
        ("browser", False),
        ("browser_force_full", True),
        ("browser_force_lite", True),
        ("desktop_off", False),
        ("desktop_shot", False),
        ("desktop_storage_throws", True),
    ],
)
def test_gate_play_conditions(gate_results, name, expected) -> None:
    """桌面端首启才默认播放；URL 参数可强制播放或关闭；localStorage 不可读时桌面端照常播放。"""
    result = gate_results[name]
    assert result["pending"] is expected, result
    # 播放时才挂 10s 兜底计时器，不播放时什么都不做
    assert result["timers"] == ([10000] if expected else []), result
    assert (result["gateTimer"] is not None) is expected, result


def test_gate_fallback_timer_removes_mask(gate_results) -> None:
    """控制器没接手时，10s 兜底计时器移除 intro-pending 并删掉残留的开场层。"""
    result = gate_results["fallback_timer"]
    assert result["pending"] is True
    assert result["pendingAfterTimer"] is False
    assert result["overlayRemoved"] is True


# ---------------- 真实内核（QtWebEngine） ----------------

def _chrome_major(ua: str) -> int:
    """从 UA 里取 Chrome 主版本号；取不到返回 0。"""
    match = re.search(r"Chrome/(\d+)\.", ua or "")
    return int(match.group(1)) if match else 0


def _qt_available() -> bool:
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        # 没装 PySide6 WebEngine：真实内核用例跳过，静态断言仍然覆盖。
        return False
    return True


@pytest.fixture(scope="module")
def qt_results(tmp_path_factory) -> dict:
    if not _qt_available():
        pytest.skip("未安装 PySide6 WebEngine")
    proc = subprocess.run(
        [sys.executable, str(_CHILD), str(_FRONTEND)],
        cwd=str(tmp_path_factory.mktemp("intro")), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    if proc.returncode != 0 or not proc.stdout.strip().startswith("{"):
        # 无图形环境（CI 或 offscreen 下构造视图崩溃）时只能跳过，静态断言仍然覆盖。
        pytest.skip(f"QtWebEngine 子进程不可用 rc={proc.returncode}: {proc.stderr[-400:]}")
    return json.loads(proc.stdout)


def test_real_qtwebengine_full_intro_finishes(qt_results) -> None:
    """?intro=full：10 秒内播完，无 JS 报错，开场层与 intro-pending 都已移除，主界面可见。"""
    result = qt_results["full"]
    assert _chrome_major(result["ua"]) >= 140, result["ua"]
    assert result["errors"] == [], result["errors"]
    assert result["state"] == "done", result
    assert result["playAt"] is not None and result["doneAt"] is not None, result
    assert result["doneAt"] - result["playAt"] <= 10000, result
    assert result["pending"] is False and result["overlay"] is False, result
    assert result["sidebarVisibility"] == "visible", result


def test_real_qtwebengine_escape_skips_quickly(qt_results) -> None:
    """?intro=full 播放 1.5 秒后按 Escape：从跳过到 done 不超过 1.2 秒。"""
    result = qt_results["skip"]
    assert result["errors"] == [], result["errors"]
    assert result["state"] == "done", result
    assert result["skipAt"] is not None and result["doneAt"] is not None, result
    assert result["doneAt"] > result["skipAt"], result
    assert result["doneAt"] - result["skipAt"] <= 1200, result
    assert result["pending"] is False and result["overlay"] is False, result


def test_real_qtwebengine_console_errors_are_captured(qt_results) -> None:
    """反向验证：探针确实能收到 console.error（否则上面几条「errors == []」形同虚设）。

    PySide6 的 console 级别是不可比较大小的枚举；判断写错时回调里抛异常被吞掉，错误一条都收不到，测试却全绿。
    """
    errors = qt_results["canary"]["errors"]
    assert any("FishIntro canary" in e for e in errors), errors


def test_real_qtwebengine_browser_stays_idle(qt_results) -> None:
    """不带参数、也没有 __FISHTOOL_DESKTOP__：一直 idle，不遮主界面，也不下载 three.js。"""
    result = qt_results["idle"]
    assert result["errors"] == [], result["errors"]
    assert result["desktopFlag"] is False
    assert result["state"] == "idle", result
    assert result["playAt"] is None, result
    assert result["pending"] is False and result["overlay"] is False, result
    assert result["threeLoaded"] is False, result
    assert result["replayButton"] is True
    assert result["sidebarVisibility"] == "visible", result
