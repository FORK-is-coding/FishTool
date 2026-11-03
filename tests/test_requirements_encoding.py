"""requirements 文件在中文 Windows 上能被旧版 pip 读取。

pip < 25.0（Python 3.11 官方安装包自带 pip 24.0）读取 ``-r`` 文件时：先看 BOM，
再看前两行里的 PEP 263 编码声明，都没有就按 locale 编码（中文 Windows 为 GBK）解码。
UTF-8 中文注释在 GBK 下解码失败，``pip install -r requirements.txt`` 直接报
UnicodeDecodeError。GitHub 的 Windows runner 代码页是 cp1252，复现不了 GBK，
这里按 pip 24.0 的同一规则做静态检查。
"""
from __future__ import annotations

import codecs
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# 与 pip/_internal/utils/encoding.py（24.0）中的 ENCODING_RE 相同
CODING_RE = re.compile(rb"coding[:=]\s*([-\w.]+)")


def _requirement_files() -> list[Path]:
    """仓库里所有会被 pip -r 读取的 requirements 文件（排除虚拟环境与构建产物）。"""
    skip = {".git", ".venv", "venv", "build", "dist", "node_modules"}
    return sorted(
        p for p in ROOT.rglob("*requirements*.txt")
        if not any(part in skip for part in p.relative_to(ROOT).parts)
    )


def _pip24_encoding(data: bytes) -> str | None:
    """返回 pip 24.0 会用的显式编码；没有 BOM 和声明时返回 None（即回退 locale）。"""
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8"
    for line in data.split(b"\n")[:2]:
        match = CODING_RE.search(line) if line[:1] == b"#" else None
        if match:
            return match.group(1).decode("ascii")
    return None


def test_root_requirements_is_checked() -> None:
    """根 requirements.txt 必须在检查范围内。"""
    assert ROOT / "requirements.txt" in _requirement_files()


@pytest.mark.parametrize("path", _requirement_files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_requirements_decodable_without_utf8_locale(path: Path) -> None:
    """含非 ASCII 内容的 requirements 文件必须在前两行声明 utf-8。"""
    data = path.read_bytes()
    if data.isascii():
        return
    encoding = _pip24_encoding(data)
    assert encoding is not None, (
        f"{path.relative_to(ROOT)} 含非 ASCII 字符但没有编码声明；"
        "在第一行加上 '# -*- coding: utf-8 -*-'"
    )
    assert codecs.lookup(encoding).name == "utf-8"
    data.decode(encoding)
