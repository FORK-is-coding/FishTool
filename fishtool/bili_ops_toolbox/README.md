# B站个人自媒体运营工具箱

**为B站UP主打造的智能运营助手 - 桌面版**

## ✨ 核心功能（阶段三已完成）

### 🔥 热点发现模块
- **分区热门Tag词云**：自动抓取分区热门榜，生成可视化词云
- **活动情报追踪**：官方活动+UGC运营号动态双源监控
- **AI选题助手**：基于真实热点生成创意选题，支持选题库管理

### 💬 评论监控模块
- **智能采集**：快速/普通/完整三档策略，支持增量采集
- **四层去重**：
  - 同用户复读折叠
  - 跨用户同内容聚合（高声量信号）
  - SimHash+编辑距离模糊匹配
  - 时间窗口热点检测
- **情感分析**：词典规则+LLM可选增强
- **舆情预警**：负面突增/评论暴涨/风险关键词实时监控

### 📊 头部拆解模块 ⭐NEW
- **分区头部UP主**：一键获取分区TOP榜单
- **运营策略分析**：LLM驱动的5维度拆解
  - 选题方向/标题套路/封面风格/发布节奏/互动引导
  - 每维度含：核心观察+具体打法+关键要点
- **多源数据获取**：zeroroku三方站点 > B站公开页 > 本地估算
- **数据完整性透明**：清晰标注能拿到什么、拿不到什么

### 🩺 账号自诊模块 ⭐NEW
- **全量数据采集**：粉丝/播放/互动率/投稿节奏
- **数据可得性清单**：
  - ✅ 能获取：粉丝数、播放量、评论数、投稿列表
  - ❌ 不可得：完播率、观众画像、流量来源（仅创作中心）
- **Benchmark对比**：与分区数据对比，计算排名分位
- **诊断报告导出**：Markdown/PDF格式，包含改进建议

### 🖥️ 桌面客户端 ⭐NEW
- **主窗口**：内嵌WebView，系统托盘常驻
- **桌宠悬浮窗**：WebSocket实时通信，4态切换（待机/拖拽/点击/通知）
- **消息推送**：评论预警、风控提醒实时弹窗

## 🚀 快速开始

### 方式一：桌面应用（推荐）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动桌面应用
python start_desktop.py
```

### 方式二：Web版本

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动Web服务
python start_web.py

# 3. 浏览器访问
http://localhost:8000
```

### 方式三：打包为exe

```bash
# 执行打包脚本
python build.py

# 生成的exe位于
dist/FishTool.exe
```

详细使用教程见：[QUICKSTART.md](QUICKSTART.md)

## 📁 项目结构

```
bili_ops_toolbox/
├── bilibili/           # B站API封装（WBI签名、限频器、Cookie池）
├── core/               # 核心基础设施（配置、数据库、日志、异常）
├── llm/                # LLM客户端（OpenAI兼容）
├── modules/            # 功能模块
│   ├── hotspot/        # 热点发现（Tag词云、活动、选题）
│   ├── comment/        # 评论监控（采集、去重、情感、预警）
│   ├── up_analyzer/    # 头部拆解（数据采集、策略分析） ⭐NEW
│   └── self_diagnosis/ # 账号自诊（数据分析、报告生成） ⭐NEW
├── web/                # Web服务
│   ├── main.py         # FastAPI主应用
│   ├── routers/        # API路由
│   └── frontend/       # 前端资源
│       ├── templates/  # HTML模板
│       └── static/     # CSS/JS静态资源
├── desktop/            # 桌面客户端 ⭐NEW
│   ├── main_window.py  # 主窗口（内嵌WebView）
│   ├── pet_window.py   # 桌宠悬浮窗
├── config/             # 配置文件
├── logs/               # 日志文件
├── data/               # 数据库文件
├── reports/            # 报告导出目录 ⭐NEW
├── build.spec          # PyInstaller配置 ⭐NEW
├── build.py            # 打包脚本 ⭐NEW
├── start_web.py        # Web启动脚本
├── start_desktop.py    # 桌面应用启动脚本 ⭐NEW
└── test_stage3.py      # 阶段三测试脚本 ⭐NEW
```

## 🔒 安全特性

- **敏感信息加密存储**（Fernet对称加密）
- **严格限频保护**：令牌桶+429退避+熔断机制
- **Cookie池管理**：多账号轮换，自动有效性检查
- **WBI签名**：完整实现B站签名算法，自动更新密钥

## 💡 技术亮点

1. **评论去重算法**：SimHash快速粗筛 + Levenshtein精确匹配
2. **成本控制**：LLM批量处理（100条/批），词典规则优先
3. **限频保护**：评论专用限频器（4秒间隔），多端点独立限流
4. **松耦合设计**：无LLM配置仍可使用降级方案
5. **数据源退化策略**：zeroroku → B站公开页 → 本地估算，透明告知数据完整性 ⭐NEW
6. **桌面客户端架构**：主窗口+桌宠双进程，WebSocket实时通信 ⭐NEW
7. **更新机制**：SQLite数据库，覆盖更新不丢数据 ⭐NEW

## 📊 已完成进度

- ✅ 阶段一：工程骨架（~5700行）
- ✅ 阶段二：P0功能模块（~5020行）
  - 热点发现模块
  - 评论监控模块
  - WebUI基础框架
- ✅ 阶段三：P1功能与客户端（~3071行） ⭐NEW
  - 头部拆解模块
  - 账号自诊模块
  - PyQt桌面客户端
  - 打包与更新机制

**项目累计代码量：~13,791行**

## 📝 API文档

启动服务后访问：http://localhost:8000/docs

## ⚠️ 注意事项

1. **限频遵守**：工具内置严格限频，请勿绕过
2. **Cookie安全**：敏感信息加密存储，请妥善保管配置文件
3. **合规使用**：仅用于个人运营分析，不得用于恶意爬取
4. **桌面客户端依赖**：需要安装PyQt5和wkhtmltopdf（PDF导出） ⭐NEW
5. **数据持久化**：更新时data/和config/目录会保留，放心覆盖exe ⭐NEW

## 🔧 依赖安装

### 核心依赖（必装）
```bash
pip install -r requirements.txt
```

### 桌面客户端（可选）
```bash
pip install PyQt5 PyQtWebEngine websocket-client
```

### PDF导出（可选）
```bash
pip install markdown2 pdfkit
# 下载wkhtmltopdf: https://wkhtmltopdf.org/downloads.html
```

## 🧪 测试

运行阶段三功能测试：
```bash
python test_stage3.py
```

## 📦 打包为exe

```bash
python build.py
```

生成的exe位于`dist/FishTool.exe`，可直接分发。

## 📮 反馈与支持

遇到问题请查看日志：`logs/error.log`

---

**版本**：v1.0.0 (阶段三完成)  
**最后更新**：2025-01-XX
