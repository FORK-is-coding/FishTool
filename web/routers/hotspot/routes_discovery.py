"""04 第一批 · 热点发现首屏「只读接线」路由（FishTool 04 · R5 第一批「首屏有内容」）。

本文件是 06（采集广度 · 聚合入口发现通道）与 Web 前端之间的**只读接线层**：
不新增采集、不新增表、不新增配额，数据全部来自 06 已经跑过的那一轮发现。

端点
----
- ``GET  /discovery/latest``          只读：最近一轮发现结果；无快照时 ``snapshot: null``。
- ``POST /discovery/research_draft``  低成本研究草案：复用 ``topic_generator`` 的降级模板
  路径，以最近一轮**热搜词**为素材生成草案；**零出站 HTTP**（不触发任何采集）。

数据来源（全部为 06 现成方法，本文件只读、只调用，**不改任何签名**）
------------------------------------------------------------------
- ``DiscoveryService.iter_video_candidates()``   —— 合并后的视频候选（来源全留 + ``conflict`` 标记）；
- ``DiscoveryService.list_keyword_candidates()`` —— 热搜词候选（``BroadKeyword``）；
- ``DiscoveryService.latest_sources_state()``    —— 各来源运行态摘要；
- ``DiscoveryService.snapshot_store.latest()``   —— 一轮快照元数据（采样时刻 / 是否命中缓存 / others 计数）。

硬口径（R5 §3，写死在本层，不许走样）
-------------------------------------
1. 质量三态 ``ok`` / ``missing`` / ``invalid``；未知值一律 ``None``，**禁止补 0**；
2. ``sources`` 原样透出 06 ``SourceRun.to_snapshot()`` 摘要，**不在前后端合并成单一 state**；
   ``state=error``（异常）与 ``item_count=0``（真实空榜）各自独立透出，绝不互相掩盖；
3. 展示值来源优先级固定 ``ranking_all > popular > ranking_all_others``，由 06
   ``merge_video_candidates`` 决定，本层**不做任何重排、绝不按播放量挑边**；
4. ``heat_score`` 只叫「平台接口返回热搜分数」，本层不得改叫播放量 / 搜索人数 / 独立用户数；
5. ``pid_v2`` / ``tidv2`` / legacy ``tid`` 各显各的，**不做「统一 tid」**；
6. ``served_from_cache`` 必须透出，让前端标明「新抓 / 上轮缓存」。

只读保证：本文件不 import 任何网络客户端、不构造任何 B 站 API 客户端；
``_resolve_discovery_service`` 只读取已存在的服务实例（或惰性装配一个**不触发轮询**的
默认实例），全程不发任何出站请求。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from modules.hotspot import TopicGenerator

from . import router

logger = logging.getLogger(__name__)

#: 研究草案素材（热搜词）最多取前 N 条。
_DRAFT_MAX_TAGS = 15


class ResearchDraftRequest(BaseModel):
    """低成本研究草案请求（字段全部可选，缺省给中性值，不做任何采集）。"""

    direction: str = Field(default="", description="创作方向，可空")
    zone_name: str = Field(default="", description="分区名，仅作展示标签，不做任何采集")
    count: int = Field(default=5, ge=1, le=20, description="草案条数（1-20）")


class _NoCollectionTagGenerator:
    """草案专用占位 tag 生成器。

    ``topic_generator`` 的**降级模板路径不会调用 tag 生成器**；本占位对象仅用于满足
    ``TopicGenerator.__init__`` 的装配要求，任何真实调用都会立即抛错，
    从结构上保证「研究草案」永不触发采集。
    """

    def __getattr__(self, name: str):  # pragma: no cover - 结构性护栏
        raise RuntimeError(f"research_draft_no_collection: 草案路径不得调用 tag 生成器({name})")


#: 本模块惰性兜底发现服务（仅在拿不到常驻服务实例时使用；只读、不轮询）。
_fallback_service: Any = None


def _resolve_discovery_service() -> Optional[Any]:
    """获取发现服务实例（**只读**，绝不触发任何采集）。

    优先复用常驻监控服务里已经装配好的 ``DiscoveryService``（若发现循环正在跑，
    那就是本轮共享轮询缓存所在实例）；拿不到时常驻装配一个**只读**默认实例
    （只用于读快照与内存候选，**绝不调用 ``poll_once``**，故无任何出站 HTTP）。

    Returns:
        Optional[DiscoveryService]: 可用的发现服务实例；所有路径都失败时返回 None。
    """
    # 1) 常驻监控服务（``web.main`` 全局，lifespan 启动时创建）。运行时惰性导入，避免循环依赖。
    try:
        from ...main import monitor_service  # noqa: WPS433
    except Exception:  # noqa: BLE001 - 拿不到主应用时降级到兜底实例
        monitor_service = None
    if monitor_service is not None:
        try:
            return monitor_service.discovery_service
        except Exception as exc:  # noqa: BLE001 - 单点失败不阻断只读端点
            logger.warning("读取常驻发现服务失败，改用兜底只读实例: %r", exc)

    # 2) 兜底：模块级惰性默认实例（进程内缓存，避免每个请求重复装配）。
    global _fallback_service
    if _fallback_service is None:
        try:
            from modules.hotspot.discovery import build_discovery_service

            _fallback_service = build_discovery_service()
        except Exception as exc:  # noqa: BLE001
            logger.error("装配兜底发现服务失败: %r", exc)
            return None
    return _fallback_service


def _read_snapshot_meta(service: Optional[Any]) -> Optional[Dict[str, Any]]:
    """读取最近一轮快照元数据（``snapshot_store.latest()``）。

    Args:
        service: 发现服务实例，可为 None。

    Returns:
        Optional[Dict[str, Any]]: 快照 dict；无快照 / 读取失败时返回 None（不抛异常）。
    """
    if service is None:
        return None
    try:
        store = getattr(service, "snapshot_store", None)
        latest = store.latest() if store is not None else None
    except Exception as exc:  # noqa: BLE001 - 快照读失败按「无快照」处理，不阻断端点
        logger.warning("读取发现快照失败: %r", exc)
        return None
    return latest if isinstance(latest, dict) else None


def _read_video_candidates(service: Optional[Any]) -> List[Dict[str, Any]]:
    """读取合并后的视频候选（06 只读消费接口 ``iter_video_candidates``）。

    Args:
        service: 发现服务实例，可为 None。

    Returns:
        List[Dict[str, Any]]: 每个 bvid 一条、来源全留的合并结果（读取失败返回空列表）。
    """
    if service is None:
        return []
    try:
        items = service.iter_video_candidates()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取视频候选失败: %r", exc)
        return []
    return [dict(item) for item in (items or []) if isinstance(item, dict)]


def _read_keyword_candidates(service: Optional[Any]) -> List[Dict[str, Any]]:
    """读取热搜词候选（06 只读消费接口 ``list_keyword_candidates``）。

    Args:
        service: 发现服务实例，可为 None。

    Returns:
        List[Dict[str, Any]]: ``BroadKeyword.to_dict()`` 列表（读取失败返回空列表）。
    """
    if service is None:
        return []
    try:
        items = service.list_keyword_candidates()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取热搜词候选失败: %r", exc)
        return []
    candidates: List[Dict[str, Any]] = []
    for item in items or []:
        if hasattr(item, "to_dict"):
            candidates.append(item.to_dict())
        elif isinstance(item, dict):
            candidates.append(dict(item))
    return candidates


def _read_sources_state(service: Optional[Any]) -> Dict[str, Any]:
    """读取各来源运行态摘要（06 只读消费接口 ``latest_sources_state``）。

    **原样透出**：不合并 ``state``、不改字段名、不补 0；每个来源各自带
    ``state`` / ``item_count`` / ``returned_count`` / ``error_code`` / ``reason`` / ``from_cache``。

    Args:
        service: 发现服务实例，可为 None。

    Returns:
        Dict[str, Any]: ``{source: 摘要}``；无快照 / 读取失败返回空 dict。
    """
    if service is None:
        return {}
    try:
        sources = service.latest_sources_state()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取来源运行态失败: %r", exc)
        return {}
    return sources if isinstance(sources, dict) else {}


@router.get("/discovery/latest")
async def get_discovery_latest():
    """返回最近一轮发现结果（只读；无快照时 ``snapshot: null``，**绝不伪造空对象**）。

    Returns:
        ``{"success": True, "data": {"snapshot": {...} | None}}``。snapshot 含：

        - ``captured_epoch_s``：最近一轮采样时刻（UTC 秒）；
        - ``served_from_cache``：整轮是否全部命中共享缓存（新抓 / 上轮缓存）；
        - ``sources``：**逐源**运行态摘要（原样透出 06 ``to_snapshot()``，不合并 state）；
        - ``video_count`` / ``keyword_count``：与下方列表长度一致；
        - ``others_count``：``ranking`` 的 ``others`` 原始条数（缺则 None，不补 0）；
        - ``videos``：合并后视频候选（来源全留 + ``conflict`` 标记）；
        - ``keywords``：热搜词候选。
    """
    service = _resolve_discovery_service()
    snapshot_meta = _read_snapshot_meta(service)
    if snapshot_meta is None:
        # 尚未跑过任何一轮发现：如实返回 null，让前端显示「尚未跑过发现轮」，
        # 不返回 {} / 0 之类的伪造结构。
        return {"success": True, "data": {"snapshot": None}}

    videos = _read_video_candidates(service)
    keywords = _read_keyword_candidates(service)
    sources = _read_sources_state(service)

    snapshot: Dict[str, Any] = {
        "captured_epoch_s": snapshot_meta.get("captured_epoch_s"),
        "served_from_cache": snapshot_meta.get("served_from_cache"),
        "sources": sources,
        "video_count": len(videos),
        "keyword_count": len(keywords),
        "others_count": snapshot_meta.get("others_count"),
        "videos": videos,
        "keywords": keywords,
    }
    return {"success": True, "data": {"snapshot": snapshot}}


@router.post("/discovery/research_draft")
async def create_research_draft(payload: ResearchDraftRequest):
    """生成「低成本研究草案」（复用 ``topic_generator`` 降级模板路径，**零采集**）。

    素材 = 最近一轮热搜词的 ``keyword``（来自 06 共享轮询结果，本层**不再发任何请求**）。
    无素材时如实返回空草案 + ``reason``，不伪造内容。

    Args:
        payload: ``ResearchDraftRequest``；``direction`` / ``zone_name`` / ``count`` 均可选。

    Returns:
        ``{"success": True, "data": {"used_llm": False, "mode": "fallback_template",
        "hot_tags": [...], "topics": [...], "count": N}}``。

    Raises:
        HTTPException: 降级模板生成异常时 500。
    """
    service = _resolve_discovery_service()
    keywords = _read_keyword_candidates(service)
    hot_tags = [
        item["keyword"]
        for item in keywords
        if isinstance(item.get("keyword"), str) and item["keyword"].strip()
    ][:_DRAFT_MAX_TAGS]

    if not hot_tags:
        # 无素材不伪造：明确告知前端「没有可用热搜词」，草案为空。
        return {
            "success": True,
            "data": {
                "used_llm": False,
                "mode": "fallback_template",
                "hot_tags": [],
                "topics": [],
                "count": 0,
                "reason": "no_keyword_candidates",
            },
        }

    try:
        # 只走降级模板路径：不配置 LLM、不构造 B 站客户端、不调用任何采集接口。
        generator = TopicGenerator(
            api=None, llm_client=None, tag_generator=_NoCollectionTagGenerator()
        )
        topics = await generator.generate_topics_fallback(
            direction=payload.direction.strip() or "当前热点",
            zone_name=payload.zone_name.strip() or "全站",
            hot_tags=hot_tags,
            count=int(payload.count),
        )
    except Exception as exc:  # noqa: BLE001 - 统一转 500，便于前端提示
        logger.error("生成低成本研究草案失败: %r", exc)
        raise HTTPException(status_code=500, detail=f"生成研究草案失败: {exc}") from exc

    return {
        "success": True,
        "data": {
            "used_llm": False,
            "mode": "fallback_template",
            "hot_tags": hot_tags,
            "topics": topics,
            "count": len(topics),
        },
    }
