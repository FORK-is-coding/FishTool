"""parse_count 返回值契约：永远返回 ``(value, status)`` 二元组，且分类稳定。

背景（为什么单独立这个文件）：
    外部评审看的是 GitHub 网页折叠版，误判 ``parse_count(1.0)`` 会返回**裸 None**
    （从而 ``value, status = parse_count(...)`` 解包、或 ``parse_count(...)[1]``
    取下标会炸）。实际实现末尾是 ``return None, 'invalid'``，返回的是**元组**。
    本文件把「无论走哪条分支，返回值都是 tuple」这一契约逐条钉死，防止以后
    有人为了方便把它改回裸 ``None`` 而无人发现。

被测对象：
    ``core/data_quality.parse_count``

覆盖输入（对齐其 docstring 的三态分类）：
    - ``None``                        -> ``(None, 'missing')``
    - ``True`` / ``False``            -> ``(None, 'invalid')``   # bool 先于 int 拦截
    - ``-1``                          -> ``(None, 'invalid')``
    - ``1.0`` / ``1.5`` / ``0.0`` / ``nan`` -> ``(None, 'invalid')``
    - ``"12"`` / ``" 12 "``           -> ``(12, 'ok')``
    - ``"abc"`` / ``"1.5"`` / ``""`` / ``[]`` / ``{}`` / ``object()`` -> ``(None, 'invalid')``

关键断言：**每个用例都断言返回值是 tuple（不是裸 None）**，这正是本次评审误判点。
"""
from __future__ import annotations

from typing import Any

import pytest

from core.data_quality import parse_count


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 缺失
        (None, (None, "missing")),
        # bool 是 int 子类，必须在 type(raw) is int 之前拦截
        (True, (None, "invalid")),
        (False, (None, "invalid")),
        # 负整数非法
        (-1, (None, "invalid")),
        # float 一律非法（含边界 1.0 / 0.0 与 NaN）
        (1.0, (None, "invalid")),
        (1.5, (None, "invalid")),
        (0.0, (None, "invalid")),
        (float("nan"), (None, "invalid")),
        # 纯数字字符串（允许首尾空白）合法
        ("12", (12, "ok")),
        (" 12 ", (12, "ok")),
        # 含非数字字符 / 空串 / 非 str 非 int 容器对象
        ("abc", (None, "invalid")),
        ("1.5", (None, "invalid")),
        ("", (None, "invalid")),
        ([], (None, "invalid")),
        ({}, (None, "invalid")),
        (object(), (None, "invalid")),
    ],
)
def test_parse_count_always_returns_tuple(raw: Any, expected: tuple) -> None:
    """任何输入下，parse_count 都必须返回 (value, status) 二元组，绝不是裸 None。"""
    result = parse_count(raw)

    # ① 钉死评审误判点：返回值必须是 tuple。
    assert isinstance(result, tuple), f"parse_count({raw!r}) 返回了非 tuple：{result!r}"
    assert len(result) == 2, f"parse_count({raw!r}) 元组长度不是 2：{result!r}"

    # ② 分类必须与契约一致，且缺失/非法时 value 必须是 None。
    assert result == expected, f"parse_count({raw!r}) = {result!r}，期望 {expected!r}"

    # ③ 显式证明「可解包、可按下标取 status」都不会抛异常（评审担忧的用法）。
    value, status = result
    assert (value, status) == expected
    assert result[1] == expected[1]
