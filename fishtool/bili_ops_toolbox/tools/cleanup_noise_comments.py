"""批量清理 Python 源码中的机械模板注释。

默认仅扫描并报告；传入 ``--apply`` 后才会写回文件。脚本只删除独占整行且
命中白名单规则的注释，不处理行尾注释、docstring、编码声明和有业务含义的说明。
"""
from __future__ import annotations

import argparse
import json
import re
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


EXCLUDED_PARTS = {
    ".git",
    ".codegraph",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "backups",
    "build",
    "dist",
}

# 这些短语来自仓库中机械批量生成的注释，只描述紧邻代码的字面动作。
NOISE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"执行处理",
        r"设置初始值/默认状态，避免后续空引用",
        r"写入配置/属性，影响后续行为",
        r"对数据进行加工/分发",
        r"返回处理结果",
        r"调用成员方法/属性",
        r"调用\s+[A-Za-z_][A-Za-z0-9_.]*\s+处理",
        r"记录运行日志",
        r"读取数据并赋值给当前作用域变量",
        r"验证状态/条件，决定下一步分支",
        r"持久化数据，防止丢失",
        r"将原始文本转为结构化数据",
        r"用新值覆盖旧值，保持数据一致",
        r"实例化对象并准备使用",
        r"将数据持久化到表中",
        r"初始化(?:字符串|容器|计数|列表|字典|集合|变量|状态|结果)\s+[A-Za-z_][A-Za-z0-9_]*",
        r"支撑(?:解析|网络|持久化|配置|数据|业务|请求|响应|日志|缓存)+流程",
    )
)


@dataclass(frozen=True)
class FileCleanup:
    """记录单个文件的清理结果。"""

    path: Path
    removed_lines: tuple[int, ...]
    removed_texts: tuple[str, ...]


def iter_python_files(root: Path) -> Iterable[Path]:
    """遍历仓库 Python 文件，跳过产物、缓存和备份目录。"""

    for path in sorted(root.rglob("*.py")):
        if not any(part in EXCLUDED_PARTS for part in path.relative_to(root).parts):
            yield path


def is_noise_comment(comment: str) -> bool:
    """判断注释正文是否属于明确的机械模板噪音。"""

    text = comment.removeprefix("#").strip()
    return any(pattern.fullmatch(text) for pattern in NOISE_PATTERNS)


def find_noise_lines(path: Path) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """使用 tokenize 查找独占整行的噪音注释，避免误判字符串内容。"""

    try:
        raw = path.read_bytes()
        encoding, _ = tokenize.detect_encoding(iter(raw.splitlines(keepends=True)).__next__)
        text = raw.decode(encoding)
        lines = text.splitlines(keepends=True)
        tokens = tokenize.generate_tokens(iter(lines).__next__)
        line_numbers: list[int] = []
        comments: list[str] = []
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            line_prefix = token.line[: token.start[1]]
            if line_prefix.strip() or not is_noise_comment(token.string):
                continue
            line_numbers.append(token.start[0])
            comments.append(token.string.strip())
        return tuple(line_numbers), tuple(comments)
    except (OSError, UnicodeError, SyntaxError, tokenize.TokenError) as exc:
        raise RuntimeError(f"无法分析 {path}: {exc}") from exc


def clean_file(path: Path, line_numbers: tuple[int, ...], apply: bool) -> None:
    """删除指定整行；apply 为假时保持文件不变。"""

    if not apply or not line_numbers:
        return
    try:
        raw = path.read_bytes()
        encoding, _ = tokenize.detect_encoding(iter(raw.splitlines(keepends=True)).__next__)
        text = raw.decode(encoding)
        remove_set = set(line_numbers)
        cleaned = "".join(
            line for number, line in enumerate(text.splitlines(keepends=True), 1)
            if number not in remove_set
        )
        path.write_text(cleaned, encoding=encoding, newline="")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"无法写回 {path}: {exc}") from exc


def run(root: Path, apply: bool) -> dict[str, object]:
    """扫描并按需清理仓库，返回可写入报告的结构化统计。"""

    results: list[FileCleanup] = []
    for path in iter_python_files(root):
        line_numbers, comments = find_noise_lines(path)
        if not line_numbers:
            continue
        clean_file(path, line_numbers, apply)
        results.append(FileCleanup(path, line_numbers, comments))

    phrase_counts = Counter(text for result in results for text in result.removed_texts)
    return {
        "mode": "apply" if apply else "dry-run",
        "removed_total": sum(len(result.removed_lines) for result in results),
        "files": {
            str(result.path.relative_to(root)): len(result.removed_lines)
            for result in results
        },
        "phrases": dict(phrase_counts.most_common()),
    }


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true", help="实际写回清理结果")
    return parser.parse_args()


def main() -> int:
    """执行扫描或清理，并输出 JSON 统计。"""

    args = parse_args()
    try:
        result = run(args.root.resolve(), args.apply)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
