"""热点生命周期、采集进度、账号关联与时间轴 API。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import HTTPException, Query

from core.data_quality import parse_count
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


#: 时间轴需要返回质量状态的指标集合（与 heuristic_v1 读取的字段一致）。
_TIMELINE_KEYS = ("view", "danmaku", "reply", "like")

#: metric_status / view_status 内允许的显式三态。
_VALID_METRIC_STATES = ("ok", "missing", "invalid")


def _resolve_read_status(stats, key: str) -> tuple[int | None, str]:
    """按 03 规格 §4.3 的质量读取优先级解析单个快照字段。

    优先级（对应规格 §4.3 第 1—5 条）：

    1. ``metric_status`` 存在且含该 key -> 使用该状态；
    2. 存在但缺 key -> unknown，不继承 ok；
    3. 整张 ``metric_status`` 为 NULL 且字段为 view、``view_status`` 明确 -> 回退 view_status；
    4. 非 view 指标且旧 ``stat_status='ok'`` -> 仅当数值本身合法时视为完整证据；
    5. 其余 -> unknown。

    Args:
        stats: ``VideoStats`` 行对象（可能为旧记录，质量列全为 NULL）。
        key: 待解析字段名，如 ``"view"``。

    Returns:
        ``(value, status)``：``value`` 为合法非负整数或 ``None``；
        ``status`` 取 ok / missing / invalid / unknown / inconsistent_quality。
    """
    raw_value = getattr(stats, key, None)
    metric_status = getattr(stats, "metric_status", None)

    # metric_status 非 NULL 但不是 dict：整体判 invalid，禁止走 legacy 回退。
    if metric_status is not None and not isinstance(metric_status, dict):
        return None, "invalid"

    if isinstance(metric_status, dict):
        if key not in metric_status:
            return None, "unknown"  # 规则2：缺 key 不继承 ok
        stated = metric_status.get(key)
        # view 与 view_status 同时明确却矛盾时保守标 inconsistent_quality。
        if key == "view":
            view_status = getattr(stats, "view_status", None)
            if (
                isinstance(view_status, str)
                and view_status in _VALID_METRIC_STATES
                and stated in _VALID_METRIC_STATES
                and view_status != stated
            ):
                return None, "inconsistent_quality"
        if stated == "ok":
            value, parsed = parse_count(raw_value)
            if parsed != "ok":
                return None, parsed  # 显式 ok 也不能让 NULL/非法值有效
            return value, "ok"
        if stated in ("missing", "invalid"):
            return None, stated
        return None, "invalid"  # 未知状态值

    # ---- 以下为 metric_status 整张为 NULL 的旧记录 ----
    if key == "view":
        view_status = getattr(stats, "view_status", None)
        if isinstance(view_status, str) and view_status in _VALID_METRIC_STATES:
            if view_status == "ok":
                value, parsed = parse_count(raw_value)
                return (value, "ok") if parsed == "ok" else (None, parsed)
            return None, view_status
        return None, "unknown"  # 连 view_status 也 NULL -> 未知（§3.3）

    # 非 view 指标：partial / NULL 的 stat_status 都不能推定具体指标有效。
    stat_status = getattr(stats, "stat_status", None)
    if stat_status == "ok":
        value, parsed = parse_count(raw_value)
        return (value, "ok") if parsed == "ok" else (None, parsed)
    return None, "unknown"


def _load_snapshots(tid: int | None = None, bvid: str | None = None) -> list[Snapshot]:
    """从视频统计历史读取算法快照，附带质量 marker 与采集 epoch。

    与旧实现的关键差异（规格 §4.4）：不再把 NULL 播放量兜底成 0。质量非 ok 的
    记录保留为 ``view=None`` 的 marker，并沿用其已知 ``captured_epoch_s``，供 02
    生命周期 adapter 作为断段屏障消费（03 只负责把 marker/epoch 传到读端，
    不在本文件实现 02 的 lifecycle_v2 算法）。

    Args:
        tid: 可选分区筛选（采集归属分区）。
        bvid: 可选 BV 号筛选。

    Returns:
        list[Snapshot]，按 ``snapshot_time`` 升序；质量非 ok 的 marker ``view`` 为 None。
    """
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
            # 质量感知：只有解析为 ok 的整数播放量才进入 Snapshot.view。
            view_value, view_quality = _resolve_read_status(stats, "view")
            raw_view, _ = parse_count(getattr(stats, "view", None))
            epoch = getattr(stats, "captured_epoch_s", None)
            epoch = epoch if type(epoch) is int else None
            captured_at = stats.snapshot_time
            if captured_at is None:
                # 仅在有明确 epoch 时做本地显示兜底；不凭机器时区解释旧 naive 时间。
                captured_at = datetime.fromtimestamp(epoch) if epoch is not None else datetime.now()
            rows.append(Snapshot(
                bvid=video.bvid,
                tid=int(video.tid or 0),
                captured_at=captured_at,
                view=view_value,  # None 表示质量非 ok 的 marker，不再兜底成 0
                title=video.title or "",
                owner_mid=int(video.mid or 0),
                owner_name=video.author or "",
                source="video_stats",
                captured_epoch_s=epoch,
                view_quality=view_quality,
                raw_view=raw_view,
                metric_status=stats.metric_status if isinstance(getattr(stats, "metric_status", None), dict) else None,
                collection_tid=stats.collection_tid if type(getattr(stats, "collection_tid", None)) is int else None,
                raw_tid=stats.raw_tid if type(getattr(stats, "raw_tid", None)) is int else None,
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
        # 规格 §4.4：heuristic_v1 回放只接受整数有效快照；质量非 ok 的记录作为
        # marker 单独计数上报，不把 None 送进旧算式造成 TypeError。
        valid_snapshots = [snapshot for snapshot in snapshots if type(snapshot.view) is int]
        rejected_count = len(snapshots) - len(valid_snapshots)
        service = HotspotService(algorithm_name=algorithm)
        items = service.analyze(valid_snapshots)
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
                "valid_count": len(valid_snapshots),
                "rejected_count": rejected_count,
                # 缺失屏障不可表达时如实标注回放受限，不宣称完整可靠。
                "history_limited": rejected_count > 0,
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
        points: list[dict[str, Any]] = []
        for row in rows:
            # 质量感知：每指标返回有效值或 null，原始库值另放 raw，避免把 missing
            # 状态 0 画成真实 0（规格 §4.4）。
            values: dict[str, int | None] = {}
            raw: dict[str, int | None] = {}
            status: dict[str, str] = {}
            for key in _TIMELINE_KEYS:
                value, state = _resolve_read_status(row, key)
                values[key] = value
                raw[key] = parse_count(getattr(row, key, None))[0]
                status[key] = state
            epoch = getattr(row, "captured_epoch_s", None)
            points.append({
                "time": row.snapshot_time.isoformat(timespec="seconds") if row.snapshot_time else None,
                "captured_epoch_s": epoch if type(epoch) is int else None,
                "view": values["view"],
                "danmaku": values["danmaku"],
                "reply": values["reply"],
                "like": values["like"],
                "status": status,
                "raw": raw,
                "metric_status": row.metric_status if isinstance(getattr(row, "metric_status", None), dict) else None,
            })
        return {
            "success": True,
            "data": {
                "bvid": bvid,
                "title": video.title or "",
                "points": points,
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
