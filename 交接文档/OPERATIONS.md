# 部署、配置与打包

## 1. 环境

建议 Windows 10/11、Python 3.11 或 3.12、独立虚拟环境。项目依赖见 `requirements.txt`，包括 aiohttp/FastAPI/SQLAlchemy、PySide6（含 WebEngine）、数据分析和 LLM 客户端。PySide6 不能与 PyQt5 装在同一个虚拟环境：PyInstaller 收集到两个 Qt 绑定会中止打包。

```powershell
cd D:\tasks\cola\bili_ops_toolbox
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

PDF 导出还依赖系统安装 `wkhtmltopdf`；没有它时 Markdown 导出仍可用。

## 2. 配置层

加载顺序：`config/config.yaml` → `config/user_config.yaml` 覆盖 → `config/.secrets` 解密后放到 `secrets`。

主要配置：

- `server.host/port/reload`：配置默认 127.0.0.1:8080，但 `app.py` CLI 默认 127.0.0.1:8000；命令参数优先影响实际启动。
- `database.path`：默认 `data/bili_ops.db`。
- `bilibili.rate_limit.normal/comment/dynamic`：默认 2/4/2.5 秒间隔。
- `bilibili.cookie_check_interval`：默认 1800 秒。
- `crawler.timeout/retry_times/concurrent_limit/enable_checkpoint`。
- `monitor.enable/check_interval/alert_keywords/alert_thresholds`。
- `llm.api_base/model/temperature/max_tokens/daily_token_limit`；API Key 应通过加密 secrets 保存。`daily_token_limit` 是所有 AI 功能共用的每日 token 上限，默认 200 万，0 表示不限制，可在配置页修改。`max_tokens` 是单次回复的最大输出 token，默认 2000，取值 1～100 万，可在配置页修改；开启思考模式的模型，思考过程也计入。
- `export.output_dir/formats`、`desktop_pet.websocket_port`。

不要手工编辑 `.secrets` 密文，也不要更换 `.key` 后继续使用旧 `.secrets` 或旧 Cookie 密文。

## 3. Web 部署

初始化：

```powershell
python app.py init
```

本机运行（仅 Web）：

```powershell
python app.py web --host 127.0.0.1 --port 8000
```

开发热重载：

```powershell
python app.py web --host 127.0.0.1 --port 8000 --reload
```

验证：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

应返回 `{"status":"ok"}`。浏览器访问 `/`，接口文档访问 `/docs`。默认只监听本机（`app.py web` 与桌面版内嵌服务均为 127.0.0.1）；确需局域网访问时显式传 `--host 0.0.0.0`，并用 `FISHTOOL_ALLOWED_HOSTS` 列出允许的 Host（逗号分隔，覆盖默认白名单，需同时保留 `localhost,127.0.0.1`），否则 `/api/config` 返回 403。

写请求与 `/ws` 的同源判定比较 scheme、主机和端口：默认放行 80/443/8000/8080，以及与服务自身相同的端口（例如 `start_web.bat 9000` 时的 9000）。如果页面由另一个本机端口提供（例如前端开发服务器 5173），用 `FISHTOOL_ALLOWED_PORTS=5173,8000` 显式列出（逗号分隔，整体覆盖默认端口集）。设置了 `FISHTOOL_ALLOWED_PORTS` 后，`FISHTOOL_ALLOWED_HOSTS` 里写的 `主机:端口` 不再合并进端口集；值全部非法时回退默认端口集。与服务自身相同的端口始终放行。对局域网开放前必须增加认证、反向代理和访问控制，当前业务接口没有完整用户鉴权。

## 4. 桌面运行

```powershell
python app.py
```

桌面版会同时启动主窗口、桌宠和本地 Web 服务，浏览器访问 `http://localhost:8000`
也可以使用同一套界面/API。桌面端没有首次启动向导：扫码登录在 WebUI「欢迎」页的「B站登录态」区域，LLM 在「配置」页的「AI大模型配置」区域保存。exe/冻结环境下，ConfigManager 把持久化根目录设为 `sys.executable` 所在目录，因此分发时必须让 exe 旁的 `config/`、`data/` 可写。

## 5. PyInstaller 打包

当前唯一实际打包描述是 `build.spec`（Analysis 入口为 `app.py`）；项目根目录没有 `build.py`，README 快速开始第 3 步与下面的命令一致。

在已执行 `pip install -r requirements.txt` 的虚拟环境中安装构建工具并打包：

```powershell
pip install pyinstaller
pyinstaller --clean --noconfirm build.spec
```

PyInstaller 不在 `requirements.txt` 中，需要单独安装。图标是 PNG，PyInstaller 要靠 Pillow 转成 ico；Pillow 已由 `requirements.txt` 中的 `qrcode[pil]` 带入，缺失时打包会在图标处理阶段报错。

输出：`dist/FishTool.exe`。spec 打包 `config/*.yaml`、前端 templates/static，并声明 PySide6、FastAPI、SQLAlchemy、数据分析等 hidden imports；WebEngine 调试资源（*.debug.pak）不打进包，不用 UPX。

构建前检查：

- `build.spec` 使用仓库内图标 `assets/图标.png`（整个 `assets/` 也打进 datas，供运行时 `desktop/icon_utils.py` 查找窗口图标）；该文件缺失时 exe 回退为无自定义图标。
- `.key/.secrets` 不在 datas 中，这是正确的安全默认；不要把个人密钥打进通用 exe。
- spec 的 Analysis 只列出 `app.py`，发布前必须实际启动验证桌面入口与 `app.py web` 两种模式。
- `build/` 是中间产物，`dist/` 是发布候选；不要以旧 `backups/*.exe` 作为最新版。

打包验收：

1. 在空白临时目录放 exe。
2. 启动并确认自动创建/使用 exe 旁的 `config/`、`data/`。
3. 检查 WebUI、静态 ECharts、扫码登录、SQLite 写入、退出后无残留后台进程。
4. 查看 PyInstaller 生成的 `build/build/warn-build.txt`，确认没有新增的缺失模块。`tools/run_packaging_test.ps1` 依赖仓库中不存在的 `run_packaging.py`，当前不可用。

## 6. 发布与升级

升级时保留用户的 `data/` 和 `config/`，只替换 exe/源码和静态资源。发布前先备份数据库与密钥组。若 schema 改变，先运行版本化迁移，不能依赖 `create_all()`。

日志主位置是 `data/logs/`；根目录 `logs/` 含较多历史验证日志，不应作为唯一生产日志来源。
