"""引擎启动环境变量：AI 伪人巡检的 AstrBot 标记与后台「允许代发举报」开关。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from fishtool_adapter import supervisor as sup_module
from fishtool_adapter.constants import engine_cfg


class _Proc:
    pid = 4321


def _layout(tmp_path: Path) -> SimpleNamespace:
    for name in ("logs", "runtime"):
        (tmp_path / name).mkdir()
    return SimpleNamespace(logs_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime", root=tmp_path,
                           engine_log=tmp_path / "logs" / "engine.log", pid_file=tmp_path / "engine.pid")


def _spawn_env(tmp_path, monkeypatch, raw_cfg) -> dict:
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs.get("env") or {})
        return _Proc()

    async def fake_healthy(self, timeout=90):
        return None

    monkeypatch.setattr(sup_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sup_module.EngineSupervisor, "_wait_healthy", fake_healthy)
    sup = sup_module.EngineSupervisor(_layout(tmp_path), tmp_path, engine_cfg(raw_cfg), logging.getLogger("test"))
    sup._python = "python"
    monkeypatch.setattr(sup_module.EngineSupervisor, "engine_root", property(lambda self: tmp_path), raising=False)
    asyncio.run(sup._spawn_and_wait())
    return captured


@pytest.mark.parametrize("raw,expected", [({}, "0"), ({"allow_proxy_report": False}, "0"),
                                          ({"allow_proxy_report": True}, "1"), ({"allow_proxy_report": "true"}, "1")])
def test_allow_proxy_report_env(tmp_path, monkeypatch, raw, expected):
    env = _spawn_env(tmp_path, monkeypatch, raw)
    assert env["FISHTOOL_EMBEDDED_BY"] == "astrbot"
    assert env["FISHTOOL_ASTRBOT_ALLOW_PROXY_REPORT"] == expected
