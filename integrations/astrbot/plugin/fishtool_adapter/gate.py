"""聊天指令的准入判断：开关、三级权限、冷却。

纯逻辑、不 import astrbot，main.py 把事件里的几个字段传进来即可，便于单测。
"""
from __future__ import annotations

import time

#: 权限级别。公开：所有人；受限：按 chat.restricted_scope；管理员：始终只限 AstrBot admins_id。
PUBLIC, RESTRICTED, ADMIN = "public", "restricted", "admin"

#: 准入结果。
OFF, DENIED, COOLDOWN, OK = "off", "denied", "cooldown", "ok"

#: 冷却字典条目数达到这个值时才做一次清理（删掉已过冷却期的记录），避免每次都遍历。
_PRUNE_AT = 512

DENIED_TEXT = "您(ID: {sid})的权限不足以使用此指令。通过 /sid 获取 ID 并请管理员添加。"


class Gate:
    """每个插件实例一个；冷却记录只在内存里，插件重载后清空。"""

    def __init__(self, chat: dict, *, is_subscribed=None, clock=time.monotonic):
        self.chat = chat
        self._is_subscribed = is_subscribed or (lambda umo: False)
        self._clock = clock
        self._last: dict[tuple[str, str], float] = {}
        self._prune_at = _PRUNE_AT

    def check(self, *, name: str, level: str, umo: str, is_admin: bool) -> str:
        """判断一次指令调用能否执行。

        Args:
            name: 指令标识，用于冷却分桶（如 ``status``、``resident:状态``）。
            level: ``public`` / ``restricted`` / ``admin``。
            umo: 会话标识 ``event.unified_msg_origin``。
            is_admin: ``event.is_admin()``。

        Returns:
            str: ``off`` / ``denied`` / ``cooldown`` / ``ok``。
        """
        if not self.chat.get("enable", True):
            return OFF
        if is_admin:
            return OK
        if level == ADMIN:
            return DENIED
        if level == RESTRICTED and not self._restricted_allowed(umo):
            return DENIED
        return self._cooldown(name, umo)

    def _restricted_allowed(self, umo: str) -> bool:
        scope = self.chat.get("restricted_scope", "admin")
        if scope == "everyone":
            return True
        if scope == "subscribed":
            return bool(umo) and self._is_subscribed(umo)
        return False

    def _cooldown(self, name: str, umo: str) -> str:
        window = self.chat.get("cooldown_seconds", 10)
        if window <= 0:
            return OK
        now = self._clock()
        key = (umo, name)
        if now - self._last.get(key, float("-inf")) < window:
            return COOLDOWN
        self._last[key] = now
        if len(self._last) >= self._prune_at:
            # 已过冷却期的记录不再影响判断，可以直接删
            self._last = {k: t for k, t in self._last.items() if now - t < window}
            # 清理后仍然很多（大量会话都在冷却期内）：抬高下次清理的门槛，避免每次调用都遍历
            self._prune_at = max(_PRUNE_AT, len(self._last) * 2)
        return OK
