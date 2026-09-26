# B站接口维护手册

本文只描述当前源码实际使用的 B 站接口。B 站接口可能变更，字段说明以运行时响应为准；变更时先在 `bilibili/api.py` 或对应业务模块做最小适配，再同步本文。

## 1. 统一请求契约

`BilibiliAPI.request()` 位于 `bilibili/api.py:429` 附近，统一处理：

- `method/url/params/data/json/headers/need_sign/retry_times`。
- 请求前按需从 `CookiePool` 取有效 Cookie，再经 `RateLimiter` 获取令牌。
- 成功要求外层 `code == 0`，返回 `result.data`；业务便捷方法再包装为 `{'data': data}`。
- `-101` Cookie 失效；`-352/-412` 风控；`-509` 访问过快；`-799` 空间接口频率受限。
- HTTP 429 按 `Retry-After` 或限流器策略退避；5xx、网络错误、超时按可重试规则处理；HTTP 412 直接视为反爬拦截。
- 默认请求头在 `BilibiliAPI.__init__`：User-Agent、Referer、Origin。不要在接口函数里另造一套请求会话。

## 2. WBI 相关接口

### `GET /x/web-interface/nav`

用途：获取登录态信息和 `data.wbi_img.img_url/sub_url`，从文件名提取 `img_key/sub_key`。

入参：通常无；带 Cookie 时返回用户登录信息，不带 Cookie 也可能返回 WBI 图片字段。

出参重点：`code/message/data.isLogin/data.mid/data.uname/data.wbi_img.img_url/data.wbi_img.sub_url`。

签名：该接口自身不需要 WBI。`WBISigner.update_wbi_keys()` 与 `auth.validate_cookie()`、Cookie 巡检都会调用。

同步点：`bilibili/api.py:181-240`、`bilibili/auth.py`、`bilibili/cookie_pool.py`。若字段路径变化，三处都要改，并验证匿名 nav 仍可刷新密钥。

### `GET /x/frontend/finger/spi`

用途：初始化/获取浏览器指纹辅助信息，属于 `BilibiliAPI.init_session()` 的兼容逻辑。

入参：无。出参只用于会话初始化，不进入业务数据库。

同步点：`bilibili/api.py` 会话初始化段。失败通常不应掩盖主请求错误，修改时保持会话可关闭。

### WBI 签名算法

适用当前代码中的 `/x/space/wbi/acc/info`、`/x/space/wbi/arc/search`、评论主接口和热点榜单调用。

1. `nav` 取 `img_key + sub_key`。
2. 使用 `WBISigner.MIXIN_KEY_ENC_TAB` 重排，截取前 32 位 `mixin_key`。
3. 参数加 `wts`，按 key 排序，过滤 `!'()*`，`urlencode`。
4. `MD5(query + mixin_key)` 生成 `w_rid`。

签名入口是 `WBISigner.sign_params()`；不要在业务模块手写 `w_rid`。密钥默认约 1 小时刷新。

## 3. 用户和 UP 主

### `GET /x/space/wbi/acc/info`

代码入口：`BilibiliAPI.get_user_info(uid)`。

入参：`mid`，整数 UID；WBI 签名。

出参重点：`mid/name/face/sign/level/birthday/official` 等用户资料。粉丝/关注数不保证在此返回，使用 relation 接口。

### `GET /x/relation/stat`

代码入口：`get_user_relation_stat(uid)`。

入参：`vmid`，整数 UID；不需要 WBI。

出参重点：`follower/following`。

### `GET /x/space/upstat`

代码入口：`get_user_upstat(uid)`。

入参：`mid`；不需要 WBI。

出参重点：`data.archive.view` 为累计投稿播放量。不要恢复旧的顶层播放字段读取。

### `GET /x/space/wbi/arc/search`

代码入口：`get_user_videos(uid, page=1, page_size=30)`。

入参：`mid`、`pn`、`ps`、`order=pubdate`、`index=1`；WBI 签名。

出参重点：`list.vlist[]` 中的 `aid/bvid/title/play/pic/description/created`，以及 `page.pn/page.ps/page.count`。

同步点：`bilibili/api.py` 便捷方法、`modules/up_analyzer/data_fetcher.py`、自诊模块的字段映射。

### `GET /x/web-interface/ranking/v2`

代码入口：`get_ranking(rid, day=7, original=0)`；热点也在 `modules/hotspot/tag_cloud.py` 直接调用。

入参：当前版本使用 `rid`、`day`、`type=all|origin`；热点分页另加 `pn`、`ps`（每页最多 50）。签名现状并不统一：`BilibiliAPI.get_ranking()` 传 `need_sign=False`，`TagCloudGenerator` 的直接调用传 `need_sign=True`。接口变更排查时必须分别验证这两条调用链；除非有实测依据，不要贸然全局统一。

出参重点：`list[]` 的 `aid/bvid/title/owner/stat`。榜单不保证带 tag，标签必须走专用接口。

注意：不要传旧版整数 `type=0/1` 给新版 ranking/v2；`get_ranking()` 已转换为字符串。

## 4. 热点与动态

### `GET /x/tag/archive/tags`

代码入口：`modules/hotspot/tag_cloud.py` 的 `extract_tags_from_videos()`。

入参：视频 `aid`/`bvid`，以当前源码调用处为准；不需要 WBI。

出参：`data` 是标签数组，重点字段 `tag_name`；业务层再合并视频分区名 `tname`。

注意：`/x/web-interface/view` 和 ranking/v2 当前不可靠地提供 tag，不要仅从视频详情取标签。

### `GET /x/activity/page/list`

代码入口：`ActivityTracker.get_official_activities()`。

入参：`pn`、`ps`、`type=0`、`plat=1`；不需要 WBI。缺少 `plat=1` 当前会触发 `-400`。

出参重点：`data.list[]` 的 `id/name/pc_url|h5_url|url/cover/desc/stime/etime/tags`；业务层转为统一活动对象。

### `GET /x/polymer/web-dynamic/v1/feed/space`

代码入口：`get_ugc_account_dynamics(uid, offset=0, limit=10)`。

入参：`host_mid`；offset 大于 0 时才传 `offset`。当前 offset=0 禁止显式传参，否则可能返回 `4101129`。

签名：不需要 WBI；无登录态可能 HTTP 412，优先使用有效 Cookie 池。

出参重点：`data.items[]`，业务层解析 `id_str/type/modules.module_dynamic` 下的文字、topic、major、图片和时间，再过滤活动相关动态。

## 5. 视频与评论

### `GET /x/web-interface/view`

代码入口：`modules/comment/collector.py` 的 `_get_video_oid()` 等。

入参：视频 `bvid`（必要时 aid）；不需要 WBI。

出参重点：视频详情中的 `aid`，它是评论接口的 `oid`；不要假设详情返回完整 `tag`。

### `GET /x/v2/reply/main`

代码入口：`CommentCollector`。

通用入参：`oid=aid`、`type=1`（视频）、`ps=20`、`mode`。热门评论 `mode=3`，时间排序 `mode=2`。

普通评论翻页：从 `data.cursor` 读取 `is_end`、`pagination_reply.next_offset` 或 `cursor.next`，下一页传 `pagination_str={"offset":"..."}` JSON 字符串；不要恢复旧的 `pn` 分页。

出参重点：`data.replies[]`（`rpid/content/member/ctime/like/replies` 等）、`data.cursor`。业务层 `_parse_comment_replies()` 负责清洗为统一字典，再去重、情感分析、入库。

限流：评论专用 `RateLimiter`，默认间隔 4 秒；完整评论可能产生大量请求，必须有上限或明确用户确认。

## 6. 扫码登录接口

### `GET passport.bilibili.com/x/passport-login/web/qrcode/generate`

无登录态；出参重点为 `qrcode_key` 和二维码 URL/图片数据，入口在 `bilibili/auth.py`。

### `GET passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=...`

无登录态；轮询间隔由桌面向导控制，成功时从响应跳转 URL 解析 Cookie。不要把返回 Cookie 写入日志。

### `GET /x/web-interface/nav`（登录态校验）

`validate_cookie()` 通过 nav 判断登录态；`-101` 表示失效。扫码成功后的 Cookie 最终保存为 `ConfigManager` secret 键 `bilibili.cookie`，再由 CookiePool 导入。

## 7. 接口变更同步清单

任何 B 站接口变更，按以下顺序同步：

1. `bilibili/api.py`：URL、参数、`need_sign`、返回包装、错误码和重试策略。
2. 直接业务调用：`modules/hotspot/tag_cloud.py`、`activity_tracker.py`、`modules/comment/collector.py`、`modules/up_analyzer/data_fetcher.py`。
3. 数据映射：数据库 ORM 字段、模块输出字典、Web router 的 JSON 序列化。
4. 限流分类：`get_rate_limiter()` 的 normal/comment/dynamic 类型和 `config/config.yaml`。
5. 认证/迁移：若 Cookie 或 nav 字段变化，同步 `auth.py`、`cookie_pool.py`。
6. 前端契约：`web/routers` 返回字段和 `web/frontend/static/js/app.js`。
7. 文档与验证：更新本文、运行 `/health`、目标接口小样本验证、检查 `data/logs/error.log` 和 `risk_control.log`。

禁止直接改线上 Cookie、跳过限频或用归档脚本验证接口；先备份数据库和配置。
