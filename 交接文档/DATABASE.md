# 数据库结构与维护

数据库默认路径：`data/bili_ops.db`。ORM 定义在 `core/database.py`，当前实际 SQLite 中有 14 张业务表。

## 1. 表清单

| 表 | 主键/唯一约束 | 主要职责 |
|---|---|---|
| `accounts` | `id`; `uid` unique | 绑定账号、主账号/启用状态、资料和检查时间 |
| `cookie_pool` | `id`; FK `account_id` | 加密 Cookie、有效性、失败次数、使用/检查时间 |
| `videos` | `id`; `bvid`、`aid` unique | 视频元数据、作者、分区、互动统计、监控标记 |
| `video_stats` | `id`; FK `video_id` | 视频指标时间快照 |
| `up_masters` | `id`; `mid` unique | UP 资料、粉丝、投稿频率、平均播放/互动、第三方数据 |
| `comments` | `id`; `rpid` unique; FK `video_id` | 评论文本、用户、点赞、情感、垃圾/重复、关键词 |
| `comment_alerts` | `id`; FK `video_id` | 预警类型、级别、阈值、消息、已读/已处理 |
| `hotspots` | `id` | 多来源热点、标签、关键词、热度、趋势、时间窗 |
| `topics` | `id`; FK `hotspot_id` | 选题、状态、优先级、AI 建议、关联视频 |
| `activities` | `id`; `activity_id` unique | 官方/动态活动、时间、奖励、要求、状态 |
| `tasks` | `id` | 异步任务状态、参数、进度、结果、错误、checkpoint |
| `operation_logs` | `id` | 用户操作审计、模块、状态、错误和来源 IP |
| `llm_usage` | `id` | 日期、模型、模块、token 和请求次数统计 |
| `monitor_state` | `id`; `name` unique | 常驻评论监控开关、目标 BV、累计采集和最近错误 |

关系重点：Account 1:N CookiePool、Account 1:N Video、Video 1:N Comment/VideoStats/CommentAlert、Hotspot 1:N Topic。

## 2. 会话与事务

- 启动调用 `init_database()`，内部 `Base.metadata.create_all()`。
- 普通代码通过 `get_session()` 获取 Session，必须 `commit/rollback/close`；FastAPI 依赖可使用 `get_db()`。
- SQLite 使用 `StaticPool` 和 `check_same_thread=False` 以兼容线程访问；这不是多进程数据库方案。
- 写入先利用唯一键做幂等，捕获异常时 rollback。不要长期持有 Session 跨 await 或跨多轮后台循环。

## 3. Cookie 安全

`cookie_pool.cookie_data` 是加密文本。旧明文记录在加载时会尝试自动加密回写；`sessdata/bili_jct/buvid3` 字段存在于当前 schema，维护时同样按敏感数据处理。

Fernet 密钥位于 `config/.key`。仅有数据库而没有原密钥时，已有密文不能解密；备份/迁移必须把数据库、`.key`、`.secrets` 作为同一组，但不得公开分发。

## 4. 备份与恢复

维护前：

```powershell
Copy-Item data\bili_ops.db backups\bili_ops_$(Get-Date -Format yyyyMMdd_HHmmss).db
Copy-Item config\.key backups\config_key_$(Get-Date -Format yyyyMMdd_HHmmss).bak
Copy-Item config\.secrets backups\config_secrets_$(Get-Date -Format yyyyMMdd_HHmmss).bak
```

项目也提供 `DatabaseManager.backup()`。恢复时先停 Web/桌面进程，整体替换数据库与配套密钥文件，再启动 `/health` 和登录态验证。不要在 SQLite 正在写入时直接覆盖文件。

## 5. Schema 变更

`create_all()` 不会给已有表自动加列、改类型或创建迁移历史。变更步骤：

1. 备份数据库和敏感配置。
2. 修改 `core/database.py` ORM。
3. 编写一次性、幂等迁移 SQL/脚本；先在副本执行。
4. 同步写入/查询模块、router 响应、前端字段和本文。
5. 用 `PRAGMA table_info('<table>')` 与关键查询核对。
6. 启动服务，做旧数据读取和新数据写入测试。

SQLite 改复杂约束时使用“新表 → 拷贝数据 → 校验行数/唯一键 → 原子替换”方式，禁止直接删除生产表。

## 6. 常用只读检查

```sql
PRAGMA integrity_check;
SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;
SELECT is_valid, COUNT(*) FROM cookie_pool GROUP BY is_valid;
SELECT status, COUNT(*) FROM tasks GROUP BY status;
SELECT status, target_bvids, total_collected, last_error FROM monitor_state;
SELECT COUNT(*) FROM comments;
SELECT alert_level, COUNT(*) FROM comment_alerts GROUP BY alert_level;
```

排障查询只读优先。任何 DELETE/DROP/UPDATE 前先备份并核对 WHERE 影响范围。
