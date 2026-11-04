"""聊天指令准入：开关、三级权限、冷却。"""
from __future__ import annotations

import pytest

from fishtool_adapter.constants import chat_cfg
from fishtool_adapter.gate import ADMIN, COOLDOWN, DENIED, DENIED_QUIET, OFF, OK, PUBLIC, RESTRICTED, Gate


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def make(subscribed=(), clock=None, **raw):
    return Gate(chat_cfg(raw), is_subscribed=lambda umo: umo in subscribed, clock=clock or Clock())


def check(gate, *, name="status", level=PUBLIC, umo="qq:GroupMessage:1", admin=False):
    return gate.check(name=name, level=level, umo=umo, is_admin=admin)


def test_disabled_returns_off_even_for_admin():
    gate = make(enable=False)
    assert check(gate, admin=True) == OFF
    assert check(gate, level=ADMIN) == OFF


def test_admin_level_requires_admin():
    gate = make()
    assert check(gate, level=ADMIN) == DENIED
    assert check(gate, level=ADMIN, admin=True) == OK


def test_admin_level_ignores_scope_everyone():
    # 高成本指令没有配置开关：restricted_scope=everyone 也不放行
    gate = make(restricted_scope="everyone")
    assert check(gate, level=ADMIN) == DENIED


@pytest.mark.parametrize("scope,subscribed,expect", [
    ("admin", True, DENIED),
    ("subscribed", False, DENIED),
    ("subscribed", True, OK),
    ("everyone", False, OK),
])
def test_restricted_scope(scope, subscribed, expect):
    umo = "qq:GroupMessage:1"
    gate = make(subscribed={umo} if subscribed else set(), restricted_scope=scope)
    assert check(gate, level=RESTRICTED, umo=umo) == expect


def test_restricted_admin_always_ok():
    assert check(make(), level=RESTRICTED, admin=True) == OK


def test_cooldown_per_session_and_command():
    clock = Clock()
    gate = make(clock=clock)
    assert check(gate) == OK
    assert check(gate) == COOLDOWN
    assert check(gate, name="hotspot") == OK
    assert check(gate, umo="qq:GroupMessage:2") == OK
    clock.now += 10
    assert check(gate) == OK


def test_cooldown_not_applied_to_admin():
    gate = make()
    for _ in range(3):
        assert check(gate, admin=True) == OK


def test_cooldown_zero_disables():
    gate = make(cooldown_seconds=0)
    assert check(gate) == OK
    assert check(gate) == OK


def test_denied_reply_rate_limited_per_sender():
    clock = Clock()
    gate = make(clock=clock)
    assert gate.check(name="monitor", level=ADMIN, umo="g1", is_admin=False, sender="u1") == DENIED
    # 同一人在冷却窗口内再刷管理员指令（换指令也一样）：静默
    assert gate.check(name="diag", level=ADMIN, umo="g1", is_admin=False, sender="u1") == DENIED_QUIET
    # 另一个人、另一个会话各自提示一次
    assert gate.check(name="monitor", level=ADMIN, umo="g1", is_admin=False, sender="u2") == DENIED
    assert gate.check(name="monitor", level=ADMIN, umo="g2", is_admin=False, sender="u1") == DENIED
    clock.now += 10
    assert gate.check(name="monitor", level=ADMIN, umo="g1", is_admin=False, sender="u1") == DENIED


def test_denied_quiet_does_not_consume_command_cooldown():
    clock = Clock()
    gate = make(clock=clock, restricted_scope="subscribed")
    gate.check(name="alerts", level=RESTRICTED, umo="g1", is_admin=False, sender="u1")
    gate.check(name="alerts", level=RESTRICTED, umo="g1", is_admin=False, sender="u1")
    # 公开指令的冷却不受影响
    assert gate.check(name="status", level=PUBLIC, umo="g1", is_admin=False, sender="u1") == OK


def test_denied_not_rate_limited_when_cooldown_zero():
    gate = make(cooldown_seconds=0)
    for _ in range(3):
        assert gate.check(name="x", level=ADMIN, umo="g", is_admin=False, sender="u") == DENIED


def test_denied_does_not_consume_cooldown():
    clock = Clock()
    gate = make(clock=clock, restricted_scope="subscribed")
    assert check(gate, level=RESTRICTED) == DENIED
    gate._is_subscribed = lambda umo: True
    assert check(gate, level=RESTRICTED) == OK


def test_prune_threshold_grows_when_many_active():
    clock = Clock()
    gate = make(clock=clock, cooldown_seconds=1000)
    for i in range(600):
        check(gate, umo=f"s{i}")
    # 全部仍在窗口内：清理后门槛应抬高，后续调用不再每次全量遍历
    assert gate._prune_at >= 1024
    assert len(gate._last) == 600


def test_cooldown_records_pruned():
    clock = Clock()
    gate = make(clock=clock, cooldown_seconds=1)
    for i in range(600):
        clock.now += 0.01  # 每条间隔 0.01 秒：写到第 512 条时，前 412 条已过 1 秒冷却期
        check(gate, umo=f"s{i}")
    assert len(gate._last) < 600
    # 清理只删过期记录：最近仍在冷却期内的会话照样被挡
    assert check(gate, umo="s599") == COOLDOWN


def test_prune_keeps_active_records():
    clock = Clock()
    gate = make(clock=clock, cooldown_seconds=1000)
    for i in range(600):
        check(gate, umo=f"s{i}")
    assert check(gate, umo="s0") == COOLDOWN
