"""热点发现模块 - AI 选题生成接口（第三批 g：接入生成账本 / 幂等键）。

拆分自 hotspot.py 原始 L535-L662；第三批 g 在此扩展 **带键** 路径：

- 无键 tag_only：**保留原行为**，功能可用，但**不宣称具备幂等保证**；
- 带键（``generation_request_id`` / ``opportunity_run_id``）：转
  :class:`TopicGenerationService`，返回 **200 已完成 / 202 生成中 / 409 键冲突**；
- 新增只读 ``GET /topics/generation-runs/{generation_request_id}`` 生成账本，
  **不触发生成、不隐式重试**；``404`` 表示尚无该键。
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from modules.hotspot.topic_generation_service import (
    GenerationKeyConflict,
    GenerationRequest,
    GenerationTerminalError,
    GenerationTimeoutError,
    GenerationUnavailable,
    GenerationValidationError,
    TopicGenerationService,
)

from . import router
from .deps import TopicGenerator, get_api, get_llm_client
from .schemas import TopicGenerateRequest

#: 生成编排服务（由 ``web/main.py`` lifespan 装配；测试可注入）。
_generation_service: Optional[TopicGenerationService] = None


def set_generation_service(service: Optional[TopicGenerationService]) -> None:
    """装配生成编排服务（lifespan / 测试调用）。

    Args:
        service: ``TopicGenerationService``；``None`` 表示未装配。
    """
    global _generation_service
    _generation_service = service


def get_generation_service() -> Optional[TopicGenerationService]:
    """返回已装配的生成编排服务（未装配为 ``None``）。"""
    return _generation_service


async def _legacy_generate(request: TopicGenerateRequest):
    """旧无键 tag_only 路径：行为与拆分前一致（无幂等保证）。

    Args:
        request: 旧请求模型。

    Returns:
        dict: ``{"success": True, "data": <原生成结果>}``。

    Raises:
        HTTPException: 500 —— 生成失败。
    """
    try:
        # 获取API客户端实例
        api = get_api()
        # 获取LLM客户端（可能为None）
        llm_client = get_llm_client()
        # 创建选题生成器
        generator = TopicGenerator(api, llm_client)
        # 生成选题（异步调用）
        result = await generator.generate_topics(
            direction=request.direction,
            zone_name=request.zone_name,
            count=request.count,
            use_llm=request.use_llm,
        )
        return {"success": True, "data": result}
    except Exception as e:
        # 生成失败（LLM调用失败、数据获取失败等）
        raise HTTPException(status_code=500, detail=f"生成选题失败: {str(e)}")


@router.post("/topics/generate")
async def generate_topics(request: TopicGenerateRequest):
    """生成 AI 选题（200 已完成 / 202 生成中 / 409 键冲突）。

    - 无 ``generation_request_id`` 且无 ``opportunity_run_id`` → 旧 tag_only 路径；
    - 带键 → :class:`TopicGenerationService`；运行中返回 **202**（**不能**被旧
      ``renderTopicsResult`` 当成已生成）。

    Raises:
        HTTPException: 409 键冲突 / 终态；422 请求非法；503 状态未知；500 生成失败。
    """
    try:
        keyed = request.generation_request_id is not None or request.opportunity_run_id is not None
        if not keyed:
            return await _legacy_generate(request)

        service = get_generation_service()
        if service is None:
            raise HTTPException(status_code=503, detail={"error_code": "generation_service_unavailable"})

        generation_request = GenerationRequest(
            direction=request.direction,
            zone_name=request.zone_name,
            count=request.count,
            use_llm=request.use_llm,
            opportunity_run_id=request.opportunity_run_id,
            selected_event_ids=request.selected_event_ids,
            generation_request_id=request.generation_request_id,
            context_mode=request.context_mode,
        )
        result = await service.generate(generation_request)
        if isinstance(result, dict) and result.get("accepted"):
            # 运行中受理视图 → HTTP 202（专用 pollTopicGeneration 识别）。
            return JSONResponse(status_code=202, content={"success": True, "data": result})
        return {"success": True, "data": result}
    except HTTPException:
        # 必须排在宽泛异常之前：422 不许被吞成 500。
        raise
    except GenerationValidationError as exc:
        raise HTTPException(status_code=422, detail={"error_code": exc.code, "message": str(exc)})
    except GenerationKeyConflict as exc:
        raise HTTPException(status_code=409, detail={"error_code": exc.code, "message": str(exc)})
    except GenerationTerminalError as exc:
        raise HTTPException(status_code=409, detail={"error_code": exc.code, "message": str(exc)})
    except GenerationTimeoutError as exc:
        raise HTTPException(status_code=503, detail={"error_code": exc.code, "message": str(exc)})
    except GenerationUnavailable as exc:
        raise HTTPException(status_code=503, detail={"error_code": exc.code, "message": str(exc)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成选题失败: {str(e)}")


@router.get("/topics/generation-runs/{generation_request_id}")
async def get_generation_run(generation_request_id: str):
    """只读生成账本：running/completed/failed/cancelled/interrupted。

    ``completed`` 含持久 result，其它终态含 ``reason_code``；``404`` 表示尚无该键。
    **该 GET 不触发生成或隐式重试。**
    """
    try:
        service = get_generation_service()
        if service is None:
            raise HTTPException(status_code=503, detail={"error_code": "generation_service_unavailable"})
        view = service.store.read_state(generation_request_id)
        if view is None:
            raise HTTPException(status_code=404, detail={"error_code": "generation_run_not_found"})
        return {"success": True, "data": view}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取生成账本失败: {str(e)}")
