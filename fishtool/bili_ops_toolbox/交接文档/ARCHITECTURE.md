# 架构与维护边界

## 1. 运行入口

- `python start_web.py`：先 `init_project()` 初始化配置、数据库、目录，再由 uvicorn 加载 `web.main:app`。
- `python start_desktop.py`：PyQt 桌面入口；主窗口内嵌 WebUI，首次运行由向导写入加密配置。
- `web/main.py` lifespan：再次确保 ConfigManager 和数据库全局单例可用，创建 `ResidentCommentMonitor`，启动后台循环，关闭时取消任务。
- `main.py`：源码级初始化/入口逻辑；维护 Web 服务优先看 `start_web.py` 与 `web/main.py`。

注意：`config/config.yaml` 的 server 默认端口是 8080，但 `start_web.py` CLI 默认端口为 8000；实际以启动参数为准。CORS 当前白名单同时覆盖 8000/8080 本机地址。

## 2. 分层依赖

```text
web/frontend
  ↓ HTTP/WebSocket
web/routers
  ↓
modules/* 业务服务
  ↓                 ↘ llm/client.py
bilibili/* 外部API    core/database.py
  ↓                    core/config/logger/exceptions
B站公开接口             SQLite / 文件日志
```

允许依赖方向：Web → modules/core；modules → bilibili/core/llm；bilibili → core。避免让 `core` 反向导入 Web 或具体业务模块。

## 3. 核心对象

- `ConfigManager`：三层配置，主 YAML → user YAML → 加密 secrets。exe 环境以 exe 所在目录为持久化根。
- `DatabaseManager`：SQLite + SQLAlchemy + StaticPool，模块级 `init_database/get_session/get_db`。
- `BilibiliAPI`：aiohttp 会话、统一 `code/data` 解析、WBI、Cookie 和重试。
- `MultiEndpointRateLimiter`：normal/comment/dynamic 独立策略。
- `CookiePool`：数据库和内存双状态、轮换、有效性检查、旧明文迁移。
- `ResidentCommentMonitor`：常驻任务控制面，状态保存在 `monitor_state`。
- `ConnectionManager`：`/ws` WebSocket 广播，用于桌宠/告警。

## 4. 业务模块

- 热点：`TagCloudGenerator` 拉榜单和标签；`ActivityTracker` 合并官方活动与运营号动态；`TopicGenerator` 生成/保存选题。
- 评论：`CommentCollector` 拉取与增量入库；`CommentDeduplicator` 分层去重；`SentimentAnalyzer` 词典优先、LLM 可选；`CommentMonitor` 生成预警。
- UP 分析：`UPDataFetcher` 按第三方站点 → B站公开数据 → 本地估算降级。源码已注明 zeroroku 端点是假设格式，不能把它当稳定生产依赖。
- 自诊：拉公开账号和投稿数据，计算可得指标，报告生成器导出 Markdown/PDF。

## 5. Web API 边界

路由前缀：

- `/api/auth`：扫码登录、轮询、退出、登录状态。
- `/api/config`：普通配置、LLM 配置和用量。
- `/api/logs`：日志查询、导出和类型。
- `/api/hotspot`：分区、标签云异步任务、活动、选题。
- `/api`：UP 分析任务、自诊和报告导出。
- `/api/comment`：采集、监控、看板、常驻任务、预警、关键词。
- `/health`、`/docs`、`/ws`：健康、OpenAPI、WebSocket。

前端是单页静态实现，主要契约集中在 `web/frontend/static/js/app.js`。后端字段改名必须搜索该文件同步。

## 6. 数据一致性约定

- `BilibiliAPI.request()` 默认剥离 B站外层 `data`；便捷方法部分重新包装为 `{'data': ...}`。新增方法必须明确采用哪种形式，避免双层/少层 data。
- 评论唯一键是 `rpid`，视频唯一键是 `bvid/aid`，账号唯一键是 `uid`，UP 主唯一键是 `mid`。
- 常驻监控用 SQLite checkpoint/唯一键实现幂等；分页时发现重复 rpid 立即停止。
- SQLAlchemy `create_all()` 只创建缺表，不迁移已有表列。模型变更必须提供迁移步骤。
- Cookie、LLM API Key 不进入普通 YAML、日志、接口响应或交接文档。

## 7. 新功能落点

新增 B站数据源：在 `bilibili/api.py` 建便捷方法；若仅某业务一次使用，可在模块直接调用统一 `api.get()`，但仍必须复用签名、Cookie、限流。

新增业务功能：在 `modules/<domain>` 封装处理，router 只做参数校验、调用和 JSON/HTTP 错误转换。

新增前端页面：同步 `index.html`、`app.js`、`style.css`，保持对应 router 的请求/响应契约，并在桌面 WebView 中验证。

新增后台任务：接入 FastAPI lifespan 的统一启动/关闭，提供持久化状态、幂等启动、停止事件、退避和可观测日志；不要在 import 阶段创建 asyncio task。
