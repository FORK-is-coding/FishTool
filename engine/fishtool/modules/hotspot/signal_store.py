"""热点信号存储服务，封装数据库写入和30天滚动裁剪。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from core.database import HotspotSignal, get_session


class HotspotSignalStore:
    """统一负责热点信号落盘，调用方不直接操作 ORM。"""

    def save_many(self, signals: list[dict[str, Any]]) -> int:
        """保存结构化信号并裁剪30天以前数据，返回新增数量。"""
        session = get_session()
        try:
            for item in signals:
                source = str(item.get("source") or "unknown")
                session.add(HotspotSignal(tid=int(item.get("tid") or 0), bvid=str(item.get("bvid") or ""), collected_at=item.get("collected_at") or datetime.now(), source=source, value={"source": source, "payload": item.get("value", {})}))
            cutoff = datetime.now() - timedelta(days=30)
            session.query(HotspotSignal).filter(HotspotSignal.collected_at < cutoff).delete(synchronize_session=False)
            session.commit()
            return len(signals)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_recent(self, tid: int | None = None, limit: int = 500) -> list[HotspotSignal]:
        """读取近期信号，按最新时间倒序返回。"""
        session = get_session()
        try:
            query = session.query(HotspotSignal).order_by(HotspotSignal.collected_at.desc())
            if tid is not None:
                query = query.filter(HotspotSignal.tid == tid)
            return query.limit(max(1, min(limit, 5000))).all()
        finally:
            session.close()
