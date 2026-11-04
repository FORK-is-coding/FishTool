"""长回复改用合并转发的规则（replies.py，方案 6.5）。"""
from __future__ import annotations

from fishtool_adapter.replies import FORWARD_MIN_CHARS, FORWARD_MIN_ITEMS, MAX_NODES, Reply


def test_short_reply_stays_plain_text():
    reply = Reply("标题", ["a", "b"])
    assert not reply.use_forward()
    assert reply.as_text() == "标题\na\nb"
    assert str(reply) == reply.as_text()


def test_many_items_use_forward():
    reply = Reply("标题", [f"第{i}条" for i in range(FORWARD_MIN_ITEMS + 1)])
    assert reply.use_forward()
    nodes = reply.nodes()
    assert nodes[0] == "标题" and len(nodes) == FORWARD_MIN_ITEMS + 2


def test_long_text_uses_forward_even_with_few_items():
    reply = Reply("标题", ["长" * (FORWARD_MIN_CHARS + 1)])
    assert reply.use_forward()


def test_exactly_at_threshold_stays_plain():
    reply = Reply("t", ["x"] * FORWARD_MIN_ITEMS)
    assert not reply.use_forward()


def test_tail_is_last_node_and_empty_entries_dropped():
    reply = Reply("头", ["", "a", None, "b"], ["注意：x", ""])
    assert reply.items == ["a", "b"]
    assert reply.nodes() == ["头", "a", "b", "注意：x"]


def test_nodes_capped_and_overflow_merged():
    reply = Reply("头", [f"i{i}" for i in range(MAX_NODES + 10)])
    nodes = reply.nodes()
    assert len(nodes) == MAX_NODES
    assert nodes[-1].startswith(f"i{MAX_NODES - 2}") and nodes[-1].endswith(f"i{MAX_NODES + 9}")
