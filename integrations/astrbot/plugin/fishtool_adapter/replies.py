"""长回复的拆分规则：超过阈值时改用合并转发（方案 6.5）。

个人 QQ 的普通消息过长会被折叠或拒发，群里刷出一整屏也影响阅读；合并转发在会话里只占一条，
点开逐条看。本模块只决定“发普通文本还是合并转发、每个节点放什么”，不 import astrbot。
"""
from __future__ import annotations

#: 条目数超过这个值改用合并转发。
FORWARD_MIN_ITEMS = 5
#: 全文字数超过这个值改用合并转发。
FORWARD_MIN_CHARS = 500
#: 一个合并转发最多的节点数；超出部分合进最后一个节点，避免协议端拒发。
MAX_NODES = 60


class Reply:
    """一条回复：标题 + 条目。``as_text()`` 用于普通消息，``nodes()`` 用于合并转发。

    Args:
        head: 标题行（合并转发时作为第一个节点）。
        items: 条目，每条一行或几行，合并转发时一条一个节点。
        tail: 条目之后的附注（如“另有 N 条”），合并转发时作为最后一个节点。
    """

    def __init__(self, head: str, items: list[str] | None = None, tail: list[str] | None = None):
        self.head = head
        self.items = [i for i in (items or []) if i]
        self.tail = [t for t in (tail or []) if t]

    def as_text(self) -> str:
        return "\n".join([self.head, *self.items, *self.tail])

    def use_forward(self) -> bool:
        return len(self.items) > FORWARD_MIN_ITEMS or len(self.as_text()) > FORWARD_MIN_CHARS

    def nodes(self) -> list[str]:
        parts = [self.head, *self.items, *self.tail]
        if len(parts) > MAX_NODES:
            parts = parts[: MAX_NODES - 1] + ["\n".join(parts[MAX_NODES - 1:])]
        return parts

    def __str__(self) -> str:  # 兼容只取文本的调用方
        return self.as_text()
