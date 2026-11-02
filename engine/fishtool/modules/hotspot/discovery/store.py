"""06 采集广度 · 发现信号落库写入器。

- :class:`KeywordSignalStore`：写 ``hot_keyword_signal``（关键词纯事实表）；
- :class:`VideoSignalStore`：写 ``HotspotSignal``（**复用视频类信号表，不新增表**）。

``value`` 层级与既有 ``modules/hotspot/signal_store.HotspotSignalStore`` 保持一致
（``{"source": ..., "payload": {...}}``），新 parser 与读端按同一层级约定解析。

写入器均接受可注入的 ``session_factory``，便于离线测试指向临时库，绝不触碰仓库真库。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.database import HotKeywordSignal, HotspotSignal, get_session

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Any]


class KeywordSignalStore:
    """热搜关键词信号写入器（``hot_keyword_signal``）。

    幂等口径：唯一键 ``(keyword, captured_epoch_s)``。同一响应重放（保留原采样时间）
    不重复写入；新抓一次生成新 ``captured_epoch_s`` 属新观察，允许新增。
    """

    def __init__(self, session_factory: Optional[SessionFactory] = None) -> None:
        """初始化写入器。

        Args:
            session_factory: 会话工厂；缺省用 ``core.database.get_session``。
        """
        self._session_factory: SessionFactory = session_factory or get_session

    def save_observations(
        self, keywords: Iterable[Any], *, snapshot_id: Optional[str] = None
    ) -> int:
        """写入关键词观测，按 ``(keyword, captured_epoch_s)`` 去重。

        Args:
            keywords: BroadKeyword 序列（缺失 heat_score 的候选照常写入，score 记 NULL）。
            snapshot_id: 所属全局发现快照标识（可空）。

        Returns:
            int: 实际新增条数。

        Raises:
            Exception: 写库失败时回滚并原样抛出。
        """
        rows = list(keywords)
        if not rows:
            return 0
        session = self._session_factory()
        try:
            epochs = {row.captured_epoch_s for row in rows}
            existing: set = set()
            for epoch in epochs:
                existing.update(
                    (item[0], item[1])
                    for item in session.query(
                        HotKeywordSignal.keyword, HotKeywordSignal.captured_epoch_s
                    )
                    .filter(HotKeywordSignal.captured_epoch_s == epoch)
                    .all()
                )
            inserted = 0
            for row in rows:
                key = (row.keyword, row.captured_epoch_s)
                if key in existing:
                    continue
                session.add(
                    HotKeywordSignal(
                        keyword=row.keyword,
                        heat_score=row.heat_score,  # 缺失即 None -> SQL NULL，不用 0 顶替
                        heat_status=row.heat_status,
                        rank=row.rank,
                        captured_epoch_s=row.captured_epoch_s,
                        source=row.source,
                        snapshot_id=snapshot_id,
                    )
                )
                existing.add(key)
                inserted += 1
            session.commit()
            return inserted
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_recent(self, limit: int = 200) -> List[HotKeywordSignal]:
        """读取近期关键词观测（按时间倒序）。

        Args:
            limit: 返回条数上限（夹取到 1..5000）。

        Returns:
            List[HotKeywordSignal]: 观测行。
        """
        session = self._session_factory()
        try:
            return (
                session.query(HotKeywordSignal)
                .order_by(HotKeywordSignal.captured_epoch_s.desc(), HotKeywordSignal.rank.asc())
                .limit(max(1, min(int(limit), 5000)))
                .all()
            )
        finally:
            session.close()


class VideoSignalStore:
    """视频发现信号写入器（复用 ``HotspotSignal``）。

    说明：``HotspotSignal.tid`` 非空，但发现条目未知 tid 时**不得**补成已验证值；
    这里写哨兵 ``0`` 占位，并在 ``value.payload.tid_status`` 明确标注 ``missing``，
    读端据 status 区分「哨兵 0」与「真实 0 分区」。
    """

    def __init__(
        self,
        session_factory: Optional[SessionFactory] = None,
        *,
        retention_days: int = 30,
    ) -> None:
        """初始化写入器。

        Args:
            session_factory: 会话工厂；缺省用 ``core.database.get_session``。
            retention_days: 滚动裁剪天数（沿用现有信号表 30 天口径）。
        """
        self._session_factory: SessionFactory = session_factory or get_session
        self.retention_days = int(retention_days)

    def save_candidates(self, candidates: Iterable[Dict[str, Any]]) -> int:
        """写入已合并的视频候选（每个 bvid 一条，来源全部记入 value）。

        Args:
            candidates: :func:`merge_video_candidates` 产出的 dict 列表。

        Returns:
            int: 实际写入条数。

        Raises:
            Exception: 写库失败时回滚并原样抛出。
        """
        rows = list(candidates)
        if not rows:
            return 0
        session = self._session_factory()
        try:
            for row in rows:
                captured = int(row.get("captured_epoch_s") or 0)
                payload = self._build_payload(row)
                session.add(
                    HotspotSignal(
                        # tid 非空：缺失写哨兵 0，真值状态记在 payload.legacy_tid_status。
                        tid=int(row.get("legacy_tid") or 0),
                        bvid=str(row.get("bvid") or ""),
                        collected_at=self._collected_at(captured),
                        source=str(row.get("display_source") or "unknown"),
                        value={"source": str(row.get("display_source") or "unknown"), "payload": payload},
                    )
                )
            # 与既有视频类信号表一致：滚动裁剪 retention_days 以前的数据。
            cutoff = datetime.now() - timedelta(days=self.retention_days)
            session.query(HotspotSignal).filter(HotspotSignal.collected_at < cutoff).delete(
                synchronize_session=False
            )
            session.commit()
            return len(rows)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _collected_at(captured_epoch_s: int) -> datetime:
        """把观测时刻换算为 ``collected_at``（沿用现有本地 naive 显示口径）。

        Args:
            captured_epoch_s: UTC 秒级时间戳。

        Returns:
            datetime: 本地 naive datetime；非法输入回退当前时间。
        """
        try:
            return datetime.fromtimestamp(captured_epoch_s)
        except (ValueError, OSError, OverflowError):
            return datetime.now()

    @staticmethod
    def _build_payload(row: Dict[str, Any]) -> Dict[str, Any]:
        """构造 ``value.payload``：保留来源明细与三套分类字段，未知值标 missing。

        Args:
            row: 合并后的候选 dict。

        Returns:
            Dict[str, Any]: 结构化 payload。
        """
        return {
            "captured_epoch_s": row.get("captured_epoch_s"),
            "display_source": row.get("display_source"),
            "sources": list(row.get("sources") or []),
            "source_details": list(row.get("source_details") or []),
            "conflict": bool(row.get("conflict")),
            # 三套分类字段并存，不合并成统一 tid。
            "legacy_tid": row.get("legacy_tid"),
            "legacy_tid_status": row.get("legacy_tid_status"),
            "tidv2": row.get("tidv2"),
            "tidv2_status": row.get("tidv2_status"),
            "pid_v2": row.get("pid_v2"),
            "pid_v2_status": row.get("pid_v2_status"),
            "tname": row.get("tname"),
            "tnamev2": row.get("tnamev2"),
            "owner_mid": row.get("owner_mid"),
            "owner_status": row.get("owner_status"),
            "view": row.get("view"),
            "view_status": row.get("view_status"),
            "rcmd_reason": row.get("rcmd_reason"),
        }

    def list_recent(self, limit: int = 500) -> List[HotspotSignal]:
        """读取近期视频发现信号（按时间倒序）。

        Args:
            limit: 返回条数上限（夹取到 1..5000）。

        Returns:
            List[HotspotSignal]: 信号行。
        """
        session = self._session_factory()
        try:
            return (
                session.query(HotspotSignal)
                .order_by(HotspotSignal.collected_at.desc())
                .limit(max(1, min(int(limit), 5000)))
                .all()
            )
        finally:
            session.close()
