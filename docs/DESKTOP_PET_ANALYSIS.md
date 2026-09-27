# 桌宠模块现状分析报告

- 项目：`bili_ops_toolbox`
- 分析范围：桌宠窗口、桌面启动编排、WebSocket 服务端、预警生产链路及相关文档
- 分析结论：当前可以安全增加“低侵入”的桌宠功能；涉及消息协议、生命周期、重连策略或业务事件分发的改动，需要先补齐边界和测试，否则容易影响桌面启动与常驻监控。

## 1. 文件与职责

| 文件 | 关键对象/函数 | 当前职责 | 与桌宠耦合度 |
|---|---|---|---|
| `desktop/pet_window.py` | `WebSocketThread`、`PetWindow`、`run_pet_window` | WebSocket 客户端、Qt 悬浮窗、状态机、拖拽、通知气泡、资源清理 | 核心，高 |
| `desktop/main_window.py` | `MainWindow.__init__`、`init_ui`、`start_web_service`、`quit_application`、`run_desktop_app` | 主窗口、Web 服务生命周期、托盘和退出 | 间接，中 |
| `start_desktop.py` | `main` | 创建 `QApplication`、`MainWindow`、`PetWindow`，进入 Qt 事件循环 | 直接，高 |
| `desktop/__init__.py` | `MainWindow`、`PetWindow` 导出 | 桌面包公共入口 | 低 |
| `web/main.py` | `ConnectionManager`、`websocket_endpoint`、`push_alert` | `/ws` 连接池、接收心跳、向所有客户端广播预警 | 协议核心，高 |
| `web/routers/comment.py` | `get_monitor` | 延迟导入 `push_alert`，将评论监控器的预警回调接到 WebSocket 广播 | 桥接，高 |
| `modules/comment/monitor.py` | `CommentMonitor.__init__`、监控循环及回调调用处 | 采集评论、生成预警并异步调用 `alert_callback` | 业务侧，中 |
| `docs/ACCEPTANCE.md` | 桌宠验收条目 | 记录四态、拖拽、WebSocket 和通知气泡验收要求 | 文档，低 |

## 2. 模块内部结构

### 2.1 `WebSocketThread`：通信层

文件：`desktop/pet_window.py:63-214`

- `__init__(ws_url)`：保存服务地址、WebSocket 对象和运行标记。
- `run()`：在 `QThread` 中创建 `websocket.WebSocketApp`，注册 `on_open`、`on_message`、`on_error`、`on_close`，并阻塞运行 `run_forever()`。
- `on_open(ws)`：发出 `connected` 信号，并额外启动一个 Python 心跳线程，每 30 秒发送 `{"type": "ping"}`。
- `on_message(ws, message)`：JSON 解码后发出 `message_received(dict)`。
- `on_error`、`on_close`：记录错误/断开并发出 Qt 信号。
- `stop()`：设置 `running=False`，关闭 WebSocket。

内部耦合度：高。线程、WebSocket 客户端、心跳线程和 Qt 信号都集中在同一个类中。好处是改动边界清晰；风险是新增重连、鉴权、协议版本或多个消息类型时容易继续膨胀。

### 2.2 `PetWindow`：展示层与交互层

文件：`desktop/pet_window.py:220-612`

- `__init__(ws_port=8000)`：构造 URL、初始化状态和拖拽位置，调用 `init_ui()` 与 `setup_websocket()`。
- `init_ui()`：设置无边框、置顶、透明窗口，创建图片标签和通知标签，加载占位图并定位到屏幕右下角。
- `load_placeholder_images()`：为四种状态生成纯色圆形 `QPixmap`。目前没有外部素材加载逻辑。
- `set_state(state)`：修改 `current_state` 并切换图片。
- `setup_websocket()`：实例化 `WebSocketThread`，连接四个 Qt 信号并启动线程。
- `on_ws_connected`、`on_ws_disconnected`、`on_ws_error`：处理连接状态；断线目前只记日志，没有重连或离线图状态。
- `on_ws_message(data)`：仅识别 `type == "alert"`，并转交 `show_notification`。
- `show_notification(alert_data)`：切换通知状态、把标题和消息写入 QLabel，5 秒后调用 `hide_notification()`。
- `hide_notification()`：隐藏气泡并恢复待机状态。
- `mousePressEvent`、`mouseMoveEvent`、`mouseReleaseEvent`：实现左键拖拽和状态切换。
- `mouseDoubleClickEvent`：预留快捷菜单，目前只显示点击状态 500ms。
- `closeEvent`：停止 WebSocket 线程并等待线程结束。
- `run_pet_window()`：独立运行入口，当前桌面主流程没有调用它，而是由 `start_desktop.py` 集成运行。

内部耦合度：高。UI 创建、状态机、消息解析、通知展示和输入事件都在 `PetWindow` 内；`show_notification` 还直接依赖消息字典字段和 Qt 富文本行为。

### 2.3 服务端 WebSocket：传输与广播层

文件：`web/main.py:234-329`

- `ConnectionManager.__init__()`：维护 `active_connections` 列表。
- `connect(websocket)`：接受连接并登记。
- `disconnect(websocket)`：从列表删除连接。
- `broadcast(message)`：逐个异步 `send_json`，单个连接失败只记录日志。
- `websocket_endpoint(websocket)`：接受客户端连接，持续读取客户端文本消息，断开时移除连接。
- `push_alert(alert)`：包装为 `{type: "alert", data: alert}` 后广播。

服务端与桌宠通过固定的 `/ws` 地址和 `type/data` 字典协议耦合。当前服务端并不区分桌宠、前端或其他客户端，所有连接都会收到相同广播。

### 2.4 业务预警桥接

文件：`web/routers/comment.py:115-130`、`modules/comment/monitor.py:822-848`

`get_monitor()` 延迟导入 `web.main.push_alert`，创建 `CommentMonitor(..., alert_callback=push_alert)`。评论监控在产生预警后异步执行 `await self.alert_callback(alert_data)`。因此实际链路为：

```text
CommentMonitor
  -> alert_callback
  -> web.main.push_alert
  -> ConnectionManager.broadcast
  -> /ws
  -> WebSocketThread.on_message
  -> Qt message_received
  -> PetWindow.on_ws_message
  -> PetWindow.show_notification
```

这条链路的耦合度高，但职责边界仍可识别：业务模块只依赖回调，桌宠只依赖 WebSocket 协议，没有直接导入评论监控代码。

## 3. 外部调用关系

### 启动关系

```text
start_desktop.main
  -> QApplication
  -> MainWindow(web_port=8000)
       -> start_web_service()
       -> 源码环境启动 start_web.py 子进程
       -> 冻结环境在线程内启动 uvicorn
  -> PetWindow(ws_port=8000)
       -> setup_websocket()
       -> WebSocketThread.start()
```

`MainWindow` 和 `PetWindow` 是同一个 Qt 事件循环中的两个窗口。桌宠依赖本机 `8000/ws`，因此 Web 服务未启动或端口不一致时桌宠只能离线。

### 服务关系

```text
FastAPI app
  -> /ws websocket_endpoint
  -> ConnectionManager.active_connections
  -> push_alert
  -> comment.get_monitor 的 alert_callback
  -> CommentMonitor 预警回调
```

`desktop/main_window.py` 本身不调用 `PetWindow`；桌宠的创建只发生在 `start_desktop.py`。这使主窗口与桌宠相对解耦，但桌面统一入口仍承担两者的装配职责。

## 4. 耦合与可扩展性判断

### 当前可以安全扩展的部分

1. 增加新的纯展示状态：在 `PetWindow` 增加状态常量、图片资源和 `set_state` 映射，风险较低。
2. 替换桌宠素材：将 `load_placeholder_images()` 替换为资源加载器，保持 `self.images[state] -> QPixmap` 接口不变，兼容性最好。
3. 增加双击菜单：在 `mouseDoubleClickEvent` 中调用独立的菜单/动作类，不要把业务请求直接写进窗口事件函数。
4. 增加通知格式化：新增独立 `format_alert_message()`，让 `show_notification()` 只负责渲染。
5. 增加客户端本地设置：新增 `PetSettings` 或配置适配器，避免把位置、透明度、开关继续堆进 `PetWindow`。

### 需要先做架构准备的扩展

1. 自动重连和退避：需要改 `WebSocketThread` 生命周期，明确线程退出、重连次数、定时器归属，不能只在 `on_ws_disconnected` 中递归调用 `start()`。
2. 多类消息协议：应先定义消息类型和字段校验，再扩展 `on_ws_message`；不要对任意字典直接取字段。
3. 桌宠专属消息/定向推送：需要服务端增加客户端注册或频道字段，否则新消息会广播给所有 `/ws` 客户端。
4. 桌宠操作反向调用业务：建议增加明确的本地 API/命令层，不要让 UI 线程直接调用数据库、B站 API 或评论监控对象。
5. 多桌宠实例或独立进程：当前全局连接池和固定端口设计没有实例身份，需要先设计 session/client_id 和关闭策略。

### 结论

当前可以安全加新功能，但安全边界是“桌宠内部展示与交互增强”。涉及服务端协议、重连、业务控制、跨线程任务和进程生命周期的功能，建议先抽象接口并补测试后再改。整体扩展性评估：中等偏上，现有链路可用但基础设施仍偏集中式。

## 5. 风险点

- **缺少重连机制**：`on_ws_disconnected()` 只记录日志，网络短暂中断后桌宠不会自动恢复。
- **心跳线程不可管理**：`on_open()` 每次连接都创建匿名线程，没有保存引用；未来加入重连后可能产生多个心跳线程。
- **线程关闭可能阻塞**：`closeEvent()` 调用 `stop()` 后无超时 `wait()`，WebSocket 库未及时返回时可能拖住 Qt 退出。
- **广播失败连接不清理**：`ConnectionManager.broadcast()` 只记录发送异常，不移除失效连接；长期运行可能累积坏连接。
- **连接列表变更边界不足**：广播期间连接断开或列表被修改时，当前实现没有快照和统一清理策略。
- **协议校验薄弱**：客户端只判断 `type`，`data` 非字典、字段类型异常时可能导致展示异常。
- **通知存在富文本注入风险**：`show_notification()` 使用 `setText(f"<b>{title}</b><br>{message}")`，外部预警文本会按 Qt 富文本解释；应使用纯文本转义或分别设置标题/正文。
- **通知定时器互相覆盖**：连续预警会建立多个 5 秒定时器，旧定时器可能把新通知提前切回待机。
- **端口和地址硬编码**：`start_desktop.py`、`PetWindow`、`MainWindow` 默认使用 8000；改端口需同步多个入口。
- **素材和运行资源边界不统一**：当前图片是代码生成的占位图，换成资源后必须同时考虑源码路径、PyInstaller `datas` 和冻结环境路径。
- **缺少桌宠自动化测试**：仓库当前验收文档有手工项，但缺少对协议解析、状态切换、关闭清理和广播异常的单元测试。

## 6. 推荐扩展方式

建议按以下层次逐步演进：

1. **协议层**：定义 `PetMessage` 数据结构，至少校验 `type`、`data`、版本字段；未知消息安全忽略并记录日志。
2. **传输层**：把 `WebSocketThread` 的连接、重连、心跳和停止封装为独立客户端类，Qt 线程只负责把结果转成信号。
3. **状态层**：把状态常量、状态转移和超时策略集中到 `PetStateController`，`PetWindow` 只负责显示。
4. **展示层**：将图片加载、通知格式化、气泡管理拆成小组件；为连续通知设计单一可取消定时器。
5. **业务适配层**：服务端通过事件/频道适配器推送桌宠事件，避免桌宠直接依赖 `CommentMonitor`、数据库或B站 API。
6. **测试层**：先覆盖 JSON 消息解析、非法消息、断线清理、四态切换、通知覆盖和关闭线程，再接入真实 Qt/WebSocket 集成测试。

推荐的最小改造顺序是：先修复消息校验和通知纯文本渲染，再抽出 `PetMessage`/`PetStateController`，最后实现可控重连和定向推送。这样可以保持现有 `start_desktop.py` 装配方式不变，降低回归范围。
