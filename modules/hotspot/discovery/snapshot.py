"""06 采集广度 · 全局发现快照（硬约束 2：失败状态必须有落点）。

背景：06 R1.0 正文「不建台账」与「落本次失败状态、支持回放与幂等」互相矛盾。
本模块给一个**最小方案**——一个**全局发现快照**（不是每事件一份），只保存
「这一轮三个入口各自发生了什么」，用于区分四种情况：

    1. 本轮真空榜          -> source.state == 'ok' 且 returned_count == 0；
    2. 本轮请求失败        -> source.state == 'error'（error_code 有值）；
    3. 仅第一页成功        -> source.state == 'partial'（多页源）；
    4. 当前展示的是上轮缓存 -> source.from_cache 为 True（cache_age_s 给出陈旧度）。

落点：**单个原子 JSON 文件**（不进业务库，不新增台账表）。选择理由：发现快照是
「操作状态」，不是观测事实，用文件即可；且只需一份全局状态，不需要按事件复制。

更新与保留协议（写进代码注释，供读端遵守）
------------------------------------------
- ``current``：**整体覆盖**为最近一轮快照（每一轮 poll 结束时写一次）；
- ``history``：把每轮精简摘要 append 到尾部，只保留最近 ``history_limit`` 轮
  （默认 50），超出裁掉最旧——**history 上限即保留期**，不另设清理任务；
- 写入走「同目录临时文件 + ``os.replace``」**原子替换**，读端永远看到完整 JSON；
- 文件缺失 / 损坏：``load`` 返回空结构并记日志，**不抛异常**（发现通道不因快照读失败停摆）；
- 回放幂等：``snapshot_id`` 由「参数 hash + 采样时刻」确定；同一 ``snapshot_id``
  已在 ``current`` 或 ``history`` 中 -> ``has`` 为真，service 直接短路，不重复写库。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: 快照结构版本。
SCHEMA_VERSION = 1
#: ``history`` 保留的最大轮数（即保留期）。
DEFAULT_HISTORY_LIMIT = 50
#: 缺省快照文件位置（data/ 下，与其它运行态文件同域）。
DEFAULT_SNAPSHOT_PATH = Path("data") / "hotspot" / "discovery_snapshot.json"


def _empty_payload() -> Dict[str, Any]:
    """返回空的快照文件结构。

    Returns:
        Dict[str, Any]: ``{"schema_version", "updated_epoch_s", "current", "history"}``。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_epoch_s": None,
        "current": None,
        "history": [],
    }


class DiscoverySnapshotStore:
    """全局发现快照的读写（单文件 + 原子替换 + 有界 history）。

    Attributes:
        path: 快照文件路径。
        history_limit: history 保留轮数上限。
    """

    def __init__(self, path: Any = DEFAULT_SNAPSHOT_PATH, *, history_limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        """初始化快照存储。

        Args:
            path: 快照文件路径（可为 str / Path）。
            history_limit: history 保留轮数上限（至少 1）。
        """
        self.path = Path(path)
        self.history_limit = max(1, int(history_limit))

    # ---------------------------------------------------------------- 读
    def load(self) -> Dict[str, Any]:
        """读取快照文件。

        Returns:
            Dict[str, Any]: 完整结构；文件缺失 / 损坏时返回空结构（不抛异常）。
        """
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError("snapshot root is not a dict")
            payload.setdefault("schema_version", SCHEMA_VERSION)
            payload.setdefault("current", None)
            if not isinstance(payload.get("history"), list):
                payload["history"] = []
            return payload
        except FileNotFoundError:
            return _empty_payload()
        except Exception as exc:  # noqa: BLE001 - 快照读失败不阻断发现通道
            logger.warning("发现快照读取失败，按空结构处理 (%s): %r", self.path, exc)
            return _empty_payload()

    def latest(self) -> Optional[Dict[str, Any]]:
        """返回最近一轮快照（``current``）。

        Returns:
            Optional[Dict[str, Any]]: 快照 dict；从未写入时返回 None。
        """
        return self.load().get("current")

    def find(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        """按 ``snapshot_id`` 查找既有一轮（先 current 后 history）。

        Args:
            snapshot_id: 快照标识。

        Returns:
            Optional[Dict[str, Any]]: 命中的快照 / 摘要；未命中返回 None。
        """
        payload = self.load()
        current = payload.get("current")
        if isinstance(current, dict) and current.get("snapshot_id") == snapshot_id:
            return current
        for entry in reversed(payload.get("history") or []):
            if isinstance(entry, dict) and entry.get("snapshot_id") == snapshot_id:
                return entry
        return None

    def has(self, snapshot_id: str) -> bool:
        """判断某 ``snapshot_id`` 是否已记录（回放幂等依据）。

        Args:
            snapshot_id: 快照标识。

        Returns:
            bool: 已记录返回 True。
        """
        return self.find(snapshot_id) is not None

    def latest_sources_state(self) -> Dict[str, Any]:
        """返回最近一轮各来源的运行态摘要（供读端区分「新抓 / 上轮缓存」）。

        Returns:
            Dict[str, Any]: ``{source: {state, from_cache, cache_age_s, ...}}``；
            无快照时返回空 dict。
        """
        current = self.latest()
        if not isinstance(current, dict):
            return {}
        return dict(current.get("sources") or {})

    # ---------------------------------------------------------------- 写
    def save(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """写入一轮快照：整体覆盖 ``current``，并向 ``history`` 追加摘要。

        Args:
            snapshot: service 组装的一轮快照 dict。

        Returns:
            Dict[str, Any]: 落盘后的完整文件结构。

        Raises:
            OSError: 目录不可写 / 原子替换失败时抛出（调用方决定是否上抛）。
        """
        payload = self.load()
        record = dict(snapshot)
        payload["schema_version"] = SCHEMA_VERSION
        payload["updated_epoch_s"] = record.get("captured_epoch_s")
        payload["current"] = record

        summary_keys = (
            "snapshot_id",
            "captured_epoch_s",
            "params_hash",
            "sources",
            "served_from_cache",
            "keyword_count",
            "video_count",
        )
        summary = {key: record.get(key) for key in summary_keys if key in record}
        history: List[Dict[str, Any]] = list(payload.get("history") or [])
        history.append(summary)
        payload["history"] = history[-self.history_limit:]

        self._write_atomic(payload)
        return payload

    def _write_atomic(self, payload: Dict[str, Any]) -> None:
        """原子写出快照文件（同目录临时文件 + ``os.replace``）。

        Args:
            payload: 待写入结构。

        Returns:
            无。

        Raises:
            OSError: 写入或替换失败时抛出。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, temp_path = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
