"""requirements.txt 锁住容易漂移的传递依赖，且与直接依赖的约束一致。

背景：requirements.txt 原先只锁直接依赖。旧环境里残留的新版 pydantic / starlette，
或用 ``--no-deps`` 单独升级过某个包，都会得到不匹配的组合，例如
``SystemError: The installed pydantic-core version (2.14.5) is incompatible with the
current pydantic version``。显式锁住后，``pip install -r requirements.txt`` 会把它们拉回一致。
"""
from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQ = ROOT / "requirements.txt"


def _pins() -> dict[str, str]:
    """解析 ``name==version`` 行，name 按 PEP 503 规范化。"""
    pins = {}
    for raw in REQ.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)(\[[^\]]*\])?==([^\s;]+)", line)
        if match:
            pins[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(3)
    return pins


def test_transitive_pins_present() -> None:
    """pydantic-core 与 starlette 必须显式锁定。"""
    pins = _pins()
    assert "pydantic-core" in pins
    assert "starlette" in pins


@pytest.mark.parametrize(
    ("parent", "child"),
    [("pydantic", "pydantic-core"), ("fastapi", "starlette")],
)
def test_transitive_pin_matches_parent_requirement(parent: str, child: str) -> None:
    """锁定的子依赖版本满足父依赖（同一锁定版本）的 Requires-Dist。

    读已安装包的元数据；当前环境装的父包版本与锁定不同（本地开发环境）时跳过。
    """
    requirements = pytest.importorskip("packaging.requirements")
    pins = _pins()
    try:
        installed = metadata.version(parent)
    except metadata.PackageNotFoundError:
        pytest.skip(f"{parent} 未安装")
    if installed != pins[parent]:
        pytest.skip(f"已安装 {parent} {installed}，与锁定 {pins[parent]} 不同")

    specs = [
        requirements.Requirement(r)
        for r in metadata.requires(parent) or []
        if re.sub(r"[-_.]+", "-", requirements.Requirement(r).name).lower() == child
        and requirements.Requirement(r).marker is None
    ]
    assert specs, f"{parent} {installed} 的元数据里没有 {child} 约束"
    for spec in specs:
        assert spec.specifier.contains(pins[child], prereleases=True), (
            f"{child}=={pins[child]} 不满足 {parent} {installed} 的约束 {spec.specifier}"
        )
