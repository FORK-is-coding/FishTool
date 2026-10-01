"""06 采集广度 · 低频联网冒烟（默认跳过，不作为常规单测）。

启用方式（二选一）：
    - 环境变量 ``FISHTOOL_DISCOVERY_SMOKE=1``；
    - 在仓库根创建标记文件 ``data/.discovery_smoke_enabled``。

用例只转发到 ``tools/discovery_smoke.py`` 的一次性实现：三入口各请求 1 次
（含礼貌间隔），断言 code==0、条数>0、必填字段非空、search/square 的
heat_score 全为正整数。真实低频调用，**不产生常规轮询**。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

_MARKER = Path(__file__).resolve().parents[1] / "data" / ".discovery_smoke_enabled"
_ENABLED = os.environ.get("FISHTOOL_DISCOVERY_SMOKE") == "1" or _MARKER.exists()

pytestmark = pytest.mark.skipif(
    not _ENABLED,
    reason="联网冒烟默认跳过；设置 FISHTOOL_DISCOVERY_SMOKE=1 或创建 data/.discovery_smoke_enabled 启用",
)


def test_low_frequency_smoke() -> None:
    """跑一次真实低频冒烟，退出码 0 即通过。"""
    from tools import discovery_smoke

    assert discovery_smoke.main() == 0
