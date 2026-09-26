"""桌宠 WebSocket 信号桥接。

拆分自原 pet_window.py 的 PetWindow 通信部分：
负责建立 WebSocket 线程并把线程信号桥接到主线程槽函数。
"""
from .assets import logger
from .ws_thread import WebSocketThread


class WsMixin:
    """WebSocket 通信混入：连接、信号桥接与消息处理。"""

    def setup_websocket(self):
        """设置WebSocket连接
        # 写入配置/属性，影响后续行为
        
        创建独立线程运行 WebSocket 客户端，连接到后端 Web 服务。
        # 实例化对象并准备使用
        接收实时推送的预警消息和状态更新。
        # 用新值覆盖旧值，保持数据一致
        """
        self.ws_thread = WebSocketThread(self.ws_url)
        
        # 连接信号
        # 将线程信号桥接到主线程槽函数
        self.ws_thread.message_received.connect(self.on_ws_message)
        # 建立连接
        self.ws_thread.connected.connect(self.on_ws_connected)
        # 建立连接
        self.ws_thread.disconnected.connect(self.on_ws_disconnected)
        # 建立连接
        self.ws_thread.error.connect(self.on_ws_error)
        
        # 启动WebSocket线程
        # 触发服务/线程开始运行
        self.ws_thread.start()
        
        logger.info("[桌宠] WebSocket连接中...")

    def on_ws_connected(self):
        """WebSocket连接成功"""
        logger.info("[桌宠] WebSocket已连接")
        # 设置state属性
        self.set_state(self.STATE_IDLE)

    def on_ws_disconnected(self):
        """WebSocket断开连接"""
        logger.warning("[桌宠] WebSocket已断开，显示离线状态")

    def on_ws_error(self, error: str):
        """WebSocket错误"""
        logger.error(f"[桌宠] WebSocket错误: {error}")

    def on_ws_message(self, data: dict):
        """处理WebSocket消息
        # 对数据进行加工/分发
        
        根据消息类型执行不同操作：
        - alert: 显示预警通知气泡
        # 将内容呈现到界面上
        - ping/pong: 心跳保活（自动处理）
        # 对数据进行加工/分发
        
        Args:
            data: WebSocket 消息数据字典
        """
        msg_type = data.get('type')
        
        # 边界/有效性检查
        if msg_type == 'alert':
            # 收到预警消息，显示通知
            # 将内容呈现到界面上
            alert_data = data.get('data', {})
            self.show_notification(alert_data)
