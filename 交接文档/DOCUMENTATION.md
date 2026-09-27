# 交接文档索引

建议阅读顺序：

1. [HANDOFF.md](HANDOFF.md)：项目总纲、状态、目录、数据流、接手步骤。
2. [ARCHITECTURE.md](ARCHITECTURE.md)：入口、依赖方向、业务边界、扩展落点。
3. [INTERFACES.md](INTERFACES.md)：B站接口参数、返回、WBI、限流和同步清单。
4. [DATABASE.md](DATABASE.md)：14 张表、关系、备份、恢复和 schema 变更。
5. [OPERATIONS.md](OPERATIONS.md)：环境、配置、Web/桌面运行、PyInstaller 打包和升级。
6. [SCHEDULE.md](SCHEDULE.md)：asyncio 常驻任务、状态、周期、退避和新增任务规则。
7. [TROUBLESHOOTING.md](TROUBLESHOOTING.md)：按症状排障。

已有 `README.md`、`QUICKSTART.md`、`STRUCTURE.md` 和 `docs/` 中的历史验收材料可作为背景，但若与上述交接包或当前源码冲突，以当前源码、实际 SQLite schema 和本交接包的明确勘误为准。

安全提醒：`config/.key`、`config/.secrets`、`data/bili_ops.db` 和日志可能含敏感信息，不要上传公共仓库或发送到群聊。
