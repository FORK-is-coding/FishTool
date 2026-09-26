# 常驻任务与调度说明

## 1. 当前事实

项目依赖中有 APScheduler，但源码没有发现 `add_job`、cron 或 scheduler 启动代码。当前“定时任务”由 `core/monitor_service.py` 的 `ResidentCommentMonitor` 使用 asyncio 常驻循环实现，并由 `web/main.py` lifespan 统一启动/关闭。

当前两个循环：

- Cookie 巡检：始终创建 `bili-cookie-check` task，按 CookiePool `check_interval` 周期执行 `check_all_cookies()`。
- 评论常驻监控：仅在配置/持久化状态允许时创建 `bili-comment-monitor` task，对目标 BV 做增量采集。

动态/活动采集没有常驻任务入口，由 WebUI 请求触发。

## 2. 生命周期

```text
uvicorn 启动
 → web.main.lifespan
 → ConfigManager + init_database
 → ResidentCommentMonitor(comment.get_monitor, config)
 → monitor_service.start()
 → Cookie loop
 → 若 monitor.enable / monitor_state enabled，则 comment loop

uvicorn 关闭
 → monitor_service.shutdown()
 → stop_event.set()
 → cancel + await 两个 task
 → 关闭服务
```

不要在 module import 时创建后台 task；reload 模式会重复 import，容易产生重复循环。

## 3. 状态和控制接口

状态保存于 `monitor_state` 单例记录：`enabled/paused/status/target_bvids/last_collect_at/total_collected/last_error/consecutive_failures/updated_at`。

Web 接口：

- `GET /api/comment/resident/status`：状态快照。
- `POST /api/comment/resident/enable`：启用，可更新目标 BV 列表。
- `POST /api/comment/resident/pause`：暂停但保留目标和累计统计。
- `POST /api/comment/resident/stop`：停止 task，保留历史状态。

`monitor_state` 是 WebUI 与后台任务的本地控制面；服务重启后状态仍可展示并按逻辑恢复。

## 4. 间隔与退避

- 评论正常间隔：`monitor.check_interval`，默认 300 秒，代码最小 10 秒，并增加最多约 10% 随机抖动。
- 评论失败：`check_interval * 2^min(failures,4)`，最大 1800 秒；记录 `last_error/consecutive_failures`。
- Cookie 巡检：`bilibili.cookie_check_interval`，默认 1800 秒，循环最小等待 30 秒。
- B站请求自身还受 normal/comment/dynamic 限流和 429 退避控制。任务间隔不能替代接口级限流。

## 5. 修改调度

调整评论周期：修改 `config/config.yaml` 的 `monitor.check_interval`，再调用配置 reload 或重启服务。不要把周期降到低于接口限流和业务处理耗时。

调整 Cookie 周期：修改 `bilibili.cookie_check_interval`；确认 CookiePool 初始化实际读取该项，再重启观察巡检日志。

新增任务必须包含：

1. FastAPI lifespan 中创建和 shutdown 中回收。
2. 幂等 start，避免已有 task 未结束时重复创建。
3. `asyncio.Event` 或 cancel 响应，禁止不可中断的长 sleep。
4. 持久化状态和最近错误，不只保存在内存。
5. 指数退避、随机抖动、单轮超时和接口限流。
6. Session 每轮关闭，避免跨 await 泄漏数据库连接。
7. 状态/启停 API 和至少一个异常恢复测试。

确需 APScheduler 时，先决定单进程约束。当前 SQLite + StaticPool 和内存 Cookie/限流器不适合多个 worker 各自启动同一 scheduler；至少要使用进程锁/外部 job store 和任务唯一身份。

## 6. 运维检查

- `/api/comment/resident/status` 中 `monitor_task_running` 与 `cookie_task_running` 应和预期一致。
- `last_collect_at` 长时间不更新：检查目标 BV、Cookie、`last_error`、风控日志。
- `consecutive_failures` 持续增加：不要立即缩短间隔，应先查接口码、网络、数据库锁和 Cookie。
- reload/桌面重复启动时出现双采集：检查是否有两个 Python/exe 进程监听不同端口；SQLite 状态不能阻止不同进程各自启动任务。
