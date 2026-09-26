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
# 从 routers 导入符号
from .routers import hotspot, comment, config as config_router, logs, analysis, auth as auth_router, lottery

# 统一使用 core.logger 的日志实例，避免重复配置
logger = get_logger(__name__)

# 全局配置
# 在 lifespan 启动阶段初始化，供各路由懒加载使用
config_manager = None
# 常驻评论监控服务由 FastAPI lifespan 统一创建和销毁。
monitor_service = None


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
    logger.info("Web服务已启动")
    
    yield
    
    # 关闭常驻任务，确保 asyncio 任务和数据库会话完整退出。
    if monitor_service is not None:
        await monitor_service.shutdown()
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