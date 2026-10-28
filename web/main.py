"""
B站运营工具箱 - Web API 主应用
FastAPI后端，提供RESTful API接口

本模块是 Web 服务的 FastAPI 应用模块，负责：
1. 应用生命周期管理：启动时初始化 ConfigManager，关闭时清理
# 设置初始值/默认状态，避免后续空引用
# 写入配置/属性，影响后续行为
2. CORS 配置：显式白名单，只允许本地前端地址跨域访问
3. 静态资源挂载：前端页面（HTML/JS/CSS）由 FastAPI 直接托管
4. 路由注册：热点发现、评论监控、配置管理、日志查看、
   UP分析与自诊五个业务模块统一挂载到 /api 前缀
5. WebSocket 端点：用于向桌宠等客户端实时推送预警消息

核心对象：
- app: FastAPI 应用实例，uvicorn 启动入口
# 触发服务/线程开始运行
- manager: ConnectionManager，维护 WebSocket 连接池
- push_alert: 业务侧调用的预警推送入口

运行方式：
- 统一入口：``python app.py web``（桌面版会自动以子进程方式启动它）
- 本模块只提供 FastAPI 应用对象 ``app``，不单独作为启动入口
"""
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
# 从 fastapi.staticfiles 导入符号
from fastapi.staticfiles import StaticFiles
# 从 fastapi.templating 导入符号
from fastapi.templating import Jinja2Templates
# 从 fastapi.middleware.cors 导入符号
from fastapi.middleware.cors import CORSMiddleware
# 从 contextlib 导入符号
from contextlib import asynccontextmanager
# 导入模块
import asyncio
import logging
# 导入系统模块，用于识别 PyInstaller frozen 环境。
import sys
# 导入操作系统接口，用于读取 §18.2 事件开关环境变量。
import os
# 导入类型工具
from typing import Any, Callable
# 从 pathlib 导入符号
from pathlib import Path

# 从 core.config 导入符号
from core.config import ConfigManager
from core.database import init_database
# 从 core.logger 导入符号
from core.logger import get_logger
from core.exceptions import format_exception
from core.error_policy import ErrorPriority, report_exception
from core.toast import emit_toast, register_toast_sink
from core.monitor_service import ResidentCommentMonitor
# 排名服务由本层组装（modules 不反向 import web）；client 限定自己拥有、只关自己。
from bilibili.api import BilibiliAPI
from bilibili.cookie_pool import get_cookie_pool
from bilibili.rate_limiter import get_rate_limiter
from core.database import get_session
from modules.self_diagnosis.benchmark.service import BenchmarkService
from modules.self_diagnosis.benchmark.store import BenchmarkStore
# 跨站来源判定与 benchmark 的 local_guard 共用同一套规则（含 FISHTOOL_ALLOWED_HOSTS 覆盖）
from web.local_guard import is_cross_site_origin
# 从 routers 导入符号
from .routers import (
    hotspot, comment, config as config_router, logs, analysis,
    auth as auth_router, lottery, benchmark,
)
# 本机同源校验（Host / Origin），用于配置接口防 DNS rebinding 读取
from .local_guard import require_local_read

# 统一使用 core.logger 的日志实例，避免重复配置
logger = get_logger(__name__)

# 全局配置
# 在 lifespan 启动阶段初始化，供各路由懒加载使用
config_manager = None
# 常驻评论监控服务由 FastAPI lifespan 统一创建和销毁。
monitor_service = None
# 排名服务与它拥有的 client：由 lifespan 组装 / 关闭（只关自己的 client）。
benchmark_service = None
benchmark_client = None
# 第三批 g：事件评估服务与生成编排服务（由 lifespan 装配；不另起第二份 API/watch 链）。
event_aggregation_service = None
topic_generation_service = None
# 第三批 h：事件发现服务（3c 门面，由同一 lifespan 装配；开关默认关）。
event_discovery_service = None


class _LazyBilibiliClient:
    """惰性构造的 BilibiliAPI：首次真正请求时才创建。

    启动期不读 ``config/``、不连数据库；底层限频复用 ``get_rate_limiter()`` 单例，
    因此排名的任务级预算不会改变其它任务上限。校验目标是「只关闭自己拥有的 client」。
    """

    def __init__(self) -> None:
        """初始化：此时不创建任何会话或网络资源。"""
        self._api = None

    def _ensure(self):
        """确保真实 client 已创建并返回（构造过程无网络请求）。

        Returns:
            BilibiliAPI: 共享限频器的真实 client。
        """
        if self._api is None:
            self._api = BilibiliAPI(rate_limiter=get_rate_limiter(), cookie_pool=get_cookie_pool())
        return self._api

    def __getattr__(self, name: str):
        """把任意方法访问代理给真实 client（先惰性构造）。

        Args:
            name: 被访问的属性名。

        Returns:
            可 await 的代理方法。
        """
        async def _call(*args, **kwargs):
            """构造真实 client 后转调同名方法。"""
            return await getattr(self._ensure(), name)(*args, **kwargs)

        return _call

    async def aclose(self) -> None:
        """关闭真实 client（从未创建则什么也不做）。

        Returns:
            无。
        """
        if self._api is not None:
            await self._api.close()


def _read_event_switch(config_key: str, env_name: str, *, default: bool) -> bool:
    """读取 §18.2 事件开关（环境变量 > config.yaml > 默认值）。

    Args:
        config_key: ``config.yaml`` 点号键（如 ``event_switches.auto_discovery_enabled``）。
        env_name: 对应环境变量名（如 ``EVENT_AUTO_DISCOVERY_ENABLED``）。
        default: 两者均缺失时的默认布尔值。

    Returns:
        bool: 归一后的开关值（1/true/yes/on 视为真）。
    """
    raw_env = os.environ.get(env_name)
    if raw_env is not None:
        return str(raw_env).strip().lower() in ("1", "true", "yes", "on")
    if config_manager is not None:
        raw = config_manager.get(config_key)
        if raw is not None:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return bool(default)


def _to_source_outcome(source: str, parsed: Any, now_s: int):
    """把 06 ``ParseOutcome`` 归一为 3c ``SourceOutcome``（候选只保留带 bvid 的条目）。

    Args:
        source: 来源标识（``popular`` / ``ranking`` / ``search_square``）。
        parsed: 06 解析结果（``ParseOutcome``）。
        now_s: 本轮采样时刻（UTC 秒）。

    Returns:
        SourceOutcome: 归一结果；06 视频条目缺标题等文本处**不补造**。
    """
    from modules.hotspot.event_discovery_service import SourceOutcome

    candidates: list = []
    for item in getattr(parsed, "items", None) or []:
        data = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        bvid = data.get("bvid")
        # 热搜关键词（search_square）没有 bvid，不是视频候选：只记来源状态，不硬塞候选。
        if not isinstance(bvid, str) or not bvid:
            continue
        candidates.append(
            {
                "bvid": bvid,
                "title": str(data.get("title") or ""),
                "sources": [source],
                "owner_mid": data.get("owner_mid"),
                "published_epoch_s": data.get("captured_epoch_s"),
                "raw_tid": data.get("tidv2") or data.get("legacy_tid"),
                "discovered_at_s": int(now_s),
            }
        )
    raw_code = getattr(parsed, "error_code", None)
    return SourceOutcome(
        source=source,
        state=str(getattr(parsed, "state", "ok") or "ok"),
        candidates=candidates,
        returned_count=int(getattr(parsed, "returned_count", 0) or 0),
        error_code=(str(raw_code) if raw_code not in (None, 0) else None),
        reason=getattr(parsed, "reason", None),
    )


def _build_event_discovery_fetcher():
    """构造事件发现的可注入聚合源（复用 06 现有采集能力，不新建第二套采集）。

    复用 ``modules.hotspot.discovery.sources`` 的三个既有入口与同一 ``BilibiliAPI`` 单例；
    仅在「发现已启用」时才被围栏调用（默认关闭 → 不发任何真实请求）。

    Returns:
        Callable[[int], Awaitable[list]]: 一轮读三个入口，返回 ``SourceOutcome`` 列表。
    """

    async def _fetcher(now_s: int) -> list:
        """读取 popular / ranking / search_square 三个既有入口（单入口失败不影响其它）。"""
        from modules.hotspot.discovery.sources import (
            fetch_hot_keywords,
            fetch_popular_page,
            fetch_ranking,
            parse_hot_keywords,
            parse_popular,
            parse_ranking,
        )
        from modules.hotspot.event_discovery_service import SourceOutcome
        from web.routers.hotspot.deps import get_api as _hotspot_get_api

        api = _hotspot_get_api()  # 复用既有 BilibiliAPI 单例（延迟构造，无网络）
        parsers = {
            "popular": parse_popular,
            "ranking": parse_ranking,
            "search_square": parse_hot_keywords,
        }

        async def _one(source: str, coro) -> Any:
            """执行单个入口并归一为 SourceOutcome（异常记为该来源失败）。"""
            try:
                envelope = await coro
            except Exception as exc:  # noqa: BLE001 - 外部源失败不炸整轮
                logger.warning("发现来源 %s 请求失败，本轮按错误态返回: %s", source, exc, exc_info=True)
                return SourceOutcome(
                    source=source,
                    state="error",
                    error_code=f"fetch_error:{type(exc).__name__}",
                    started_s=int(now_s),
                    finished_s=int(now_s),
                )
            parsed = parsers[source](envelope, int(now_s))
            return _to_source_outcome(source, parsed, int(now_s))

        return [
            await _one("popular", fetch_popular_page(api, page=1, ps=20)),
            await _one("ranking", fetch_ranking(api, rid=0)),
            await _one("search_square", fetch_hot_keywords(api, limit=10)),
        ]

    return _fetcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理

    启动阶段：
    # 触发服务/线程开始运行
    - 初始化全局 ConfigManager（读配置、连数据库）
    # 设置初始值/默认状态，避免后续空引用
    # 写入配置/属性，影响后续行为
    - 打日志标记服务启动完成
    # 触发服务/线程开始运行

    关闭阶段：
    # 释放连接/窗口资源
    - 打日志标记服务关闭，等待进程回收
    # 释放连接/窗口资源
    """
    global config_manager
    
    # 启动时初始化
    logger.info("Web服务启动中...")
    # 先挂 toast 桥：后续启动装配失败也能通过 /ws 告知前端与桌宠。
    toast_unregister = _install_toast_bridge()
    # 赋值并准备后续使用
    config_manager = ConfigManager()
    # Web 入口独立运行时也必须初始化数据库全局管理器
    init_database()
    # 仅接线评论区常驻监控；动态采集没有常驻任务入口。
    global monitor_service
    monitor_service = ResidentCommentMonitor(comment.get_monitor, config_manager)
    await monitor_service.start()
    # 单视频跟踪接口需要知道 watch 采样任务是否真在运行（复用 snapshot 的 watch_task_running）。
    from web.routers.hotspot import routes_watch as _routes_watch
    _routes_watch.configure_runtime(lambda: monitor_service.snapshot())

    # 排名服务：由 Web 层组装 client + service 再注入；启动期不读 config/、不连库。
    global benchmark_service, benchmark_client
    benchmark_client = _LazyBilibiliClient()
    benchmark_service = BenchmarkService(
        benchmark_client,
        BenchmarkStore(get_session),
        None,
        {'max_http_attempts': 3000},
    )
    benchmark.set_benchmark_service(benchmark_service)

    # 第三批 g：事件评估服务 + 生成编排服务（复用既有 TopicGenerator；不新建第二套 API/watch 链）。
    global event_aggregation_service, topic_generation_service, event_discovery_service
    try:
        from web.routers.hotspot import routes_events as _routes_events
        from web.routers.hotspot import routes_topics_generate as _routes_gen
        from web.routers.hotspot.deps import get_api as _hotspot_get_api
        from web.routers.hotspot.deps import get_llm_client as _hotspot_get_llm
        from modules.hotspot.events.service import EventAggregationService
        from modules.hotspot.topic_generation_service import TopicGenerationService, TopicGenerationStore
        from modules.hotspot import TopicGenerator as _HotspotTopicGenerator

        event_aggregation_service = EventAggregationService(session_factory=get_session)
        _store = TopicGenerationStore(session_factory=get_session)
        _generator = _HotspotTopicGenerator(_hotspot_get_api(), _hotspot_get_llm())
        topic_generation_service = TopicGenerationService(_generator, _store)

        # 第三批 h：事件发现服务（3c 门面）在**同一 lifespan** 装配；
        # 不另起第二份 API / watch / 清理链，fetcher 复用现有公共采集能力。
        from core.database.event_discovery_repository import EventDiscoveryRepository
        from modules.hotspot.event_discovery_fence import EventDiscoveryFence
        from modules.hotspot.event_discovery_service import (
            EventDiscoveryBatchStore as _BatchStore,
            EventDiscoveryService as _EventDiscoveryService,
            SharedDiscoveryCache as _SharedCache,
        )

        # §18.2 开关（默认关闭）：关闭时发现端点返回明确未启用状态，不发任何真实外部请求。
        auto_discovery_enabled = _read_event_switch(
            "event_switches.auto_discovery_enabled", "EVENT_AUTO_DISCOVERY_ENABLED", default=False
        )
        fast_watch_enabled = _read_event_switch(
            "event_switches.fast_watch_enabled", "EVENT_FAST_WATCH_ENABLED", default=False
        )
        _shared_cache = _SharedCache(
            _build_event_discovery_fetcher(),
            batch_store=_BatchStore(),
            ttl_s=600,
            max_candidates=100,
            policy_plan=["popular", "ranking", "search_square"],
        )
        _fence_holder: dict = {}

        async def _fence_fetch(event_id: str, rule_version: int, source_policy_hash: str):
            """围栏外部源：转发到服务真实分发（读共享缓存；同轮只读一次）。"""
            return await _fence_holder["fetch"](event_id, rule_version, source_policy_hash)

        event_discovery_service = _EventDiscoveryService(
            fence=EventDiscoveryFence(
                repository=EventDiscoveryRepository(),
                session_factory=get_session,
                fetch_fn=_fence_fetch,
            ),
            shared_cache=_shared_cache,
            session_factory=get_session,
        )
        _fence_holder["fetch"] = event_discovery_service.make_fetch_fn()

        _routes_events.configure(
            session_factory=get_session,
            aggregation_service=event_aggregation_service,
            discovery_service=event_discovery_service,
            discovery_enabled=auto_discovery_enabled,
        )
        _routes_gen.set_generation_service(topic_generation_service)
        # 恢复失联的生成任务（有界；不静默换阈值）。
        await topic_generation_service.recover_expired()
        logger.info(
            "事件 API 服务已装配（EventAggregationService / TopicGenerationService / "
            "EventDiscoveryService auto_discovery=%s fast_watch=%s）",
            auto_discovery_enabled,
            fast_watch_enabled,
        )
    except Exception as exc:  # noqa: BLE001 - 装配失败不阻塞 Web 启动，端点按契约返 503
        report_exception(
            exc,
            logger=logger,
            action="装配事件 API 服务",
            priority=ErrorPriority.HIGH,
            toast_title="事件服务不可用",
            toast_message="事件 API 服务装配失败，相关功能暂时不可用，详细原因已写入日志。",
            source="web",
        )

    logger.info("Web服务已启动")
    
    try:
        yield
    finally:
        # 分别 await 各子系统清理：排名任务 -> 评论常驻监控 -> 排名自己的 client。
        # 排名只取消 / await 自己创建的任务，绝不 close 由其它模块拥有的共享 client。
        if benchmark_service is not None:
            try:
                await benchmark_service.shutdown()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不阻塞进程退出
                logger.error("排名服务关闭失败: %s", exc, exc_info=True)
        _routes_watch.configure_runtime(None)
        if monitor_service is not None:
            await monitor_service.shutdown()
        if benchmark_client is not None:
            try:
                await benchmark_client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.error("排名 client 关闭失败: %s", exc, exc_info=True)
        # 先摘掉 toast 桥，避免关闭阶段再向已断开的 WebSocket 广播。
        toast_unregister()
        logger.info("Web服务关闭中...")


# 创建FastAPI应用
# lifespan 绑定生命周期钩子，title/description 用于 /docs 文档页展示
# 将内容呈现到界面上
app = FastAPI(
    title="B站运营工具箱 API",
    description="个人自媒体运营工具箱的后端API",
    version="1.0.0",
    lifespan=lifespan
)


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    """HTTPException 统一处理：5xx 视为高优先级错误，额外弹 toast。

    4xx 属于用户输入/业务校验问题，前端已有就地提示，不重复打扰。
    """
    if exc.status_code >= 500:
        detail = exc.detail
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail.get("error_code") or "服务处理失败")
            error_code = str(detail.get("error_code") or f"HTTP_{exc.status_code}")
        else:
            message = str(detail or "服务处理失败")
            error_code = f"HTTP_{exc.status_code}"
        emit_toast(
            "操作失败",
            message[:240] or "服务处理失败，请查看日志。",
            level="error",
            source="web",
            error_code=error_code,
        )
        logger.error(
            "请求处理失败: %s %s -> %s",
            request.method,
            request.url.path,
            exc.status_code,
            exc_info=exc,
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, exc: Exception):
    """统一兜底未处理异常，HTTPException 继续交给 FastAPI 默认处理。"""
    # 明确转交 HTTPException 的 FastAPI 默认处理器，保持原始状态码和 detail。
    if isinstance(exc, HTTPException):
        return await http_exception_handler(request, exc)
    # 高优先级：未处理异常意味着请求无法完成，除日志外必须让用户看见。
    report_exception(
        exc,
        logger=logger,
        action="处理 API 请求",
        priority=ErrorPriority.HIGH,
        context={"method": request.method, "path": request.url.path},
        toast_title="操作失败",
        toast_message="服务处理请求时发生未预期错误，详细原因已写入日志。",
        source="web",
    )
    return JSONResponse(
        status_code=500,
        content=format_exception(exc),
    )

# CORS配置
# 显式白名单，只放行本地开发/部署地址，避免任意来源跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:8080", "http://127.0.0.1:8080"],  # 显式白名单
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 静态资源禁用强缓存
# 前端每次发版都 bump index.html 里的 ?v= 版本号，配合这里的
# Cache-Control: no-cache 强制客户端（浏览器与打包 exe 内嵌页）每次
# 都走协商缓存（ETag/Last-Modified），资源更新后刷新即可拉到新版本，
# 避免采集结果等数据已变但页面仍渲染旧脚本的“假死”现象。
@app.middleware("http")
async def no_cache_static_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return response


# 跨站写请求拦截
# CORS 白名单只阻止跨站页面「读取响应」，并不阻止请求被执行：任意网页都能用
# fetch(..., {mode: 'no-cors'}) 发出不带 Content-Type 的 POST，而 FastAPI 仍会按 JSON
# 解析请求体，于是可以跨站改写 LLM 配置、退出登录、增删监控目标等。
# 浏览器跨站请求一定带 Origin 头；这里对 /api/ 下的非安全方法，只要 Origin 不属于
# 本机允许集合就直接 403。无 Origin 的请求（同源 GET、CLI、桌面内嵌页、测试客户端）不受影响。
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def block_cross_site_writes(request: Request, call_next):
    if (
        request.method in _UNSAFE_METHODS
        and request.url.path.startswith("/api/")
        and is_cross_site_origin(request.headers.get("origin"))
    ):
        logger.warning("已拦截跨站写请求: %s %s origin=%s", request.method, request.url.path, request.headers.get("origin"))
        return JSONResponse(status_code=403, content={"detail": "cross_site_request_blocked"})
    return await call_next(request)


# 静态文件
# web/frontend 下存放前端页面，存在则挂载 /static 供浏览器加载。
# PyInstaller onefile 会把数据解压到 sys._MEIPASS，而 web.main.py 可能仍来自 PYZ，
# 因此不能只依赖 __file__ 的父目录定位模板和静态资源。
def _resolve_web_dir() -> Path:
    """解析源码与 PyInstaller frozen 环境共用的 Web 资源目录。

    Returns:
        Path: 包含 frontend/templates 与 frontend/static 的 web 目录。
    """
    try:
        # frozen 环境的数据文件由 build.spec 放入 _MEIPASS/web/。
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            frozen_web_dir = Path(meipass) / "web"
            if frozen_web_dir.exists():
                return frozen_web_dir

        # 源码环境沿用当前模块所在的 web 目录。
        source_web_dir = Path(__file__).resolve().parent
        if source_web_dir.exists():
            return source_web_dir
    except Exception as exc:
        # 资源定位失败时记录清晰日志，避免桌面端表现为“按钮无响应”。
        logger.error("解析 Web 资源目录失败: %s", exc, exc_info=True)

    # 返回一个稳定的兜底路径，让后续 Jinja/StaticFiles 报出可定位的路径。
    return Path(__file__).resolve().parent


web_dir = _resolve_web_dir()
# 计算结果存入 static_dir
# 对输入做运算得到结果
static_dir = web_dir / "frontend" / "static"
# 计算结果存入 templates_dir
# 对输入做运算得到结果
templates_dir = web_dir / "frontend" / "templates"
logger.info("Web 资源目录: web=%s, static=%s, templates=%s", web_dir, static_dir, templates_dir)

# 前端静态资源目录存在时才挂载，避免构建期报错
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Jinja2 模板引擎，用于渲染 index.html 等页面
templates = Jinja2Templates(directory=str(templates_dir))

# 注册路由
# 每个业务模块独立 APIRouter，按功能前缀挂载
app.include_router(hotspot.router, prefix="/api/hotspot", tags=["热点发现"])
app.include_router(comment.router, prefix="/api/comment", tags=["评论监控"])
# 配置接口含密钥相关读写：Host / Origin 须在本机白名单内（可用 FISHTOOL_ALLOWED_HOSTS 覆盖）
app.include_router(
    config_router.router,
    prefix="/api/config",
    tags=["配置管理"],
    dependencies=[Depends(require_local_read)],
)
app.include_router(logs.router, prefix="/api/logs", tags=["日志查看"])
# 抽奖工具（目标预览、真人筛选与随机抽奖）
app.include_router(lottery.router, prefix="/api", tags=["抽奖工具"])
app.include_router(analysis.router, prefix="/api", tags=["UP分析与自诊"])
# 01 正确排名：候选发现 / 任务编排 / 冻结结果读取
app.include_router(benchmark.router, prefix="/api", tags=["同行排名"])
# 登录态管理（B站扫码登录的 Web 端入口）
app.include_router(auth_router.router, prefix="/api/auth", tags=["登录态"])


# ============ 根路由 ============

@app.get("/")
async def root(request: Request):
    """根路径 - 渲染前端页面

    访问 http://localhost:8000/ 时返回前端入口页面
    # 将结果交回调用方
    """
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api")
async def api_info():
    """API信息

    返回服务名称、版本与运行状态，
    # 将结果交回调用方
    用于前端加载时确认后端可达
    # 从存储/网络读入数据
    """
    return {
        "name": "B站运营工具箱 API",
        "version": "1.0.0",
        "status": "running"
    }


@app.get("/health")
async def health_check():
    """健康检查
    # 验证状态/条件，决定下一步分支

    返回 {"status": "ok"}，供部署探针/监控使用
    # 将结果交回调用方
    """
    return {"status": "ok"}


# ============ WebSocket（用于实时推送预警）============

class ConnectionManager:
    """WebSocket连接管理器

    维护所有活跃的 WebSocket 连接列表，提供：
    - connect: 接受连接并加入列表
    - disconnect: 从列表移除
    - broadcast: 向所有连接广播消息（逐个 try，单点失败不影响其他）

    线程安全说明：FastAPI 单事件循环下，列表操作
    在同一线程内执行，无需额外加锁。
    """
    
    def __init__(self):
        """连接管理器：维护活跃 WebSocket 列表"""
        # 活跃连接列表，元素为已 accept 的 WebSocket 对象
        self.active_connections: list[WebSocket] = []
    
    async def connect(self, websocket: WebSocket):
        """接受新连接并加入广播列表"""
        # 接受握手并登记连接
        await websocket.accept()
        # 追加到列表
        self.active_connections.append(websocket)
        logger.info(f"WebSocket连接建立，当前连接数: {len(self.active_connections)}")
    
    def disconnect(self, websocket: WebSocket):
        """断开连接并从广播列表移除

        幂等实现：连接可能已因广播失败被移除，重复调用不应抛 ValueError。
        """
        # 断开时从列表移除；不存在时按已断开处理。
        try:
            self.active_connections.remove(websocket)
        except ValueError:
            # 已移除说明并发/重复调用，不是错误，保持幂等。
            logger.debug("WebSocket 连接已不在广播列表，忽略重复断开")
            return
        logger.info(f"WebSocket连接断开，当前连接数: {len(self.active_connections)}")
    
    async def broadcast(self, message: dict):
        """广播消息到所有连接

        逐个连接发送，异常只记日志（含堆栈）不中断，
        避免单个坏连接拖垮整个广播；发送失败的连接会被移除。
        """
        # 遍历快照：发送失败时 disconnect 会修改 active_connections。
        for connection in list(self.active_connections):
            # 异常保护：局部失败不影响主流程
            try:
                # 异步等待结果
                await connection.send_json(message)
            # 异常处理
            except Exception as exc:
                logger.warning("WebSocket发送消息失败，移除该连接: %s", exc, exc_info=True)
                self.disconnect(connection)


# 全局唯一的连接管理器实例
manager = ConnectionManager()


def _install_toast_bridge() -> Callable[[], None]:
    """把 :func:`core.toast.emit_toast` 桥接到 ``/ws`` 广播。

    Returns:
        Callable[[], None]: 注销函数。
    """
    loop = asyncio.get_running_loop()

    def _sink(payload) -> None:
        """任意线程调用：把 toast 调度回 FastAPI 事件循环广播。"""
        message = {"type": "toast", "data": payload.to_dict()}
        try:
            future = asyncio.run_coroutine_threadsafe(manager.broadcast(message), loop)

            def _log_broadcast_result(done) -> None:
                """消费广播 Future 的结果，避免异常只留在 Future 里无人读取。"""
                try:
                    done.result()
                except Exception:  # noqa: BLE001 - 通知失败不能反向打断业务
                    logger.warning("toast 广播失败，已忽略", exc_info=True)

            future.add_done_callback(_log_broadcast_result)
        except Exception:  # noqa: BLE001 - 通知失败不能反向打断业务
            logger.warning("toast 广播调度失败，已忽略", exc_info=True)

    return register_toast_sink(_sink)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket端点（用于桌宠推送预警）

    连接建立后进入接收循环：
    - 客户端发送心跳/消息时读取（当前仅用于保活）
    - 客户端断开时自动从管理器移除

    业务侧通过 push_alert() 向此端点广播预警
    """
    # WebSocket 不受 CORS 约束：跨站页面可直接连接并订阅预警（含评论原文），握手前校验 Origin。
    # 桌宠客户端（websocket-client）不带 Origin，不受影响。
    if is_cross_site_origin(websocket.headers.get("origin")):
        logger.warning("已拒绝跨站 WebSocket 连接: origin=%s", websocket.headers.get("origin"))
        await websocket.close(code=1008)
        return
    await manager.connect(websocket)
    # 异常保护：局部失败不影响主流程
    try:
        # 循环处理，满足条件后退出
        while True:
            # 接收客户端消息（心跳）
            data = await websocket.receive_text()
            # 可以在这里处理客户端消息
    except WebSocketDisconnect:
        # 断开连接
        manager.disconnect(websocket)
    except Exception as exc:
        # 连接异常关闭（网络中断/协议错误）同样按断开处理，避免异常冒泡。
        logger.warning("WebSocket 连接异常关闭: %s", exc, exc_info=True)
        manager.disconnect(websocket)


async def push_alert(alert: dict):
    """推送预警到所有WebSocket连接

    供监控模块在检测到风控/异常时调用，
    前端收到 type=alert 的消息后弹窗提示
    """
    await manager.broadcast({
        "type": "alert",
        "data": alert
    })
