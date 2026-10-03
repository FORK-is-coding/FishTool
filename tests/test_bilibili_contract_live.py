"""第三批 i：真·B 站契约验证的隔离用例（默认 skip；全量 pytest 默认 0 真实请求）。

口径（对齐 3i 规格 §3.2 + §4）：
- live 用例必须带 ``live`` 标记且**默认跳过**：仅当环境变量
  ``BILIBILI_LIVE_CONTRACT=1`` 时执行；
- **默认全量路径不得发任何真实网络请求**：本文件其余用例只做纯计算 / 门禁断言，
  且用拦截 ``socket.socket.connect`` 的方式证明默认路径 0 次外部连接；
- 真请求的硬上限与“只留摘要”由 :mod:`tools.verify_bilibili_contract` 保证。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TOOL_PATH = PROJECT_ROOT / "tools" / "verify_bilibili_contract.py"

#: live 结果允许外露的摘要字段白名单（不得出现 Cookie / 完整响应 / 敏感头）。
_ALLOWED_RESULT_KEYS = {
    "name", "endpoint", "http_status", "status", "observed_s", "reason_code",
    "business_code", "envelope_keys", "data_keys", "list_len", "item_keys", "error_type",
}


def _load_tool():
    """以文件路径加载 tools 脚本（tools 非包，且被 pytest 排除收集）。"""
    spec = importlib.util.spec_from_file_location("verify_bilibili_contract", _TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


verifier = _load_tool()

_LIVE = verifier.live_enabled()


def test_contract_plan_is_low_limit():
    """计划本身必须低限额：总请求 <= 12、单端点 <= 3。"""
    summary = verifier.summarize_plan()
    assert summary["total_requests"] <= 12
    assert summary["max_per_endpoint"] <= 3
    assert summary["steps"] >= 1


def test_default_path_makes_zero_external_requests(monkeypatch):
    """默认路径（构计划 / 摘要 / 门禁）不得触发 ``_fetch`` 网络入口。"""
    calls = {"n": 0}

    async def _boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("默认路径不应发起网络请求")

    monkeypatch.setattr(verifier, "_fetch", _boom)
    assert verifier.live_enabled({}) is False
    summary = verifier.summarize_plan()
    assert summary["total_requests"] == summary["steps"]
    assert calls["n"] == 0


def test_no_socket_connect_on_default_paths(monkeypatch):
    """拦截 ``socket.socket.connect``：默认路径仍应“零连接”通过。"""
    import socket

    def _deny(*args, **kwargs):
        raise AssertionError("默认路径不应建立任何网络连接")

    monkeypatch.setattr(socket.socket, "connect", _deny)
    verifier.summarize_plan()
    assert verifier.live_enabled({}) is False


def test_live_gate_defaults_off(monkeypatch):
    """未设置环境变量时 live 门禁为关（默认跳过真请求）。"""
    monkeypatch.delenv("BILIBILI_LIVE_CONTRACT", raising=False)
    assert verifier.live_enabled() is False


@pytest.mark.live
@pytest.mark.skipif(not _LIVE, reason="需显式 BILIBILI_LIVE_CONTRACT=1 才执行真请求")
def test_live_contract_low_limit():
    """真·B 站低限额契约验证（默认跳过；须显式授权后才跑）。"""
    summary = asyncio.run(verifier.run_live(timeout=10))
    assert summary["request_count"] <= 12
    per_endpoint: dict = {}
    for row in summary["results"]:
        per_endpoint[row["endpoint"]] = per_endpoint.get(row["endpoint"], 0) + 1
    assert all(count <= 3 for count in per_endpoint.values())
    # 只留摘要：结果字段必须落在白名单内。
    for row in summary["results"]:
        assert set(row) <= _ALLOWED_RESULT_KEYS, row
