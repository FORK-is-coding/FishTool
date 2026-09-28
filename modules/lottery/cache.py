"""抽奖评论与用户画像的本地 JSON 缓存。

画像缓存（``user_profiles.json``）采用 **schema_version=3** 的分组空间结构：

```json
{
  "schema_version": 3,
  "profiles": {
    "full": {
      "123": {
        "groups": {
          "info": {
            "latest_started_seq": 1,
            "last_success": {"fetched_s": 123456, "values": {}, "field_status": {}},
            "last_attempt": {"seq": 1, "started_s": 123455, "at_s": 123456, "status": "ok", "reason_code": null},
            "retry_after_s": null
          }
        }
      }
    },
    "draw": {}
  },
  "legacy": {}
}
```

设计要点（对齐 03 规格 §6.2—§6.4）：

- **source-group 原子更新**：一次响应只更新对应 group，不把旧 ratio 与新 count 拼成同次事实。
- **seq 定序**：时间戳仅用于 TTL 与审计；并发顺序由 ``latest_started_seq`` 决定，
  迟到的旧请求不得覆盖新结果（即使其返回更晚）。
- **旧缓存全量标 legacy_unverified**：旧裸 UID 字典（含非 None 的 0）不可信，
  迁移时保留在 ``legacy`` 并逐条标 ``_legacy``，读端必须重采或判 indeterminate。
- **原子 + 进程内锁**：同一文件用进程内 RLock 串行 merge，唯一临时文件 + ``os.replace``。
  多进程并发不在本版能力内。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger(__name__)

#: 各 source-group 的默认 TTL（秒）：info=24h，videos/dynamics/relation=6h，draw 资格=1h。
_GROUP_TTL_S: Dict[str, int] = {
    "info": 24 * 3600,
    "videos": 6 * 3600,
    "dynamics": 6 * 3600,
    "relation": 6 * 3600,
    "draw": 3600,
}
_DEFAULT_TTL_S = 6 * 3600

#: 当前 schema 版本。
_SCHEMA_VERSION = 3

#: 每个缓存文件的进程内锁，避免同进程并发写互相覆盖（跨进程不支持）。
_FILE_LOCKS: Dict[str, "threading.RLock"] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> "threading.RLock":
    """获取（或惰性创建）指定文件绝对路径对应的进程内可重入锁。"""
    key = str(path.resolve())
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
        return lock


def group_is_fresh(group: Dict[str, Any], now_s: int, ttl_s: int) -> bool:
    """判断一个 source-group 的最新成功是否可作为本次自动资格输入（规格 §6.4）。

    要求：TTL 内、最新 attempt 为 ok、seq 对齐且成功时间等于该次 attempt 结束时间；
    最新尝试失败时不得把历史成功当本次新鲜证据。

    Args:
        group: 单个 group 的缓存字典。
        now_s: 当前 UTC 秒级时间戳。
        ttl_s: 该 group 的 TTL 秒数。

    Returns:
        ``True`` 表示新鲜可用。
    """
    if not isinstance(group, dict):
        return False
    success = group.get("last_success") or {}
    attempt = group.get("last_attempt") or {}
    stamp = success.get("fetched_s")
    attempt_s = attempt.get("at_s")
    if type(now_s) is not int or type(ttl_s) is not int or ttl_s <= 0:
        return False
    if type(stamp) is not int or not 0 <= now_s - stamp <= ttl_s:
        return False
    if attempt.get("status") != "ok" or type(attempt_s) is not int:
        return False
    seq = attempt.get("seq")
    if type(seq) is not int or seq != group.get("latest_started_seq"):
        return False
    # 同一成功 attempt 结束时间即事实接收时间，不接受未来/不一致的最新 attempt。
    return attempt_s == stamp and attempt_s <= now_s


@dataclass
class CacheLookup:
    """画像缓存查询结果，携带状态与需刷新的 group 列表。"""

    profile: Optional[Dict[str, Any]]
    cache_state: str  # hit / miss / stale / legacy / error
    reason_codes: List[str] = field(default_factory=list)
    refresh_groups: List[str] = field(default_factory=list)


class LotteryCache:
    """集中管理抽奖缓存路径、容错读取和原子写入。"""

    def __init__(self, cache_dir: Path) -> None:
        """初始化缓存仓储。

        Args:
            cache_dir: 抽奖缓存根目录。

        Returns:
            无。
        """
        self.cache_dir = cache_dir

    # ------------------------------------------------------------------ 动态评论

    def load_dynamic_comments(self, dynamic_id: str) -> List[Dict[str, Any]]:
        """读取动态评论缓存，缺失或损坏时返回空列表。

        Args:
            dynamic_id: B 站动态 ID。

        Returns:
            已缓存的标准评论列表。
        """
        try:
            path = self.dynamic_path(dynamic_id)
            if not path.exists():
                return []
            payload = json.loads(path.read_text(encoding="utf-8"))
            comments = payload.get("comments") if isinstance(payload, dict) else None
            return comments if isinstance(comments, list) else []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("动态评论缓存读取失败: %s", exc)
            return []

    def save_dynamic_comments(self, dynamic_id: str, comments: List[Dict[str, Any]]) -> None:
        """原子保存动态评论缓存。

        Args:
            dynamic_id: B 站动态 ID。
            comments: 标准评论列表。

        Returns:
            无。
        """
        payload = {"saved_at": datetime.now().isoformat(), "comments": comments}
        self._write_json(self.dynamic_path(dynamic_id), payload, "动态评论")

    # ------------------------------------------------------------------ 画像兼容层

    def load_profiles(self, namespace: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """读取用户画像缓存（兼容快照）。

        Args:
            namespace: ``None`` 返回旧扁平兼容快照（``{uid: {...}}``）；
                指定 ``'full'`` / ``'draw'`` 时返回该 v3 空间的合并快照。

        Returns:
            以字符串 UID 为键的画像映射；结构异常时返回空字典。
        """
        doc = self._read_profile_doc()
        if not doc:
            return {}
        if doc.get("schema_version") == _SCHEMA_VERSION:
            if namespace is None:
                legacy = doc.get("legacy")
                if isinstance(legacy, dict):
                    return legacy
                return self._flatten_namespace(doc, "full")
            return self._flatten_namespace(doc, namespace)
        # 旧扁平文件
        return doc if namespace is None else {}

    def save_profiles(
        self,
        profiles: Dict[str, Dict[str, Any]],
        namespace: Optional[str] = None,
    ) -> None:
        """保存用户画像缓存。

        Args:
            profiles: 以字符串 UID 为键的画像映射。
            namespace: ``None`` 为旧扁平兼容代理（新业务禁止整映射覆盖，
                应改用 ``begin_attempt`` / ``merge_attempt``）；指定空间时写入 v3。

        Returns:
            无。
        """
        if namespace is None:
            # 兼容代理：仅旧调用使用，直接写扁平文件。
            self._write_json(self.profile_path(), profiles, "抽奖用户画像")
            return
        with _lock_for(self.profile_path()):
            doc = self._read_profile_doc()
            if not isinstance(doc, dict) or doc.get("schema_version") != _SCHEMA_VERSION:
                doc = self._migrate_to_v3(doc)
            space = doc.setdefault("profiles", {}).setdefault(namespace, {})
            now = int(time.time())
            for uid_key, values in (profiles or {}).items():
                entry = space.setdefault(str(uid_key), {"groups": {}, "_legacy": False})
                entry["_legacy"] = False
                group = entry.setdefault("groups", {}).setdefault("__compat__", {})
                seq = int(group.get("latest_started_seq") or 0) + 1
                group["latest_started_seq"] = seq
                group["last_success"] = {"fetched_s": now, "values": dict(values), "field_status": {}}
                group["last_attempt"] = {"seq": seq, "started_s": now, "at_s": now, "status": "ok", "reason_code": None}
                group["retry_after_s"] = None
            self._write_profile_doc(doc)

    # ------------------------------------------------------------------ v3 空间 API

    def get_profile(
        self,
        uid: int,
        *,
        namespace: str = "full",
        now_s: Optional[int] = None,
        required_groups: Optional[List[str]] = None,
    ) -> CacheLookup:
        """按 03 规格 §6.4 命中规则查询单个 UID 的画像。

        Args:
            uid: B 站用户 ID。
            namespace: 缓存空间，``full`` 或 ``draw``。
            now_s: 当前 UTC 秒级时间戳；缺省取系统时间。
            required_groups: 该业务需要的 source-group 列表。

        Returns:
            ``CacheLookup``：全部 group 新鲜 -> hit；有需刷新 -> stale；
            旧迁移条目 -> legacy；无条目 -> miss；读取异常 -> error。
        """
        uid_key = str(int(uid))
        groups_required = list(required_groups or [])
        doc = self._read_profile_doc()
        if not doc:
            return CacheLookup(None, "miss", ["missing_entry"], groups_required)
        if doc.get("schema_version") != _SCHEMA_VERSION:
            # 纯旧扁平文件：整份视为 legacy，需重采。
            return CacheLookup(None, "legacy", ["legacy_unverified"], groups_required)
        space = (doc.get("profiles") or {}).get(namespace) or {}
        entry = space.get(uid_key) if isinstance(space, dict) else None
        if not isinstance(entry, dict):
            return CacheLookup(None, "miss", ["missing_entry"], groups_required)
        if entry.get("_legacy"):
            return CacheLookup(None, "legacy", ["legacy_unverified"], groups_required)
        groups = entry.get("groups") if isinstance(entry.get("groups"), dict) else {}
        now = int(now_s) if type(now_s) is int else int(time.time())
        refresh: List[str] = []
        values: Dict[str, Any] = {}
        stale_values: Dict[str, Any] = {}
        for name in groups_required:
            group = groups.get(name)
            if isinstance(group, dict):
                vals = (group.get("last_success") or {}).get("values")
                if isinstance(vals, dict):
                    stale_values.update(vals)
            ttl = self._ttl_for(name)
            if not group_is_fresh(group, now, ttl):
                refresh.append(name)
            else:
                success = group.get("last_success") or {}
                vals = success.get("values")
                if isinstance(vals, dict):
                    values.update(vals)
        if refresh:
            # 有新刷新未结束/过期时，可展示旧值但不作本次自动资格判断。
            return CacheLookup(stale_values or None, "stale", ["stale_group"], refresh)
        return CacheLookup(values or None, "hit", [], [])

    def begin_attempt(
        self,
        uid: int,
        *,
        namespace: str = "full",
        group: str,
        now_s: Optional[int] = None,
    ) -> int:
        """在锁内领取该 group 的递增 attempt 序号并记录 pending 尝试（§6.4）。

        Returns:
            新分配的 ``attempt_seq``（严格递增）。
        """
        now = int(now_s) if type(now_s) is int else int(time.time())
        with _lock_for(self.profile_path()):
            doc = self._read_profile_doc()
            if not isinstance(doc, dict) or doc.get("schema_version") != _SCHEMA_VERSION:
                doc = self._migrate_to_v3(doc)
            entry = self._ensure_entry(doc, namespace, str(int(uid)))
            grp = self._ensure_group(entry, group)
            seq = int(grp.get("latest_started_seq") or 0) + 1
            grp["latest_started_seq"] = seq
            grp["last_attempt"] = {
                "seq": seq, "started_s": now, "at_s": None,
                "status": "pending", "reason_code": None,
            }
            self._write_profile_doc(doc)
            return seq

    def merge_attempt(
        self,
        uid: int,
        *,
        namespace: str = "full",
        group: str,
        attempt: Any,
        success_payload: Optional[Dict[str, Any]] = None,
        now_s: Optional[int] = None,
    ) -> None:
        """在锁内把某次 attempt 的结果合并回缓存（§6.4）。

        只有 ``seq`` 等于该 group 当前 ``latest_started_seq`` 的完成结果可更新
        ``last_attempt`` / ``last_success``；更早请求迟到只记诊断，不覆盖当前状态。

        Args:
            uid: B 站用户 ID。
            namespace: 缓存空间。
            group: source-group 名称。
            attempt: ``begin_attempt`` 返回的 seq（或含 seq 的字典）。
            success_payload: 成功结果字典；``None`` 表示本次失败。
            now_s: 本次结束时间 UTC 秒；缺省取系统时间。
        """
        now = int(now_s) if type(now_s) is int else int(time.time())
        seq = attempt if type(attempt) is int else (attempt or {}).get("seq") if isinstance(attempt, dict) else None
        if type(seq) is not int:
            raise ValueError("invalid_attempt_seq")
        with _lock_for(self.profile_path()):
            doc = self._read_profile_doc()
            if not isinstance(doc, dict) or doc.get("schema_version") != _SCHEMA_VERSION:
                doc = self._migrate_to_v3(doc)
            entry = self._ensure_entry(doc, namespace, str(int(uid)))
            grp = self._ensure_group(entry, group)
            started_s = (grp.get("last_attempt") or {}).get("started_s")
            if seq != grp.get("latest_started_seq"):
                # 迟到旧请求：只记诊断历史，绝不覆盖最新 attempt / last_success 状态。
                diagnostics = grp.setdefault("diagnostics", [])
                if isinstance(diagnostics, list):
                    diagnostics.append({"seq": seq, "at_s": now, "reason_code": "superseded_by_newer_attempt"})
                    del diagnostics[:-5]  # 只保留最近 5 条，避免无界增长
                self._write_profile_doc(doc)
                return
            if isinstance(success_payload, dict):
                grp["last_success"] = {"fetched_s": now, "values": dict(success_payload), "field_status": {}}
                grp["last_attempt"] = {
                    "seq": seq, "started_s": started_s, "at_s": now,
                    "status": "ok", "reason_code": None,
                }
                grp["retry_after_s"] = None
            else:
                # 失败：保留历史 last_success，不把成功时间戳刷成 now；短退避避免循环轰炸。
                grp["last_attempt"] = {
                    "seq": seq, "started_s": started_s, "at_s": now,
                    "status": "error", "reason_code": "fetch_failed",
                }
                grp["retry_after_s"] = now + 60
            self._write_profile_doc(doc)

    # ------------------------------------------------------------------ 路径与写入

    def dynamic_path(self, dynamic_id: str) -> Path:
        """返回指定动态的缓存路径。

        Args:
            dynamic_id: B 站动态 ID。

        Returns:
            动态评论 JSON 路径。
        """
        return self.cache_dir / f"dynamic_{dynamic_id}.json"

    def profile_path(self) -> Path:
        """返回用户画像缓存路径。

        Returns:
            用户画像 JSON 路径。
        """
        return self.cache_dir / "user_profiles.json"

    def _ttl_for(self, group: str) -> int:
        """返回 group 的 TTL 秒数，未知 group 使用默认 6h。"""
        return _GROUP_TTL_S.get(group, _DEFAULT_TTL_S)

    def _read_profile_doc(self) -> Dict[str, Any]:
        """容错读取画像缓存原始文档，异常/非 dict 返回空字典。"""
        try:
            path = self.profile_path()
            if not path.exists():
                return {}
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取抽奖用户画像缓存失败: %s", exc)
            return {}

    def _write_profile_doc(self, doc: Dict[str, Any]) -> None:
        """原子写入画像缓存文档（调用方须已持有文件锁）。"""
        self._write_json(self.profile_path(), doc, "抽奖用户画像")

    @staticmethod
    def _flatten_namespace(doc: Dict[str, Any], namespace: str) -> Dict[str, Dict[str, Any]]:
        """把某空间的各 group ``last_success.values`` 合并成扁平兼容快照。"""
        space = (doc.get("profiles") or {}).get(namespace) or {}
        out: Dict[str, Dict[str, Any]] = {}
        if not isinstance(space, dict):
            return out
        for uid_key, entry in space.items():
            if not isinstance(entry, dict) or entry.get("_legacy"):
                continue
            values: Dict[str, Any] = {}
            for group in (entry.get("groups") or {}).values():
                vals = (group.get("last_success") or {}).get("values") if isinstance(group, dict) else None
                if isinstance(vals, dict):
                    values.update(vals)
            if values:
                out[str(uid_key)] = values
        return out

    @staticmethod
    def _ensure_entry(doc: Dict[str, Any], namespace: str, uid_key: str) -> Dict[str, Any]:
        """确保 v3 文档中存在指定空间/UID 的条目并返回它。"""
        space = doc.setdefault("profiles", {}).setdefault(namespace, {})
        entry = space.get(uid_key)
        if not isinstance(entry, dict):
            entry = {"groups": {}, "_legacy": False}
            space[uid_key] = entry
        entry.setdefault("groups", {})
        return entry

    @staticmethod
    def _ensure_group(entry: Dict[str, Any], group: str) -> Dict[str, Any]:
        """确保条目中存在指定 group 并返回它。"""
        groups = entry.setdefault("groups", {})
        grp = groups.get(group)
        if not isinstance(grp, dict):
            grp = {"latest_started_seq": 0, "last_success": None, "last_attempt": None, "retry_after_s": None}
            groups[group] = grp
        return grp

    @staticmethod
    def _migrate_to_v3(doc: Any) -> Dict[str, Any]:
        """把旧扁平文档迁移为 v3 envelope。

        旧缓存无来源/时间依据，**全部**标 ``legacy_unverified``（含旧非 None 的 0），
        原数据保留在 ``legacy`` 下，不销毁用户文件，也不凭加载时间盖新鲜时间戳。
        """
        if isinstance(doc, dict) and doc.get("schema_version") == _SCHEMA_VERSION:
            return doc
        legacy = dict(doc) if isinstance(doc, dict) else {}
        envelope: Dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "profiles": {"full": {}, "draw": {}},
            "legacy": legacy,
        }
        for uid_key in legacy:
            envelope["profiles"]["full"][str(uid_key)] = {"groups": {}, "_legacy": True}
        return envelope

    def _write_json(self, target: Path, payload: Any, label: str) -> None:
        """通过同目录唯一临时文件原子写入 JSON。

        Args:
            target: 最终文件路径。
            payload: 可 JSON 序列化的数据。
            label: 日志中的缓存名称。

        Returns:
            无（写入失败仅记录日志，不影响本次结果）。
        """
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            # 唯一临时文件名避免并发写互相覆盖；os.replace 保证原子替换。
            temporary = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, target)
        except (OSError, TypeError) as exc:
            logger.warning("写入%s缓存失败，不影响本次结果: %s", label, exc)
