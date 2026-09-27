"""桌宠 WebSocket 通信线程。

拆分自原 pet_window.py 的 WebSocketThread：
独立 QThread 运行 WebSocket 客户端，连接后端 /ws 端点，
接收实时预警推送，30 秒心跳保活。
"""
from typing import Optional
from PyQt5.QtCore import QThread, pyqtSignal
import json
import threading
import websocket

from .assets import logger

class WebSocketThread(QThread):
    """WebSocket通信线程
    
    在独立线程中运行 WebSocket 客户端，
    避免阻塞 Qt 主线程事件循环。
    
    信号说明：
    - message_received: 收到服务端推送的 JSON 消息
    - connected: 连接建立成功
    - disconnected: 连接断开
    - error: 发生错误（携带错误信息字符串）
    """
    
    # 信号定义
    message_received = pyqtSignal(dict)  # 接收到消息
    connected = pyqtSignal()  # 连接成功
    disconnected = pyqtSignal()  # 连接断开
    error = pyqtSignal(str)  # 错误
    
    def __init__(self, ws_url: str):
        """初始化WebSocket线程
        # 设置初始值/默认状态，避免后续空引用
        
        Args:
            ws_url: WebSocket服务地址
        """
        super().__init__()
        self.ws_url = ws_url
        self.ws: Optional[websocket.WebSocketApp] = None
        self.running = False
    
    def run(self):
        """线程运行主循环
        
        创建 WebSocketApp 实例，建立连接并保持运行。
        # 实例化对象并准备使用
        启动心跳线程，保持长连接。
        # 触发服务/线程开始运行
        """
        self.running = True
        
        # 异常保护：局部失败不影响主流程
        try:
            # 创建 WebSocketApp 并注册各事件回调
            self.ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close
            )
            
            # 运行WebSocket（阻塞）
            # run_forever 内部维护事件循环，直到连接关闭
            # 释放连接/窗口资源
            self.ws.run_forever()
            
        except Exception as e:
            logger.error(f"[WebSocket] 线程异常: {e}")
            # 发射 Qt 信号
            self.error.emit(str(e))
    
    def on_open(self, ws):
        """WebSocket连接打开回调
        
        连接成功后启动心跳线程，每 30 秒发送一次 ping。
        # 触发服务/线程开始运行
        
        Args:
            ws: WebSocket 实例
        """
        logger.info("[WebSocket] 连接已建立")
        # 发射 Qt 信号
        self.connected.emit()
        
        # 发送心跳
        # 保活线程：每30秒发送一次ping，防止连接被服务端回收
        def heartbeat():
            """心跳线程：每 30 秒发送一次 ping 保活"""
            while self.running:
                # 异常保护：局部失败不影响主流程
                try:
                    # 发送消息/请求
                    ws.send(json.dumps({"type": "ping"}))
                    # 等待一段时间
                    # 阻塞直到条件满足或超时
                    threading.Event().wait(30)  # 30秒心跳
                except Exception:
                    # 退出循环
                    break
        
        # 启动线程/进程/服务
        # 触发服务/线程开始运行
        threading.Thread(target=heartbeat, daemon=True).start()
    
    def on_message(self, ws, message):
        """接收到消息
        
        解析 JSON 消息并通过信号传递给主线程。
        # 将原始文本转为结构化数据
        
        Args:
            ws: WebSocket 实例
            message: 原始消息字符串
        """
        try:
            # 解析 JSON 消息
            data = json.loads(message)
            logger.info(f"[WebSocket] 收到消息: {data.get('type')}")
            # 通过信号发送到主线程
            self.message_received.emit(data)
        except Exception as e:
            logger.error(f"[WebSocket] 消息解析失败: {e}")
    
    def on_error(self, ws, error):
        """WebSocket错误
        
        Args:
            ws: WebSocket 实例
            error: 错误信息
        """
        logger.error(f"[WebSocket] 错误: {error}")
        # 发射 Qt 信号
        self.error.emit(str(error))
    
    def on_close(self, ws, close_status_code, close_msg):
        """WebSocket关闭
        # 释放连接/窗口资源
        
        Args:
            ws: WebSocket 实例
            close_status_code: 关闭状态码
            # 释放连接/窗口资源
            close_msg: 关闭消息
            # 释放连接/窗口资源
        """
        logger.info("[WebSocket] 连接已关闭")
        # 发射 Qt 信号
        self.disconnected.emit()
    
    def stop(self):
        """停止WebSocket连接
        
        设置运行标志为 False，关闭连接。
        # 写入配置/属性，影响后续行为
        """
        self.running = False
        # 条件分支处理
        if self.ws:
            # 关闭连接释放资源
            # 释放连接/窗口资源
            self.ws.close()
