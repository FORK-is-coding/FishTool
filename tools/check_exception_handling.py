"""静态检查异常处理规范。

用法::

    python tools/check_exception_handling.py          # 人类可读
    python tools/check_exception_handling.py --json   # 机器可读

检查规则（CI / pytest 共用）：
1. 禁止裸 ``except:``。
2. 捕获 ``Exception`` / ``BaseException`` 时，处理块必须至少做一件事：
   写日志（``logger.*``）或重新抛出（``raise``）。只返回兜底值而不记日志 = 违规。
3. 任何异常的 ``except`` 块不得只写 ``pass``（``asyncio.CancelledError`` /
   ``asyncio.TimeoutError`` 等控制流例外除外）。

为什么只查这两类：
- 窄异常（``except ValueError``）常用于解析兜底，返回值本身已表达状态；
- 宽异常最容易把未知错误吞掉，因此要求必须留下日志或向上抛出。
"""
from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List

#: 仓库根目录（本文件位于 tools/ 下）。
REPO_ROOT = Path(__file__).resolve().parent.parent

#: 不参与检查的目录：测试/工具自身/构建产物/缓存。
EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".tmp",
    "__pycache__",
    "archive",
    "backups",
    "build",
    "dist",
    "evidence",
    "node_modules",
    "tests",
    "tools",
    "venv",
}

#: 允许「只 pass/continue」的控制流异常（不是业务错误）。
CONTROL_FLOW_EXCEPTIONS = {
    "asyncio.CancelledError",
    "asyncio.TimeoutError",
    "CancelledError",
    "TimeoutError",
}


@dataclass(frozen=True)
class Violation:
    """一条违规记录。"""

    file: str
    line: int
    kind: str
    detail: str


def _iter_python_files(root: Path) -> Iterable[Path]:
    """遍历需要检查的 Python 文件。

    Args:
        root: 仓库根目录。

    Yields:
        Path: 未被排除的 .py 文件。
    """
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue
        yield path


def _handler_type_text(node: ast.ExceptHandler) -> str:
    """返回 except 子句的类型文本；裸 except 返回空串。

    Args:
        node: except 处理器节点。

    Returns:
        str: 例如 ``Exception`` / ``(ValueError, TypeError)``；裸 except 为空串。
    """
    if node.type is None:
        return ""
    try:
        return ast.unparse(node.type)
    except Exception:  # noqa: BLE001 - 反解析失败不应让检查器崩溃
        return "<unparseable>"


def _is_broad(type_text: str) -> bool:
    """判断是否宽泛异常捕获。

    Args:
        type_text: except 类型文本。

    Returns:
        bool: 是否包含 ``Exception`` / ``BaseException``。
    """
    return "Exception" in type_text or "BaseException" in type_text


def _has_logger_call(node: ast.AST) -> bool:
    """判断处理块内是否调用了 logger / 统一错误上报入口。

    Args:
        node: except 处理器。

    Returns:
        bool: 是否出现 ``*.debug/info/warning/error/exception/critical`` 调用，
        或 ``report_exception`` / ``log_low_priority`` / ``report_high_priority``。
    """
    log_methods = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
    log_functions = {"report_exception", "log_low_priority", "report_high_priority"}
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Attribute):
            if child.func.attr in log_methods:
                return True
        elif isinstance(child.func, ast.Name) and child.func.id in log_functions:
            return True
    return False


def _has_raise(node: ast.AST) -> bool:
    """判断处理块内是否重新抛出异常。

    Args:
        node: except 处理器。

    Returns:
        bool: 是否存在 ``raise``。
    """
    return any(isinstance(child, ast.Raise) for child in ast.walk(node))


def _only_pass(node: ast.ExceptHandler) -> bool:
    """判断处理块是否只有一个 ``pass``。

    Args:
        node: except 处理器。

    Returns:
        bool: 是否仅 ``pass``。
    """
    return len(node.body) == 1 and isinstance(node.body[0], ast.Pass)


def _is_control_flow(type_text: str) -> bool:
    """判断异常类型是否为允许静默的控制流异常。

    Args:
        type_text: except 类型文本。

    Returns:
        bool: 是否整段都属于控制流例外。
    """
    stripped = type_text.strip().strip("()")
    names = {item.strip() for item in stripped.split(",") if item.strip()}
    return bool(names) and names.issubset(CONTROL_FLOW_EXCEPTIONS)


def find_violations(root: Path = REPO_ROOT) -> List[Violation]:
    """扫描仓库，返回全部异常处理违规。

    Args:
        root: 仓库根目录。

    Returns:
        List[Violation]: 违规列表；空列表表示通过。
    """
    violations: List[Violation] = []
    for path in _iter_python_files(root):
        rel = str(path.relative_to(root))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            violations.append(Violation(rel, 0, "parse_error", repr(exc)))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            type_text = _handler_type_text(node)
            if node.type is None:
                violations.append(Violation(rel, node.lineno, "bare_except", "禁止裸 except:"))
                continue
            if _only_pass(node):
                if _is_control_flow(type_text):
                    continue
                violations.append(
                    Violation(
                        rel,
                        node.lineno,
                        "only_pass",
                        f"except {type_text}: 只写 pass，错误信息被吞掉",
                    )
                )
                continue
            if _is_broad(type_text):
                if not _has_logger_call(node) and not _has_raise(node):
                    violations.append(
                        Violation(
                            rel,
                            node.lineno,
                            "silent_broad_except",
                            f"except {type_text}: 既未写日志也未重新抛出",
                        )
                    )
    return violations


def main(argv: List[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv: 参数列表（默认取 sys.argv[1:]）。

    Returns:
        int: 0 通过；1 存在违规。
    """
    parser = argparse.ArgumentParser(description="检查 FishTool 异常处理规范")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--root", default=str(REPO_ROOT), help="仓库根目录")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    violations = find_violations(root)
    if args.json:
        print(json.dumps([asdict(item) for item in violations], ensure_ascii=False, indent=2))
    else:
        if not violations:
            print(f"OK：未发现异常处理违规（root={root}）")
        else:
            print(f"发现 {len(violations)} 处异常处理违规：")
            for item in violations:
                print(f"  {item.file}:{item.line} [{item.kind}] {item.detail}")
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
