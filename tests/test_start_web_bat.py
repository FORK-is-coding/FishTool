"""一键启动脚本 ``start_web.bat`` 的静态契约。

cmd 对批处理文件的编码和换行很敏感，这里锁住几条一旦违反就会在用户机器上直接坏掉的约束：

- 必须 CRLF 换行：LF 下 ``goto`` / ``call :label`` 会定位错行；
- 不能带 UTF-8 BOM：BOM 会粘在第一行 ``@echo off`` 前面，变成“不是内部或外部命令”；
- ``chcp 65001`` 之前、以及所有 ``rem`` 注释必须是纯 ASCII：
  cmd 按当前代码页逐行解码，非 ASCII 注释会被切碎后当命令执行；
- 必须经统一入口 ``app.py web`` 启动，并且只监听 127.0.0.1（issue #5）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAT = ROOT / "start_web.bat"


def _raw() -> bytes:
    """读取脚本原始字节（不做换行转换）。"""
    return BAT.read_bytes()


def _lines() -> list[str]:
    """按 CRLF 拆分后的文本行。"""
    return _raw().decode("utf-8").split("\r\n")


def test_bat_uses_crlf_without_bom() -> None:
    """只用 CRLF 换行，且不带 UTF-8 BOM。"""
    raw = _raw()
    assert not raw.startswith(b"\xef\xbb\xbf"), "start_web.bat 不能带 UTF-8 BOM"
    assert re.search(rb"(?<!\r)\n", raw) is None, "start_web.bat 存在 LF 换行，必须全部为 CRLF"
    assert raw.endswith(b"\r\n"), "start_web.bat 末行需以 CRLF 结尾"


def test_gitattributes_pins_bat_to_crlf() -> None:
    """.gitattributes 固定 *.bat 检出为 CRLF，避免 autocrlf=false 的机器拿到 LF。"""
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\.bat\s+.*eol=crlf", text, re.M), ".gitattributes 缺少 *.bat eol=crlf"


def test_non_ascii_only_after_utf8_codepage() -> None:
    """chcp 65001 之前全是 ASCII；所有 rem 注释都是 ASCII。"""
    lines = _lines()
    chcp_index = next(i for i, line in enumerate(lines) if "chcp 65001" in line)
    for no, line in enumerate(lines[:chcp_index], start=1):
        assert line.isascii(), f"第 {no} 行在 chcp 65001 之前出现非 ASCII 字符"
    for no, line in enumerate(lines, start=1):
        if line.lstrip().lower().startswith("rem"):
            assert line.isascii(), f"第 {no} 行 rem 注释含非 ASCII 字符"


def test_bat_starts_web_via_unified_entry_on_loopback() -> None:
    """经 app.py web 启动，显式只监听 127.0.0.1。"""
    text = _raw().decode("utf-8")
    assert re.search(r"app\.py web --host 127\.0\.0\.1 --port %PORT%", text)
    assert "web/main.py" not in text and "web\\main.py" not in text


def test_probe_imports_cover_pillow() -> None:
    """依赖探测要 import PIL：只有 qrcode 没有 Pillow 的 Python 不能算“依赖齐全”。"""
    text = _raw().decode("utf-8")
    probe = next(line for line in text.split("\r\n") if "import fastapi" in line)
    modules = {m.strip() for m in probe.split("import", 1)[1].split('"', 1)[0].split(",")}
    assert {"pydantic", "qrcode", "PIL"} <= modules


def test_bat_runs_pip_check_after_install() -> None:
    """自动安装后执行 pip check，提示旧环境残留的版本冲突。"""
    text = _raw().decode("utf-8")
    install = text.index("-m pip install -r requirements.txt")
    assert "-m pip check" in text[install:]


def test_bat_waits_for_health_before_opening_browser() -> None:
    """浏览器在 /health 返回 ok 之后才打开，并且可以用环境变量关掉。"""
    text = _raw().decode("utf-8")
    assert "/health" in text
    assert "FISHTOOL_NO_BROWSER" in text
