# FishTool → AstrBot 插件适配

开发者说明（不进发布包）。设计依据见《FishTool_AstrBot适配执行案_20261102.md》。

## 架构

```text
AstrBot 面板 → 插件视图 iframe（views/console，原前端 + bridge-shim.js）
   └─ window.AstrBotPluginView.apiGet/apiPost/subscribeSSE/download
        → /api/v1/plugins/extensions/astrbot_plugin_fishtool/<endpoint>（面板 JWT 鉴权）
           → main.py（AstrBot 进程内，只做进程管理 + 代理 + 事件中继）
              → 127.0.0.1:<随机端口> FishTool 引擎子进程（独立 venv，原样代码）
```

- 原业务代码（`core/ modules/ web/ bilibili/ llm/ app.py`）一行不改；适配代码只在本目录。
- 引擎依赖与 AstrBot 冲突（fastapi/pydantic 等锁定旧版本），所以引擎跑在独立 venv：
  `data/fishtool_runtime/venv`，按 `engine_requirements.txt` 哈希自动创建/重建。
- 数据全部在 `data/plugin_data/astrbot_plugin_fishtool/`：`config/`（.key .secrets config.yaml budget.yaml）、
  `runtime/`（数据库、日志、报告）。插件更新会整目录替换插件代码，数据不受影响。

## 目录

| 路径 | 内容 |
|---|---|
| `plugin/` | 插件源码，发布时原样复制到发布分支根目录 |
| `plugin/main.py` | Star 入口：生命周期、注册 Web API |
| `plugin/fishtool_adapter/` | paths / runtime_env / supervisor / client / proxy / events / exports |
| `plugin/engine_launcher/launcher.py` | 引擎子进程入口：路径重定向 → uvicorn |
| `frontend_shim/` | `bridge-shim.js`（fetch/WebSocket/Storage 适配）、`astrbot-overrides.js`（导出/复制/外链）、`shim.css` |
| `release_tools/` | `build_plugin.py`、`transform_views.py`、`verify_package.py`、`publish_branch.py`、`engine_manifest.txt` |
| `harness/` | `astrbot_harness.py`（本机 AstrBot 联调）、`smoke_views.py`（Playwright 视图冒烟） |
| `tests/` | 适配层单测（主线 `pytest` 不收集，CI 单独运行） |

> 构建脚本目录叫 `release_tools/` 而不是执行案里的 `build/`：仓库 `.gitignore` 忽略所有 `build/` 目录。

## 本地开发

```powershell
# 1. 引擎依赖环境（Python 3.11/3.12）
uv venv -p 3.12 .venv-engine
python integrations/astrbot/release_tools/build_plugin.py --out dist/plugin
uv pip install -p .venv-engine -r dist/plugin/engine_requirements.txt pytest psutil

# 2. 单测
.venv-engine\Scripts\python -m pytest integrations/astrbot/tests -q
python tools/check_exception_handling.py

# 3. 构建 + 校验
python integrations/astrbot/release_tools/build_plugin.py --out dist/plugin --version 0.1.0
python integrations/astrbot/release_tools/verify_package.py dist/plugin --max-zip-mb 16

# 4. 本机 AstrBot 联调（需要另一个装了 astrbot==4.29.4 的 Python 3.12 环境）
<astrbot-python> integrations/astrbot/harness/astrbot_harness.py --astrbot-python <astrbot-python> `
    --root <临时目录> --plugin dist/plugin --port 6195 --keep-running
<astrbot-python> integrations/astrbot/harness/smoke_views.py --base http://127.0.0.1:6195 --password FishTool-Harness-2026
```

修改引擎代码后在插件视图之外调用 `_engine/restart`（或面板“重载插件”）即可，不必重启 AstrBot。

## 发布

推送 tag `astrbot-v<版本>`（如 `astrbot-v0.1.1`）后，CI 依次执行：测试 → `build_plugin.py` → `verify_package.py` → 发布到两个位置。两处都只追加提交，不强推：

| 位置 | 用途 |
|---|---|
| 本仓库孤儿分支 `astrbot` | `https://github.com/FORK-is-coding/FishTool/tree/astrbot` 从仓库安装 |
| 独立插件仓库 `FORK-is-coding/astrbot_plugin_FishTool` 的默认分支 `main` | 插件市场上架源，也可直接从仓库安装 |

为什么要独立仓库：AstrBot Cloud 发布页只解析仓库默认分支根目录的 `metadata.yaml`（发布页按 `raw.githubusercontent.com/<repo>/<default_branch>/metadata.yaml` 探测，解析请求不带分支参数）。FishTool 的默认分支是 `main`，根目录是 FishTool 本体，无法直接上架。

独立仓库的推送令牌放在 FishTool 仓库的 Secret `PLUGIN_REPO_TOKEN`：fine-grained token，只授权 `astrbot_plugin_FishTool` 一个仓库，权限 Contents: Read and write。令牌只通过一次性 HTTP 认证头使用，不写进 remote 地址或日志。

插件包根目录带 FishTool 仓库的 `LICENSE`（AGPL-3.0），由 `build_plugin.py` 复制、`verify_package.py` 校验。发布是整树替换，包里没有 LICENSE 会删掉独立仓库现有的许可证文件。

两个安装源使用同一个数据目录（`data/plugin_data/astrbot_plugin_fishtool/`）。AstrBot 更新插件时用安装时记录的源，所以从 `/tree/astrbot` 安装的用户会继续从该分支更新。

插件市场：`https://cloud.astrbot.app/publish` → 授权 GitHub App 访问 `astrbot_plugin_FishTool` → 选择该仓库解析。上架前需人工验收（B 站扫码、LLM Key、评论监控、自诊报告下载）。

## 插件 Web API

均挂在 `/api/v1/plugins/extensions/astrbot_plugin_fishtool/` 下，所有响应 HTTP 200（插件从不返回 401，否则面板会登出）。

| endpoint | 方法 | 说明 |
|---|---|---|
| `_status` | GET | 引擎状态、进度、最近错误、版本 |
| `_engine/start` `stop` `restart` | POST | 引擎启停 |
| `_engine/clean-venv` | POST | 停止引擎并删除 venv（下次启动重建） |
| `_engine/log` | GET | 引擎启动日志尾部 |
| `_events` | GET (SSE) | 引擎 `/ws` 事件（toast / alert） |
| `_export/report` `_export/text` | POST | 保存导出内容，返回 `file_id` |
| `_export/file?id=` | GET | 下载导出文件 |
| `api/<path>` | GET/POST/PUT/PATCH/DELETE | 引擎 API 代理；响应信封 `{"status":"ok","data":{"http_status","body"}}`；POST 体含 `__fishtool_tunnel:1` 时按隧道还原方法与 query |

Open API Key（默认 scope 含 `plugin`）与面板登录同等可用；插件配置 `engine.block_api_key_write` 可禁止 API Key 调写接口与凭据接口。

## 已知限制（一期）

- 自诊报告导出为 HTML 文件下载，用浏览器打开后“打印 → 另存为 PDF”（沙箱禁止 `print()`）。
- 外部链接（GitHub、B 站主页等）在视图内弹窗显示，需复制到浏览器打开（沙箱禁止弹窗）。
- 卸载时勾选“删除数据”不会删除 `data/fishtool_runtime/`（venv），需手动删除或先调用 `_engine/clean-venv`。
- 机器人遥控（执行案第 9 章）为二期。
