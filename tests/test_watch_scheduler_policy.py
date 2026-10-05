"""watch 逻辑调度策略（``config/budget.yaml`` 的 ``watch_scheduler`` 段）加载与真预算联测。

07 执行案 §10.1 / §10.2 / §13 W2：

- ``risk_control.load_watch_scheduler_policy`` 解析 ``partitioned`` / ``shared_only``；
- ``partitioned``：类别窗与总窗**并行校验**，任一不足即拒绝（真 ``RequestBudget``）；
- ``shared_only`` / 缺段：**显式** ``category_isolation=False``，绝不默认静默降级；
- 有本段但非法：抛 ``WatchSchedulerConfigError``，绝不悄悄回 shared。

口径：不触网、不读用户 data/secrets；临时 YAML 一律写 ``tmp_path``，真 ``RequestBudget`` 固定时钟。
"""
from __future__ import annotations

import pytest

from modules.hotspot import risk_control as rc
from modules.hotspot.risk_control import (
    WATCH_SCHEDULER_MODE_PARTITIONED,
    WATCH_SCHEDULER_MODE_SHARED_ONLY,
    RequestBudget,
    WatchSchedulerConfigError,
    load_watch_scheduler_policy,
)

#: 固定单调时钟读数（只喂 RequestBudget，绝不落库）。
NOW_MONO: float = 3000.0

#: 一份最小可加载的账本骨架（只有 quota 段，用于拼缺段 / 非法段用例）。
_BASE_HEAD = """\
version: 1
quota:
  global_limit: 10
  window_hours: 24
  retention_hours: 25
  categories:
    discovery:
      domain: no_cookie
      limit: 10
"""


def _write_yaml(tmp_path, text: str):
    """把文本写成临时 ``budget.yaml``，返回其 Path。"""
    path = tmp_path / "budget.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _partitioned_section(*, normal, fast, total) -> str:
    """拼一段合法 partitioned 段（``normal`` / ``fast`` / ``total`` 各为 (pm, ph, pd)）。"""

    def _limits(triple) -> str:
        pm, ph, pd = triple
        return f"      per_minute: {pm}\n      per_hour: {ph}\n      per_day: {pd}\n"

    return (
        "watch_scheduler:\n"
        "  mode: partitioned\n"
        "  total:\n"
        f"    per_minute: {total[0]}\n    per_hour: {total[1]}\n    per_day: {total[2]}\n"
        "  category_limits:\n"
        "    normal_watch:\n" + _limits(normal) +
        "    fast_watch:\n" + _limits(fast) +
        "  fair_order:\n    - normal_watch\n    - fast_watch\n"
    )


# ===========================================================================
# 交付配置（真实仓库 config/budget.yaml）
# ===========================================================================


def test_real_repo_config_is_partitioned_and_self_consistent() -> None:
    """交付配置为 partitioned：类别窗每窗口合计 <= total，且提案数字明确标 pending。"""
    policy = load_watch_scheduler_policy()
    assert policy.mode == WATCH_SCHEDULER_MODE_PARTITIONED
    assert policy.category_isolation is True
    assert set(policy.fair_order) == {"normal_watch", "fast_watch"}
    assert policy.category_limits is not None
    assert {"normal_watch", "fast_watch"} <= set(policy.category_limits)
    # 每窗口「各类上限之和 <= 总窗上限」（分桶自洽的前提）。
    for index, total in enumerate((policy.per_minute, policy.per_hour, policy.per_day)):
        assert sum(spec[index][1] for spec in policy.category_limits.values()) <= total

    # 提案数字必须逐处标「待维护者确认」，不得被当既有常量。
    raw = rc._watch_scheduler_config_path().read_text(encoding="utf-8")
    assert "pending_maintainer_approval" in raw
    assert "待维护者确认" in raw

    # 真预算联测：策略构造出的 budget 类别隔离已生效，且版本标签可观测。
    budget = policy.build_budget()
    snapshot = budget.snapshot(NOW_MONO)
    assert snapshot["category_isolation"] is True
    assert snapshot["mode"] == "partitioned"
    assert snapshot["policy_version"] == policy.policy_version


# ===========================================================================
# shared_only / 缺段：显式 category_isolation=False
# ===========================================================================


def test_shared_only_is_explicit_and_never_claims_isolation(tmp_path) -> None:
    """``mode: shared_only``：显式 category_isolation=False，不冒充「类别配额已修复」。"""
    path = _write_yaml(
        tmp_path,
        _BASE_HEAD
        + "watch_scheduler:\n"
        + "  mode: shared_only\n"
        + "  total:\n    per_minute: 3\n    per_hour: 30\n    per_day: 300\n",
    )
    policy = load_watch_scheduler_policy(path)
    assert policy.mode == WATCH_SCHEDULER_MODE_SHARED_ONLY
    assert policy.category_isolation is False
    assert policy.category_limits is None

    budget = policy.build_budget()
    assert budget.category_limits is None
    snapshot = budget.snapshot(NOW_MONO)
    assert snapshot["category_isolation"] is False
    assert snapshot["mode"] == "shared"


def test_missing_section_is_explicit_compat_shared(tmp_path) -> None:
    """缺 ``watch_scheduler`` 段：显式兼容为 shared_only（category_isolation=False），非静默。"""
    path = _write_yaml(tmp_path, _BASE_HEAD)
    policy = load_watch_scheduler_policy(path)
    assert policy.mode == WATCH_SCHEDULER_MODE_SHARED_ONLY
    assert policy.category_isolation is False
    assert policy.policy_version == "watch_logical_compat"


def test_missing_file_is_explicit_compat_shared(tmp_path) -> None:
    """账本文件不存在：同样显式 shared_only（category_isolation=False）。"""
    policy = load_watch_scheduler_policy(tmp_path / "nonexistent.yaml")
    assert policy.mode == WATCH_SCHEDULER_MODE_SHARED_ONLY
    assert policy.category_isolation is False


# ===========================================================================
# 非法段：抛配置错误，绝不悄悄回 shared
# ===========================================================================


def test_invalid_mode_raises(tmp_path) -> None:
    """未知 mode：抛 WatchSchedulerConfigError，不降级。"""
    path = _write_yaml(
        tmp_path, _BASE_HEAD + "watch_scheduler:\n  mode: bogus\n"
    )
    with pytest.raises(WatchSchedulerConfigError):
        load_watch_scheduler_policy(path)


def test_category_sum_over_total_raises(tmp_path) -> None:
    """类别窗合计超过总窗：加载即抛（否则分桶自相矛盾）。"""
    section = _partitioned_section(normal=(15, 150, 1500), fast=(10, 120, 1200), total=(20, 300, 3000))
    path = _write_yaml(tmp_path, _BASE_HEAD + section)
    with pytest.raises(WatchSchedulerConfigError):
        load_watch_scheduler_policy(path)


def test_partitioned_missing_required_category_raises(tmp_path) -> None:
    """partitioned 缺必需类别（fast_watch）：加载即抛。"""
    path = _write_yaml(
        tmp_path,
        _BASE_HEAD
        + "watch_scheduler:\n"
        + "  mode: partitioned\n"
        + "  total:\n    per_minute: 20\n    per_hour: 300\n    per_day: 3000\n"
        + "  category_limits:\n"
        + "    normal_watch:\n      per_minute: 10\n      per_hour: 150\n      per_day: 1500\n",
    )
    with pytest.raises(WatchSchedulerConfigError):
        load_watch_scheduler_policy(path)


def test_partitioned_non_positive_limit_raises(tmp_path) -> None:
    """类别窗出现 0 等非正整数：加载即抛（拒绝 bool / 0 / 负数）。"""
    section = _partitioned_section(normal=(0, 150, 1500), fast=(8, 120, 1200), total=(20, 300, 3000))
    path = _write_yaml(tmp_path, _BASE_HEAD + section)
    with pytest.raises(ValueError):
        load_watch_scheduler_policy(path)


# ===========================================================================
# partitioned 真预算：类别窗与总窗并行校验
# ===========================================================================


def test_partitioned_windows_check_parallel_in_real_budget() -> None:
    """partitioned：类别窗与总窗**并行**校验，任一不足即拒绝（真 RequestBudget）。"""
    budget = RequestBudget(
        per_minute=3,
        per_hour=30,
        per_day=300,
        category_limits={
            "normal_watch": {"per_minute": 2, "per_hour": 20, "per_day": 200},
            "fast_watch": {"per_minute": 2, "per_hour": 20, "per_day": 200},
        },
        clock=lambda: NOW_MONO,
    )

    # fast 用满自身分钟窗（2/2）-> fast 被拒；normal 仍有独立份额（类别窗并行）。
    for index in range(2):
        result = budget.reserve("fast_watch", NOW_MONO, operation_key=f"F{index}")
        assert result.decision.granted is True
    assert budget.peek("fast_watch", NOW_MONO).granted is False
    assert budget.peek("normal_watch", NOW_MONO).granted is True

    # 总窗 = 3：normal 再用 1 份后合计 3/3，两类都被总窗拒（总窗并行生效）。
    normal = budget.reserve("normal_watch", NOW_MONO, operation_key="N0")
    assert normal.decision.granted is True
    assert budget.peek("fast_watch", NOW_MONO).granted is False
    assert budget.peek("normal_watch", NOW_MONO).granted is False

    # 释放一份 reservation 后总窗腾出，normal 类别窗也仍有余额 -> normal 恢复可放行。
    budget.release_unused(result.admission)
    assert budget.peek("normal_watch", NOW_MONO).granted is True


def test_build_budget_overrides_still_partitioned() -> None:
    """``build_budget`` 支持测试用小数字覆盖，但仍保持 partitioned 语义。"""
    policy = load_watch_scheduler_policy()
    budget = policy.build_budget(
        per_minute=2,
        per_hour=20,
        per_day=200,
        category_limits={
            "normal_watch": (1, 10, 100),
            "fast_watch": (1, 10, 100),
        },
    )
    assert budget.category_limits is not None
    assert budget.peek("normal_watch", NOW_MONO).granted is True
    assert budget.peek("fast_watch", NOW_MONO).granted is True
