# B站舆情监控工具箱交接总纲

> 项目根目录：`D:\tasks\cola\bili_ops_toolbox`
> 当前文档按现有源码和实际 SQLite schema 编写。先读本文件，再按主题进入专项文档。

## 1. 项目定位

这是一个面向 B 站内容运营和舆情观察的本地工具箱，核心能力包括：

- 热点：分区榜单、视频标签词云、官方活动和重点运营号动态、AI 选题。
- 评论：热门/普通/完整评论采集、增量采集、四层去重、情感分析、关键词和预警。
- UP 主：分区头部 UP 榜单、公开数据采集、多源降级、运营策略拆解。
- 自诊：账号公开数据分析、基准对比、Markdown/PDF 报告。
- 运行形态：FastAPI WebUI；PyQt5 桌面壳内嵌 WebUI；PyInstaller 单文件 exe。
- 后台任务：FastAPI lifespan 启动 Cookie 巡检和评论常驻监控两个 asyncio 循环。

项目是个人运营分析工具，必须遵守 B 站服务条款、robots/接口约束和合理请求频率；不要把它改造成高并发或绕过风控的抓取器。

## 2. 当前状态

- 异常统一格式化已上线：`core/exceptions.py` 的 `format_exception()`，Web 未捕获异常返回结构化错误。
- 可重试判断已上线：`is_retryable_error()` 与 `bilibili/api.py` 请求循环配合；Cookie 失效 `-101`、风控 `-352/-412` 不做短重试，429/`-509` 走退避。
- Cookie 迁移已完成：`bilibili/cookie_pool.py` 读取旧明文失败时会尝试兼容并加密回写；新数据由 `ConfigManager` 的 Fernet 加密体系管理。迁移前先备份数据库和 `config/.key`、`config/.secrets`。
- `tools/archive/` 是归档区，不是当前测试入口。里面保留历史验收、临时诊断和已废弃脚本；其中 `dead_*.py` 明确不应接回生产调用链。
- 当前源码目录中仍有 `.tmp/.fixtmp` 和缓存目录，它们不是运行入口，提交/交接时不要把临时文件当实现依据。
- 项目没有发现实际 APScheduler 注册代码；`requirements.txt` 虽包含 APScheduler，但当前常驻任务由 `core/monitor_service.py` 的 asyncio 循环负责。

## 3. 目录职责

```text
bili_ops_toolbox/
├─ bilibili/                 # 外部 B 站接口层：WBI、统一请求、限流、Cookie、扫码登录
│  ├─ api.py                 # BilibiliAPI、WBISigner、重试和业务便捷方法
│  ├─ auth.py                # 二维码登录、Cookie 校验/解析
│  ├─ cookie_pool.py         # 多 Cookie 轮换、失效检查、旧数据迁移
│  └─ rate_limiter.py        # 令牌桶、抖动、429退避、熔断、多端点限流
├─ core/                     # 基础设施和跨模块契约
│  ├─ config.py              # YAML + user_config + 加密 secrets
│  ├─ database.py            # SQLAlchemy ORM、SQLite 会话、备份
│  ├─ exceptions.py          # 异常类型、格式化和可重试判断
│  ├─ logger.py              # 普通日志与 risk_control.log
│  └─ monitor_service.py     # 常驻 Cookie/评论任务和 SQLite 状态
├─ modules/                  # 业务编排，不直接复制通用请求逻辑
│  ├─ hotspot/               # 榜单、标签、活动、选题
│  ├─ comment/               # 评论采集、去重、情感、监控预警
│  ├─ up_analyzer/           # UP 数据采集和策略分析
│  └─ self_diagnosis/        # 自诊分析和报告
├─ web/                      # FastAPI 与前端
│  ├─ main.py                # lifespan、路由挂载、静态页、WebSocket
│  ├─ routers/               # auth/config/logs/hotspot/comment/analysis
│  └─ frontend/              # index.html、app.js、style.css、ECharts
├─ desktop/                  # PyQt 主窗口、桌宠、首次运行向导
├─ llm/                      # OpenAI 兼容客户端和用量统计
├─ config/                   # config.yaml；.key/.secrets 为敏感文件
├─ data/                     # bili_ops.db 与 data/logs
├─ tools/                    # 当前测试脚本；archive 为历史归档
├─ dist/                     # 已构建 exe 和运行时数据样例
├─ build/                    # PyInstaller 中间产物，不是源码
├─ build.spec                # PyInstaller 配置
├─ start_web.py              # Web 初始化及 uvicorn 启动
├─ start_desktop.py          # 桌面启动
└─ main.py                   # 源码主入口/初始化逻辑
```

## 4. 数据流

```text
WebUI/桌面操作或常驻任务
  → routers / monitor_service
  → modules 业务编排
  → bilibili.BilibiliAPI / auth
  → RateLimiter.acquire()
  → CookiePool.get_cookie() 轮换有效 Cookie
  → WBI 签名（需要时）
  → B站接口
  → 统一解析 code/data + 异常/退避
  → 模块清洗、去重、情感、统计
  → SQLAlchemy Session 写入 data/bili_ops.db
  → router JSON / WebSocket / 前端图表 / 导出报告
```

失败路径：网络/超时/5xx 可重试；429、`-509` 等等待退避；`-101` 标记 Cookie 失效；`-352/-412` 记录风控并停止短重试。所有风控事件看 `data/logs/risk_control.log`。

## 5. 启动与交接顺序

1. 复制并保护 `config/`、`data/bili_ops.db`，确认不要把 `.key/.secrets` 发到群或提交到公共仓库。
2. 建立 Python 虚拟环境并安装 `requirements.txt`。
3. 运行 `python start_web.py --init-only`，确认配置、数据库、`logs/data/backups` 可创建。
4. 运行 `python start_web.py --host 127.0.0.1 --port 8000`，打开 `http://127.0.0.1:8000/`；健康检查为 `/health`，OpenAPI 为 `/docs`。
5. 桌面模式运行 `python start_desktop.py`；桌面端会通过 Web 服务提供业务能力。
6. 先验证 `/health`、登录态、Cookie 池，再进行真实采集；从小 limit 开始。

## 6. 修改规则

- B站接口路径/参数/签名/错误码：只在 `INTERFACES.md` 指引的同步点修改，并补测试。
- 数据库模型变化：同步 ORM、已有数据库迁移/兼容策略、Web 序列化和备份说明；`create_all()` 不会自动修改已有列。
- 路由变化：同步 `web/routers`、前端 `app.js`、`/docs` 验证和专项文档。
- 常驻任务变化：同步 `core/monitor_service.py`、`web/main.py` lifespan、配置项和状态表。

专项文档：
- [INTERFACES.md](INTERFACES.md)：B站接口和同步点
- [ARCHITECTURE.md](ARCHITECTURE.md)：调用链、模块边界
- [DATABASE.md](DATABASE.md)：14 张表和备份/变更
- [OPERATIONS.md](OPERATIONS.md)：部署、配置、exe 打包
- [SCHEDULE.md](SCHEDULE.md)：常驻任务、间隔、启停
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md)：排障手册
