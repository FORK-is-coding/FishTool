"""
主窗口 - 内嵌WebUI的桌面应用主窗口
使用QWebEngineView加载本地Web服务

本模块是桌面客户端的核心窗口，采用"桌面壳 + WebUI"架构：
- QWebEngineView 内嵌加载本地 FastAPI Web 服务页面
- Web 服务启动策略按环境区分：
  - 源码环境：subprocess 子进程启动 start_web.py
  - 冻结环境（PyInstaller）：进程内线程启动 uvicorn，
    不依赖源码文件（V7 修复）
- 系统托盘：支持最小化到托盘、托盘菜单、双击恢复
- 启动流程：加载动画 → 轮询 /health → 加载 WebUI

关键流程（start_web_service）：
1. 检查端口是否已被占用（check_web_service）
2. 按环境选择启动方式（子进程/线程内嵌）
3. QTimer 定时轮询 /health 直至就绪
4. 就绪后 setUrl 加载主界面

资源清理（quit_application）：
- 确认对话框防误退
- 终止 Web 服务子进程（terminate -> kill 兜底）
- QApplication.quit 退出事件循环

V7 修复标记: 2026-08-19 20:34 - 冻结环境 Web 服务进程内启动

使用方式：
    window = MainWindow(web_port=8000)
    window.show()
"""
import sys
# 导入模块
import asyncio
# 从 typing 导入符号
from typing import Optional
# 从 PyQt5.QtWidgets 导入符号
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QWidget,
    QSystemTrayIcon, QMenu, QAction, QMessageBox
)
# 从 PyQt5.QtCore 导入符号
from PyQt5.QtCore import QUrl, Qt, QTimer, pyqtSignal
# 从 PyQt5.QtGui 导入符号
from PyQt5.QtGui import QIcon
# 从 PyQt5.QtWebEngineWidgets 导入符号
from PyQt5.QtWebEngineWidgets import (
    QWebEngineView, QWebEngineSettings, QWebEnginePage, QWebEngineProfile
)
# 导入模块
import logging
# 导入模块
import subprocess
# 导入模块
import time
# 导入模块
import requests

# 从 core.logger 导入符号
from core.logger import get_logger
from desktop.icon_utils import load_app_icon

logger = get_logger(__name__)


class DesktopWebPage(QWebEnginePage):
    """桌面端 WebEngine 页面，记录 frozen 客户端的 JS 错误和调试信息。"""

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        """记录网页控制台消息，避免 windowed exe 静默吞掉 JS 运行错误。"""
        logger.warning(
            "[桌面Web] JS控制台 level=%s line=%s source=%s message=%s",
            level,
            line_number,
            source_id,
            message,
        )
        super().javaScriptConsoleMessage(level, message, line_number, source_id)


class MainWindow(QMainWindow):
    """主窗口 - 内嵌WebUI
    
    负责窗口创建、Web服务启动编排、托盘管理与资源清理。
    
    主要职责：
    1. 创建并管理 QWebEngineView（渲染 Web 界面）
    2. 按环境启动 Web 服务（子进程或线程）
    3. 维护系统托盘图标与菜单
    4. 退出时清理 Web 服务进程
    
    信号：
    - web_ready: Web 服务就绪后触发，供外部监听
    """

    # 启动尺寸按前端桌面版式设置，避免初始视口落入中间挤压状态。
    INITIAL_WINDOW_WIDTH = 1440
    INITIAL_WINDOW_HEIGHT = 900
    # 最小尺寸仍保留完整侧栏、40px内容边距和双列工作区的可用宽度。
    MIN_WINDOW_WIDTH = 1200
    MIN_WINDOW_HEIGHT = 750
    
    # 信号定义
    web_ready = pyqtSignal()  # Web服务就绪信号
    
    def __init__(self, web_port: int = 8000):
        """初始化主窗口
        
        Args:
            web_port: Web服务端口
        """
        # 调用父类QMainWindow的构造函数
        super().__init__()
        
        # 保存Web服务端口号配置
        self.web_port = web_port
        # 构造Web服务完整URL地址
        self.web_url = f"http://localhost:{web_port}"
        # 初始化Web服务进程引用（用于源码环境子进程模式）
        self.web_process: Optional[subprocess.Popen] = None
        
        # 主窗口显式设置图标，覆盖 Windows 标题栏、Alt+Tab 和任务栏显示。
        self.setWindowIcon(load_app_icon())
        self.init_ui()
        # 设置系统托盘图标和菜单
        self.setup_tray()
        
        # 启动Web服务（异步）
        # 触发服务/线程开始运行
        self.start_web_service()
    
    def init_ui(self):
        """初始化UI界面
        # 设置初始值/默认状态，避免后续空引用
        
        创建主窗口布局，设置 WebEngineView 用于加载 Web 服务。
        # 实例化对象并准备使用
        显示启动加载动画，等待后端服务就绪。
        # 从存储/网络读入数据
        """
        # 启动尺寸与网页版式保持 8:5 比例；最小尺寸避免内容区被压入窄屏布局。
        self.setMinimumSize(self.MIN_WINDOW_WIDTH, self.MIN_WINDOW_HEIGHT)
        self.setGeometry(
            100,
            100,
            self.INITIAL_WINDOW_WIDTH,
            self.INITIAL_WINDOW_HEIGHT,
        )
        
        # 中央窗口
        central_widget = QWidget()
        # 将承载 Web 页面的控件设为中心部件
        self.setCentralWidget(central_widget)
        
        # 布局（无外边距，WebView 全屏填充）
        layout = QVBoxLayout(central_widget)
        # 去掉布局边距，让 Web 页面铺满窗口
        layout.setContentsMargins(0, 0, 0, 0)
        
        # WebView 使用自定义页面记录 JS 控制台错误；windowed exe 没有 stdout，
        # 没有这层日志时脚本 404/语法异常会被误判为按钮失效。
        self.web_view = QWebEngineView()
        self.web_view.setPage(DesktopWebPage(self.web_view))
        self.web_view.loadFinished.connect(self._on_web_load_finished)

        # frozen 客户端的 WebEngine 缓存位于用户目录，会跨 exe 版本保留旧 app.js。
        # 桌面壳禁用 HTTP 缓存，确保每次启动都读取当前 exe 内置的 JS/CSS；
        # 仅影响内嵌客户端，不改变 8000 端口在普通浏览器中的缓存策略。
        profile = self.web_view.page().profile()
        profile.setHttpCacheType(QWebEngineProfile.NoCache)
        profile.clearHttpCache()
        
        # 启用开发者工具
        # 允许本地页面访问远程资源（API等）
        settings = self.web_view.settings()
        # 允许 Web 页面访问远程资源（加载 CDN 前端文件）
        # 从存储/网络读入数据
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        # 允许访问本地文件（加载本地构建产物）
        # 从存储/网络读入数据
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        
        # QWebEngine 默认使用 1.0 CSS 缩放，避免桌面端相对浏览器再次放大或缩小。
        self.web_view.setZoomFactor(1.0)

        # 将 WebView 加入布局
        layout.addWidget(self.web_view)
        
        # 初始加载页面（等待Web服务启动）
        # 内嵌 HTML 加载动画：渐变背景 + CSS 旋转 spinner
        # 从存储/网络读入数据
        self.web_view.setHtml("""
        <html>
        <head>
            <style>
                body {
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    font-family: "SimHei", "Microsoft YaHei", sans-serif;
                }
                .loading {
                    text-align: center;
                    color: white;
                }
                .spinner {
                    border: 4px solid rgba(255,255,255,0.3);
                    border-radius: 50%;
                    border-top: 4px solid white;
                    width: 50px;
                    height: 50px;
                    animation: spin 1s linear infinite;
                    margin: 0 auto 20px;
                }
                @keyframes spin {
                    0% { transform: rotate(0deg); }
                    100% { transform: rotate(360deg); }
                }
            </style>
        </head>
        <body>
            <div class="loading">
                <div class="spinner"></div>
                <h2>B站运营工具箱正在启动...</h2>
                # 触发服务/线程开始运行
                <p>请稍候，正在初始化Web服务</p>
                # 设置初始值/默认状态，避免后续空引用
            </div>
        </body>
        </html>
        """)
        
        logger.info("[主窗口] UI初始化完成")
    
    def setup_tray(self):
        """设置系统托盘图标和菜单
        # 写入配置/属性，影响后续行为
        
        创建托盘图标，支持：
        # 实例化对象并准备使用
        - 显示/隐藏主窗口
        # 将内容呈现到界面上
        - 双击托盘恢复窗口
        - 右键菜单快速操作
        - 最小化到托盘而非退出
        """
        self.tray_icon = QSystemTrayIcon(self)
        
        # 创建托盘菜单
        tray_menu = QMenu()
        
        # "显示主窗口"菜单项
        # 将内容呈现到界面上
        show_action = QAction("显示主窗口", self)
        # 建立连接
        show_action.triggered.connect(self.show_window)
        # 添加菜单动作
        # 将元素加入容器/布局
        tray_menu.addAction(show_action)
        
        # "隐藏到托盘"菜单项
        hide_action = QAction("隐藏到托盘", self)
        # 建立连接
        hide_action.triggered.connect(self.hide)
        # 添加菜单动作
        # 将元素加入容器/布局
        tray_menu.addAction(hide_action)
        
        # 菜单分隔线
        tray_menu.addSeparator()
        
        # "退出"菜单项（带确认）
        quit_action = QAction("退出", self)
        # 建立连接
        quit_action.triggered.connect(self.quit_application)
        # 添加菜单动作
        # 将元素加入容器/布局
        tray_menu.addAction(quit_action)
        
        # 使用与 QApplication、主窗口完全一致的图标，避免托盘回退到默认图标。
        self.tray_icon.setIcon(load_app_icon())
        self.tray_icon.setContextMenu(tray_menu)
        # 设置悬浮提示
        self.tray_icon.setToolTip("B站运营工具箱")
        
        # 双击托盘图标显示窗口
        # 将内容呈现到界面上
        self.tray_icon.activated.connect(self.on_tray_activated)
        
        # 显示托盘图标（需要图标文件，这里先不设置）
        # self.tray_icon.setIcon(QIcon("path/to/icon.png"))
        self.tray_icon.show()
        
        logger.info("[主窗口] 系统托盘已设置")
    
    def on_tray_activated(self, reason):
        """托盘图标激活事件处理
        # 对数据进行加工/分发
        
        Args:
            reason: 激活原因（单击/双击/右键等）
        """
        # 双击托盘时恢复主窗口
        # 单击不处理，避免误触
        if reason == QSystemTrayIcon.DoubleClick:
            # 双击托盘图标时恢复主窗口
            self.show_window()
    
    def show_window(self):
        """显示主窗口并激活
        # 将内容呈现到界面上
        
        从托盘或最小化状态恢复窗口，置顶并获得焦点。
        """
        self.show()
        # 从托盘恢复时激活窗口并置顶
        self.activateWindow()
    
    def start_web_service(self):
        """启动Web服务
        # 触发服务/线程开始运行
        
        V7 修复: 添加冻结环境检测，PyInstaller 打包后使用进程内启动。
        # 将元素加入容器/布局
        步骤：端口检查 -> 环境检测 -> 启动 -> 定时轮询就绪。
        # 验证状态/条件，决定下一步分支
        """
        logger.info("[主窗口] 启动Web服务...")
        
        # 异常保护：局部失败不影响主流程
        try:
            # 检查端口是否已被占用
            # 已在运行则直接加载界面
            # 从存储/网络读入数据
            if self.check_web_service():
                # Web 服务已就绪，直接加载界面
                # 从存储/网络读入数据
                logger.info("[主窗口] Web服务已在运行")
                # 加载 Web 界面
                # 从存储/网络读入数据
                self.load_web_ui()
                return
            
            # 检测是否为冻结环境（PyInstaller打包）
            # 冻结时 start_web.py 不在文件系统，只能进程内启动
            # 触发服务/线程开始运行
            is_frozen = getattr(sys, 'frozen', False)
            
            # 根据运行环境选择启动方式
            # 触发服务/线程开始运行
            if is_frozen:
                # 冻结环境：进程内启动（start_web.py源文件不存在于文件系统）
                # 触发服务/线程开始运行
                logger.info("[主窗口] 检测到冻结环境，使用进程内启动Web服务")
                self._start_web_service_inprocess()
            else:
                # 源码环境：子进程启动
                # 触发服务/线程开始运行
                logger.info("[主窗口] 检测到源码环境，使用子进程启动Web服务")
                self._start_web_service_subprocess()
            
            # 等待Web服务就绪
            # 3秒后首次检查，未就绪则继续轮询
            QTimer.singleShot(3000, self.check_and_load_web)
            
        except Exception as e:
            logger.error(f"[主窗口] 启动Web服务失败: {e}")
            # 弹窗展示错误
            # 将内容呈现到界面上
            self.show_error("启动失败", f"Web服务启动失败: {str(e)}")
    
    def _start_web_service_subprocess(self):
        """子进程方式启动Web服务（源码环境）
        # 触发服务/线程开始运行
        
        使用 subprocess.Popen 启动独立的 Python 进程运行 start_web.py。
        # 触发服务/线程开始运行
        适用于未打包的开发环境。
        """
        from pathlib import Path
        
        # 获取项目根目录
        project_root = Path(__file__).parent.parent
        # 计算结果存入 start_script
        # 对输入做运算得到结果
        start_script = project_root / "start_web.py"
        
        # 启动脚本不存在则报错
        # 触发服务/线程开始运行
        if not start_script.exists():
            logger.error(f"[主窗口] 启动脚本不存在: {start_script}")
            # 弹窗展示错误
            # 将内容呈现到界面上
            self.show_error("启动失败", "找不到Web服务启动脚本")
            return
        
        # 启动子进程
        # 独立进程运行，cwd 指向项目根目录保证相对路径正确
        self.web_process = subprocess.Popen(
            [sys.executable, str(start_script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(project_root)
        )
        
        logger.info(f"[主窗口] Web服务进程已启动: PID={self.web_process.pid}")
    
    def _start_web_service_inprocess(self):
        """进程内启动Web服务（冻结环境）
        # 触发服务/线程开始运行
        
        V7 修复: 冻结环境下使用线程+uvicorn.Server启动，不依赖 start_web.py 源文件
        # 触发服务/线程开始运行
        使用 Config+Server 方式替代 run()，更适合在线程中运行
        """
        import threading
        # 导入模块
        import asyncio
        # 导入模块
        import uvicorn
        # 从 web.main 导入符号
        from web.main import app
        # 从 core.database 导入符号
        from core.database import init_database
        
        # 初始化数据库
        # 冻结环境下数据库路径默认为 data/bili_ops.db
        try:
            init_database()
            logger.info("[主窗口] 数据库初始化成功")
        except Exception as e:
            logger.warning(f"[主窗口] 数据库初始化失败: {e}")
        
        # 后台线程运行uvicorn
        # 线程化避免阻塞Qt主线程事件循环
        def run_uvicorn():
            """在后台线程中启动 Web 服务
            # 触发服务/线程开始运行

            使用 Config+Server 方式运行，线程内创建独立事件循环。
            # 实例化对象并准备使用
            """
            try:
                logger.info(f"[主窗口] 在后台线程启动uvicorn: 0.0.0.0:{self.web_port}")
                
                # 使用 Config+Server 方式，更适合在线程中运行
                # run() 内部会调用 loop.run_until_complete，在线程中会冲突
                # Bug2 修复：log_config=None 禁用 uvicorn 的 dictConfig 日志配置。
                # frozen(windowed) 环境下 sys.stdout 为 None，
                # uvicorn 0.52 的 ColourizedFormatter.__init__ 会执行
                # sys.stdout.isatty() 直接抛 AttributeError，
                # 导致 "Unable to configure formatter 'default'"，
                # uvicorn.Config 构造失败、服务起不来。
                # 日志改走项目自身的 logging 配置（冒泡到根 logger 文件 handler）。
                config = uvicorn.Config(
                    app=app,
                    host="0.0.0.0",
                    port=self.web_port,
                    log_level="info",
                    access_log=False,  # 减少日志输出
                    log_config=None    # 禁用 uvicorn 自带日志配置，规避 frozen 环境 formatter 崩溃
                )
                server = uvicorn.Server(config)
                
                # 创建新的事件循环并运行
                # 线程内需要独立的 event loop
                loop = asyncio.new_event_loop()
                # 设置线程事件循环
                asyncio.set_event_loop(loop)
                # 运行事件循环直到服务退出
                loop.run_until_complete(server.serve())
                
            except Exception as e:
                logger.error(f"[主窗口] uvicorn运行失败: {e}", exc_info=True)
        
        # 启动守护线程
        # daemon=True 保证主窗口关闭时线程自动退出
        # 释放连接/窗口资源
        web_thread = threading.Thread(target=run_uvicorn, daemon=True, name="WebServiceThread")
        # 启动线程/进程/服务
        # 触发服务/线程开始运行
        web_thread.start()
        logger.info("[主窗口] Web服务线程已启动")
    
    def check_web_service(self) -> bool:
        """检查Web服务是否已启动
        # 验证状态/条件，决定下一步分支
        
        请求 /health 端点，2秒超时。
        
        Returns:
            True表示服务已启动
            # 触发服务/线程开始运行
        """
        try:
            # 发送健康检查请求
            response = requests.get(f"{self.web_url}/health", timeout=2)
            return response.status_code == 200
        except:
            # 连接失败视为未启动
            # 触发服务/线程开始运行
            return False
    
    def check_and_load_web(self):
        """检查Web服务并加载UI
        # 从存储/网络读入数据
        
        定时轮询 /health 端点，确认服务就绪后加载主界面。
        # 从存储/网络读入数据
        如果未就绪则继续等待重试。
        # 阻塞直到条件满足或超时
        """
        if self.check_web_service():
            logger.info("[主窗口] Web服务已就绪")
            # 加载 Web 界面
            # 从存储/网络读入数据
            self.load_web_ui()
        else:
            logger.warning("[主窗口] Web服务未就绪，继续等待...")
            # 继续等待
            # 2秒后再次检查
            QTimer.singleShot(2000, self.check_and_load_web)
    
    def _on_web_load_finished(self, ok: bool):
        """检查桌面端页面加载结果及弹窗入口是否成功导出。

        Args:
            ok: QWebEngine 页面加载是否成功。
        """
        logger.info("[桌面Web] 页面加载完成: ok=%s url=%s", ok, self.web_view.url().toString())
        if not ok or not self.web_view.url().toString().startswith(self.web_url):
            # init_ui 的内嵌加载页也会触发 loadFinished；只验收真实 HTTP 业务页。
            return

        # 通过实际按钮 click() 创建两类弹窗，再检查布局尺寸和 z-index。
        # 这比直接调用函数更接近用户操作，可捕获 onclick 未导出、遮罩不可见等问题。
        probe_script = """
            (() => {
                const result = {
                    pageUrl: window.location.href,
                    groupEntry: typeof window.showGroupQrModal === 'function',
                    verifyEntry: typeof window.verifyLotteryWinners === 'function',
                    alertEntry: typeof window.showAppAlert === 'function',
                    groupButton: false,
                    verifyButton: false,
                    groupModal: false,
                    groupImage: false,
                    groupVisible: false,
                    verifyModal: false,
                    verifyVisible: false
                };
                const isVisible = element => {
                    if (!element) return false;
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0 &&
                        Number(style.zIndex || 0) >= 1000;
                };
                try {
                    const groupButton = document.querySelector('button[onclick="showGroupQrModal()"]');
                    result.groupButton = Boolean(groupButton);
                    groupButton?.click();
                    result.groupModal = Boolean(document.querySelector('.group-qr-modal'));
                    const groupBackdrop = document.querySelector('.app-modal-backdrop');
                    const image = document.querySelector('.group-qr-image');
                    result.groupImage = Boolean(image && image.src.includes('/static/'));
                    result.groupVisible = isVisible(groupBackdrop);
                    groupBackdrop?.remove();

                    const verifyButton = document.getElementById('lottery-verify-winners-button');
                    result.verifyButton = Boolean(verifyButton);
                    verifyButton?.click();
                    const verifyBackdrop = document.querySelector('.app-modal-backdrop');
                    result.verifyModal = Boolean(verifyBackdrop);
                    result.verifyVisible = isVisible(verifyBackdrop);
                    verifyBackdrop?.remove();
                } catch (error) {
                    result.error = String(error && error.stack ? error.stack : error);
                }
                return result;
            })();
        """
        self.web_view.page().runJavaScript(
            probe_script,
            lambda result: logger.info("[桌面Web] 弹窗实测探针: %s", result),
        )

    def load_web_ui(self):
        """加载 WebUI 页面。

        该方法必须保持为 MainWindow 的独立方法，供启动完成回调和
        已运行服务分支共同调用；此前误缩进到页面回调中会导致 frozen
        exe 启动时出现 ``MainWindow has no attribute load_web_ui``。
        """
        logger.info("[主窗口] 加载WebUI: %s", self.web_url)
        # 每次桌面启动使用独立查询参数，规避旧 WebEngine 缓存索引残留；
        # 静态 JS/CSS 由 NoCache profile 强制从当前进程内服务重新读取。
        desktop_url = QUrl(f"{self.web_url}/?desktop_start={int(time.time() * 1000)}")
        self.web_view.setUrl(desktop_url)
        # 服务就绪后通知外部观察者，保持原有 Qt 信号契约。
        self.web_ready.emit()
    
    def show_error(self, title: str, message: str):
        """显示错误对话框
        # 将内容呈现到界面上
        
        Args:
            title: 对话框标题
            message: 错误消息内容
        """
        QMessageBox.critical(self, title, message)
    
    def closeEvent(self, event):
        """窗口关闭事件处理
        # 对数据进行加工/分发
        
        覆盖默认关闭行为，最小化到托盘而非直接退出应用。
        # 释放连接/窗口资源
        
        Args:
            event: 关闭事件对象
            # 释放连接/窗口资源
        """
        # 关闭事件可能发生在托盘初始化未完成的异常路径，所有清理操作都必须可空安全。
        event.ignore()
        self.hide()
        tray_icon = getattr(self, "tray_icon", None)
        if tray_icon is not None:
            tray_icon.showMessage(
                "B站运营工具箱",
                "程序已最小化到系统托盘",
                QSystemTrayIcon.Information,
                2000,
            )
    
    def quit_application(self):
        """退出应用程序
        
        弹出确认对话框，用户确认后：
        1. 终止 Web 服务进程
        2. 清理资源
        3. 退出 Qt 应用
        """
        # 弹窗询问用户是否确认退出
        # 默认按钮设为 No，防止误触关掉程序
        reply = QMessageBox.question(
            self,
            "确认退出",
            "确定要退出B站运营工具箱吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        # 用户点了确认，进入退出清理流程
        if reply == QMessageBox.Yes:
            logger.info("[主窗口] 应用程序退出")
            
            # 停止Web服务
            # 先优雅终止，5 秒超时未退出则强制 kill
            
            # 先尝试优雅终止，失败则强制 kill
            if self.web_process:
                # 异常保护：局部失败不影响主流程
                try:
                    # 终止进程
                    self.web_process.terminate()
                    # 等待一段时间
                    # 阻塞直到条件满足或超时
                    self.web_process.wait(timeout=5)
                    logger.info("[主窗口] Web服务进程已终止")
                except:
                    # 强制结束进程
                    self.web_process.kill()
                    logger.warning("[主窗口] Web服务进程被强制终止")
            
            # 退出应用
            QApplication.quit()


def run_desktop_app():
    """运行桌面应用入口函数
    
    创建 QApplication 实例，初始化主窗口并进入事件循环。
    # 设置初始值/默认状态，避免后续空引用
    这是桌面客户端的主入口点。
    """
    # 创建 Qt 应用实例
    # argv 传入以支持 Qt 命令行参数解析
    app = QApplication(sys.argv)
    app.setWindowIcon(load_app_icon())
    # 设置应用名，用于窗口标题和系统托盘
    app.setApplicationName("B站运营工具箱")
    
    # 创建主窗口
    main_window = MainWindow()
    # 显示窗口/内容
    # 将内容呈现到界面上
    main_window.show()
    
    # 进入 Qt 事件循环
    sys.exit(app.exec_())


# 入口：直接运行本文件时启动桌面应用
# 触发服务/线程开始运行
if __name__ == "__main__":
    run_desktop_app()