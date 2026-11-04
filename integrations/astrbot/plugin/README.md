# FishTool B站运营工具箱（AstrBot 插件）

[FishTool](https://github.com/FORK-is-coding/FishTool) 的 AstrBot 插件版，可以把你的bot变成真正具备生产力的虚拟秘书，让你一人拥有企业级运营工作流，成为你的虚拟运营主管，为你的视频创作而工作。

FishTool是一个为B站UP主打造的智能运营Agent，可自由配置你喜欢的AI大模型作为你的助理，目前覆盖【选题参考 → 热点发现 → 舆情监控 → 竞品调研 → 账号自诊 → 抽奖真人筛选】的完整新媒体运营链路。

FishTool的未来更新目标是成为开源、免费、共创、普惠、为所有视频创作者提供专业运营方法论与工具的、能够覆盖新媒体-社区-用户-发行-营销等多方面业务场景的专业运营工作台。

欢迎提 Issue 和 PR，如您具备相关领域工作经验，也欢迎提出想法与思路，让本项目能够更好地服务于个人创作者的实际需求。

>本仓库为Astrbot插件版，issue与PR请前往https://github.com/FORK-is-coding/FishTool 。
## 安装

推荐在 AstrBot 插件市场搜索 FishTool 安装。也可以在 AstrBot 面板 → 插件 → 从仓库安装，填写：

```text
https://github.com/FORK-is-coding/astrbot_plugin_FishTool
```

要求 AstrBot ≥ 4.29。

## 首次启动

- 插件启用后会自动创建引擎运行环境（独立 Python venv，位于 `data/fishtool_runtime/`）并安装依赖，**需要联网 pip，约需数分钟**。可在插件配置 `engine.pip_index_url` 填 pip 镜像加速。
- 引擎需要 **Python 3.11 或 3.12**。AstrBot 默认解释器是 3.12 时无需配置；否则（或使用 AstrBot 桌面客户端、内置 Python 无法创建 venv 时）请在插件配置 `engine.python` 填写一个 3.11/3.12 解释器的完整路径。
- 准备完成后，打开 AstrBot 面板 → 插件 → FishTool → 视图「FishTool 控制台」使用。

## 数据

| 位置 | 内容 |
|---|---|
| `data/plugin_data/astrbot_plugin_fishtool/config/` | 配置、加密密钥 `.key`、加密凭据 `.secrets`、配额账本 |
| `data/plugin_data/astrbot_plugin_fishtool/runtime/` | 数据库、日志、报告 |
| `data/fishtool_runtime/` | 引擎 venv（可随时删除，下次启动自动重建） |

- 插件更新不会影响数据。卸载时勾选“删除数据”会删除 `plugin_data` 下的数据，但不会删除 `data/fishtool_runtime/`，请手动删除。
- 插件数据与桌面版 / Web 版 FishTool 互不共享：需要在插件里重新扫码登录 B 站、重新填写 LLM Key。
- 引擎只监听 `127.0.0.1` 随机端口，外部无法直接访问；所有操作经 AstrBot 面板登录鉴权。

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `engine.autostart` | `true` | 插件启用时自动启动引擎 |
| `engine.python` | 空 | 引擎解释器路径（3.11/3.12），空 = AstrBot 当前解释器 |
| `engine.pip_index_url` | 空 | pip 镜像地址 |
| `engine.port` | `0` | 引擎端口，0 = 自动分配 |
| `engine.request_timeout_s` | `120` | 普通请求超时 |
| `engine.long_request_timeout_s` | `1800` | 评论分析、账号自诊等长请求超时 |
| `engine.block_api_key_write` | `false` | 禁止 AstrBot Open API Key 调用写接口与凭据接口 |
| `chat.enable` | `true` | 启用聊天指令与告警推送 |
| `chat.restricted_scope` | `admin` | `告警`、`监控 状态` 谁能用：`admin` 仅管理员 / `subscribed` 管理员 + 已订阅推送的会话 / `everyone` 所有人 |
| `chat.cooldown_seconds` | `10` | 公开指令在同一会话的最短间隔，0 = 不限制 |
| `chat.notify_min_level` | `high` | 推送的最低告警级别 |
| `chat.notify_merge_seconds` | `120` | 告警合并窗口 |
| `chat.report_font_cjk` | 空 | 自诊 PDF 的中文字体路径（.ttf/.ttc），见下文 |
| `chat.report_font_latin` | 空 | 自诊 PDF 的数字与英文字体路径（可选） |
| `chat.report_charts` | `true` | 自诊 PDF 包含矢量图表 |

## 聊天指令

在任意已接入 AstrBot 的会话里发送（默认唤醒前缀 `/`；个人 QQ 经 OneBot v11 接入时已测试，其他平台未测）：

| 指令 | 谁能用 | 作用 |
|---|---|---|
| `/fishtool 状态` | 所有人 | 引擎状态、B站登录态、常驻监控概况、自诊报告字体 |
| `/fishtool 热点 [数量]` | 所有人 | 最近一轮热点发现的热搜词和视频（1～30，默认 10） |
| `/fishtool 告警 [数量] [级别]` | 见 `chat.restricted_scope` | 最近未读的评论舆情告警（1～20，级别 all/high/medium/low） |
| `/fishtool 监控 状态` | 见 `chat.restricted_scope` | 常驻评论监控状态 |
| `/fishtool 舆情 <BV号> [normal\|full]` | 管理员 | 后台跑一次评论舆情分析，完成后把摘要发回本会话 |
| `/fishtool 监控 开启 [BV号,BV号]` / `暂停` / `停止` | 管理员 | 管理常驻评论监控 |
| `/fishtool 订阅` / `/fishtool 退订` | 管理员 | 本会话接收 / 不再接收评论舆情告警推送 |
| `/fishtool 自诊 [UID]` | 管理员 | 生成账号自诊报告并以 PDF 文件发到本会话；不填 UID 用已登录的 B 站账号 |

`/ft` 是 `/fishtool` 的别名；只发 `/fishtool` 会列出全部子指令。

- **管理员**指 AstrBot 配置里的 `admins_id`（用 `/sid` 查看自己的 ID）。舆情、监控开关、订阅、自诊会触发 LLM 调用、用你的 Cookie 抓取 B 站或把告警推到某个会话，始终只限管理员，没有开关。
- **冷却**：公开指令在同一会话内默认 10 秒一次（`chat.cooldown_seconds`），冷却期内不回复；管理员不受限。“权限不足”的提示也按同一窗口限频：同一个人在同一会话里反复调用管理员指令，窗口内只提示一次。
- **告警推送**：`/fishtool 订阅` 后，引擎检测到的告警按 `chat.notify_merge_seconds`（默认 120 秒）合并成一条消息发送，多条时为合并转发，降低个人 QQ 风控风险。订阅保存在 `data/plugin_data/astrbot_plugin_fishtool/remote/subscriptions.json`。引擎重启或断线期间的告警会在重连后补发；插件重载前后可能有少量告警重复推送。在 AstrBot 里给平台适配器改名后，旧订阅会失效，需要重新订阅。
- 关闭 `chat.enable` 后所有子指令不再回复、告警推送停止；只发 `/fishtool` 时 AstrBot 自动生成的帮助仍会显示（框架行为）。

### 自诊报告字体

PDF 用 reportlab 生成，只能嵌入 **TrueType 轮廓**的字体（`.ttf` / `.ttc`）。Noto Sans CJK、思源黑体的 `.otf` 是 CFF 轮廓，用不了——AstrBot 官方 Docker 镜像自带的正是这种，所以 **Docker 用户需要自己挂一个字体**：

```bash
# 例：把一个 TrueType 中文字体（文泉驿微米黑、更纱黑体 TTF 等）放进 /path/fonts
docker run ... -v /path/fonts:/AstrBot/data/fonts:ro ...
```

然后在插件配置 `chat.report_font_cjk` 填容器内路径，例如 `/AstrBot/data/fonts/wqy-microhei.ttc`。`/fishtool 状态` 会显示当前用的字体；找不到可用字体时 PDF 退回宋体（不嵌入，部分阅读器显示方块），摘要里会提示。Windows 会自动使用微软雅黑与 Bahnschrift。

### 收不到文件

PDF 存放在 `data/plugin_data/astrbot_plugin_fishtool/exports/`（24 小时后清理），以文件消息发给协议端（NapCat 等）：

- **AstrBot 与协议端在同一台机器 / 同一容器**：直接按本地路径发送，无需配置。
- **分开部署（常见的两个 Docker 容器）**：协议端读不到 AstrBot 容器里的路径。二选一：
  - 在 AstrBot 配置 `callback_api_base`，填协议端能访问到的 AstrBot 地址（如 `http://astrbot:6185`）。此时协议端回来下载文件，链接 **300 秒内有效、只能下载一次**（AstrBot 文件服务的默认值），协议端下载失败重试会拿到 404；
  - 或把 AstrBot 的 `data` 目录以**相同路径**挂载进协议端容器。
- 协议端先返回成功、再在后台下载失败时插件感知不到，所以摘要消息末尾固定附了一句提示。

## 已知限制

- 视图内导出的自诊报告是 HTML 文件，用浏览器打开后选择“打印 → 另存为 PDF”；聊天里的 `/fishtool 自诊` 直接发 PDF。
- 视图内无法打开新窗口，外部链接会以弹窗显示，请复制到浏览器打开。
- 若在 AstrBot 前配置了反向代理（如 nginx），请调高代理读超时，避免评论分析、账号自诊等长请求被提前断开。

## 反馈

- GitHub Issues：https://github.com/FORK-is-coding/FishTool/issues
- 邮箱：3575029912@qq.com
