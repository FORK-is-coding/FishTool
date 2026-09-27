"""AST-backed comment-rate verifier for Python source files.

The denominator is the number of executable AST statement line numbers.
A statement is considered commented when its line or the immediately preceding
non-empty line contains a real ``#`` token. Docstrings are deliberately
excluded from the numerator, so documentation cannot inflate the result.
"""
from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {".git", "__pycache__", "build", "dist", "backups", "archive"}
NON_PRODUCTION_NAMES = {"__init__.py"}
NON_PRODUCTION_PREFIXES = ("test_", "verify_")


def comment_lines(source: str) -> set[int]:
    """Return physical line numbers containing real hash comments."""
    result: set[int] = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                result.add(token.start[0])
    except (IndentationError, tokenize.TokenError):
        return result
    return result


def statement_lines(tree: ast.AST) -> set[int]:
    """Return AST statement lines, excluding docstring expression nodes."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or not getattr(node, "lineno", None):
            continue
        if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            if isinstance(node.value.value, str):
                continue
        lines.add(node.lineno)
    return lines


def measure(path: Path) -> tuple[int, int, float]:
    """Measure one Python file and return total, commented, and percentage."""
    source = path.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(source, filename=str(path))
    statements = statement_lines(tree)
    comments = comment_lines(source)
    covered = {line for line in statements if line in comments or line - 1 in comments}
    ratio = len(covered) / len(statements) if statements else 1.0
    return len(statements), len(covered), ratio


def source_files() -> list[Path]:
    """Collect production Python sources while excluding generated, archived, and test artifacts."""
    return sorted(
        path
        for path in ROOT.rglob("*.py")
        if not any(part in EXCLUDED_DIRS for part in path.parts)
        and path.name not in NON_PRODUCTION_NAMES
        and not path.name.startswith(NON_PRODUCTION_PREFIXES)
    )


def test_ast_comment_rates() -> None:
    """Print every file's AST rate and fail when any source is below 50%."""
    failures: list[str] = []
    report_lines = ["AST comment-rate report (docstrings excluded)"]
    for path in source_files():
        total, covered, ratio = measure(path)
        relative = path.relative_to(ROOT)
        line = f"{ratio:.2%} ({covered}/{total}) {relative}"
        print(line)
        report_lines.append(line)
        if ratio < 0.50:
            failures.append(f"{relative}: {ratio:.2%}")
    (ROOT / "evidence" / "ast_comment_rate_report.txt").write_text(
        "\n".join(report_lines) + "\n", encoding="utf-8"
    )
    assert not failures, "AST comment-rate red zone: " + "; ".join(failures)
