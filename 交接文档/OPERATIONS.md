# 部署、配置与打包

## 1. 环境

建议 Windows 10/11、Python 3.11 或 3.12、独立虚拟环境。项目依赖见 `requirements.txt`，包括 aiohttp/FastAPI/SQLAlchemy、PyQt5/PyQtWebEngine、数据分析和 LLM 客户端。

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

- `server.host/port/reload`：配置默认 127.0.0.1:8080，但 `start_web.py` CLI 默认 0.0.0.0:8000；命令参数优先影响实际启动。
- `database.path`：默认 `data/bili_ops.db`。
- `bilibili.rate_limit.normal/comment/dynamic`：默认 2/4/2.5 秒间隔。
- `bilibili.cookie_check_interval`：默认 1800 秒。
- `crawler.timeout/retry_times/concurrent_limit/enable_checkpoint`。
- `monitor.enable/check_interval/alert_keywords/alert_thresholds`。
- `llm.api_base/model/temperature/max_tokens/daily_token_limit`；API Key 应通过加密 secrets 保存。
- `export.output_dir/formats`、`desktop_pet.websocket_port`。

不要手工编辑 `.secrets` 密文，也不要更换 `.key` 后继续使用旧 `.secrets` 或旧 Cookie 密文。

## 3. Web 部署

初始化：

```powershell
python start_web.py --init-only
```

本机运行：

```powershell
python start_web.py --host 127.0.0.1 --port 8000
```

开发热重载：

```powershell
python start_web.py --host 127.0.0.1 --port 8000 --reload
```

验证：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

应返回 `{"status":"ok"}`。浏览器访问 `/`，接口文档访问 `/docs`。默认建议只监听本机；对局域网开放前必须增加认证、反向代理和访问控制，当前业务接口没有完整用户鉴权。

## 4. 桌面运行

```powershell
python start_desktop.py
```

首次启动向导可扫码登录并配置 LLM。exe/冻结环境下，ConfigManager 把持久化根目录设为 `sys.executable` 所在目录，因此分发时必须让 exe 旁的 `config/`、`data/` 可写。

## 5. PyInstaller 打包

当前唯一实际打包描述是 `build.spec`；README 中的 `python build.py` 已过时，因为项目根目录没有 `build.py`。

安装构建工具并打包：

```powershell
pip install pyinstaller
pyinstaller --clean --noconfirm build.spec
```

输出：`dist/FishTool.exe`。spec 打包 `config/*.yaml`、前端 templates/static，并声明 PyQt、FastAPI、SQLAlchemy、数据分析等 hidden imports。

构建前检查：

- `build.spec` 使用桌面图标 `C:\Users\27418\Desktop\图标.png`，当前文件已确认存在；打包环境若缺失该文件则回退为无自定义图标。
- `.key/.secrets` 不在 datas 中，这是正确的安全默认；不要把个人密钥打进通用 exe。
- spec 的 Analysis 同时列出 `start_desktop.py` 和 `start_web.py`，发布前必须实际启动验证主入口行为。
- `build/` 是中间产物，`dist/` 是发布候选；不要以旧 `backups/*.exe` 作为最新版。

打包验收：

1. 在空白临时目录放 exe。
2. 启动并确认自动创建/使用 exe 旁的 `config/`、`data/`。
3. 检查 WebUI、静态 ECharts、扫码登录、SQLite 写入、退出后无残留后台进程。
4. 用 `tools/run_packaging_test.ps1` 做基础检查，并查看 `dist/PACKAGE_BUILD_LOG*.txt`。

## 6. 发布与升级

升级时保留用户的 `data/` 和 `config/`，只替换 exe/源码和静态资源。发布前先备份数据库与密钥组。若 schema 改变，先运行版本化迁移，不能依赖 `create_all()`。

日志主位置是 `data/logs/`；根目录 `logs/` 含较多历史验证日志，不应作为唯一生产日志来源。
