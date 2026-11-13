"""构建期视图转换：FishTool 前端 → AstrBot 插件视图 ``pages/console/``。

只改写发布产物，不改仓库原文件。规则（执行案 5.2）：

- index.html：``href/src/data-three/data-logo="/static/…"`` → ``./static/…``；
  ``<head>`` 最前插入 bridge SDK 与 bridge-shim；``</body>`` 前插入 astrbot-overrides。
- app.intro.js：兜底 ``'/static/…'`` → ``'./static/…'``。
- css/*.css：``url("/static/x/…")`` → ``url("../x/…")``。
- fonts/fonts.css：``url('/static/fonts/x')`` → ``url('./x')``。
- ``群聊.jpg`` → ``group-qr.jpg``，并改 app.ui.js 的引用；``图标.png`` 不复制。

转换后断言：输出中不得再出现指向 ``/static/`` 的资源引用。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

BRIDGE_SDK_TAG = '<script src="/api/plugin/page/bridge-sdk.js"></script>'
SHIM_TAG = '<script src="./bridge-shim.js"></script>'
SHIM_CSS_TAG = '<link rel="stylesheet" href="./shim.css">'
OVERRIDES_TAG = '<script src="./astrbot-overrides.js"></script>'

#: 中文文件名改为 ASCII，避免路径编码问题。
RENAMES = {"群聊.jpg": "group-qr.jpg"}
#: 前端未引用的大文件不复制。
SKIP_FILES = {"图标.png"}

_HTML_ATTR_RE = re.compile(r'(?P<attr>\b(?:href|src|data-three|data-logo))=(?P<q>["\'])/static/', re.IGNORECASE)
_CSS_URL_RE = re.compile(r'url\(\s*(?P<q>["\']?)/static/(?P<rest>[^)"\']*)(?P=q)\s*\)')
_JS_STATIC_LITERAL_RE = re.compile(r"(?P<q>['\"])/static/")

#: 残留检查：资源引用里出现绝对 /static/。注释里的说明文字不算。
_RESIDUAL_PATTERNS = (
    re.compile(r'(?:href|src|data-three|data-logo)=["\']/static/', re.IGNORECASE),
    re.compile(r'url\(\s*["\']?/static/'),
    re.compile(r"\|\|\s*['\"]/static/"),
)


class TransformError(RuntimeError):
    """转换规则未覆盖到某处绝对路径。"""


def transform_html(html: str) -> str:
    """改写 index.html。"""
    out = _HTML_ATTR_RE.sub(lambda m: f"{m.group('attr')}={m.group('q')}./static/", html)
    head = re.search(r"<head[^>]*>", out, re.IGNORECASE)
    if not head:
        raise TransformError("index.html has no <head>")
    inject = f"\n    {BRIDGE_SDK_TAG}\n    {SHIM_TAG}\n    {SHIM_CSS_TAG}\n"
    out = out[: head.end()] + inject + out[head.end():]
    if "</body>" not in out:
        raise TransformError("index.html has no </body>")
    out = out.replace("</body>", f"    {OVERRIDES_TAG}\n</body>", 1)
    return out


def transform_css(css: str, *, in_fonts_dir: bool) -> str:
    """改写 CSS 中的 url(/static/…)。

    Args:
        css: 原文。
        in_fonts_dir: 是否位于 ``static/fonts/``（相对基准不同）。
    """

    def _repl(m: re.Match) -> str:
        rest = m.group("rest")
        q = m.group("q")
        if in_fonts_dir and rest.startswith("fonts/"):
            return f"url({q}./{rest[len('fonts/'):]}{q})"
        # css/ 与 fonts/ 都在 static/ 下一级，所以其他资源统一走 "../"。
        return f"url({q}../{rest}{q})"

    return _CSS_URL_RE.sub(_repl, css)


def transform_js(name: str, js: str) -> str:
    """改写个别 JS 文件中的绝对路径与中文文件名引用。"""
    if name == "app.intro.js":
        js = _JS_STATIC_LITERAL_RE.sub(lambda m: f"{m.group('q')}./static/", js)
    for old, new in RENAMES.items():
        js = js.replace(f"static/{old}", f"static/{new}")
    return js


def check_no_absolute_static(root: Path) -> list[str]:
    """返回仍包含绝对 /static/ 资源引用的位置列表。"""
    hits: list[str] = []
    for path in root.rglob("*"):
        if path.suffix.lower() not in {".html", ".css", ".js"} or not path.is_file():
            continue
        text = path.read_text("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in _RESIDUAL_PATTERNS):
                hits.append(f"{path.relative_to(root).as_posix()}:{lineno}: {line.strip()[:160]}")
    return hits


def transform_views(frontend_dir: Path, shim_dir: Path, out_dir: Path) -> Path:
    """生成插件视图目录。

    Args:
        frontend_dir: FishTool ``web/frontend``。
        shim_dir: ``integrations/astrbot/frontend_shim``。
        out_dir: 输出 ``pages/console``（会被清空）。

    Returns:
        Path: 输出目录。

    Raises:
        TransformError: 有绝对路径未被改写。
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    static_src = frontend_dir / "static"
    static_out = out_dir / "static"
    for src in sorted(static_src.rglob("*")):
        if not src.is_file() or src.name in SKIP_FILES:
            continue
        rel = src.relative_to(static_src)
        target = static_out / rel.parent / RENAMES.get(src.name, src.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = src.suffix.lower()
        if suffix == ".css":
            in_fonts = rel.parts[0] == "fonts"
            target.write_text(transform_css(src.read_text("utf-8"), in_fonts_dir=in_fonts), "utf-8")
        elif suffix == ".js" and rel.parts[0] in {"js", "intro"}:
            target.write_text(transform_js(src.name, src.read_text("utf-8")), "utf-8")
        else:
            shutil.copy2(src, target)

    index_src = frontend_dir / "templates" / "index.html"
    (out_dir / "index.html").write_text(transform_html(index_src.read_text("utf-8")), "utf-8")
    for name in ("bridge-shim.js", "shim.css", "astrbot-overrides.js"):
        shutil.copy2(shim_dir / name, out_dir / name)

    residual = check_no_absolute_static(out_dir)
    if residual:
        raise TransformError("absolute /static references remain:\n" + "\n".join(residual))
    return out_dir
