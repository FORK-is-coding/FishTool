"""告警推送订阅的持久化。"""
from __future__ import annotations

import json
import logging

import pytest

from fishtool_adapter.subscriptions import MAX_SUBSCRIPTIONS, SubscriptionStore

log = logging.getLogger("test")


def test_add_update_remove_roundtrip(tmp_path):
    path = tmp_path / "remote" / "subscriptions.json"
    store = SubscriptionStore(path, log)
    assert store.list() == []
    assert store.add("qq:GroupMessage:1", "10001") == "added"
    assert store.add("qq:GroupMessage:1", "10002") == "updated"
    assert store.add("qq:FriendMessage:2") == "added"
    assert store.contains("qq:FriendMessage:2")

    reloaded = SubscriptionStore(path, log)
    assert reloaded.list() == [
        {"umo": "qq:GroupMessage:1", "self_id": "10002"},
        {"umo": "qq:FriendMessage:2", "self_id": ""},
    ]
    assert reloaded.remove("qq:GroupMessage:1") is True
    assert reloaded.remove("qq:GroupMessage:1") is False
    assert SubscriptionStore(path, log).list() == [{"umo": "qq:FriendMessage:2", "self_id": ""}]


def test_update_keeps_old_self_id_when_new_is_empty(tmp_path):
    store = SubscriptionStore(tmp_path / "s.json", log)
    store.add("u", "10001")
    store.add("u", "")
    assert store.list() == [{"umo": "u", "self_id": "10001"}]


def test_corrupt_file_treated_as_empty(tmp_path, caplog):
    path = tmp_path / "s.json"
    path.write_text("{not json", "utf-8")
    with caplog.at_level(logging.WARNING):
        store = SubscriptionStore(path, log)
    assert store.list() == []
    assert "读取推送订阅失败" in caplog.text


def test_load_filters_bad_entries(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"sessions": [
        {"umo": "a", "self_id": "1"},
        {"umo": "a", "self_id": "2"},
        {"umo": ""},
        "b",
        {"umo": " c ", "self_id": 5},
    ]}), "utf-8")
    assert SubscriptionStore(path, log).list() == [
        {"umo": "a", "self_id": "1"},
        {"umo": "c", "self_id": ""},
    ]


def test_limit(tmp_path):
    store = SubscriptionStore(tmp_path / "s.json", log)
    for i in range(MAX_SUBSCRIPTIONS):
        assert store.add(f"u{i}") == "added"
    assert store.add("overflow") == "full"
    assert store.add("u0", "x") == "updated"
    assert len(store.list()) == MAX_SUBSCRIPTIONS


def test_save_failure_rolls_back(tmp_path, monkeypatch):
    store = SubscriptionStore(tmp_path / "s.json", log)
    store.add("u1")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("fishtool_adapter.subscriptions.os.replace", boom)
    with pytest.raises(OSError):
        store.add("u2")
    assert store.list() == [{"umo": "u1", "self_id": ""}]
    with pytest.raises(OSError):
        store.remove("u1")
    assert store.contains("u1")
    assert not list(tmp_path.glob(".subscriptions.*.tmp"))


def test_whitespace_handled_consistently(tmp_path):
    store = SubscriptionStore(tmp_path / "s.json", log)
    store.add("  u1  ")
    assert store.contains("u1") and store.contains(" u1 ")
    assert store.remove(" u1") is True
    assert not store.contains("u1")


def test_empty_umo_rejected(tmp_path):
    with pytest.raises(ValueError):
        SubscriptionStore(tmp_path / "s.json", log).add("  ")
