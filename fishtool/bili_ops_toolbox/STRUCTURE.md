# 项目结构说明

```
bili_ops_toolbox/
├── main.py                      # 主程序入口
├── requirements.txt             # 依赖列表
├── README.md                    # 项目说明
├── PROGRESS.md                  # 开发进度记录
│
├── config/                      # 配置目录
│   ├── config.yaml             # 默认配置
│   ├── user_config.yaml        # 用户自定义配置（运行时生成）
│   ├── .secrets                # 加密的敏感配置（运行时生成）
│   └── .key                    # 加密密钥（运行时生成）
│
├── core/                        # 核心模块
│   ├── __init__.py
│   ├── config.py               # 配置管理器（支持加密、热重载）
│   ├── logger.py               # 日志系统（分级、风控专用）
│   ├── database.py             # 数据库模型（SQLAlchemy ORM）
│   └── exceptions.py           # 自定义异常类
│
├── bilibili/                    # B站API封装
│   ├── __init__.py
│   ├── api.py                  # API基础类（WBI签名、请求封装）
│   ├── auth.py                 # 扫码登录
│   ├── cookie_pool.py          # Cookie池管理
│   └── rate_limiter.py         # 限频器（令牌桶+429退避+熔断）
│
├── llm/                         # LLM接口
│   ├── __init__.py
│   └── client.py               # OpenAI兼容接口
│
├── modules/                     # 功能模块（待实现）
│   ├── __init__.py
│   ├── hotspot/                # 热点发现
│   │   ├── tag_cloud.py       # 分区tag词云
│   │   ├── activity.py        # 活动情报
│   │   └── topic_generator.py # AI选题助手
│   │
│   ├── analysis/               # 头部拆解
│   │   ├── upmaster.py        # UP主分析
│   │   └── strategy.py        # 运营策略拆解
│   │
│   ├── comment/                # 评论监控
│   │   ├── crawler.py         # 评论采集
│   │   ├── deduplicator.py    # 评论去重
│   │   ├── sentiment.py       # 情感分析
│   │   └── alert.py           # 舆情预警
│   │
│   ├── filter/                 # 真人筛选
│   │   └── spam_detector.py   # 抽奖号识别
│   │
│   ├── diagnosis/              # 账号自诊
│   │   ├── data_collector.py  # 数据采集
│   │   └── report_generator.py # 报告生成
│   │
│   └── export/                 # 导出功能
│       ├── pdf_exporter.py    # PDF导出
│       └── md_exporter.py     # Markdown导出
│
├── gui/                         # 图形界面（待实现）
│   ├── __init__.py
│   ├── main_window.py          # 主窗口（PyQt6 + WebView）
│   └── pet_window.py           # 桌宠悬浮窗
│
├── web/                         # WebUI（待实现）
│   ├── __init__.py
│   ├── app.py                  # FastAPI应用
│   ├── api/                    # API路由
│   ├── static/                 # 静态资源
│   │   ├── css/
│   │   ├── js/
│   │   └── images/
│   └── templates/              # 模板
│
└── data/                        # 数据目录（运行时生成）
    ├── bili_ops.db             # SQLite数据库
    ├── cookies/                # Cookie存储
    ├── logs/                   # 日志文件
    │   ├── app.log
    │   ├── error.log
    │   ├── crawler.log
    │   └── risk_control.log
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
  
- **database.py**: 数据库模型
  - 13张表覆盖所有业务场景
  - SQLAlchemy ORM映射
  - 自动备份功能
  
- **exceptions.py**: 异常类体系
  - 20+自定义异常类
  - 统一错误码
  - 异常可重试判断

### bilibili/ - B站API封装
- **api.py**: API基础类
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
  - Cookie提取（待完善）

### llm/ - LLM接口
- **client.py**: OpenAI兼容客户端
  - 支持OpenAI/Azure/自定义端点
  - 批量处理
  - 流式输出
  - 每日Token限额统计

## 下一步开发重点

### 优先级P0（核心功能）
1. **热点发现模块** - 让用户快速发现创作机会
2. **评论监控模块** - 核心舆情分析能力
3. **WebUI基础框架** - 用户交互界面

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
