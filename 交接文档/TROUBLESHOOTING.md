# 常见问题排查

按“健康检查 → 进程/端口 → 配置 → 日志 → Cookie → 接口 → 数据库”顺序排查，避免一上来删库或重登录。

## 1. WebUI 无法打开

1. 运行 `python start_web.py --host 127.0.0.1 --port 8000`。
2. 请求 `http://127.0.0.1:8000/health`；失败则看启动终端和 `data/logs/error.log`。
3. 检查 8000 是否被占用；改端口时同步 CORS 白名单，否则前后端跨域可能失败。
4. `/health` 正常但页面空白：检查 `/static/js/app.js`、模板目录和浏览器控制台；exe 中确认 spec 已包含 frontend。

## 2. 配置修改不生效

加载优先级是 config.yaml → user_config.yaml → secrets。检查是否被 `user_config.yaml` 覆盖，并通过 `/api/config/reload` 或重启加载。

启动端口以 `start_web.py --port` 为准，不一定采用 config.yaml 的 `server.port`。exe 环境配置在 exe 旁，不是 PyInstaller 临时目录。

## 3. 登录态无效或频繁掉线

- `/api/auth/status` 验证；Cookie 校验走 `/x/web-interface/nav`。
- `-101` 是未登录/Cookie 失效，不应盲目重试；重新扫码。
- 确认 `config/.key` 与 `.secrets` 成套，数据库 Cookie 密文也依赖密钥。
- 不要在日志/群消息中贴完整 Cookie。CookiePool 失效统计看 `cookie_pool.is_valid/fail_count`。

## 4. HTTP 412、429 或业务风控码

- HTTP 412、`-352/-412`：IP/UA 风控，短重试无效；停止任务、延长间隔、确认登录态和网络环境。
- HTTP 429、`-509`：等待限流器退避，不要手工循环快速重试。
- `-799`：空间接口频率限制，代码已有 6/12/18 秒级退避；持续出现时降低调用频率。
- 动态 `4101129`：首屏不要传 `offset=0`。
- 查看 `data/logs/risk_control.log`；先降速，不要绕过限制。

## 5. WBI 签名失败

1. 测试匿名 nav 是否返回 `data.wbi_img.img_url/sub_url`。
2. 检查系统时间是否准确，`wts` 使用当前 Unix 时间。
3. 确认参数签名前没有被后续代码修改，特殊字符过滤和排序仍在。
4. 清除内存密钥只能通过重启/刷新，不要硬编码固定 img/sub key。
5. 若 B站算法变更，只改 `WBISigner`，业务模块仍调用 `need_sign=True`。

## 6. 评论重复、少数据或翻页不动

- 评论接口使用 `oid=aid`；先确认 `/x/web-interface/view?bvid=...` 能得到 aid。
- `/x/v2/reply/main` 普通评论用 cursor + `pagination_str`，不是 `pn`。
- 重复 rpid 会触发停止翻页，这是防止接口忽略游标导致虚高，不应删除该保护。
- `comments.rpid` 是唯一键；数据库重复插入应按幂等处理。
- 热门 `mode=3` 与普通 `mode=2` 会重叠，合并时必须按 rpid 去重。

## 7. 热点/标签为空

- ranking/v2 的 `type` 使用 `all|origin`，不要用旧整数。
- 榜单和 view 当前不提供可靠 tag；逐视频调用 `/x/tag/archive/tags`。
- 先用小 limit 验证，逐视频标签请求受 normal 限流，批量耗时是正常现象。

## 8. 活动或动态为空

- 官方活动必须带 `plat=1`。
- 动态首屏只传 `host_mid`；有下一页 offset 才传 offset。
- 动态无登录态可能 412；检查 Cookie 池。
- 业务模块还会按活动关键词过滤，原始动态存在不代表统一活动列表一定有结果。

## 9. UP 分析数据不完整

`UPDataFetcher` 设计为多源降级。检查返回的数据源/完整度标记；zeroroku 端点在源码中被标为假设格式，第三方失败时应退化到 B站公开数据和本地估算。

完播率、观众画像、流量来源等创作中心私有数据无法从当前公开接口可靠取得，不能用估算值伪装成真实值。

## 10. SQLite 错误或数据丢失

- 先停所有 Web/桌面进程，再运行 `PRAGMA integrity_check;`。
- 确认实际数据库路径：源码默认项目 `data/`，exe 默认 exe 旁 `data/`。
- `database is locked`：检查是否运行了多个进程或有外部 SQLite 工具持有写锁。
- ORM 加列后旧库没有新列：`create_all()` 不迁移，按 `DATABASE.md` 做显式迁移。
- 恢复 Cookie 数据必须同时恢复 `.key/.secrets`，不要只换数据库。

## 11. exe 打包后启动失败

- 当前应执行 `pyinstaller --clean --noconfirm build.spec`，不是 README 中不存在的 `build.py`。
- 查 `build/build/warn-build.txt`、`dist/PACKAGE_BUILD_LOG*.txt` 和 exe 旁 `data/logs/error.log`。
- 检查 frontend 静态资源、PyQtWebEngine hidden import、可写 config/data 目录。
- PDF 单独失败时检查系统 `wkhtmltopdf`，不是重装整个程序。

## 12. 测试与归档

当前有效脚本优先使用 `tools/test_logs_api.py`、`tools/test_web.ps1`、`tools/run_packaging_test.ps1`。`tools/archive/` 是历史验证资料；运行前必须读源码确认端口、数据写入和依赖，`dead_*.py` 禁止作为生产测试。

故障修复后至少记录：触发条件、接口/错误码、改动文件、数据库是否迁移、验证命令、日志位置和回滚方法。
