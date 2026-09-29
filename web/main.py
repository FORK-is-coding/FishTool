"""
B站运营工具箱 - Web API 主应用
FastAPI后端，提供RESTful API接口

本模块是整个 Web 服务的入口，负责：
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
- 直接 python web/main.py 启动，监听 0.0.0.0:8000
# 触发服务/线程开始运行
- 或通过 start_web.py 包装启动
# 触发服务/线程开始运行
"""
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
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
import logging
# 导入系统模块，用于识别 PyInstaller frozen 环境。
import sys
# 从 pathlib 导入符号
from pathlib import Path

# 从 core.config 导入符号
from core.config import ConfigManager
from core.database import init_database
# 从 core.logger 导入符号
from core.logger import get_logger
from core.exceptions import format_exception
from core.monitor_service import ResidentCommentMonitor
# 排名服务由本层组装（modules 不反向 import web）；client 限定自己拥有、只关自己。
from bilibili.api import BilibiliAPI
from bilibili.cookie_pool import get_cookie_pool
from bilibili.rate_limiter import get_rate_limiter
from core.database import get_session
from modules.self_diagnosis.benchmark.service import BenchmarkService
from modules.self_diagnosis.benchmark.store import BenchmarkStore
# 从 routers 导入符号
from .routers import (
    hotspot, comment, config as config_router, logs, analysis,
    auth as auth_router, lottery, benchmark,
)

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
    # 赋值并准备后续使用
    config_manager = ConfigManager()
    # Web 入口独立运行时也必须初始化数据库全局管理器
    init_database()
    # 仅接线评论区常驻监控；动态采集没有常驻任务入口。
    global monitor_service
    monitor_service = ResidentCommentMonitor(comment.get_monitor, config_manager)
    await monitor_service.start()

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
                logger.error("排名服务关闭失败: %s", exc)
        if monitor_service is not None:
            await monitor_service.shutdown()
        if benchmark_client is not None:
            try:
                await benchmark_client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.error("排名 client 关闭失败: %s", exc)
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


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, exc: Exception):
    """统一兜底未处理异常，HTTPException 继续交给 FastAPI 默认处理。"""
    # 明确转交 HTTPException 的 FastAPI 默认处理器，保持原始状态码和 detail。
    if isinstance(exc, HTTPException):
        return await http_exception_handler(request, exc)
    logger.exception("未捕获异常: %s", exc)
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
app.include_router(config_router.router, prefix="/api/config", tags=["配置管理"])
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
        """断开连接并从广播列表移除"""
        # 断开时从列表移除，注意 list.remove 不存在的元素会抛错
        # 但调用方保证 disconnect 只在已连接状态下触发
        self.active_connections.remove(websocket)
        logger.info(f"WebSocket连接断开，当前连接数: {len(self.active_connections)}")
    
    async def broadcast(self, message: dict):
        """广播消息到所有连接

        逐个连接发送，异常只记日志不中断，
        避免单个坏连接拖垮整个广播
        """
        for connection in self.active_connections:
            # 异常保护：局部失败不影响主流程
            try:
                # 异步等待结果
                await connection.send_json(message)
            # 异常处理
            except Exception as e:
                logger.error(f"WebSocket发送消息失败: {e}")


# 全局唯一的连接管理器实例
manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket端点（用于桌宠推送预警）

    连接建立后进入接收循环：
    - 客户端发送心跳/消息时读取（当前仅用于保活）
    - 客户端断开时自动从管理器移除

    业务侧通过 push_alert() 向此端点广播预警
    """
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


async def push_alert(alert: dict):
    """推送预警到所有WebSocket连接

    供监控模块在检测到风控/异常时调用，
    前端收到 type=alert 的消息后弹窗提示
    """
    await manager.broadcast({
        "type": "alert",
        "data": alert
    })


# 边界/有效性检查
if __name__ == "__main__":
    # 导入模块
    import uvicorn
    # 0.0.0.0 允许局域网访问，端口默认 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)