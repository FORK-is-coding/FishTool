# -*- coding: utf-8 -*-
"""FishTool 源码统计脚本

统计工作副本的代码文件数与行数，按扩展名分组输出。

排除规则：
- 路径带「源码备份」的目录一律排除（E:\\fishtool源码备份*）
- backups / __pycache__ / venv / node_modules / build / dist / .git
- 只统计 .py / .js / .css / .html

用法：
    python tools/code_stats.py [根目录]
    不传根目录时默认统计 D:\\tasks\\cola\\bili_ops_toolbox
"""
import os
import sys

EXTENSIONS = {".py", ".js", ".css", ".html"}

EXCLUDE_DIR_NAMES = {
    "backups",
    "__pycache__",
    "venv",
    "node_modules",
    "build",
    "dist",
    ".git",
    ".idea",
    ".vscode",
}

# 路径片段命中即整体跳过（字符串包含匹配，兼容大小写）
EXCLUDE_PATH_PARTS = [
    "源码备份",
]


def is_excluded(rel_parts):
    """按相对路径的各层级目录名判断是否排除"""
    for part in rel_parts:
        if part in EXCLUDE_DIR_NAMES:
            return True
        for kw in EXCLUDE_PATH_PARTS:
            if kw in part:
                return True
    return False


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else r"D:\tasks\cola\bili_ops_toolbox"
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print(f"[ERROR] 根目录不存在: {root}")
        return 1

    stats = {ext: {"files": 0, "lines": 0} for ext in EXTENSIONS}
    total_files = 0
    total_lines = 0

    for dirpath, dirnames, filenames in os.walk(root):
        # 相对根目录的层级
        rel = os.path.relpath(dirpath, root)
        rel_parts = [] if rel == "." else rel.split(os.sep)

        # 过滤子目录：命中排除规则的不再进入
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDE_DIR_NAMES
            and not any(kw in d for kw in EXCLUDE_PATH_PARTS)
        ]

        # 当前目录本身是否排除（防止根目录恰好命中）
        if is_excluded(rel_parts):
            continue

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in EXTENSIONS:
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    n = sum(1 for _ in f)
            except OSError as e:
                print(f"[WARN] 跳过 {fpath}: {e}")
                continue
            stats[ext]["files"] += 1
            stats[ext]["lines"] += n
            total_files += 1
            total_lines += n

    print(f"根目录: {root}")
    print(f"{'扩展名':<8}{'文件数':>8}{'行数':>10}")
    print("-" * 30)
    for ext in sorted(EXTENSIONS):
        s = stats[ext]
        print(f"{ext:<8}{s['files']:>8}{s['lines']:>10}")
    print("-" * 30)
    print(f"{'合计':<8}{total_files:>8}{total_lines:>10}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
