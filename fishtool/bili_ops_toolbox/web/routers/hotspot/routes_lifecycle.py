"""热点生命周期、采集进度、账号关联与时间轴 API。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import HTTPException, Query

from core.database import Video, VideoStats, get_session
from core.logger import get_logger
from modules.hotspot.algorithm import Snapshot

logger = get_logger(__name__)
from modules.hotspot.collector import HotspotCollector, get_progress
from modules.hotspot.service import HotspotService
from modules.hotspot.up_metrics import get_up_light_metrics
from modules.hotspot.up_relation import get_up_relation
from .deps import get_api
from . import router


_collect_task: asyncio.Task | None = None


def _load_snapshots(tid: int | None = None, bvid: str | None = None) -> list[Snapshot]:
    """从视频统计历史读取算法快照。"""
    session = get_session()
    try:
        query = session.query(Video, VideoStats).join(VideoStats, Video.id == VideoStats.video_id)
        if tid is not None:
            query = query.filter(Video.tid == tid)
        if bvid:
            query = query.filter(Video.bvid == bvid)
        # 展示层兜底：只返回 7 天窗口内的视频，防止存量/误入的过期数据上卡片。
        from datetime import timedelta as _td
        cutoff = datetime.now() - _td(days=7)
        query = query.filter(
            (Video.pubdate.is_(None)) | (Video.pubdate >= cutoff)
        )
        rows: list[Snapshot] = []
        for video, stats in query.order_by(VideoStats.snapshot_time.asc()).all():
            rows.append(Snapshot(
                bvid=video.bvid,
                tid=int(video.tid or 0),
                captured_at=stats.snapshot_time or datetime.now(),
                view=int(stats.view or 0),
                title=video.title or "",
                owner_mid=int(video.mid or 0),
                owner_name=video.author or "",
                source="video_stats",
            ))
        return rows
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取热点快照失败: {exc}") from exc
    finally:
        session.close()


@router.get("/lifecycle")
async def get_lifecycle(
    tid: int | None = Query(default=None),
    bvid: str | None = Query(default=None),
    algorithm: str = Query(default="heuristic_v1"),
):
    """返回生命周期 Detection DTO，展示层不感知算法实现。"""
    try:
        snapshots = _load_snapshots(tid=tid, bvid=bvid)
        service = HotspotService(algorithm_name=algorithm)
        items = service.analyze(snapshots)
        # 批量补 owner 轻量指标（粉丝/舰长/充电），供卡片页展示每千粉转化率。
        # 只在确有 UP 时请求；失败降级不影响列表主体渲染。
        try:
            mids = sorted({int(item.get("owner_mid") or 0) for item in items if item.get("owner_mid")})
            owner_metrics = await get_up_light_metrics(get_api(), mids)
            for item in items:
                owner = owner_metrics.get(int(item.get("owner_mid") or 0))
                item["owner_metrics"] = owner or None
        except Exception as exc:
            logger.warning("生命周期卡片 owner_metrics 批量补充失败: %s", exc)
            for item in items:
                item.setdefault("owner_metrics", None)
        return {
            "success": True,
            "data": {
                "algorithm_version": service.detector.version,
                "items": items,
                "sample_count": len(snapshots),
            },
        }
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生命周期分析失败: {exc}") from exc


@router.get("/lifecycle/timeline")
async def get_lifecycle_timeline(bvid: str = Query(...)):
    """返回单个视频的统计历史时间轴，供前端详情区绘制趋势。"""
    session = get_session()
    try:
        video = session.query(Video).filter(Video.bvid == bvid).first()
        if video is None:
            raise HTTPException(status_code=404, detail="视频不存在，请先采集")
        rows = (
            session.query(VideoStats)
            .filter(VideoStats.video_id == video.id)
            .order_by(VideoStats.snapshot_time.asc())
            .all()
        )
        return {
            "success": True,
            "data": {
                "bvid": bvid,
                "title": video.title or "",
                "points": [
                    {
                        "time": row.snapshot_time.isoformat(timespec="seconds") if row.snapshot_time else None,
                        "view": row.view,
                        "danmaku": row.danmaku,
                        "reply": row.reply,
                        "like": row.like,
                    }
                    for row in rows
                ],
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取时间轴失败: {exc}") from exc
    finally:
        session.close()


@router.get("/lifecycle/accounts")
async def get_lifecycle_accounts(bvid: str = Query(...)):
    """返回热点视频 UP 主的关联数据，供"查看上涨账号"抽屉使用。"""
    session = get_session()
    try:
        video = session.query(Video).filter(Video.bvid == bvid).first()
        if video is None or not video.mid:
            raise HTTPException(status_code=404, detail="视频或 UP 主不存在")
        mid = int(video.mid)
    finally:
        session.close()
    try:
        data = await get_up_relation(get_api(), mid)
        try:
            # 抽屉与 UP 分析共用充电人数接口，失败时保留明确的不可用标记。
            charge_data = await get_api().get_charge_count(mid)
            data['charge_count'] = int(charge_data.get('charge_count') or 0)
            data['charge_source'] = charge_data.get('source', 'unavailable')
        except Exception as exc:
            data['charge_count'] = None
            data['charge_source'] = 'unavailable'
            import logging
            logging.getLogger(__name__).warning("上涨账号充电人数获取失败 mid=%s: %s", mid, exc)
        return {"success": True, "data": data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取账号数据失败: {exc}") from exc


@router.post("/collect")
async def start_collect(
    tid: int = Query(default=4),
    limit: int = Query(default=20, ge=1, le=200),
    min_view: int = Query(default=0, ge=0),
    sample_comments: bool = Query(default=True),
    sample_danmaku: bool = Query(default=True),
):
    """触发一轮热点采集，后台异步执行，进度由 collect/progress 轮询。

    注意：limit 上限 200 对应榜单翻页（ranking/v2 单页 50 条），
    超过 50 会额外消耗请求预算，普通场景建议保持 20-50。
    min_view 仅绘画分区生效：搜索主采播放量下限（0=不限，10000/50000/100000 等）。
    """
    global _collect_task
    # 已有任务运行时直接返回当前进度，避免并发采集打穿预算。
    if _collect_task is not None and not _collect_task.done():
        return {"success": True, "data": {"started": False, "message": "已有采集任务进行中"}}
    collector = HotspotCollector(api=get_api())

    async def _run() -> None:
        try:
            await collector.collect(
                tid=tid,
                limit=limit,
                min_view=min_view,
                sample_comments=sample_comments,
                sample_danmaku=sample_danmaku,
            )
        except Exception as exc:
            # 采集异常已由采集器写入进度状态，此处仅记录日志。
            import logging
            logging.getLogger(__name__).error("后台采集任务异常: %s", exc)

    _collect_task = asyncio.create_task(_run())
    return {
        "success": True,
        "data": {
            "started": True,
            "message": f"采集已启动：分区 {tid}，上限 {limit} 个视频",
        },
    }


@router.get("/collect/progress")
async def get_collect_progress() -> dict[str, Any]:
    """返回采集分页批次进度，前端可轮询。"""
    return {"success": True, "data": get_progress()}
