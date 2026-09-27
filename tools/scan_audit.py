"""全量源码静态扫描：安全漏洞与健壮性隐患。

用法：
    python tools/scan_audit.py

只读扫描，不修改任何源码。按严重级别输出命中位置，
方便人工复核后再决定是否整改。
"""
from __future__ import annotations

import pathlib
import re
import sys
from typing import Iterator

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_PARTS = {
    ".git", "__pycache__", "node_modules", ".codegraph", "build", "dist",
    ".venv", "venv", "tools", ".pytest_cache", "evidence", "data", "logs",
}
SCAN_EXTS = {".py", ".js", ".html"}

# (级别, 编号, 正则, 说明)
LINE_RULES: list[tuple[str, str, str, str]] = [
    # ---- 高危：代码执行与反序列化 ----
    ("HIGH", "S001", r"\beval\s*\(", "eval 可执行任意代码"),
    ("HIGH", "S002", r"\bexec\s*\(", "exec 可执行任意代码"),
    ("HIGH", "S003", r"pickle\.loads?\s*\(", "pickle 反序列化不可信数据"),
    ("HIGH", "S004", r"yaml\.load\s*\((?![^)\n]*Safe)", "yaml.load 未指定 SafeLoader"),
    ("HIGH", "S005", r"os\.system\s*\(", "os.system 存在命令注入风险"),
    ("HIGH", "S006", r"subprocess\.[a-zA-Z_]+\([^)\n]*shell\s*=\s*True", "shell=True 命令注入"),
    ("HIGH", "S007", r"verify\s*=\s*False", "关闭 TLS 证书校验"),
    ("HIGH", "S008",
     r"(password|passwd|secret|api_key|apikey|access_token|auth_token)\s*=\s*[\"'][^\"']{6,}[\"']",
     "疑似硬编码凭证"),
    # ---- 中危：注入与异常处理 ----
    ("MED", "S010", r"\.execute\s*\(\s*f[\"']", "SQL 使用 f-string 拼接"),
    ("MED", "S011", r"\.execute\s*\([^)\n]*%\s*\(", "SQL 使用 % 拼接"),
    ("MED", "S012", r"\.execute\s*\([^)\n]*\.format\s*\(", "SQL 使用 format 拼接"),
    ("MED", "S013", r"\.execute\s*\([^)\n]*\+\s*", "SQL 使用字符串相加"),
    ("MED", "S014", r"^\s*except\s*:", "裸 except 吞掉全部异常"),
    ("MED", "S015", r"except[^:\n]*:\s*pass\s*$", "静默吞异常"),
    ("MED", "S016", r"(?<![=!<>])==\s*None", "应使用 is None"),
    ("MED", "S017", r"\.get\s*\([^)]*\)\s*or\s*0", "缺值退化为 0，需确认是否会把缺失当真实零"),
    # ---- 低危：代码卫生 ----
    ("LOW", "S020", r"def\s+\w+\([^)\n]*=\s*(\[\]|\{\})\s*[,)]", "可变默认参数"),
    ("LOW", "S021", r"^\s*print\s*\(", "残留 print"),
    ("LOW", "S022", r"#\s*(TODO|FIXME|XXX)\b", "遗留待办"),
    ("LOW", "S023", r"\bsys\.exit\s*\(", "直接退出进程"),
    # ---- 前端 ----
    ("MED", "S030", r"innerHTML\s*=(?!=)", "innerHTML 赋值，需确认已转义"),
    ("MED", "S031", r"insertAdjacentHTML\s*\(", "insertAdjacentHTML，需确认已转义"),
    ("MED", "S032", r"document\.write\s*\(", "document.write"),
    ("LOW", "S033", r"localStorage\.setItem\s*\(", "localStorage 写入，确认无敏感信息"),
]


def iter_files() -> Iterator[pathlib.Path]:
    """遍历需要扫描的源码文件。"""
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SCAN_EXTS:
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        yield path


def main() -> int:
    """执行扫描并输出分组结果。"""
    findings: dict[str, list[tuple[str, int, str, str]]] = {}
    compiled = [(lvl, code, re.compile(pat), desc) for lvl, code, pat, desc in LINE_RULES]
    files_scanned = 0
    for path in iter_files():
        files_scanned += 1
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        rel = str(path.relative_to(ROOT))
        for lineno, line in enumerate(lines, start=1):
            for lvl, code, rx, desc in compiled:
                if rx.search(line):
                    findings.setdefault(lvl, []).append((code, lineno, rel, line.strip()[:150]))

    print(f"扫描文件数: {files_scanned}")
    total = sum(len(v) for v in findings.values())
    print(f"命中总数: {total}")
    for lvl in ("HIGH", "MED", "LOW"):
        items = findings.get(lvl, [])
        print()
        print(f"===== {lvl} =====", f"({len(items)} 条)")
        for code, lineno, rel, snippet in items:
            print(f"[{code}] {rel}:{lineno}")
            print(f"        {snippet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
