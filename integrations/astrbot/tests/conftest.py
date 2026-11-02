"""适配层测试公共夹具。

- ``REPO_ROOT``：FishTool 仓库根（integrations/astrbot/tests 向上三级）。
- ``engine_python``：能运行 FishTool 引擎的解释器（已装引擎依赖）。优先读环境变量
  ``FISHTOOL_ENGINE_PYTHON``，否则用当前解释器；缺依赖时相关测试跳过。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ASTRBOT_DIR = REPO_ROOT / "integrations" / "astrbot"
PLUGIN_DIR = ASTRBOT_DIR / "plugin"

if str(ASTRBOT_DIR) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_DIR))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


@pytest.fixture(scope="session")
def engine_python() -> str:
    """返回装好 FishTool 引擎依赖的解释器路径；没有则跳过。"""
    candidate = os.environ.get("FISHTOOL_ENGINE_PYTHON") or sys.executable
    probe = subprocess.run(
        [candidate, "-c", "import fastapi, sqlalchemy, uvicorn, yaml, cryptography"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"engine deps not available in {candidate}: {probe.stderr.strip()[-200:]}")
    return candidate
