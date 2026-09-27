# bili_ops_toolbox 收尾清理报告

日期：2026-08-21
项目：`D:\\tasks\\cola\\bili_ops_toolbox`

## 1. 清理结果

已删除并确认无项目生产引用：

- `modules/comment/collector.py.tmp`（19.6KB）
- `modules/comment/collector.py.fixtmp`（19.6KB）
- `modules/comment/test_overwrite.tmp`（5B）
- `modules/self_diagnosis/self_analyzer.py.tmp`（15.1KB）
- `modules/self_diagnosis/test_write.tmp`（5B）
- `modules/lottery/service.clean.py`（27.9KB，重复实现）
- `desktop/probe6.txt`（2B，占位文件）
- 项目各级 `__pycache__` 缓存目录（共 109 个缓存文件，约 1.1MB）
- `backups/BiliOpsToolbox_0920_094942.exe`（199,889,881 字节，工具显示 190.6MB）

保留：

- `tools/archive/dead_monitor_batch.py`
- `tools/archive/dead_collect_user_videos_comments.py`

这两个文件属于既定历史归档，不在生产导入和打包路径中。全项目搜索显示 `monitor_batch`、`BatchMonitorRequest`、`collect_user_videos_comments` 的命中仅位于归档文件、历史报告和注释；生产路由/业务实现无定义或调用残留。

## 2. B站接口维护边界

业务模块当前通过 `BilibiliAPI.get()` 发起请求，没有发现业务代码直接创建 B站 HTTP 会话。扫码轮询此前因需要保留 `Set-Cookie` 响应头存在两处特殊直连，已统一收敛为 `BilibiliAPI.poll_qrcode()`。

后续 B站接口变更优先检查：

| 接口 | 维护位置 |
|---|---|
| 通用 GET/POST、Cookie、重试、限频、WBI 签名 | `bilibili/api.py`：`BilibiliAPI.request/get/post` |
| WBI 密钥、指纹接口 | `bilibili/api.py`：`WBISigner.update_wbi_keys`、`BilibiliAPI.init_session` |
| 用户资料 | `bilibili/api.py`：`BilibiliAPI.get_user_info` |
| 粉丝/关注统计 | `bilibili/api.py`：`BilibiliAPI.get_user_relation_stat` |
| 累计播放统计 | `bilibili/api.py`：`BilibiliAPI.get_user_upstat` |
| UP 主投稿 | `bilibili/api.py`：`BilibiliAPI.get_user_videos` |
| 分区排行榜 | `bilibili/api.py`：`BilibiliAPI.get_ranking`；词云分页调用在 `modules/hotspot/tag_cloud.py::TagCloudGenerator.get_zone_ranking` |
| 评论主楼 | `modules/comment/collector.py::CommentCollector` 的 `self.api.get` 调用；接口路径集中在该模块业务方法参数中 |
| 活动列表/动态 | `modules/hotspot/activity_tracker.py::ActivityTracker` 的 `self.api.get` 调用 |
| 登录二维码生成、登录态 nav 校验 | `bilibili/auth.py`：`QRCodeLogin.generate_qrcode`、`CookieLoginHelper.validate_cookie`、`CookieLoginHelper.get_user_info` |
| 扫码轮询及 Set-Cookie 提取 | `bilibili/api.py::BilibiliAPI.poll_qrcode`；调用方为 `bilibili/auth.py::QRCodeLogin.poll_login_status` 和 `web/routers/auth.py::poll_login` |
| Cookie 池登录态验证 | `bilibili/cookie_pool.py`：通过 `BilibiliAPI.get` 调用 nav |

`modules/up_analyzer/data_fetcher.py::fetch_from_zeroroku` 访问的是第三方 `zeroroku`，不属于 B站接口。

## 3. EXE验证证据

删除前验证正式版：

- 文件：`dist/BiliOpsToolbox.exe`
- SHA-256：`fbe1d521d7ce1a3bf7b440d3be437d1c212b69ce71aede5122c1f64984b0ef6b`
- 大小：`211,581,496` 字节
- 启动结果：出现 `BiliOpsToolbox.exe` 进程
- 端口结果：`127.0.0.1:8000` 正在监听
- 健康检查：`GET /health` 返回 `200 {"status":"ok"}`
- 回归测试：`10 passed, 1 warning`

旧版 EXE 删除前 SHA-256：`69f8b4dd96b77f77386650bc8689e2c1aa938e12464aa85300368fe6e32f8f9b`。

删除后 `dist/backups` 范围内仅剩正式版 `dist/BiliOpsToolbox.exe`。
