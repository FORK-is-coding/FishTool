# 项目结构说明

```
FishTool/
├── app.py                       # 唯一启动入口（默认桌面版；app.py web 为纯 Web）
├── start_web.bat                # Windows 一键启动 Web 版并打开浏览器
├── requirements.txt             # 依赖列表
├── build.spec                   # PyInstaller 打包配置
├── README.md / QUICKSTART.md    # 项目说明与上手步骤
├── STRUCTURE.md                 # 本文件
├── CHANGELOG.md                 # 变更记录
│
├── config/                      # 配置目录
│   ├── config.yaml             # 默认配置
│   ├── budget.yaml             # 请求预算配置
│   ├── user_config.yaml        # 用户自定义配置（运行时生成）
│   ├── .secrets                # 加密的敏感配置（运行时生成）
│   └── .key                    # 加密密钥（运行时生成）
│
├── core/                        # 核心基础设施
│   ├── config.py               # 配置管理器（支持加密、热重载）
│   ├── logger.py               # 日志系统（分级、风控专用、结构化 JSON）
│   ├── database/               # SQLAlchemy ORM：模型、仓储、迁移与备份
│   ├── exceptions.py           # 自定义异常类
│   ├── error_policy.py         # 异常分级：低优先级记日志，高优先级另发 toast
│   ├── toast.py                # 进程内 toast 总线（Web / 桌面 sink 注册）
│   └── ...                     # 缓存、监控、配额、请求预算、数据质量
│
├── bilibili/                    # B站API封装
│   ├── api/                    # 请求客户端、WBI 签名、UP主数据
│   ├── auth.py                 # 扫码登录
│   ├── cookie_pool.py          # Cookie池管理
│   └── rate_limiter.py         # 限频器（令牌桶+429退避+熔断）
│
├── llm/                         # LLM接口
│   └── client.py               # OpenAI兼容接口
│
├── modules/                     # 业务模块
│   ├── hotspot/                # 热点发现、事件追踪、选题生成
│   ├── comment/                # 评论采集、去重、情感分析、舆情预警
│   ├── lottery/                # 抽奖号识别与真人筛选
│   ├── self_diagnosis/         # 账号自诊与报告生成
│   └── up_analyzer/            # 头部UP主拆解与运营策略
│
├── web/                         # WebUI（FastAPI）
│   ├── main.py                 # FastAPI 应用与 /ws 推送
│   ├── local_guard.py          # 本机 token 校验
│   ├── routers/                # 各业务路由（评论、热点、抽奖、自诊、日志…）
│   └── frontend/               # 前端资源（templates/ + static/）
│
├── desktop/                     # 桌面壳（PySide6 + QtWebEngine）
│   ├── main_window.py          # 主窗口
│   ├── pet_window/             # 桌宠悬浮窗
│   └── ...                     # 主题、图标、WebEngine profile、报告导出
│
├── integrations/astrbot/        # AstrBot 插件（适配器、发布工具、构建脚本）
├── tests/                       # 测试
├── tools/                       # 开发脚本（异常处理检查等）
├── docs/                        # 规范与设计文档
├── assets/                      # 图标等静态资源
│
└── data/                        # 数据目录（运行时生成）
    ├── bili_ops.db             # SQLite数据库
    ├── cookies/                # Cookie存储
    ├── logs/                   # 日志文件（app / error / crawler / risk_control）
    ├── exports/                # 导出文件
    └── cache/                  # 缓存
```

## 已完成模块说明

### core/ - 核心基础设施
- **config.py**: 配置管理器
  - 支持多级配置（点号分隔访问）
  - 敏感信息Fernet加密存储
  - 热重载支持

- **logger.py**: 日志系统
  - 分级日志（应用/错误/爬虫/风控）
  - 彩色控制台输出（Windows ANSI支持）
  - 结构化JSON日志
  - 风控事件专用记录器
  - 日志导出和查询功能
  - 日志默认写到 `data/logs/`，控制台着色不会污染文件与 JSON 输出的 level

- **database/**: 数据库层
  - 模型按业务拆分（账号、视频、评论、热点、额度、系统…）
  - SQLAlchemy ORM映射与仓储封装
  - 自动迁移与备份功能

- **exceptions.py**: 异常类体系
  - 20+自定义异常类
  - 统一错误码
  - 异常可重试判断

- **error_policy.py** / **toast.py**: 统一异常处理与通知
  - `report_exception` / `log_low_priority` / `report_high_priority` 分级入口
  - 低优先级只写日志，高优先级写 ERROR 日志并投递 toast
  - toast 通过 `/ws` 推送前端与桌宠，桌面壳用系统托盘气泡
  - 规范与检查：`docs/EXCEPTION_HANDLING.md`、`tools/check_exception_handling.py`

### bilibili/ - B站API封装
- **api/**: API基础层
  - 完整WBI签名算法实现
  - 自动密钥更新（每小时）
  - 请求重试机制（指数退避）
  - 异常统一处理

- **rate_limiter.py**: 智能限频器
  - 令牌桶算法
  - 429退避策略（30s→10min）
  - 连续失败熔断保护
  - 多端点独立限频

- **cookie_pool.py**: Cookie池管理
  - 多账号轮换
  - 自动有效性检查
  - 失效标记和提醒

- **auth.py**: 扫码登录
  - 二维码生成
  - 状态轮询
  - Cookie提取

### modules/ - 业务模块
- **hotspot/**: 热点发现、事件追踪、词云与选题生成
- **comment/**: 评论采集、去重、情感分析与舆情预警
- **lottery/**: 抽奖号识别与真人筛选
- **self_diagnosis/**: 账号自诊、基准对比与报告生成
- **up_analyzer/**: 头部UP主数据拆解与运营策略

### llm/ - LLM接口
- **client.py**: OpenAI兼容客户端
  - 支持OpenAI/Azure/自定义端点
  - 批量处理
  - 流式输出
  - 每日Token限额统计

## 维护重点

1. **热点发现 / 评论监控** - 采集稳定性与告警准确度
2. **账号自诊与导出** - PDF 报告排版、图表与字体兼容
3. **AstrBot 插件** - 适配器行为、发布打包与引擎托管

### 数据流示例
```
用户操作 → WebUI → FastAPI → 业务模块 → B站API
                                    ↓
                              数据库存储
                                    ↓
                              LLM分析（可选）
                                    ↓
                              结果展示/预警
```

## 技术特点

1. **松耦合架构**: 各模块独立，易于维护和扩展
2. **异步优先**: 使用asyncio提升性能
3. **防风控设计**: 
   - 严格限频
   - 429自动退避
   - 连续失败熔断
   - Cookie池轮换
4. **数据安全**: 
   - Cookie加密存储
   - 配置文件权限保护
   - 本地SQLite存储
5. **可观测性**: 
   - 分级日志
   - 风控事件追踪
   - Token用量统计
   - 操作审计日志
