"""单视频跟踪（``hotspot_watch``）管理 API（FishTool 02 · 批 4）。

职责：把 02 批 2 / 批 3 落地的 watch 数据与服务通过 Web API 暴露出去：

- ``GET  /watch``                列表（支持 ``state`` 三态过滤 + ``limit``/``offset`` 分页）；
- ``GET  /watch/{bvid}``         单目标详情（含 ``state_json`` / ``failure_count`` /
  ``last_error_code`` / ``next_due_epoch_s`` 等）；
- ``POST /watch``                手动加 watch（``bvid`` 必填，``collection_tid`` /
  ``sample_interval_s`` 可选；重复 bvid 走库里幂等 UPSERT，不炸）；
- ``POST /watch/{bvid}/release`` 手动停追（写 ``stop_reason='manual_stop'`` +
  ``released_epoch_s``，``active=0``）。

口径（批 4 硬要求，照单实现）：
- 三态判定**唯一复用** ``modules.hotspot.watch_store.classify_state``；本文件不另写
  一套 ``active`` / ``ttl`` 判断，避免两套判断早晚漂移；
- 手动停追写 ``manual_stop``；自动到期释放由 ``watch_store.release_expired`` 负责，
  本文件一行不碰，故自动到期行的原因码不受手动路径影响（重复调用为幂等空操作）；
- 列表每项带**算好的** ``state`` 字段（由 ``classify_state(row, now)`` 现算下发），
  前端不自行判断三态；
- 时间字段一律 ``*_epoch_s`` 原样透出，本层**不做任何格式化 / 时区换算**。

事务口径：读路径只查不改；写路径由本文件 ``commit``（``watch_store`` 只 ``flush``，
不 commit / rollback / close，事务由调用方持有）。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException, Query
from sqlalchemy import update

from core.database import HotspotWatch, get_session
from core.logger import get_logger
from modules.hotspot.watch_store import WatchState, classify_state, upsert_watch

from . import router
from .schemas import WatchCreateRequest

logger = get_logger(__name__)

#: ``/watch`` 链路**恒用** ``algorithm/lifecycle_v2.LifecycleV2``：编排层缺省构造器
#: ``watch_service._build_detector`` 与 ``watch_service.DEFAULT_DETECTOR`` 都硬绑该类。
#: 故本层版本号**写死取自该链路**，**不经算法注册表**——注册表默认是 ``heuristic_v1``，
#: 且其注册名与 watch 实际算法解耦，走注册表会引入「报错版本 / KeyError」风险。
#: 单测钉死本常量与 ``LifecycleV2().version`` 等值，防漂移。
_WATCH_ALGORITHM_VERSION = "lifecycle_v2"

#: state 过滤合法取值：唯一来源是 watch_store 的 ``WatchState`` 枚举，不在此另造态名。
_WATCH_STATE_VALUES = frozenset(state.value for state in WatchState)


def _now_epoch_s() -> int:
    """返回当前 UTC 秒级时间戳（本模块唯一取时入口，便于测试注入固定时钟）。

    Returns:
        秒级 int 时间戳。
    """
    return int(time.time())


def _serialize_watch(row: HotspotWatch, now_epoch_s: int) -> dict[str, Any]:
    """把一行 ``hotspot_watch`` 序列化为 API DTO。

    时间字段一律 ``*_epoch_s`` 原样透出、不做格式化；``state`` 由
    ``classify_state(row, now)`` 现算，保证与算法层三态判定同源。

    Args:
        row: ``hotspot_watch`` ORM 行。
        now_epoch_s: 判定时刻（UTC 秒）。

    Returns:
        可直接 JSON 化的 dict（字段名与表列一一对应，附算好的 ``state``）。
    """
    return {
        "bvid": row.bvid,
        "category_key": row.category_key,
        "collection_tid": row.collection_tid,
        "discovery_source": row.discovery_source,
        "active": bool(row.active),
        "stop_reason": row.stop_reason,
        "first_seen_epoch_s": row.first_seen_epoch_s,
        "last_seen_epoch_s": row.last_seen_epoch_s,
        "ttl_end_epoch_s": row.ttl_end_epoch_s,
        "released_epoch_s": row.released_epoch_s,
        "next_due_epoch_s": row.next_due_epoch_s,
        "last_attempt_epoch_s": row.last_attempt_epoch_s,
        "last_success_epoch_s": row.last_success_epoch_s,
        "failure_count": row.failure_count,
        "last_error_code": row.last_error_code,
        "sample_interval_s": row.sample_interval_s,
        "last_evaluation_epoch_s": row.last_evaluation_epoch_s,
        "last_confirmed_stage": row.last_confirmed_stage,
        "state_json": row.state_json,
        "state_revision": row.state_revision,
        "coverage_ratio": row.coverage_ratio,
        "coverage_state": row.coverage_state,
        "state": classify_state(row, now_epoch_s).value,
    }


def _serialize_watch_dto(row: HotspotWatch, now_epoch_s: int) -> dict[str, Any]:
    """序列化一行 watch 并附上**链路算法版本号**，供 /watch 系列端点统一下发。

    版本号**写死取自 watch 链路**（恒 ``LifecycleV2``）；不改动 :func:`_serialize_watch`
    本身（其契约是「字段名与表列一一对应」）。纯加字段，不改既有字段名与语义。

    Args:
        row: ``hotspot_watch`` ORM 行。
        now_epoch_s: 判定时刻（UTC 秒）。

    Returns:
        dict: :func:`_serialize_watch` 的结果外加 ``algorithm_version``。
    """
    dto = _serialize_watch(row, now_epoch_s)
    dto["algorithm_version"] = _WATCH_ALGORITHM_VERSION
    return dto


@router.get("/watch")
async def list_watch(
    state: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列出跟踪目标，支持三态过滤与分页。

    Args:
        state: 可选过滤值，取值 ``tracking`` / ``expired`` / ``released``；缺省返回全部。
        limit: 单页条数，1—200，默认 50。
        offset: 起始偏移，>=0，默认 0。

    Returns:
        ``{"success": True, "data": {"items": [...], "total": N, "limit": L, "offset": O}}``。
        其中 ``total`` 是**过滤后**的匹配总数，``items`` 是「先过滤、后分页」的切片。

    Raises:
        HTTPException: state 非法 400；库读取失败 500。
    """
    if state is not None and state not in _WATCH_STATE_VALUES:
        raise HTTPException(status_code=400, detail=f"未知的 state 过滤值: {state}")

    now_epoch_s = _now_epoch_s()
    session = get_session()
    try:
        # 三态过滤一律走 classify_state（逐行现算），不在 SQL 里另写一套 active/ttl 判断。
        rows = session.query(HotspotWatch).order_by(HotspotWatch.id.asc()).all()
        matched: list[dict[str, Any]] = []
        for row in rows:
            item = _serialize_watch(row, now_epoch_s)
            if state is None or item["state"] == state:
                matched.append(item)
        # 先按 state 过滤，再对逻辑结果集做分页，避免「SQL 先分页后过滤」造成漏项。
        page = matched[offset: offset + limit]
        return {
            "success": True,
            "data": {
                "items": page,
                "total": len(matched),
                "limit": limit,
                "offset": offset,
                # 来源字段：与本页每个目标所用算法同源（watch 链路恒 LifecycleV2）。
                "algorithm_version": _WATCH_ALGORITHM_VERSION,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("读取跟踪列表失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"读取跟踪列表失败: {exc}") from exc
    finally:
        session.close()


@router.get("/watch/{bvid}")
async def get_watch(bvid: str):
    """返回单个跟踪目标的详情。

    Args:
        bvid: 视频 BV 号（路径参数）。

    Returns:
        ``{"success": True, "data": {...}}``；data 含 ``state_json``、``failure_count``、
        ``last_error_code``、``next_due_epoch_s`` 等全部列与算好的 ``state``。

    Raises:
        HTTPException: 目标不存在 404；库读取失败 500。
    """
    now_epoch_s = _now_epoch_s()
    session = get_session()
    try:
        row = session.query(HotspotWatch).filter(HotspotWatch.bvid == bvid).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"跟踪目标不存在: {bvid}")
        return {"success": True, "data": _serialize_watch_dto(row, now_epoch_s)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("读取跟踪目标失败 bvid=%s: %s", bvid, exc)
        raise HTTPException(status_code=500, detail=f"读取跟踪目标失败: {exc}") from exc
    finally:
        session.close()


@router.post("/watch")
async def create_watch(payload: WatchCreateRequest):
    """手动加入一个跟踪目标（重复 bvid 走幂等 UPSERT，不报错、不重置调度）。

    Args:
        payload: ``WatchCreateRequest``；``bvid`` 必填，``collection_tid`` /
            ``sample_interval_s`` 可选（缺省时沿用库内既有值 / watch_store 默认 3600）。

    Returns:
        ``{"success": True, "data": {...}}``，data 为入库后的行；重复 bvid 时返回既有行，
        幂等结果一致。

    Raises:
        HTTPException: bvid 非法 400；写库失败 500。
    """
    now_epoch_s = _now_epoch_s()
    session = get_session()
    try:
        kwargs: dict[str, Any] = {"bvid": payload.bvid, "now_epoch_s": now_epoch_s}
        if payload.collection_tid is not None:
            kwargs["collection_tid"] = payload.collection_tid
        if payload.sample_interval_s is not None:
            kwargs["sample_interval_s"] = payload.sample_interval_s
        # upsert_watch 只 flush；幂等由 bvid UNIQUE + ON CONFLICT 保证，重复不产生第二行。
        row = upsert_watch(session, **kwargs)
        session.commit()
        session.refresh(row)
        return {"success": True, "data": _serialize_watch_dto(row, now_epoch_s)}
    except ValueError as exc:
        session.rollback()
        logger.warning("加入跟踪参数非法 bvid=%s: %s", payload.bvid, exc)
        raise HTTPException(status_code=400, detail=f"非法的跟踪参数: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        logger.error("加入跟踪失败 bvid=%s: %s", payload.bvid, exc)
        raise HTTPException(status_code=500, detail=f"加入跟踪失败: {exc}") from exc
    finally:
        session.close()


@router.post("/watch/{bvid}/release")
async def release_watch(bvid: str):
    """手动停追：把仍在池中的目标置为「已释放」，写 ``stop_reason='manual_stop'``。

    口径：只有**手动停追**才写 ``manual_stop``；自动到期释放由
    ``watch_store.release_expired`` 负责，本接口不碰，因此已释放行（无论自动还是手动）
    不会被本接口再次改写原因码——对已释放行重复调用是幂等空操作。

    Args:
        bvid: 视频 BV 号（路径参数）。

    Returns:
        ``{"success": True, "data": {...}}``，data 为释放后的行（``state`` 应为 released）。

    Raises:
        HTTPException: 目标不存在 404；写库失败 500。
    """
    now_epoch_s = _now_epoch_s()
    session = get_session()
    try:
        exists = session.query(HotspotWatch.id).filter(HotspotWatch.bvid == bvid).first()
        if exists is None:
            raise HTTPException(status_code=404, detail=f"跟踪目标不存在: {bvid}")
        # 条件 UPDATE：只释放仍在池中的行，不覆盖已释放行原有的 stop_reason；
        # 释放改变调度状态，按 02 §0 裁定一同事务内 state_revision + 1（fence 迟到写入）。
        session.execute(
            update(HotspotWatch)
            .where(HotspotWatch.bvid == bvid, HotspotWatch.active.is_(True))
            .values(
                active=False,
                stop_reason="manual_stop",
                released_epoch_s=now_epoch_s,
                state_revision=HotspotWatch.state_revision + 1,
            )
        )
        session.commit()
        row = session.query(HotspotWatch).filter(HotspotWatch.bvid == bvid).one()
        return {"success": True, "data": _serialize_watch_dto(row, now_epoch_s)}
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        logger.error("停止跟踪失败 bvid=%s: %s", bvid, exc)
        raise HTTPException(status_code=500, detail=f"停止跟踪失败: {exc}") from exc
    finally:
        session.close()
