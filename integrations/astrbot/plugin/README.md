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

## 已知限制

- 自诊报告导出为 HTML 文件，用浏览器打开后选择“打印 → 另存为 PDF”。
- 视图内无法打开新窗口，外部链接会以弹窗显示，请复制到浏览器打开。
- 若在 AstrBot 前配置了反向代理（如 nginx），请调高代理读超时，避免评论分析、账号自诊等长请求被提前断开。

## 反馈

- GitHub Issues：https://github.com/FORK-is-coding/FishTool/issues
- 邮箱：3575029912@qq.com
