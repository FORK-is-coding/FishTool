"""热点算法注册表的契约级测试。

覆盖 modules/hotspot/algorithm/registry.py 的全部公开函数：
- register：正常注册、覆盖注册、空名/不可调用工厂的异常分支
- create_detector：默认算法、自定义工厂传参、未知算法 KeyError
- list_algorithms：排序稳定性

所有用例通过 monkeypatch 把模块级 ``_REGISTRY`` 换成可写副本，
保证不污染全局注册表、不影响其它用例。
"""

from __future__ import annotations

import pytest

from modules.hotspot.algorithm import registry as registry_module
from modules.hotspot.algorithm.base import Detection, LifecycleDetector, Snapshot
from modules.hotspot.algorithm.heuristic_v1 import HeuristicV1
from modules.hotspot.algorithm.lifecycle_v2 import LifecycleV2


class _StubDetector(LifecycleDetector):
    """最小可实例化的算法契约实现，用于验证注册表工厂调用与传参。"""

    def __init__(self, tag: str = "stub") -> None:
        self.tag = tag

    def detect(self, snapshots: list[Snapshot]) -> list[Detection]:
        """桩实现：始终返回空检测结果。"""
        return []

    @property
    def version(self) -> str:
        """返回桩算法版本号。"""
        return "stub_v1"

    @property
    def config_schema(self) -> dict:
        """返回桩算法配置描述。"""
        return {"version": self.version}


@pytest.fixture()
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> dict:
    """为每个用例提供模块级注册表的可写副本。"""
    copy = dict(registry_module._REGISTRY)
    monkeypatch.setattr(registry_module, "_REGISTRY", copy)
    return copy


def test_module_registry_has_default_algorithm() -> None:
    """真实全局注册表默认应包含 heuristic_v1。"""
    assert registry_module._REGISTRY["heuristic_v1"] is HeuristicV1


def test_module_registry_registers_lifecycle_v2() -> None:
    """真实全局注册表应新增 lifecycle_v2 注册（路由 / 门面默认所需）。"""
    assert registry_module._REGISTRY["lifecycle_v2"] is LifecycleV2


def test_create_detector_can_build_lifecycle_v2() -> None:
    """create_detector("lifecycle_v2") 应可实例化且版本号正确。"""
    detector = registry_module.create_detector("lifecycle_v2")

    assert isinstance(detector, LifecycleV2)
    assert detector.version == "lifecycle_v2"


def test_register_stores_custom_factory(isolated_registry: dict) -> None:
    """合法工厂应被登记，并可被 create_detector 实例化。"""
    registry_module.register("stub", _StubDetector)

    assert isolated_registry["stub"] is _StubDetector
    detector = registry_module.create_detector("stub")
    assert isinstance(detector, _StubDetector)


def test_register_passes_kwargs_to_factory(isolated_registry: dict) -> None:
    """create_detector 的关键字参数应原样透传给工厂。"""
    registry_module.register("stub", _StubDetector)

    detector = registry_module.create_detector("stub", tag="hello")

    assert detector.tag == "hello"


def test_register_overwrites_duplicate_name(isolated_registry: dict) -> None:
    """同名注册应直接覆盖，以支持插件热替换。"""
    registry_module.register("heuristic_v1", _StubDetector)

    detector = registry_module.create_detector("heuristic_v1")

    assert isinstance(detector, _StubDetector)
    assert not isinstance(detector, HeuristicV1)


@pytest.mark.parametrize("bad_name", ["", None])
def test_register_rejects_empty_name(isolated_registry: dict, bad_name) -> None:
    """空名称应抛出 ValueError。"""
    with pytest.raises(ValueError):
        registry_module.register(bad_name, _StubDetector)


@pytest.mark.parametrize("bad_factory", [123, "not-callable", object()])
def test_register_rejects_non_callable_factory(isolated_registry: dict, bad_factory) -> None:
    """非可调用工厂应抛出 ValueError。"""
    with pytest.raises(ValueError):
        registry_module.register("bad", bad_factory)


def test_create_detector_default_is_heuristic_v1() -> None:
    """不传名称时应创建默认的 heuristic_v1。"""
    detector = registry_module.create_detector()

    assert isinstance(detector, HeuristicV1)
    assert detector.version == "heuristic_v1"


def test_create_detector_unknown_name_raises_keyerror() -> None:
    """未注册名称应抛出 KeyError 且含名称提示。"""
    with pytest.raises(KeyError) as excinfo:
        registry_module.create_detector("does_not_exist")

    assert "does_not_exist" in str(excinfo.value)


def test_list_algorithms_sorted_and_contains_default(isolated_registry: dict) -> None:
    """算法名称列表应按字典序排序并包含默认算法。"""
    registry_module.register("zzz_last", _StubDetector)
    registry_module.register("aaa_first", _StubDetector)

    names = registry_module.list_algorithms()

    assert names == sorted(names)
    assert "heuristic_v1" in names
    assert names[0] == "aaa_first"
    assert names[-1] == "zzz_last"
