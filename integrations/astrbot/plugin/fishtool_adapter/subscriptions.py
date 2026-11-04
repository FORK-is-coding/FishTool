"""告警推送订阅：哪些会话接收引擎推送的评论舆情告警。

存放在 ``<data_root>/remote/subscriptions.json``，不放进插件配置：
- AstrBot 面板的 list 配置项只按字符串渲染（dashboard ListConfigItem.vue 对元素调用 ``.length``
  / ``.slice``），对象列表会显示异常；
- 在面板保存插件配置会触发插件重载（dashboard config_service.save_plugin_configs），订阅是由
  ``/fishtool 订阅`` 写入的运行数据，不应和用户手填的配置混在一起。

不 import astrbot；logger 由调用方传入。写入用临时文件 + ``os.replace``，避免写到一半崩溃留下坏文件。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

#: 订阅数量上限，防止被滥用后每次告警都向成百个会话推送。
MAX_SUBSCRIPTIONS = 50


def _clean(items) -> list[dict]:
    """只保留 ``{"umo": 非空字符串, "self_id": 字符串}``，按 umo 去重、保持顺序。"""
    result: list[dict] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        umo = item.get("umo").strip() if isinstance(item.get("umo"), str) else ""
        if not umo or umo in seen:
            continue
        seen.add(umo)
        self_id = item.get("self_id")
        result.append({"umo": umo, "self_id": self_id.strip() if isinstance(self_id, str) else ""})
    return result[:MAX_SUBSCRIPTIONS]


class SubscriptionStore:
    """内存中保留一份副本；每次修改立即落盘。"""

    def __init__(self, path: Path, logger):
        self.path = path
        self.log = logger
        self._items: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            self.log.warning(f"[fishtool] 读取推送订阅失败，按空列表处理: {exc}")
            return []
        return _clean(raw.get("sessions") if isinstance(raw, dict) else raw)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".subscriptions.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"sessions": self._items}, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001 - 先清理临时文件再原样抛出
            try:
                os.unlink(tmp)
            except OSError as exc:
                self.log.warning(f"[fishtool] 清理订阅临时文件失败: {exc}")
            raise

    def list(self) -> list[dict]:
        return [dict(item) for item in self._items]

    def contains(self, umo: str) -> bool:
        umo = (umo or "").strip()
        return any(item["umo"] == umo for item in self._items)

    def add(self, umo: str, self_id: str = "") -> str:
        """新增或更新一条订阅。

        Returns:
            str: ``added`` / ``updated`` / ``full``。

        Raises:
            ValueError: umo 为空。
            OSError: 落盘失败（内存中的修改会回滚）。
        """
        umo = (umo or "").strip()
        if not umo:
            raise ValueError("umo 不能为空")
        before = [dict(item) for item in self._items]
        for item in self._items:
            if item["umo"] == umo:
                item["self_id"] = self_id or item["self_id"]
                result = "updated"
                break
        else:
            if len(self._items) >= MAX_SUBSCRIPTIONS:
                return "full"
            self._items.append({"umo": umo, "self_id": self_id or ""})
            result = "added"
        try:
            self._save()
        except OSError:
            self._items = before
            raise
        return result

    def remove(self, umo: str) -> bool:
        """移除订阅；不存在时返回 False。落盘失败抛 OSError 并回滚。"""
        umo = (umo or "").strip()
        before = [dict(item) for item in self._items]
        self._items = [item for item in self._items if item["umo"] != umo]
        if len(self._items) == len(before):
            return False
        try:
            self._save()
        except OSError:
            self._items = before
            raise
        return True
