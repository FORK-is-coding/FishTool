"""校验发布树：结构、metadata、禁止文件、体积。

用法::

    python integrations/astrbot/release_tools/verify_package.py dist/plugin --max-zip-mb 16
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from pathlib import Path

REQUIRED_FILES = (
    "main.py",
    "README.md",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "logo.png",
    "engine_requirements.txt",
    "engine/VERSION",
    "engine/fishtool/web/main.py",
    "engine/fishtool/core/config.py",
    "engine/fishtool/config/config.yaml",
    "engine/fishtool/config/budget.yaml",
    "engine_launcher/launcher.py",
    "fishtool_adapter/__init__.py",
    "views/console/index.html",
    "views/console/bridge-shim.js",
    "views/console/astrbot-overrides.js",
    ".astrbot-plugin/i18n/zh-CN.json",
    ".astrbot-plugin/i18n/en-US.json",
)
REQUIRED_META = ("name", "desc", "version", "author")
ALLOWED_REPOS = {
    "https://github.com/FORK-is-coding/astrbot_plugin_fishtool",
    "https://github.com/FORK-is-coding/FishTool/tree/astrbot",
}
FORBIDDEN_NAMES = {".key", ".secrets", "user_config.yaml", "bili_ops.db", ".env"}
FORBIDDEN_DIRS = {"__pycache__", "tests", "desktop", "docs", "evidence", ".git", "venv", ".venv"}
FORBIDDEN_SUFFIX = {".db", ".sqlite", ".sqlite3", ".log", ".pyc", ".pem"}


def _parse_metadata(text: str) -> dict:
    """极简 YAML 顶层键解析（发布机可能没有 PyYAML）。"""
    meta: dict = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return meta


def zip_size(root: Path) -> int:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(root).as_posix())
    return buf.tell()


def verify(root: Path, max_zip_mb: float = 16.0) -> list[str]:
    """返回问题列表；空列表表示通过。"""
    errors: list[str] = []
    for rel in REQUIRED_FILES:
        if not (root / rel).is_file():
            errors.append(f"missing: {rel}")
    meta_path = root / "metadata.yaml"
    if meta_path.is_file():
        meta = _parse_metadata(meta_path.read_text("utf-8"))
        for key in REQUIRED_META:
            if not meta.get(key):
                errors.append(f"metadata.{key} empty")
        if meta.get("name") != "astrbot_plugin_fishtool":
            errors.append(f"metadata.name must be astrbot_plugin_fishtool, got {meta.get('name')!r}")
        if not re.fullmatch(r"\d+\.\d+\.\d+([.-][0-9A-Za-z.]+)?", meta.get("version", "")):
            errors.append(f"metadata.version invalid: {meta.get('version')!r}")
        # 插件市场只解析仓库默认分支，所以 repo 指向独立插件仓库（其默认分支就是插件根）。
        if meta.get("repo", "").rstrip("/") not in ALLOWED_REPOS:
            errors.append(f"metadata.repo must be one of {sorted(ALLOWED_REPOS)}, got {meta.get('repo')!r}")
    try:
        json.loads((root / "_conf_schema.json").read_text("utf-8"))
        for loc in ("zh-CN", "en-US"):
            json.loads((root / ".astrbot-plugin" / "i18n" / f"{loc}.json").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"invalid json: {exc}")
    reqs = (root / "requirements.txt").read_text("utf-8") if (root / "requirements.txt").is_file() else ""
    if any(line.strip() and not line.strip().startswith("#") for line in reqs.splitlines()):
        errors.append("requirements.txt must stay empty: engine deps belong in engine_requirements.txt")
    eng_text = (root / "engine_requirements.txt").read_text("utf-8") if (root / "engine_requirements.txt").is_file() else ""
    eng = "\n".join(l for l in eng_text.lower().splitlines() if l.strip() and not l.strip().startswith("#"))
    if "pyside6" in eng:
        errors.append("engine_requirements.txt must not contain PySide6")
    if "uvicorn[standard]" not in eng:
        errors.append("engine_requirements.txt must keep uvicorn[standard] (engine /ws needs websockets)")

    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in FORBIDDEN_DIRS for part in rel.parts[:-1]) or (path.is_dir() and path.name in FORBIDDEN_DIRS):
            errors.append(f"forbidden dir: {rel.as_posix()}")
            continue
        if path.is_file() and (path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIX):
            errors.append(f"forbidden file: {rel.as_posix()}")
        if path.is_file() and not rel.as_posix().isascii() and rel.parts[0] == "views":
            errors.append(f"non-ascii view asset name: {rel.as_posix()}")

    size = zip_size(root)
    if size > max_zip_mb * 1024 * 1024:
        errors.append(f"zip size {size / 1024 / 1024:.2f} MB exceeds {max_zip_mb} MB")
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="verify AstrBot plugin package")
    parser.add_argument("root")
    parser.add_argument("--max-zip-mb", type=float, default=16.0)
    args = parser.parse_args(argv)
    root = Path(args.root)
    errors = verify(root, args.max_zip_mb)
    size = zip_size(root)
    files = sum(1 for p in root.rglob("*") if p.is_file())
    print(f"package: {root} files={files} zip={size / 1024 / 1024:.2f}MB")
    for err in errors:
        print(f"ERROR {err}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
